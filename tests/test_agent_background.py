"""AgentService 后台轮询、恢复与关闭测试。"""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest
from astrbot_plugin_firecrawl.agent.service import AgentService
from astrbot_plugin_firecrawl.firecrawl_api.keys import key_reference
from astrbot_plugin_firecrawl.firecrawl_api.tasks import make_task_handle

from tests.test_agent_service import (
    _HANDLE,
    _JOB_ID,
    _K1,
    _PAYLOAD,
    _PROMPT,
    _RID,
    _SECRET_BODY,
    _SECRET_KEY,
    _SECRET_URL,
    _UMO,
    assert_log_safe,
    logged_text,
    ready,
    ready_real,
    seed_accepted,
    seed_terminal,
)

_COMPLETED = {
    "status": "completed",
    "creditsUsed": 1,
    "data": {"ok": True},
}


@pytest.mark.asyncio
async def test_submit_processing_then_completed_notifies_once(tmp_path):
    plugin, store, service, executor = await ready(tmp_path, poll_interval_seconds=0.01)
    executor.status_queue = [{"status": "processing"}]
    executor.status_body = _COMPLETED
    try:
        result = await service.submit(payload=_PAYLOAD, estimate=30, umo=_UMO)
        worker = service._workers[_RID]
        await asyncio.wait_for(asyncio.shield(worker), timeout=2)
        recorded = await store.get(_RID)
        assert recorded.status == "completed"
        assert recorded.notified_at is not None
        assert recorded.result == {"content": '{"ok": true}'}
        assert len(plugin.sent) == 1
        assert plugin.sent[0][0] == _UMO
        assert "状态：completed" in plugin.sent[0][1]
        assert _HANDLE in plugin.sent[0][1]
        assert result["task_handle"] == _HANDLE
        assert result["task_handle"] == _HANDLE
        assert executor.submit_calls == [("agent", _PAYLOAD, 30, True)]
        assert executor.status_calls == [_HANDLE, _HANDLE]
        assert service._workers == {}
        await asyncio.sleep(0)
        assert len(plugin.sent) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_processing_cancel_refusal_notifies_cancelled_once(tmp_path):
    plugin, store, service, executor = await ready(tmp_path, poll_interval_seconds=0.01)
    await seed_accepted(store)
    executor.status_body = {
        "status": "failed",
        "creditsUsed": 0,
        "error": "Refusal: Error: Agent was cancelled",
    }
    try:
        await service.track(_HANDLE)
        worker = service._workers[_RID]
        await asyncio.wait_for(asyncio.shield(worker), timeout=2)
        recorded = await store.get(_RID)
        assert recorded.status == "cancelled"
        assert recorded.error is None
        assert recorded.credits_used == 0.0
        assert recorded.notified_at is not None
        assert len(plugin.sent) == 1
        assert plugin.sent[0][0] == _UMO
        text = plugin.sent[0][1]
        assert "状态：已取消" in text
        assert "错误：" not in text
        assert executor.status_calls == [_HANDLE]
        assert service._workers == {}
        await asyncio.sleep(0)
        assert len(plugin.sent) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_repeated_track_and_recover_single_worker(tmp_path):
    plugin, store, service, executor = await ready(tmp_path, poll_interval_seconds=0.01)
    executor.status_block = asyncio.Event()
    await seed_accepted(store)
    try:
        await service.track(_HANDLE)
        first = service._workers[_RID]
        await asyncio.wait_for(executor.status_started.get(), timeout=2)
        await service.track(_HANDLE)
        await service.track(_HANDLE)
        counts = await service.recover()
        assert counts == {"processing": 1, "unnotified": 0}
        assert service._workers[_RID] is first
        assert list(service._workers) == [_RID]
        assert not first.done()
        executor.status_block.set()
    finally:
        await service.close()
    assert first.cancelled() or first.done()
    assert executor.cancel_calls == []
    assert plugin.executor.submit_calls == []


