"""firecrawl_scrape FunctionTool 执行链路回归：格式裁剪、预算、失败与脱敏。"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from astrbot_plugin_firecrawl.firecrawl_api.budget import (
    BudgetPolicy,
    ReservationState,
    estimate_page,
)
from astrbot_plugin_firecrawl.firecrawl_api.client import FirecrawlClient
from astrbot_plugin_firecrawl.firecrawl_api.keys import KeyPool
from astrbot_plugin_firecrawl.persistence import SQLiteStore
from astrbot_plugin_firecrawl.tools.execution import BudgetedExecutor

K1 = "fc-scrape-execution-key-1"
URL = "https://example.com/page"
EXAMPLE = {"url": "https://example.com"}
PDF_CAP = 1
CONFIG = {"max_content_chars": 3, "pdf_max_pages": PDF_CAP, "site_max_pages": 1}
QUESTION = "页面主题是什么？"
PROMPT = "抽取标题"
SCHEMA = {"type": "object"}
METADATA = {"title": "t", "sourceURL": "https://example.com/"}
LINKS = [
    "https://a.example/1",
    "https://b.example/2",
    "https://c.example/3",
    "https://d.example/4",
]
JSON_DATA = {"name": "n" * 30, "tags": ["a", "b"], "count": 2}
FORMATS = ("markdown", "summary", "html", "links", "question", "json")

RESPONSE_DATA = {
    "markdown": {"markdown": "abcdef"},
    "summary": {"summary": "uvwxyz!"},
    "html": {"html": "<p>abcdef</p>"},
    "links": {"links": LINKS},
    "question": {"answer": "很长的回答内容"},
    "json": {"json": JSON_DATA},
}

TRIMMED = {
    "markdown": {"content": "abc", "truncated": True},
    "summary": {"content": "uvw", "truncated": True},
    "html": {"content": "<p>", "truncated": True},
    "links": {"links": LINKS},
    "question": {"answer": "很长的", "truncated": True},
    "json": {"json": JSON_DATA},
}

REQUEST_FORMATS = {
    "markdown": ["markdown"],
    "summary": ["summary"],
    "html": ["html"],
    "links": ["links"],
    "question": [{"type": "question", "question": QUESTION}],
    "json": [{"type": "json", "prompt": PROMPT, "schema": SCHEMA}],
}

EXTRA_CALLS = {
    "markdown": {},
    "summary": {"format": "summary"},
    "html": {"format": "html"},
    "links": {"format": "links"},
    "question": {"format": "question", "question": QUESTION},
    "json": {"format": "json", "json_prompt": PROMPT, "json_schema": SCHEMA},
}
IMAGE_URL = "https://img.example/shot.png"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"png-data"
DELIVERY_HINT = "页面抓取已完成并结算，未自动重试；重新调用会再次产生费用。"


def scrape_ok(data, credits_used=2.5):
    """带顶层 warning/creditsUsed 与统一 metadata 的标准成功响应。"""
    return {
        "success": True,
        "warning": "w",
        "creditsUsed": credits_used,
        "data": {"metadata": METADATA, **data},
    }


class FakeRequestScope:
    """记录进入次数的 request_scope 替身。"""

    def __init__(self) -> None:
        self.entered = 0

    async def __aenter__(self):
        self.entered += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FailingExecutor:
    """固定抛出预设异常的执行器替身，用于错误封装路径。"""

    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def execute_sync(self, kind, payload, *, estimate):
        raise self.error
        raise self.error


class FakeEvent:
    """记录 send 调用与发送时文件存在性的会话事件替身。"""

    def __init__(self) -> None:
        self.chains = []
        self.existed_at_send = []

    async def send(self, chain) -> None:
        self.existed_at_send.append([Path(p).exists() for p in chain.image_paths])
        self.chains.append(chain)


class FailingEvent:
    """send 固定抛错的会话事件替身。"""

    async def send(self, chain) -> None:
        raise RuntimeError("send failed with SECRET-MARKER")


class FakePlugin:
    """与运行接口一致的最小插件替身：config + executor + request_scope。"""

    def __init__(self, executor, config=None) -> None:
        self.config = config or {}
        self.executor = executor
        self.scope = FakeRequestScope()

    def request_scope(self):
        return self.scope


async def make_plugin(
    tmp_path, responses, config=None, *, keys=(K1,), policy=None, on_request=None
):
    """真实 SQLiteStore + KeyPool + FirecrawlClient + BudgetedExecutor 组装插件替身。"""
    store = SQLiteStore(tmp_path / "budget.db", policy=policy or BudgetPolicy())
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

    client = FirecrawlClient(
        KeyPool(list(keys)), transport=httpx.MockTransport(handler)
    )
    plugin = FakePlugin(BudgetedExecutor(client, store), config)
    plugin.seen = seen
    plugin.store = store
    plugin.client = client
    return plugin


async def assert_usage(plugin, used, active_reserved):
    snapshot = await plugin.store.usage()
    assert snapshot.used == used
    assert snapshot.active_reserved == active_reserved


@pytest.mark.parametrize("fmt", FORMATS)
@pytest.mark.asyncio
async def test_formats_round_trip_trim_and_billing(
    firecrawl_scrape_tool, tmp_path, fmt
):
    snapshots = []

    async def on_request(store):
        snapshots.append(await store.list_pending())

    plugin = await make_plugin(
        tmp_path, [scrape_ok(RESPONSE_DATA[fmt])], config=CONFIG, on_request=on_request
    )
    tool = firecrawl_scrape_tool(plugin=plugin)
    out = await tool.call(None, url=URL, **EXTRA_CALLS[fmt])
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["data"] == {
        "title": "t",
        "url": METADATA["sourceURL"],
        **TRIMMED[fmt],
        "warning": "w",
    }
    assert json.loads(plugin.seen[0].content) == {
        "url": URL,
        "formats": REQUEST_FORMATS[fmt],
        "parsers": [{"type": "pdf", "maxPages": PDF_CAP}],
        "proxy": "auto",
        "onlyMainContent": True,
        "waitFor": 0,
        "checkPromptInjection": False,
    }
    assert len(plugin.seen) == 1
    assert plugin.scope.entered == 1
    pending = snapshots[0]
    assert len(pending) == 1
    assert pending[0].state is ReservationState.SUBMITTING
    assert pending[0].estimate == estimate_page(fmt, PDF_CAP, False)
    await assert_usage(plugin, 2.5, 0.0)
    assert await plugin.store.list_pending() == []


@pytest.mark.parametrize(
    ("config", "call", "injection", "explicit_options"),
    [
        (
            {"check_prompt_injection": True, "pdf_max_pages": 7},
            {
                "only_main_content": False,
                "wait_for_ms": 0,
                "max_age_ms": 0,
                "proxy": "enhanced",
            },
            True,
            True,
        ),
        (
            {"check_prompt_injection": True, "pdf_max_pages": 7},
            {"check_prompt_injection": False},
            False,
            False,
        ),
        ({"pdf_max_pages": 7}, {"check_prompt_injection": True}, True, False),
    ],
)
@pytest.mark.asyncio
async def test_injection_config_override_and_estimate(
    firecrawl_scrape_tool, tmp_path, config, call, injection, explicit_options
):
    snapshots = []

    async def on_request(store):
        snapshots.append(await store.list_pending())

    response = {"success": True, "data": {"markdown": "ok"}}
    plugin = await make_plugin(
        tmp_path, [response], config=config, on_request=on_request
    )
    tool = firecrawl_scrape_tool(plugin=plugin)
    out = await tool.call(None, url=URL, **call)
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is True
    body = json.loads(plugin.seen[0].content)
    assert body["checkPromptInjection"] is injection
    assert body["parsers"] == [{"type": "pdf", "maxPages": 7}]
    if explicit_options:
        assert body["onlyMainContent"] is False
        assert body["waitFor"] == 0
        assert body["maxAge"] == 0
        assert body["proxy"] == "enhanced"
    else:
        assert "maxAge" not in body
    estimate = estimate_page("markdown", 7, injection)
    assert snapshots[0][0].estimate == estimate
    await assert_usage(plugin, estimate, 0.0)


@pytest.mark.asyncio
async def test_anonymous_scrape_without_authorization(firecrawl_scrape_tool, tmp_path):
    plugin = await make_plugin(tmp_path, [scrape_ok({"markdown": "abc"})], keys=())
    tool = firecrawl_scrape_tool(plugin=plugin)
    out = await tool.call(None, url=URL)
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is True
    assert "authorization" not in plugin.seen[0].headers
    assert plugin.scope.entered == 1
    await assert_usage(plugin, 2.5, 0.0)


@pytest.mark.asyncio
async def test_per_call_budget_exceeded_no_network_or_reserve(
    firecrawl_scrape_tool, tmp_path
):
    policy = BudgetPolicy(max_estimated_credits_per_call=20)
    plugin = await make_plugin(tmp_path, [], policy=policy)
    tool = firecrawl_scrape_tool(plugin=plugin)
    out = await tool.call(None, url=URL)
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["category"] == "usage"
    assert "单次预估 21 credits 超过单次上限 20" in payload["error"]
    assert payload["example"] == EXAMPLE
    assert plugin.seen == []
    assert plugin.scope.entered == 1
    await assert_usage(plugin, 0.0, 0.0)
    assert await plugin.store.list_pending() == []


@pytest.mark.asyncio
async def test_quota_402_releases_reservation(firecrawl_scrape_tool, tmp_path):
    plugin = await make_plugin(
        tmp_path, [httpx.Response(402, json={"success": False, "error": "no credits"})]
    )
    tool = firecrawl_scrape_tool(plugin=plugin)
    out = await tool.call(None, url=URL)
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["category"] == "quota"
    assert payload["retryable"] is False
    assert "no credits" not in payload["error"]
    assert "example" not in payload
    assert len(plugin.seen) == 1
    await assert_usage(plugin, 0.0, 0.0)
    assert await plugin.store.list_pending() == []


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(
            httpx.Response(500, json={"success": False, "error": "boom"}),
            id="server_500",
        ),
        pytest.param(httpx.ReadTimeout("too slow"), id="read_timeout"),
        pytest.param(
            httpx.Response(
                200, json={"success": True, "creditsUsed": "many", "data": {}}
            ),
            id="bad_credits",
        ),
    ],
)
@pytest.mark.asyncio
async def test_indeterminate_single_request_and_reservation(
    firecrawl_scrape_tool, tmp_path, response
):
    plugin = await make_plugin(tmp_path, [response])
    tool = firecrawl_scrape_tool(plugin=plugin)
    out = await tool.call(None, url=URL)
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["category"] == "indeterminate"
    assert payload["pending"] is True
    assert payload["retryable"] is False
    assert payload["reservation_id"]
    assert "task_handle" not in payload
    assert "example" not in payload
    assert len(plugin.seen) == 1
    record = await plugin.store.get(payload["reservation_id"])
    assert record.state is ReservationState.INDETERMINATE
    assert record.accounted_credits == 0.0
    await assert_usage(plugin, 0.0, 21.0)


@pytest.mark.asyncio
async def test_unknown_error_server_fail_without_echoing(firecrawl_scrape_tool, caplog):
    error = RuntimeError("secret fc-scrape-key-abc123 token=xyz")
    plugin = FakePlugin(FailingExecutor(error))
    tool = firecrawl_scrape_tool(plugin=plugin)
    with caplog.at_level(logging.WARNING, logger="astrbot.stub"):
        out = await tool.call(None, url=URL)
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["category"] == "server"
    assert payload["error"] == "插件内部错误。"
    assert payload["retryable"] is False
    assert "example" not in payload
    assert "secret" not in out
    assert "token=xyz" not in out
    records = [r for r in caplog.records if r.name == "astrbot.stub"]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "firecrawl_scrape" in message
    assert "RuntimeError" in message
    assert "secret" not in message


@pytest.mark.asyncio
async def test_cancelled_error_propagates(firecrawl_scrape_tool):
    plugin = FakePlugin(FailingExecutor(asyncio.CancelledError()))
    tool = firecrawl_scrape_tool(plugin=plugin)
    with pytest.raises(asyncio.CancelledError):
        await tool.call(None, url=URL)


def image_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_screenshot_round_trip_downloads_sends_and_cleans(
    firecrawl_scrape_tool, tmp_path
):
    seen_downloads = []

    def download_handler(request: httpx.Request) -> httpx.Response:
        seen_downloads.append(request)
        return httpx.Response(
            200, headers={"content-type": "image/png"}, content=PNG_BYTES
        )

    plugin = await make_plugin(
        tmp_path, [scrape_ok({"screenshot": IMAGE_URL})], config=CONFIG
    )
    event = FakeEvent()
    tool = firecrawl_scrape_tool(
        plugin=plugin,
        data_dir=tmp_path,
        download_transport=image_transport(download_handler),
    )
    out = await tool.call(
        SimpleNamespace(context=SimpleNamespace(event=event)),
        url=URL,
        format="screenshot",
    )
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["data"] == {
        "title": "t",
        "url": METADATA["sourceURL"],
        "warning": "w",
        "sent_to_user": True,
    }
    assert json.loads(plugin.seen[0].content)["formats"] == ["screenshot"]
    assert len(plugin.seen) == 1
    assert "authorization" not in seen_downloads[0].headers
    assert event.existed_at_send == [[True]]
    sent_path = Path(event.chains[0].image_paths[0])
    assert sent_path.suffix == ".png"
    assert not sent_path.exists()
    assert list((tmp_path / "screenshots").iterdir()) == []
    assert plugin.scope.entered == 1
    await assert_usage(plugin, 2.5, 0.0)
    assert await plugin.store.list_pending() == []


@pytest.mark.asyncio
async def test_screenshot_without_event_no_network_or_reservation(
    firecrawl_scrape_tool, tmp_path
):
    plugin = await make_plugin(tmp_path, [], config=CONFIG)
    tool = firecrawl_scrape_tool(plugin=plugin, data_dir=tmp_path)
    out = await tool.call(None, url=URL, format="screenshot")
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["category"] == "usage"
    assert plugin.seen == []
    assert plugin.scope.entered == 0
    await assert_usage(plugin, 0.0, 0.0)
    assert await plugin.store.list_pending() == []


@pytest.mark.asyncio
async def test_screenshot_download_failure_keeps_settled_without_pending(
    firecrawl_scrape_tool, tmp_path
):
    def download_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"<html>no</html>"
        )

    plugin = await make_plugin(
        tmp_path, [scrape_ok({"screenshot": IMAGE_URL})], config=CONFIG
    )
    tool = firecrawl_scrape_tool(
        plugin=plugin,
        data_dir=tmp_path,
        download_transport=image_transport(download_handler),
    )
    out = await tool.call(
        SimpleNamespace(context=SimpleNamespace(event=FakeEvent())),
        url=URL,
        format="screenshot",
    )
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["category"] == "server"
    assert payload["hint"] == DELIVERY_HINT
    assert IMAGE_URL not in out
    assert len(plugin.seen) == 1
    assert list((tmp_path / "screenshots").iterdir()) == []
    await assert_usage(plugin, 2.5, 0.0)
    assert await plugin.store.list_pending() == []


@pytest.mark.asyncio
async def test_screenshot_send_failure_keeps_settled_without_pending(
    firecrawl_scrape_tool, tmp_path
):
    def download_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "image/png"}, content=PNG_BYTES
        )

    plugin = await make_plugin(
        tmp_path, [scrape_ok({"screenshot": IMAGE_URL})], config=CONFIG
    )
    tool = firecrawl_scrape_tool(
        plugin=plugin,
        data_dir=tmp_path,
        download_transport=image_transport(download_handler),
    )
    out = await tool.call(
        SimpleNamespace(context=SimpleNamespace(event=FailingEvent())),
        url=URL,
        format="screenshot",
    )
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["category"] == "server"
    assert payload["hint"] == DELIVERY_HINT
    assert IMAGE_URL not in out
    assert "SECRET-MARKER" not in out
    assert len(plugin.seen) == 1
    assert list((tmp_path / "screenshots").iterdir()) == []
    await assert_usage(plugin, 2.5, 0.0)
    assert await plugin.store.list_pending() == []


def failing_unlink(self, missing_ok=False):
    raise OSError("boom-unlink")


@pytest.mark.asyncio
async def test_screenshot_send_success_cleanup_failure_still_ok(
    firecrawl_scrape_tool, tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(Path, "unlink", failing_unlink)

    def download_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "image/png"}, content=PNG_BYTES
        )

    plugin = await make_plugin(
        tmp_path, [scrape_ok({"screenshot": IMAGE_URL})], config=CONFIG
    )
    event = FakeEvent()
    tool = firecrawl_scrape_tool(
        plugin=plugin,
        data_dir=tmp_path,
        download_transport=image_transport(download_handler),
    )
    with caplog.at_level("WARNING", logger="astrbot.stub"):
        out = await tool.call(
            SimpleNamespace(context=SimpleNamespace(event=event)),
            url=URL,
            format="screenshot",
        )
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["data"]["sent_to_user"] is True
    assert any("OSError" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_screenshot_send_failure_cleanup_failure_keeps_delivery_error(
    firecrawl_scrape_tool, tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(Path, "unlink", failing_unlink)

    def download_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "image/png"}, content=PNG_BYTES
        )

    plugin = await make_plugin(
        tmp_path, [scrape_ok({"screenshot": IMAGE_URL})], config=CONFIG
    )
    tool = firecrawl_scrape_tool(
        plugin=plugin,
        data_dir=tmp_path,
        download_transport=image_transport(download_handler),
    )
    with caplog.at_level("WARNING", logger="astrbot.stub"):
        out = await tool.call(
            SimpleNamespace(context=SimpleNamespace(event=FailingEvent())),
            url=URL,
            format="screenshot",
        )
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["category"] == "server"
    assert payload["hint"] == DELIVERY_HINT
    assert "boom-unlink" not in out
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "RuntimeError" in messages
    assert "OSError" in messages


@pytest.mark.asyncio
async def test_screenshot_cancellation_not_replaced_by_cleanup_error(
    firecrawl_scrape_tool, tmp_path, monkeypatch
):
    monkeypatch.setattr(Path, "unlink", failing_unlink)

    class SlowEvent:
        async def send(self, chain) -> None:
            await asyncio.sleep(5)

    def download_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "image/png"}, content=PNG_BYTES
        )

    plugin = await make_plugin(
        tmp_path, [scrape_ok({"screenshot": IMAGE_URL})], config=CONFIG
    )
    tool = firecrawl_scrape_tool(
        plugin=plugin,
        data_dir=tmp_path,
        download_transport=image_transport(download_handler),
    )
    task = asyncio.create_task(
        tool.call(
            SimpleNamespace(context=SimpleNamespace(event=SlowEvent())),
            url=URL,
            format="screenshot",
        )
    )
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await plugin.client.aclose()


@pytest.mark.asyncio
async def test_screenshot_network_error_keeps_settled_without_pending(
    firecrawl_scrape_tool, tmp_path
):
    def download_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom-net")

    plugin = await make_plugin(
        tmp_path, [scrape_ok({"screenshot": IMAGE_URL})], config=CONFIG
    )
    tool = firecrawl_scrape_tool(
        plugin=plugin,
        data_dir=tmp_path,
        download_transport=image_transport(download_handler),
    )
    out = await tool.call(
        SimpleNamespace(context=SimpleNamespace(event=FakeEvent())),
        url=URL,
        format="screenshot",
    )
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["category"] == "server"
    assert payload["hint"] == DELIVERY_HINT
    assert "boom-net" not in out
    assert len(plugin.seen) == 1
    assert list((tmp_path / "screenshots").iterdir()) == []
    await assert_usage(plugin, 2.5, 0.0)
    assert await plugin.store.list_pending() == []


@pytest.mark.asyncio
async def test_screenshot_write_oserror_keeps_settled_without_pending(
    firecrawl_scrape_tool, tmp_path, monkeypatch
):
    def broken_write(path, chunks):
        raise OSError("boom-write")

    monkeypatch.setattr(
        "astrbot_plugin_firecrawl.tools.screenshot._write_chunks", broken_write
    )

    def download_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "image/png"}, content=PNG_BYTES
        )

    plugin = await make_plugin(
        tmp_path, [scrape_ok({"screenshot": IMAGE_URL})], config=CONFIG
    )
    tool = firecrawl_scrape_tool(
        plugin=plugin,
        data_dir=tmp_path,
        download_transport=image_transport(download_handler),
    )
    out = await tool.call(
        SimpleNamespace(context=SimpleNamespace(event=FakeEvent())),
        url=URL,
        format="screenshot",
    )
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["category"] == "server"
    assert payload["hint"] == DELIVERY_HINT
    assert "boom-write" not in out
    assert len(plugin.seen) == 1
    assert list((tmp_path / "screenshots").iterdir()) == []
    await assert_usage(plugin, 2.5, 0.0)
    assert await plugin.store.list_pending() == []


@pytest.mark.asyncio
async def test_screenshot_accepts_numeric_string_timeout(
    firecrawl_scrape_tool, tmp_path
):
    def download_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "image/png"}, content=PNG_BYTES
        )

    config = {"timeout_seconds": "30", "max_content_chars": 3}
    plugin = await make_plugin(
        tmp_path, [scrape_ok({"screenshot": IMAGE_URL})], config=config
    )
    tool = firecrawl_scrape_tool(
        plugin=plugin,
        data_dir=tmp_path,
        download_transport=image_transport(download_handler),
    )
    out = await tool.call(
        SimpleNamespace(context=SimpleNamespace(event=FakeEvent())),
        url=URL,
        format="screenshot",
    )
    await plugin.client.aclose()
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["data"]["sent_to_user"] is True
