"""Agent 任务提交、查询、终态通知与后台轮询。"""

from __future__ import annotations

import asyncio
import math

from astrbot.api import logger
from astrbot.api.event import MessageChain

from ..firecrawl_api.agent import trim_agent_response
from ..firecrawl_api.budget import Reservation
from ..firecrawl_api.errors import FirecrawlError
from ..firecrawl_api.tasks import make_task_handle
from ..tools.execution import PendingRequestError
from .store import AgentStore, AgentTask

_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_DEFAULT_NOTIFY_CHARS = 6000
_NOTIFY_TRUNCATED_NOTE = "\n[通知已截断]"
_ACCEPT_WARNING = "任务已受理，但本地记录写入失败；请使用任务句柄查询，请勿重发。"

_AGENT_CANCELLED_REFUSAL = "Refusal: Error: Agent was cancelled"


def _validate_max_chars(max_chars: int) -> None:
    """max_chars 必须是 >= 1 的整数。"""
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
        raise ValueError("max_chars 必须是 >= 1 的整数")


def _validate_poll_interval(value: object) -> float:
    """poll_interval_seconds 必须是正有限数。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("poll_interval_seconds 必须是正数")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("poll_interval_seconds 必须是正数")
    return number


def _validate_max_consecutive_errors(value: object) -> int:
    """max_consecutive_errors 必须是 >= 1 的整数。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("max_consecutive_errors 必须是正整数")
    return value


def _result_from_projection(trimmed: dict) -> dict | None:
    """从裁剪投影取出可入库的结果；partial 保留为不完整结果，不升格为完整 data。"""
    if "content" not in trimmed:
        return None
    result = {"content": trimmed["content"]}
    if trimmed.get("partial") is True:
        result["partial"] = True
    if trimmed.get("truncated") is True:
        result["truncated"] = True
    return result


def _normalize_agent_projection(trimmed: dict) -> dict:
    """仅当 failed 且 error 精确等于取消拒答时，改为 cancelled 并移除 error。"""
    if (
        trimmed.get("status") != "failed"
        or trimmed.get("error") != _AGENT_CANCELLED_REFUSAL
    ):
        return trimmed
    projection = dict(trimmed)
    projection["status"] = "cancelled"
    projection.pop("error")
    return projection


def build_agent_notification(
    task: AgentTask, *, max_chars: int = _DEFAULT_NOTIFY_CHARS
) -> str:
    """构造终态通知：状态、句柄、预留、费用、失败 error 与正文；总长有界并明确截断。"""
    _validate_max_chars(max_chars)
    status_text = "已取消" if task.status == "cancelled" else task.status
    lines = [
        f"[Firecrawl agent] 状态：{status_text}",
        f"任务句柄：{task.task_handle}",
        f"预留：{task.reservation_id}",
    ]
    if task.credits_used is not None:
        lines.append(f"费用：{task.credits_used}")
    if task.error:
        lines.append(f"错误：{task.error}")
    result = task.result if isinstance(task.result, dict) else None
    if result is not None:
        if result.get("partial") is True:
            lines.append("不完整结果")
        if result.get("truncated") is True:
            lines.append("结果已截断")
    header = "\n".join(lines)
    content = result.get("content") if result is not None else None
    if isinstance(content, str) and content:
        text = f"{header}\n\n{content}"
    else:
        text = header
    if len(text) <= max_chars:
        return text
    note = _NOTIFY_TRUNCATED_NOTE
    if max_chars < len(note):
        raise ValueError("max_chars 容纳不下截断标记")
    return text[: max_chars - len(note)] + note


