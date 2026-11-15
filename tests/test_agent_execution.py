"""tools.execution agent 提交测试：/agent 路由、after_reserve 回调与任务句柄对账。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from astrbot_plugin_firecrawl.firecrawl_api.budget import (
    BudgetPolicy,
    ReservationState,
)
from astrbot_plugin_firecrawl.firecrawl_api.client import FirecrawlClient
from astrbot_plugin_firecrawl.firecrawl_api.keys import KeyPool, key_reference
from astrbot_plugin_firecrawl.firecrawl_api.tasks import (
    make_task_handle,
    parse_task_handle,
)
from astrbot_plugin_firecrawl.persistence import SQLiteStore
from astrbot_plugin_firecrawl.tools.execution import (
    BudgetedExecutor,
    PendingRequestError,
)

K1 = "fc-aaaaaaaa1111"
K2 = "fc-bbbbbbbb2222"
K3 = "fc-cccccccc3333"
STAMP = "2026-01-01T12:00:00+00:00"
TASK_ID = "agent-task_1"
AGENT_URL = "https://api.firecrawl.dev/v2/agent"
AGENT_TASK_URL = f"{AGENT_URL}/{TASK_ID}"
PAYLOAD = {"prompt": "帮我调研 X", "maxCredits": 30}


def fixed_clock() -> datetime:
    """固定 UTC 时刻，保证日切与用量断言稳定。"""
    return datetime.fromisoformat(STAMP)


def make_transport(
    responses: list, on_request=None
) -> tuple[httpx.MockTransport, list]:
    """按顺序回放响应或抛出异常，并记录收到的请求。"""
    requests: list[httpx.Request] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if on_request is not None:
            on_request(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.MockTransport(handler), requests


async def make_executor(
    tmp_path: Path,
    responses: list,
    keys: tuple = (K1,),
    on_request=None,
) -> tuple[BudgetedExecutor, SQLiteStore, list]:
    """创建带真实 SQLiteStore 与 MockTransport 的执行器。"""
    transport, requests = make_transport(responses, on_request)
    client = FirecrawlClient(KeyPool(list(keys)), transport=transport)
    store = SQLiteStore(
        tmp_path / "budget.db", policy=BudgetPolicy(), clock=fixed_clock
    )
    await store.initialize()
    return BudgetedExecutor(client, store), store, requests


def identity_rows(db_path: Path) -> list[tuple]:
    """读取全部预留身份字段：预留 ID、状态、Key 摘要与任务 ID。"""
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT reservation_id, state, key_ref, job_id FROM reservations ORDER BY rowid"
        ).fetchall()
    finally:
        conn.close()


def block_release_trigger(db_path: Path) -> None:
    """安装 BEFORE UPDATE 触发器，使预留释放写库中断。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TRIGGER block_release BEFORE UPDATE ON reservations"
            " BEGIN SELECT RAISE(ABORT, 'blocked'); END;"
        )
        conn.commit()
    finally:
        conn.close()


def agent_handle(key: str = K1) -> str:
    """构造指向指定 Key 的合法 agent 任务句柄。"""
    return make_task_handle("agent", TASK_ID, key_reference(key))


def submit_body(**overrides) -> dict:
    body = {"success": True, "id": TASK_ID}
    body.update(overrides)
    return body


async def submit_agent(executor: BudgetedExecutor):
    """建立一笔 accepted 的 agent 任务预留并返回。"""
    _, reservation = await executor.submit("agent", PAYLOAD, estimate=30)
    return reservation


