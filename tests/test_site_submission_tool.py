"""firecrawl_site crawl/batch 提交链路回归：请求体契约、估算一致、任务句柄与 pending 账目。"""

from __future__ import annotations

import json
import logging
import sys
import types
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import httpx
import pytest
from astrbot_plugin_firecrawl.firecrawl_api.budget import BudgetPolicy, ReservationState
from astrbot_plugin_firecrawl.firecrawl_api.client import FirecrawlClient
from astrbot_plugin_firecrawl.firecrawl_api.keys import KeyPool, key_reference
from astrbot_plugin_firecrawl.firecrawl_api.site import (
    estimate_site_credits,
    parse_task_handle,
)
from astrbot_plugin_firecrawl.persistence import SQLiteStore
from astrbot_plugin_firecrawl.tools.execution import BudgetedExecutor

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "astrbot_plugin_firecrawl"

KEY = "fc-site-submit-secret-key"
KEY_REF = key_reference(KEY)
URL = "https://example.com"
BATCH_URLS = ["https://a.example/1", "https://b.example/2"]
CRAWL_ID = "crawl-job-1"
BATCH_ID = "batch-job-1"
PDF_MAX_PAGES = 7

CONFIG = {
    "site_max_pages": 3,
    "max_content_chars": 8000,
    "site_admin_only": False,
    "pdf_max_pages": PDF_MAX_PAGES,
    "check_prompt_injection": True,
}

CRAWL_BODY = {
    "url": URL,
    "limit": 2,
    "allowExternalLinks": False,
    "scrapeOptions": {
        "formats": ["markdown"],
        "parsers": [{"type": "pdf", "maxPages": PDF_MAX_PAGES}],
        "proxy": "auto",
        "onlyMainContent": True,
        "waitFor": 0,
        "checkPromptInjection": True,
    },
}

BATCH_BODY = {
    "urls": BATCH_URLS,
    "formats": ["markdown"],
    "parsers": [{"type": "pdf", "maxPages": PDF_MAX_PAGES}],
    "proxy": "auto",
    "onlyMainContent": True,
    "waitFor": 0,
    "checkPromptInjection": True,
}

# 与请求体同一配置反推的估算，保证 estimate 与 payload 一致。
PER_PAGE = estimate_site_credits(
    "crawl", 1, pdf_max_pages=PDF_MAX_PAGES, check_prompt_injection=True
)
CRAWL_ESTIMATE = estimate_site_credits(
    "crawl",
    CRAWL_BODY["limit"],
    pdf_max_pages=PDF_MAX_PAGES,
    check_prompt_injection=True,
)
BATCH_ESTIMATE = estimate_site_credits(
    "batch",
    len(BATCH_BODY["urls"]),
    pdf_max_pages=PDF_MAX_PAGES,
    check_prompt_injection=True,
)

# 哨兵：区分响应缺省 invalidURLs 与显式 null。
_MISSING = object()


@dataclass
class _StubFunctionTool:
    """与 conftest 相同形状的 SDK 替身，避免依赖 sdk_tool_loader fixture。"""

    name: str
    description: str
    parameters: dict
    handler: object = None
    active: bool = True


@contextmanager
def _load_site_tool():
    """在独立 alias 中加载 tools/site.py，结束后精确还原被替换的 sys.modules 项。"""
    api_module = types.ModuleType("astrbot.api")
    api_module.FunctionTool = _StubFunctionTool
    api_module.logger = logging.getLogger("astrbot.stub")
    root_module = types.ModuleType("astrbot")
    root_module.api = api_module
    alias = f"{PACKAGE}.tools._sdk_site_submit"
    missing = object()
    saved = {
        name: sys.modules.get(name, missing)
        for name in ("astrbot", "astrbot.api", alias)
    }
    sys.modules["astrbot"] = root_module
    sys.modules["astrbot.api"] = api_module
    spec = spec_from_file_location(alias, ROOT / "tools" / "site.py")
    tool_module = module_from_spec(spec)
    sys.modules[alias] = tool_module
    try:
        spec.loader.exec_module(tool_module)
        yield tool_module.FirecrawlSiteTool
    finally:
        for name, original in saved.items():
            if original is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