@pytest.mark.asyncio
async def test_recover_processing_and_unnotified_after_restart(tmp_path):
    plugin, store, service, executor = await ready(tmp_path, poll_interval_seconds=0.01)
    handle2 = make_task_handle("agent", "job-2", format(2, "064x"))
    await seed_accepted(store)
    await seed_terminal(
        store,
        "completed",
        result={"content": '{"ok": true}'},
        credits_used=1,
        rid="r-term",
        handle=handle2,
    )
    await store.create(reservation_id="r-sub", umo=_UMO, prompt=_PROMPT)
    await service.close()

    restarted = AgentService(plugin, store, poll_interval_seconds=0.01)
    executor.status_body = _COMPLETED
    try:
        counts = await restarted.recover()
        assert counts == {"processing": 1, "unnotified": 1}
        workers = list(restarted._workers.values())
        assert len(workers) == 2
        await asyncio.wait_for(
            asyncio.gather(*workers, return_exceptions=True), timeout=2
        )
        processing = await store.get(_RID)
        terminal = await store.get("r-term")
        submitting = await store.get("r-sub")
        assert processing.status == "completed"
        assert processing.notified_at is not None
        assert terminal.notified_at is not None
        assert submitting.status == "submitting"
        assert submitting.task_handle is None
        assert len(plugin.sent) == 2
        assert _HANDLE in executor.status_calls
        assert handle2 not in executor.status_calls
        assert executor.submit_calls == []
        assert executor.cancel_calls == []
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_recover_submitting_without_handle_zero_network(tmp_path):
    plugin, store, service, executor = await ready(tmp_path, poll_interval_seconds=0.01)
    await store.create(reservation_id=_RID, umo=_UMO, prompt=_PROMPT)
    try:
        counts = await service.recover()
        assert counts == {"processing": 0, "unnotified": 0}
        assert service._workers == {}
        assert executor.status_calls == []
        assert executor.submit_calls == []
        assert executor.cancel_calls == []
        recorded = await store.get(_RID)
        assert recorded.status == "submitting"
        assert recorded.task_handle is None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_send_failure_retries_until_success(tmp_path):
    plugin, store, service, executor = await ready(tmp_path, poll_interval_seconds=0.01)
    await seed_terminal(store, "completed", result={"content": "x"}, credits_used=1)
    executor.status_error = RuntimeError("should not GET")

    async def flipping(umo: str, chain) -> bool:
        plugin.sent.append((umo, chain.text))
        return len(plugin.sent) > 1

    plugin.send_message = flipping
    try:
        await service.track(_HANDLE)
        worker = service._workers[_RID]
        await asyncio.wait_for(asyncio.shield(worker), timeout=2)
        recorded = await store.get(_RID)
        assert recorded.notified_at is not None
        assert recorded.status == "completed"
        assert len(plugin.sent) == 2
        assert executor.status_calls == []
        assert executor.submit_calls == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_consecutive_query_errors_are_bounded(tmp_path, caplog):
    plugin, store, service, executor = await ready(
        tmp_path, poll_interval_seconds=0.01, max_consecutive_errors=3
    )
    await seed_accepted(store)
    executor.status_error = RuntimeError(f"{_SECRET_BODY} {_SECRET_KEY} {_SECRET_URL}")
    try:
        await service.track(_HANDLE)
        worker = service._workers[_RID]
        with caplog.at_level(logging.WARNING, logger="astrbot.stub"):
            await asyncio.wait_for(asyncio.shield(worker), timeout=2)
        assert len(executor.status_calls) == 3
        assert executor.submit_calls == []
        assert executor.cancel_calls == []
        assert service._workers == {}
        assert (await store.get(_RID)).status == "processing"
        assert (await store.get(_RID)).notified_at is None
        assert "RuntimeError" in logged_text(caplog)
        assert_log_safe(caplog)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_close_cancels_wait_zero_delete_and_repeatable(tmp_path):
    plugin, store, service, executor = await ready(tmp_path, poll_interval_seconds=0.01)
    executor.status_block = asyncio.Event()
    await seed_accepted(store)
    await service.track(_HANDLE)
    await asyncio.wait_for(executor.status_started.get(), timeout=2)
    await service.close()
    assert service._workers == {}
    assert executor.cancel_calls == []
    assert executor.submit_calls == []
    await service.close()
    await service.track(_HANDLE)
    assert service._workers == {}
    assert executor.status_calls == [_HANDLE]


