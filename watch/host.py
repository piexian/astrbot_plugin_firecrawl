"""watch 订阅调度：经公开 cron API 为每订阅维护唯一每分钟唤醒 job。"""

from __future__ import annotations

from astrbot.api import logger

from ..persistence import WatchStore
from .service import WatchService

# 本插件 watch job 的名称前缀，reload 清理残留时按此识别。
_JOB_NAME_PREFIX = "firecrawl-watch:"
_CRON_EVERY_MINUTE = "* * * * *"


def _job_name(subscription_id: str) -> str:
    """按订阅 ID 生成固定 job 名称。"""
    return f"{_JOB_NAME_PREFIX}{subscription_id}"


class WatchHost:
    """管理订阅的 cron 生命周期：reload、start、remove、stop；到期内联到 service。"""

    def __init__(self, context, service: WatchService, store: WatchStore) -> None:
        self._context = context
        self._service = service
        self._store = store

    async def reload(self) -> None:
        """清理本插件残留 job 后按全部订阅重建唯一 persistent job。"""
        await self._cleanup_jobs()
        for sub in await self._store.list_all():
            await self._create_job(sub.id)

    async def start(self, subscription_id: str) -> None:
        """为新增订阅创建 cron job 并保存 job_id。"""
        await self._create_job(subscription_id)

    async def remove_subscription(self, subscription_id: str) -> bool:
        """删除订阅的 job 并清除 job_id；全部相关 job 删除成功才返回 True。"""
        try:
            sub = await self._store.get(subscription_id)
        except KeyError:
            sub = None
        ok = True
        if sub is not None and sub.cron_job_id is not None:
            ok = await self._delete_job(sub.cron_job_id)
        ok = await self._delete_jobs_named(subscription_id) and ok
        if ok and sub is not None:
            await self._store.set_cron_job_id(sub.id, None)
        return ok

    async def stop(self) -> None:
        """删除本插件全部订阅 job 并清除持久化 job_id，订阅本身保留。"""
        await self._cleanup_jobs()
        for sub in await self._store.list_all():
            if sub.cron_job_id is not None:
                await self._store.set_cron_job_id(sub.id, None)

    async def _create_job(self, subscription_id: str) -> None:
        """创建 persistent job 并写回 job_id；写回失败时删除新 job 再上抛。"""
        job = await self._context.cron_manager.add_basic_job(
            name=_job_name(subscription_id),
            cron_expression=_CRON_EVERY_MINUTE,
            handler=self._on_tick,
            payload={"subscription_id": subscription_id},
            timezone="UTC",
            persistent=True,
        )
        try:
            await self._store.set_cron_job_id(subscription_id, job.job_id)
        except BaseException:
            await self._delete_job(job.job_id)
            raise

    async def _cleanup_jobs(self) -> None:
        """删除所有本插件名称前缀的 basic job，不触碰其他插件或用户的 job。"""
        manager = self._context.cron_manager
        for job in await manager.list_jobs("basic"):
            if job.name.startswith(_JOB_NAME_PREFIX):
                await self._delete_job(job.job_id)

    async def _delete_jobs_named(self, subscription_id: str) -> bool:
        """按固定名称删除指定订阅的残留 job，返回是否全部成功。"""
        manager = self._context.cron_manager
        ok = True
        for job in await manager.list_jobs("basic"):
            if job.name == _job_name(subscription_id):
                ok = await self._delete_job(job.job_id) and ok
        return ok

    async def _delete_job(self, job_id: str) -> bool:
        """删除单个 job；失败记录日志并返回 False。"""
        try:
            await self._context.cron_manager.delete_job(job_id)
            return True
        except Exception as exc:
            logger.warning("watch cron job 删除失败：%s", type(exc).__name__)
            return False

    async def _on_tick(self, *, subscription_id: str) -> None:
        """cron 每分钟唤醒入口：到期才检查；异常记录日志，不中断调度。"""
        try:
            await self._service.check_due(subscription_id)
        except Exception as exc:
            logger.warning("watch 定时检查异常：%s", type(exc).__name__)
