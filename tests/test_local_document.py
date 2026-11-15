"""tools.local_document 测试：真实 DOCX/CSV/XHTML 转换、截断、大小豁免与错误路径。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot_plugin_firecrawl.tools import local_document
from astrbot_plugin_firecrawl.tools.local_document import (
    SUPPORTED_LOCAL_EXTENSIONS,
    LocalDocumentError,
    convert_local_document,
)
from docx import Document


def write_docx(path: Path) -> None:
    document = Document()
    document.add_heading("季度报告", level=1)
    document.add_paragraph("正文第一段内容。")
    document.save(path)


@pytest.mark.asyncio
async def test_converts_real_docx_to_markdown(tmp_path):
    docx_path = tmp_path / "季度报告.docx"
    write_docx(docx_path)
    result = await convert_local_document(docx_path, "季度报告.docx", 5000)
    assert result["parser"] == "markitdown"
    assert result["sourceFile"] == "季度报告.docx"
    assert "# 季度报告" in result["content"]
    assert "正文第一段内容。" in result["content"]
    assert "truncated" not in result


@pytest.mark.asyncio
async def test_converts_real_csv_to_markdown_table(tmp_path):
    csv_path = tmp_path / "data.csv"
    csv_path.write_text("name,age\nalice,30\n", encoding="utf-8")
    result = await convert_local_document(csv_path, "data.csv", 5000)
    assert "| name | age |" in result["content"]
    assert "| alice | 30 |" in result["content"]


@pytest.mark.asyncio
async def test_converts_real_xhtml_via_mimetype(tmp_path):
    xhtml_path = tmp_path / "page.xhtml"
    xhtml_path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml">'
        "<head><title>t</title></head>"
        "<body><h1>XHTML 标题</h1></body></html>",
        encoding="utf-8",
    )
    result = await convert_local_document(xhtml_path, "page.xhtml", 5000)
    assert "XHTML 标题" in result["content"]


def test_supported_extensions_match_install():
    assert SUPPORTED_LOCAL_EXTENSIONS == {
        ".pdf",
        ".docx",
        ".xlsx",
        ".xls",
        ".pptx",
        ".epub",
        ".csv",
        ".html",
        ".htm",
        ".xhtml",
    }


@pytest.mark.asyncio
async def test_truncates_long_content(tmp_path):
    csv_path = tmp_path / "rows.csv"
    rows = "".join(f"user{i},{i}\n" for i in range(50))
    csv_path.write_text(f"name,age\n{rows}", encoding="utf-8")
    result = await convert_local_document(csv_path, "rows.csv", 40)
    assert len(result["content"]) == 40
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_converter_built_with_plugins_disabled(tmp_path, monkeypatch):
    target = tmp_path / "doc.pdf"
    target.write_bytes(b"%PDF-1.4\n")
    calls = {}

    class FakeConverter:
        def __init__(self, *, enable_builtins=None, enable_plugins=None):
            calls["init"] = (enable_builtins, enable_plugins)

        def convert_local(self, path, *, file_extension=None, **kwargs):
            calls["convert"] = {"path": path, "file_extension": file_extension}
            return SimpleNamespace(text_content="# fake")

    monkeypatch.setattr(local_document, "MarkItDown", FakeConverter)
    result = await convert_local_document(target, "doc.pdf", 100)
    assert calls["init"] == (True, False)
    assert calls["convert"]["file_extension"] == ".pdf"
    assert result["content"] == "# fake"


@pytest.mark.asyncio
async def test_rejects_non_string_content(tmp_path, monkeypatch):
    target = tmp_path / "doc.csv"
    target.write_text("a,b\n", encoding="utf-8")

    class FakeConverter:
        def __init__(self, *, enable_builtins=None, enable_plugins=None):
            pass

        def convert_local(self, path, *, file_extension=None, **kwargs):
            return SimpleNamespace(text_content=None)

    monkeypatch.setattr(local_document, "MarkItDown", FakeConverter)
    with pytest.raises(LocalDocumentError):
        await convert_local_document(target, "doc.csv", 100)


@pytest.mark.asyncio
async def test_local_files_bypass_50mb_limit(tmp_path, monkeypatch):
    big = tmp_path / "big.csv"
    with big.open("wb") as fh:
        fh.truncate(50 * 1024 * 1024 + 1)

    class FakeConverter:
        def __init__(self, *, enable_builtins=None, enable_plugins=None):
            pass

        def convert_local(self, path, *, file_extension=None, **kwargs):
            return SimpleNamespace(text_content="# big")

    monkeypatch.setattr(local_document, "MarkItDown", FakeConverter)
    result = await convert_local_document(big, "big.csv", 100)
    assert big.stat().st_size > 50 * 1024 * 1024
    assert result["content"] == "# big"


@pytest.mark.asyncio
async def test_rejects_unsupported_extensions(tmp_path, monkeypatch):
    def fail_convert(*args, **kwargs):
        raise AssertionError("MarkItDown 不应被调用")

    monkeypatch.setattr(local_document, "MarkItDown", fail_convert)
    legacy = tmp_path / "old.doc"
    legacy.write_text("legacy", encoding="utf-8")
    for filename in ("old.doc", "old.ppt", "file.rtf", "file.odt", "noext"):
        with pytest.raises(LocalDocumentError):
            await convert_local_document(legacy, filename, 100)


@pytest.mark.asyncio
async def test_rejects_invalid_path_and_filename(tmp_path):
    real = tmp_path / "real.csv"
    real.write_text("a,b\n", encoding="utf-8")
    with pytest.raises(LocalDocumentError):
        await convert_local_document(str(real), "real.csv", 100)
    with pytest.raises(LocalDocumentError):
        await convert_local_document(tmp_path / "missing.pdf", "missing.pdf", 100)
    for bad in ("../real.csv", "dir/real.csv", "real.csv\\x", "real.csv\0", ""):
        with pytest.raises(LocalDocumentError):
            await convert_local_document(real, bad, 100)
    with pytest.raises(LocalDocumentError):
        await convert_local_document(tmp_path, "dir.csv", 100)


@pytest.mark.asyncio
async def test_wraps_conversion_failure_with_fixed_message(tmp_path, monkeypatch):
    target = tmp_path / "broken.csv"
    target.write_text("a,b\n", encoding="utf-8")

    class BrokenConverter:
        def __init__(self, *, enable_builtins=None, enable_plugins=None):
            pass

        def convert_local(self, path, *, file_extension=None, **kwargs):
            raise RuntimeError("secret disk error for /tmp/xyz")

    monkeypatch.setattr(local_document, "MarkItDown", BrokenConverter)
    with pytest.raises(LocalDocumentError) as excinfo:
        await convert_local_document(target, "broken.csv", 100)
    assert "broken.csv" not in str(excinfo.value)
    assert "secret disk error" not in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, RuntimeError)


@pytest.mark.asyncio
async def test_cancellation_propagates_unwrapped(tmp_path, monkeypatch):
    target = tmp_path / "slow.csv"
    target.write_text("a,b\n", encoding="utf-8")

    class CancellingConverter:
        def __init__(self, *, enable_builtins=None, enable_plugins=None):
            pass

        def convert_local(self, path, *, file_extension=None, **kwargs):
            raise asyncio.CancelledError()

    monkeypatch.setattr(local_document, "MarkItDown", CancellingConverter)
    with pytest.raises(asyncio.CancelledError):
        await convert_local_document(target, "slow.csv", 100)
