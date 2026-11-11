"""tools.execution BudgetedExecutor 测试：预留、发送前落账、结算、任务终态费用对账与失败收敛。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from astrbot_plugin_firecrawl.firecrawl_api.budget import (
    BudgetExceeded,
    BudgetPolicy,
    ReservationState,
)
from astrbot_plugin_firecrawl.firecrawl_api.client import (
    KEY_REF_EXTENSION,
    FirecrawlClient,
)
from astrbot_plugin_firecrawl.firecrawl_api.errors import ErrorCategory, FirecrawlError
from astrbot_plugin_firecrawl.firecrawl_api.keys import KeyPool, key_reference
from astrbot_plugin_firecrawl.firecrawl_api.site import (
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

TASK_ID = "task-abc_123"

SUBMIT_URLS = {
    "crawl": "https://api.firecrawl.dev/v2/crawl",
    "batch": "https://api.firecrawl.dev/v2/batch/scrape",
}
BLOCK_UPDATE_TRIGGER = (
    "CREATE TRIGGER block_update BEFORE UPDATE ON reservations"
    " BEGIN SELECT RAISE(ABORT, 'blocked by trigger'); END;"
)
BLOCK_ACCEPT_TRIGGER = (
    "CREATE TRIGGER block_accept BEFORE UPDATE OF state ON reservations"
    " WHEN NEW.state = 'accepted' BEGIN SELECT RAISE(ABORT, 'blocked'); END;"
)
BLOCK_SETTLE_TRIGGER = (
    "CREATE TRIGGER block_settle BEFORE UPDATE OF state ON reservations"
    " WHEN NEW.state = 'settled' BEGIN SELECT RAISE(ABORT, 'blocked'); END;"
)
BLOCK_KEY_REF_TRIGGER = (
    "CREATE TRIGGER block_key_ref BEFORE UPDATE ON reservations"
    " WHEN NEW.key_ref = '{ref}'"
    " BEGIN SELECT RAISE(ABORT, 'blocked'); END;"
)


def fixed_clock() -> datetime:
    """固定 UTC 时刻，保证日切断言稳定。"""
    return datetime.fromisoformat(STAMP)


def canonical_fingerprint(kind: str, endpoint: str, payload: dict) -> str:
    """按预算记录约定复算规范 JSON 指纹。"""
    canonical = json.dumps(
        {"kind": kind, "method": "POST", "endpoint": endpoint, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
    policy: BudgetPolicy | None = None,
    on_request=None,
) -> tuple[BudgetedExecutor, SQLiteStore, list]:
    """创建带真实 SQLiteStore 与 MockTransport 的执行器。"""
    transport, requests = make_transport(responses, on_request)
    client = FirecrawlClient(KeyPool(list(keys)), transport=transport)
    store = SQLiteStore(
        tmp_path / "budget.db", policy=policy or BudgetPolicy(), clock=fixed_clock
    )
    await store.initialize()
    return BudgetedExecutor(client, store), store, requests


def rows(db_path: Path) -> list[tuple]:
    """读取全部预留记录行。"""
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT reservation_id, state, key_ref, actual_credits,"
            " accounted_credits, request_fingerprint FROM reservations ORDER BY rowid"
        ).fetchall()
    finally:
        conn.close()


def used(db_path: Path) -> float:
    """读取当日已用量，无记录返回 0。"""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT used FROM daily_usage").fetchone()
    finally:
        conn.close()
    return row[0] if row else 0.0


def add_trigger(db_path: Path, sql: str) -> None:
    """在真实数据库上安装触发器以中断写库。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(sql)
        conn.commit()
    finally:
        conn.close()


