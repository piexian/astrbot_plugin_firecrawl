"""firecrawl_parse 工具层测试：schema 契约、零 I/O 护栏与本地路由。"""

from __future__ import annotations

import inspect
import json
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from astrbot.api.message_components import File

# sdk_tool_loader 为 tools/parse.py 使用的独立别名模块名。
_PARSE_ALIAS = "astrbot_plugin_firecrawl.tools._sdk_parse"
_FAKE_KEY = "fc-fake123456"


class _FakeExecutor:
    """执行器替身：一旦被调用即计数并中断，用于证明零云端。"""

    def __init__(self) -> None:
        self.calls = 0

    async def execute_parse(self, **kwargs: Any) -> str:
        self.calls += 1
        raise AssertionError("executor 不应被调用")


_AUTO = object()


class _FakePlugin:
    """最简插件替身：config、request_scope 计数、executor 与 Key 池计数。"""

    def __init__(
        self,
        config: dict | None = None,
        *,
        executor: Any = _AUTO,
        key_count: int = 1,
    ) -> None:
        self.config = config or {}
        self.executor = _FakeExecutor() if executor is _AUTO else executor
        self.key_pool = SimpleNamespace(key_count=key_count)
        self.scope_entries = 0

    @asynccontextmanager
    async def request_scope(self):
        self.scope_entries += 1
        yield


@pytest.fixture()
def parse_tool(sdk_tool_loader):
    """经 sdk_tool_loader 以独立 alias 加载 tools/parse.py。"""
    with sdk_tool_loader("parse", "FirecrawlParseTool") as tool_class:
        yield tool_class


def _event(*components: Any) -> SimpleNamespace:
    """构造带当前消息列表的最小事件替身。"""
    return SimpleNamespace(message_obj=SimpleNamespace(message=list(components)))


def _context(event: Any) -> SimpleNamespace:
    return SimpleNamespace(context=SimpleNamespace(event=event))


def _patch_convert(monkeypatch, fake):
    monkeypatch.setattr(sys.modules[_PARSE_ALIAS], "convert_local_document", fake)


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


def test_tool_metadata_matches_schema(parse_tool):
    """name、描述与唯一可选 file_name 参数须与 schema 一致。"""
    tool = parse_tool()
    assert tool.name == "firecrawl_parse"
    assert tool.description == "将当前或引用消息中的文档转为 Markdown 供当前 LLM 回答。"
    schema = tool.parameters
    assert schema["required"] == []
    props = schema["properties"]
    assert list(props) == ["file_name"]
    assert props["file_name"]["type"] == "string"
    params = inspect.signature(parse_tool.call).parameters
    named = [
        name
        for name, param in params.items()
        if name not in ("self", "context")
        and param.kind is not inspect.Parameter.VAR_KEYWORD
    ]
    assert named == ["file_name"]
    var_keyword = [
        name
        for name, param in params.items()
        if param.kind is inspect.Parameter.VAR_KEYWORD
    ]
    assert var_keyword == ["unexpected"]
    _walk_schema(schema)


def test_schema_instances_do_not_share_state(parse_tool):
    """每次实例化得到全新 schema，嵌套结构互不共享。"""
    first = parse_tool()
    second = parse_tool()
    assert first.parameters is not second.parameters
    assert first.parameters["properties"] is not second.parameters["properties"]
    first.parameters["properties"]["file_name"]["type"] = "hacked"
    assert second.parameters["properties"]["file_name"]["type"] == "string"


def test_repr_hides_injected_plugin(parse_tool):
    """repr 只含 name、parameters 与 description，不泄露注入的假敏感配置。"""
    plugin = {"config": {"api_keys": [_FAKE_KEY]}}
    tool = parse_tool(plugin=plugin)
    text = repr(tool)
    assert _FAKE_KEY not in text
    assert "api_keys" not in text


@pytest.mark.asyncio
async def test_call_without_plugin_returns_config(parse_tool):
    """未注入 plugin 时零 I/O 返回 config 错误。"""
    tool = parse_tool()
    result = json.loads(await tool.call(None))
    assert result["ok"] is False
    assert result["category"] == "config"


