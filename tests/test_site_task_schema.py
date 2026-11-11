"""firecrawl_site 任务动作回归：schema 契约、status/cancel 权限与参数拒绝，零 I/O。"""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace

import httpx
import pytest
from astrbot_plugin_firecrawl.firecrawl_api.errors import classify_error

MARKER = "fc-FAKE-SECRET-MARKER"
HANDLE = "crawl:task-1@" + "a" * 64
BATCH_HANDLE = "batch:task_2@" + "b" * 64
STATUS_RESPONSE = {"status": "scraping", "data": None, "next": None}


class StubExecutor:
    """计数执行器替身：status/cancel 记录收到的 job_id 并返回固定响应。"""

    def __init__(self) -> None:
        self.status_calls = 0
        self.cancel_calls = 0
        self.received = []

    async def status(self, job_id):
        self.status_calls += 1
        self.received.append(("status", job_id))
        return httpx.Response(200, json=STATUS_RESPONSE)

    async def cancel(self, job_id):
        self.cancel_calls += 1
        self.received.append(("cancel", job_id))
        return httpx.Response(200, json=STATUS_RESPONSE)


class StubEvent:
    """最小事件替身：仅提供 is_admin 的结果。"""

    def __init__(self, is_admin: bool) -> None:
        self._is_admin = is_admin

    def is_admin(self) -> bool:
        return self._is_admin


class StubContext:
    """最小调用上下文替身：模拟 AstrBot 的 context.context.event 访问链。"""

    def __init__(self, is_admin: bool) -> None:
        self.context = SimpleNamespace(event=StubEvent(is_admin))


class FakeRuntime:
    """最小插件替身：config + executor + 自带计数的 request_scope。"""

    def __init__(self, executor=None, config=None) -> None:
        self.config = config or {}
        self.executor = executor if executor is not None else StubExecutor()
        self.scope_entered = 0

    def request_scope(self):
        return self

    async def __aenter__(self):
        self.scope_entered += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _walk_schema(node) -> None:
    """递归断言 schema 不含 anyOf 与 default。"""
    if isinstance(node, dict):
        assert "anyOf" not in node
        assert "default" not in node
        for value in node.values():
            _walk_schema(value)
    elif isinstance(node, list):
        for item in node:
            _walk_schema(item)


def test_task_schema_contract(firecrawl_site_tool):
    """schema：action 枚举含五个动作，properties 含 job_id，required 只有 action。"""
    tool = firecrawl_site_tool()
    schema = tool.parameters
    assert schema["type"] == "object"
    assert schema["required"] == ["action"]
    assert schema["properties"]["action"]["enum"] == [
        "map",
        "crawl",
        "batch",
        "status",
        "cancel",
    ]
    assert "job_id" in schema["properties"]
    assert schema["properties"]["job_id"]["type"] == "string"
    url_description = schema["properties"]["url"]["description"]
    assert "仅 map/crawl 必填" in url_description
    assert "status/cancel 不要传 url" in url_description
    urls_description = schema["properties"]["urls"]["description"]
    assert "任一 URL 校验失败或命中 blocked_domains 都会整批拒绝" in urls_description
    assert "不自动剔除" in urls_description
    _walk_schema(schema)


def test_task_schema_signature_and_isolation(firecrawl_site_tool):
    """keyword-only 参数与 properties 顺序一致，实例嵌套结构互不共享。"""
    first = firecrawl_site_tool()
    second = firecrawl_site_tool()
    props = first.parameters["properties"]
    named = [
        name
        for name, param in inspect.signature(first.call).parameters.items()
        if param.kind is inspect.Parameter.KEYWORD_ONLY
    ]
    assert named == list(props)
    var_keyword = [
        name
        for name, param in inspect.signature(first.call).parameters.items()
        if param.kind is inspect.Parameter.VAR_KEYWORD
    ]
    assert var_keyword == ["unexpected"]
    assert first.parameters is not second.parameters
    assert first.parameters["properties"] is not second.parameters["properties"]
    first.parameters["properties"]["job_id"]["description"] = "unshared-marker"
    assert second.parameters["properties"]["job_id"]["description"] != "unshared-marker"