@pytest.mark.asyncio
async def test_agent_submit_routes_exact_endpoint_and_payload(tmp_path):
    executor, store, requests = await make_executor(
        tmp_path, [httpx.Response(200, json=submit_body())]
    )
    response, reservation = await executor.submit("agent", PAYLOAD, estimate=30)

    assert str(requests[0].url) == AGENT_URL
    assert requests[0].method == "POST"
    assert json.loads(requests[0].content) == PAYLOAD
    assert requests[0].headers["authorization"] == f"Bearer {K1}"
    assert response.json() == submit_body()
    assert reservation.state is ReservationState.ACCEPTED
    assert reservation.kind == "agent"
    assert reservation.job_id == TASK_ID
    assert reservation.estimate == 30
    assert reservation.actual_credits is None
    assert reservation.accounted_credits == 0.0
    assert identity_rows(tmp_path / "budget.db") == [
        (reservation.reservation_id, "accepted", key_reference(K1), TASK_ID)
    ]
    snapshot = await store.usage()
    assert snapshot.used == 0.0
    assert snapshot.active_reserved == 30.0


@pytest.mark.asyncio
async def test_agent_submit_task_handle_matches_reservation(tmp_path):
    executor, _, _ = await make_executor(
        tmp_path, [httpx.Response(200, json=submit_body())]
    )
    _, reservation = await executor.submit("agent", PAYLOAD, estimate=30)

    handle = make_task_handle("agent", reservation.job_id, reservation.key_ref)
    assert handle == agent_handle()
    assert parse_task_handle(handle) == {
        "kind": "agent",
        "firecrawl_id": TASK_ID,
        "key_ref": key_reference(K1),
    }


@pytest.mark.asyncio
async def test_after_reserve_runs_once_before_key_and_network(tmp_path):
    events: list[str] = []
    executor, store, requests = await make_executor(
        tmp_path,
        [httpx.Response(200, json=submit_body())],
        on_request=lambda request: events.append("network"),
    )

    async def after_reserve(reservation):
        assert requests == []
        record = await store.get(reservation.reservation_id)
        events.append(f"after_reserve:{reservation.state.value}:{record.state.value}")

    _, reservation = await executor.submit(
        "agent", PAYLOAD, estimate=30, after_reserve=after_reserve
    )
    assert events == ["after_reserve:reserved:reserved", "network"]
    assert reservation.state is ReservationState.ACCEPTED
    assert identity_rows(tmp_path / "budget.db")[0][1] == "accepted"


@pytest.mark.asyncio
async def test_after_reserve_failure_releases_without_network(tmp_path):
    executor, store, requests = await make_executor(
        tmp_path, [httpx.Response(200, json=submit_body())]
    )

    async def failing(reservation):
        raise RuntimeError("回调失败")

    with pytest.raises(RuntimeError):
        await executor.submit("agent", PAYLOAD, estimate=30, after_reserve=failing)

    assert requests == []
    assert identity_rows(tmp_path / "budget.db")[0][1] == "released"
    snapshot = await store.usage()
    assert snapshot.used == 0.0
    assert snapshot.active_reserved == 0.0


@pytest.mark.asyncio
async def test_after_reserve_cancelled_releases_without_network(tmp_path):
    executor, store, requests = await make_executor(
        tmp_path, [httpx.Response(200, json=submit_body())]
    )

    async def cancelling(reservation):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await executor.submit("agent", PAYLOAD, estimate=30, after_reserve=cancelling)

    assert requests == []
    assert identity_rows(tmp_path / "budget.db")[0][1] == "released"
    snapshot = await store.usage()
    assert snapshot.used == 0.0
    assert snapshot.active_reserved == 0.0


@pytest.mark.asyncio
async def test_after_reserve_failure_with_release_error_propagates_plain(tmp_path):
    executor, store, requests = await make_executor(
        tmp_path, [httpx.Response(200, json=submit_body())]
    )
    block_release_trigger(tmp_path / "budget.db")
    seen: list[str] = []

    async def failing(reservation):
        seen.append(reservation.reservation_id)
        raise RuntimeError("回调失败")

    with pytest.raises(sqlite3.IntegrityError) as excinfo:
        await executor.submit("agent", PAYLOAD, estimate=30, after_reserve=failing)

    assert seen
    assert not isinstance(excinfo.value, PendingRequestError)
    assert requests == []
    record = await store.get(seen[0])
    assert record.state is ReservationState.RESERVED