@pytest.mark.asyncio
async def test_missing_event_rejects_without_io(parse_tool):
    """缺少当前事件时零 scope 进入、零附件下载、零云端、零预留地拒绝。"""
    plugin = _FakePlugin({})
    tool = parse_tool(plugin=plugin)
    contexts = [
        None,
        SimpleNamespace(context=None),
        SimpleNamespace(context=SimpleNamespace(event=None)),
    ]
    for context in contexts:
        result = json.loads(await tool.call(context))
        assert result["ok"] is False
        assert result["category"] == "usage"
        assert "会话事件" in result["error"]
    assert plugin.scope_entries == 0
    assert plugin.executor.calls == 0


@pytest.mark.asyncio
async def test_unknown_fields_hidden_and_zero_io(parse_tool):
    """未知字段携带假标记时固定 usage，不回显名称/值，零 scope 进入。"""
    plugin = _FakePlugin({})
    tool = parse_tool(plugin=plugin)
    result = json.loads(
        await tool.call(
            None,
            file_name="a.pdf",
            url="https://x.com/a",
            api_key=_FAKE_KEY,
        )
    )
    assert result["ok"] is False
    assert result["category"] == "usage"
    text = json.dumps(result, ensure_ascii=False)
    assert "https://x.com" not in text
    assert _FAKE_KEY not in text
    assert plugin.scope_entries == 0
    assert plugin.executor.calls == 0


@pytest.mark.asyncio
async def test_no_attachment_error(parse_tool):
    """事件中没有文件附件时按 usage 报错，不触达执行器。"""
    plugin = _FakePlugin({})
    tool = parse_tool(plugin=plugin)
    result = json.loads(await tool.call(_context(_event(SimpleNamespace()))))
    assert result["ok"] is False
    assert result["category"] == "usage"
    assert result["error"] == "当前消息和引用消息中都没有文件附件。"
    assert plugin.executor.calls == 0
    assert plugin.scope_entries == 1


@pytest.mark.asyncio
async def test_multiple_attachments_require_file_name(parse_tool, tmp_path):
    """多个附件且未指定文件名时按 usage 报错，不下载任何附件。"""
    plugin = _FakePlugin({})
    tool = parse_tool(plugin=plugin)
    event = _event(
        File(name="a.csv", path=str(tmp_path / "a.csv")),
        File(name="b.csv", path=str(tmp_path / "b.csv")),
    )
    result = json.loads(await tool.call(_context(event)))
    assert result["ok"] is False
    assert result["category"] == "usage"
    assert result["error"] == "消息中有多个文件附件，请指定要使用的文件名。"
    assert plugin.executor.calls == 0


@pytest.mark.asyncio
async def test_small_unsupported_extension_rejects(parse_tool, tmp_path):
    """小文件必须是云端支持类型，否则在读取与云端前按 usage 拒绝。"""
    plugin = _FakePlugin({})
    tool = parse_tool(plugin=plugin)
    notes = tmp_path / "notes.txt"
    notes.write_text("hello", encoding="utf-8")
    result = json.loads(
        await tool.call(_context(_event(File(name="notes.txt", path=str(notes)))))
    )
    assert result["ok"] is False
    assert result["category"] == "usage"
    assert "不支持的文件扩展名" in result["error"]
    assert plugin.executor.calls == 0


