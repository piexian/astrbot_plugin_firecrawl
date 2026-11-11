"""firecrawl_search FunctionTool 测试：schema、参数护栏、预算执行链路与结果投影。"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from astrbot_plugin_firecrawl.firecrawl_api.budget import (
    BudgetPolicy,
    ReservationState,
)
from astrbot_plugin_firecrawl.firecrawl_api.client import FirecrawlClient
from astrbot_plugin_firecrawl.firecrawl_api.keys import KeyPool
from astrbot_plugin_firecrawl.firecrawl_api.search import normalize_search_args
from astrbot_plugin_firecrawl.persistence import SQLiteStore
from astrbot_plugin_firecrawl.tools.execution import BudgetedExecutor

K1 = "fc-search-tool-key-1"

EXAMPLE = {"query": "firecrawl api", "sources": ["web"]}


def web_page(index: int, **overrides):
    page = {
        "title": f"web-{index}",
        "url": f"https://example.com/{index}",
        "description": f"desc-{index}",
    }
    page.update(overrides)
    return page


def news_item(index: int, **overrides):
    item = {
        "title": f"news-{index}",
        "url": f"https://example.com/news/{index}",
        "snippet": f"snippet-{index}",
        "date": "2026-01-01",
        "imageUrl": f"https://cdn.example.com/news/{index}.jpg",
    }
    item.update(overrides)
    return item


def image_item(index: int, **overrides):
    item = {
        "title": f"img-{index}",
        "url": f"https://example.com/page/{index}",
        "imageUrl": f"https://cdn.example.com/img/{index}.jpg",
        "imageWidth": 640,
        "imageHeight": 480,
    }
    item.update(overrides)
    return item


def search_ok(**data_overrides):
    """带顶层 warning/creditsUsed 的标准成功响应。"""
    data = {"web": [web_page(1, markdown="m" * 20)]}
    data.update(data_overrides)
    return {
        "success": True,
        "warning": "partial results",
        "creditsUsed": 4,
        "data": data,
    }


def recording_handler(seen, responses):
    """按顺序回放响应或异常，并记录收到的请求。"""
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return handler


class FakeRequestScope:
    """记录进入次数的 request_scope 替身。"""

    def __init__(self) -> None:
        self.entered = 0

    async def __aenter__(self):
        self.entered += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FailingExecutor:
    """固定抛出预设异常的执行器替身，用于错误封装路径。"""

    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def execute_sync(self, kind, payload, *, estimate):
        raise self.error


class FakePlugin:
    """与 2C 运行接口一致的最小插件替身：config + executor + request_scope。"""

    def __init__(self, executor, config=None) -> None:
        self.config = config or {}
        self.executor = executor
        self.scope = FakeRequestScope()

    def request_scope(self):
        return self.scope


async def make_plugin(tmp_path, handler, config=None, *, keys=(K1,), policy=None):
    """用真实 SQLiteStore + KeyPool + FirecrawlClient + BudgetedExecutor 组装插件替身。"""
    store = SQLiteStore(tmp_path / "budget.db", policy=policy or BudgetPolicy())
    await store.initialize()
    client = FirecrawlClient(
        KeyPool(list(keys)), transport=httpx.MockTransport(handler)
    )
    plugin = FakePlugin(BudgetedExecutor(client, store), config)
    plugin.store = store
    plugin.client = client
    return plugin


async def assert_usage(plugin, used, active_reserved):
    snapshot = await plugin.store.usage()
    assert snapshot.used == used
    assert snapshot.active_reserved == active_reserved


def test_schema_structure_and_required(firecrawl_search_tool):
    tool = firecrawl_search_tool(plugin=None)
    schema = tool.parameters
    assert schema["type"] == "object"
    assert schema["required"] == ["query"]
    props = schema["properties"]
    assert set(props) == {
        "query",
        "limit",
        "sources",
        "categories",
        "tbs",
        "location",
        "country",
        "include_domains",
        "exclude_domains",
        "with_content",
    }
    assert props["query"]["type"] == "string"
    assert props["limit"]["type"] == "integer"
    assert props["sources"]["type"] == "array"
    assert props["sources"]["items"] == {
        "type": "string",
        "enum": ["web", "news", "images"],
    }
    assert props["categories"]["items"] == {
        "type": "string",
        "enum": ["developer", "gov", "research", "pdf"],
    }
    assert props["with_content"]["type"] == "boolean"
    assert props["include_domains"]["type"] == "array"
    assert props["exclude_domains"]["type"] == "array"
    for prop in props.values():
        assert "anyOf" not in prop
        assert "default" not in prop
    assert "Firecrawl" in tool.description
    assert "每个 source" in tool.description
    assert "总条数" in tool.description
    assert "逐条额外计费" in tool.description
    assert "每个 source" in props["limit"]["description"]
    assert "只接受数组" in props["sources"]["description"]
    assert "必须单独使用" in props["categories"]["description"]
    assert "互斥" in props["include_domains"]["description"]
    assert "互斥" in props["exclude_domains"]["description"]
    assert "额外计费" in props["with_content"]["description"]


def test_schema_instance_isolation(firecrawl_search_tool):
    first = firecrawl_search_tool()
    second = firecrawl_search_tool()
    assert first.parameters is not second.parameters
    first.parameters["properties"]["query"]["type"] = "mutated"
    assert second.parameters["properties"]["query"]["type"] == "string"
    sentinel = object()
    tool = firecrawl_search_tool(plugin=sentinel)
    assert tool.plugin is sentinel
    assert repr(sentinel) not in repr(tool)


@pytest.mark.asyncio
async def test_plugin_none_returns_config_fail(firecrawl_search_tool):
    tool = firecrawl_search_tool()
    out = await tool.call(None, query="q")
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["category"] == "config"
    assert payload["retryable"] is False
    assert payload["hint"]
    assert "example" not in payload


@pytest.mark.asyncio
async def test_default_search_round_trip(firecrawl_search_tool, tmp_path):
    seen = []
    plugin = await make_plugin(
        tmp_path, recording_handler(seen, [httpx.Response(200, json=search_ok())])
    )
    tool = firecrawl_search_tool(plugin=plugin)
    out = await tool.call(None, query="firecrawl api")
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is True
    assert len(seen) == 1
    assert seen[0].url.path == "/v2/search"
    assert seen[0].headers["authorization"] == f"Bearer {K1}"
    assert json.loads(seen[0].content) == {
        "query": "firecrawl api",
        "limit": 10,
        "sources": ["web"],
        "highlights": False,
    }
    assert plugin.scope.entered == 1
    await assert_usage(plugin, 4.0, 0.0)
    assert await plugin.store.list_pending() == []
    assert payload["data"]["success"] is True
    assert payload["data"]["warning"] == "partial results"
    assert payload["data"]["creditsUsed"] == 4
    assert payload["data"]["data"]["web"] == [
        {
            "title": "web-1",
            "url": "https://example.com/1",
            "description": "desc-1",
            "markdown": "m" * 20,
        }
    ]
    assert "resultsTruncated" not in payload["data"]


@pytest.mark.asyncio
async def test_custom_cap_limit15_trim_total20(firecrawl_search_tool, tmp_path):
    response = {
        "success": True,
        "creditsUsed": 12,
        "data": {
            "web": [web_page(i) for i in range(15)],
            "news": [news_item(i) for i in range(15)],
            "images": [image_item(i) for i in range(15)],
        },
    }
    seen = []
    plugin = await make_plugin(
        tmp_path,
        recording_handler(seen, [httpx.Response(200, json=response)]),
        config={"search_max_results": 20},
    )
    tool = firecrawl_search_tool(plugin=plugin)
    out = await tool.call(None, query="q", limit=15, sources=["web", "news", "images"])
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is True
    assert json.loads(seen[0].content) == {
        "query": "q",
        "limit": 15,
        "sources": ["web", "news", "images"],
        "highlights": False,
    }
    data = payload["data"]["data"]
    assert len(data["web"]) == 15
    assert len(data["news"]) == 5
    assert data["images"] == []
    assert payload["data"]["resultsTruncated"] is True
    await assert_usage(plugin, 12.0, 0.0)


@pytest.mark.asyncio
async def test_explicit_search_max_results_reaches_payload(
    firecrawl_search_tool, tmp_path
):
    response = {"success": True, "creditsUsed": 6, "data": {"web": []}}
    seen = []
    plugin = await make_plugin(
        tmp_path,
        recording_handler(seen, [httpx.Response(200, json=response)]),
        config={"search_max_results": 20},
    )
    tool = firecrawl_search_tool(plugin=plugin)
    out = await tool.call(None, query="q")
    await plugin.client.aclose()
    assert json.loads(seen[0].content)["limit"] == 20
    payload = json.loads(out)
    assert payload["ok"] is True
    await assert_usage(plugin, 6.0, 0.0)


@pytest.mark.asyncio
async def test_with_content_payload_config(firecrawl_search_tool, tmp_path):
    response = {
        "success": True,
        "creditsUsed": 98,
        "data": {
            "web": [web_page(1, markdown="x" * 600), web_page(2, markdown=None)],
            "news": [news_item(1, markdown="n" * 400), news_item(2)],
        },
    }
    seen = []
    plugin = await make_plugin(
        tmp_path,
        recording_handler(seen, [httpx.Response(200, json=response)]),
        config={
            "pdf_max_pages": 7,
            "check_prompt_injection": True,
            "max_content_chars": 500,
        },
    )
    tool = firecrawl_search_tool(plugin=plugin)
    out = await tool.call(
        None, query="q", limit=4, sources=["web", "news"], with_content=True
    )
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is True
    request = json.loads(seen[0].content)
    assert request["limit"] == 4
    assert request["scrapeOptions"] == {
        "formats": ["markdown"],
        "onlyMainContent": True,
        "parsers": [{"type": "pdf", "maxPages": 7}],
        "checkPromptInjection": True,
    }
    await assert_usage(plugin, 98.0, 0.0)


@pytest.mark.asyncio
async def test_with_content_estimate_uses_request_volume(
    firecrawl_search_tool, tmp_path
):
    # limit 4 × 2 sources = 8 页，PDF 上限 7 + 注入检测：上界 = 2 + 8 × (1+7+4) = 98。
    policy = BudgetPolicy(max_estimated_credits_per_call=97)
    seen = []
    plugin = await make_plugin(
        tmp_path,
        recording_handler(seen, []),
        config={"pdf_max_pages": 7, "check_prompt_injection": True},
        policy=policy,
    )
    tool = firecrawl_search_tool(plugin=plugin)
    out = await tool.call(
        None, query="q", limit=4, sources=["web", "news"], with_content=True
    )
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["category"] == "usage"
    assert "单次预估 98 credits 超过单次上限 97" in payload["error"]
    assert payload["example"] == EXAMPLE
    assert seen == []
    await assert_usage(plugin, 0.0, 0.0)


@pytest.mark.parametrize(
    ("kwargs", "fragment", "config"),
    [
        (
            {"query": "q", "include_domains": ["a.com"], "exclude_domains": ["b.com"]},
            "互斥",
            None,
        ),
        ({"query": "q", "sources": "web"}, "sources 必须是字符串列表", None),
        ({"query": "q", "sources": ["alexandria"]}, "sources 只允许", None),
        ({"query": "q", "categories": ["nope"]}, "categories 只允许", None),
        ({"query": "q", "categories": ["gov", "research"]}, "必须单独使用", None),
        ({}, "query 必须是字符串", None),
        ({"query": "   "}, "不能为空", None),
        ({"query": "q" * 501}, "query 长度不能超过 500", None),
        ({"query": "q", "limit": 0}, "limit 必须在 1-10 之间", None),
        ({"query": "q", "limit": 11}, "limit 必须在 1-10 之间", None),
        ({"query": "q", "limit": "5"}, "limit 必须是整数", None),
        (
            {"query": "q", "with_content": True, "include_domains": ["x.com"]},
            "blocked_domains",
            {"blocked_domains": ["x.com"]},
        ),
    ],
)
@pytest.mark.asyncio
async def test_invalid_arguments_reject_before_request(
    firecrawl_search_tool, tmp_path, kwargs, fragment, config
):
    seen = []
    plugin = await make_plugin(tmp_path, recording_handler(seen, []), config)
    tool = firecrawl_search_tool(plugin=plugin)
    out = await tool.call(None, **kwargs)
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["category"] == "usage"
    assert fragment in payload["error"]
    assert payload["retryable"] is False
    assert payload["example"] == EXAMPLE
    assert seen == []
    await assert_usage(plugin, 0.0, 0.0)


@pytest.mark.asyncio
async def test_without_content_missing_markdown_not_blocked(
    firecrawl_search_tool, tmp_path
):
    response = {"success": True, "data": {"web": [web_page(1)]}}
    seen = []
    plugin = await make_plugin(
        tmp_path, recording_handler(seen, [httpx.Response(200, json=response)])
    )
    tool = firecrawl_search_tool(plugin=plugin)
    out = await tool.call(None, query="q")
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["data"]["data"]["web"] == [
        {
            "title": "web-1",
            "url": "https://example.com/1",
            "description": "desc-1",
        }
    ]


@pytest.mark.asyncio
async def test_timeout_returns_pending_and_one_reservation(
    firecrawl_search_tool, tmp_path
):
    seen = []
    plugin = await make_plugin(
        tmp_path, recording_handler(seen, [httpx.ReadTimeout("too slow")])
    )
    tool = firecrawl_search_tool(plugin=plugin)
    out = await tool.call(None, query="q")
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["category"] == "indeterminate"
    assert payload["pending"] is True
    assert payload["retryable"] is False
    assert payload["reservation_id"]
    assert "task_handle" not in payload
    assert "example" not in payload
    assert len(seen) == 1
    record = await plugin.store.get(payload["reservation_id"])
    assert record.state is ReservationState.INDETERMINATE
    pending = await plugin.store.list_pending()
    assert len(pending) == 1
    await assert_usage(plugin, 0.0, 2.0)


@pytest.mark.asyncio
async def test_missing_credits_settles_with_estimate(firecrawl_search_tool, tmp_path):
    response = {"success": True, "data": {"web": [web_page(1)]}}
    seen = []
    plugin = await make_plugin(
        tmp_path, recording_handler(seen, [httpx.Response(200, json=response)])
    )
    tool = firecrawl_search_tool(plugin=plugin)
    out = await tool.call(None, query="q")
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is True
    assert "creditsUsed" not in payload["data"]
    await assert_usage(plugin, 2.0, 0.0)


@pytest.mark.asyncio
async def test_bad_credits_returns_pending_no_resend(firecrawl_search_tool, tmp_path):
    response = {"success": True, "creditsUsed": "many", "data": {"web": []}}
    seen = []
    plugin = await make_plugin(
        tmp_path, recording_handler(seen, [httpx.Response(200, json=response)])
    )
    tool = firecrawl_search_tool(plugin=plugin)
    out = await tool.call(None, query="q")
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["category"] == "indeterminate"
    assert payload["pending"] is True
    assert payload["reservation_id"]
    assert len(seen) == 1
    record = await plugin.store.get(payload["reservation_id"])
    assert record.state is ReservationState.INDETERMINATE


@pytest.mark.asyncio
async def test_anonymous_search_without_authorization(firecrawl_search_tool, tmp_path):
    response = {"success": True, "data": {"web": [web_page(1)]}}
    seen = []
    plugin = await make_plugin(
        tmp_path,
        recording_handler(seen, [httpx.Response(200, json=response)]),
        keys=(),
    )
    tool = firecrawl_search_tool(plugin=plugin)
    out = await tool.call(None, query="q")
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is True
    assert "authorization" not in seen[0].headers
    await assert_usage(plugin, 2.0, 0.0)


@pytest.mark.asyncio
async def test_budget_exceeded_before_reserve(firecrawl_search_tool, tmp_path):
    policy = BudgetPolicy(max_estimated_credits_per_call=9)
    seen = []
    plugin = await make_plugin(
        tmp_path,
        recording_handler(seen, []),
        config={"search_max_results": 20},
        policy=policy,
    )
    tool = firecrawl_search_tool(plugin=plugin)
    out = await tool.call(None, query="q", limit=15, sources=["web", "news", "images"])
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["category"] == "usage"
    assert "单次预估 10 credits 超过单次上限 9" in payload["error"]
    assert payload["example"] == EXAMPLE
    assert seen == []
    await assert_usage(plugin, 0.0, 0.0)


@pytest.mark.parametrize(
    ("status", "body", "category"),
    [
        (401, {"success": False, "error": "bad key"}, "auth"),
        (402, {"success": False, "error": "no credits"}, "quota"),
    ],
)
@pytest.mark.asyncio
async def test_final_rejection_releases_reservation(
    firecrawl_search_tool, tmp_path, status, body, category
):
    seen = []
    plugin = await make_plugin(
        tmp_path, recording_handler(seen, [httpx.Response(status, json=body)])
    )
    tool = firecrawl_search_tool(plugin=plugin)
    out = await tool.call(None, query="q")
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["category"] == category
    assert payload["retryable"] is False
    assert body["error"] not in payload["error"]
    assert "example" not in payload
    assert len(seen) == 1
    await assert_usage(plugin, 0.0, 0.0)
    assert await plugin.store.list_pending() == []


@pytest.mark.asyncio
async def test_round_trip_projection_and_truncation(firecrawl_search_tool, tmp_path):
    response = {
        "success": True,
        "warning": "partial",
        "warnings": ["w-1", "w-2"],
        "creditsUsed": 3,
        "data": {
            "web": [
                web_page(1, markdown=None),
                web_page(2, markdown="x" * 30),
            ],
            "news": [news_item(1, markdown=None)],
        },
    }
    seen = []
    plugin = await make_plugin(
        tmp_path,
        recording_handler(seen, [httpx.Response(200, json=response)]),
        config={"max_content_chars": 10},
    )
    tool = firecrawl_search_tool(plugin=plugin)
    out = await tool.call(None, query="q")
    await plugin.client.aclose()
    assert json.loads(out) == {
        "ok": True,
        "data": {
            "success": True,
            "warning": "partial",
            "warnings": ["w-1", "w-2"],
            "creditsUsed": 3,
            "data": {
                "web": [
                    {
                        "title": "web-1",
                        "url": "https://example.com/1",
                        "description": "desc-1",
                        "markdown": None,
                    },
                    {
                        "title": "web-2",
                        "url": "https://example.com/2",
                        "description": "desc-2",
                        "markdown": "x" * 10,
                        "truncated": True,
                    },
                ],
                "news": [
                    {
                        "title": "news-1",
                        "url": "https://example.com/news/1",
                        "snippet": "snippet-1",
                        "date": "2026-01-01",
                        "imageUrl": "https://cdn.example.com/news/1.jpg",
                        "markdown": None,
                    }
                ],
            },
        },
    }


@pytest.mark.asyncio
async def test_unknown_exception_server_fail_and_class_only_log(
    firecrawl_search_tool, caplog
):
    plugin = FakePlugin(FailingExecutor(RuntimeError("secret-detail")))
    tool = firecrawl_search_tool(plugin=plugin)
    with caplog.at_level(logging.WARNING, logger="astrbot.stub"):
        out = await tool.call(None, query="q")
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["category"] == "server"
    assert payload["error"] == "插件内部错误。"
    assert payload["retryable"] is False
    assert "example" not in payload
    records = [r for r in caplog.records if r.name == "astrbot.stub"]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "firecrawl_search" in message
    assert "RuntimeError" in message
    assert "secret-detail" not in message


@pytest.mark.asyncio
async def test_cancelled_error_propagates(firecrawl_search_tool):
    plugin = FakePlugin(FailingExecutor(asyncio.CancelledError()))
    tool = firecrawl_search_tool(plugin=plugin)
    with pytest.raises(asyncio.CancelledError):
        await tool.call(None, query="q")


@pytest.mark.asyncio
async def test_unexpected_argument_rejected_without_request(
    firecrawl_search_tool, tmp_path
):
    def forbidden_handler(request):
        raise AssertionError("未知参数不应发起网络请求")

    plugin = await make_plugin(tmp_path, forbidden_handler)
    tool = firecrawl_search_tool(plugin=plugin)
    out = await tool.call(None, query="firecrawl api", bogus=1)
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["category"] == "usage"
    assert payload["error"] == "不支持的搜索参数。"
    assert payload["retryable"] is False
    assert payload["example"] == EXAMPLE
    assert plugin.scope.entered == 0
    await assert_usage(plugin, 0.0, 0.0)


@pytest.mark.asyncio
async def test_empty_query_example_passes_normalize_with_cap_1(
    firecrawl_search_tool, tmp_path
):
    def forbidden_handler(request):
        raise AssertionError("空 query 不应发起网络请求")

    plugin = await make_plugin(
        tmp_path, forbidden_handler, config={"search_max_results": 1}
    )
    tool = firecrawl_search_tool(plugin=plugin)
    out = await tool.call(None, query="")
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["category"] == "usage"
    assert payload["example"] == EXAMPLE
    assert plugin.scope.entered == 0
    await assert_usage(plugin, 0.0, 0.0)
    args = normalize_search_args(**payload["example"], search_max_results=1)
    assert args is not None
