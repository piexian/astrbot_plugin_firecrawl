"""工具参数 schema 装配：深复制调用方 properties 并组装对象 schema。"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from typing import Any


def object_schema(properties: dict[str, Any], *, required: Sequence[str] = ()) -> dict:
    """深复制 properties，装配 type/properties/required 三个根字段的参数 schema。"""
    return {
        "type": "object",
        "properties": copy.deepcopy(properties),
        "required": list(required),
    }
