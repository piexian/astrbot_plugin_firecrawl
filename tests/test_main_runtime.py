"""main.py 运行时测试：配置组装、生命周期、并发与关闭语义（stub AstrBot SDK）。"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot_plugin_firecrawl.firecrawl_api import FirecrawlError

_ROOT = Path(__file__).resolve().parent.parent
_PACKAGE = "astrbot_plugin_firecrawl"

K1 = "fc-aaaaaaaa1111"
K2 = "fc-bbbbbbbb2222"
SECRET = "fc-secret-value-9f3a"
_SDK_MODULES = (
    "astrbot",
    "astrbot.api",
    "astrbot.api.star",
    "astrbot.api.event",
    "astrbot.core",
    "astrbot.core.star",
    "astrbot.core.star.filter",
    "astrbot.core.star.filter.command",
)
_MISSING = object()

BASE_CONFIG = {
    "api_keys": [K1, K2],
    "key_cooldown_seconds": 30,
    "base_url": "https://proxy.example.com",
    "timeout_seconds": 12,
    "max_estimated_credits_per_call": 100,
    "daily_credit_limit": 500,
    "max_concurrent_requests": 3,
}


class _StubLogger:
    """记录全部日志文本的替身，用于断言不泄露 Key 与异常细节。"""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def _record(self, level: str, message, *args) -> None:
        self.messages.append(f"{level} {message % args if args else message}")

    def info(self, message, *args) -> None:
        self._record("INFO", message, *args)

    def warning(self, message, *args) -> None:
        self._record("WARNING", message, *args)

    def error(self, message, *args) -> None:
        self._record("ERROR", message, *args)


class _StubContext:
    """记录 add_llm_tools 注册项的最小 Context 替身。"""

    def __init__(self) -> None:
        self.added_tools: list = []

    def add_llm_tools(self, *tools) -> None:
        self.added_tools.extend(tools)


class _StubStar:
    """与 AstrBot 4.29.3 Star 基类同形的最小替身。"""

    def __init__(self, context, config=None) -> None:
        self.context = context
        self.config = config

    async def initialize(self) -> None:
        return None

    async def terminate(self) -> None:
        return None


class _StubStarTools:
    """get_data_dir 返回测试数据目录并确保其存在的最小替身。"""

    data_dir = Path("/tmp")

    @classmethod
    def get_data_dir(cls, plugin_name=None) -> Path:
        cls.data_dir.mkdir(parents=True, exist_ok=True)
        return cls.data_dir


@pytest.fixture(scope="module")
def main_sdk():
    """以最小 astrbot stub 经 importlib 加载 main.py，结束后还原 sys.modules。"""
    saved = {name: sys.modules.get(name, _MISSING) for name in _SDK_MODULES}
    logger_stub = _StubLogger()
    api = types.ModuleType("astrbot.api")
    api.FunctionTool = type("FunctionTool", (), {})
    api.logger = logger_stub
    star = types.ModuleType("astrbot.api.star")
    event = types.ModuleType("astrbot.api.event")

    class _CommandGroup:
        def __init__(self, full_name):
            self.full_name = full_name

        def command(self, _name, **_kwargs):
            return lambda function: function

        def group(self, name, **_kwargs):
            def decorate(function):
                return _CommandGroup(f"{self.full_name} {name}")

            return decorate

    def command_group(name, **_kwargs):
        return lambda function: _CommandGroup(name)

    def permission_type(_permission, **_kwargs):
        return lambda obj: obj

    event.AstrMessageEvent = type("AstrMessageEvent", (), {})
    event.filter = SimpleNamespace(
        command_group=command_group,
        permission_type=permission_type,
        PermissionType=SimpleNamespace(ADMIN="ADMIN"),
    )
    star.Context = type("Context", (), {})
    star.Star = _StubStar
    star.StarTools = _StubStarTools
    command_module = types.ModuleType("astrbot.core.star.filter.command")
    command_module.GreedyStr = type("GreedyStr", (str,), {})
    core = types.ModuleType("astrbot.core")
    core_star = types.ModuleType("astrbot.core.star")
    core_filter = types.ModuleType("astrbot.core.star.filter")
    for package in (core, core_star, core_filter):
        package.__path__ = []
    core.star = core_star
    core_star.filter = core_filter
    core_filter.command = command_module
    root = types.ModuleType("astrbot")
    root.api = api
    root.core = core
    sys.modules.update(
        {
            "astrbot": root,
            "astrbot.api": api,
            "astrbot.api.star": star,
            "astrbot.api.event": event,
            "astrbot.core": core,
            "astrbot.core.star": core_star,
            "astrbot.core.star.filter": core_filter,
            "astrbot.core.star.filter.command": command_module,
        }
    )
    sys.modules["astrbot.api.event"] = event
    name = f"{_PACKAGE}.main"
    spec = importlib.util.spec_from_file_location(name, _ROOT / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        yield SimpleNamespace(module=module, logger=logger_stub)
    finally:
        sys.modules.pop(name, None)
        for key, original in saved.items():
            if original is _MISSING:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = original


def _new_plugin(module, config, context=None):
    return module.FirecrawlPlugin(context or _StubContext(), config)


def _log_slice(sdk, start: int) -> list[str]:
    return sdk.logger.messages[start:]


def _tracking_client_class(module, created: list):
    """记录实例与 aclose 次数的客户端替身类。"""

    class TrackingClient(module.FirecrawlClient):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.aclose_calls = 0
            created.append(self)

        async def aclose(self) -> None:
            self.aclose_calls += 1
            await super().aclose()

    return TrackingClient


@pytest.mark.asyncio
async def test_initialize_assembles_runtime_and_registers_tools(
    main_sdk, monkeypatch, tmp_path
):
    monkeypatch.setattr(_StubStarTools, "data_dir", tmp_path / "data")
    context = _StubContext()
    plugin = _new_plugin(main_sdk.module, dict(BASE_CONFIG), context)
    await plugin.initialize()
    try:
        assert [tool.name for tool in context.added_tools] == [
            "firecrawl_search",
            "firecrawl_scrape",
            "firecrawl_site",
        ]
        assert all(tool.plugin is plugin for tool in context.added_tools)
        assert plugin.key_pool.key_count == 2
        assert plugin.key_pool._cooldown_seconds == 30
        assert plugin.client._base_url == "https://proxy.example.com"
        assert plugin.client._timeout == 12.0
        db_path = tmp_path / "data" / "firecrawl.sqlite3"
        assert plugin.store._db_path == db_path
        assert db_path.exists()
        assert plugin.store._policy.max_estimated_credits_per_call == 100
        assert plugin.store._policy.daily_credit_limit == 500
        assert plugin.executor._client is plugin.client
        assert plugin.executor._store is plugin.store
        assert plugin._semaphore._value == 3
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_api_keys_string_empty_missing_and_invalid(
    main_sdk, monkeypatch, tmp_path
):
    monkeypatch.setattr(_StubStarTools, "data_dir", tmp_path / "data")
    module = main_sdk.module

    single = _new_plugin(module, {"api_keys": K1})
    await single.initialize()
    assert single.key_pool.key_count == 1
    await single.terminate()

    none_value = _new_plugin(module, {"api_keys": None})
    await none_value.initialize()
    assert none_value.key_pool.key_count == 0
    await none_value.terminate()

    missing = _new_plugin(module, {})
    await missing.initialize()
    assert missing.key_pool.key_count == 0
    await missing.terminate()

    invalid = _new_plugin(module, {"api_keys": 42})
    start = len(main_sdk.logger.messages)
    await invalid.initialize()
    assert invalid.key_pool.key_count == 0
    logged = "\n".join(_log_slice(main_sdk, start))
    assert "空 Key 池" in logged
    assert "42" not in logged
    await invalid.terminate()


@pytest.mark.asyncio
async def test_blank_and_duplicate_keys_dropped_without_leaking(
    main_sdk, monkeypatch, tmp_path
):
    monkeypatch.setattr(_StubStarTools, "data_dir", tmp_path / "data")
    start = len(main_sdk.logger.messages)
    plugin = _new_plugin(main_sdk.module, {"api_keys": [K1, K1, "", "   "]})
    await plugin.initialize()
    try:
        assert plugin.key_pool.key_count == 1
        logged = "\n".join(_log_slice(main_sdk, start))
        assert K1 not in logged
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_int_options_accept_numeric_strings(main_sdk, monkeypatch, tmp_path):
    monkeypatch.setattr(_StubStarTools, "data_dir", tmp_path / "data")
    plugin = _new_plugin(
        main_sdk.module,
        {**BASE_CONFIG, "timeout_seconds": "15", "max_concurrent_requests": "2"},
    )
    await plugin.initialize()
    try:
        assert plugin.client._timeout == 15.0
        assert plugin._semaphore._value == 2
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_recover_precedes_tool_registration(main_sdk, monkeypatch, tmp_path):
    monkeypatch.setattr(_StubStarTools, "data_dir", tmp_path / "data")
    module = main_sdk.module
    events: list[str] = []

    class RecordingStore(module.SQLiteStore):
        async def initialize(self):
            events.append("store.initialize")
            await super().initialize()

        async def recover(self):
            events.append("store.recover")
            return await super().recover()

    class RecordingContext(_StubContext):
        def add_llm_tools(self, *tools):
            events.append("add_llm_tools")
            super().add_llm_tools(*tools)

    monkeypatch.setattr(module, "SQLiteStore", RecordingStore)
    plugin = _new_plugin(module, {"api_keys": [K1]}, RecordingContext())
    await plugin.initialize()
    try:
        assert events == ["store.initialize", "store.recover", "add_llm_tools"]
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_request_scope_limits_concurrency(main_sdk, monkeypatch, tmp_path):
    monkeypatch.setattr(_StubStarTools, "data_dir", tmp_path / "data")
    plugin = _new_plugin(main_sdk.module, {**BASE_CONFIG, "max_concurrent_requests": 1})
    await plugin.initialize()
    entered = False

    async def enter_second() -> None:
        nonlocal entered
        async with plugin.request_scope():
            entered = True

    try:
        async with plugin.request_scope():
            task = asyncio.create_task(enter_second())
            for _ in range(5):
                await asyncio.sleep(0)
            assert entered is False
        await task
        assert entered is True
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_terminate_rejects_new_scope_waits_inflight_and_closes_client(
    main_sdk, monkeypatch, tmp_path
):
    monkeypatch.setattr(_StubStarTools, "data_dir", tmp_path / "data")
    module = main_sdk.module
    created: list = []
    client_class = _tracking_client_class(module, created)
    monkeypatch.setattr(module, "FirecrawlClient", client_class)
    plugin = _new_plugin(module, {**BASE_CONFIG, "max_concurrent_requests": 2})
    await plugin.initialize()
    client = created[0]
    try:
        async with plugin.request_scope():
            terminate_task = asyncio.create_task(plugin.terminate())
            for _ in range(5):
                await asyncio.sleep(0)
            assert not terminate_task.done()
            assert client.aclose_calls == 0
            with pytest.raises(RuntimeError, match="运行时不可用"):
                async with plugin.request_scope():
                    pass
        await terminate_task
        assert client.aclose_calls == 1
        assert plugin.client is None
    finally:
        await plugin.terminate()
    assert client.aclose_calls == 1


@pytest.mark.asyncio
async def test_scope_unavailable_before_initialize_and_after_terminate(
    main_sdk, monkeypatch, tmp_path
):
    monkeypatch.setattr(_StubStarTools, "data_dir", tmp_path / "data")
    plugin = _new_plugin(main_sdk.module, {"api_keys": [K1]})
    with pytest.raises(RuntimeError, match="运行时不可用"):
        async with plugin.request_scope():
            pass
    await plugin.initialize()
    await plugin.terminate()
    with pytest.raises(RuntimeError, match="运行时不可用"):
        async with plugin.request_scope():
            pass


@pytest.mark.asyncio
async def test_initialize_failure_after_client_creation_rolls_back(
    main_sdk, monkeypatch, tmp_path
):
    monkeypatch.setattr(_StubStarTools, "data_dir", tmp_path / "data")
    module = main_sdk.module
    created: list = []
    client_class = _tracking_client_class(module, created)
    monkeypatch.setattr(module, "FirecrawlClient", client_class)

    class ExplodingContext(_StubContext):
        def add_llm_tools(self, *tools):
            raise RuntimeError(f"工具注册失败，内部细节 {SECRET}")

    plugin = _new_plugin(module, dict(BASE_CONFIG), ExplodingContext())
    start = len(main_sdk.logger.messages)
    with pytest.raises(RuntimeError):
        await plugin.initialize()
    client = created[0]
    assert client.aclose_calls == 1
    assert plugin.client is None
    assert plugin.key_pool is None
    assert plugin.store is None
    assert plugin.executor is None
    assert plugin._semaphore is None
    logged = "\n".join(_log_slice(main_sdk, start))
    assert "回滚" in logged
    assert SECRET not in logged
    assert "RuntimeError" not in logged


@pytest.mark.asyncio
async def test_invalid_concurrency_fails_without_echo(main_sdk, monkeypatch, tmp_path):
    monkeypatch.setattr(_StubStarTools, "data_dir", tmp_path / "data")
    module = main_sdk.module
    created: list = []
    client_class = _tracking_client_class(module, created)
    monkeypatch.setattr(module, "FirecrawlClient", client_class)
    plugin = _new_plugin(module, {**BASE_CONFIG, "max_concurrent_requests": 0})
    with pytest.raises(ValueError) as excinfo:
        await plugin.initialize()
    assert str(excinfo.value) == "max_concurrent_requests 必须至少为 1。"
    client = created[0]
    assert client.aclose_calls == 1
    assert plugin.client is None
    assert plugin.key_pool is None


@pytest.mark.asyncio
async def test_non_int_config_rejected_without_echo(main_sdk, monkeypatch, tmp_path):
    monkeypatch.setattr(_StubStarTools, "data_dir", tmp_path / "data")
    plugin = _new_plugin(main_sdk.module, {**BASE_CONFIG, "timeout_seconds": "abc"})
    with pytest.raises(ValueError) as excinfo:
        await plugin.initialize()
    assert str(excinfo.value) == "配置项 timeout_seconds 必须是整数。"
    assert "abc" not in str(excinfo.value)
    assert plugin.client is None
    assert plugin.key_pool is None


@pytest.mark.asyncio
async def test_invalid_base_url_fails_without_client(main_sdk, monkeypatch, tmp_path):
    monkeypatch.setattr(_StubStarTools, "data_dir", tmp_path / "data")
    plugin = _new_plugin(
        main_sdk.module, {**BASE_CONFIG, "base_url": "ftp://example.com"}
    )
    with pytest.raises(FirecrawlError):
        await plugin.initialize()
    assert plugin.client is None
    assert plugin.key_pool is None
    assert plugin.store is None
