"""WatchService 检查执行测试：到期、推送、去重、失败路径与并发锁。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from astrbot_plugin_firecrawl.persistence import WatchStore
from astrbot_plugin_firecrawl.watch.core import notification_identity
from astrbot_plugin_firecrawl.watch.service import WatchService

_UMO = "qq:group:42"
_URL = "https://example.com/a"
_TAG = "tag-1"
_T0 = "2026-05-01T08:00:00+00:00"


class _StubExecutor:
    """记录请求并按预设返回响应或异常的最小执行器替身。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, int]] = []
        self.response: dict | None = None
        self.error: Exception | None = None
        self.release: asyncio.Event | None = None
        self.entered = asyncio.Event()

    async def execute_sync(self, kind: str, payload: dict, *, estimate: int):
        self.calls.append((kind, payload, estimate))
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        return SimpleNamespace(json=lambda: self.response)


class _StubPlugin:
    """提供 executor、request_scope 与 send_message 的最小插件替身。"""

    def __init__(self) -> None:
        self.executor = _StubExecutor()
        self.config: dict = {}
        self.closing = False
        self.sent: list[tuple[str, str]] = []
        self.send_error: Exception | None = None
        self.send_result: bool = True

    @asynccontextmanager
    async def request_scope(self):
        if self.closing:
            raise RuntimeError("runtime unavailable")
        yield

    async def send_message(self, umo: str, chain) -> bool:
        self.sent.append((umo, chain.text))
        if self.send_error is not None:
            raise self.send_error
        return self.send_result

    @property
    def context(self):
        return SimpleNamespace(send_message=self.send_message)


class _Clock:
    """可设定与推进的 UTC 时钟。"""

    def __init__(self, start: str) -> None:
        self.now = datetime.fromisoformat(start)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def tracking_response(**overrides):
    """构造带合法 changeTracking 的 scrape 响应，可按字段覆盖。"""
    data = {
        "markdown": "body",
        "changeTracking": {
            "previousScrapeAt": "2026-05-01T00:00:00+00:00",
            "changeStatus": "same",
            "visibility": "visible",
        },
    }
    data.update(overrides)
    return {"success": True, "data": data}


def changed_response():
    """构造 changed 响应。"""
    return tracking_response(
        changeTracking={
            "previousScrapeAt": "2026-05-01T00:00:00+00:00",
            "changeStatus": "changed",
            "visibility": "visible",
            "diff": {"text": "- old\n+ new"},
        }
    )


async def _make(store_path, clock):
    plugin = _StubPlugin()
    store = WatchStore(store_path, clock=clock)
    await store.initialize()
    service = WatchService(plugin, store, clock=clock)
    record = await store.create(umo=_UMO, url=_URL, interval_minutes=5, tag=_TAG)
    return plugin, store, service, record


@pytest.mark.asyncio
async def test_check_due_not_due_makes_no_request(tmp_path):
    clock = _Clock(_T0)
    plugin, store, service, record = await _make(tmp_path / "w.db", clock)
    await store.record_check(record.id, "same")
    clock.advance(minutes=1)
    text = await service.check_due(record.id)
    assert text.startswith("未到期")
    assert plugin.executor.calls == []
    assert (await store.get(record.id)).last_status == "same"


@pytest.mark.asyncio
async def test_check_due_runs_when_due_and_records_status(tmp_path):
    clock = _Clock(_T0)
    plugin, store, service, record = await _make(tmp_path / "w.db", clock)
    plugin.executor.response = tracking_response()
    text = await service.check_due(record.id)
    assert "same" in text
    kind, payload, estimate = plugin.executor.calls[0]
    assert kind == "scrape"
    assert payload["url"] == _URL
    assert payload["formats"][-1] == {
        "type": "changeTracking",
        "modes": ["git-diff"],
        "tag": _TAG,
    }
    assert payload["formats"][0] == "markdown"
    assert estimate == 21
    checked = await store.get(record.id)
    assert checked.last_status == "same"
    assert checked.next_check_at == "2026-05-01T08:05:00+00:00"


