"""firecrawl_parse FunctionTool 执行链路回归：multipart、预算、回退与脱敏。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from astrbot.api.message_components import File
from astrbot_plugin_firecrawl.firecrawl_api.budget import BudgetPolicy, ReservationState
from astrbot_plugin_firecrawl.firecrawl_api.client import FirecrawlClient
from astrbot_plugin_firecrawl.firecrawl_api.keys import KeyPool
from astrbot_plugin_firecrawl.persistence import SQLiteStore
from astrbot_plugin_firecrawl.tools.execution import BudgetedExecutor
from astrbot_plugin_firecrawl.tools.local_document import LocalDocumentError
from docx import Document

K1 = "fc-parse-execution-key-1"
PARSE_URL = "https://api.firecrawl.dev/v2/parse"
PARSE_ALIAS = "astrbot_plugin_firecrawl.tools._sdk_parse"
PDF_CAP = 7
PDF_CONFIG = {"max_content_chars": 5, "pdf_max_pages": PDF_CAP}
CSV_CONFIG = {"max_content_chars": 1000, "pdf_max_pages": PDF_CAP}
PDF_BYTES = b"%PDF-1.4\n" + bytes(range(64))
PENDING_HINT = "请勿重发该请求；请与管理员核对或用 fc pending 查询在途请求。"
PENDING_LOCAL_HINT = "本地回退解析失败；请勿重发该请求，请管理员核对原预留。"
PENDING_WARNING = "云端结果不确定，已本地解析；管理员需核对原预留。"
CLOUD_FAILED_WARNING = "云端解析失败，已本地解析。"
SETTLED_INVALID_WARNING = "云端响应无效，已本地解析。"
SETTLED_INVALID_TEXT = "云端已结算但返回的解析结果无效，重新调用会再次计费。"


class FakeRequestScope:
    """记录进入次数的 request_scope 替身。"""

    def __init__(self) -> None:
        self.entered = 0

    async def __aenter__(self):
        self.entered += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePlugin:
    """与运行接口一致的最小插件替身：config + executor + key_pool + request_scope。"""

    def __init__(self, executor, config=None) -> None:
        self.config = config or {}
        self.executor = executor
        self.key_pool = None
        self.scope = FakeRequestScope()

    def request_scope(self):
        return self.scope


async def make_plugin(tmp_path, responses, config=None, *, keys=(K1,), on_request=None):
    """真实 SQLiteStore + KeyPool + FirecrawlClient + BudgetedExecutor 组装插件替身。"""
    store = SQLiteStore(tmp_path / "budget.db", policy=BudgetPolicy())
    await store.initialize()
    seen = []
    queue = list(responses)

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if on_request is not None:
            await on_request(store)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return httpx.Response(200, json=item) if isinstance(item, dict) else item

    pool = KeyPool(list(keys))
    client = FirecrawlClient(pool, transport=httpx.MockTransport(handler))
    plugin = FakePlugin(BudgetedExecutor(client, store), config)
    plugin.key_pool = pool
    plugin.seen = seen
    plugin.store = store
    plugin.client = client
    return plugin


async def assert_usage(plugin, used, active_reserved):
    snapshot = await plugin.store.usage()
    assert snapshot.used == used
    assert snapshot.active_reserved == active_reserved


def _context(event) -> SimpleNamespace:
    return SimpleNamespace(context=SimpleNamespace(event=event))


def _event(*components) -> SimpleNamespace:
    return SimpleNamespace(message_obj=SimpleNamespace(message=list(components)))


def write_docx(path: Path) -> None:
    document = Document()
    document.add_heading("季度报告", level=1)
    document.add_paragraph("正文第一段内容。")
    document.save(path)


def write_pdf(tmp_path: Path) -> Path:
    path = tmp_path / "论文.pdf"
    path.write_bytes(PDF_BYTES)
    return path


def write_csv(tmp_path: Path) -> Path:
    path = tmp_path / "数据.csv"
    path.write_text("name,age\nalice,30\n", encoding="utf-8")
    return path


def canonical_options(options: dict) -> str:
    """按执行器约定复算 options 规范 JSON 字符串。"""
    return json.dumps(
        options,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def multipart_fields(request: httpx.Request) -> list[tuple[dict[str, str], bytes]]:
    """按请求头 boundary 解析 multipart 请求体为 (字段头, 字段数据) 列表。"""
    content_type = request.headers["content-type"]
    assert content_type.startswith("multipart/form-data; boundary=")
    boundary = b"--" + content_type.split("boundary=", 1)[1].encode()
    fields = []
    for segment in request.content.split(boundary)[1:]:
        if segment.startswith(b"--"):
            continue
        header_block, _, data = segment.lstrip(b"\r\n").partition(b"\r\n\r\n")
        assert data.endswith(b"\r\n")
        headers = {}
        for line in header_block.split(b"\r\n"):
            name, _, value = line.partition(b":")
            headers[name.decode().strip().lower()] = value.decode().strip()
        fields.append((headers, data[:-2]))
    return fields


def parse_options(config: dict) -> dict:
    """按配置复算云端 parse options。"""
    return {
        "formats": ["markdown"],
        "parsers": [{"type": "pdf", "maxPages": config["pdf_max_pages"]}],
        "checkPromptInjection": False,
    }


@pytest.mark.asyncio
async def test_cloud_parse_multipart_success_trims_and_settles(
    sdk_tool_loader, tmp_path
):
    """PDF multipart 成功：options/file 字段、估算、实际费用与结果裁剪全部落地。"""
    snapshots = []

    async def on_request(store):
        snapshots.append(await store.list_pending())

    body = {
        "success": True,
        "warning": "w",
        "creditsUsed": 2.5,
        "data": {
            "markdown": "abcdef",
            "metadata": {
                "title": "t",
                "sourceFile": "server.pdf",
                "numPages": 3,
                "totalPages": 5,
            },
        },
    }
    plugin = await make_plugin(
        tmp_path, [body], config=PDF_CONFIG, on_request=on_request
    )
    pdf_path = write_pdf(tmp_path)
    with sdk_tool_loader("parse", "FirecrawlParseTool") as tool_class:
        tool = tool_class(plugin=plugin)
        out = await tool.call(
            _context(_event(File(name="论文.pdf", path=str(pdf_path))))
        )
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["data"] == {
        "content": "abcde",
        "truncated": True,
        "title": "t",
        "sourceFile": "论文.pdf",
        "numPages": 3,
        "totalPages": 5,
        "pagesTruncated": True,
        "warning": "w",
        "parser": "firecrawl",
    }
    request = plugin.seen[0]
    assert str(request.url) == PARSE_URL
    assert request.method == "POST"
    assert request.headers["authorization"] == f"Bearer {K1}"
    fields = multipart_fields(request)
    assert len(fields) == 2
    assert fields[0][0] == {"content-disposition": 'form-data; name="options"'}
    assert fields[0][1] == canonical_options(parse_options(PDF_CONFIG)).encode("utf-8")
    assert fields[1][0] == {
        "content-disposition": 'form-data; name="file"; filename="论文.pdf"',
        "content-type": "application/pdf",
    }
    assert fields[1][1] == PDF_BYTES
    assert len(plugin.seen) == 1
    assert plugin.scope.entered == 1
    assert snapshots[0][0].estimate == 1 + PDF_CAP
    assert snapshots[0][0].state is ReservationState.SUBMITTING
    await assert_usage(plugin, 2.5, 0.0)
    assert await plugin.store.list_pending() == []
    assert str(tmp_path) not in out
    assert "%PDF-1.4" not in out


@pytest.mark.asyncio
async def test_cloud_parse_csv_estimate_and_result(sdk_tool_loader, tmp_path):
    """CSV 走云端时估算按非 PDF 计算，结果覆盖 sourceFile 并带 parser。"""
    body = {
        "success": True,
        "creditsUsed": 1,
        "data": {"markdown": "| name | age |\n| alice | 30 |", "metadata": {}},
    }
    plugin = await make_plugin(tmp_path, [body], config=CSV_CONFIG)
    csv_path = write_csv(tmp_path)
    with sdk_tool_loader("parse", "FirecrawlParseTool") as tool_class:
        tool = tool_class(plugin=plugin)
        out = await tool.call(
            _context(_event(File(name="数据.csv", path=str(csv_path))))
        )
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["data"]["parser"] == "firecrawl"
    assert payload["data"]["sourceFile"] == "数据.csv"
    assert "| alice | 30 |" in payload["data"]["content"]
    assert "truncated" not in payload["data"]
    fields = multipart_fields(plugin.seen[0])
    assert fields[0][1] == canonical_options(parse_options(CSV_CONFIG)).encode("utf-8")
    assert fields[1][0]["content-type"] == "text/csv"
    assert len(plugin.seen) == 1
    await assert_usage(plugin, 1.0, 0.0)
    assert await plugin.store.list_pending() == []


@pytest.mark.asyncio
async def test_oversize_local_routes_zero_cloud(sdk_tool_loader, tmp_path, monkeypatch):
    """超限本地路由零云端零预留，请求队列为空即任何上云都会失败。"""
    plugin = await make_plugin(tmp_path, [], config=CSV_CONFIG)
    big = tmp_path / "big.csv"
    with open(big, "wb") as handle:
        handle.truncate(50_000_001)

    async def fake_convert(path, filename, max_content_chars):
        return {"parser": "markitdown", "sourceFile": filename, "content": "local"}

    with sdk_tool_loader("parse", "FirecrawlParseTool") as tool_class:
        monkeypatch.setattr(
            sys.modules[PARSE_ALIAS], "convert_local_document", fake_convert
        )
        tool = tool_class(plugin=plugin)
        out = await tool.call(_context(_event(File(name="big.csv", path=str(big)))))
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["data"] == {
        "parser": "markitdown",
        "sourceFile": "big.csv",
        "content": "local",
    }
    assert plugin.seen == []
    assert plugin.scope.entered == 1
    await assert_usage(plugin, 0.0, 0.0)
    assert await plugin.store.list_pending() == []


@pytest.mark.asyncio
async def test_oversize_unsupported_rejects_zero_cloud(sdk_tool_loader, tmp_path):
    """超限且本地不支持时明确拒绝，零云端、零预留。"""
    plugin = await make_plugin(tmp_path, [], config=CSV_CONFIG)
    big = tmp_path / "big.rtf"
    with open(big, "wb") as handle:
        handle.truncate(50_000_001)
    with sdk_tool_loader("parse", "FirecrawlParseTool") as tool_class:
        tool = tool_class(plugin=plugin)
        out = await tool.call(_context(_event(File(name="big.rtf", path=str(big)))))
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["category"] == "usage"
    assert "50000000" in payload["error"]
    assert plugin.seen == []
    await assert_usage(plugin, 0.0, 0.0)
    assert await plugin.store.list_pending() == []


@pytest.mark.asyncio
async def test_quota_error_falls_back_to_real_docx(sdk_tool_loader, tmp_path):
    """云端 402 后用真实 DOCX 本地回退一次：成功不计费，不伪造 pending。"""
    plugin = await make_plugin(
        tmp_path,
        [httpx.Response(402, json={"success": False, "error": "no credits"})],
        config=CSV_CONFIG,
    )
    docx_path = tmp_path / "季度报告.docx"
    write_docx(docx_path)
    with sdk_tool_loader("parse", "FirecrawlParseTool") as tool_class:
        tool = tool_class(plugin=plugin)
        out = await tool.call(
            _context(_event(File(name="季度报告.docx", path=str(docx_path))))
        )
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is True
    data = payload["data"]
    assert data["parser"] == "markitdown"
    assert data["sourceFile"] == "季度报告.docx"
    assert "# 季度报告" in data["content"]
    assert "正文第一段内容。" in data["content"]
    assert data["warning"] == CLOUD_FAILED_WARNING
    assert "cloud_pending" not in data
    assert len(plugin.seen) == 1
    await assert_usage(plugin, 0.0, 0.0)
    assert await plugin.store.list_pending() == []
    assert str(docx_path) not in out


@pytest.mark.asyncio
async def test_pending_with_real_csv_fallback_keeps_pending(sdk_tool_loader, tmp_path):
    """pending 后真实 CSV 本地回退成功：保留 pending 状态与预留 ID，不结算。"""
    body = {"success": True, "creditsUsed": "bad", "data": {"markdown": "x"}}
    plugin = await make_plugin(tmp_path, [body], config=CSV_CONFIG)
    csv_path = write_csv(tmp_path)
    with sdk_tool_loader("parse", "FirecrawlParseTool") as tool_class:
        tool = tool_class(plugin=plugin)
        out = await tool.call(
            _context(_event(File(name="数据.csv", path=str(csv_path))))
        )
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is True
    data = payload["data"]
    assert data["parser"] == "markitdown"
    assert data["sourceFile"] == "数据.csv"
    assert "| alice | 30 |" in data["content"]
    assert data["cloud_pending"] is True
    assert data["warning"] == PENDING_WARNING
    record = await plugin.store.get(data["reservation_id"])
    assert record.state is ReservationState.INDETERMINATE
    assert record.accounted_credits == 0.0
    assert len(plugin.seen) == 1
    assert plugin.scope.entered == 1
    await assert_usage(plugin, 0.0, 1.0)


@pytest.mark.asyncio
async def test_settled_bad_response_falls_back_without_pending(
    sdk_tool_loader, tmp_path
):
    """云端已结算但响应无效：真实 CSV 本地回退成功且不标 pending。"""
    body = {"success": True, "creditsUsed": 3, "data": {"markdown": 123}}
    plugin = await make_plugin(tmp_path, [body], config=CSV_CONFIG)
    csv_path = write_csv(tmp_path)
    with sdk_tool_loader("parse", "FirecrawlParseTool") as tool_class:
        tool = tool_class(plugin=plugin)
        out = await tool.call(
            _context(_event(File(name="数据.csv", path=str(csv_path))))
        )
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is True
    data = payload["data"]
    assert data["parser"] == "markitdown"
    assert "| alice | 30 |" in data["content"]
    assert data["warning"] == SETTLED_INVALID_WARNING
    assert "cloud_pending" not in data
    assert "reservation_id" not in data
    assert len(plugin.seen) == 1
    await assert_usage(plugin, 3.0, 0.0)
    assert await plugin.store.list_pending() == []


@pytest.mark.asyncio
async def test_pending_local_unsupported_returns_pending(sdk_tool_loader, tmp_path):
    """pending 且本地不支持时仍按 pending 返回，预留保持不确定。"""
    body = {"success": True, "creditsUsed": "bad", "data": {"markdown": "x"}}
    plugin = await make_plugin(tmp_path, [body], config=CSV_CONFIG)
    odt_path = tmp_path / "d.odt"
    odt_path.write_bytes(b"ODT")
    with sdk_tool_loader("parse", "FirecrawlParseTool") as tool_class:
        tool = tool_class(plugin=plugin)
        out = await tool.call(_context(_event(File(name="d.odt", path=str(odt_path)))))
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["category"] == "indeterminate"
    assert payload["pending"] is True
    assert payload["retryable"] is False
    assert payload["hint"] == PENDING_HINT
    assert "task_handle" not in payload
    record = await plugin.store.get(payload["reservation_id"])
    assert record.state is ReservationState.INDETERMINATE
    assert len(plugin.seen) == 1
    await assert_usage(plugin, 0.0, 1.0)


@pytest.mark.asyncio
async def test_pending_local_failure_returns_pending_hint(
    sdk_tool_loader, tmp_path, monkeypatch
):
    """pending 且本地转换也失败时按 pending 返回并说明本地回退失败。"""
    body = {"success": True, "creditsUsed": "bad", "data": {"markdown": "x"}}
    plugin = await make_plugin(tmp_path, [body], config=CSV_CONFIG)
    docx_path = tmp_path / "broken.docx"
    write_docx(docx_path)
    with sdk_tool_loader("parse", "FirecrawlParseTool") as tool_class:
        tool = tool_class(plugin=plugin)

        async def fail_convert(path, filename, max_content_chars):
            raise LocalDocumentError("本地文档转换失败。")

        monkeypatch.setattr(
            sys.modules[PARSE_ALIAS], "convert_local_document", fail_convert
        )
        out = await tool.call(
            _context(_event(File(name="broken.docx", path=str(docx_path))))
        )
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["category"] == "indeterminate"
    assert payload["pending"] is True
    assert payload["hint"] == PENDING_LOCAL_HINT
    record = await plugin.store.get(payload["reservation_id"])
    assert record.state is ReservationState.INDETERMINATE
    assert len(plugin.seen) == 1
    await assert_usage(plugin, 0.0, 1.0)


@pytest.mark.asyncio
async def test_settled_local_failure_reports_settlement(sdk_tool_loader, tmp_path):
    """已结算坏响应且本地不支持时提示已结算、重调会再次计费。"""
    body = {"success": True, "creditsUsed": 3, "data": {"markdown": 123}}
    plugin = await make_plugin(tmp_path, [body], config=CSV_CONFIG)
    rtf_path = tmp_path / "d.rtf"
    rtf_path.write_bytes(b"RTF")
    with sdk_tool_loader("parse", "FirecrawlParseTool") as tool_class:
        tool = tool_class(plugin=plugin)
        out = await tool.call(_context(_event(File(name="d.rtf", path=str(rtf_path)))))
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["category"] == "usage"
    assert payload["error"] == SETTLED_INVALID_TEXT
    assert len(plugin.seen) == 1
    await assert_usage(plugin, 3.0, 0.0)
    assert await plugin.store.list_pending() == []


@pytest.mark.asyncio
async def test_settled_local_convert_failure_reports_settlement(
    sdk_tool_loader, tmp_path, monkeypatch
):
    """已结算坏响应且本地转换失败时固定已结算提示，费用只结算一次。"""
    body = {"success": True, "creditsUsed": 3, "data": {"markdown": 123}}
    plugin = await make_plugin(tmp_path, [body], config=CSV_CONFIG)
    csv_path = write_csv(tmp_path)
    with sdk_tool_loader("parse", "FirecrawlParseTool") as tool_class:
        tool = tool_class(plugin=plugin)

        async def fail_convert(path, filename, max_content_chars):
            raise LocalDocumentError("本地文档转换失败。")

        monkeypatch.setattr(
            sys.modules[PARSE_ALIAS], "convert_local_document", fail_convert
        )
        out = await tool.call(
            _context(_event(File(name="数据.csv", path=str(csv_path))))
        )
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["category"] == "usage"
    assert payload["error"] == SETTLED_INVALID_TEXT
    assert len(plugin.seen) == 1
    await assert_usage(plugin, 3.0, 0.0)
    assert await plugin.store.list_pending() == []


@pytest.mark.asyncio
async def test_cloud_error_local_failure_keeps_category(sdk_tool_loader, tmp_path):
    """云端失败且本地不支持时保留原错误类别，不吞错。"""
    plugin = await make_plugin(
        tmp_path,
        [httpx.Response(402, json={"success": False, "error": "no credits"})],
        config=CSV_CONFIG,
    )
    odt_path = tmp_path / "d.odt"
    odt_path.write_bytes(b"ODT")
    with sdk_tool_loader("parse", "FirecrawlParseTool") as tool_class:
        tool = tool_class(plugin=plugin)
        out = await tool.call(_context(_event(File(name="d.odt", path=str(odt_path)))))
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["category"] == "quota"
    assert payload["retryable"] is False
    assert "no credits" not in payload["error"]
    assert len(plugin.seen) == 1
    await assert_usage(plugin, 0.0, 0.0)
    assert await plugin.store.list_pending() == []


@pytest.mark.asyncio
async def test_cancelled_error_propagates(sdk_tool_loader, tmp_path):
    """取消异常原样传播，不自动切换本地或重试云端。"""

    class CancelExecutor:
        async def execute_parse(self, **kwargs):
            raise asyncio.CancelledError()

    plugin = await make_plugin(tmp_path, [], config=CSV_CONFIG)
    plugin.executor = CancelExecutor()
    csv_path = write_csv(tmp_path)
    with sdk_tool_loader("parse", "FirecrawlParseTool") as tool_class:
        tool = tool_class(plugin=plugin)
        with pytest.raises(asyncio.CancelledError):
            await tool.call(_context(_event(File(name="数据.csv", path=str(csv_path)))))
    await plugin.client.aclose()
    assert plugin.seen == []
    assert plugin.scope.entered == 1
