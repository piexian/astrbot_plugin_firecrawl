"""生产代码日志导入白名单：logger 只能来自 astrbot.api。"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_BANNED_LOGGING_MODULES = {"logging", "loguru"}
_ALLOWED_LOGGER_MODULE = "astrbot.api"


def test_production_logging_imports_are_astrbot_only():
    """生产模块不得绕过 AstrBot logger，使用 logger 时必须显式从 astrbot.api 导入。"""
    paths = [*_ROOT.glob("firecrawl_api/*.py"), *_ROOT.glob("tools/*.py")]
    paths.extend([_ROOT / "main.py", _ROOT / "persistence.py"])
    assert paths

    violations = []
    for path in paths:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        has_astrbot_logger = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in _BANNED_LOGGING_MODULES:
                        violations.append(
                            f"{path.name}:{node.lineno} 导入 {alias.name}"
                        )
            elif isinstance(node, ast.ImportFrom):
                module_root = (node.module or "").split(".")[0]
                if module_root in _BANNED_LOGGING_MODULES:
                    violations.append(f"{path.name}:{node.lineno} 导入 {node.module}")
                for alias in node.names:
                    if alias.name != "logger":
                        continue
                    if node.module == _ALLOWED_LOGGER_MODULE:
                        has_astrbot_logger = True
                    else:
                        violations.append(
                            f"{path.name}:{node.lineno} 从 {node.module} 导入 logger"
                        )
        if "logger." in source and not has_astrbot_logger:
            violations.append(f"{path.name} 使用 logger 但未从 astrbot.api 导入")

    assert not violations, "\n".join(violations)