@pytest.mark.asyncio
async def test_check_now_bypasses_due_and_pushes_changed(tmp_path):
    clock = _Clock(_T0)
    clock.advance(minutes=2)
    plugin, store, service, record = await _make(tmp_path / "w.db", clock)
    plugin.executor.response = changed_response()
    text = await service.check_now(record.id)
    assert "已推送" in text
    assert plugin.sent == [(record.umo, plugin.sent[0][1])]
    assert "页面已变更" in plugin.sent[0][1]
    assert plugin.sent[0][1].startswith("[Firecrawl watch] 页面已变更")
    saved = await store.get(record.id)
    assert saved.last_status == "changed"
    assert saved.last_notification_hash == notification_identity(
        "2026-05-01T00:00:00+00:00", "- old\n+ new"
    )


@pytest.mark.asyncio
async def test_changed_skips_duplicate_notification_hash(tmp_path):
    clock = _Clock(_T0)
    plugin, store, service, record = await _make(tmp_path / "w.db", clock)
    plugin.executor.response = changed_response()
    await service.check_now(record.id)
    text = await service.check_now(record.id)
    assert "已跳过" in text
    assert len(plugin.sent) == 1


@pytest.mark.asyncio
async def test_send_false_keeps_hash_unsaved(tmp_path):
    clock = _Clock(_T0)
    plugin, store, service, record = await _make(tmp_path / "w.db", clock)
    plugin.executor.response = changed_response()
    plugin.send_result = False
    text = await service.check_now(record.id)
    assert "推送失败" in text
    saved = await store.get(record.id)
    assert saved.last_status == "changed"
    assert saved.last_notification_hash is None


@pytest.mark.asyncio
async def test_send_exception_keeps_hash_unsaved(tmp_path):
    clock = _Clock(_T0)
    plugin, store, service, record = await _make(tmp_path / "w.db", clock)
    plugin.executor.response = changed_response()
    plugin.send_error = RuntimeError("send broke")
    text = await service.check_now(record.id)
    assert "推送失败" in text
    assert (await store.get(record.id)).last_notification_hash is None


@pytest.mark.asyncio
async def test_notify_stops_when_subscription_removed(tmp_path):
    clock = _Clock(_T0)
    plugin, store, service, record = await _make(tmp_path / "w.db", clock)
    from astrbot_plugin_firecrawl.watch.core import parse_watch_response

    result = parse_watch_response(changed_response())
    await store.delete(record.id)
    text = await service._notify_changed(record.id, result)
    assert "已删除" in text
    assert plugin.sent == []


@pytest.mark.asyncio
async def test_request_failure_advances_without_push(tmp_path):
    clock = _Clock(_T0)
    plugin, store, service, record = await _make(tmp_path / "w.db", clock)
    plugin.executor.error = ValueError("boom")
    text = await service.check_now(record.id)
    assert "检查失败" in text and "ValueError" in text
    saved = await store.get(record.id)
    assert saved.last_status is None
    assert saved.next_check_at == "2026-05-01T08:05:00+00:00"
    assert plugin.sent == []


@pytest.mark.asyncio
async def test_bad_response_advances_without_push(tmp_path):
    clock = _Clock(_T0)
    plugin, store, service, record = await _make(tmp_path / "w.db", clock)
    plugin.executor.response = {"success": True}
    text = await service.check_now(record.id)
    assert "变更跟踪数据" in text
    saved = await store.get(record.id)
    assert saved.last_status is None
    assert saved.next_check_at == "2026-05-01T08:05:00+00:00"


@pytest.mark.asyncio
async def test_runtime_unavailable_does_not_advance(tmp_path):
    clock = _Clock(_T0)
    plugin, store, service, record = await _make(tmp_path / "w.db", clock)
    plugin.closing = True
    text = await service.check_now(record.id)
    assert "运行时不可用" in text
    saved = await store.get(record.id)
    assert saved.last_status is None
    assert saved.next_check_at == "2026-05-01T08:00:00+00:00"
    assert plugin.executor.calls == []


