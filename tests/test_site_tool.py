"""firecrawl_site FunctionTool 长期回归：schema 契约、预算 map、参数拒绝与失败脱敏。"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from astrbot_plugin_firecrawl.firecrawl_api.budget import (
    BudgetPolicy,
    ReservationState,
)
from astrbot_plugin_firecrawl.firecrawl_api.client import FirecrawlClient
from astrbot_plugin_firecrawl.firecrawl_api.keys import KeyPool
from astrbot_plugin_firecrawl.persistence import SQLiteStore
from astrbot_plugin_firecrawl.tools.execution import BudgetedExecutor

URL = "https://example.com"
SEARCH = " docs "
EXAMPLE = {"action": "map", "url": "https://example.com"}
MARKER = "fc-site-secret-marker"
UNICODE_DOMAIN = "例え.com"
CONFIG = {"site_max_pages": 2, "max_content_chars": 8000, "site_admin_only": True}
LINKS = ["https://a.example/1", "https://b.example/2", "https://c.example/3"]
MAP_OK = {"success": True, "warning": "w", "creditsUsed": 1, "links": LINKS}


class StubExecutor:
    """计数执行器替身：被调用即计数并抛出预设异常，专用于拒绝路径。"""

    def __init__(self, error=None) -> None:
        self.calls = 0
        self.submits = 0
        self.error = error or AssertionError("拒绝路径不应进入执行器。")

    async def execute_sync(self, kind, payload, *, estimate):
        self.calls += 1
        raise self.error

    async def submit(self, kind, payload, *, estimate):
        self.submits += 1
        raise self.error


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


@asynccontextmanager
async def open_plugin(
    tmp_path,
    responses,
    config=None,
    *,
    keys=("fc-site-tool-key-1",),
    policy=None,
    on_request=None,
):
    """真实 SQLiteStore + KeyPool + FirecrawlClient + BudgetedExecutor 组装插件替身。"""
    store = SQLiteStore(tmp_path / "budget.db", policy=policy or BudgetPolicy())
    await store.initialize()
    seen = []
    queue = list(responses)

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if on_request is not None:
            await on_request(store)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return httpx.Response(200, json=item) if isinstance(item, dict) else item

    client = FirecrawlClient(
        KeyPool(list(keys)), transport=httpx.MockTransport(handler)
    )
    runtime = FakeRuntime(BudgetedExecutor(client, store), config)
    runtime.seen, runtime.store, runtime.client = seen, store, client
    try:
        yield runtime
    finally:
        await client.aclose()


async def assert_usage(runtime, used, active_reserved):
    snapshot = await runtime.store.usage()
    assert snapshot.used == used
    assert snapshot.active_reserved == active_reserved


def test_site_schema_contract_and_nested_isolation(firecrawl_site_tool):
    first = firecrawl_site_tool()
    second = firecrawl_site_tool()
    schema = first.parameters
    assert first.name == "firecrawl_site"
    declared = {
        name
        for name, parameter in inspect.signature(first.call).parameters.items()
        if parameter.kind == inspect.Parameter.KEYWORD_ONLY
    }
    assert declared == set(schema["properties"])
    assert schema["type"] == "object"
    assert schema["required"] == ["action"]
    assert sorted(schema["properties"]) == [
        "action",
        "exclude_paths",
        "include_paths",
        "job_id",
        "limit",
        "search",
        "url",
        "urls",
    ]
    assert schema["properties"]["action"]["enum"] == [
        "map",
        "crawl",
        "batch",
        "status",
        "cancel",
    ]
    assert "anyOf" not in schema
    assert "default" not in schema
    assert all(
        "default" not in prop and "anyOf" not in prop
        for prop in schema["properties"].values()
    )
    assert "crawl" in first.description and "batch" in first.description
    action_description = schema["properties"]["action"]["description"]
    assert "crawl" in action_description and "batch" in action_description
    first.parameters["properties"]["action"]["enum"].append("unshared-marker")
    assert second.parameters["properties"]["action"]["enum"] == [
        "map",
        "crawl",
        "batch",
        "status",
        "cancel",
    ]


@pytest.mark.asyncio
async def test_site_without_plugin_config_error_and_repr_hides_config(
    firecrawl_site_tool,
):
    plugin = FakeRuntime(config={"api_key": MARKER})
    tool = firecrawl_site_tool(plugin=plugin)
    out = await firecrawl_site_tool().call(None, action="map", url=URL)
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["category"] == "config"
    assert payload["error"] == "插件未初始化，无法调用 firecrawl_site。"
    assert "example" not in payload
    assert plugin.scope_entered == 0
    assert MARKER not in repr(tool)
    assert "plugin=" not in repr(tool)


@pytest.mark.parametrize(
    ("limit", "expected_limit"),
    [(None, 2), (1, 1)],
    ids=["default_limit", "explicit_smaller"],
)
@pytest.mark.asyncio
async def test_map_trims_links_to_payload_limit(
    firecrawl_site_tool, tmp_path, limit, expected_limit
):
    snapshots = []

    async def on_request(store):
        snapshots.append(await store.list_pending())

    kwargs = {"action": "map", "url": URL, "search": SEARCH}
    if limit is not None:
        kwargs["limit"] = limit
    async with open_plugin(
        tmp_path, [MAP_OK], config=CONFIG, on_request=on_request
    ) as runtime:
        out = await firecrawl_site_tool(plugin=runtime).call(None, **kwargs)
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["data"] == {
        "success": True,
        "warning": "w",
        "creditsUsed": 1,
        "links": LINKS[:expected_limit],
        "linksTruncated": True,
    }
    request = runtime.seen[0]
    assert request.method == "POST"
    assert request.url.path == "/v2/map"
    assert json.loads(request.content) == {
        "url": URL,
        "limit": expected_limit,
        "search": "docs",
    }
    assert len(runtime.seen) == 1
    assert runtime.scope_entered == 1
    [pending] = snapshots
    assert len(pending) == 1
    assert pending[0].state is ReservationState.SUBMITTING
    assert pending[0].estimate == 1
    await assert_usage(runtime, 1.0, 0.0)
    assert await runtime.store.list_pending() == []


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        pytest.param({"url": URL}, "action", id="missing_action"),
        pytest.param({"action": "watch", "url": URL}, "action", id="unknown_action"),
        pytest.param({"action": 7, "url": URL}, "action", id="bad_action_type"),
        pytest.param({"action": "map"}, "url", id="missing_url"),
        pytest.param(
            {"action": "map", "url": URL, "limit": 3}, "limit", id="limit_over_max"
        ),
        pytest.param(
            {"action": "map", "url": f"https://{UNICODE_DOMAIN}"},
            "blocked_domains",
            id="blocked_unicode_host",
        ),
        pytest.param(
            {"action": "map", "url": f"https://sub.{UNICODE_DOMAIN}"},
            "blocked_domains",
            id="blocked_unicode_subdomain",
        ),
        pytest.param(
            {"action": "map", "url": URL, "api_key": MARKER},
            "不支持的站点参数",
            id="unknown_field",
        ),
        pytest.param(
            {"action": "crawl", "url": URL}, "limit 必填", id="crawl_missing_limit"
        ),
        pytest.param({"action": "batch"}, "urls 必填", id="batch_missing_urls"),
        pytest.param(
            {"action": "map", "url": URL, "urls": [URL]},
            "urls 仅在 batch",
            id="map_with_urls",
        ),
        pytest.param(
            {"action": "crawl", "url": URL, "limit": 1, "search": SEARCH},
            "search 仅在 map",
            id="crawl_with_search",
        ),
        pytest.param(
            {"action": "batch", "urls": [URL], "limit": 1},
            "limit 仅在 map/crawl",
            id="batch_with_limit",
        ),
        pytest.param(
            {"action": "batch", "urls": [URL], "include_paths": ["/a"]},
            "include_paths 仅在 crawl",
            id="batch_with_include_paths",
        ),
        pytest.param(
            {"action": "crawl", "url": URL, "limit": 1, "api_key": MARKER},
            "不支持的站点参数",
            id="crawl_unknown_field",
        ),
        pytest.param(
            {"action": "batch", "urls": [URL], "api_key": MARKER},
            "不支持的站点参数",
            id="batch_unknown_field",
        ),
        pytest.param(
            {"action": "crawl", "url": f"https://{UNICODE_DOMAIN}", "limit": 1},
            "blocked_domains",
            id="crawl_blocked_unicode_host",
        ),
        pytest.param(
            {"action": "batch", "urls": [f"https://{UNICODE_DOMAIN}"]},
            "blocked_domains",
            id="batch_blocked_unicode_url",
        ),
    ],
)
@pytest.mark.asyncio
async def test_map_rejects_before_scope_and_network(firecrawl_site_tool, kwargs, match):
    blocked = {
        **CONFIG,
        "site_admin_only": False,
        "blocked_domains": [UNICODE_DOMAIN],
    }
    runtime = FakeRuntime(config=blocked)
    out = await firecrawl_site_tool(plugin=runtime).call(None, **kwargs)
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["category"] == "usage"
    assert match in payload["error"]
    assert payload["example"] == EXAMPLE
    assert MARKER not in out
    assert runtime.scope_entered == 0
    assert runtime.executor.calls == 0


@pytest.mark.asyncio
async def test_crawl_batch_admin_gate_same_instance(firecrawl_site_tool):
    plugin = FakeRuntime(config=CONFIG)
    tool = firecrawl_site_tool(plugin=plugin)
    submits = 0
    for action, kwargs in (
        ("crawl", {"url": URL, "limit": 1}),
        ("batch", {"urls": [URL]}),
    ):
        scope_before = plugin.scope_entered
        submits_before = plugin.executor.submits
        member = await tool.call(StubContext(is_admin=False), action=action, **kwargs)
        payload = json.loads(member)
        assert payload["ok"] is False
        assert f"{action} 仅管理员可用" in payload["error"]
        assert plugin.scope_entered == scope_before
        assert plugin.executor.calls == 0
        assert plugin.executor.submits == submits_before

        admin = await tool.call(StubContext(is_admin=True), action=action, **kwargs)
        payload = json.loads(admin)
        submits += 1
        assert payload["ok"] is False
        assert payload["category"] == "server"
        assert payload["error"] == "插件内部错误。"
        assert "仅管理员" not in payload["error"]
        assert plugin.executor.submits == submits


@pytest.mark.asyncio
async def test_crawl_batch_rejects_missing_event_by_default(firecrawl_site_tool):
    plugin = FakeRuntime(config=CONFIG)
    tool = firecrawl_site_tool(plugin=plugin)
    crawl = await tool.call(None, action="crawl", url=URL, limit=1)
    batch = await tool.call(None, action="batch", urls=[URL])
    no_event = await tool.call(SimpleNamespace(), action="crawl", url=URL, limit=1)
    for out in (crawl, batch, no_event):
        payload = json.loads(out)
        assert payload["ok"] is False
        assert "仅管理员可用" in payload["error"]
        assert payload["example"] == EXAMPLE
    assert plugin.scope_entered == 0
    assert plugin.executor.calls == 0
    assert plugin.executor.submits == 0


@pytest.mark.asyncio
async def test_crawl_batch_allowed_when_admin_only_disabled(firecrawl_site_tool):
    plugin = FakeRuntime(config={**CONFIG, "site_admin_only": False})
    tool = firecrawl_site_tool(plugin=plugin)
    crawl = await tool.call(None, action="crawl", url=URL, limit=1)
    payload = json.loads(crawl)
    assert payload["ok"] is False
    assert payload["category"] == "server"
    assert plugin.scope_entered == 1
    assert plugin.executor.submits == 1

    batch = await tool.call(None, action="batch", urls=[URL])
    payload = json.loads(batch)
    assert payload["ok"] is False
    assert payload["category"] == "server"
    assert plugin.scope_entered == 2
    assert plugin.executor.submits == 2


@pytest.mark.asyncio
async def test_map_skips_admin_check(firecrawl_site_tool):
    plugin = FakeRuntime(config=CONFIG)
    tool = firecrawl_site_tool(plugin=plugin)
    out = await tool.call(None, action="map", url=URL)
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["category"] == "server"
    assert plugin.scope_entered == 1
    assert plugin.executor.calls == 1
    assert plugin.executor.submits == 0


@pytest.mark.asyncio
async def test_map_daily_budget_second_call_rejected(firecrawl_site_tool, tmp_path):
    async with open_plugin(
        tmp_path, [MAP_OK], policy=BudgetPolicy(daily_credit_limit=1)
    ) as runtime:
        tool = firecrawl_site_tool(plugin=runtime)
        first = await tool.call(None, action="map", url=URL)
        second = await tool.call(None, action="map", url=URL)
    assert json.loads(first)["ok"] is True
    payload = json.loads(second)
    assert payload["ok"] is False
    assert payload["category"] == "usage"
    assert "日预算不足" in payload["error"]
    assert payload["example"] == EXAMPLE
    assert len(runtime.seen) == 1
    await assert_usage(runtime, 1.0, 0.0)
    assert await runtime.store.list_pending() == []


@pytest.mark.asyncio
async def test_map_empty_pool_config_error_releases_reservation(
    firecrawl_site_tool, tmp_path
):
    async with open_plugin(tmp_path, [], keys=()) as runtime:
        tool = firecrawl_site_tool(plugin=runtime)
        out = await tool.call(None, action="map", url=URL)
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["category"] == "config"
    assert runtime.seen == []
    assert runtime.scope_entered == 1
    await assert_usage(runtime, 0.0, 0.0)
    assert await runtime.store.list_pending() == []


@pytest.mark.parametrize(
    ("response", "released"),
    [
        pytest.param(
            httpx.Response(402, json={"success": False, "error": "no credits"}),
            True,
            id="quota_402_released",
        ),
        pytest.param(
            httpx.Response(500, json={"success": False, "error": "boom"}),
            False,
            id="server_500_indeterminate",
        ),
        pytest.param(httpx.ReadTimeout("too slow"), False, id="read_timeout"),
    ],
)
@pytest.mark.asyncio
async def test_map_failure_shaping(firecrawl_site_tool, tmp_path, response, released):
    async with open_plugin(tmp_path, [response]) as runtime:
        tool = firecrawl_site_tool(plugin=runtime)
        out = await tool.call(None, action="map", url=URL)
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["retryable"] is False
    assert "example" not in payload
    assert len(runtime.seen) == 1
    if released:
        assert payload["category"] == "quota"
        assert "no credits" not in out
        await assert_usage(runtime, 0.0, 0.0)
        assert await runtime.store.list_pending() == []
    else:
        assert payload["category"] == "indeterminate"
        assert payload["pending"] is True
        record = await runtime.store.get(payload["reservation_id"])
        assert record.state is ReservationState.INDETERMINATE
        assert record.accounted_credits == 0.0
        await assert_usage(runtime, 0.0, 1.0)


@pytest.mark.asyncio
async def test_map_unknown_error_masked_and_cancelled_propagates(
    firecrawl_site_tool, caplog
):
    plugin = FakeRuntime(StubExecutor(RuntimeError(f"secret {MARKER}")))
    tool = firecrawl_site_tool(plugin=plugin)
    with caplog.at_level(logging.WARNING, logger="astrbot.stub"):
        out = await tool.call(None, action="map", url=URL)
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["category"] == "server"
    assert payload["error"] == "插件内部错误。"
    assert "example" not in payload
    assert MARKER not in out
    records = [r for r in caplog.records if r.name == "astrbot.stub"]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "firecrawl_site" in message
    assert "RuntimeError" in message
    assert MARKER not in message

    broken = FakeRuntime(StubExecutor(asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await firecrawl_site_tool(plugin=broken).call(None, action="map", url=URL)
