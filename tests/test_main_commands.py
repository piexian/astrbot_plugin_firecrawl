"""main.py 管理指令测试：注册、权限、credit/pending 行为与脱敏（stub AstrBot SDK）。"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

_ROOT = Path(__file__).resolve().parent.parent
_PACKAGE = "astrbot_plugin_firecrawl"

K1 = "fc-key-one-1111"
K2 = "fc-key-two-2222"
SECRET = "fc-secret-value-9f3a"
FP1 = "b" * 64
FP2 = "c" * 64
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


class _StubGreedyStr(str):
    """与 AstrBot GreedyStr 同形的测试类型。"""


def ref_of(key: str) -> str:
    """与 KeyPool.key_reference 一致的完整摘要。"""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class _StubLogger:
    """记录全部日志文本的替身，用于断言不泄露异常细节。"""

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


class _StubMessageChain:
    """与 MessageChain 同形的最小替身。"""

    def message(self, text: str):
        self.text = text
        return self


class _StubContext:
    """最小 Context 替身：LLM 工具注册、cron 管理与会话推送记录。"""

    def __init__(self) -> None:
        self.added_tools: list = []
        self.cron_manager = _StubCronManager()
        self.sent: list[tuple[str, str]] = []

    def add_llm_tools(self, *tools) -> None:
        self.added_tools.extend(tools)

    async def send_message(self, umo: str, chain) -> bool:
        self.sent.append((umo, chain.text))
        return True


class _StubStar:
    """与 AstrBot Star 基类同形的最小替身。"""

    def __init__(self, context, config=None) -> None:
        self.context = context
        self.config = config


class _StubStarTools:
    """get_data_dir 返回测试数据目录的最小替身。"""

    data_dir = Path("/tmp")

    @classmethod
    def get_data_dir(cls, plugin_name=None) -> Path:
        return cls.data_dir


class StubEvent:
    """最小事件替身：消息文本、管理员身份、会话与 plain_result 输出收集。"""

    def __init__(self, message: str, is_admin: bool, umo: str = "") -> None:
        self._message = message
        self._is_admin = is_admin
        self.unified_msg_origin = umo
        self.outputs: list = []

    def get_message_str(self) -> str:
        return self._message

    def is_admin(self) -> bool:
        return self._is_admin

    def plain_result(self, text: str):
        result = SimpleNamespace(chain=[SimpleNamespace(text=text)])
        self.outputs.append(result)
        return result


def _text(result) -> str:
    return result.chain[0].text


@pytest.fixture(scope="module")
def main_sdk():
    """以最小 astrbot stub 经 importlib 加载 main.py，结束后还原 sys.modules。"""
    saved = {name: sys.modules.get(name, _MISSING) for name in _SDK_MODULES}
    logger_stub = _StubLogger()
    api = types.ModuleType("astrbot.api")
    api.FunctionTool = type("FunctionTool", (), {})
    api.logger = logger_stub
    star = types.ModuleType("astrbot.api.star")
    star.Context = type("Context", (), {})
    star.Star = _StubStar
    star.StarTools = _StubStarTools
    event_module = types.ModuleType("astrbot.api.event")
    event_module.AstrMessageEvent = type("AstrMessageEvent", (), {})
    event_module.MessageChain = _StubMessageChain
    registered: list[dict[str, object]] = []

    def _register(full_name, func, *, is_group):
        record = {
            "name": full_name,
            "handler": func,
            "is_group": is_group,
            "admin": getattr(func, "__admin_only__", False),
        }
        registered.append(record)
        return record

    class _CommandGroup:
        def __init__(self, full_name, record, parent=None):
            self.full_name = full_name
            self.record = record
            self.parent = parent

        def command(self, command_name, alias=None, **kwargs):
            def decorator(func):
                _register(f"{self.full_name} {command_name}", func, is_group=False)
                return func

            return decorator

        def group(self, group_name, alias=None, **kwargs):
            def decorator(func):
                full_name = f"{self.full_name} {group_name}"
                record = _register(full_name, func, is_group=True)
                return _CommandGroup(full_name, record, parent=self)

            return decorator

    def _stub_command_group(command_group_name, alias=None, **kwargs):
        def decorator(func):
            record = _register(command_group_name, func, is_group=True)
            return _CommandGroup(command_group_name, record)

        return decorator

    def _stub_permission_type(permission_type, raise_error=True, **kwargs):
        def decorator(obj):
            if isinstance(obj, _CommandGroup):
                obj.record["admin"] = True
            else:
                obj.__admin_only__ = True
                for record in registered:
                    if record["handler"] is obj:
                        record["admin"] = True
            return obj

        return decorator

    event_module.filter = SimpleNamespace(
        command_group=_stub_command_group,
        permission_type=_stub_permission_type,
        PermissionType=SimpleNamespace(ADMIN="ADMIN"),
    )
    command_module = types.ModuleType("astrbot.core.star.filter.command")
    command_module.GreedyStr = _StubGreedyStr
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
    api.star = star
    api.event = event_module
    sys.modules.update(
        {
            "astrbot": root,
            "astrbot.api": api,
            "astrbot.api.star": star,
            "astrbot.api.event": event_module,
            "astrbot.core": core,
            "astrbot.core.star": core_star,
            "astrbot.core.star.filter": core_filter,
            "astrbot.core.star.filter.command": command_module,
        }
    )
    name = f"{_PACKAGE}.main"
    spec = importlib.util.spec_from_file_location(name, _ROOT / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        yield SimpleNamespace(module=module, logger=logger_stub, registered=registered)
    finally:
        sys.modules.pop(name, None)
        for key, original in saved.items():
            if original is _MISSING:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = original


def _recording_transport(calls: list, responder=None):
    """记录收到的请求并按 responder 回应；缺省回应 200 空对象。"""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if responder is not None:
            return responder(request)
        return httpx.Response(200, json={"success": True, "data": {}})

    return httpx.MockTransport(handler)


async def _wired_plugin(
    main_sdk,
    tmp_path,
    *,
    keys=(K1, K2),
    transport=None,
    with_watch=True,
    config=None,
):
    """直接装配运行时属性：真实 SQLiteStore、可选 MockTransport 与 watch 运行时。"""
    module = main_sdk.module
    context = _StubContext()
    plugin = module.FirecrawlPlugin(context, {"api_keys": list(keys), **(config or {})})
    pool = module.KeyPool(list(keys))
    store = module.SQLiteStore(tmp_path / "commands.sqlite3")
    await store.initialize()
    client = module.FirecrawlClient(
        pool, base_url="https://api.test", transport=transport
    )
    plugin.key_pool = pool
    plugin.store = store
    plugin.client = client
    plugin.executor = module.BudgetedExecutor(client, store)
    plugin._semaphore = asyncio.Semaphore(2)
    if with_watch:
        watch_store = module.WatchStore(tmp_path / "commands.sqlite3")
        await watch_store.initialize()
        plugin.watch_store = watch_store
        plugin.watch_service = module.WatchService(plugin, watch_store)
        plugin.watch_host = module.WatchHost(context, plugin.watch_service, watch_store)
    return plugin


async def _run(plugin, message: str, *, admin: bool = True, umo: str = "") -> str:
    """按官方指令名调用对应 handler；裸指令组与未知指令不在此模拟。"""
    text = message.strip().removeprefix("/")
    parts = text.split()
    if not parts or parts[0].lower() != "fc":
        raise AssertionError(f"非 fc 指令：{message}")
    parts = parts[1:]
    if not parts:
        raise AssertionError("裸 fc 由 AstrBot 指令组显示指令树")
    action, rest = parts[0], parts[1:]
    event = StubEvent(message, admin, umo)
    if action == "help":
        return _text(await plugin.fc_help(event))
    if action == "credit":
        extra = " ".join(rest) if rest else _StubGreedyStr
        return _text(await plugin.fc_credit(event, extra))
    if action == "pending" and rest:
        sub_action, values = rest[0], rest[1:]
        if sub_action == "list":
            extra = " ".join(values) if values else _StubGreedyStr
            return _text(await plugin.fc_pending_list(event, extra))
        if sub_action == "resolve":
            reservation_id = values[0] if values else ""
            actual_credits = values[1] if len(values) > 1 else ""
            extra = " ".join(values[2:]) if len(values) > 2 else _StubGreedyStr
            return _text(
                await plugin.fc_pending_resolve(
                    event, reservation_id, actual_credits, extra
                )
            )
    if action == "watch" and rest:
        sub_action, values = rest[0], rest[1:]
        if sub_action == "add":
            url = values[0] if values else ""
            minutes = values[1] if len(values) > 1 else ""
            extra = " ".join(values[2:]) if len(values) > 2 else _StubGreedyStr
            return _text(await plugin.fc_watch_add(event, url, minutes, extra))
        if sub_action == "list":
            extra = " ".join(values) if values else _StubGreedyStr
            return _text(await plugin.fc_watch_list(event, extra))
        if sub_action == "remove":
            sub_id = values[0] if values else ""
            extra = " ".join(values[1:]) if len(values) > 1 else _StubGreedyStr
            return _text(await plugin.fc_watch_remove(event, sub_id, extra))
        if sub_action == "check":
            sub_id = values[0] if values else ""
            extra = " ".join(values[1:]) if len(values) > 1 else _StubGreedyStr
            return _text(await plugin.fc_watch_check(event, sub_id, extra))
    raise AssertionError(f"未注册的 fc 指令：{message}")


async def _seed(
    store,
    *,
    estimate: int,
    kind: str,
    fingerprint: str,
    key_ref: str | None = None,
    job_id: str | None = None,
    accept: bool = False,
) -> str:
    """创建预留并按需推进到 accepted，返回预留 ID。"""
    record = await store.reserve(estimate, kind=kind, request_fingerprint=fingerprint)
    rid = record.reservation_id
    if key_ref is not None or accept:
        await store.mark_submitting(rid, key_ref=key_ref)
    if accept:
        await store.accept(rid, job_id=job_id)
    return rid


def test_fc_command_group_tree_registered(main_sdk):
    records = {record["name"]: record for record in main_sdk.registered}
    assert list(records) == [
        "fc",
        "fc help",
        "fc credit",
        "fc pending",
        "fc pending list",
        "fc pending resolve",
        "fc watch",
        "fc watch add",
        "fc watch list",
        "fc watch remove",
        "fc watch check",
    ]
    assert records["fc"]["is_group"] is True
    assert records["fc pending"]["is_group"] is True
    assert all(
        records[name]["is_group"] is False
        for name in ("fc help", "fc credit", "fc pending list", "fc pending resolve")
    )


@pytest.mark.asyncio
async def test_help_available_to_all_users(main_sdk, tmp_path):
    module = main_sdk.module
    calls: list = []
    plugin = await _wired_plugin(
        main_sdk, tmp_path, transport=_recording_transport(calls)
    )
    try:
        admin_text = await _run(plugin, "fc help", admin=True)
        member_text = await _run(plugin, "fc help", admin=False)
        slash_text = await _run(plugin, "/fc help", admin=False)
        assert admin_text == member_text == slash_text == module._HELP_TEXT
        assert calls == []
    finally:
        await plugin.terminate()


def test_fc_admin_permissions_declared(main_sdk):
    records = {record["name"]: record for record in main_sdk.registered}
    assert records["fc"]["admin"] is False
    assert records["fc help"]["admin"] is False
    assert all(
        records[name]["admin"] is True
        for name in (
            "fc credit",
            "fc pending",
            "fc pending list",
            "fc pending resolve",
        )
    )


@pytest.mark.asyncio
async def test_credit_queries_each_key_without_accounting(main_sdk, tmp_path):
    calls: list = []

    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "remainingCredits": 123.5,
                    "planCredits": 1000,
                    "billingPeriodStart": "2026-11-01",
                    "billingPeriodEnd": "2026-11-30",
                    "planName": "Pro",
                    "message": "internal-server-note",
                },
            },
        )

    plugin = await _wired_plugin(
        main_sdk, tmp_path, transport=_recording_transport(calls, responder)
    )
    store = plugin.store
    try:
        output = await _run(plugin, "fc credit", admin=True)
        assert len(calls) == 2
        for request, key in zip(calls, (K1, K2)):
            assert request.method == "GET"
            assert str(request.url) == "https://api.test/v2/team/credit-usage"
            assert request.headers["Authorization"] == f"Bearer {key}"
        assert f"sha256:{ref_of(K1)[:8]}" in output
        assert f"sha256:{ref_of(K2)[:8]}" in output
        assert "123.5" in output and "1000" in output
        assert output.count("（正常）") == 2
        assert "2026-11-01" in output and "2026-11-30" in output
        assert K1 not in output and K2 not in output
        assert ref_of(K1) not in output and ref_of(K2) not in output
        assert "Pro" not in output and "internal-server-note" not in output
        assert "message" not in output
        assert await store.list_pending() == []
        snapshot = await store.usage()
        assert snapshot.used == 0 and snapshot.active_reserved == 0
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_credit_displays_latest_cooldown_without_leaking(main_sdk, tmp_path):
    """限流失败后展示原因和剩余秒数，不回显 Key 或远端错误原文。"""
    calls: list = []

    def responder(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == f"Bearer {K2}":
            return httpx.Response(
                429,
                json={"error": "too many internal details"},
                headers={"Retry-After": "60"},
            )
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "remainingCredits": 123.5,
                    "planCredits": 1000,
                    "billingPeriodStart": "2026-11-01",
                    "billingPeriodEnd": "2026-11-30",
                },
            },
        )

    plugin = await _wired_plugin(
        main_sdk, tmp_path, transport=_recording_transport(calls, responder)
    )
    try:
        output = await _run(plugin, "fc credit", admin=True)
        lines = output.splitlines()
        assert len(calls) == 2
        assert "（正常）" in lines[0]
        assert "冷却中：rate_limit" in lines[1]
        assert "剩余" in lines[1] and "秒" in lines[1]
        assert "too many internal details" not in output
        assert K1 not in output and K2 not in output
        snapshot = await plugin.store.usage()
        assert snapshot.used == 0 and snapshot.active_reserved == 0
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_credit_remote_errors_sanitized(main_sdk, tmp_path):
    calls: list = []
    responses = [
        httpx.Response(
            401, json={"success": False, "error": f"Invalid API key: {SECRET}"}
        ),
        httpx.Response(
            400,
            json={
                "success": False,
                "error": f"billing period detail {SECRET}",
                "code": "BAD_PERIOD",
            },
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responses.pop(0)

    plugin = await _wired_plugin(
        main_sdk, tmp_path, keys=(K1,), transport=httpx.MockTransport(handler)
    )
    try:
        auth_text = await _run(plugin, "fc credit", admin=True)
        assert "API Key 无效或无权限" in auth_text
        assert SECRET not in auth_text and "Invalid API key" not in auth_text
        usage_text = await _run(plugin, "fc credit", admin=True)
        assert "用法或参数受限" in usage_text
        assert SECRET not in usage_text and "billing period" not in usage_text
        assert len(calls) == 2
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_credit_without_keys_reports_unconfigured(main_sdk, tmp_path):
    module = main_sdk.module
    calls: list = []
    plugin = await _wired_plugin(
        main_sdk, tmp_path, keys=(), transport=_recording_transport(calls)
    )
    try:
        output = await _run(plugin, "fc credit", admin=True)
        assert output == module._NO_KEY_TEXT
        assert calls == []
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_runtime_unavailable_before_init_closing_and_after_terminate(
    main_sdk, tmp_path
):
    module = main_sdk.module
    fresh = module.FirecrawlPlugin(_StubContext(), {"api_keys": [K1]})
    assert await _run(fresh, "fc credit", admin=True) == module._RUNTIME_UNAVAILABLE
    assert await _run(fresh, "fc pending list", admin=True) == (
        module._RUNTIME_UNAVAILABLE
    )
    assert await _run(fresh, "fc pending resolve abc 1", admin=True) == (
        module._RUNTIME_UNAVAILABLE
    )
    plugin = await _wired_plugin(main_sdk, tmp_path)
    try:
        plugin._closing = True
        assert await _run(plugin, "fc credit", admin=True) == (
            module._RUNTIME_UNAVAILABLE
        )
        assert await _run(plugin, "fc pending list", admin=True) == (
            module._RUNTIME_UNAVAILABLE
        )
        plugin._closing = False
    finally:
        await plugin.terminate()
    assert await _run(plugin, "fc credit", admin=True) == module._RUNTIME_UNAVAILABLE
    assert await _run(plugin, "fc pending list", admin=True) == (
        module._RUNTIME_UNAVAILABLE
    )


@pytest.mark.asyncio
async def test_pending_list_masks_fingerprint_key_and_job_id(main_sdk, tmp_path):
    plugin = await _wired_plugin(main_sdk, tmp_path, keys=(K1,))
    store = plugin.store
    try:
        rid1 = await _seed(
            store,
            estimate=5,
            kind="scrape",
            fingerprint=FP1,
            key_ref=ref_of(K1),
            job_id="job-abcdef123456789",
            accept=True,
        )
        rid2 = await _seed(store, estimate=2, kind="search", fingerprint=FP2)
        output = await _run(plugin, "fc pending list", admin=True)
        assert rid1 in output and rid2 in output
        assert "scrape" in output and "search" in output
        assert "accepted" in output and "reserved" in output
        assert "预估 5" in output and "预估 2" in output
        assert "UTC" in output and "已用 0" in output and "活动预留 7" in output
        assert f"sha256:{ref_of(K1)[:8]}" in output
        assert ref_of(K1) not in output
        assert FP1 not in output and FP2 not in output
        assert K1 not in output
        assert "job-abcdef123456789" not in output
        assert "job-abcd…" in output
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_resolve_positive_settles_actual_credits(main_sdk, tmp_path):
    plugin = await _wired_plugin(main_sdk, tmp_path, keys=(K1,))
    store = plugin.store
    try:
        rid = await _seed(
            store,
            estimate=10,
            kind="scrape",
            fingerprint=FP1,
            key_ref=ref_of(K1),
            accept=True,
        )
        output = await _run(plugin, f"fc pending resolve {rid} 3.5", admin=True)
        assert output == f"{rid}：已按实际费用结算 3.5 credits。"
        record = await store.get(rid)
        assert record.state.value == "settled"
        assert record.actual_credits == 3.5 and record.accounted_credits == 3.5
        snapshot = await store.usage()
        assert snapshot.used == 3.5
        assert await store.list_pending() == []
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_resolve_zero_confirms_not_accepted(main_sdk, tmp_path):
    plugin = await _wired_plugin(main_sdk, tmp_path, keys=(K1,))
    store = plugin.store
    try:
        rid = await _seed(
            store,
            estimate=4,
            kind="search",
            fingerprint=FP1,
            key_ref=ref_of(K1),
        )
        output = await _run(plugin, f"fc pending resolve {rid} 0", admin=True)
        assert output == f"{rid}：已确认未受理，预留已释放，不计费。"
        record = await store.get(rid)
        assert record.state.value == "released"
        assert record.actual_credits == 0.0 and record.accounted_credits == 0.0
        snapshot = await store.usage()
        assert snapshot.used == 0 and snapshot.active_reserved == 0
        assert await store.list_pending() == []
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_resolve_invalid_params_and_states_zero_writes(main_sdk, tmp_path):
    module = main_sdk.module
    plugin = await _wired_plugin(main_sdk, tmp_path, keys=(K1,))
    store = plugin.store
    try:
        rid = await _seed(
            store,
            estimate=10,
            kind="scrape",
            fingerprint=FP1,
            key_ref=ref_of(K1),
            accept=True,
        )
        rid2 = await _seed(store, estimate=2, kind="search", fingerprint=FP2)
        pending_before = [r.reservation_id for r in await store.list_pending()]
        usage_before = await store.usage()
        long_amount = "1" * 17
        cases = [
            (f"fc pending resolve {rid}", module._RESOLVE_USAGE_TEXT),
            (f"fc pending resolve {rid} nan", module._RESOLVE_INVALID_TEXT),
            (f"fc pending resolve {rid} inf", module._RESOLVE_INVALID_TEXT),
            (f"fc pending resolve {rid} -1", module._RESOLVE_INVALID_TEXT),
            (f"fc pending resolve {rid} {long_amount}", module._RESOLVE_INVALID_TEXT),
            (f"fc pending resolve {rid} 0.1234567", module._RESOLVE_INVALID_TEXT),
            (f"fc pending resolve {rid} abc", module._RESOLVE_INVALID_TEXT),
            (f"fc pending resolve {rid} 1 extra", module._RESOLVE_USAGE_TEXT),
            ("fc pending resolve no-such-rid 1", module._RESOLVE_NOT_ALLOWED_TEXT),
            (f"fc pending resolve {rid2} 1", module._RESOLVE_NOT_ALLOWED_TEXT),
            ("fc credit now", module._CREDIT_USAGE_TEXT),
        ]
        for message, expected in cases:
            assert await _run(plugin, message, admin=True) == expected, message
        assert [r.reservation_id for r in await store.list_pending()] == pending_before
        usage_after = await store.usage()
        assert usage_after.used == usage_before.used
        assert usage_after.active_reserved == usage_before.active_reserved
        for rid_now in (rid, rid2):
            record = await store.get(rid_now)
            assert record.state.value in ("accepted", "reserved")
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_output_truncated_to_limit(main_sdk, tmp_path):
    calls: list = []

    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "remainingCredits": 1,
                    "planCredits": 2,
                    "billingPeriodStart": "2026-11-01",
                    "billingPeriodEnd": "x" * 7000,
                },
            },
        )

    plugin = await _wired_plugin(
        main_sdk, tmp_path, keys=(K1,), transport=_recording_transport(calls, responder)
    )
    try:
        output = await _run(plugin, "fc credit", admin=True)
        assert len(output) <= 6000
        assert "x" * 7000 not in output
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_pending_list_read_failure_returns_fixed_text(main_sdk, tmp_path):
    module = main_sdk.module
    plugin = await _wired_plugin(main_sdk, tmp_path, keys=(K1,))
    start = len(main_sdk.logger.messages)
    try:

        class FailingStore:
            async def list_pending(self):
                raise RuntimeError("db SECRET detail")

            async def usage(self):
                raise RuntimeError("db SECRET detail")

        plugin.store = FailingStore()
        output = await _run(plugin, "fc pending list", admin=True)
        assert output == module._PENDING_READ_FAILED_TEXT
        logged = "\n".join(main_sdk.logger.messages[start:])
        assert "RuntimeError" in logged
        assert "SECRET" not in logged
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_watch_commands_registered_as_admin(main_sdk, tmp_path):
    plugin = await _wired_plugin(main_sdk, tmp_path)
    try:
        records = {record["name"]: record for record in main_sdk.registered}
        assert records["fc watch"]["is_group"] is True
        assert records["fc watch"]["admin"] is True
        for command in ("add", "list", "remove", "check"):
            record = records[f"fc watch {command}"]
            assert record["is_group"] is False
            assert record["admin"] is True
        assert "/fc watch" in await _run(plugin, "/fc help")
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_watch_add_creates_subscription_and_job(main_sdk, tmp_path):
    plugin = await _wired_plugin(main_sdk, tmp_path)
    try:
        text = await _run(plugin, "/fc watch add https://example.com/a 10", umo="u1")
        assert "已订阅" in text and "订阅ID" in text
        subs = await plugin.watch_store.list_all()
        assert len(subs) == 1
        sub = subs[0]
        assert sub.umo == "u1"
        assert sub.url == "https://example.com/a"
        assert sub.interval_minutes == 10
        assert sub.tag.startswith("fcwatch-")
        assert sub.next_check_at == sub.created_at
        jobs = plugin.context.cron_manager.jobs
        assert len(jobs) == 1
        job = next(iter(jobs.values()))
        assert job.payload == {"subscription_id": sub.id}
        assert (await plugin.watch_store.get(sub.id)).cron_job_id == job.job_id
        again = await _run(plugin, "/fc watch add https://example.com/a 15", umo="u1")
        assert "同一会话已订阅此 URL" in again
        assert len(await plugin.watch_store.list_all()) == 1
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_watch_add_rejects_bad_args_without_state(main_sdk, tmp_path):
    plugin = await _wired_plugin(main_sdk, tmp_path)
    try:
        assert "用法" in await _run(plugin, "/fc watch add", umo="u1")
        assert "用法" in await _run(
            plugin, "/fc watch add https://example.com/a", umo="u1"
        )
        assert "用法" in await _run(
            plugin, "/fc watch add https://example.com/a abc", umo="u1"
        )
        assert "订阅失败" in await _run(
            plugin, "/fc watch add https://example.com/a 3", umo="u1"
        )
        assert "用法" in await _run(
            plugin, "/fc watch add https://example.com/a 10 多余", umo="u1"
        )
        assert "会话" in await _run(
            plugin, "/fc watch add https://example.com/a 10", umo=""
        )
        assert await plugin.watch_store.list_all() == []
        assert plugin.context.cron_manager.jobs == {}
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_watch_add_rolls_back_when_cron_fails(main_sdk, tmp_path):
    plugin = await _wired_plugin(main_sdk, tmp_path)
    plugin.context.cron_manager.fail_add = True
    try:
        text = await _run(plugin, "/fc watch add https://example.com/a 10", umo="u1")
        assert "已回滚" in text
        assert await plugin.watch_store.list_all() == []
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_watch_list_filters_by_current_umo(main_sdk, tmp_path):
    plugin = await _wired_plugin(main_sdk, tmp_path)
    try:
        await _run(plugin, "/fc watch add https://example.com/a 10", umo="u1")
        await _run(plugin, "/fc watch add https://example.com/b 15", umo="u1")
        await _run(plugin, "/fc watch add https://example.com/c 5", umo="u2")
        text = await _run(plugin, "/fc watch list", umo="u1")
        assert "共 2 个" in text
        assert "https://example.com/a" in text
        assert "https://example.com/c" not in text
        empty = await _run(plugin, "/fc watch list", umo="u3")
        assert "没有" in empty
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_watch_remove_deletes_subscription_and_job(main_sdk, tmp_path):
    plugin = await _wired_plugin(main_sdk, tmp_path)
    try:
        await _run(plugin, "/fc watch add https://example.com/a 10", umo="u1")
        sub = (await plugin.watch_store.list_all())[0]
        result = await _run(plugin, f"/fc watch remove {sub.id}", umo="u1")
        assert "已删除" in result
        assert await plugin.watch_store.list_all() == []
        assert plugin.context.cron_manager.jobs == {}
        assert "不存在" in await _run(plugin, f"/fc watch remove {sub.id}", umo="u1")
        await _run(plugin, "/fc watch add https://example.com/b 10", umo="u2")
        other = (await plugin.watch_store.list_all())[0]
        cross = await _run(plugin, f"/fc watch remove {other.id}", umo="u1")
        assert "不属于当前会话" in cross
        assert len(await plugin.watch_store.list_all()) == 1
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_watch_check_runs_and_pushes_changed(main_sdk, tmp_path):
    calls: list = []

    def responder(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "success": True,
                "creditsUsed": 21,
                "data": {
                    "markdown": "body",
                    "changeTracking": {
                        "previousScrapeAt": "2026-05-01T00:00:00+00:00",
                        "changeStatus": "changed",
                        "visibility": "visible",
                        "diff": {"text": "- old\n+ new"},
                    },
                },
            },
        )

    plugin = await _wired_plugin(
        main_sdk, tmp_path, transport=_recording_transport(calls, responder)
    )
    try:
        await _run(plugin, "/fc watch add https://example.com/a 10", umo="u1")
        sub = (await plugin.watch_store.list_all())[0]
        text = await _run(plugin, f"/fc watch check {sub.id}", umo="u1")
        assert "已推送" in text
        assert plugin.context.sent[0][0] == "u1"
        assert "- old" in plugin.context.sent[0][1]
        assert len(calls) == 2
        payload = json.loads(calls[0].content)
        assert payload["formats"][-1]["type"] == "changeTracking"
        saved = await plugin.watch_store.get(sub.id)
        assert saved.last_status == "changed"
        assert saved.last_notification_hash is not None
        again = await _run(plugin, f"/fc watch check {sub.id}", umo="u1")
        assert "已跳过" in again
        assert len(plugin.context.sent) == 1
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_watch_check_missing_tracking_and_unknown_id(main_sdk, tmp_path):
    calls: list = []

    def responder(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"success": True, "data": {"markdown": "b"}})

    plugin = await _wired_plugin(
        main_sdk, tmp_path, transport=_recording_transport(calls, responder)
    )
    try:
        assert "不存在" in await _run(plugin, "/fc watch check missing", umo="u1")
        await _run(plugin, "/fc watch add https://example.com/a 10", umo="u1")
        sub = (await plugin.watch_store.list_all())[0]
        text = await _run(plugin, f"/fc watch check {sub.id}", umo="u1")
        assert "变更跟踪数据" in text
        saved = await plugin.watch_store.get(sub.id)
        assert saved.last_status == "unavailable"
        assert saved.next_check_at > saved.created_at
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_watch_commands_require_watch_runtime(main_sdk, tmp_path):
    plugin = await _wired_plugin(main_sdk, tmp_path, with_watch=False)
    try:
        text = await _run(plugin, "/fc watch add https://example.com/a 10", umo="u1")
        assert "运行时不可用" in text
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_watch_add_rejects_blocked_url_without_state(main_sdk, tmp_path):
    plugin = await _wired_plugin(
        main_sdk, tmp_path, config={"blocked_domains": ["x.com"]}
    )
    try:
        text = await _run(plugin, "/fc watch add https://x.com/post 10", umo="u1")
        assert "订阅失败" in text
        assert await plugin.watch_store.list_all() == []
        assert plugin.context.cron_manager.jobs == {}
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_watch_remove_failure_keeps_subscription_for_retry(main_sdk, tmp_path):
    plugin = await _wired_plugin(main_sdk, tmp_path)
    try:
        await _run(plugin, "/fc watch add https://example.com/a 10", umo="u1")
        sub = (await plugin.watch_store.list_all())[0]
        manager = plugin.context.cron_manager
        original_delete = manager.delete_job

        async def failing_delete(job_id):
            raise RuntimeError("cron unavailable")

        manager.delete_job = failing_delete
        text = await _run(plugin, f"/fc watch remove {sub.id}", umo="u1")
        assert "删除失败" in text
        saved = await plugin.watch_store.get(sub.id)
        assert saved.url == "https://example.com/a"
        assert saved.cron_job_id is not None
        assert len(manager.jobs) == 1
        manager.delete_job = original_delete
        retry = await _run(plugin, f"/fc watch remove {sub.id}", umo="u1")
        assert "已删除" in retry
        assert await plugin.watch_store.list_all() == []
        assert manager.jobs == {}
    finally:
        await plugin.terminate()