@pytest.mark.asyncio
async def test_overlapping_check_runs_once(tmp_path):
    clock = _Clock(_T0)
    plugin, store, service, record = await _make(tmp_path / "w.db", clock)
    plugin.executor.response = tracking_response()
    plugin.executor.release = asyncio.Event()

    first = asyncio.create_task(service.check_due(record.id))
    await asyncio.wait_for(plugin.executor.entered.wait(), timeout=2)
    assert len(plugin.executor.calls) == 1
    second = asyncio.create_task(service.check_due(record.id))
    for _ in range(5):
        await asyncio.sleep(0)
    assert len(plugin.executor.calls) == 1
    for _ in range(5):
        await asyncio.sleep(0)
    assert len(plugin.executor.calls) == 1

    plugin.executor.release.set()
    first_text = await first
    second_text = await second
    assert "same" in first_text
    assert second_text.startswith("未到期")
    assert len(plugin.executor.calls) == 1


@pytest.mark.asyncio
async def test_unknown_subscription_reports_without_network(tmp_path):
    clock = _Clock(_T0)
    plugin, store, service, _record = await _make(tmp_path / "w.db", clock)
    assert "不存在" in await service.check_due("missing")
    assert "不存在" in await service.check_now("missing")
    assert plugin.executor.calls == []


@pytest.mark.asyncio
async def test_build_request_uses_plugin_config(tmp_path):
    clock = _Clock(_T0)
    plugin, _store, service, _record = await _make(tmp_path / "w.db", clock)
    plugin.config = {
        "check_prompt_injection": True,
        "pdf_max_pages": 5,
        "blocked_domains": ["x.com"],
    }
    request = service.build_request(_URL, _TAG)
    assert request.estimate == 10
    assert request.payload["parsers"] == [{"type": "pdf", "maxPages": 5}]
    assert request.payload["checkPromptInjection"] is True
    with pytest.raises(ValueError):
        service.build_request("https://x.com/post", _TAG)


@pytest.mark.asyncio
async def test_invalid_runtime_config_advances_and_reports(tmp_path):
    clock = _Clock(_T0)
    plugin, store, service, record = await _make(tmp_path / "w.db", clock)
    plugin.config = {"pdf_max_pages": "broken"}
    text = await service.check_now(record.id)
    assert "配置无效" in text
    saved = await store.get(record.id)
    assert saved.next_check_at == "2026-05-01T08:05:00+00:00"
    assert plugin.executor.calls == []


@pytest.mark.asyncio
async def test_pending_request_reports_reservation_and_advances(tmp_path):
    from astrbot_plugin_firecrawl.tools.execution import PendingRequestError

    clock = _Clock(_T0)
    plugin, store, service, record = await _make(tmp_path / "w.db", clock)
    plugin.executor.error = PendingRequestError("rid-42")
    text = await service.check_now(record.id)
    assert "rid-42" in text
    assert "pending resolve" in text
    saved = await store.get(record.id)
    assert saved.next_check_at == "2026-05-01T08:05:00+00:00"
    assert plugin.sent == []


@pytest.mark.asyncio
async def test_remote_error_reports_fixed_text_and_advances(tmp_path, caplog):
    import logging

    from astrbot_plugin_firecrawl.firecrawl_api.errors import (
        ErrorCategory,
        FirecrawlError,
    )

    clock = _Clock(_T0)
    plugin, store, service, record = await _make(tmp_path / "w.db", clock)
    plugin.executor.error = FirecrawlError(
        ErrorCategory.USAGE, "secret remote detail", status=400
    )
    with caplog.at_level(logging.WARNING, logger="astrbot.stub"):
        text = await service.check_now(record.id)
    assert "secret remote detail" not in text
    assert "检查失败" in text
    assert "secret remote detail" in caplog.text
    saved = await store.get(record.id)
    assert saved.next_check_at == "2026-05-01T08:05:00+00:00"
