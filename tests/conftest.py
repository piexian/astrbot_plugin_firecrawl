"""测试导入命名空间：把仓库根目录注册为插件包，供相对导入 persistence。"""

from __future__ import annotations

import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PACKAGE = "astrbot_plugin_firecrawl"


def pytest_configure(config):
    module = types.ModuleType(_PACKAGE)
    module.__path__ = [str(_ROOT)]
    sys.modules.setdefault(_PACKAGE, module)