@pytest.fixture()
def site_tool_class():
    with _load_site_tool() as tool_class:
        yield tool_class


class FakeRuntime:
    """最小插件替身：config + executor + 自带计数的 request_scope。"""

    def __init__(self, executor, config=None) -> None:
        self.config = config or {}
        self.executor = executor
        self.scope_entered = 0

    def request_scope(self):
        return self

    async def __aenter__(self):
        self.scope_entered += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


@asynccontextmanager
async def open_plugin(
    tmp_path, responses, config=None, *, key=KEY, policy=None, on_request=None
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

    client = FirecrawlClient(KeyPool([key]), transport=httpx.MockTransport(handler))
    runtime = FakeRuntime(BudgetedExecutor(client, store), config)
    runtime.seen, runtime.store = seen, store
    try:
        yield runtime
    finally:
        await client.aclose()


def assert_task_handle(handle, *, kind, job_id):
    """任务句柄必须编码任务 kind、firecrawl_id 与原 Key 的完整摘要。"""
    parsed = parse_task_handle(handle)
    assert parsed == {"kind": kind, "firecrawl_id": job_id, "key_ref": KEY_REF}


async def assert_accepted_not_settled(store, *, kind, job_id, estimate):
    """成功提交后单笔 accepted：原 Key 摘要、job_id、账目不提前结算。"""
    [reservation] = await store.list_pending()
    assert reservation.state is ReservationState.ACCEPTED
    assert reservation.kind == kind
    assert reservation.job_id == job_id
    assert reservation.key_ref == KEY_REF
    assert reservation.estimate == estimate
    assert reservation.actual_credits is None
    assert reservation.accounted_credits == 0.0
    snapshot = await store.usage()
    assert snapshot.used == 0.0
    assert snapshot.active_reserved == float(estimate)


async def assert_indeterminate_usage(store, reservation_id, estimate):
    """pending 后账目保留：单笔非终态、未入账、额度仍占用。"""
    [reservation] = await store.list_pending()
    assert reservation.state is ReservationState.INDETERMINATE
    assert reservation.reservation_id == reservation_id
    assert reservation.job_id is None
    assert reservation.estimate == estimate
    assert reservation.accounted_credits == 0.0
    snapshot = await store.usage()
    assert snapshot.used == 0.0
    assert snapshot.active_reserved == float(estimate)


@pytest.mark.asyncio
async def test_crawl_submit_body_estimate_and_accepted_handle(
    site_tool_class, tmp_path
):
    snapshots = []

    async def on_request(store):
        snapshots.append(await store.list_pending())

    response = {
        "success": True,
        "id": CRAWL_ID,
        "url": "https://api.firecrawl.dev/v2/crawl/crawl-job-1",
        "warning": "w",
        "creditsUsed": 0,
    }
    async with open_plugin(
        tmp_path, [response], config=CONFIG, on_request=on_request
    ) as runtime:
        out = await site_tool_class(plugin=runtime).call(
            None, action="crawl", url=URL, limit=2
        )
    payload = json.loads(out)

    assert payload["ok"] is True
    handle = payload["data"].pop("task_handle")
    assert payload["data"] == {
        "success": True,
        "warning": "w",
        "creditsUsed": 0,
        "id": CRAWL_ID,
        "url": "https://api.firecrawl.dev/v2/crawl/crawl-job-1",
    }
    assert_task_handle(handle, kind="crawl", job_id=CRAWL_ID)
    assert KEY not in out

    request = runtime.seen[0]
    assert request.method == "POST"
    assert request.url.path == "/v2/crawl"
    sent_body = json.loads(request.content)
    assert sent_body == CRAWL_BODY

    # 估算必须与实际请求体一致：按请求体反推并与发送时的预留对账。
    expected = estimate_site_credits(
        "crawl",
        sent_body["limit"],
        pdf_max_pages=sent_body["scrapeOptions"]["parsers"][0]["maxPages"],
        check_prompt_injection=sent_body["scrapeOptions"]["checkPromptInjection"],
    )
    [sent_reservation] = snapshots[0]
    assert sent_reservation.state is ReservationState.SUBMITTING
    assert sent_reservation.estimate == expected == CRAWL_ESTIMATE == 2 * PER_PAGE

    await assert_accepted_not_settled(
        runtime.store, kind="crawl", job_id=CRAWL_ID, estimate=CRAWL_ESTIMATE
    )
    assert len(runtime.seen) == 1
    assert runtime.scope_entered == 1


@pytest.mark.asyncio
async def test_batch_submit_body_no_limit_no_nested_options(site_tool_class, tmp_path):
    response = {"success": True, "id": BATCH_ID, "warning": "w", "creditsUsed": 0}
    async with open_plugin(tmp_path, [response], config=CONFIG) as runtime:
        out = await site_tool_class(plugin=runtime).call(
            None, action="batch", urls=BATCH_URLS
        )
    payload = json.loads(out)

    assert payload["ok"] is True
    handle = payload["data"].pop("task_handle")
    assert payload["data"] == {
        "success": True,
        "warning": "w",
        "creditsUsed": 0,
        "id": BATCH_ID,
    }
    assert_task_handle(handle, kind="batch", job_id=BATCH_ID)
    assert KEY not in out

    request = runtime.seen[0]
    assert request.method == "POST"
    assert request.url.path == "/v2/batch/scrape"
    sent_body = json.loads(request.content)
    assert sent_body == BATCH_BODY
    assert "limit" not in sent_body
    assert "scrapeOptions" not in sent_body

    expected = estimate_site_credits(
        "batch",
        len(sent_body["urls"]),
        pdf_max_pages=sent_body["parsers"][0]["maxPages"],
        check_prompt_injection=sent_body["checkPromptInjection"],
    )
    assert expected == BATCH_ESTIMATE
    await assert_accepted_not_settled(
        runtime.store, kind="batch", job_id=BATCH_ID, estimate=BATCH_ESTIMATE
    )
    assert len(runtime.seen) == 1
    assert runtime.scope_entered == 1


@pytest.mark.parametrize(
    ("invalid_urls", "expected"),
    [
        pytest.param(_MISSING, None, id="missing"),
        pytest.param(None, {"invalidURLs": None}, id="null"),
        pytest.param([], {"invalidURLs": []}, id="empty"),
        pytest.param(
            [
                "https://u1.example/a",
                "https://u2.example/b",
                "https://u3.example/c",
                "https://u4.example/d",
            ],
            {
                "invalidURLs": [
                    "https://u1.example/a",
                    "https://u2.example/b",
                    "https://u3.example/c",
                ],
                "invalidURLsTruncated": True,
            },
            id="truncated_to_site_max_pages",
        ),
    ],
)
@pytest.mark.asyncio
async def test_batch_invalid_urls_trim_variants(
    site_tool_class, tmp_path, invalid_urls, expected
):
    response = {"success": True, "id": BATCH_ID}
    if invalid_urls is not _MISSING:
        response["invalidURLs"] = invalid_urls
    async with open_plugin(tmp_path, [response], config=CONFIG) as runtime:
        out = await site_tool_class(plugin=runtime).call(
            None, action="batch", urls=BATCH_URLS
        )
    payload = json.loads(out)

    assert payload["ok"] is True
    data = payload["data"]
    if expected is None:
        assert "invalidURLs" not in data
    else:
        for field, value in expected.items():
            assert data[field] == value
    assert_task_handle(data["task_handle"], kind="batch", job_id=BATCH_ID)
    assert KEY not in out
    await assert_accepted_not_settled(
        runtime.store, kind="batch", job_id=BATCH_ID, estimate=BATCH_ESTIMATE
    )
    assert len(runtime.seen) == 1


@pytest.mark.parametrize(
    "response",
    [
        pytest.param({"success": True, "warning": "w"}, id="missing_id"),
        pytest.param({"success": True, "id": ["not-a-string"]}, id="id_not_string"),
        pytest.param(
            httpx.Response(200, content=b"<html>not json</html>"), id="invalid_json"
        ),
    ],
)
@pytest.mark.asyncio
async def test_batch_submit_shape_failure_pending_without_handle(
    site_tool_class, tmp_path, response
):
    async with open_plugin(tmp_path, [response], config=CONFIG) as runtime:
        out = await site_tool_class(plugin=runtime).call(
            None, action="batch", urls=BATCH_URLS
        )
    payload = json.loads(out)

    assert payload["ok"] is False
    assert payload["category"] == "indeterminate"
    assert payload["pending"] is True
    assert payload["retryable"] is False
    assert payload["reservation_id"]
    assert "task_handle" not in payload
    assert "结果不确定" in payload["error"]
    assert "请勿重发" in payload["hint"]
    assert KEY not in out
    assert len(runtime.seen) == 1

    await assert_indeterminate_usage(
        runtime.store, payload["reservation_id"], BATCH_ESTIMATE
    )


@pytest.mark.asyncio
async def test_batch_submit_bad_url_metadata_pending_with_handle(
    site_tool_class, tmp_path
):
    response = {"success": True, "id": BATCH_ID, "url": 123}
    async with open_plugin(tmp_path, [response], config=CONFIG) as runtime:
        out = await site_tool_class(plugin=runtime).call(
            None, action="batch", urls=BATCH_URLS
        )
    payload = json.loads(out)

    assert payload["ok"] is False
    assert payload["category"] == "indeterminate"
    assert payload["pending"] is True
    assert payload["retryable"] is False
    assert payload["reservation_id"]
    assert "任务已受理" in payload["error"]
    assert "请勿重发" in payload["hint"]
    assert_task_handle(payload["task_handle"], kind="batch", job_id=BATCH_ID)
    assert KEY not in out
    assert len(runtime.seen) == 1

    await assert_accepted_not_settled(
        runtime.store, kind="batch", job_id=BATCH_ID, estimate=BATCH_ESTIMATE
    )


@pytest.mark.parametrize(
    ("literal", "credits_used"),
    [("NaN", float("nan")), ("Infinity", float("inf"))],
    ids=["nan", "inf"],
)
@pytest.mark.asyncio
async def test_batch_submit_non_finite_credits_pending_with_handle(
    site_tool_class, tmp_path, literal, credits_used
):
    response = httpx.Response(
        200,
        content=f'{{"success":true,"id":"{BATCH_ID}","creditsUsed":{literal}}}'.encode(),
        headers={"Content-Type": "application/json"},
    )
    async with open_plugin(tmp_path, [response], config=CONFIG) as runtime:
        out = await site_tool_class(plugin=runtime).call(
            None, action="batch", urls=BATCH_URLS
        )
    payload = json.loads(out)

    assert payload["ok"] is False
    assert payload["category"] == "indeterminate"
    assert payload["pending"] is True
    assert payload["retryable"] is False
    assert payload["reservation_id"]
    assert "任务已受理" in payload["error"]
    assert "请勿重发" in payload["hint"]
    assert_task_handle(payload["task_handle"], kind="batch", job_id=BATCH_ID)
    assert KEY not in out
    assert len(runtime.seen) == 1

    await assert_accepted_not_settled(
        runtime.store, kind="batch", job_id=BATCH_ID, estimate=BATCH_ESTIMATE
    )


@pytest.mark.asyncio
async def test_crawl_budget_rejected_before_network(site_tool_class, tmp_path):
    policy = BudgetPolicy(daily_credit_limit=1)
    assert CRAWL_ESTIMATE > 1
    async with open_plugin(tmp_path, [], config=CONFIG, policy=policy) as runtime:
        out = await site_tool_class(plugin=runtime).call(
            None, action="crawl", url=URL, limit=2
        )
    payload = json.loads(out)

    assert payload["ok"] is False
    assert payload["category"] == "usage"
    assert "日预算不足" in payload["error"]
    assert payload["retryable"] is False
    assert payload["example"] == {"action": "map", "url": URL}
    assert KEY not in out
    assert runtime.seen == []
    assert await runtime.store.list_pending() == []
    snapshot = await runtime.store.usage()
    assert snapshot.used == 0.0
    assert snapshot.active_reserved == 0.0
