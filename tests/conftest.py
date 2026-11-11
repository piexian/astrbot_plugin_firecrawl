"""测试导入命名空间：把仓库根目录注册为插件包，供相对导入 persistence。"""

from __future__ import annotations

import importlib.util
import logging
import sys
import types
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_PACKAGE = "astrbot_plugin_firecrawl"


def pytest_configure(config):
    module = types.ModuleType(_PACKAGE)
    module.__path__ = [str(_ROOT)]
    sys.modules.setdefault(_PACKAGE, module)

    _install_sdk_stub()


@dataclass
class _StubFunctionTool:
    """与 AstrBot 4.29.3 FunctionTool 同形的最小替身，避免测试依赖真实 SDK。"""

    name: str
    description: str
    parameters: dict
    handler: Any = None
    active: bool = True

    def __repr__(self) -> str:
        return (
            f"FuncTool(name={self.name}, parameters={self.parameters}, "
            f"description={self.description})"
        )


def _install_sdk_stub() -> None:
    """安装 pytest 专用 AstrBot API stub，提供 FunctionTool、MessageChain 与 logger。"""
    api_module = types.ModuleType("astrbot.api")
    api_module.FunctionTool = _StubFunctionTool
    api_module.logger = logging.getLogger("astrbot.stub")
    event_module = types.ModuleType("astrbot.api.event")
    event_module.MessageChain = type(
        "MessageChain",
        (),
        {"message": lambda self, text: setattr(self, "text", text) or self},
    )
    root_module = types.ModuleType("astrbot")
    root_module.api = api_module
    root_module.event = event_module
    sys.modules["astrbot"] = root_module
    sys.modules["astrbot.api"] = api_module
    sys.modules["astrbot.api.event"] = event_module


_SDK_MODULE_NAMES = ("astrbot", "astrbot.api", "astrbot.api.event")
_MISSING = object()


@contextmanager
def _load_sdk_tool_class(module_name: str, class_name: str):
    """在独立 alias 中执行 tools/{module_name}.py，结束后精确还原被替换的 sys.modules 项。"""
    alias = f"{_PACKAGE}.tools._sdk_{module_name}"
    saved = {
        name: sys.modules.get(name, _MISSING) for name in (*_SDK_MODULE_NAMES, alias)
    }
    _install_sdk_stub()
    spec = importlib.util.spec_from_file_location(
        alias, _ROOT / "tools" / f"{module_name}.py"
    )
    tool_module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = tool_module
    try:
        spec.loader.exec_module(tool_module)
        yield getattr(tool_module, class_name)
    finally:
        for name, original in saved.items():
            if original is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


@pytest.fixture()
def sdk_tool_loader():
    """返回 contextmanager 工厂：with sdk_tool_loader("search", "FirecrawlSearchTool") as ToolClass。"""
    return _load_sdk_tool_class


@pytest.fixture()
def firecrawl_search_tool(sdk_tool_loader):
    """经 sdk_tool_loader 以独立 alias 加载 tools/search.py，作用域结束自动还原。"""
    with sdk_tool_loader("search", "FirecrawlSearchTool") as tool_class:
        yield tool_class


@pytest.fixture()
def firecrawl_scrape_tool(sdk_tool_loader):
    """经 sdk_tool_loader 以独立 alias 加载 tools/scrape.py，作用域结束自动还原。"""
    with sdk_tool_loader("scrape", "FirecrawlScrapeTool") as tool_class:
        yield tool_class


@pytest.fixture()
def firecrawl_site_tool(sdk_tool_loader):
    """经 sdk_tool_loader 以独立 alias 加载 tools/site.py，作用域结束自动还原。"""
    with sdk_tool_loader("site", "FirecrawlSiteTool") as tool_class:
        yield tool_class