@pytest.mark.asyncio
async def test_close_real_executor_sends_no_delete(tmp_path):
    handle = make_task_handle("agent", _JOB_ID, key_reference(_K1))
    get_seen = asyncio.Event()
    plugin, store, service, _executor, requests = await ready_real(
        tmp_path,
        [
            httpx.Response(200, json={"success": True, "id": _JOB_ID}),
            httpx.Response(200, json={"success": True, "status": "processing"}),
            httpx.Response(200, json={"success": True, "status": "processing"}),
        ],
        on_request=lambda request: get_seen.set() if request.method == "GET" else None,
        poll_interval_seconds=0.01,
    )
    try:
        submitted = await service.submit(payload=_PAYLOAD, estimate=30, umo=_UMO)
        assert submitted["task_handle"] == handle
        await asyncio.wait_for(get_seen.wait(), timeout=2)
        await service.close()
        await service.close()
        await service.track(handle)
        assert all(item.method != "DELETE" for item in requests)
        assert any(item.method == "POST" for item in requests)
        assert service._workers == {}
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_notification_mark_failure_is_not_false_success(tmp_path):
    plugin, store, service, executor = await ready(tmp_path, poll_interval_seconds=0.01)
    await seed_accepted(store)
    executor.status_body = _COMPLETED
    original = store.mark_notification_sent
    calls: list[str] = []
    first_call = asyncio.Event()

    async def boom(reservation_id: str):
        calls.append(reservation_id)
        first_call.set()
        if len(calls) == 1:
            raise RuntimeError("stamp failed")
        return await original(reservation_id)

    store.mark_notification_sent = boom
    try:
        await service.track(_HANDLE)
        worker = service._workers[_RID]
        await asyncio.wait_for(first_call.wait(), timeout=2)
        assert (await store.get(_RID)).notified_at is None
        assert not worker.done()
        await asyncio.wait_for(asyncio.shield(worker), timeout=2)
        recorded = await store.get(_RID)
        assert recorded.notified_at is not None
        assert len(calls) == 2
        assert len(plugin.sent) == 2
        assert executor.status_calls == [_HANDLE]
        assert executor.submit_calls == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_manual_query_waits_for_worker_lock(tmp_path):
    plugin, store, service, executor = await ready(tmp_path, poll_interval_seconds=30.0)
    executor.status_block = asyncio.Event()
    await seed_accepted(store)
    try:
        await service.track(_HANDLE)
        await asyncio.wait_for(executor.status_started.get(), timeout=2)
        manual = asyncio.create_task(service.query(_HANDLE))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not manual.done()
        assert executor.status_calls == [_HANDLE]
        executor.status_block.set()
        await asyncio.wait_for(manual, timeout=2)
        assert executor.status_calls == [_HANDLE, _HANDLE]
        assert plugin.scope_entries == 2
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_track_skips_already_notified_terminal(tmp_path):
    plugin, store, service, executor = await ready(tmp_path, poll_interval_seconds=0.01)
    await seed_terminal(store, "completed", result={"content": "x"}, credits_used=1)
    await store.mark_notification_sent(_RID)
    try:
        await service.track(_HANDLE)
        assert service._workers == {}
        assert executor.status_calls == []
        counts = await service.recover()
        assert counts == {"processing": 0, "unnotified": 0}
        assert service._workers == {}
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_recover_unnotified_notifies_without_get_when_status_errors(
    tmp_path,
):
    plugin, store, service, executor = await ready(tmp_path, poll_interval_seconds=0.01)
    await seed_terminal(store, "completed", result={"content": "x"}, credits_used=1)
    executor.status_error = RuntimeError("network down")
    try:
        counts = await service.recover()
        assert counts == {"processing": 0, "unnotified": 1}
        worker = service._workers[_RID]
        await asyncio.wait_for(asyncio.shield(worker), timeout=2)
        recorded = await store.get(_RID)
        assert recorded.notified_at is not None
        assert len(plugin.sent) == 1
        assert "状态：completed" in plugin.sent[0][1]
        assert executor.status_calls == []
        assert executor.submit_calls == []
        assert service._workers == {}
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_processing_queries_at_interval(tmp_path):
    plugin, store, service, executor = await ready(tmp_path, poll_interval_seconds=0.01)
    executor.status_body = {"status": "processing"}
    await seed_accepted(store)
    try:
        await service.track(_HANDLE)
        await asyncio.wait_for(executor.status_started.get(), timeout=2)
        await asyncio.wait_for(executor.status_started.get(), timeout=2)
        assert executor.status_calls[0] == _HANDLE
        assert executor.status_calls[1] == _HANDLE
        assert executor.submit_calls == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_latest_get_failure_is_counted_not_uncaught(tmp_path, caplog):
    plugin, store, service, executor = await ready(
        tmp_path, poll_interval_seconds=0.01, max_consecutive_errors=3
    )
    await seed_terminal(
        store, "failed", result={"content": "x"}, credits_used=1, error="boom"
    )
    executor.status_error = RuntimeError("should not GET")
    plugin.send_result = False
    original = store.get
    calls = {"n": 0}

    async def wrapped(reservation_id: str):
        calls["n"] += 1
        if calls["n"] % 3 == 0:
            raise RuntimeError("db down")
        return await original(reservation_id)

    store.get = wrapped
    try:
        await service.track(_HANDLE)
        worker = service._workers[_RID]
        with caplog.at_level(logging.WARNING, logger="astrbot.stub"):
            await asyncio.wait_for(asyncio.shield(worker), timeout=2)
        assert worker.exception() is None
        assert executor.status_calls == []
        assert (await original(_RID)).notified_at is None
        assert "RuntimeError" in logged_text(caplog)
        assert_log_safe(caplog)
    finally:
        await service.close()
