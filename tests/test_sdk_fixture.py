"""sdk_tool_loader fixture 的回归测试：sys.modules 隔离与精确还原。"""

from __future__ import annotations

import sys
import types

import pytest

_ALIAS = "astrbot_plugin_firecrawl.tools._sdk_search"


def _fake_module(name: str) -> types.ModuleType:
    return types.ModuleType(name)


def _plant_fake_tools_tree(monkeypatch):
    """种入假 tools 包与假 execution/result/schema/canonical search 模块，返回假对象字典。"""
    canonical_search = _fake_module("astrbot_plugin_firecrawl.tools.search")
    execution = _fake_module("astrbot_plugin_firecrawl.tools.execution")
    execution.PendingRequestError = type("PendingRequestError", (Exception,), {})
    result = _fake_module("astrbot_plugin_firecrawl.tools.result")
    result.fail = object()
    result.ok = object()
    schema = _fake_module("astrbot_plugin_firecrawl.tools.schema")
    schema.object_schema = lambda *args, **kwargs: {}
    tools_pkg = _fake_module("astrbot_plugin_firecrawl.tools")
    tools_pkg.__path__ = []
    tools_pkg.search = canonical_search
    monkeypatch.setitem(sys.modules, "astrbot_plugin_firecrawl.tools", tools_pkg)
    monkeypatch.setitem(
        sys.modules, "astrbot_plugin_firecrawl.tools.search", canonical_search
    )
    monkeypatch.setitem(
        sys.modules, "astrbot_plugin_firecrawl.tools.execution", execution
    )
    monkeypatch.setitem(sys.modules, "astrbot_plugin_firecrawl.tools.result", result)
    monkeypatch.setitem(sys.modules, "astrbot_plugin_firecrawl.tools.schema", schema)
    return {
        "tools": tools_pkg,
        "search": canonical_search,
        "execution": execution,
    }


def test_normal_exit_preserves_preexisting_modules(sdk_tool_loader, monkeypatch):
    fake_root = _fake_module("astrbot")
    fake_api = _fake_module("astrbot.api")
    monkeypatch.setitem(sys.modules, "astrbot", fake_root)
    monkeypatch.setitem(sys.modules, "astrbot.api", fake_api)
    fakes = _plant_fake_tools_tree(monkeypatch)

    with sdk_tool_loader("search", "FirecrawlSearchTool") as tool_class:
        assert tool_class.__name__ == "FirecrawlSearchTool"
        assert tool_class.__module__ == _ALIAS

    assert sys.modules["astrbot"] is fake_root
    assert sys.modules["astrbot.api"] is fake_api
    assert sys.modules["astrbot_plugin_firecrawl.tools"] is fakes["tools"]
    assert sys.modules["astrbot_plugin_firecrawl.tools.search"] is fakes["search"]
    assert sys.modules["astrbot_plugin_firecrawl.tools.execution"] is fakes["execution"]
    assert getattr(fakes["tools"], "search") is fakes["search"]
    assert _ALIAS not in sys.modules


def test_fake_canonical_cache_still_loads_real_source(sdk_tool_loader, monkeypatch):
    canonical_search = _fake_module("astrbot_plugin_firecrawl.tools.search")
    sentinel_class = object()
    canonical_search.FirecrawlSearchTool = sentinel_class
    monkeypatch.setitem(
        sys.modules, "astrbot_plugin_firecrawl.tools.search", canonical_search
    )

    with sdk_tool_loader("search", "FirecrawlSearchTool") as tool_class:
        assert tool_class is not sentinel_class
        assert tool_class.__module__ == _ALIAS

    assert sys.modules["astrbot_plugin_firecrawl.tools.search"] is canonical_search


def test_missing_sdk_and_alias_removed_after_exit(sdk_tool_loader, monkeypatch):
    monkeypatch.delitem(sys.modules, "astrbot", raising=False)
    monkeypatch.delitem(sys.modules, "astrbot.api", raising=False)
    monkeypatch.delitem(sys.modules, _ALIAS, raising=False)

    with sdk_tool_loader("search", "FirecrawlSearchTool") as tool_class:
        assert tool_class.__name__ == "FirecrawlSearchTool"

    assert "astrbot" not in sys.modules
    assert "astrbot.api" not in sys.modules
    assert _ALIAS not in sys.modules


def test_none_sdk_entries_restored_to_none(sdk_tool_loader, monkeypatch):
    monkeypatch.setitem(sys.modules, "astrbot", None)
    monkeypatch.setitem(sys.modules, "astrbot.api", None)

    with sdk_tool_loader("search", "FirecrawlSearchTool") as tool_class:
        assert tool_class.__name__ == "FirecrawlSearchTool"

    assert sys.modules["astrbot"] is None
    assert sys.modules["astrbot.api"] is None


def test_exec_error_restores_modules(sdk_tool_loader, monkeypatch):
    fake_root = _fake_module("astrbot")
    fake_api = _fake_module("astrbot.api")
    monkeypatch.setitem(sys.modules, "astrbot", fake_root)
    monkeypatch.setitem(sys.modules, "astrbot.api", fake_api)

    with pytest.raises(OSError):
        with sdk_tool_loader("does_not_exist", "FirecrawlSearchTool"):
            pass

    assert sys.modules["astrbot"] is fake_root
    assert sys.modules["astrbot.api"] is fake_api
    assert "astrbot_plugin_firecrawl.tools._sdk_does_not_exist" not in sys.modules


def test_getattr_error_restores_modules(sdk_tool_loader, monkeypatch):
    fake_root = _fake_module("astrbot")
    fake_api = _fake_module("astrbot.api")
    monkeypatch.setitem(sys.modules, "astrbot", fake_root)
    monkeypatch.setitem(sys.modules, "astrbot.api", fake_api)
    _plant_fake_tools_tree(monkeypatch)

    with pytest.raises(AttributeError):
        with sdk_tool_loader("search", "NoSuchToolClass"):
            pass

    assert sys.modules["astrbot"] is fake_root
    assert sys.modules["astrbot.api"] is fake_api
    assert _ALIAS not in sys.modules