@pytest.mark.asyncio
async def test_submit_rotates_keys_with_single_reservation_and_callback(tmp_path):
    executor, _, requests = await make_executor(
        tmp_path,
        [
            httpx.Response(402, json={"success": False, "error": "quota"}),
            httpx.Response(200, json=submit_body()),
        ],
        keys=(K1, K2, K3),
    )
    calls: list[str] = []

    async def after_reserve(reservation):
        calls.append(reservation.reservation_id)

    _, reservation = await executor.submit(
        "agent", PAYLOAD, estimate=30, after_reserve=after_reserve
    )

    assert len(requests) == 2
    assert requests[0].headers["authorization"] == f"Bearer {K1}"
    assert requests[1].headers["authorization"] == f"Bearer {K2}"
    assert calls == [reservation.reservation_id]
    assert identity_rows(tmp_path / "budget.db") == [
        (reservation.reservation_id, "accepted", key_reference(K2), TASK_ID)
    ]


@pytest.mark.asyncio
async def test_status_processing_returns_without_settlement(tmp_path):
    status_body = {"success": True, "status": "processing"}
    executor, store, requests = await make_executor(
        tmp_path,
        [
            httpx.Response(200, json=submit_body()),
            httpx.Response(200, json=status_body),
        ],
    )
    reservation = await submit_agent(executor)
    response = await executor.status(agent_handle())

    assert response.json() == status_body
    assert len(requests) == 2
    assert str(requests[1].url) == AGENT_TASK_URL
    assert requests[1].method == "GET"
    assert requests[1].headers["authorization"] == f"Bearer {K1}"
    record = await store.get(reservation.reservation_id)
    assert record.state is ReservationState.ACCEPTED
    assert record.actual_credits is None
    assert record.accounted_credits == 0.0
    snapshot = await store.usage()
    assert snapshot.used == 0.0
    assert snapshot.active_reserved == 30.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_status", "credits"),
    [
        ("completed", 0),
        ("completed", 2.5),
        ("completed", 40),
        ("failed", 4),
        ("cancelled", 4.5),
    ],
)
async def test_status_terminal_settles_reported_credits(tmp_path, task_status, credits):
    status_body = {"success": True, "status": task_status, "creditsUsed": credits}
    executor, store, requests = await make_executor(
        tmp_path,
        [
            httpx.Response(200, json=submit_body()),
            httpx.Response(200, json=status_body),
        ],
    )
    reservation = await submit_agent(executor)
    response = await executor.status(agent_handle())

    assert response.json() == status_body
    assert len(requests) == 2
    assert str(requests[1].url) == AGENT_TASK_URL
    assert requests[1].method == "GET"
    record = await store.get(reservation.reservation_id)
    assert record.state is ReservationState.SETTLED
    assert record.actual_credits == record.accounted_credits == float(credits)
    snapshot = await store.usage()
    assert snapshot.used == float(credits)
    assert snapshot.active_reserved == 0.0


@pytest.mark.asyncio
async def test_status_terminal_without_credits_stays_accepted_pending(tmp_path):
    status_body = {"success": True, "status": "completed", "data": {"answer": "x"}}
    executor, store, requests = await make_executor(
        tmp_path,
        [
            httpx.Response(200, json=submit_body()),
            httpx.Response(200, json=status_body),
        ],
    )
    reservation = await submit_agent(executor)
    with pytest.raises(PendingRequestError) as excinfo:
        await executor.status(agent_handle())

    assert len(requests) == 2
    assert excinfo.value.reservation_id == reservation.reservation_id
    assert excinfo.value.task_handle == agent_handle()
    record = await store.get(reservation.reservation_id)
    assert record.state is ReservationState.ACCEPTED
    assert record.accounted_credits == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize("credits", ["4", -1, True, None])