@pytest.mark.asyncio
async def test_oversize_local_routes_without_cloud(parse_tool, tmp_path, monkeypatch):
    """超过上传上限且本地支持时直接本地转换，零云端、零预留。"""
    plugin = _FakePlugin({"max_content_chars": 100})
    tool = parse_tool(plugin=plugin)
    big = tmp_path / "big.csv"
    with open(big, "wb") as handle:
        handle.truncate(50_000_001)
    calls = {}

    async def fake_convert(path, filename, max_content_chars):
        calls["args"] = (path, filename, max_content_chars)
        return {
            "parser": "markitdown",
            "sourceFile": filename,
            "content": "x" * 100,
            "truncated": True,
        }

    _patch_convert(monkeypatch, fake_convert)
    result = json.loads(
        await tool.call(_context(_event(File(name="big.csv", path=str(big)))))
    )
    assert result["ok"] is True
    assert result["data"] == {
        "parser": "markitdown",
        "sourceFile": "big.csv",
        "content": "x" * 100,
        "truncated": True,
    }
    assert calls["args"] == (big, "big.csv", 100)
    assert plugin.executor.calls == 0
    assert plugin.scope_entries == 1


@pytest.mark.asyncio
async def test_oversize_unsupported_rejects(parse_tool, tmp_path, monkeypatch):
    """超过上传上限且本地不支持时明确拒绝，不本地转换也不上云。"""
    plugin = _FakePlugin({})
    tool = parse_tool(plugin=plugin)
    big = tmp_path / "big.rtf"
    with open(big, "wb") as handle:
        handle.truncate(50_000_001)

    async def fail_convert(*args: Any, **kwargs: Any) -> dict:
        raise AssertionError("不应本地转换")

    _patch_convert(monkeypatch, fail_convert)
    result = json.loads(
        await tool.call(_context(_event(File(name="big.rtf", path=str(big)))))
    )
    assert result["ok"] is False
    assert result["category"] == "usage"
    assert "50000000" in result["error"]
    assert "不支持本地解析" in result["error"]
    assert plugin.executor.calls == 0


@pytest.mark.asyncio
async def test_invalid_config_rejects_before_cloud(parse_tool, tmp_path):
    """非法配置在云端执行前按 usage 拒绝，零云端、零预留。"""
    plugin = _FakePlugin({"pdf_max_pages": "x"}, key_count=1)
    tool = parse_tool(plugin=plugin)
    csv_path = tmp_path / "d.csv"
    csv_path.write_text("a,b\n", encoding="utf-8")
    result = json.loads(
        await tool.call(_context(_event(File(name="d.csv", path=str(csv_path)))))
    )
    assert result["ok"] is False
    assert result["category"] == "usage"
    assert "pdf_max_pages" in result["error"]
    assert plugin.executor.calls == 0


@pytest.mark.asyncio
async def test_read_failure_fixed_error_no_cloud(parse_tool, tmp_path, monkeypatch):
    """附件字节读取失败收敛为固定文案，不回显异常细节与路径，零云端。"""
    plugin = _FakePlugin({})
    tool = parse_tool(plugin=plugin)
    csv_path = tmp_path / "d.csv"
    csv_path.write_text("a,b\n", encoding="utf-8")

    def broken_read(self):
        raise OSError("boom-read SECRET")

    monkeypatch.setattr(Path, "read_bytes", broken_read)
    result = json.loads(
        await tool.call(_context(_event(File(name="d.csv", path=str(csv_path)))))
    )
    assert result["ok"] is False
    assert result["category"] == "usage"
    assert result["error"] == "读取附件文件失败。"
    text = json.dumps(result, ensure_ascii=False)
    assert "boom-read" not in text
    assert str(csv_path) not in text
    assert plugin.executor.calls == 0


@pytest.mark.asyncio
async def test_no_key_local_supported_uses_local(parse_tool, tmp_path):
    """未配置 API Key 且本地支持时直接本地转换并附简洁 warning。"""
    plugin = _FakePlugin({"max_content_chars": 1000}, key_count=0)
    tool = parse_tool(plugin=plugin)
    csv_path = tmp_path / "数据.csv"
    csv_path.write_text("name,age\nalice,30\n", encoding="utf-8")
    result = json.loads(
        await tool.call(_context(_event(File(name="数据.csv", path=str(csv_path)))))
    )
    assert result["ok"] is True
    data = result["data"]
    assert data["parser"] == "markitdown"
    assert data["sourceFile"] == "数据.csv"
    assert "alice" in data["content"]
    assert data["warning"].endswith("已本地解析。")
    assert plugin.executor.calls == 0
    assert plugin.scope_entries == 1