class AgentService:
    """提交/查询 agent 任务，按 reservation_id 单 worker 轮询终态并通知。"""

    def __init__(
        self,
        plugin,
        store: AgentStore,
        *,
        poll_interval_seconds: float = 30.0,
        max_consecutive_errors: int = 3,
    ) -> None:
        self._plugin = plugin
        self._store = store
        self._poll_interval_seconds = _validate_poll_interval(poll_interval_seconds)
        self._max_consecutive_errors = _validate_max_consecutive_errors(
            max_consecutive_errors
        )
        self._workers: dict[str, asyncio.Task] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._closing = False

    def _lock_for(self, reservation_id: str) -> asyncio.Lock:
        """返回任务的进程内写锁，避免手动查询与后台查询并发写同一记录。"""
        lock = self._locks.get(reservation_id)
        if lock is None:
            lock = self._locks[reservation_id] = asyncio.Lock()
        return lock

    async def submit(self, *, payload: dict, estimate: int, umo: str) -> dict:
        """预留后先写本地 submitting，再提交 /agent；成功或已知句柄的 pending 返回 processing。"""
        reservation_id: str | None = None

        async def after_reserve(reservation: Reservation) -> None:
            nonlocal reservation_id
            await self._store.create(
                reservation_id=reservation.reservation_id,
                umo=umo,
                prompt=payload["prompt"],
            )
            reservation_id = reservation.reservation_id

        try:
            async with self._plugin.request_scope():
                _response, accepted = await self._plugin.executor.submit(
                    "agent",
                    payload,
                    estimate=estimate,
                    after_reserve=after_reserve,
                )
        except PendingRequestError as error:
            if error.task_handle is None:
                raise
            return await self._accept_known_handle(
                error.reservation_id,
                error.task_handle,
                pending=True,
            )
        except FirecrawlError:
            if reservation_id is not None:
                await self._delete_unaccepted_quietly(reservation_id)
            raise

        handle = make_task_handle("agent", accepted.job_id, accepted.key_ref)
        return await self._accept_known_handle(
            accepted.reservation_id, handle, pending=False
        )

    async def query(self, task_handle: str) -> dict:
        """request_scope 内查询一次状态并裁剪；有本地记录时在任务锁内回写。"""
        return await self._query_status(task_handle, enter_scope=True)

    async def cancel(self, task_handle: str) -> dict:
        """同一 request_scope 内先 DELETE 再 query；返回投影并加上 cancel_requested 与句柄。"""
        async with self._plugin.request_scope():
            await self._plugin.executor.cancel(task_handle)
            trimmed = await self._query_status(task_handle, enter_scope=False)
        payload = dict(trimmed)
        payload["cancel_requested"] = True
        payload["task_handle"] = task_handle
        return payload

    async def _query_status(self, task_handle: str, *, enter_scope: bool) -> dict:
        """按句柄查询并裁剪；enter_scope=False 时复用调用方已持有的 request_scope。"""
        try:
            task = await self._store.get_by_handle(task_handle)
        except KeyError:
            return await self._fetch_trimmed(task_handle, enter_scope=enter_scope)
        async with self._lock_for(task.reservation_id):
            trimmed = await self._fetch_trimmed(task_handle, enter_scope=enter_scope)
            await self._record_projection(task, trimmed)
            return trimmed

    async def notify_terminal(self, reservation_id: str) -> bool:
        """向原会话发送终态通知；仅 send_message 返回 True 时标记已通知。"""
        task = await self._store.get(reservation_id)
        if task.status not in _TERMINAL_STATUSES:
            raise ValueError("仅终态可调用")
        if task.notified_at is not None:
            return False
        text = build_agent_notification(task)
        try:
            sent = await self._plugin.context.send_message(
                task.umo, MessageChain().message(text)
            )
        except Exception as exc:
            logger.warning("agent 通知发送异常：%s", type(exc).__name__)
            return False
        if sent is not True:
            return False
        await self._store.mark_notification_sent(reservation_id)
        return True

    async def track(self, task_handle: str) -> None:
        """为本地任务启动唯一后台 worker；已通知终态与关闭后不创建。"""
        if self._closing:
            return
        task = await self._store.get_by_handle(task_handle)
        if task.notified_at is not None:
            return
        if task.task_handle is None:
            return
        existing = self._workers.get(task.reservation_id)
        if existing is not None and not existing.done():
            return
        worker = asyncio.create_task(
            self._run_worker(task.reservation_id, task.task_handle)
        )
        self._workers[task.reservation_id] = worker

    async def recover(self) -> dict[str, int]:
        """恢复 processing 轮询与未通知终态；submitting 无句柄保留不动。"""
        processing = 0
        unnotified = 0
        if self._closing:
            return {"processing": processing, "unnotified": unnotified}
        for task in await self._store.list_active():
            if task.status != "processing" or task.task_handle is None:
                continue
            await self.track(task.task_handle)
            processing += 1
        for task in await self._store.list_unnotified_terminal():
            if task.task_handle is None:
                continue
            await self.track(task.task_handle)
            unnotified += 1
        return {"processing": processing, "unnotified": unnotified}

    async def close(self) -> None:
        """标记 closing，取消并等待本地 worker；不向远端发送 DELETE。"""
        self._closing = True
        workers = list(self._workers.values())
        for worker in workers:
            worker.cancel()
        if not workers:
            return
        results = await asyncio.gather(*workers, return_exceptions=True)
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                continue
            if isinstance(result, BaseException):
                logger.warning("agent 后台任务关闭异常：%s", type(result).__name__)

    async def _pause_after_error(self, consecutive_errors: int) -> bool:
        """连续失败达到上限则应退出；否则按间隔等待后继续。"""
        if consecutive_errors >= self._max_consecutive_errors:
            return True
        await asyncio.sleep(self._poll_interval_seconds)
        return False

    async def _run_worker(self, reservation_id: str, task_handle: str) -> None:
        """先读本地记录：终态直接通知；processing 才 query，返回终态则当轮通知。"""
        consecutive_errors = 0
        try:
            while not self._closing:
                try:
                    task = await self._store.get(reservation_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    consecutive_errors += 1
                    logger.warning("agent 后台读取异常：%s", type(exc).__name__)
                    if await self._pause_after_error(consecutive_errors):
                        return
                    continue

                if task.notified_at is not None:
                    return

                if task.status == "processing":
                    try:
                        trimmed = await self.query(task_handle)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        consecutive_errors += 1
                        logger.warning("agent 后台查询异常：%s", type(exc).__name__)
                        if await self._pause_after_error(consecutive_errors):
                            return
                        continue
                    if trimmed.get("status") == "processing":
                        consecutive_errors = 0
                        await asyncio.sleep(self._poll_interval_seconds)
                        continue

                try:
                    sent = await self.notify_terminal(reservation_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    consecutive_errors += 1
                    logger.warning("agent 后台通知异常：%s", type(exc).__name__)
                    if await self._pause_after_error(consecutive_errors):
                        return
                    continue

                if sent:
                    return

                try:
                    latest = await self._store.get(reservation_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    consecutive_errors += 1
                    logger.warning("agent 后台读取异常：%s", type(exc).__name__)
                    if await self._pause_after_error(consecutive_errors):
                        return
                    continue

                if latest.notified_at is not None:
                    return

                consecutive_errors += 1
                logger.warning("agent 后台通知未成功")
                if await self._pause_after_error(consecutive_errors):
                    return
        finally:
            current = asyncio.current_task()
            if self._workers.get(reservation_id) is current:
                self._workers.pop(reservation_id, None)

    async def _fetch_trimmed(
        self, task_handle: str, *, enter_scope: bool = True
    ) -> dict:
        """status 一次并裁剪，精确取消拒答归一为 cancelled；不写本地记录。"""
        config = getattr(self._plugin, "config", None) or {}
        max_content_chars = config.get("max_content_chars", 8000)
        if enter_scope:
            async with self._plugin.request_scope():
                response = await self._plugin.executor.status(task_handle)
        else:
            response = await self._plugin.executor.status(task_handle)
        trimmed = trim_agent_response(
            response.json(), max_content_chars=max_content_chars
        )
        return _normalize_agent_projection(trimmed)

    async def _accept_known_handle(
        self, reservation_id: str, task_handle: str, *, pending: bool
    ) -> dict:
        """把已知句柄写入本地 processing；写库成功后 track，失败仍返回句柄并附 warning。"""
        payload: dict = {
            "task_handle": task_handle,
            "reservation_id": reservation_id,
            "status": "processing",
        }
        if pending:
            payload["pending"] = True
        try:
            await self._store.mark_accepted(reservation_id, task_handle)
        except Exception as exc:
            logger.warning("agent 本地受理记录写入失败：%s", type(exc).__name__)
            payload["warning"] = _ACCEPT_WARNING
            return payload
        await self.track(task_handle)
        return payload

    async def _record_projection(self, task: AgentTask, trimmed: dict) -> None:
        """按裁剪投影回写本地状态；终态结果保留 partial，不把不完整内容当成完整结果。"""
        status = trimmed["status"]
        if status == "processing":
            await self._store.record_status(task.reservation_id, "processing")
            return
        await self._store.record_status(
            task.reservation_id,
            status,
            result=_result_from_projection(trimmed),
            credits_used=trimmed.get("creditsUsed"),
            error=trimmed.get("error"),
        )

    async def _delete_unaccepted_quietly(self, reservation_id: str) -> None:
        """删除仍为 submitting 且无句柄的本地记录；失败只记异常类型。"""
        try:
            task = await self._store.get(reservation_id)
        except KeyError:
            return
        except Exception as exc:
            logger.warning("agent 未受理任务清理失败：%s", type(exc).__name__)
            return
        if task.status != "submitting" or task.task_handle is not None:
            return
        try:
            await self._store.delete_unaccepted(reservation_id)
        except Exception as exc:
            logger.warning("agent 未受理任务清理失败：%s", type(exc).__name__)
