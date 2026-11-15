"""AgentService 测试：提交顺序、查询投影、终态通知与日志边界。"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from astrbot_plugin_firecrawl.agent.service import (
    AgentService,
    build_agent_notification,
)
from astrbot_plugin_firecrawl.agent.store import AgentStore, AgentTask
from astrbot_plugin_firecrawl.firecrawl_api.budget import (
    BudgetPolicy,
    Reservation,
    ReservationState,
)
from astrbot_plugin_firecrawl.firecrawl_api.client import FirecrawlClient
from astrbot_plugin_firecrawl.firecrawl_api.errors import ErrorCategory, FirecrawlError
from astrbot_plugin_firecrawl.firecrawl_api.keys import KeyPool, key_reference
from astrbot_plugin_firecrawl.firecrawl_api.tasks import make_task_handle
from astrbot_plugin_firecrawl.persistence import SQLiteStore
from astrbot_plugin_firecrawl.tools.execution import (
    BudgetedExecutor,
    PendingRequestError,
)

_STAMP = "2026-01-01T12:00:00+00:00"
_RID = "r-1"
_JOB_ID = "job-1"
_KEY_REF = format(1, "064x")
_HANDLE = make_task_handle("agent", _JOB_ID, _KEY_REF)
_UMO = "qq:group:1"
_PROMPT = "调研 X"
_PAYLOAD = {"prompt": _PROMPT, "maxCredits": 30}
_K1 = "fc-aaaaaaaa1111"
_SECRET_KEY = "fc-real-key-999"
_SECRET_URL = "https://api.firecrawl.dev/v2/agent/secret-job"
_SECRET_BODY = "SECRET-AGENT-BODY"

_LOG_SECRETS = (_SECRET_KEY, _SECRET_URL, _SECRET_BODY, "SECRET")


def fixed_clock() -> datetime:
    """固定 UTC 时刻。"""
    return datetime.fromisoformat(_STAMP)


class _TickClock:
    """可手动推进的 timezone-aware clock。"""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> None:
        self._now += timedelta(**kwargs)


def _reservation(
    *,
    reservation_id: str = _RID,
    state: ReservationState = ReservationState.RESERVED,
    job_id: str | None = None,
    key_ref: str | None = None,
    estimate: int = 30,
) -> Reservation:
    """构造测试用预留记录。"""
    return Reservation(
        reservation_id=reservation_id,
        utc_day="2026-01-01",
        estimate=estimate,
        kind="agent",
        request_fingerprint="a" * 64,
        created_at=_STAMP,
        updated_at=_STAMP,
        state=state,
        key_ref=key_ref,
        job_id=job_id,
    )


class FakeExecutor:
    """记录 after_reserve/网络顺序，并按预设返回或抛出。"""

    def __init__(self, store: AgentStore) -> None:
        self.store = store
        self.events: list[str] = []
        self.submit_calls: list[tuple] = []
        self.status_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self.ops: list[str] = []
        self.snapshot = None
        self.error: BaseException | None = None
        self.fail_before_after_reserve = False
        self.status_error: BaseException | None = None
        self.cancel_error: BaseException | None = None
        self.cancel_body: dict = {"success": True}
        self.status_body: dict = {"status": "processing"}
        self.status_queue: list | None = None
        self.status_started: asyncio.Queue[str] = asyncio.Queue()
        self.status_block: asyncio.Event | None = None
        self.reserved = _reservation()
        self.accepted = _reservation(
            state=ReservationState.ACCEPTED,
            job_id=_JOB_ID,
            key_ref=_KEY_REF,
        )

    async def submit(self, kind, payload, *, estimate, after_reserve=None):
        self.submit_calls.append((kind, payload, estimate, after_reserve is not None))
        if self.error is not None and self.fail_before_after_reserve:
            raise self.error
        if after_reserve is not None:
            await after_reserve(self.reserved)
            self.snapshot = await self.store.get(self.reserved.reservation_id)
        self.events.append("network")
        if self.error is not None:
            raise self.error
        return SimpleNamespace(json=lambda: {"id": _JOB_ID}), self.accepted

    async def status(self, task_handle: str):
        self.status_calls.append(task_handle)
        self.ops.append("status")
        await self.status_started.put(task_handle)
        if self.status_block is not None:
            await self.status_block.wait()
        if self.status_error is not None:
            raise self.status_error
        if self.status_queue:
            body = self.status_queue.pop(0)
        else:
            body = self.status_body
        if isinstance(body, BaseException):
            raise body
        return SimpleNamespace(json=lambda: dict(body))

    async def cancel(self, task_handle: str):
        self.cancel_calls.append(task_handle)
        self.ops.append("cancel")
        if self.cancel_error is not None:
            raise self.cancel_error
        return SimpleNamespace(json=lambda: dict(self.cancel_body))


class StubPlugin:
    """提供 executor、request_scope、send_message 与 config 的最小插件替身。"""

    def __init__(self, executor, config: dict | None = None) -> None:
        self.executor = executor
        self.config = config or {"max_content_chars": 8000}
        self.scope_entries = 0
        self.sent: list[tuple[str, str]] = []
        self.send_error: Exception | None = None
        self.send_result: bool | None = True

    @asynccontextmanager
    async def request_scope(self):
        self.scope_entries += 1
        yield

    async def send_message(self, umo: str, chain) -> bool | None:
        self.sent.append((umo, chain.text))
        if self.send_error is not None:
            raise self.send_error
        return self.send_result

    @property
    def context(self):
        return SimpleNamespace(send_message=self.send_message)


async def ready(tmp_path, *, clock=None, config=None, **service_kw):
    """真实 AgentStore + Fake executor 的服务组合。"""
    store = AgentStore(tmp_path / "agent.db", clock=clock or fixed_clock)
    await store.initialize()
    executor = FakeExecutor(store)
    plugin = StubPlugin(executor, config=config)
    return plugin, store, AgentService(plugin, store, **service_kw), executor


def make_transport(responses: list, on_request=None):
    """按顺序回放响应，并记录请求。"""
    requests: list[httpx.Request] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if on_request is not None:
            on_request(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.MockTransport(handler), requests


async def ready_real(
    tmp_path, responses: list, on_request=None, *, config=None, **service_kw
):
    """真实 AgentStore + BudgetedExecutor + MockTransport。"""
    transport, requests = make_transport(responses, on_request)
    client = FirecrawlClient(KeyPool([_K1]), transport=transport)
    db = tmp_path / "firecrawl.db"
    budget = SQLiteStore(db, policy=BudgetPolicy(), clock=fixed_clock)
    await budget.initialize()
    store = AgentStore(db, clock=fixed_clock)
    await store.initialize()
    executor = BudgetedExecutor(client, budget)
    plugin = StubPlugin(executor, config=config)
    return plugin, store, AgentService(plugin, store, **service_kw), executor, requests


async def seed_accepted(store: AgentStore, *, rid=_RID, handle=_HANDLE) -> AgentTask:
    """写入一条 processing 任务。"""
    await store.create(reservation_id=rid, umo=_UMO, prompt=_PROMPT)
    return await store.mark_accepted(rid, handle)


async def seed_terminal(
    store: AgentStore,
    status: str,
    *,
    result=None,
    credits_used=1,
    error=None,
    rid=_RID,
    handle=_HANDLE,
) -> AgentTask:
    """写入一条终态任务。"""
    await seed_accepted(store, rid=rid, handle=handle)
    return await store.record_status(
        rid, status, result=result, credits_used=credits_used, error=error
    )


def sample_task(**overrides) -> AgentTask:
    """构造通知用的终态任务。"""
    fields = {
        "reservation_id": _RID,
        "umo": _UMO,
        "prompt": _PROMPT,
        "status": "completed",
        "task_handle": _HANDLE,
        "result": {"content": "正文"},
        "credits_used": 2.5,
        "error": None,
        "notified_at": None,
        "created_at": _STAMP,
        "updated_at": _STAMP,
    }
    fields.update(overrides)
    return AgentTask(**fields)


def logged_text(caplog) -> str:
    """合并 astrbot.stub 日志文本。"""
    return "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == "astrbot.stub"
    )


def assert_log_safe(caplog) -> None:
    """日志不含 Key、正文或完整 URL。"""
    text = logged_text(caplog)
    for secret in _LOG_SECRETS:
        assert secret not in text
    assert _PROMPT not in text
    assert _HANDLE not in text


def test_init_rejects_non_positive_poll_settings():
    with pytest.raises(ValueError, match="poll_interval_seconds"):
        AgentService(None, None, poll_interval_seconds=0)
    with pytest.raises(ValueError, match="poll_interval_seconds"):
        AgentService(None, None, poll_interval_seconds=-1.0)
    with pytest.raises(ValueError, match="poll_interval_seconds"):
        AgentService(None, None, poll_interval_seconds=True)
    with pytest.raises(ValueError, match="max_consecutive_errors"):
        AgentService(None, None, max_consecutive_errors=0)
    with pytest.raises(ValueError, match="max_consecutive_errors"):
        AgentService(None, None, max_consecutive_errors=True)


@pytest.mark.asyncio
async def test_submit_creates_local_record_before_network(tmp_path):
    plugin, store, service, executor = await ready(tmp_path)
    try:
        result = await service.submit(payload=_PAYLOAD, estimate=30, umo=_UMO)

        assert executor.events == ["network"]
        assert executor.snapshot is not None
        assert executor.snapshot.status == "submitting"
        assert executor.snapshot.task_handle is None
        assert executor.snapshot.reservation_id == _RID
        assert executor.snapshot.umo == _UMO
        assert executor.snapshot.prompt == _PROMPT
        assert executor.submit_calls == [("agent", _PAYLOAD, 30, True)]
        assert result == {
            "task_handle": _HANDLE,
            "reservation_id": _RID,
            "status": "processing",
        }
        recorded = await store.get(_RID)
        assert recorded.status == "processing"
        assert recorded.task_handle == _HANDLE
        assert _RID in service._workers
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_submit_success_with_real_executor_create_before_network(tmp_path):
    events: list[str] = []
    plugin, store, service, _executor, requests = await ready_real(
        tmp_path,
        [
            httpx.Response(200, json={"success": True, "id": _JOB_ID}),
            httpx.Response(200, json={"success": True, "status": "processing"}),
        ],
        on_request=lambda request: (
            events.append("network") if request.method == "POST" else None
        ),
    )
    original = store.create

    async def tracking_create(**kwargs):
        assert requests == []
        events.append("create")
        return await original(**kwargs)

    store.create = tracking_create
    handle = make_task_handle("agent", _JOB_ID, key_reference(_K1))
    try:
        result = await service.submit(payload=_PAYLOAD, estimate=30, umo=_UMO)

        assert events == ["create", "network"]
        assert result["task_handle"] == handle
        assert result["status"] == "processing"
        assert "pending" not in result
        recorded = await store.get(result["reservation_id"])
        assert recorded.status == "processing"
        assert recorded.task_handle == handle
        assert recorded.prompt == _PROMPT
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_submit_pending_with_handle_marks_accepted(tmp_path):
    plugin, store, service, executor = await ready(tmp_path)
    executor.error = PendingRequestError(_RID, task_handle=_HANDLE)
    try:
        result = await service.submit(payload=_PAYLOAD, estimate=30, umo=_UMO)

        assert result == {
            "task_handle": _HANDLE,
            "reservation_id": _RID,
            "status": "processing",
            "pending": True,
        }
        recorded = await store.get(_RID)
        assert recorded.status == "processing"
        assert recorded.task_handle == _HANDLE
        assert _RID in service._workers
        assert executor.submit_calls == [("agent", _PAYLOAD, 30, True)]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_submit_pending_without_handle_reraises(tmp_path):
    _plugin, store, service, executor = await ready(tmp_path)
    error = PendingRequestError(_RID)
    executor.error = error
    with pytest.raises(PendingRequestError) as excinfo:
        await service.submit(payload=_PAYLOAD, estimate=30, umo=_UMO)
    assert excinfo.value is error
    recorded = await store.get(_RID)
    assert recorded.status == "submitting"
    assert recorded.task_handle is None


@pytest.mark.asyncio
async def test_submit_firecrawl_error_deletes_unaccepted(tmp_path):
    _plugin, store, service, executor = await ready(tmp_path)
    executor.error = FirecrawlError(ErrorCategory.AUTH, "unauthorized", rotate_key=True)
    with pytest.raises(FirecrawlError):
        await service.submit(payload=_PAYLOAD, estimate=30, umo=_UMO)
    with pytest.raises(KeyError):
        await store.get(_RID)


@pytest.mark.asyncio
async def test_submit_firecrawl_error_before_create_does_not_delete(tmp_path):
    _plugin, store, service, executor = await ready(tmp_path)
    await store.create(reservation_id="other", umo=_UMO, prompt=_PROMPT)
    executor.fail_before_after_reserve = True
    executor.error = FirecrawlError(ErrorCategory.USAGE, "bad request")
    with pytest.raises(FirecrawlError):
        await service.submit(payload=_PAYLOAD, estimate=30, umo=_UMO)
    assert (await store.get("other")).status == "submitting"
    with pytest.raises(KeyError):
        await store.get(_RID)


@pytest.mark.asyncio
async def test_submit_cleanup_failure_preserves_firecrawl_error(tmp_path, caplog):
    _plugin, store, service, executor = await ready(tmp_path)
    executor.error = FirecrawlError(ErrorCategory.AUTH, "unauthorized")

    async def boom(_reservation_id):
        raise RuntimeError(f"{_SECRET_BODY} {_SECRET_KEY} {_SECRET_URL}")

    store.delete_unaccepted = boom
    with caplog.at_level(logging.WARNING, logger="astrbot.stub"):
        with pytest.raises(FirecrawlError) as excinfo:
            await service.submit(payload=_PAYLOAD, estimate=30, umo=_UMO)
    assert excinfo.value.category is ErrorCategory.AUTH
    assert (await store.get(_RID)).status == "submitting"
    text = logged_text(caplog)
    assert "RuntimeError" in text
    assert_log_safe(caplog)


@pytest.mark.asyncio
async def test_submit_unknown_exception_does_not_delete(tmp_path):
    _plugin, store, service, executor = await ready(tmp_path)
    executor.error = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        await service.submit(payload=_PAYLOAD, estimate=30, umo=_UMO)
    recorded = await store.get(_RID)
    assert recorded.status == "submitting"
    assert recorded.task_handle is None


@pytest.mark.asyncio
async def test_submit_mark_accepted_failure_still_returns_handle(tmp_path, caplog):
    plugin, store, service, executor = await ready(tmp_path)

    async def boom(_reservation_id, _task_handle):
        raise RuntimeError(f"{_SECRET_BODY} {_SECRET_KEY} {_SECRET_URL}")

    store.mark_accepted = boom
    with caplog.at_level(logging.WARNING, logger="astrbot.stub"):
        result = await service.submit(payload=_PAYLOAD, estimate=30, umo=_UMO)
    assert result["task_handle"] == _HANDLE
    assert result["reservation_id"] == _RID
    assert result["status"] == "processing"
    assert "warning" in result
    assert "本地记录写入失败" in result["warning"]
    assert "pending" not in result
    recorded = await store.get(_RID)
    assert recorded.status == "submitting"
    assert recorded.task_handle is None
    assert executor.status_calls == []
    assert service._workers == {}
    assert "RuntimeError" in logged_text(caplog)
    assert_log_safe(caplog)


@pytest.mark.asyncio
async def test_submit_pending_mark_accepted_failure_keeps_handle(tmp_path, caplog):
    _plugin, store, service, executor = await ready(tmp_path)
    executor.error = PendingRequestError(_RID, task_handle=_HANDLE)

    async def boom(_reservation_id, _task_handle):
        raise OSError("disk full")

    store.mark_accepted = boom
    with caplog.at_level(logging.WARNING, logger="astrbot.stub"):
        result = await service.submit(payload=_PAYLOAD, estimate=30, umo=_UMO)
    assert result["pending"] is True
    assert result["task_handle"] == _HANDLE
    assert "warning" in result
    assert "OSError" in logged_text(caplog)
    assert service._workers == {}


@pytest.mark.asyncio
async def test_submit_cancelled_error_propagates(tmp_path):
    _plugin, store, service, executor = await ready(tmp_path)
    executor.error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await service.submit(payload=_PAYLOAD, estimate=30, umo=_UMO)
    recorded = await store.get(_RID)
    assert recorded.status == "submitting"
    assert recorded.task_handle is None


@pytest.mark.asyncio
async def test_query_processing_records_heartbeat(tmp_path):
    clock = _TickClock(datetime.fromisoformat(_STAMP))
    plugin, store, service, executor = await ready(tmp_path, clock=clock)
    first = await seed_accepted(store)
    clock.advance(minutes=2)
    executor.status_body = {"status": "processing"}
    trimmed = await service.query(_HANDLE)
    assert trimmed == {"status": "processing"}
    recorded = await store.get(_RID)
    assert recorded.status == "processing"
    assert recorded.result is None
    assert recorded.updated_at == "2026-01-01T12:02:00+00:00"
    assert recorded.updated_at != first.updated_at
    assert executor.status_calls == [_HANDLE]
    assert plugin.scope_entries == 1
    assert plugin.sent == []


@pytest.mark.asyncio
async def test_query_completed_records_actual_credits_and_content(tmp_path):
    plugin, store, service, executor = await ready(tmp_path)
    await seed_accepted(store)
    data = {"名": "值", "n": 1}
    executor.status_body = {
        "status": "completed",
        "creditsUsed": 2.5,
        "data": data,
    }
    trimmed = await service.query(_HANDLE)
    content = json.dumps(data, ensure_ascii=False, sort_keys=True)
    assert trimmed["status"] == "completed"
    assert trimmed["creditsUsed"] == 2.5
    assert trimmed["content"] == content
    assert "partial" not in trimmed
    recorded = await store.get(_RID)
    assert recorded.status == "completed"
    assert recorded.credits_used == 2.5
    assert recorded.result == {"content": content}
    assert recorded.error is None
    assert plugin.sent == []
    assert plugin.executor.submit_calls == []


@pytest.mark.asyncio
async def test_query_failed_partial_is_not_stored_as_complete(tmp_path):
    plugin, store, service, executor = await ready(tmp_path)
    await seed_accepted(store)
    executor.status_body = {
        "status": "failed",
        "creditsUsed": 1,
        "error": "boom",
        "partial": {"b": 2, "a": 1},
    }
    trimmed = await service.query(_HANDLE)
    assert trimmed["status"] == "failed"
    assert trimmed["partial"] is True
    assert trimmed["content"] == '{"a": 1, "b": 2}'
    assert trimmed["error"] == "boom"
    recorded = await store.get(_RID)
    assert recorded.status == "failed"
    assert recorded.error == "boom"
    assert recorded.result == {"content": '{"a": 1, "b": 2}', "partial": True}
    assert recorded.result.get("partial") is True


@pytest.mark.asyncio
async def test_query_cancelled_records_status(tmp_path):
    _plugin, store, service, executor = await ready(tmp_path)
    await seed_accepted(store)
    executor.status_body = {"status": "cancelled", "creditsUsed": 0}
    trimmed = await service.query(_HANDLE)
    assert trimmed == {"status": "cancelled", "creditsUsed": 0}
    recorded = await store.get(_RID)
    assert recorded.status == "cancelled"
    assert recorded.credits_used == 0.0
    assert recorded.result is None
    assert recorded.error is None


@pytest.mark.asyncio
async def test_query_cancel_refusal_records_cancelled_without_error(tmp_path):
    plugin, store, service, executor = await ready(tmp_path)
    await seed_accepted(store)
    executor.status_body = {
        "status": "failed",
        "creditsUsed": 0,
        "error": "Refusal: Error: Agent was cancelled",
    }
    trimmed = await service.query(_HANDLE)
    assert trimmed == {"status": "cancelled", "creditsUsed": 0}
    assert "error" not in trimmed
    recorded = await store.get(_RID)
    assert recorded.status == "cancelled"
    assert recorded.error is None
    assert recorded.credits_used == 0.0
    assert recorded.result is None
    assert plugin.sent == []


@pytest.mark.asyncio
async def test_query_ordinary_failed_keeps_error(tmp_path):
    plugin, store, service, executor = await ready(tmp_path)
    await seed_accepted(store)
    executor.status_body = {
        "status": "failed",
        "creditsUsed": 1,
        "error": "boom",
    }
    trimmed = await service.query(_HANDLE)
    assert trimmed["status"] == "failed"
    assert trimmed["error"] == "boom"
    recorded = await store.get(_RID)
    assert recorded.status == "failed"
    assert recorded.error == "boom"
    assert recorded.credits_used == 1.0
    text = build_agent_notification(recorded)
    assert "状态：failed" in text
    assert "错误：boom" in text


@pytest.mark.asyncio
async def test_query_truncates_content_and_stores_flag(tmp_path):
    plugin, store, service, executor = await ready(
        tmp_path, config={"max_content_chars": 10}
    )
    await seed_accepted(store)
    data = {"k": "x" * 50}
    full = json.dumps(data, ensure_ascii=False, sort_keys=True)
    executor.status_body = {
        "status": "completed",
        "creditsUsed": 3,
        "data": data,
    }
    trimmed = await service.query(_HANDLE)
    assert trimmed["content"] == full[:10]
    assert trimmed["truncated"] is True
    recorded = await store.get(_RID)
    assert recorded.result == {"content": full[:10], "truncated": True}


@pytest.mark.asyncio
async def test_query_unknown_local_record_returns_projection(tmp_path):
    plugin, store, service, executor = await ready(tmp_path)
    executor.status_body = {
        "status": "completed",
        "creditsUsed": 4,
        "data": {"ok": True},
    }
    trimmed = await service.query(_HANDLE)
    assert trimmed["status"] == "completed"
    assert trimmed["creditsUsed"] == 4
    with pytest.raises(KeyError):
        await store.get(_RID)
    with pytest.raises(KeyError):
        await store.get_by_handle(_HANDLE)
    assert plugin.scope_entries == 1
    assert executor.status_calls == [_HANDLE]


@pytest.mark.asyncio
async def test_query_single_request(tmp_path):
    plugin, store, service, executor = await ready(tmp_path)
    await seed_accepted(store)
    executor.status_body = {"status": "processing"}
    await service.query(_HANDLE)
    assert executor.status_calls == [_HANDLE]
    assert plugin.scope_entries == 1


@pytest.mark.asyncio
async def test_query_completed_with_real_executor(tmp_path):
    handle = make_task_handle("agent", _JOB_ID, key_reference(_K1))
    get_seen = asyncio.Event()
    plugin, store, service, _executor, requests = await ready_real(
        tmp_path,
        [
            httpx.Response(200, json={"success": True, "id": _JOB_ID}),
            httpx.Response(200, json={"success": True, "status": "processing"}),
            httpx.Response(
                200,
                json={
                    "success": True,
                    "status": "completed",
                    "creditsUsed": 4,
                    "data": {"answer": "ok"},
                },
            ),
        ],
        config={"max_content_chars": 8000},
        on_request=lambda request: get_seen.set() if request.method == "GET" else None,
    )
    try:
        submitted = await service.submit(payload=_PAYLOAD, estimate=30, umo=_UMO)
        assert submitted["task_handle"] == handle
        await asyncio.wait_for(get_seen.wait(), timeout=2)
        await service.close()
        trimmed = await service.query(handle)
        assert trimmed["status"] == "completed"
        assert trimmed["creditsUsed"] == 4
        assert trimmed["content"] == '{"answer": "ok"}'
        recorded = await store.get(submitted["reservation_id"])
        assert recorded.status == "completed"
        assert recorded.credits_used == 4.0
        assert recorded.result == {"content": '{"answer": "ok"}'}
        assert [item.method for item in requests] == ["POST", "GET", "GET"]
        assert plugin.sent == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_query_cancelled_error_propagates(tmp_path):
    _plugin, store, service, executor = await ready(tmp_path)
    await seed_accepted(store)
    executor.status_error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await service.query(_HANDLE)
    assert (await store.get(_RID)).status == "processing"


def test_build_agent_notification_includes_status_handle_credits_and_body():
    text = build_agent_notification(sample_task())
    assert "状态：completed" in text
    assert _HANDLE in text
    assert _RID in text
    assert "费用：2.5" in text
    assert "正文" in text
    assert "不完整结果" not in text


def test_build_agent_notification_failed_error_partial_and_truncated():
    text = build_agent_notification(
        sample_task(
            status="failed",
            error="boom",
            credits_used=1.0,
            result={
                "content": "半成品",
                "partial": True,
                "truncated": True,
            },
        )
    )
    assert "状态：failed" in text
    assert "错误：boom" in text
    assert "不完整结果" in text
    assert "结果已截断" in text
    assert "半成品" in text


def test_build_agent_notification_cancelled_without_body():
    text = build_agent_notification(
        sample_task(status="cancelled", result=None, credits_used=0.0, error=None)
    )
    assert "状态：已取消" in text
    assert _HANDLE in text
    assert "费用：0.0" in text
    assert "错误：" not in text


def test_build_agent_notification_bounds_total_length():
    content = "汉" * 8000
    text = build_agent_notification(
        sample_task(result={"content": content}), max_chars=6000
    )
    assert len(text) == 6000
    assert text.endswith("[通知已截断]")
    assert _HANDLE in text
    assert _RID in text


@pytest.mark.asyncio
async def test_notify_completed_sends_and_marks(tmp_path):
    plugin, store, service, _executor = await ready(tmp_path)
    await seed_terminal(
        store,
        "completed",
        result={"content": "正文"},
        credits_used=2.5,
    )
    assert await service.notify_terminal(_RID) is True
    assert len(plugin.sent) == 1
    umo, text = plugin.sent[0]
    assert umo == _UMO
    assert "状态：completed" in text
    assert _HANDLE in text
    assert _RID in text
    assert "费用：2.5" in text
    assert "正文" in text
    recorded = await store.get(_RID)
    assert recorded.notified_at == _STAMP
    assert await service.notify_terminal(_RID) is False
    assert len(plugin.sent) == 1


@pytest.mark.asyncio
async def test_notify_failed_and_cancelled(tmp_path):
    plugin, store, service, _executor = await ready(tmp_path)
    await seed_terminal(
        store,
        "failed",
        result={"content": "半成品", "partial": True},
        credits_used=1,
        error="boom",
        rid="r-fail",
        handle=make_task_handle("agent", "fail-1", format(2, "064x")),
    )
    await seed_terminal(
        store,
        "cancelled",
        credits_used=1.5,
        rid="r-cancel",
        handle=make_task_handle("agent", "cancel-1", format(3, "064x")),
    )
    assert await service.notify_terminal("r-fail") is True
    assert await service.notify_terminal("r-cancel") is True
    fail_text = plugin.sent[0][1]
    cancel_text = plugin.sent[1][1]
    assert "状态：failed" in fail_text
    assert "错误：boom" in fail_text
    assert "不完整结果" in fail_text
    assert "半成品" in fail_text
    assert "状态：已取消" in cancel_text
    assert "错误：" not in cancel_text
    assert "费用：1.5" in cancel_text
    assert (await store.get("r-fail")).notified_at == _STAMP
    assert (await store.get("r-cancel")).notified_at == _STAMP


@pytest.mark.asyncio
async def test_notify_send_false_can_retry(tmp_path):
    plugin, store, service, _executor = await ready(tmp_path)
    await seed_terminal(store, "completed", result={"content": "x"}, credits_used=1)
    plugin.send_result = False
    assert await service.notify_terminal(_RID) is False
    assert (await store.get(_RID)).notified_at is None
    plugin.send_result = True
    assert await service.notify_terminal(_RID) is True
    assert (await store.get(_RID)).notified_at == _STAMP
    assert len(plugin.sent) == 2


@pytest.mark.asyncio
async def test_notify_send_exception_can_retry(tmp_path, caplog):
    plugin, store, service, _executor = await ready(tmp_path)
    await seed_terminal(store, "completed", result={"content": "x"}, credits_used=1)
    plugin.send_error = RuntimeError(f"{_SECRET_BODY} {_SECRET_KEY} {_SECRET_URL}")
    with caplog.at_level(logging.WARNING, logger="astrbot.stub"):
        assert await service.notify_terminal(_RID) is False
    assert (await store.get(_RID)).notified_at is None
    assert "RuntimeError" in logged_text(caplog)
    assert_log_safe(caplog)
    plugin.send_error = None
    assert await service.notify_terminal(_RID) is True
    assert (await store.get(_RID)).notified_at == _STAMP


@pytest.mark.asyncio
async def test_notify_mark_failure_after_send_raises(tmp_path):
    plugin, store, service, _executor = await ready(tmp_path)
    await seed_terminal(store, "completed", result={"content": "x"}, credits_used=1)

    async def boom(_reservation_id):
        raise RuntimeError("stamp failed")

    store.mark_notification_sent = boom
    with pytest.raises(RuntimeError, match="stamp failed"):
        await service.notify_terminal(_RID)
    assert plugin.sent
    assert (await store.get(_RID)).notified_at is None


@pytest.mark.asyncio
async def test_notify_rejects_non_terminal(tmp_path):
    plugin, store, service, _executor = await ready(tmp_path)
    await seed_accepted(store)
    with pytest.raises(ValueError, match="仅终态"):
        await service.notify_terminal(_RID)
    assert plugin.sent == []


@pytest.mark.asyncio
async def test_notify_cancelled_error_propagates(tmp_path):
    plugin, store, service, _executor = await ready(tmp_path)
    await seed_terminal(store, "completed", result={"content": "x"}, credits_used=1)
    plugin.send_error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await service.notify_terminal(_RID)
    assert (await store.get(_RID)).notified_at is None


async def _noop_track(_task_handle: str) -> None:
    """测试中暂停后台 track，避免额外 GET。"""
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_body", "expected_status", "credits"),
    [
        (
            {"status": "completed", "creditsUsed": 2.5, "data": {"a": 1}},
            "completed",
            2.5,
        ),
        (
            {"status": "failed", "creditsUsed": 1, "error": "boom"},
            "failed",
            1,
        ),
        (
            {"status": "cancelled", "creditsUsed": 0},
            "cancelled",
            0,
        ),
        ({"status": "processing"}, "processing", None),
    ],
)
async def test_cancel_delete_then_query_projection(
    tmp_path, status_body, expected_status, credits
):
    plugin, store, service, executor = await ready(tmp_path)
    await seed_accepted(store)
    executor.status_body = status_body
    result = await service.cancel(_HANDLE)
    assert executor.ops == ["cancel", "status"]
    assert executor.cancel_calls == [_HANDLE]
    assert executor.status_calls == [_HANDLE]
    assert plugin.scope_entries == 1
    assert result["status"] == expected_status
    assert result["cancel_requested"] is True
    assert result["task_handle"] == _HANDLE
    recorded = await store.get(_RID)
    assert recorded.status == expected_status
    if credits is None:
        assert "creditsUsed" not in result
        assert recorded.credits_used is None
        assert recorded.result is None
    else:
        assert result["creditsUsed"] == credits
        assert recorded.credits_used == float(credits)
    assert plugin.sent == []


@pytest.mark.asyncio
async def test_cancel_maps_cancel_refusal_to_cancelled(tmp_path):
    plugin, store, service, executor = await ready(tmp_path)
    await seed_accepted(store)
    executor.status_body = {
        "status": "failed",
        "creditsUsed": 0,
        "error": "Refusal: Error: Agent was cancelled",
    }
    result = await service.cancel(_HANDLE)
    assert executor.ops == ["cancel", "status"]
    assert result["status"] == "cancelled"
    assert result["cancel_requested"] is True
    assert result["task_handle"] == _HANDLE
    assert result["creditsUsed"] == 0
    assert "error" not in result
    recorded = await store.get(_RID)
    assert recorded.status == "cancelled"
    assert recorded.error is None
    assert recorded.credits_used == 0.0
    assert plugin.sent == []


@pytest.mark.asyncio
async def test_cancel_unknown_handle_still_deletes_then_gets(tmp_path):
    plugin, store, service, executor = await ready(tmp_path)
    executor.status_body = {"status": "cancelled", "creditsUsed": 0}
    result = await service.cancel(_HANDLE)
    assert executor.ops == ["cancel", "status"]
    assert result == {
        "status": "cancelled",
        "creditsUsed": 0,
        "cancel_requested": True,
        "task_handle": _HANDLE,
    }
    with pytest.raises(KeyError):
        await store.get(_RID)
    assert plugin.scope_entries == 1


@pytest.mark.asyncio
async def test_cancel_query_pending_propagates_not_completed(tmp_path):
    _plugin, store, service, executor = await ready(tmp_path)
    await seed_accepted(store)
    error = PendingRequestError(_RID, task_handle=_HANDLE)
    executor.status_error = error
    with pytest.raises(PendingRequestError) as excinfo:
        await service.cancel(_HANDLE)
    assert excinfo.value is error
    assert executor.ops == ["cancel", "status"]
    recorded = await store.get(_RID)
    assert recorded.status == "processing"
    assert recorded.credits_used is None


@pytest.mark.asyncio
async def test_cancel_delete_error_skips_status(tmp_path):
    _plugin, store, service, executor = await ready(tmp_path)
    await seed_accepted(store)
    executor.cancel_error = FirecrawlError(ErrorCategory.USAGE, "cannot cancel")
    with pytest.raises(FirecrawlError) as excinfo:
        await service.cancel(_HANDLE)
    assert excinfo.value.category is ErrorCategory.USAGE
    assert executor.ops == ["cancel"]
    assert executor.status_calls == []
    assert (await store.get(_RID)).status == "processing"


@pytest.mark.asyncio
async def test_cancel_cancelled_error_on_delete_propagates(tmp_path):
    _plugin, store, service, executor = await ready(tmp_path)
    await seed_accepted(store)
    executor.cancel_error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await service.cancel(_HANDLE)
    assert executor.ops == ["cancel"]
    assert executor.status_calls == []
    assert (await store.get(_RID)).status == "processing"


@pytest.mark.asyncio
async def test_cancel_real_executor_settles_actual_credits_original_key(tmp_path):
    handle = make_task_handle("agent", _JOB_ID, key_reference(_K1))
    plugin, store, service, executor, requests = await ready_real(
        tmp_path,
        [
            httpx.Response(200, json={"success": True, "id": _JOB_ID}),
            httpx.Response(200, json={"success": True}),
            httpx.Response(
                200,
                json={"success": True, "status": "cancelled", "creditsUsed": 3},
            ),
        ],
    )
    service.track = _noop_track
    try:
        submitted = await service.submit(payload=_PAYLOAD, estimate=30, umo=_UMO)
        budget = await executor._store.usage()
        assert budget.used == 0.0
        assert budget.active_reserved == 30.0
        result = await service.cancel(submitted["task_handle"])
        assert result["status"] == "cancelled"
        assert result["creditsUsed"] == 3
        assert result["cancel_requested"] is True
        assert result["task_handle"] == handle
        budget = await executor._store.usage()
        assert budget.used == 3.0
        assert budget.active_reserved == 0.0
        assert [item.method for item in requests] == ["POST", "DELETE", "GET"]
        assert str(requests[1].url) == f"https://api.firecrawl.dev/v2/agent/{_JOB_ID}"
        assert str(requests[2].url) == str(requests[1].url)
        assert requests[1].headers["authorization"] == f"Bearer {_K1}"
        assert requests[2].headers["authorization"] == f"Bearer {_K1}"
        recorded = await store.get(submitted["reservation_id"])
        assert recorded.status == "cancelled"
        assert recorded.credits_used == 3.0
        assert plugin.scope_entries == 2
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_cancel_real_executor_processing_does_not_settle(tmp_path):
    plugin, store, service, executor, requests = await ready_real(
        tmp_path,
        [
            httpx.Response(200, json={"success": True, "id": _JOB_ID}),
            httpx.Response(200, json={"success": True}),
            httpx.Response(200, json={"success": True, "status": "processing"}),
        ],
    )
    service.track = _noop_track
    try:
        submitted = await service.submit(payload=_PAYLOAD, estimate=30, umo=_UMO)
        result = await service.cancel(submitted["task_handle"])
        assert result["status"] == "processing"
        assert result["cancel_requested"] is True
        assert "creditsUsed" not in result
        budget = await executor._store.usage()
        assert budget.used == 0.0
        assert budget.active_reserved == 30.0
        reservation = await executor._store.get(submitted["reservation_id"])
        assert reservation.state is ReservationState.ACCEPTED
        recorded = await store.get(submitted["reservation_id"])
        assert recorded.status == "processing"
        assert [item.method for item in requests] == ["POST", "DELETE", "GET"]
        assert plugin.scope_entries == 2
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_cancel_real_executor_status_pending_keeps_accepted(tmp_path):
    plugin, store, service, executor, requests = await ready_real(
        tmp_path,
        [
            httpx.Response(200, json={"success": True, "id": _JOB_ID}),
            httpx.Response(200, json={"success": True}),
            httpx.Response(
                200,
                json={"success": True, "status": "completed", "data": {"answer": "x"}},
            ),
        ],
    )
    service.track = _noop_track
    try:
        submitted = await service.submit(payload=_PAYLOAD, estimate=30, umo=_UMO)
        with pytest.raises(PendingRequestError) as excinfo:
            await service.cancel(submitted["task_handle"])
        assert excinfo.value.reservation_id == submitted["reservation_id"]
        assert excinfo.value.task_handle == submitted["task_handle"]
        budget = await executor._store.usage()
        assert budget.used == 0.0
        assert budget.active_reserved == 30.0
        reservation = await executor._store.get(submitted["reservation_id"])
        assert reservation.state is ReservationState.ACCEPTED
        recorded = await store.get(submitted["reservation_id"])
        assert recorded.status == "processing"
        assert [item.method for item in requests] == ["POST", "DELETE", "GET"]
    finally:
        await service.close()