@pytest.mark.asyncio
async def test_no_executor_local_supported_uses_local(parse_tool, tmp_path):
    """未注入 executor 且本地支持时同样走本地转换。"""
    plugin = _FakePlugin({"max_content_chars": 1000}, executor=None)
    tool = parse_tool(plugin=plugin)
    csv_path = tmp_path / "d.csv"
    csv_path.write_text("name,age\nbob,42\n", encoding="utf-8")
    result = json.loads(
        await tool.call(_context(_event(File(name="d.csv", path=str(csv_path)))))
    )
    assert result["ok"] is True
    assert result["data"]["parser"] == "markitdown"
    assert "bob" in result["data"]["content"]
    assert result["data"]["warning"].endswith("已本地解析。")


@pytest.mark.asyncio
async def test_no_cloud_and_local_unsupported_rejects(parse_tool, tmp_path):
    """云端不可用且本地不支持时明确拒绝，零云端。"""
    plugin = _FakePlugin({}, executor=None, key_count=0)
    tool = parse_tool(plugin=plugin)
    odt_path = tmp_path / "d.odt"
    odt_path.write_bytes(b"ODT")
    result = json.loads(
        await tool.call(_context(_event(File(name="d.odt", path=str(odt_path)))))
    )
    assert result["ok"] is False
    assert result["category"] == "usage"
    assert "不支持本地解析" in result["error"]
    assert plugin.scope_entries == 1


@pytest.mark.parametrize("value", [0, -1, True, "5", None])
@pytest.mark.asyncio
async def test_invalid_max_content_chars_rejects_before_io(
    parse_tool, tmp_path, monkeypatch, value
):
    """非法 max_content_chars 在 scope 进入、云端与本地转换前固定拒绝。"""
    plugin = _FakePlugin({"max_content_chars": value}, key_count=1)
    tool = parse_tool(plugin=plugin)
    csv_path = tmp_path / "d.csv"
    csv_path.write_text("a,b\n", encoding="utf-8")

    async def fail_convert(*args: Any, **kwargs: Any) -> dict:
        raise AssertionError("不应本地转换")

    _patch_convert(monkeypatch, fail_convert)
    result = json.loads(
        await tool.call(_context(_event(File(name="d.csv", path=str(csv_path)))))
    )
    assert result["ok"] is False
    assert result["category"] == "usage"
    assert result["error"] == "max_content_chars 必须是 >= 1 的整数。"
    assert plugin.executor.calls == 0
    assert plugin.scope_entries == 0


@pytest.mark.asyncio
async def test_unknown_exception_returns_fixed_usage_without_echo(
    parse_tool, tmp_path, monkeypatch, caplog
):
    """未知异常只按类型记日志，固定 usage 文案，不回显底层细节。"""
    plugin = _FakePlugin({}, key_count=0)
    tool = parse_tool(plugin=plugin)
    csv_path = tmp_path / "secret.csv"
    csv_path.write_text("a,b\n", encoding="utf-8")

    async def boom(path, filename, max_content_chars):
        raise RuntimeError("SECRET /root/secret/secret.csv token=fc-real-key-999")

    _patch_convert(monkeypatch, boom)
    with caplog.at_level(logging.WARNING, logger="astrbot.stub"):
        result = json.loads(
            await tool.call(
                _context(_event(File(name="secret.csv", path=str(csv_path))))
            )
        )
    assert result["ok"] is False
    assert result["category"] == "usage"
    assert result["error"] == "文件解析失败。"
    text = json.dumps(result, ensure_ascii=False)
    assert "SECRET" not in text
    assert "/root/secret" not in text
    assert "fc-real-key-999" not in text
    records = [r for r in caplog.records if r.name == "astrbot.stub"]
    assert len(records) == 1
    assert "RuntimeError" in records[0].getMessage()
    assert "SECRET" not in records[0].getMessage()
    assert plugin.executor.calls == 0
    assert plugin.scope_entries == 1