def drop_trigger(db_path: Path, name: str) -> None:
    """移除指定触发器以恢复写库。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        conn.commit()
    finally:
        conn.close()


def identity_rows(db_path: Path) -> list[tuple]:
    """读取全部预留身份字段：预留 ID、状态、Key 摘要与任务 ID。"""
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT reservation_id, state, key_ref, job_id FROM reservations ORDER BY rowid"
        ).fetchall()
    finally:
        conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["crawl", "batch", "status", ""])
async def test_unknown_kind_fails_before_reserve_and_network(tmp_path, kind):
    executor, _, requests = await make_executor(tmp_path, [])
    with pytest.raises(ValueError):
        await executor.execute_sync(kind, {"query": "q"}, estimate=1)
    assert requests == []
    assert rows(tmp_path / "budget.db") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [float("nan"), object()])
async def test_unserializable_payload_creates_no_reservation(tmp_path, bad):
    executor, _, requests = await make_executor(tmp_path, [])
    with pytest.raises((ValueError, TypeError)):
        await executor.execute_sync("search", {"query": bad}, estimate=1)
    assert requests == []
    assert rows(tmp_path / "budget.db") == []


@pytest.mark.asyncio
async def test_unknown_kind_error_does_not_echo_input(tmp_path):
    """未知 kind 报固定错误：不回显输入、不写库、不发网络。"""
    executor, _, requests = await make_executor(tmp_path, [])
    kind = "crawl fc-FAKE-SECRET-MARKER"
    with pytest.raises(ValueError) as excinfo:
        await executor.execute_sync(kind, {"query": "q"}, estimate=1)
    assert "FAKE-SECRET-MARKER" not in str(excinfo.value)
    assert "crawl" not in str(excinfo.value)
    assert requests == []
    assert rows(tmp_path / "budget.db") == []


@pytest.mark.asyncio
async def test_budget_exceeded_creates_no_reservation_and_no_network(tmp_path):
    policy = BudgetPolicy(daily_credit_limit=5)
    executor, _, requests = await make_executor(tmp_path, [], policy=policy)
    with pytest.raises(BudgetExceeded):
        await executor.execute_sync("map", {"url": "https://example.com"}, estimate=10)
    assert requests == []
    assert rows(tmp_path / "budget.db") == []


@pytest.mark.asyncio
async def test_mark_submitting_records_key_ref_before_send(tmp_path):
    observed = []

    def peek(_request: httpx.Request) -> None:
        conn = sqlite3.connect(tmp_path / "budget.db")
        try:
            observed.append(
                conn.execute("SELECT state, key_ref FROM reservations").fetchone()
            )
        finally:
            conn.close()

    body = {"success": True, "creditsUsed": 1}
    executor, _, requests = await make_executor(
        tmp_path, [httpx.Response(200, json=body)], on_request=peek
    )
    response = await executor.execute_sync(
        "scrape", {"url": "https://example.com"}, estimate=1
    )
    assert observed == [("submitting", key_reference(K1))]
    assert str(requests[0].url) == "https://api.firecrawl.dev/v2/scrape"
    assert requests[0].method == "POST"
    assert requests[0].headers["authorization"] == f"Bearer {K1}"
    assert response.extensions[KEY_REF_EXTENSION] == key_reference(K1)
    assert response.json() == body
    _, state, key_ref, actual, accounted, _ = rows(tmp_path / "budget.db")[0]
    assert state == "settled"
    assert key_ref == key_reference(K1)
    assert actual == accounted == 1.0
    assert used(tmp_path / "budget.db") == 1.0


@pytest.mark.asyncio
async def test_rotation_keeps_single_reservation(tmp_path):
    responses = [
        httpx.Response(401, json={"success": False, "error": "Unauthorized"}),
        httpx.Response(402, json={"success": False, "error": "quota"}),
        httpx.Response(200, json={"success": True, "creditsUsed": 2}),
    ]
    executor, _, requests = await make_executor(tmp_path, responses, keys=(K1, K2, K3))
    response = await executor.execute_sync(
        "search", {"query": "q", "limit": 5}, estimate=4
    )
    assert response.status_code == 200
    assert len(requests) == 3
    table = rows(tmp_path / "budget.db")
    assert len(table) == 1
    _, state, _, actual, accounted, _ = table[0]
    assert state == "settled"
    assert actual == accounted == 2.0
    assert used(tmp_path / "budget.db") == 2.0


@pytest.mark.asyncio
async def test_fingerprint_is_canonical_and_order_stable(tmp_path):
    executor, _, _ = await make_executor(
        tmp_path, [httpx.Response(200, json={"success": True}) for _ in range(3)]
    )
    await executor.execute_sync("search", {"query": "q", "limit": 5}, estimate=2)
    await executor.execute_sync("search", {"limit": 5, "query": "q"}, estimate=2)
    await executor.execute_sync("map", {"url": "https://example.com"}, estimate=1)
    table = rows(tmp_path / "budget.db")
    search = canonical_fingerprint("search", "/search", {"query": "q", "limit": 5})
    mapping = canonical_fingerprint("map", "/map", {"url": "https://example.com"})
    assert table[0][5] == table[1][5] == search
    assert table[2][5] == mapping
    assert table[0][5] != table[2][5]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"success": True, "creditsUsed": 0}, 0.0),
        ({"success": True, "creditsUsed": 0.5}, 0.5),
        ({"success": True, "creditsUsed": 25}, 25.0),
        ({"success": True, "data": {"creditsUsed": 99}}, 1.0),
    ],
)
async def test_credits_settlement_variants(tmp_path, body, expected):
    executor, _, _ = await make_executor(tmp_path, [httpx.Response(200, json=body)])
    response = await executor.execute_sync(
        "scrape", {"url": "https://example.com"}, estimate=1
    )
    assert response.status_code == 200
    _, state, _, actual, accounted, _ = rows(tmp_path / "budget.db")[0]
    assert state == "settled"
    assert actual == accounted == expected
    assert used(tmp_path / "budget.db") == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        b'{"success": true, "creditsUsed": true}',
        b'{"success": true, "creditsUsed": null}',
        b'{"success": true, "creditsUsed": -1}',
        b'{"success": true, "creditsUsed": "3"}',
        b'{"success": true, "creditsUsed": NaN}',
        b'{"success": true, "creditsUsed": Infinity}',
        b"[1, 2]",
    ],
)
async def test_bad_success_body_keeps_indeterminate(tmp_path, raw):
    executor, _, requests = await make_executor(
        tmp_path,
        [
            httpx.Response(
                200, content=raw, headers={"content-type": "application/json"}
            )
        ],
    )
    with pytest.raises(PendingRequestError) as excinfo:
        await executor.execute_sync("search", {"query": "q"}, estimate=2)
    assert len(requests) == 1
    assert excinfo.value.reservation_id
    assert "请勿重发" in str(excinfo.value)
    assert "q" not in str(excinfo.value)
    _, state, _, actual, accounted, _ = rows(tmp_path / "budget.db")[0]
    assert state == "indeterminate"
    assert actual is None
    assert accounted == 0.0
    assert used(tmp_path / "budget.db") == 0.0


@pytest.mark.asyncio
async def test_sync_oversized_credits_marks_indeterminate(tmp_path):
    """同步 creditsUsed 超出有限浮点表示：按坏费用路径标记不确定，单笔预留零入账。"""
    body = {"success": True, "creditsUsed": 10**400}
    executor, _, requests = await make_executor(
        tmp_path, [httpx.Response(200, json=body)]
    )
    with pytest.raises(PendingRequestError) as excinfo:
        await executor.execute_sync(
            "scrape", {"url": "https://example.com"}, estimate=1
        )
    assert excinfo.value.reservation_id
    assert excinfo.value.task_handle is None
    assert str(10**400) not in str(excinfo.value)
    assert len(requests) == 1
    _, state, _, actual, accounted, _ = rows(tmp_path / "budget.db")[0]
    assert state == "indeterminate"
    assert actual is None
    assert accounted == 0.0
    assert used(tmp_path / "budget.db") == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body"),
    [
        (402, {"success": False, "error": "quota exhausted"}),
        (
            403,
            {"success": False, "error": "denied", "code": "INSUFFICIENT_PERMISSIONS"},
        ),
    ],
)
async def test_final_rotation_rejection_releases(tmp_path, status, body):
    executor, _, requests = await make_executor(
        tmp_path, [httpx.Response(status, json=body)]
    )
    with pytest.raises(FirecrawlError) as excinfo:
        await executor.execute_sync("map", {"url": "https://example.com"}, estimate=1)
    assert excinfo.value.rotate_key is True
    assert excinfo.value.status == status
    assert len(requests) == 1
    _, state, _, actual, accounted, _ = rows(tmp_path / "budget.db")[0]
    assert state == "released"
    assert actual == accounted == 0.0
    assert used(tmp_path / "budget.db") == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        httpx.Response(403, json={"success": False, "error": "site blocked"}),
        httpx.Response(400, json={"success": False, "error": "bad request"}),
        httpx.Response(302),
        httpx.Response(500, json={"success": False, "error": "boom"}),
        httpx.ConnectError("network down"),
    ],
)
async def test_unclassified_failures_stay_indeterminate(tmp_path, outcome):
    executor, _, requests = await make_executor(tmp_path, [outcome])
    with pytest.raises(PendingRequestError) as excinfo:
        await executor.execute_sync("search", {"query": "q"}, estimate=2)
    assert len(requests) == 1
    assert isinstance(excinfo.value.__cause__, FirecrawlError)
    _, state, _, actual, accounted, _ = rows(tmp_path / "budget.db")[0]
    assert state == "indeterminate"
    assert actual is None
    assert accounted == 0.0


@pytest.mark.asyncio
async def test_submitting_persistence_failure_propagates_raw(tmp_path):
    executor, _, requests = await make_executor(
        tmp_path, [httpx.Response(200, json={"success": True})]
    )
    add_trigger(tmp_path / "budget.db", BLOCK_UPDATE_TRIGGER)
    with pytest.raises(sqlite3.IntegrityError):
        await executor.execute_sync("map", {"url": "https://example.com"}, estimate=1)
    assert requests == []
    _, state, _, actual, accounted, _ = rows(tmp_path / "budget.db")[0]
    assert state == "reserved"
    assert actual is None
    assert accounted == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("blocked_key", "sent", "kept_key"),
    [(K2, 1, K1), (K3, 2, K2)],
)
async def test_rotation_submitting_persistence_failure_propagates_raw(
    tmp_path, blocked_key, sent, kept_key
):
    """轮换中第 N 次发送前落账中断：原样抛出，保留 submitting 与前一 Key 摘要。"""
    executor, store, requests = await make_executor(
        tmp_path,
        [
            httpx.Response(401, json={"success": False, "error": "Unauthorized"}),
            httpx.Response(402, json={"success": False, "error": "quota"}),
            httpx.Response(200, json={"success": True, "creditsUsed": 2}),
        ],
        keys=(K1, K2, K3),
    )
    add_trigger(
        tmp_path / "budget.db",
        BLOCK_KEY_REF_TRIGGER.format(ref=key_reference(blocked_key)),
    )
    with pytest.raises(sqlite3.IntegrityError):
        await executor.execute_sync("search", {"query": "q"}, estimate=2)
    assert len(requests) == sent
    _, state, key_ref, actual, accounted, _ = rows(tmp_path / "budget.db")[0]
    assert state == "submitting"
    assert key_ref == key_reference(kept_key)
    assert actual is None
    assert accounted == 0.0
    drop_trigger(tmp_path / "budget.db", "block_key_ref")
    await store.recover()
    assert rows(tmp_path / "budget.db")[0][1] == "indeterminate"


@pytest.mark.asyncio
async def test_settle_interruption_keeps_accepted(tmp_path):
    executor, _, requests = await make_executor(
        tmp_path,
        [httpx.Response(200, json={"success": True, "creditsUsed": 3})],
    )
    add_trigger(tmp_path / "budget.db", BLOCK_SETTLE_TRIGGER)
    with pytest.raises(PendingRequestError) as excinfo:
        await executor.execute_sync("map", {"url": "https://example.com"}, estimate=1)
    assert len(requests) == 1
    assert isinstance(excinfo.value.__cause__, sqlite3.IntegrityError)
    _, state, _, actual, accounted, _ = rows(tmp_path / "budget.db")[0]
    assert state == "accepted"
    assert actual is None
    assert accounted == 0.0


@pytest.mark.asyncio
async def test_accept_interruption_keeps_submitting(tmp_path):
    executor, _, _ = await make_executor(
        tmp_path, [httpx.Response(200, json={"success": True})]
    )
    add_trigger(tmp_path / "budget.db", BLOCK_ACCEPT_TRIGGER)
    with pytest.raises(PendingRequestError) as excinfo:
        await executor.execute_sync("map", {"url": "https://example.com"}, estimate=1)
    assert isinstance(excinfo.value.__cause__, sqlite3.IntegrityError)
    _, state, _, _, _, _ = rows(tmp_path / "budget.db")[0]
    assert state == "submitting"


@pytest.mark.asyncio
async def test_cancel_propagates_and_recover_converges(tmp_path):
    """在响应前抛出取消异常，固定验证 submitting 的恢复路径。"""

    def cancel_task(_request: httpx.Request) -> None:
        raise asyncio.CancelledError

    executor, store, requests = await make_executor(
        tmp_path, [httpx.Response(200, json={"success": True})], on_request=cancel_task
    )
    with pytest.raises(asyncio.CancelledError):
        await executor.execute_sync("map", {"url": "https://example.com"}, estimate=1)
    assert len(requests) == 1
    assert rows(tmp_path / "budget.db")[0][1] == "submitting"
    await store.recover()
    assert rows(tmp_path / "budget.db")[0][1] == "indeterminate"


@pytest.mark.asyncio
async def test_anonymous_request_is_billed_without_authorization(tmp_path):
    executor, _, requests = await make_executor(
        tmp_path,
        [httpx.Response(200, json={"success": True, "creditsUsed": 1})],
        keys=(),
    )
    response = await executor.execute_sync("search", {"query": "q"}, estimate=2)
    assert "authorization" not in requests[0].headers
    assert response.extensions[KEY_REF_EXTENSION] is None
    _, state, key_ref, actual, accounted, _ = rows(tmp_path / "budget.db")[0]
    assert state == "settled"
    assert key_ref is None
    assert actual == accounted == 1.0


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["crawl", "batch"])
async def test_submit_posts_payload_to_fixed_endpoint(tmp_path, kind):
    body = {"success": True, "id": TASK_ID}
    executor, store, requests = await make_executor(
        tmp_path, [httpx.Response(200, json=body)]
    )
    response, reservation = await executor.submit(
        kind, {"url": "https://example.com"}, estimate=3
    )
    assert str(requests[0].url) == SUBMIT_URLS[kind]
    assert requests[0].method == "POST"
    assert requests[0].headers["authorization"] == f"Bearer {K1}"
    assert response.status_code == 200
    assert response.json() == body
    assert response.extensions[KEY_REF_EXTENSION] == key_reference(K1)
    assert reservation == await store.get(reservation.reservation_id)
    assert reservation.state is ReservationState.ACCEPTED
    assert reservation.job_id == TASK_ID
    assert reservation.key_ref == key_reference(K1)


@pytest.mark.asyncio
async def test_submit_success_keeps_accepted_without_settlement(tmp_path):
    body = {"success": True, "id": TASK_ID}
    executor, store, _ = await make_executor(tmp_path, [httpx.Response(200, json=body)])
    _, reservation = await executor.submit(
        "crawl", {"url": "https://example.com"}, estimate=3
    )
    assert identity_rows(tmp_path / "budget.db") == [
        (reservation.reservation_id, "accepted", key_reference(K1), TASK_ID)
    ]
    fingerprint = canonical_fingerprint(
        "crawl", "/crawl", {"url": "https://example.com"}
    )
    assert rows(tmp_path / "budget.db")[0][5] == fingerprint
    snapshot = await store.usage()
    assert snapshot.used == 0.0
    assert snapshot.active_reserved == 3.0


@pytest.mark.asyncio
async def test_submit_rotation_binds_final_key_in_single_reservation(tmp_path):
    responses = [
        httpx.Response(401, json={"success": False, "error": "Unauthorized"}),
        httpx.Response(402, json={"success": False, "error": "quota"}),
        httpx.Response(200, json={"success": True, "id": TASK_ID}),
    ]
    executor, _, requests = await make_executor(tmp_path, responses, keys=(K1, K2, K3))
    _, reservation = await executor.submit(
        "crawl", {"url": "https://example.com"}, estimate=2
    )
    assert len(requests) == 3
    assert identity_rows(tmp_path / "budget.db") == [
        (reservation.reservation_id, "accepted", key_reference(K3), TASK_ID)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"success": True, "id": TASK_ID},
        {"success": True, "id": TASK_ID, "invalidURLs": None},
        {"success": True, "id": TASK_ID, "invalidURLs": []},
        {
            "success": True,
            "id": TASK_ID,
            "invalidURLs": ["https://skip.example.com"],
        },
    ],
)
async def test_submit_preserves_invalid_urls_shapes(tmp_path, body):
    executor, _, _ = await make_executor(tmp_path, [httpx.Response(200, json=body)])
    response, _ = await executor.submit(
        "batch", {"urls": ["https://example.com"]}, estimate=1
    )
    assert response.json() == body
    assert identity_rows(tmp_path / "budget.db")[0][1] == "accepted"


@pytest.mark.asyncio
async def test_submit_ignores_credits_used_until_final_status(tmp_path):
    body = {"success": True, "id": TASK_ID, "creditsUsed": 7}
    executor, store, _ = await make_executor(tmp_path, [httpx.Response(200, json=body)])
    _, reservation = await executor.submit(
        "crawl", {"url": "https://example.com"}, estimate=3
    )
    assert reservation.state is ReservationState.ACCEPTED
    assert reservation.actual_credits is None
    assert reservation.accounted_credits == 0.0
    snapshot = await store.usage()
    assert snapshot.used == 0.0
    assert snapshot.active_reserved == 3.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"success": True},
        {"success": True, "id": 123},
        {"success": True, "id": "../escape"},
        {"success": True, "jobId": TASK_ID},
    ],
)
async def test_submit_bad_id_marks_indeterminate(tmp_path, body):
    executor, _, requests = await make_executor(
        tmp_path, [httpx.Response(200, json=body)]
    )
    with pytest.raises(PendingRequestError) as excinfo:
        await executor.submit("crawl", {"url": "https://example.com"}, estimate=2)
    assert len(requests) == 1
    assert excinfo.value.task_handle is None
    assert isinstance(excinfo.value.__cause__, ValueError)
    _, state, _, actual, accounted, _ = rows(tmp_path / "budget.db")[0]
    assert state == "indeterminate"
    assert actual is None
    assert accounted == 0.0


@pytest.mark.asyncio
async def test_submit_accept_interruption_pending_carries_task_handle(tmp_path):
    body = {"success": True, "id": TASK_ID}
    executor, store, requests = await make_executor(
        tmp_path, [httpx.Response(200, json=body)]
    )
    add_trigger(tmp_path / "budget.db", BLOCK_ACCEPT_TRIGGER)
    with pytest.raises(PendingRequestError) as excinfo:
        await executor.submit("crawl", {"url": "https://example.com"}, estimate=2)
    assert len(requests) == 1
    assert excinfo.value.reservation_id
    parsed = parse_task_handle(excinfo.value.task_handle)
    assert parsed == {
        "kind": "crawl",
        "firecrawl_id": TASK_ID,
        "key_ref": key_reference(K1),
    }
    message = str(excinfo.value)
    assert "请勿重发" in message
    assert "status" in message
    assert K1 not in message
    assert rows(tmp_path / "budget.db")[0][1] == "submitting"
    drop_trigger(tmp_path / "budget.db", "block_accept")
    await store.recover()
    assert rows(tmp_path / "budget.db")[0][1] == "indeterminate"


@pytest.mark.asyncio
async def test_recover_keeps_accepted_task_record(tmp_path):
    body = {"success": True, "id": TASK_ID}
    executor, _, _ = await make_executor(tmp_path, [httpx.Response(200, json=body)])
    _, reservation = await executor.submit(
        "batch", {"urls": ["https://example.com"]}, estimate=2
    )
    reopened = SQLiteStore(
        tmp_path / "budget.db", policy=BudgetPolicy(), clock=fixed_clock
    )
    await reopened.initialize()
    await reopened.recover()
    record = await reopened.get(reservation.reservation_id)
    assert record.state is ReservationState.ACCEPTED
    assert record.job_id == TASK_ID
    assert record.key_ref == key_reference(K1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        httpx.ConnectTimeout("timed out"),
        httpx.ConnectError("network down"),
        httpx.Response(500, json={"success": False, "error": "boom"}),
    ],
)
async def test_submit_unclassified_failure_never_resubmits(tmp_path, outcome):
    executor, _, requests = await make_executor(tmp_path, [outcome])
    with pytest.raises(PendingRequestError) as excinfo:
        await executor.submit("crawl", {"url": "https://example.com"}, estimate=2)
    assert len(requests) == 1
    assert excinfo.value.task_handle is None
    assert isinstance(excinfo.value.__cause__, FirecrawlError)
    assert rows(tmp_path / "budget.db")[0][1] == "indeterminate"


@pytest.mark.asyncio
async def test_submit_final_rejection_releases_reservation(tmp_path):
    executor, _, requests = await make_executor(
        tmp_path, [httpx.Response(402, json={"success": False, "error": "quota"})]
    )
    with pytest.raises(FirecrawlError) as excinfo:
        await executor.submit("crawl", {"url": "https://example.com"}, estimate=2)
    assert excinfo.value.rotate_key is True
    assert len(requests) == 1
    _, state, _, actual, accounted, _ = rows(tmp_path / "budget.db")[0]
    assert state == "released"
    assert actual == accounted == 0.0


@pytest.mark.asyncio
async def test_submit_empty_pool_rejects_without_network(tmp_path):
    executor, _, requests = await make_executor(tmp_path, [], keys=())
    with pytest.raises(FirecrawlError) as excinfo:
        await executor.submit("crawl", {"url": "https://example.com"}, estimate=2)
    assert excinfo.value.category is ErrorCategory.CONFIG
    assert requests == []
    _, state, _, actual, accounted, _ = rows(tmp_path / "budget.db")[0]
    assert state == "released"
    assert actual == accounted == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["status", "", "scrape"])
async def test_submit_unknown_kind_fails_before_reserve_and_network(tmp_path, kind):
    executor, _, requests = await make_executor(tmp_path, [])
    with pytest.raises(ValueError) as excinfo:
        await executor.submit(kind, {"url": "https://example.com"}, estimate=1)
    assert "example.com" not in str(excinfo.value)
    assert requests == []
    assert rows(tmp_path / "budget.db") == []


TASK_URLS = {
    "crawl": f"{SUBMIT_URLS['crawl']}/{TASK_ID}",
    "batch": f"{SUBMIT_URLS['batch']}/{TASK_ID}",
}


def task_handle(kind: str, key: str) -> str:
    """构造指向指定 Key 的合法任务句柄。"""
    return make_task_handle(kind, TASK_ID, key_reference(key))


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["status", "cancel"])
@pytest.mark.parametrize("kind", ["crawl", "batch"])
async def test_task_operation_sends_exact_request_and_returns_original(
    tmp_path, operation, kind
):
    body = {"success": True, "status": "scraping"}
    original = httpx.Response(200, json=body)
    executor, _, requests = await make_executor(tmp_path, [original])
    if operation == "status":
        response = await executor.status(task_handle(kind, K1))
        method = "GET"
    else:
        response = await executor.cancel(task_handle(kind, K1))
        method = "DELETE"
    assert str(requests[0].url) == TASK_URLS[kind]
    assert requests[0].method == method
    assert requests[0].headers["authorization"] == f"Bearer {K1}"
    assert len(requests) == 1
    assert response is original
    assert response.status_code == 200
    assert response.json() == body
    assert response.request is requests[0]
    assert response.extensions[KEY_REF_EXTENSION] == key_reference(K1)
    assert K1 not in str(response.extensions)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bad", "secret"),
    [
        (None, None),
        ("", None),
        (f"crawl:..%2fSECRET-MARKER@{key_reference(K1)}", "SECRET-MARKER"),
        (f"crawl:{TASK_ID}/../SECRET-MARKER@{key_reference(K1)}", "SECRET-MARKER"),
        (f"scrape:{TASK_ID}@{key_reference(K1)}", None),
        (f"crawl:{TASK_ID}@{key_reference(K1).upper()}", None),
        (f"crawl:{TASK_ID}@{'0' * 63}", None),
    ],
)
async def test_task_operation_invalid_handle_fails_before_network(
    tmp_path, bad, secret
):
    executor, _, requests = await make_executor(tmp_path, [])
    for call in (executor.status, executor.cancel):
        with pytest.raises(ValueError) as excinfo:
            await call(bad)
        assert secret is None or secret not in str(excinfo.value)
    assert requests == []
    assert rows(tmp_path / "budget.db") == []
    assert used(tmp_path / "budget.db") == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("keys", "handle_key"),
    [((K2, K3), K1), ((), K1)],
    ids=["ref-not-in-pool", "empty-pool"],
)
async def test_task_operation_unknown_key_ref_rejects_without_network(
    tmp_path, keys, handle_key
):
    executor, _, requests = await make_executor(tmp_path, [], keys=keys)
    for call in (executor.status, executor.cancel):
        with pytest.raises(FirecrawlError) as excinfo:
            await call(task_handle("crawl", handle_key))
        assert excinfo.value.category is ErrorCategory.CONFIG
    assert key_reference(handle_key) not in str(excinfo.value)
    assert handle_key not in str(excinfo.value)
    assert requests == []
    assert rows(tmp_path / "budget.db") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["crawl", "batch"])
async def test_task_operation_sends_pinned_key_not_first_pool_key(tmp_path, kind):
    executor, _, requests = await make_executor(
        tmp_path, [httpx.Response(200, json={"success": True})], keys=(K1, K2)
    )
    await executor.cancel(task_handle(kind, K2))
    assert len(requests) == 1
    assert requests[0].headers["authorization"] == f"Bearer {K2}"


@pytest.mark.asyncio
async def test_task_operation_ignores_cooldown_of_pinned_key(tmp_path):
    pool = KeyPool([K1, K2])
    transport, requests = make_transport(
        [httpx.Response(200, json={"success": True, "status": "scraping"})]
    )
    client = FirecrawlClient(pool, transport=transport)
    store = SQLiteStore(
        tmp_path / "budget.db", policy=BudgetPolicy(), clock=fixed_clock
    )
    await store.initialize()
    executor = BudgetedExecutor(client, store)
    await pool.mark_failure(K1, FirecrawlError(ErrorCategory.RATE_LIMIT, "limited"))
    response = await executor.status(task_handle("crawl", K1))
    assert requests[0].headers["authorization"] == f"Bearer {K1}"
    assert response.extensions[KEY_REF_EXTENSION] == key_reference(K1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        httpx.Response(401, json={"success": False, "error": "Unauthorized"}),
        httpx.Response(402, json={"success": False, "error": "quota"}),
        httpx.Response(
            403,
            json={
                "success": False,
                "error": "denied",
                "code": "INSUFFICIENT_PERMISSIONS",
            },
        ),
        httpx.Response(429, json={"success": False, "error": "rate limited"}),
        httpx.Response(500, json={"success": False, "error": "boom"}),
        httpx.ConnectTimeout("timed out"),
        httpx.ConnectError("network down"),
    ],
)
async def test_task_operation_pinned_failure_never_rotates_or_retries(
    tmp_path, outcome
):
    executor, _, requests = await make_executor(tmp_path, [outcome], keys=(K1, K2))
    with pytest.raises(FirecrawlError):
        await executor.status(task_handle("crawl", K1))
    assert len(requests) == 1
    assert requests[0].headers["authorization"] == f"Bearer {K1}"


@pytest.mark.asyncio
async def test_task_operation_returns_malicious_next_without_following(tmp_path):
    body = {
        "success": True,
        "status": "scraping",
        "next": "https://evil.example.com/v2/crawl/other",
    }
    executor, _, requests = await make_executor(
        tmp_path, [httpx.Response(200, json=body)]
    )
    response = await executor.status(task_handle("crawl", K1))
    assert len(requests) == 1
    assert str(requests[0].url) == TASK_URLS["crawl"]
    assert response.json() == body


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["status", "cancel"])
async def test_task_operation_leaves_reservation_and_usage_untouched(
    tmp_path, operation
):
    body = {
        "success": True,
        "status": "scraping" if operation == "status" else "cancelled",
        "creditsUsed": 5,
    }
    executor, store, requests = await make_executor(
        tmp_path, [httpx.Response(200, json=body)]
    )
    fingerprint = canonical_fingerprint(
        "crawl", "/crawl", {"url": "https://example.com"}
    )
    reservation = await store.reserve(3, kind="crawl", request_fingerprint=fingerprint)
    rid = reservation.reservation_id
    await store.mark_submitting(rid, key_reference(K1), confirmed_not_accepted=True)
    accepted = await store.accept(rid, job_id=TASK_ID)
    before_usage = await store.usage()
    before_rows = rows(tmp_path / "budget.db")
    if operation == "status":
        response = await executor.status(task_handle("crawl", K1))
    else:
        response = await executor.cancel(task_handle("crawl", K1))
    assert len(requests) == 1
    assert response.extensions[KEY_REF_EXTENSION] == key_reference(K1)
    assert await store.get(rid) == accepted
    after_usage = await store.usage()
    assert after_usage.used == before_usage.used == 0.0
    assert after_usage.active_reserved == before_usage.active_reserved == 3.0
    assert rows(tmp_path / "budget.db") == before_rows


TERMINAL_TASK_STATUSES = ("completed", "failed", "cancelled")
OTHER_TASK_ID = "task-other_456"


async def submit_accepted(executor: BudgetedExecutor, kind: str):
    """经公开 submit 建立一笔 accepted 任务预留，返回预留与任务句柄。"""
    _, reservation = await executor.submit(
        kind, {"url": "https://example.com"}, estimate=3
    )
    return reservation, task_handle(kind, K1)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["crawl", "batch"])
@pytest.mark.parametrize("task_status", TERMINAL_TASK_STATUSES)
async def test_status_terminal_state_settles_confirmed_credits(
    tmp_path, kind, task_status
):
    """两种 kind 的三个终态都按顶层 creditsUsed 结算并保持原响应。"""
    status_body = {"success": True, "status": task_status, "creditsUsed": 2}
    executor, store, requests = await make_executor(
        tmp_path,
        [
            httpx.Response(200, json={"success": True, "id": TASK_ID}),
            httpx.Response(200, json=status_body),
        ],
    )
    reservation, handle = await submit_accepted(executor, kind)
    response = await executor.status(handle)
    assert response.json() == status_body
    assert len(requests) == 2
    assert str(requests[1].url) == TASK_URLS[kind]
    assert requests[1].method == "GET"
    assert requests[1].headers["authorization"] == f"Bearer {K1}"
    record = await store.get(reservation.reservation_id)
    assert record.state is ReservationState.SETTLED
    assert record.actual_credits == record.accounted_credits == 2.0
    snapshot = await store.usage()
    assert snapshot.used == 2.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("credits", "expected"),
    [(0, 0.0), (2.5, 2.5), (10, 10.0)],
    ids=["zero", "decimal", "over-estimate"],
)
async def test_status_settles_actual_credits_over_estimate_included(
    tmp_path, credits, expected
):
    """实际费用 0、小数与超预估都按实值结算，不做用量 delta。"""
    status_body = {"success": True, "status": "completed", "creditsUsed": credits}
    executor, store, _ = await make_executor(
        tmp_path,
        [
            httpx.Response(200, json={"success": True, "id": TASK_ID}),
            httpx.Response(200, json=status_body),
        ],
    )
    reservation, handle = await submit_accepted(executor, "crawl")
    await executor.status(handle)
    record = await store.get(reservation.reservation_id)
    assert record.state is ReservationState.SETTLED
    assert record.actual_credits == record.accounted_credits == expected
    snapshot = await store.usage()
    assert snapshot.used == expected


@pytest.mark.asyncio
async def test_status_completed_progress_never_estimates_credits(tmp_path):
    """completed 且 completed==total 但缺 creditsUsed：保持 pending，不推计费。"""
    status_body = {
        "success": True,
        "status": "completed",
        "completed": 5,
        "total": 5,
    }
    executor, store, requests = await make_executor(
        tmp_path,
        [
            httpx.Response(200, json={"success": True, "id": TASK_ID}),
            httpx.Response(200, json=status_body),
        ],
    )
    reservation, handle = await submit_accepted(executor, "crawl")
    with pytest.raises(PendingRequestError) as excinfo:
        await executor.status(handle)
    assert len(requests) == 2
    assert excinfo.value.reservation_id == reservation.reservation_id
    assert excinfo.value.task_handle == handle
    record = await store.get(reservation.reservation_id)
    assert record.state is ReservationState.ACCEPTED
    assert record.actual_credits is None
    assert record.accounted_credits == 0.0
    snapshot = await store.usage()
    assert snapshot.used == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"next": "https://api.firecrawl.dev/v2/crawl/x/next"},
        {"completed": 1, "total": 3, "data": [{"markdown": "page"}]},
    ],
    ids=["with-next", "trimmed-pages"],
)
async def test_status_terminal_with_next_or_pages_settles_confirmed_credits(
    tmp_path, extra
):
    """终态带 next 或页数被裁剪，仍按确认的 creditsUsed 结算。"""
    status_body = {"success": True, "status": "completed", "creditsUsed": 4, **extra}
    executor, store, requests = await make_executor(
        tmp_path,
        [
            httpx.Response(200, json={"success": True, "id": TASK_ID}),
            httpx.Response(200, json=status_body),
        ],
    )
    reservation, handle = await submit_accepted(executor, "crawl")
    response = await executor.status(handle)
    assert response.json() == status_body
    assert len(requests) == 2
    record = await store.get(reservation.reservation_id)
    assert record.state is ReservationState.SETTLED
    assert record.actual_credits == 4.0


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["crawl", "batch"])
@pytest.mark.parametrize("task_status", ["scraping", "queued"])
async def test_status_non_terminal_returns_as_is_without_settlement(
    tmp_path, kind, task_status
):
    """scraping 与未知字符串状态如实返回，即使进度完成或带 creditsUsed 也不结算。"""
    status_body = {
        "success": True,
        "status": task_status,
        "completed": 2,
        "total": 2,
        "creditsUsed": 5,
        "next": "https://api.firecrawl.dev/v2/crawl/x/next",
    }
    executor, store, requests = await make_executor(
        tmp_path,
        [
            httpx.Response(200, json={"success": True, "id": TASK_ID}),
            httpx.Response(200, json=status_body),
        ],
    )
    reservation, handle = await submit_accepted(executor, kind)
    response = await executor.status(handle)
    assert response.json() == status_body
    assert len(requests) == 2
    record = await store.get(reservation.reservation_id)
    assert record.state is ReservationState.ACCEPTED
    assert record.actual_credits is None
    assert record.accounted_credits == 0.0
    snapshot = await store.usage()
    assert snapshot.used == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        b'{"success": true, "status": "completed"}',
        b'{"success": true, "status": "completed", "creditsUsed": null}',
        b'{"success": true, "status": "completed", "creditsUsed": true}',
        b'{"success": true, "status": "completed", "creditsUsed": -1}',
        b'{"success": true, "status": "completed", "creditsUsed": "3"}',
        b'{"success": true, "status": "completed", "creditsUsed": NaN}',
        b'{"success": true, "status": "completed", "creditsUsed": Infinity}',
    ],
    ids=["missing", "null", "bool", "negative", "string", "nan", "inf"],
)
async def test_status_bad_terminal_credits_keep_pending_with_handle(tmp_path, raw):
    """坏费用维持 pending：附正确 rid 与句柄，不回显坏值，不写 indeterminate。"""
    executor, store, requests = await make_executor(
        tmp_path,
        [
            httpx.Response(200, json={"success": True, "id": TASK_ID}),
            httpx.Response(
                200, content=raw, headers={"content-type": "application/json"}
            ),
        ],
    )
    reservation, handle = await submit_accepted(executor, "crawl")
    with pytest.raises(PendingRequestError) as excinfo:
        await executor.status(handle)
    assert len(requests) == 2
    assert excinfo.value.reservation_id == reservation.reservation_id
    assert excinfo.value.task_handle == handle
    message = str(excinfo.value)
    assert "creditsUsed" not in message
    assert "NaN" not in message
    assert "Infinity" not in message
    record = await store.get(reservation.reservation_id)
    assert record.state is ReservationState.ACCEPTED
    assert record.actual_credits is None
    assert record.accounted_credits == 0.0
    snapshot = await store.usage()
    assert snapshot.used == 0.0


@pytest.mark.asyncio
async def test_status_oversized_credits_keep_pending_with_handle(tmp_path):
    """终态 creditsUsed 超出有限浮点表示：坏费用路径保持 pending，不重试不回显。"""
    status_body = {"success": True, "status": "completed", "creditsUsed": 10**400}
    executor, store, requests = await make_executor(
        tmp_path,
        [
            httpx.Response(200, json={"success": True, "id": TASK_ID}),
            httpx.Response(200, json=status_body),
        ],
    )
    reservation, handle = await submit_accepted(executor, "crawl")
    with pytest.raises(PendingRequestError) as excinfo:
        await executor.status(handle)
    assert excinfo.value.reservation_id == reservation.reservation_id
    assert excinfo.value.task_handle == handle
    assert isinstance(excinfo.value.__cause__, ValueError)
    assert str(10**400) not in str(excinfo.value)
    assert len(requests) == 2
    record = await store.get(reservation.reservation_id)
    assert record.state is ReservationState.ACCEPTED
    assert record.actual_credits is None
    assert record.accounted_credits == 0.0
    snapshot = await store.usage()
    assert snapshot.used == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        b"[1, 2]",
        b'{"success": true, "creditsUsed": 1}',
        b'{"success": true, "status": null, "creditsUsed": 1}',
        b'{"success": true, "status": 123, "creditsUsed": 1}',
    ],
    ids=["non-object", "missing-status", "null-status", "int-status"],
)
async def test_status_broken_shape_fixed_error_without_accounting(tmp_path, raw):
    """破损状态形状抛固定 ValueError：不回显内容与 Key，账目零改。"""
    executor, store, requests = await make_executor(
        tmp_path,
        [
            httpx.Response(200, json={"success": True, "id": TASK_ID}),
            httpx.Response(
                200, content=raw, headers={"content-type": "application/json"}
            ),
        ],
    )
    reservation, handle = await submit_accepted(executor, "crawl")
    with pytest.raises(ValueError) as excinfo:
        await executor.status(handle)
    assert str(excinfo.value) in {
        "任务状态响应必须是 JSON 对象",
        "任务状态响应缺少有效的 status 字符串",
    }
    assert "SECRET-MARKER" not in str(excinfo.value)
    assert K1 not in str(excinfo.value)
    assert key_reference(K1) not in str(excinfo.value)
    assert len(requests) == 2
    record = await store.get(reservation.reservation_id)
    assert record.state is ReservationState.ACCEPTED
    snapshot = await store.usage()
    assert snapshot.used == 0.0


@pytest.mark.asyncio
async def test_status_non_json_body_rejected_by_client_without_accounting(tmp_path):
    """非 JSON 响应在客户端层按 usage 错误拒绝，账目零改。"""
    executor, store, requests = await make_executor(
        tmp_path,
        [
            httpx.Response(200, json={"success": True, "id": TASK_ID}),
            httpx.Response(
                200,
                content=b"<html>SECRET-MARKER</html>",
                headers={"content-type": "application/json"},
            ),
        ],
    )
    reservation, handle = await submit_accepted(executor, "crawl")
    with pytest.raises(FirecrawlError) as excinfo:
        await executor.status(handle)
    assert excinfo.value.category is ErrorCategory.USAGE
    assert len(requests) == 2
    record = await store.get(reservation.reservation_id)
    assert record.state is ReservationState.ACCEPTED
    snapshot = await store.usage()
    assert snapshot.used == 0.0


@pytest.mark.asyncio
async def test_status_repeat_terminal_settles_once(tmp_path):
    """同句柄重复 status：已结算后无待决匹配，不再重复记账。"""
    status_body = {"success": True, "status": "completed", "creditsUsed": 5}
    executor, store, requests = await make_executor(
        tmp_path,
        [
            httpx.Response(200, json={"success": True, "id": TASK_ID}),
            httpx.Response(200, json=status_body),
            httpx.Response(200, json=status_body),
        ],
    )
    reservation, handle = await submit_accepted(executor, "crawl")
    first = await executor.status(handle)
    second = await executor.status(handle)
    assert first.json() == second.json() == status_body
    assert len(requests) == 3
    record = await store.get(reservation.reservation_id)
    assert record.state is ReservationState.SETTLED
    assert record.actual_credits == record.accounted_credits == 5.0
    snapshot = await store.usage()
    assert snapshot.used == 5.0


class _BarrierStore:
    """包装真实 store：并发 list_pending 全部到齐后一起放行。"""

    def __init__(self, inner: SQLiteStore, parties: int) -> None:
        self._inner = inner
        self._parties = parties
        self._arrived = 0
        self._gate = asyncio.Event()

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    async def list_pending(self):
        self._arrived += 1
        if self._arrived >= self._parties:
            self._gate.set()
        await self._gate.wait()
        return await self._inner.list_pending()


@pytest.mark.asyncio
async def test_status_concurrent_terminal_settles_once(tmp_path):
    """两并发 GET 同时读到同一预留：依赖 settle 幂等只结算一次。"""
    status_body = {"success": True, "status": "completed", "creditsUsed": 5}
    transport, requests = make_transport(
        [
            httpx.Response(200, json={"success": True, "id": TASK_ID}),
            httpx.Response(200, json=status_body),
            httpx.Response(200, json=status_body),
        ]
    )
    client = FirecrawlClient(KeyPool([K1]), transport=transport)
    store = SQLiteStore(
        tmp_path / "budget.db", policy=BudgetPolicy(), clock=fixed_clock
    )
    await store.initialize()
    executor = BudgetedExecutor(client, _BarrierStore(store, 2))
    _, reservation = await executor.submit(
        "crawl", {"url": "https://example.com"}, estimate=3
    )
    handle = task_handle("crawl", K1)
    first, second = await asyncio.gather(
        executor.status(handle), executor.status(handle)
    )
    assert first.json() == second.json() == status_body
    assert len(requests) == 3
    record = await store.get(reservation.reservation_id)
    assert record.state is ReservationState.SETTLED
    assert record.actual_credits == record.accounted_credits == 5.0
    snapshot = await store.usage()
    assert snapshot.used == 5.0


@pytest.mark.asyncio
async def test_status_settles_after_reopen_and_recover(tmp_path):
    """重开数据库并 recover 后关联保留，status 仍能精确结算。"""
    status_body = {"success": True, "status": "completed", "creditsUsed": 2}
    transport, requests = make_transport(
        [
            httpx.Response(200, json={"success": True, "id": TASK_ID}),
            httpx.Response(200, json=status_body),
        ]
    )
    client = FirecrawlClient(KeyPool([K1]), transport=transport)
    store = SQLiteStore(
        tmp_path / "budget.db", policy=BudgetPolicy(), clock=fixed_clock
    )
    await store.initialize()
    executor = BudgetedExecutor(client, store)
    reservation, handle = await submit_accepted(executor, "crawl")
    reopened = SQLiteStore(
        tmp_path / "budget.db", policy=BudgetPolicy(), clock=fixed_clock
    )
    await reopened.initialize()
    await reopened.recover()
    record = await reopened.get(reservation.reservation_id)
    assert record.state is ReservationState.ACCEPTED
    assert record.job_id == TASK_ID
    assert record.key_ref == key_reference(K1)
    response = await BudgetedExecutor(client, reopened).status(handle)
    assert response.json() == status_body
    assert len(requests) == 2
    record = await reopened.get(reservation.reservation_id)
    assert record.state is ReservationState.SETTLED
    assert record.actual_credits == 2.0
    snapshot = await reopened.usage()
    assert snapshot.used == 2.0


@pytest.mark.asyncio
async def test_status_settle_across_utc_day_stays_on_original_day(tmp_path):
    """跨日结算按原日差额入账，当日不写入。"""
    clock_holder = {"now": fixed_clock()}

    def shifting_clock() -> datetime:
        return clock_holder["now"]

    status_body = {"success": True, "status": "completed", "creditsUsed": 5}
    transport, requests = make_transport(
        [
            httpx.Response(200, json={"success": True, "id": TASK_ID}),
            httpx.Response(200, json=status_body),
        ]
    )
    client = FirecrawlClient(KeyPool([K1]), transport=transport)
    store = SQLiteStore(
        tmp_path / "budget.db", policy=BudgetPolicy(), clock=shifting_clock
    )
    await store.initialize()
    executor = BudgetedExecutor(client, store)
    _, handle = await submit_accepted(executor, "crawl")
    clock_holder["now"] = datetime.fromisoformat("2026-01-02T12:00:00+00:00")
    await executor.status(handle)
    assert len(requests) == 2
    day_one = await store.usage("2026-01-01")
    assert day_one.used == 5.0
    assert day_one.active_reserved == 0.0
    day_two = await store.usage("2026-01-02")
    assert day_two.used == 0.0
    assert day_two.active_reserved == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handle_kind", "handle_task_id", "handle_key"),
    [
        ("batch", TASK_ID, K1),
        ("crawl", OTHER_TASK_ID, K1),
        ("crawl", TASK_ID, K2),
    ],
    ids=["other-kind", "other-job-id", "other-key-ref"],
)
async def test_status_mismatched_identity_never_settles(
    tmp_path, handle_kind, handle_task_id, handle_key
):
    """kind/job_id/key_ref 任一字段不同都不串账，预留原样保留。"""
    status_body = {"success": True, "status": "completed", "creditsUsed": 5}
    executor, store, requests = await make_executor(
        tmp_path,
        [
            httpx.Response(200, json={"success": True, "id": TASK_ID}),
            httpx.Response(200, json=status_body),
        ],
        keys=(K1, K2),
    )
    reservation, _ = await submit_accepted(executor, "crawl")
    handle = make_task_handle(handle_kind, handle_task_id, key_reference(handle_key))
    response = await executor.status(handle)
    assert response.json() == status_body
    assert len(requests) == 2
    record = await store.get(reservation.reservation_id)
    assert record.state is ReservationState.ACCEPTED
    assert record.actual_credits is None
    snapshot = await store.usage()
    assert snapshot.used == 0.0


@pytest.mark.asyncio
async def test_status_never_matches_reservation_without_job_id(tmp_path):
    """同 Key 的 job_id=None 预留不被句柄猜中，不建账。"""
    status_body = {"success": True, "status": "completed", "creditsUsed": 5}
    executor, store, requests = await make_executor(
        tmp_path,
        [httpx.Response(200, json=status_body)],
    )
    fingerprint = canonical_fingerprint(
        "crawl", "/crawl", {"url": "https://example.com"}
    )
    reserved = await store.reserve(3, kind="crawl", request_fingerprint=fingerprint)
    await store.mark_submitting(
        reserved.reservation_id, key_reference(K1), confirmed_not_accepted=True
    )
    await store.accept(reserved.reservation_id)
    response = await executor.status(task_handle("crawl", K1))
    assert response.json() == status_body
    assert len(requests) == 1
    record = await store.get(reserved.reservation_id)
    assert record.state is ReservationState.ACCEPTED
    assert record.job_id is None
    snapshot = await store.usage()
    assert snapshot.used == 0.0


@pytest.mark.asyncio
async def test_status_without_local_reservation_creates_no_accounts(tmp_path):
    """远程终态但本地无关联：原样返回，不建任何账。"""
    status_body = {"success": True, "status": "completed", "creditsUsed": 5}
    executor, _, requests = await make_executor(
        tmp_path, [httpx.Response(200, json=status_body)]
    )
    response = await executor.status(task_handle("crawl", K1))
    assert response.json() == status_body
    assert len(requests) == 1
    assert rows(tmp_path / "budget.db") == []
    assert used(tmp_path / "budget.db") == 0.0


@pytest.mark.asyncio
async def test_status_ambiguous_matches_reject_without_settlement(tmp_path):
    """同句柄匹配多笔待决预留：固定 ValueError 拒绝，全部保留不挑第一笔。"""
    status_body = {"success": True, "status": "completed", "creditsUsed": 5}
    executor, store, requests = await make_executor(
        tmp_path,
        [
            httpx.Response(200, json={"success": True, "id": TASK_ID}),
            httpx.Response(200, json={"success": True, "id": TASK_ID}),
            httpx.Response(200, json=status_body),
        ],
    )
    first, _ = await submit_accepted(executor, "crawl")
    second, handle = await submit_accepted(executor, "crawl")
    with pytest.raises(ValueError) as excinfo:
        await executor.status(handle)
    assert str(excinfo.value) == "任务句柄匹配到多笔待决预留，拒绝结算"
    assert first.reservation_id not in str(excinfo.value)
    assert second.reservation_id not in str(excinfo.value)
    assert len(requests) == 3
    assert identity_rows(tmp_path / "budget.db") == [
        (first.reservation_id, "accepted", key_reference(K1), TASK_ID),
        (second.reservation_id, "accepted", key_reference(K1), TASK_ID),
    ]
    snapshot = await store.usage()
    assert snapshot.used == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize("start_state", ["accepted", "indeterminate"])
async def test_status_settle_interruption_keeps_real_state(tmp_path, start_state):
    """settle 被中断保留真实状态与 cause/句柄，移除触发器后可再次结算且不重试。"""
    status_body = {"success": True, "status": "completed", "creditsUsed": 5}
    executor, store, requests = await make_executor(
        tmp_path,
        [
            httpx.Response(200, json={"success": True, "id": TASK_ID}),
            httpx.Response(200, json=status_body),
            httpx.Response(200, json=status_body),
        ],
    )
    reservation, handle = await submit_accepted(executor, "crawl")
    rid = reservation.reservation_id
    if start_state == "indeterminate":
        # 公开状态机不产生带 job_id 的 indeterminate，直接落库模拟崩溃遗留状态。
        conn = sqlite3.connect(tmp_path / "budget.db")
        try:
            conn.execute(
                "UPDATE reservations SET state = 'indeterminate'"
                " WHERE reservation_id = ?",
                (rid,),
            )
            conn.commit()
        finally:
            conn.close()
    add_trigger(tmp_path / "budget.db", BLOCK_SETTLE_TRIGGER)
    with pytest.raises(PendingRequestError) as excinfo:
        await executor.status(handle)
    assert len(requests) == 2
    assert excinfo.value.reservation_id == rid
    assert excinfo.value.task_handle == handle
    assert isinstance(excinfo.value.__cause__, sqlite3.IntegrityError)
    assert identity_rows(tmp_path / "budget.db") == [
        (rid, start_state, key_reference(K1), TASK_ID)
    ]
    drop_trigger(tmp_path / "budget.db", "block_settle")
    response = await executor.status(handle)
    assert response.json() == status_body
    assert len(requests) == 3
    record = await store.get(rid)
    assert record.state is ReservationState.SETTLED
    assert record.actual_credits == 5.0
    snapshot = await store.usage()
    assert snapshot.used == 5.0


class _BrokenListStore:
    """包装真实 store：仅 list_pending 抛错，其余全部委托。"""

    def __init__(self, inner: SQLiteStore) -> None:
        self._inner = inner

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    async def list_pending(self):
        raise RuntimeError("list pending failed")


@pytest.mark.asyncio
async def test_status_list_pending_failure_propagates_raw(tmp_path):
    """list_pending 读取失败原样上抛，不包 Pending 不隐藏，账目不动。"""
    status_body = {"success": True, "status": "completed", "creditsUsed": 5}
    transport, requests = make_transport(
        [
            httpx.Response(200, json={"success": True, "id": TASK_ID}),
            httpx.Response(200, json=status_body),
        ]
    )
    client = FirecrawlClient(KeyPool([K1]), transport=transport)
    store = SQLiteStore(
        tmp_path / "budget.db", policy=BudgetPolicy(), clock=fixed_clock
    )
    await store.initialize()
    executor = BudgetedExecutor(client, _BrokenListStore(store))
    reservation, handle = await submit_accepted(executor, "crawl")
    with pytest.raises(RuntimeError):
        await executor.status(handle)
    assert len(requests) == 2
    record = await store.get(reservation.reservation_id)
    assert record.state is ReservationState.ACCEPTED
    snapshot = await store.usage()
    assert snapshot.used == 0.0


class _CancelOnListStore:
    """包装真实 store：list_pending 触发当前任务取消。"""

    def __init__(self, inner: SQLiteStore) -> None:
        self._inner = inner

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    async def list_pending(self):
        asyncio.current_task().cancel()
        return await self._inner.list_pending()


@pytest.mark.asyncio
async def test_status_cancelled_error_propagates_without_accounting(tmp_path):
    """终态对账中的 CancelledError 原样传播，不捕获不改账。"""
    status_body = {"success": True, "status": "completed", "creditsUsed": 5}
    transport, requests = make_transport(
        [
            httpx.Response(200, json={"success": True, "id": TASK_ID}),
            httpx.Response(200, json=status_body),
        ]
    )
    client = FirecrawlClient(KeyPool([K1]), transport=transport)
    store = SQLiteStore(
        tmp_path / "budget.db", policy=BudgetPolicy(), clock=fixed_clock
    )
    await store.initialize()
    executor = BudgetedExecutor(client, _CancelOnListStore(store))
    reservation, handle = await submit_accepted(executor, "crawl")
    with pytest.raises(asyncio.CancelledError):
        await executor.status(handle)
    assert len(requests) == 2
    record = await store.get(reservation.reservation_id)
    assert record.state is ReservationState.ACCEPTED
    snapshot = await store.usage()
    assert snapshot.used == 0.0


@pytest.mark.asyncio
async def test_cancel_terminal_response_never_settles(tmp_path):
    """cancel 携带终态与费用也只返回原响应，不结算不释放。"""
    cancel_body = {"success": True, "status": "cancelled", "creditsUsed": 9}
    executor, store, requests = await make_executor(
        tmp_path,
        [
            httpx.Response(200, json={"success": True, "id": TASK_ID}),
            httpx.Response(200, json=cancel_body),
        ],
    )
    reservation, handle = await submit_accepted(executor, "crawl")
    response = await executor.cancel(handle)
    assert response.json() == cancel_body
    assert len(requests) == 2
    assert requests[1].method == "DELETE"
    record = await store.get(reservation.reservation_id)
    assert record.state is ReservationState.ACCEPTED
    assert record.actual_credits is None
    assert record.accounted_credits == 0.0
    snapshot = await store.usage()
    assert snapshot.used == 0.0