async def test_status_bad_credits_stays_accepted_pending(tmp_path, credits):
    status_body = {"success": True, "status": "completed", "creditsUsed": credits}
    executor, store, requests = await make_executor(
        tmp_path,
        [
            httpx.Response(200, json=submit_body()),
            httpx.Response(200, json=status_body),
        ],
    )
    reservation = await submit_agent(executor)
    with pytest.raises(PendingRequestError) as excinfo:
        await executor.status(agent_handle())

    assert len(requests) == 2
    assert excinfo.value.reservation_id == reservation.reservation_id
    assert excinfo.value.task_handle == agent_handle()
    record = await store.get(reservation.reservation_id)
    assert record.state is ReservationState.ACCEPTED
    assert record.accounted_credits == 0.0
    snapshot = await store.usage()
    assert snapshot.used == 0.0
    assert snapshot.active_reserved == 30.0


@pytest.mark.asyncio
async def test_status_ignores_crawl_reservation_with_same_job_id(tmp_path):
    status_body = {"success": True, "status": "completed", "creditsUsed": 4}
    executor, store, requests = await make_executor(
        tmp_path, [httpx.Response(200, json=status_body)]
    )
    reserved = await store.reserve(3, kind="crawl", request_fingerprint="a" * 64)
    await store.mark_submitting(
        reserved.reservation_id, key_reference(K1), confirmed_not_accepted=True
    )
    await store.accept(reserved.reservation_id, job_id=TASK_ID)

    response = await executor.status(agent_handle())

    assert response.json() == status_body
    assert len(requests) == 1
    record = await store.get(reserved.reservation_id)
    assert record.state is ReservationState.ACCEPTED
    assert record.accounted_credits == 0.0
    snapshot = await store.usage()
    assert snapshot.active_reserved == 3.0


@pytest.mark.asyncio
async def test_status_duplicate_match_rejected_without_ids(tmp_path):
    executor, _, requests = await make_executor(
        tmp_path,
        [
            httpx.Response(200, json=submit_body()),
            httpx.Response(200, json=submit_body()),
            httpx.Response(
                200, json={"success": True, "status": "completed", "creditsUsed": 4}
            ),
        ],
    )
    _, first = await executor.submit("agent", PAYLOAD, estimate=30)
    _, second = await executor.submit("agent", PAYLOAD, estimate=30)

    with pytest.raises(ValueError) as excinfo:
        await executor.status(agent_handle())

    assert str(excinfo.value) == "任务句柄匹配到多笔待决预留，拒绝结算"
    assert first.reservation_id not in str(excinfo.value)
    assert second.reservation_id not in str(excinfo.value)
    assert len(requests) == 3


@pytest.mark.asyncio
async def test_cancel_deletes_with_original_key_and_does_not_settle(tmp_path):
    cancel_body = {"success": True, "status": "cancelled"}
    executor, store, requests = await make_executor(
        tmp_path,
        [
            httpx.Response(200, json=submit_body()),
            httpx.Response(200, json=cancel_body),
        ],
    )
    reservation = await submit_agent(executor)
    response = await executor.cancel(agent_handle())

    assert response.json() == cancel_body
    assert len(requests) == 2
    assert str(requests[1].url) == AGENT_TASK_URL
    assert requests[1].method == "DELETE"
    assert requests[1].headers["authorization"] == f"Bearer {K1}"
    record = await store.get(reservation.reservation_id)
    assert record.state is ReservationState.ACCEPTED
    assert record.accounted_credits == 0.0
    snapshot = await store.usage()
    assert snapshot.used == 0.0
    assert snapshot.active_reserved == 30.0


@pytest.mark.asyncio
async def test_task_operations_use_pinned_key_from_handle(tmp_path):
    executor, _, requests = await make_executor(
        tmp_path, [httpx.Response(200, json={"success": True})], keys=(K1, K2)
    )
    await executor.cancel(agent_handle(K2))

    assert len(requests) == 1
    assert str(requests[0].url) == AGENT_TASK_URL
    assert requests[0].method == "DELETE"
    assert requests[0].headers["authorization"] == f"Bearer {K2}"
