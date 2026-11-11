"""预算执行器：计费请求预留、落账、结算，任务查询/取消按句柄固定原 Key 直连。"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

import httpx

from ..firecrawl_api.budget import Reservation
from ..firecrawl_api.client import KEY_REF_EXTENSION, FirecrawlClient
from ..firecrawl_api.errors import FirecrawlError
from ..firecrawl_api.site import make_task_handle, parse_task_handle
from ..persistence import SQLiteStore

# 本执行器支持的同步 kind 到固定 POST 端点。
_ENDPOINTS = {"search": "/search", "scrape": "/scrape", "map": "/map"}

# 本执行器支持的异步提交 kind 到固定 POST 端点。
_SUBMIT_ENDPOINTS = {"crawl": "/crawl", "batch": "/batch/scrape"}

# 任务句柄操作到固定 HTTP 方法：status 用 GET，cancel 用 DELETE。
_TASK_METHODS = {"status": "GET", "cancel": "DELETE"}

# 任务终态：仅这三个字符串状态触发费用对账。
_TASK_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})

_PENDING_HINT = "请勿重发；请管理员核对预算预留的实际受理与费用。"
_TASK_PENDING_HINT = "任务已受理，请勿重发；请先用 status 查询该任务句柄。"


class PendingRequestError(Exception):
    """pending 异常：携带预留 ID 与可选任务句柄，供核对实际受理状态。"""

    def __init__(self, reservation_id: str, *, task_handle: str | None = None) -> None:
        self.reservation_id = reservation_id
        self.task_handle = task_handle
        if task_handle is None:
            super().__init__(f"请求 {reservation_id} 结果不确定，{_PENDING_HINT}")
        else:
            super().__init__(f"请求 {reservation_id} 结果不确定，{_TASK_PENDING_HINT}")


def _fingerprint(kind: str, endpoint: str, payload: dict) -> str:
    """对 kind/方法/端点/payload 的规范 JSON 计算完整 SHA-256 指纹。"""
    canonical = json.dumps(
        {"kind": kind, "method": "POST", "endpoint": endpoint, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _actual_credits(body: Any, estimate: int) -> float:
    """读取成功响应顶层 creditsUsed，字段缺省按预估，错误值抛 ValueError。"""
    if not isinstance(body, dict):
        raise ValueError("成功响应体必须是 JSON 对象")
    if "creditsUsed" not in body:
        return float(estimate)
    value = body["creditsUsed"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("creditsUsed 必须是数字")
    try:
        value = float(value)
    except OverflowError as exc:
        raise ValueError("creditsUsed 必须是有限非负数") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError("creditsUsed 必须是有限非负数")
    return value


def _task_status_body(response: httpx.Response) -> dict:
    """校验任务响应为带字符串 status 的 JSON 对象，破损形状抛固定 ValueError。"""
    try:
        body = response.json()
    except ValueError as exc:
        raise ValueError("任务状态响应必须是 JSON 对象") from exc
    if not isinstance(body, dict):
        raise ValueError("任务状态响应必须是 JSON 对象")
    if not isinstance(body.get("status"), str):
        raise ValueError("任务状态响应缺少有效的 status 字符串")
    return body


class BudgetedExecutor:
    """预算化执行器：计费请求走预留→落账→结算，任务操作按句柄固定原 Key，原样返回 Response。"""

    def __init__(self, client: FirecrawlClient, store: SQLiteStore) -> None:
        self._client = client
        self._store = store

    async def execute_sync(
        self, kind: str, payload: dict, *, estimate: int
    ) -> httpx.Response:
        """以固定 POST 端点执行一次计费请求，返回原始 httpx.Response。"""
        endpoint = _ENDPOINTS.get(kind)
        if endpoint is None:
            raise ValueError("不支持的 kind，仅支持 search、scrape、map。")
        response, rid = await self._send_budgeted(kind, endpoint, payload, estimate)

        try:
            actual = _actual_credits(response.json(), estimate)
        except ValueError as exc:
            await self._store.mark_indeterminate(rid)
            raise PendingRequestError(rid) from exc

        try:
            await self._store.accept(rid)
            await self._store.settle(rid, actual)
        except Exception as exc:
            # 写库中断：保留数据库真实状态，交由 recover 与管理员收敛。
            raise PendingRequestError(rid) from exc

        return response

    async def submit(
        self, kind: str, payload: dict, *, estimate: int
    ) -> tuple[httpx.Response, Reservation]:
        """提交 crawl/batch 异步任务，返回原始响应与 accepted 预留，本次不结算费用。"""
        endpoint = _SUBMIT_ENDPOINTS.get(kind)
        if endpoint is None:
            raise ValueError("不支持的 kind，仅支持 crawl、batch。")
        response, rid = await self._send_budgeted(kind, endpoint, payload, estimate)

        try:
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("成功响应体必须是 JSON 对象")
            firecrawl_id = body.get("id")
            task_handle = make_task_handle(
                kind, firecrawl_id, response.extensions[KEY_REF_EXTENSION]
            )
        except ValueError as exc:
            await self._store.mark_indeterminate(rid)
            raise PendingRequestError(rid) from exc

        try:
            accepted = await self._store.accept(rid, job_id=firecrawl_id)
        except Exception as exc:
            # 写库中断：保留真实 submitting 状态，句柄已可查询，不重发。
            raise PendingRequestError(rid, task_handle=task_handle) from exc

        return response, accepted

    async def status(self, task_handle: str) -> httpx.Response:
        """按任务句柄用原 Key GET 查询，终态时按确认的实际费用对账后原样返回。"""
        response = await self._send_task("status", task_handle)
        body = _task_status_body(response)
        if body["status"] not in _TASK_TERMINAL_STATUSES:
            return response
        await self._reconcile_task(task_handle, body)
        return response

    async def cancel(self, task_handle: str) -> httpx.Response:
        """按任务句柄用原 Key 以 DELETE 取消任务，原样返回 httpx.Response。"""
        return await self._send_task("cancel", task_handle)

    async def _reconcile_task(self, task_handle: str, body: dict) -> None:
        """终态对账：精确匹配唯一待决预留，按顶层 creditsUsed 结算。"""
        parsed = parse_task_handle(task_handle)
        matches = [
            reservation
            for reservation in await self._store.list_pending()
            if reservation.kind == parsed["kind"]
            and reservation.job_id == parsed["firecrawl_id"]
            and reservation.key_ref == parsed["key_ref"]
        ]
        if not matches:
            return
        if len(matches) > 1:
            raise ValueError("任务句柄匹配到多笔待决预留，拒绝结算")
        reservation = matches[0]
        rid = reservation.reservation_id
        try:
            if "creditsUsed" not in body:
                raise ValueError("任务终态响应缺少顶层 creditsUsed")
            actual = _actual_credits(body, reservation.estimate)
        except ValueError as exc:
            raise PendingRequestError(rid, task_handle=task_handle) from exc
        try:
            await self._store.settle(rid, actual)
        except Exception as exc:
            # 写库中断：保留数据库真实状态，不自动重试不重发。
            raise PendingRequestError(rid, task_handle=task_handle) from exc

    async def _send_task(self, operation: str, task_handle: str) -> httpx.Response:
        """解析句柄后按固定方法与原 Key 发送一次任务请求，不预留不结算。"""
        parsed = parse_task_handle(task_handle)
        method = _TASK_METHODS[operation]
        endpoint = f"{_SUBMIT_ENDPOINTS[parsed['kind']]}/{parsed['firecrawl_id']}"
        return await self._client.request(method, endpoint, key_ref=parsed["key_ref"])

    async def _send_budgeted(
        self, kind: str, endpoint: str, payload: dict, estimate: int
    ) -> tuple[httpx.Response, str]:
        """预留→发送前落账→发送→失败分流，返回原始响应与预留 ID。"""
        fingerprint = _fingerprint(kind, endpoint, payload)
        reservation = await self._store.reserve(
            estimate, kind=kind, request_fingerprint=fingerprint
        )
        rid = reservation.reservation_id
        entered = False
        submitting = False

        async def before_send(
            key_ref: str | None, confirmed_not_accepted: bool
        ) -> None:
            """复用客户端发送前钩子：持久化 submitting 与实际 Key 摘要。"""
            nonlocal entered, submitting
            entered = True
            # 每次发送前重置：本次落账失败不得当作已发送。
            submitting = False
            await self._store.mark_submitting(
                rid, key_ref, confirmed_not_accepted=confirmed_not_accepted
            )
            submitting = True

        try:
            response = await self._client.request(
                "POST", endpoint, json=payload, before_send=before_send
            )
        except Exception as exc:
            if not entered:
                # 零发送前置拒绝：释放预留并原样抛出。
                await self._store.release(rid)
                raise
            if not submitting:
                # 发送前回调持久化失败：保留最后持久化状态，原样传播。
                raise
            if isinstance(exc, FirecrawlError) and exc.rotate_key:
                # 最终换 Key 拒绝：确认未受理，释放并原样抛出。
                await self._store.release(rid, confirmed_not_accepted=True)
                raise
            await self._store.mark_indeterminate(rid)
            raise PendingRequestError(rid) from exc

        return response, rid
