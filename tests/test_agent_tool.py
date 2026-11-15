"""firecrawl_agent 工具层测试：schema、管理员护栏、payload 与错误脱敏。"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from astrbot_plugin_firecrawl.firecrawl_api.budget import (
    BudgetExceeded,
    InvalidBudgetTransition,
)
from astrbot_plugin_firecrawl.firecrawl_api.errors import ErrorCategory, FirecrawlError
from astrbot_plugin_firecrawl.firecrawl_api.tasks import make_task_handle
from astrbot_plugin_firecrawl.tools.execution import PendingRequestError

_FAKE_KEY = "fc-fake123456"
_SECRET_URL = "https://api.firecrawl.dev/v2/agent/secret-job"
_PROMPT = "调研机密内容"
_UMO = "qq:group:1"
_KEY_REF = "a" * 64
_HANDLE = make_task_handle("agent", "job-1", _KEY_REF)
_CRAWL_HANDLE = make_task_handle("crawl", "job-1", _KEY_REF)
_HIDDEN = ("webhook", "threadId", "thread_id", "model", "effort")


class _CountingExecutor:
    """执行器替身：被调用即失败，用于证明工具层零网络。"""

    def __init__(self) -> None:
        self.calls = 0

    async def submit(self, *args: Any, **kwargs: Any) -> None:
        self.calls += 1
        raise AssertionError("executor 不应被调用")

    async def status(self, *args: Any, **kwargs: Any) -> None:
        self.calls += 1
        raise AssertionError("executor 不应被调用")

    async def cancel(self, *args: Any, **kwargs: Any) -> None:
        self.calls += 1
        raise AssertionError("executor 不应被调用")


class FakeService:
    """AgentService 替身：记录 submit/query/cancel 并按预设返回或抛出。"""

    def __init__(self) -> None:
        self.submit_calls: list[tuple] = []
        self.query_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self.error: BaseException | None = None
        self.submit_result = {
            "task_handle": _HANDLE,
            "reservation_id": "r-1",
            "status": "processing",
        }
        self.query_result: dict = {"status": "processing"}
        self.cancel_result: dict = {
            "status": "cancelled",
            "creditsUsed": 1,
            "cancel_requested": True,
            "task_handle": _HANDLE,
        }

    async def submit(self, *, payload, estimate, umo):
        self.submit_calls.append((payload, estimate, umo))
        if self.error is not None:
            raise self.error
        return dict(self.submit_result)

    async def query(self, task_handle: str):
        self.query_calls.append(task_handle)
        if self.error is not None:
            raise self.error
        return dict(self.query_result)

    async def cancel(self, task_handle: str):
        self.cancel_calls.append(task_handle)
        if self.error is not None:
            raise self.error
        return dict(self.cancel_result)


class FakePlugin:
    """最小插件替身：config、Agent 服务、计数 request_scope 与零网络执行器。"""

    def __init__(self, service: FakeService | None, config: dict | None = None) -> None:
        self.config = config or {"blocked_domains": []}
        self.agent_service = service
        self.executor = _CountingExecutor()
        self.scope_entries = 0

    @asynccontextmanager
    async def request_scope(self):
        self.scope_entries += 1
        yield


class StubEvent:
    """最小事件替身：is_admin 与会话来源。"""

    def __init__(self, is_admin: bool = True, umo: str | None = _UMO) -> None:
        self._is_admin = is_admin
        self.unified_msg_origin = umo

    def is_admin(self) -> bool:
        return self._is_admin


def _context(event: Any) -> SimpleNamespace:
    return SimpleNamespace(context=SimpleNamespace(event=event))


def admin_context(umo: str | None = _UMO) -> SimpleNamespace:
    return _context(StubEvent(True, umo))


def member_context() -> SimpleNamespace:
    return _context(StubEvent(False, _UMO))


def _walk_schema(node: Any) -> None:
    """递归断言 schema 不含 anyOf 与 default。"""
    if isinstance(node, dict):
        assert "anyOf" not in node
        assert "default" not in node
        for value in node.values():
            _walk_schema(value)
    elif isinstance(node, list):
        for item in node:
            _walk_schema(item)


@pytest.fixture()
def agent_tool(sdk_tool_loader):
    """经 sdk_tool_loader 以独立 alias 加载 tools/agent.py。"""
    with sdk_tool_loader("agent", "FirecrawlAgentTool") as tool_class:
        yield tool_class


def _zero_io(plugin: FakePlugin, service: FakeService | None) -> None:
    assert plugin.scope_entries == 0
    assert plugin.executor.calls == 0
    if service is not None:
        assert service.submit_calls == []
        assert service.query_calls == []
        assert service.cancel_calls == []


def test_tool_metadata_matches_schema(agent_tool):
    """name、描述、action 枚举与显式参数须与 schema 一致，且不暴露内部字段。"""
    tool = agent_tool()
    assert tool.name == "firecrawl_agent"
    assert tool.description == "仅管理员使用的 Agent 后台研究任务。"
    schema = tool.parameters
    assert schema["required"] == ["action"]
    props = schema["properties"]
    assert list(props) == [
        "action",
        "prompt",
        "max_credits",
        "task_handle",
        "urls",
        "schema",
        "strict_constrain_to_urls",
    ]
    assert props["action"]["enum"] == ["start", "status", "cancel"]
    assert "1–2500" in props["max_credits"]["description"]
    named = [
        name
        for name, param in inspect.signature(agent_tool.call).parameters.items()
        if name not in ("self", "context")
        and param.kind is not inspect.Parameter.VAR_KEYWORD
    ]
    assert named == list(props)
    encoded = json.dumps(schema, ensure_ascii=False)
    for hidden in _HIDDEN:
        assert hidden not in encoded
    _walk_schema(schema)


def test_schema_instances_do_not_share_state(agent_tool):
    """每次实例化得到全新 schema，嵌套结构互不共享。"""
    first = agent_tool()
    second = agent_tool()
    assert first.parameters is not second.parameters
    assert first.parameters["properties"] is not second.parameters["properties"]
    first.parameters["properties"]["action"]["enum"].append("hacked")
    assert second.parameters["properties"]["action"]["enum"] == [
        "start",
        "status",
        "cancel",
    ]


def test_repr_hides_injected_plugin(agent_tool):
    """repr 只含 name、parameters 与 description，不泄露注入的假敏感配置。"""
    plugin = FakePlugin(FakeService(), {"api_keys": [_FAKE_KEY]})
    tool = agent_tool(plugin=plugin)
    text = repr(tool)
    assert _FAKE_KEY not in text
    assert "api_keys" not in text


@pytest.mark.asyncio
async def test_call_without_plugin_returns_config(agent_tool):
    """管理员通过但未注入 plugin 时零 I/O 返回 config 错误。"""
    tool = agent_tool()
    result = json.loads(
        await tool.call(admin_context(), action="start", prompt="x", max_credits=1)
    )
    assert result["ok"] is False
    assert result["category"] == "config"
    assert "插件未初始化" in result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["start", "status", "cancel"])
async def test_non_admin_zero_calls(agent_tool, action):
    """每个 action 在无事件或非管理员时零服务调用、零网络、零预留。"""
    service = FakeService()
    plugin = FakePlugin(service)
    tool = agent_tool(plugin=plugin)
    kwargs = {
        "action": action,
        "prompt": _PROMPT,
        "max_credits": 30,
        "task_handle": _HANDLE,
    }
    for context in (
        None,
        SimpleNamespace(context=None),
        SimpleNamespace(context=SimpleNamespace(event=None)),
        member_context(),
    ):
        result = json.loads(await tool.call(context, **kwargs))
        assert result["ok"] is False
        assert result["category"] == "usage"
        assert "仅管理员" in result["error"]
        assert _PROMPT not in json.dumps(result, ensure_ascii=False)
        assert _HANDLE not in json.dumps(result, ensure_ascii=False)
    _zero_io(plugin, service)


@pytest.mark.asyncio
async def test_start_missing_umo_zero_submit(agent_tool):
    """管理员但会话来源缺失或空白时拒绝 start，不提交；非空值会 strip。"""
    service = FakeService()
    plugin = FakePlugin(service)
    tool = agent_tool(plugin=plugin)
    for umo in ("", "   ", "\t\n", None):
        result = json.loads(
            await tool.call(
                admin_context(umo=umo),
                action="start",
                prompt=_PROMPT,
                max_credits=30,
            )
        )
        assert result["ok"] is False
        assert result["category"] == "usage"
        assert "会话来源" in result["error"]
        assert _PROMPT not in json.dumps(result, ensure_ascii=False)
    _zero_io(plugin, service)

    cleaned = json.loads(
        await tool.call(
            admin_context(umo=f"  {_UMO}  "),
            action="start",
            prompt=_PROMPT,
            max_credits=30,
        )
    )
    assert cleaned["ok"] is True
    assert service.submit_calls[0][2] == _UMO


@pytest.mark.asyncio
async def test_start_payload_estimate_and_blocked_domain(agent_tool):
    """start 组装 payload 与费用上界，命中 blocked_domains 时零提交且不回显域名。"""
    service = FakeService()
    plugin = FakePlugin(
        service,
        {"blocked_domains": ["evil.com"], "max_content_chars": 8000},
    )
    tool = agent_tool(plugin=plugin)
    result = json.loads(
        await tool.call(
            admin_context(),
            action="start",
            prompt=f"  {_PROMPT}  ",
            max_credits=30,
            urls=["https://docs.example.com/"],
            schema={"type": "object"},
            strict_constrain_to_urls=True,
        )
    )
    assert result["ok"] is True
    assert result["data"] == {
        "task_handle": _HANDLE,
        "reservation_id": "r-1",
        "status": "processing",
    }
    assert service.submit_calls == [
        (
            {
                "prompt": _PROMPT,
                "maxCredits": 30,
                "urls": ["https://docs.example.com/"],
                "schema": {"type": "object"},
                "strictConstrainToURLs": True,
            },
            30,
            _UMO,
        )
    ]
    assert service.query_calls == []
    assert service.cancel_calls == []
    assert plugin.executor.calls == 0

    blocked = json.loads(
        await tool.call(
            admin_context(),
            action="start",
            prompt=_PROMPT,
            max_credits=5,
            urls=["https://sub.evil.com/x"],
        )
    )
    assert blocked["ok"] is False
    assert blocked["category"] == "usage"
    text = json.dumps(blocked, ensure_ascii=False)
    assert "evil.com" not in text
    assert _PROMPT not in text
    assert len(service.submit_calls) == 1
    assert plugin.scope_entries == 0
    assert plugin.executor.calls == 0


@pytest.mark.asyncio
async def test_status_and_cancel_ignore_start_params_and_reject_kind(agent_tool):
    """status/cancel 只使用 agent 句柄；错误类型句柄零查询且不回显。"""
    service = FakeService()
    plugin = FakePlugin(service)
    tool = agent_tool(plugin=plugin)
    status = json.loads(
        await tool.call(
            admin_context(),
            action="status",
            prompt=_PROMPT,
            max_credits=99,
            task_handle=_HANDLE,
            urls=["https://example.com/secret"],
        )
    )
    assert status["ok"] is True
    assert status["data"]["status"] == "processing"
    assert status["data"]["task_handle"] == _HANDLE
    assert service.query_calls == [_HANDLE]
    assert service.submit_calls == []
    assert service.cancel_calls == []
    assert _PROMPT not in json.dumps(status, ensure_ascii=False)

    cancel = json.loads(
        await tool.call(
            admin_context(),
            action="cancel",
            prompt=_PROMPT,
            task_handle=_HANDLE,
        )
    )
    assert cancel["ok"] is True
    assert cancel["data"]["cancel_requested"] is True
    assert cancel["data"]["task_handle"] == _HANDLE
    assert service.cancel_calls == [_HANDLE]

    rejected = json.loads(
        await tool.call(
            admin_context(),
            action="status",
            task_handle=_CRAWL_HANDLE,
        )
    )
    assert rejected["ok"] is False
    assert rejected["category"] == "usage"
    assert "agent" in rejected["error"]
    assert _CRAWL_HANDLE not in json.dumps(rejected, ensure_ascii=False)
    assert service.query_calls == [_HANDLE]
    assert plugin.executor.calls == 0
    assert plugin.scope_entries == 0


@pytest.mark.asyncio
async def test_pending_and_errors_are_masked(agent_tool, caplog):
    """PendingRequestError 走 pending；Firecrawl/未知异常脱敏且日志只记类型。"""
    service = FakeService()
    plugin = FakePlugin(service)
    tool = agent_tool(plugin=plugin)
    service.error = PendingRequestError("r-1", task_handle=_HANDLE)
    pending = json.loads(
        await tool.call(admin_context(), action="status", task_handle=_HANDLE)
    )
    assert pending["ok"] is False
    assert pending["pending"] is True
    assert pending["reservation_id"] == "r-1"
    assert pending["task_handle"] == _HANDLE

    service.error = FirecrawlError(
        ErrorCategory.AUTH, f"unauthorized {_FAKE_KEY} {_SECRET_URL}"
    )
    failed = json.loads(
        await tool.call(admin_context(), action="cancel", task_handle=_HANDLE)
    )
    assert failed["ok"] is False
    text = json.dumps(failed, ensure_ascii=False)
    assert _FAKE_KEY not in text
    assert _SECRET_URL not in text

    service.error = RuntimeError(f"{_FAKE_KEY} {_SECRET_URL} {_PROMPT}")
    with caplog.at_level(logging.WARNING, logger="astrbot.stub"):
        unknown = json.loads(
            await tool.call(
                admin_context(), action="start", prompt=_PROMPT, max_credits=30
            )
        )
    assert unknown["ok"] is False
    assert unknown["category"] == "usage"
    assert unknown["error"] == "Agent 任务操作失败。"
    dumped = json.dumps(unknown, ensure_ascii=False)
    assert _FAKE_KEY not in dumped
    assert _SECRET_URL not in dumped
    assert _PROMPT not in dumped
    records = [item for item in caplog.records if item.name == "astrbot.stub"]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "RuntimeError" in message
    assert _FAKE_KEY not in message
    assert _SECRET_URL not in message
    assert _PROMPT not in message


@pytest.mark.asyncio
async def test_cancelled_error_propagates(agent_tool):
    """CancelledError 原样传播，不封装为 JSON。"""
    service = FakeService()
    plugin = FakePlugin(service)
    tool = agent_tool(plugin=plugin)
    service.error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await tool.call(admin_context(), action="status", task_handle=_HANDLE)


@pytest.mark.asyncio
async def test_service_missing_rejects_without_io(agent_tool):
    """agent_service 不可用时明确拒绝，不创建服务或网络。"""
    plugin = FakePlugin(None)
    tool = agent_tool(plugin=plugin)
    for action, kwargs in (
        ("start", {"prompt": _PROMPT, "max_credits": 30}),
        ("status", {"task_handle": _HANDLE}),
        ("cancel", {"task_handle": _HANDLE}),
    ):
        result = json.loads(await tool.call(admin_context(), action=action, **kwargs))
        assert result["ok"] is False
        assert result["category"] == "config"
        assert "Agent 服务不可用" in result["error"]
        assert _PROMPT not in json.dumps(result, ensure_ascii=False)
    _zero_io(plugin, None)


@pytest.mark.asyncio
async def test_unknown_fields_hidden_and_zero_io(agent_tool):
    """未知字段携带假标记时固定 usage，不回显名称/值。"""
    service = FakeService()
    plugin = FakePlugin(service)
    tool = agent_tool(plugin=plugin)
    result = json.loads(
        await tool.call(
            admin_context(),
            action="start",
            prompt=_PROMPT,
            max_credits=30,
            webhook=_SECRET_URL,
            api_key=_FAKE_KEY,
        )
    )
    assert result["ok"] is False
    assert result["category"] == "usage"
    text = json.dumps(result, ensure_ascii=False)
    assert _SECRET_URL not in text
    assert _FAKE_KEY not in text
    _zero_io(plugin, service)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["start", "status", "cancel"])
async def test_admin_check_precedes_plugin_and_unknown_fields(agent_tool, action):
    """权限是 call 的第一项校验：非管理员即使 plugin 缺失或带未知字段也只返回权限错误。"""
    kwargs = {
        "action": action,
        "prompt": _PROMPT,
        "max_credits": 30,
        "task_handle": _HANDLE,
        "webhook": _SECRET_URL,
    }
    bare = agent_tool()
    for context in (
        None,
        member_context(),
        _context(SimpleNamespace(unified_msg_origin=_UMO)),
    ):
        result = json.loads(await bare.call(context, **kwargs))
        assert result["ok"] is False
        assert result["category"] == "usage"
        assert "仅管理员" in result["error"]
        text = json.dumps(result, ensure_ascii=False)
        assert _SECRET_URL not in text
        assert "不支持的 Agent 参数" not in text
        assert "插件未初始化" not in text

    service = FakeService()
    plugin = FakePlugin(service)
    tool = agent_tool(plugin=plugin)
    result = json.loads(await tool.call(member_context(), **kwargs))
    assert result["ok"] is False
    assert "仅管理员" in result["error"]
    _zero_io(plugin, service)


@pytest.mark.asyncio
async def test_budget_errors_keep_classification(agent_tool):
    """BudgetExceeded 与非法预留状态均走 FirecrawlError/usage，不落入通用内部错误。"""
    service = FakeService()
    plugin = FakePlugin(service)
    tool = agent_tool(plugin=plugin)

    service.error = BudgetExceeded("日预算已用尽")
    exceeded = json.loads(
        await tool.call(admin_context(), action="start", prompt=_PROMPT, max_credits=30)
    )
    assert exceeded["ok"] is False
    assert exceeded["category"] == "usage"
    assert "日预算已用尽" in exceeded["error"]
    assert exceeded["error"] != "Agent 任务操作失败。"
    assert "插件内部错误" not in exceeded["error"]

    service.error = InvalidBudgetTransition("不允许从 accepted 迁移到 released")
    illegal = json.loads(
        await tool.call(admin_context(), action="start", prompt=_PROMPT, max_credits=30)
    )
    assert illegal["ok"] is False
    assert illegal["category"] == "usage"
    assert "不允许从 accepted 迁移到 released" in illegal["error"]
    assert illegal["error"] != "Agent 任务操作失败。"
    assert "插件内部错误" not in illegal["error"]
