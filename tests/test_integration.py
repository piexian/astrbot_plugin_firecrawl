"""KeyPool + FirecrawlClient + SQLiteStore 离线集成回归。"""

from __future__ import annotations

import httpx
import pytest
from astrbot_plugin_firecrawl.firecrawl_api.budget import ReservationState
from astrbot_plugin_firecrawl.persistence import SQLiteStore

from firecrawl_api import ErrorCategory, FirecrawlClient, FirecrawlError, KeyPool

K1 = "fc-integration-key-1"
K2 = "fc-integration-key-2"
K3 = "fc-integration-key-3"
FP = "a" * 64


def make_store(tmp_path) -> SQLiteStore:
    """在 tmp_path 建立已初始化的预算存储。"""
    return SQLiteStore(tmp_path / "budget.db")


async def reserve_once(store: SQLiteStore):
    """用固定 ID 预留一笔预算，验证同一 ID 不会被重复创建。"""
    return await store.reserve(
        5, kind="scrape", request_fingerprint=FP, reservation_id="r-1"
    )


def sequential_handler(seen: list[httpx.Request], responses: list):
    """按顺序回放响应或异常，并记录收到的请求。"""
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return handler


@pytest.mark.asyncio
async def test_auth_rotation_until_success_then_settle_once(tmp_path):
    store = make_store(tmp_path)
    await store.initialize()
    await reserve_once(store)
    seen: list[httpx.Request] = []
    handler = sequential_handler(
        seen,
        [
            httpx.Response(401, json={"success": False, "error": "bad key"}),
            httpx.Response(402, json={"success": False, "error": "no credits"}),
            httpx.Response(200, json={"success": True, "data": {"ok": True}}),
        ],
    )
    client = FirecrawlClient(
        KeyPool([K1, K2, K3]), transport=httpx.MockTransport(handler)
    )
    response = await client.request(
        "POST", "/scrape", json={"url": "https://example.com"}
    )
    await client.aclose()
    assert response.status_code == 200
    assert [r.headers["authorization"] for r in seen] == [
        f"Bearer {K1}",
        f"Bearer {K2}",
        f"Bearer {K3}",
    ]
    with pytest.raises(ValueError, match="预留 ID 已存在"):
        await reserve_once(store)
    await store.mark_submitting("r-1", key_ref=FP)
    await store.accept("r-1", "job-1")
    settled = await store.settle("r-1", 2.5)
    assert settled.state is ReservationState.SETTLED
    snapshot = await store.usage()
    assert snapshot.used == 2.5
    assert snapshot.active_reserved == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "category"),
    [
        (
            httpx.Response(500, json={"success": False, "error": "boom"}),
            ErrorCategory.SERVER,
        ),
        (httpx.ReadTimeout("too slow"), ErrorCategory.TIMEOUT),
        (httpx.ConnectError("refused"), ErrorCategory.NETWORK),
    ],
)
async def test_single_failure_keeps_reservation_pending(tmp_path, failure, category):
    store = make_store(tmp_path)
    await store.initialize()
    await reserve_once(store)
    seen: list[httpx.Request] = []
    handler = sequential_handler(seen, [failure])
    client = FirecrawlClient(KeyPool([K1]), transport=httpx.MockTransport(handler))
    with pytest.raises(FirecrawlError) as exc_info:
        await client.request("POST", "/scrape", json={"url": "https://example.com"})
    await client.aclose()
    assert exc_info.value.category is category
    assert len(seen) == 1
    record = await store.get("r-1")
    assert record.state is ReservationState.RESERVED
    snapshot = await store.usage()
    assert snapshot.active_reserved == 5.0
    assert snapshot.used == 0.0


@pytest.mark.asyncio
async def test_anonymous_scrape_and_search_without_authorization():
    seen: list[httpx.Request] = []
    handler = sequential_handler(
        seen,
        [
            httpx.Response(200, json={"success": True}),
            httpx.Response(200, json={"success": True}),
        ],
    )
    client = FirecrawlClient(KeyPool([]), transport=httpx.MockTransport(handler))
    await client.request("POST", "/scrape", json={"url": "https://example.com"})
    await client.request("POST", "/search", json={"query": "firecrawl"})
    await client.aclose()
    assert len(seen) == 2
    assert all("authorization" not in r.headers for r in seen)