@pytest.mark.asyncio
async def test_status_and_cancel_bypass_admin_gate(firecrawl_site_tool):
    """site_admin_only=True 时 member 与 None 上下文均可 status/cancel，各调用一次。"""
    executor = StubExecutor()
    plugin = FakeRuntime(
        executor,
        {"site_max_pages": 2, "max_content_chars": 8000, "site_admin_only": True},
    )
    tool = firecrawl_site_tool(plugin=plugin)

    out = await tool.call(StubContext(is_admin=False), action="status", job_id=HANDLE)
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["data"]["status"] == "scraping"
    assert executor.status_calls == 1
    assert executor.cancel_calls == 0
    assert executor.received == [("status", HANDLE)]
    assert plugin.scope_entered == 1

    out = await tool.call(None, action="cancel", job_id=BATCH_HANDLE)
    payload = json.loads(out)
    assert payload["ok"] is True
    assert executor.cancel_calls == 1
    assert executor.received == [("status", HANDLE), ("cancel", BATCH_HANDLE)]
    assert plugin.scope_entered == 2


@pytest.mark.asyncio
async def test_same_instance_no_permission_cache(firecrawl_site_tool):
    """同一工具实例先 status 后 cancel，member 上下文两次都放行且不缓存权限判定。"""
    executor = StubExecutor()
    plugin = FakeRuntime(
        executor,
        {"site_max_pages": 2, "max_content_chars": 8000, "site_admin_only": True},
    )
    tool = firecrawl_site_tool(plugin=plugin)
    member = StubContext(is_admin=False)

    for action, handle in (("status", HANDLE), ("cancel", BATCH_HANDLE)):
        payload = json.loads(await tool.call(member, action=action, job_id=handle))
        assert payload["ok"] is True
    assert executor.status_calls == 1
    assert executor.cancel_calls == 1


_REJECTIONS = [
    ("bad_handle", {"action": "status", "job_id": "crawl:" + MARKER + "@/short"}),
    ("missing_job_id", {"action": "cancel"}),
    ("url_on_status", {"action": "status", "job_id": HANDLE, "url": "https://x.com"}),
    (
        "urls_on_status",
        {"action": "status", "job_id": HANDLE, "urls": ["https://x.com"]},
    ),
    (
        "urls_on_cancel",
        {"action": "cancel", "job_id": HANDLE, "urls": ["https://x.com"]},
    ),
    ("unknown_field", {"action": "status", "job_id": HANDLE, "bogus": "x"}),
]


@pytest.mark.parametrize(
    ("case", "kwargs"), _REJECTIONS, ids=[c for c, _ in _REJECTIONS]
)
@pytest.mark.asyncio
async def test_rejections_before_executor_no_echo(firecrawl_site_tool, case, kwargs):
    """非法参数在网络与执行器前拒绝，错误输出不回显敏感标记。"""
    executor = StubExecutor()
    plugin = FakeRuntime(
        executor,
        {"site_max_pages": 2, "max_content_chars": 8000, "site_admin_only": True},
    )
    tool = firecrawl_site_tool(plugin=plugin)

    out = await tool.call(StubContext(is_admin=False), **kwargs)
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["category"] == "usage"
    assert MARKER not in out
    assert executor.status_calls == 0
    assert executor.cancel_calls == 0
    assert plugin.scope_entered == 0


@pytest.mark.asyncio
async def test_cancel_completed_maps_to_fixed_hint(firecrawl_site_tool):
    """已完成任务取消返回固定提示，不回显远端原文。"""
    executor = StubExecutor()
    remote_error = classify_error(
        response=httpx.Response(409, json={"error": "Crawl is already completed"})
    )

    async def completed_cancel(job_id):
        executor.cancel_calls += 1
        executor.received.append(("cancel", job_id))
        raise remote_error

    executor.cancel = completed_cancel
    plugin = FakeRuntime(executor)
    tool = firecrawl_site_tool(plugin=plugin)

    out = await tool.call(None, action="cancel", job_id=HANDLE)
    payload = json.loads(out)
    assert payload["error"] == "任务已完成，无需取消。"
    assert payload["hint"] == "请使用 action=status 和原 job_id 查询任务结果。"
    assert payload["category"] == "usage"
    assert payload["retryable"] is False
    assert "Crawl is already completed" not in out
    assert executor.cancel_calls == 1
    assert plugin.scope_entered == 1
