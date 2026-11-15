"""main.py Agent 生命周期：Store/Service 装配、恢复、关闭与失败回滚。"""

from __future__ import annotations

import pytest

from tests.test_main_runtime import (
    BASE_CONFIG,
    SECRET,
    _log_slice,
    _new_plugin,
    _StubContext,
    _StubStarTools,
)
from tests.test_main_runtime import (
    main_sdk as _runtime_main_sdk,
)

_SECRET_PROMPT = "secret-agent-prompt-do-not-log"
_SECRET_HANDLE = "agent:secret-job@" + ("a" * 64)


@pytest.fixture(scope="module")
def main_sdk():
    yield from _runtime_main_sdk._get_wrapped_function()()


@pytest.mark.asyncio
async def test_initialize_creates_agent_store_service_and_recovers(
    main_sdk, monkeypatch, tmp_path
):
    monkeypatch.setattr(_StubStarTools, "data_dir", tmp_path / "data")
    module = main_sdk.module
    events: list[str] = []
    recover_calls: list[dict] = []
    real_recover = module.AgentService.recover

    async def wrapped_recover(self):
        events.append("agent.recover")
        result = await real_recover(self)
        recover_calls.append(result)
        return result

    class RecordingContext(_StubContext):
        def add_llm_tools(self, *tools):
            events.append("add_llm_tools")
            super().add_llm_tools(*tools)

    monkeypatch.setattr(module.AgentService, "recover", wrapped_recover)
    context = RecordingContext()
    plugin = _new_plugin(module, dict(BASE_CONFIG), context)
    await plugin.initialize()
    try:
        db_path = tmp_path / "data" / "firecrawl.sqlite3"
        assert isinstance(plugin.agent_store, module.AgentStore)
        assert isinstance(plugin.agent_service, module.AgentService)
        assert plugin.agent_store._db_path == db_path
        assert plugin.store._db_path == db_path
        assert plugin.agent_service._store is plugin.agent_store
        assert plugin.agent_service._plugin is plugin
        assert recover_calls == [{"processing": 0, "unnotified": 0}]
        assert events == ["add_llm_tools", "agent.recover"]
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_terminate_closes_agent_service_once_and_releases_refs(
    main_sdk, monkeypatch, tmp_path
):
    monkeypatch.setattr(_StubStarTools, "data_dir", tmp_path / "data")
    module = main_sdk.module
    close_calls: list = []
    real_close = module.AgentService.close

    async def wrapped_close(self):
        close_calls.append(self)
        await real_close(self)

    monkeypatch.setattr(module.AgentService, "close", wrapped_close)
    plugin = _new_plugin(module, dict(BASE_CONFIG))
    await plugin.initialize()
    service = plugin.agent_service
    assert close_calls == []
    await plugin.terminate()
    assert close_calls == [service]
    assert service._closing is True
    assert plugin.agent_service is None
    assert plugin.agent_store is None
    await plugin.terminate()
    assert close_calls == [service]


@pytest.mark.asyncio
async def test_watch_reload_failure_closes_agent_service_without_masking(
    main_sdk, monkeypatch, tmp_path
):
    monkeypatch.setattr(_StubStarTools, "data_dir", tmp_path / "data")
    module = main_sdk.module
    close_calls: list = []

    async def fail_reload(self):
        raise RuntimeError("watch reload failed")

    async def fail_close(self):
        close_calls.append(self)
        raise RuntimeError(f"close leaked {SECRET}")

    monkeypatch.setattr(module.WatchHost, "reload", fail_reload)
    monkeypatch.setattr(module.AgentService, "close", fail_close)
    plugin = _new_plugin(module, dict(BASE_CONFIG))
    start = len(main_sdk.logger.messages)
    with pytest.raises(RuntimeError, match="watch reload failed") as excinfo:
        await plugin.initialize()
    assert SECRET not in str(excinfo.value)
    assert close_calls
    assert plugin.agent_service is None
    assert plugin.agent_store is None
    assert plugin.client is None
    logged = "\n".join(_log_slice(main_sdk, start))
    assert "回滚" in logged
    assert "RuntimeError" in logged
    assert SECRET not in logged


@pytest.mark.asyncio
async def test_agent_recover_log_contains_counts_not_handle_or_prompt(
    main_sdk, monkeypatch, tmp_path
):
    monkeypatch.setattr(_StubStarTools, "data_dir", tmp_path / "data")
    module = main_sdk.module
    recover_calls: list = []

    async def wrapped_recover(self):
        recover_calls.append(self)
        return {
            "processing": 2,
            "unnotified": 1,
            "prompt": _SECRET_PROMPT,
            "task_handle": _SECRET_HANDLE,
        }

    monkeypatch.setattr(module.AgentService, "recover", wrapped_recover)
    plugin = _new_plugin(module, dict(BASE_CONFIG))
    start = len(main_sdk.logger.messages)
    await plugin.initialize()
    try:
        assert recover_calls
        logged = "\n".join(_log_slice(main_sdk, start))
        assert "processing 2" in logged
        assert "未通知终态 1" in logged
        assert _SECRET_PROMPT not in logged
        assert _SECRET_HANDLE not in logged
        assert "secret-job" not in logged
    finally:
        await plugin.terminate()
