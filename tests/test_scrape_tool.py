"""firecrawl_scrape 工具层测试：schema 契约、未注入护栏与参数拒绝，全程零 I/O。"""

from __future__ import annotations

import inspect
import json
from contextlib import asynccontextmanager
from typing import Any

import pytest
from astrbot_plugin_firecrawl.firecrawl_api.scrape import normalize_scrape_args

# 断言不回显用的假敏感标记。
_FAKE_KEY = "fc-fake123456"


class _FakeExecutor:
    """执行器替身：一旦被调用即计数并中断，用于证明零 I/O。"""

    def __init__(self) -> None:
        self.calls = 0

    async def execute_sync(self, *args: Any, **kwargs: Any) -> str:
        self.calls += 1
        raise AssertionError("executor 不应被调用")


class _FakePlugin:
    """最简插件替身：仅 config、request_scope 进入计数与 executor。"""

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}
        self.executor = _FakeExecutor()
        self.scope_entries = 0

    @asynccontextmanager
    async def request_scope(self):
        self.scope_entries += 1
        yield


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


def test_tool_metadata_matches_schema(firecrawl_scrape_tool):
    """name、required 与 10 个显式 keyword 参数须与 properties 一致。"""
    tool = firecrawl_scrape_tool()
    assert tool.name == "firecrawl_scrape"
    schema = tool.parameters
    assert schema["required"] == ["url"]
    props = schema["properties"]
    params = inspect.signature(firecrawl_scrape_tool.call).parameters
    named = [
        name
        for name, param in params.items()
        if param.kind is inspect.Parameter.KEYWORD_ONLY
    ]
    assert named == list(props)
    assert len(named) == 10
    var_keyword = [
        name
        for name, param in params.items()
        if param.kind is inspect.Parameter.VAR_KEYWORD
    ]
    assert var_keyword == ["unexpected"]
    formats = ["markdown", "summary", "links", "html", "question", "json"]
    assert props["format"]["enum"] == formats
    assert props["proxy"]["enum"] == ["auto", "basic", "enhanced"]
    injection_description = props["check_prompt_injection"]["description"]
    assert "检测通过时不返回独立结论" in injection_description
    assert "扫描不完整时返回 warning" in injection_description
    _walk_schema(schema)


def test_schema_instances_do_not_share_state(firecrawl_scrape_tool):
    """每次实例化得到全新 schema，嵌套结构互不共享。"""
    first = firecrawl_scrape_tool()
    second = firecrawl_scrape_tool()
    assert first.parameters is not second.parameters
    assert first.parameters["properties"] is not second.parameters["properties"]
    first.parameters["properties"]["format"]["enum"].append("hacked")
    assert "hacked" not in second.parameters["properties"]["format"]["enum"]


@pytest.mark.asyncio
async def test_call_without_plugin_returns_config(firecrawl_scrape_tool):
    """未注入 plugin 时零 I/O 返回 config 错误。"""
    tool = firecrawl_scrape_tool()
    result = json.loads(await tool.call(None, url="https://example.com"))
    assert result["ok"] is False
    assert result["category"] == "config"


def test_repr_hides_injected_plugin(firecrawl_scrape_tool):
    """repr 只含 name、parameters 与 description，不泄露注入的假敏感配置。"""
    plugin = {"config": {"api_key": _FAKE_KEY}}
    tool = firecrawl_scrape_tool(plugin=plugin)
    text = repr(tool)
    assert _FAKE_KEY not in text
    assert "api_key" not in text


_REJECTIONS = [
    ({"url": None}, {}),
    ({"url": "https://example.com", "format": "question"}, {}),
    ({"url": "https://example.com", "format": "json"}, {}),
    ({"url": "https://example.com", "question": "页面主题是什么"}, {}),
    ({"url": "https://example.com", "json_schema": {"type": "object"}}, {}),
    ({"url": "https://example.com", "check_prompt_injection": "yes"}, {}),
    ({"url": "https://example.com", "wait_for_ms": -1}, {}),
    ({"url": "https://sub.x.com/"}, {"blocked_domains": ["x.com"]}),
    ({"url": "https://x.com。/a"}, {"blocked_domains": ["x.com"]}),
]


@pytest.mark.parametrize(("kwargs", "config"), _REJECTIONS)
@pytest.mark.asyncio
async def test_rejects_invalid_args(firecrawl_scrape_tool, kwargs, config):
    """非法参数收敛为 usage，且不进入 request_scope、不触达 executor。"""
    plugin = _FakePlugin(config)
    tool = firecrawl_scrape_tool(plugin=plugin)
    result = json.loads(await tool.call(None, **kwargs))
    assert result["ok"] is False
    assert result["category"] == "usage"
    assert plugin.scope_entries == 0
    assert plugin.executor.calls == 0


@pytest.mark.asyncio
async def test_unknown_fields_hidden_and_example_valid(firecrawl_scrape_tool):
    """未知字段携带假标记时固定 usage，不回显名称/值，example 可独立通过规范化。"""
    plugin = _FakePlugin({"blocked_domains": ["x.com"]})
    tool = firecrawl_scrape_tool(plugin=plugin)
    result = json.loads(
        await tool.call(
            None,
            url="https://example.com",
            pdf_max_pages=5,
            blocked_domains=["evil.com"],
            api_key=_FAKE_KEY,
        )
    )
    assert result["ok"] is False
    assert result["category"] == "usage"
    text = json.dumps(result, ensure_ascii=False)
    assert "pdf_max_pages" not in text
    assert "blocked_domains" not in text
    assert "evil.com" not in text
    assert _FAKE_KEY not in text
    assert plugin.scope_entries == 0
    assert plugin.executor.calls == 0
    example = result["example"]
    assert set(example) == {"url"}
    normalize_scrape_args(**example)
