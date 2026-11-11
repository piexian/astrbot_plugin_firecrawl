"""watch 检查执行：预算请求、变更解析与原会话推送。

plugin 只需提供 executor.execute_sync、request_scope() 与 context.send_message。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from astrbot.api import logger
from astrbot.api.event import MessageChain

from ..firecrawl_api.errors import FirecrawlError
from ..persistence import WatchStore, WatchSubscription
from ..tools.execution import PendingRequestError
from ..tools.result import fail
from .core import (
    WatchCheckResult,
    WatchRequest,
    build_watch_notification,
    build_watch_request,
    parse_watch_response,
)

_RUNTIME_TEXT = "Firecrawl 插件运行时不可用，无法执行检查。"
_STATUS_TEXTS = {
    "new": "检查完成：页面为首次抓取（new），暂无可比内容。",
    "same": "检查完成：页面无变化（same）。",
    "removed": "检查完成：页面已移除（removed）。",
    "unavailable": "检查完成：本次缺少变更跟踪数据（远端超时或不可用），已跳过推送。",
}


class WatchService:
    """执行订阅检查并按结果推进周期、推送变更；每订阅进程内互斥。"""

    def __init__(self, plugin, store: WatchStore, clock=None) -> None:
        """clock 返回 timezone-aware UTC datetime，缺省为当前 UTC 时刻。"""
        self._plugin = plugin
        self._store = store
        self._clock = clock or _utc_now
        self._locks: dict[str, asyncio.Lock] = {}

    def lock_for(self, subscription_id: str) -> asyncio.Lock:
        """返回订阅的进程内互斥锁，防止 cron 唤醒重叠执行。"""
        lock = self._locks.get(subscription_id)
        if lock is None:
            lock = self._locks[subscription_id] = asyncio.Lock()
        return lock

    def build_request(self, url: str, tag: str) -> WatchRequest:
        """从插件配置构造检查请求：域名护栏、PDF 页数与注入检测与工具口径一致。"""
        config = getattr(self._plugin, "config", None) or {}
        return build_watch_request(
            url,
            tag=tag,
            blocked_domains=config.get("blocked_domains", []),
            check_prompt_injection=config.get("check_prompt_injection", False),
            pdf_max_pages=config.get("pdf_max_pages", 20),
        )

    async def check_due(self, subscription_id: str) -> str:
        """cron 唤醒入口：next_check_at 未到期零网络返回，到期执行一次检查。"""
        async with self.lock_for(subscription_id):
            try:
                sub = await self._store.get(subscription_id)
            except KeyError:
                return "订阅不存在或已删除。"
            now = self._clock().isoformat()
            if sub.next_check_at > now:
                return f"未到期：下次检查时间 {sub.next_check_at}。"
            return await self._run_check(sub)

    async def check_now(self, subscription_id: str) -> str:
        """手动立即检查：跳过到期判断，执行后推进周期。"""
        async with self.lock_for(subscription_id):
            try:
                sub = await self._store.get(subscription_id)
            except KeyError:
                return "订阅不存在或已删除。"
            return await self._run_check(sub)

    async def _run_check(self, sub: WatchSubscription) -> str:
        """执行一次真实检查并推进周期；返回面向用户的单行结果。"""
        try:
            request = self.build_request(sub.url, sub.tag)
        except Exception as exc:
            await self._advance(sub.id, None)
            logger.warning("watch 检查请求构造失败：%s", type(exc).__name__)
            return "检查失败：请求配置无效，已顺延下次检查。"
        try:
            async with self._plugin.request_scope():
                response = await self._plugin.executor.execute_sync(
                    "scrape", request.payload, estimate=request.estimate
                )
        except RuntimeError:
            # 关闭中：检查未发生，不推进周期，待恢复后补查。
            return _RUNTIME_TEXT
        except PendingRequestError as exc:
            await self._advance(sub.id, None)
            logger.warning("watch 检查待核对：%s", fail(exc))
            return (
                f"检查完成但结果待核对：预留 {exc.reservation_id}，"
                "请管理员用 /fc pending resolve <预留ID> <实际credits> 结算或确认未受理。"
            )
        except FirecrawlError as exc:
            await self._advance(sub.id, None)
            report = fail(exc)
            payload = json.loads(report)
            logger.warning("watch 检查失败：%s", report)
            return f"检查失败：{payload['error']}{payload['hint']}已顺延下次检查。"
        except Exception as exc:
            await self._advance(sub.id, None)
            logger.warning("watch 检查请求失败：%s", type(exc).__name__)
            return f"检查失败（{type(exc).__name__}），已顺延下次检查。"
        try:
            result = parse_watch_response(response.json())
        except Exception as exc:
            await self._advance(sub.id, None)
            logger.warning("watch 响应解析失败：%s", type(exc).__name__)
            return "检查完成但响应缺少有效变更跟踪数据，已顺延下次检查。"
        await self._advance(sub.id, result.status)
        if result.status == "changed":
            return await self._notify_changed(sub.id, result)
        return _STATUS_TEXTS[result.status]

    async def _advance(self, subscription_id: str, status: str | None) -> None:
        """记录本次检查状态并按周期推进 next_check_at，失败记录日志。"""
        try:
            await self._store.record_check(subscription_id, status)
        except Exception as exc:
            logger.warning("watch 检查状态写入失败：%s", type(exc).__name__)

    async def _notify_changed(
        self, subscription_id: str, result: WatchCheckResult
    ) -> str:
        """推送变更到订阅原会话；send 返回 True 后才保存通知身份。"""
        try:
            sub = await self._store.get(subscription_id)
        except KeyError:
            return "订阅已删除，未推送。"
        if sub.last_notification_hash == result.notification_hash:
            return "检查完成：页面已变更，与上次推送相同，已跳过。"
        message = build_watch_notification(sub.url, result)
        try:
            sent = await self._plugin.context.send_message(
                sub.umo, MessageChain().message(message)
            )
        except Exception as exc:
            sent = False
            logger.warning("watch 推送异常：%s", type(exc).__name__)
        if sent is not True:
            logger.warning("watch 推送未成功，未保存通知身份。")
            return "检查完成：页面已变更，但推送失败，请稍后手动检查重试。"
        try:
            await self._store.save_notification_hash(
                subscription_id, result.notification_hash
            )
        except Exception as exc:
            logger.warning("watch 通知身份保存失败：%s", type(exc).__name__)
        return "检查完成：页面已变更，已推送到订阅会话。"


def _utc_now() -> datetime:
    """默认时钟：当前 UTC 时刻。"""
    return datetime.now(timezone.utc)
