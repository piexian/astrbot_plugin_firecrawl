"""firecrawl_site status/cancel 任务对账回归：原 Key 直连、终态一次结算与非终态不动账。"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx
import pytest
from astrbot_plugin_firecrawl.firecrawl_api.budget import BudgetPolicy, ReservationState
from astrbot_plugin_firecrawl.firecrawl_api.client import FirecrawlClient
from astrbot_plugin_firecrawl.firecrawl_api.keys import KeyPool, key_reference
from astrbot_plugin_firecrawl.firecrawl_api.site import (
    make_task_handle,
    parse_task_handle,
)
from astrbot_plugin_firecrawl.persistence import SQLiteStore
from astrbot_plugin_firecrawl.tools.execution import BudgetedExecutor

KEY = "fc-task-accounting-secret-key"
KEY_REF = key_reference(KEY)
TASK_ID = "task-job-1"
FINGERPRINT = "a" * 64
ESTIMATE = 5
EVIL_NEXT = "http://evil.example/steal?token=SECRET"

# site_admin_only 保持 True：status/cancel 不应受管理员限制。
CONFIG = {
    "site_max_pages": 3,
    "max_content_chars": 8000,
    "site_admin_only": True,
}


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
async def accepted_task_plugin(tmp_path, responses):
    """真实组装 + 预置一笔 accepted 的 crawl 预留与完整任务句柄。"""
    store = SQLiteStore(tmp_path / "budget.db", policy=BudgetPolicy())
    await store.initialize()
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = responses.pop(0)
        return httpx.Response(200, json=item) if isinstance(item, dict) else item

    client = FirecrawlClient(KeyPool([KEY]), transport=httpx.MockTransport(handler))
    runtime = FakeRuntime(BudgetedExecutor(client, store), CONFIG)
    runtime.seen, runtime.store = seen, store

    reservation = await store.reserve(
        ESTIMATE, kind="crawl", request_fingerprint=FINGERPRINT
    )
    await store.mark_submitting(reservation.reservation_id, KEY_REF)
    accepted = await store.accept(reservation.reservation_id, job_id=TASK_ID)
    handle = make_task_handle("crawl", TASK_ID, KEY_REF)
    try:
        yield runtime, accepted, handle
    finally:
        await client.aclose()


async def assert_accepted_not_settled(store) -> None:
    """单笔 accepted：原 Key 摘要与 job_id 绑定，账目不结算、额度仍占用。"""
    [reservation] = await store.list_pending()
    assert reservation.state is ReservationState.ACCEPTED
    assert reservation.kind == "crawl"
    assert reservation.job_id == TASK_ID
    assert reservation.key_ref == KEY_REF
    assert reservation.estimate == ESTIMATE
    assert reservation.actual_credits is None
    assert reservation.accounted_credits == 0.0
    snapshot = await store.usage()
    assert snapshot.used == 0.0
    assert snapshot.active_reserved == float(ESTIMATE)


def assert_task_request(request: httpx.Request, method: str) -> None:
    """任务请求必须固定方法、路径与原 Key Bearer。"""
    assert request.method == method
    assert request.url.path == f"/v2/crawl/{TASK_ID}"
    assert request.headers["authorization"] == f"Bearer {KEY}"


@pytest.mark.asyncio
async def test_status_completed_settles_actual_credits_once(
    firecrawl_site_tool, tmp_path
):
    body = {"success": True, "status": "completed", "creditsUsed": 2}
    async with accepted_task_plugin(tmp_path, [body]) as (runtime, accepted, handle):
        out = await firecrawl_site_tool(plugin=runtime).call(
            None, action="status", job_id=handle
        )
    payload = json.loads(out)

    assert parse_task_handle(handle) == {
        "kind": "crawl",
        "firecrawl_id": TASK_ID,
        "key_ref": KEY_REF,
    }
    assert payload["ok"] is True
    assert payload["data"] == {
        "success": True,
        "status": "completed",
        "creditsUsed": 2,
        "pages": [],
    }
    assert KEY not in out
    assert runtime.scope_entered == 1

    [request] = runtime.seen
    assert_task_request(request, "GET")

    settled = await runtime.store.get(accepted.reservation_id)
    assert settled.state is ReservationState.SETTLED
    assert settled.actual_credits == 2.0
    assert settled.accounted_credits == 2.0
    snapshot = await runtime.store.usage()
    assert snapshot.used == 2.0
    assert snapshot.active_reserved == 0.0
    assert await runtime.store.list_pending() == []


@pytest.mark.asyncio
async def test_status_scraping_malicious_next_not_followed_not_settled(
    firecrawl_site_tool, tmp_path
):
    body = {"success": True, "status": "scraping", "next": EVIL_NEXT}
    async with accepted_task_plugin(tmp_path, [body]) as (runtime, accepted, handle):
        out = await firecrawl_site_tool(plugin=runtime).call(
            None, action="status", job_id=handle
        )
    payload = json.loads(out)

    assert payload["ok"] is True
    assert payload["data"]["status"] == "scraping"
    assert payload["data"]["next"] == EVIL_NEXT
    assert payload["data"]["hasNext"] is True
    assert KEY not in out
    assert runtime.scope_entered == 1

    # next 未被跟随：仅一次 GET，账目保持 accepted 不结算。
    assert len(runtime.seen) == 1
    assert_task_request(runtime.seen[0], "GET")
    await assert_accepted_not_settled(runtime.store)


@pytest.mark.asyncio
async def test_cancel_never_settles_even_with_credits_used(
    firecrawl_site_tool, tmp_path
):
    body = {"success": True, "status": "cancelled", "creditsUsed": 999}
    async with accepted_task_plugin(tmp_path, [body]) as (runtime, accepted, handle):
        out = await firecrawl_site_tool(plugin=runtime).call(
            None, action="cancel", job_id=handle
        )
    payload = json.loads(out)

    assert payload["ok"] is True
    assert payload["data"] == {
        "success": True,
        "status": "cancelled",
        "creditsUsed": 999,
    }
    assert KEY not in out
    assert runtime.scope_entered == 1

    [request] = runtime.seen
    assert_task_request(request, "DELETE")

    await assert_accepted_not_settled(runtime.store)


@pytest.mark.parametrize("operation", ["status", "cancel"])
@pytest.mark.asyncio
async def test_task_bad_shape_body_fails_without_new_reservation(
    firecrawl_site_tool, tmp_path, operation
):
    bad_body = httpx.Response(200, json=[1, 2, 3])
    async with accepted_task_plugin(tmp_path, [bad_body]) as (
        runtime,
        accepted,
        handle,
    ):
        out = await firecrawl_site_tool(plugin=runtime).call(
            None, action=operation, job_id=handle
        )
    payload = json.loads(out)

    assert payload["ok"] is False
    assert payload["category"] == "usage"
    assert payload["error"]
    assert KEY not in out
    assert KEY_REF not in out

    # 仅一次请求，预留未被结算也未新增。
    assert len(runtime.seen) == 1
    [reservation] = await runtime.store.list_pending()
    assert reservation.reservation_id == accepted.reservation_id
    assert reservation.state is ReservationState.ACCEPTED
    assert reservation.accounted_credits == 0.0


@pytest.mark.parametrize("operation", ["status", "cancel"])
@pytest.mark.asyncio
async def test_task_removed_key_rejected_without_anonymous_request(
    firecrawl_site_tool, tmp_path, operation
):
    body = {"success": True, "status": "completed", "creditsUsed": 2}
    async with accepted_task_plugin(tmp_path, [body]) as (runtime, accepted, handle):
        # 模拟原 Key 已从池中移除：句柄摘要无法解析时必须拒绝，不得匿名发送。
        runtime.executor._client._pool = KeyPool([])
        out = await firecrawl_site_tool(plugin=runtime).call(
            None, action=operation, job_id=handle
        )
    payload = json.loads(out)

    assert payload["ok"] is False
    assert payload["category"] == "config"
    assert KEY not in out
    assert KEY_REF not in out

    assert runtime.seen == []
    [reservation] = await runtime.store.list_pending()
    assert reservation.reservation_id == accepted.reservation_id
    assert reservation.state is ReservationState.ACCEPTED
    assert reservation.accounted_credits == 0.0
    snapshot = await runtime.store.usage()
    assert snapshot.used == 0.0
    assert snapshot.active_reserved == float(ESTIMATE)


@pytest.mark.asyncio
async def test_status_terminal_repeat_calls_settle_exactly_once(
    firecrawl_site_tool, tmp_path
):
    body = {"success": True, "status": "completed", "creditsUsed": 2}
    async with accepted_task_plugin(tmp_path, [body, body]) as (
        runtime,
        accepted,
        handle,
    ):
        tool = firecrawl_site_tool(plugin=runtime)
        first = await tool.call(None, action="status", job_id=handle)
        second = await tool.call(None, action="status", job_id=handle)
    assert json.loads(first)["ok"] is True
    assert json.loads(second)["ok"] is True

    # 终态重复查询各请求一次，但只结算一次。
    assert len(runtime.seen) == 2
    for request in runtime.seen:
        assert_task_request(request, "GET")
    assert runtime.scope_entered == 2

    settled = await runtime.store.get(accepted.reservation_id)
    assert settled.state is ReservationState.SETTLED
    assert settled.actual_credits == 2.0
    assert settled.accounted_credits == 2.0
    snapshot = await runtime.store.usage()
    assert snapshot.used == 2.0
    assert snapshot.active_reserved == 0.0
    assert await runtime.store.list_pending() == []
