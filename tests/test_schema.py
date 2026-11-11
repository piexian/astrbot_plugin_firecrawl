"""tools.schema object_schema 测试：根字段装配、required 顺序与深复制隔离。"""

from __future__ import annotations

from astrbot_plugin_firecrawl.tools.schema import object_schema


def test_root_fields_and_values():
    schema = object_schema({"query": {"type": "string"}})
    assert set(schema) == {"type", "properties", "required"}
    assert schema["type"] == "object"
    assert schema["properties"] == {"query": {"type": "string"}}
    assert schema["required"] == []


def test_required_order_preserved_as_new_list():
    required = ["query", "limit"]
    schema = object_schema(
        {"query": {"type": "string"}, "limit": {"type": "number"}},
        required=required,
    )
    assert schema["required"] == ["query", "limit"]
    schema["required"].append("format")
    assert required == ["query", "limit"]


def test_nested_caller_change_not_visible_in_result():
    enum = ["auto", "markdown"]
    properties = {"formats": {"type": "array", "items": {"enum": enum}}}
    schema = object_schema(properties, required=("formats",))
    enum.append("html")
    properties["extra"] = {"type": "string"}
    assert schema["properties"]["formats"]["items"]["enum"] == ["auto", "markdown"]
    assert "extra" not in schema["properties"]


def test_nested_result_change_not_visible_in_caller():
    enum = ["auto", "markdown"]
    schema = object_schema({"formats": {"type": "array", "items": {"enum": enum}}})
    schema["properties"]["formats"]["items"]["enum"].append("html")
    assert enum == ["auto", "markdown"]
