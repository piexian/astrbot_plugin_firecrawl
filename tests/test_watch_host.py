"""WatchHost 调度测试：reload 唯一 job、残留清理、remove 与 stop 语义。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from astrbot_plugin_firecrawl.persistence import WatchStore
from astrbot_plugin_firecrawl.watch.host import WatchHost, _job_name

_UMO = "qq:group:42"
_URL_A = "https://example.com/a"
_URL_B = "https://example.com/b"


class _StubCronManager:
    """记录 basic job 增删的最小 CronManager 替身。"""

    def __init__(self, fail_add: bool = False) -> None:
        self.jobs: dict[str, SimpleNamespace] = {}
        self.handlers: dict[str, object] = {}
        self.deleted: list[str] = []
        self.fail_add = fail_add
        self._counter = 0

    async def add_basic_job(
        self,
        *,
        name,
        cron_expression,
        handler,
        description=None,
        timezone=None,
        payload=None,
        enabled=True,
        persistent=False,
    ):
        if self.fail_add:
            raise RuntimeError("cron unavailable")
        self._counter += 1
        job_id = f"job-{self._counter}"
        self.jobs[job_id] = SimpleNamespace(
            job_id=job_id,
            name=name,
            job_type="basic",
            cron_expression=cron_expression,
            timezone=timezone,
            payload=payload or {},
            persistent=persistent,
            enabled=enabled,
        )
        self.handlers[job_id] = handler
        return self.jobs[job_id]

    async def delete_job(self, job_id: str) -> None:
        self.deleted.append(job_id)
        self.jobs.pop(job_id, None)
        self.handlers.pop(job_id, None)

    async def list_jobs(self, job_type=None):
        return [
            job
            for job in self.jobs.values()
            if job_type is None or job.job_type == job_type
        ]


class _StubContext:
    """只携带 cron_manager 的最小 Context 替身。"""

    def __init__(self, manager: _StubCronManager) -> None:
        self.cron_manager = manager


class _RecordingService:
    """记录 check_due 调用的最小服务替身。"""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.error = error

    async def check_due(self, subscription_id: str) -> str:
        self.calls.append(subscription_id)
        if self.error is not None:
            raise self.error
        return "ok"


async def _make(tmp_path, *, subs=2, manager=None):
    store = WatchStore(tmp_path / "w.db")
    await store.initialize()
    ids = []
    urls = (_URL_A, _URL_B)
    for index in range(subs):
        record = await store.create(
            umo=_UMO, url=urls[index], interval_minutes=5, tag=f"tag-{index}"
        )
        ids.append(record.id)
    service = _RecordingService()
    manager = manager or _StubCronManager()
    context = _StubContext(manager)
    host = WatchHost(context, service, store)
    return store, service, manager, host, ids


@pytest.mark.asyncio
async def test_reload_creates_one_persistent_job_per_subscription(tmp_path):
    store, _service, manager, host, ids = await _make(tmp_path)
    await host.reload()
    assert len(manager.jobs) == 2
    for sub_id in ids:
        matching = [j for j in manager.jobs.values() if j.name == _job_name(sub_id)]
        assert len(matching) == 1
        job = matching[0]
        assert job.cron_expression == "* * * * *"
        assert job.timezone == "UTC"
        assert job.persistent is True
        assert job.payload == {"subscription_id": sub_id}
        assert manager.handlers[job.job_id] == host._on_tick
        assert (await store.get(sub_id)).cron_job_id == job.job_id


@pytest.mark.asyncio
async def test_reload_clears_stale_jobs_and_keeps_foreign(tmp_path):
    manager = _StubCronManager()
    stale = await manager.add_basic_job(
        name=_job_name("ghost-id"),
        cron_expression="* * * * *",
        handler=lambda **kwargs: None,
        persistent=True,
    )
    foreign = await manager.add_basic_job(
        name="other-plugin:job",
        cron_expression="0 * * * *",
        handler=lambda **kwargs: None,
        persistent=True,
    )
    store, _service, _manager, host, ids = await _make(tmp_path, manager=manager)
    await host.reload()
    assert stale.job_id in manager.deleted
    assert foreign.job_id not in manager.deleted
    assert manager.jobs[foreign.job_id].name == "other-plugin:job"
    assert len(manager.jobs) == 3
    for sub_id in ids:
        assert (await store.get(sub_id)).cron_job_id != stale.job_id


@pytest.mark.asyncio
async def test_reload_is_idempotent(tmp_path):
    store, _service, manager, host, ids = await _make(tmp_path)
    await host.reload()
    first_ids = {sub_id: (await store.get(sub_id)).cron_job_id for sub_id in ids}
    await host.reload()
    second_ids = {sub_id: (await store.get(sub_id)).cron_job_id for sub_id in ids}
    assert len(manager.jobs) == 2
    assert set(first_ids.values()) != set(second_ids.values())
    for sub_id in ids:
        names = [j.name for j in manager.jobs.values() if j.name == _job_name(sub_id)]
        assert names == [_job_name(sub_id)]
        assert second_ids[sub_id] in manager.jobs


@pytest.mark.asyncio
async def test_start_binds_job_to_new_subscription(tmp_path):
    store, _service, manager, host, ids = await _make(tmp_path, subs=1)
    await host.start(ids[0])
    assert len(manager.jobs) == 1
    assert (await store.get(ids[0])).cron_job_id == manager.jobs["job-1"].job_id


@pytest.mark.asyncio
async def test_remove_subscription_deletes_job_only(tmp_path):
    store, _service, manager, host, ids = await _make(tmp_path)
    await host.reload()
    await host.remove_subscription(ids[0])
    await host.remove_subscription(ids[0])
    assert len(manager.jobs) == 1
    remaining = next(iter(manager.jobs.values()))
    assert remaining.name == _job_name(ids[1])
    assert (await store.get(ids[0])).cron_job_id is None
    assert (await store.get(ids[1])).cron_job_id == remaining.job_id
    # host 只管调度：订阅记录本身保留，由调用方决定是否删除。
    assert (await store.get(ids[0])).url == _URL_A


@pytest.mark.asyncio
async def test_stop_clears_jobs_but_keeps_subscriptions(tmp_path):
    store, _service, manager, host, ids = await _make(tmp_path)
    await host.reload()
    await host.stop()
    assert manager.jobs == {}
    for sub_id in ids:
        assert (await store.get(sub_id)).cron_job_id is None
        assert await store.get(sub_id) is not None


@pytest.mark.asyncio
async def test_on_tick_invokes_service_and_swallows_errors(tmp_path):
    store, service, manager, host, ids = await _make(tmp_path)
    await host._on_tick(subscription_id=ids[0])
    assert service.calls == [ids[0]]
    service.error = RuntimeError("boom")
    await host._on_tick(subscription_id=ids[0])
    assert service.calls == [ids[0], ids[0]]


@pytest.mark.asyncio
async def test_job_handler_keyword_payload_reaches_service(tmp_path):
    store, service, manager, host, ids = await _make(tmp_path, subs=1)
    await host.reload()
    job = next(iter(manager.jobs.values()))
    handler = manager.handlers[job.job_id]
    await handler(**job.payload)
    assert service.calls == [ids[0]]


@pytest.mark.asyncio
async def test_start_failure_leaves_subscription_unbound(tmp_path):
    store, _service, manager, host, ids = await _make(
        tmp_path, subs=1, manager=_StubCronManager(fail_add=True)
    )
    with pytest.raises(RuntimeError):
        await host.start(ids[0])
    assert (await store.get(ids[0])).cron_job_id is None


@pytest.mark.asyncio
async def test_create_job_cleans_up_when_store_write_fails(tmp_path):
    store, _service, manager, host, ids = await _make(tmp_path, subs=1)

    class FailingStore(WatchStore):
        async def set_cron_job_id(self, subscription_id, cron_job_id):
            raise RuntimeError("db down")

    host._store = FailingStore(tmp_path / "w.db")
    with pytest.raises(RuntimeError):
        await host._create_job(ids[0])
    assert manager.jobs == {}


@pytest.mark.asyncio
async def test_remove_subscription_reports_failure_and_keeps_state(tmp_path):
    store, _service, manager, host, ids = await _make(tmp_path)
    await host.reload()
    job_id = (await store.get(ids[0])).cron_job_id
    original_delete = manager.delete_job

    async def failing_delete(target_job_id):
        raise RuntimeError("cron unavailable")

    manager.delete_job = failing_delete
    ok = await host.remove_subscription(ids[0])
    assert ok is False
    saved = await store.get(ids[0])
    assert saved.cron_job_id == job_id
    assert saved.url == _URL_A
    manager.delete_job = original_delete
    ok = await host.remove_subscription(ids[0])
    assert ok is True
    assert (await store.get(ids[0])).cron_job_id is None
    remaining = [job.name for job in manager.jobs.values()]
    assert remaining == [_job_name(ids[1])]
