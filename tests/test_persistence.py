"""SQLiteStore 预算存储测试：初始化、原子预留、生命周期、日切恢复与用量。"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from astrbot_plugin_firecrawl.firecrawl_api.budget import (
    BudgetExceeded,
    BudgetPolicy,
    InvalidBudgetTransition,
    ReservationState,
)
from astrbot_plugin_firecrawl.persistence import SQLiteStore, UsageSnapshot

_DAY = "2026-01-01"
_PAST = "2025-12-31"
_TZ8 = timezone(timedelta(hours=8))
_STAMP = "2026-01-01T00:00:00+00:00"


def fp(n: int) -> str:
    """生成第 n 个合法的 64 位小写十六进制指纹。"""
    return format(n, "064x")


def fixed_clock() -> datetime:
    """返回固定的 UTC 时刻，便于断言 utc_day。"""
    return datetime.fromisoformat(f"{_DAY}T12:00:00+00:00")


def make_store(db_path: str | Path, policy: BudgetPolicy | None = None) -> SQLiteStore:
    """在指定路径创建使用固定 clock 的 Store。"""

    return SQLiteStore(db_path, policy=policy or BudgetPolicy(), clock=fixed_clock)


def past_store(path: str | Path, policy: BudgetPolicy | None = None) -> SQLiteStore:
    """使用上一 UTC 日 12:00 固定 clock 的 Store，构建真实跨日状态。"""
    moment = datetime.fromisoformat(f"{_PAST}T12:00:00+00:00")
    return SQLiteStore(path, policy=policy or BudgetPolicy(), clock=lambda: moment)


@pytest.mark.asyncio
async def test_initialize_is_idempotent_and_sets_version(tmp_path):
    path = tmp_path / "budget.db"
    store = make_store(path)
    await store.initialize()
    await store.initialize()
    conn = sqlite3.connect(path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    conn.close()
    assert version == 1
    assert {"daily_usage", "reservations"} <= tables


@pytest.mark.asyncio
async def test_initialize_rejects_newer_schema_version(tmp_path):
    path = tmp_path / "budget.db"
    await make_store(path).initialize()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="schema 版本"):
        await make_store(path).initialize()


@pytest.mark.asyncio
async def test_initialize_reject_keeps_version_and_journal_mode(tmp_path):
    path = tmp_path / "budget.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="schema 版本"):
        await make_store(path).initialize()
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    conn.close()


@pytest.mark.asyncio
async def test_initialize_enables_wal_after_committed_migration(tmp_path):
    path = tmp_path / "budget.db"
    await make_store(path).initialize()
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()


@pytest.mark.asyncio
async def test_reservation_survives_reopen_with_all_fields(tmp_path):
    path = tmp_path / "budget.db"
    store = make_store(path)
    await store.initialize()
    record = await store.reserve(
        7,
        kind="scrape",
        request_fingerprint=fp(1),
        reservation_id="r-1",
    )
    reopened = make_store(path)
    await reopened.initialize()
    restored = await reopened.get("r-1")
    assert restored == record
    assert restored.state is ReservationState.RESERVED
    assert restored.utc_day == _DAY
    assert restored.accounted_credits == 0.0
    assert restored.actual_credits is None
    assert restored.key_ref is None
    assert restored.job_id is None


@pytest.mark.asyncio
async def test_daily_limit_reached_exactly_then_blocks(tmp_path):
    store = make_store(
        tmp_path / "budget.db", policy=BudgetPolicy(daily_credit_limit=5)
    )
    await store.initialize()
    await store.reserve(5, kind="scrape", request_fingerprint=fp(1))
    with pytest.raises(BudgetExceeded):
        await store.reserve(1, kind="scrape", request_fingerprint=fp(2))
    assert (await store.usage()).active_reserved == 5.0


@pytest.mark.asyncio
async def test_per_call_limit_rejects_oversized_estimate(tmp_path):
    policy = BudgetPolicy(max_estimated_credits_per_call=3)
    store = make_store(tmp_path / "budget.db", policy=policy)
    await store.initialize()
    with pytest.raises(BudgetExceeded):
        await store.reserve(4, kind="scrape", request_fingerprint=fp(1))
    assert (await store.usage()).active_reserved == 0.0


@pytest.mark.asyncio
async def test_zero_limit_means_unlimited_but_still_accounts(tmp_path):
    store = make_store(tmp_path / "budget.db")
    await store.initialize()
    record = await store.reserve(10**6, kind="crawl", request_fingerprint=fp(1))
    snapshot = await store.usage()
    assert snapshot.utc_day == _DAY
    assert snapshot.used == 0.0
    assert snapshot.active_reserved == 10**6
    assert (await store.get(record.reservation_id)).estimate == 10**6


@pytest.mark.asyncio
async def test_usage_counts_only_requested_day(tmp_path):
    store = make_store(tmp_path / "budget.db")
    await store.initialize()
    await store.reserve(4, kind="scrape", request_fingerprint=fp(1))
    assert await store.usage("2026-01-02") == UsageSnapshot("2026-01-02", 0.0, 0.0)
    assert (await store.usage()).active_reserved == 4.0


@pytest.mark.asyncio
async def test_usage_snapshot_survives_concurrent_settle_between_reads(
    tmp_path, monkeypatch
):
    path = tmp_path / "budget.db"
    store = make_store(path)
    await store.initialize()
    await store.reserve(
        7, kind="scrape", request_fingerprint=fp(1), reservation_id="r-1"
    )
    real_connect = sqlite3.connect

    def settle() -> None:
        writer = real_connect(path)
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute(
                "UPDATE reservations SET state = 'settled', accounted_credits = 7"
                " WHERE reservation_id = 'r-1'"
            )
            writer.execute(
                "INSERT INTO daily_usage (utc_day, used) VALUES (?, 7)"
                " ON CONFLICT (utc_day) DO UPDATE SET used = excluded.used",
                (_DAY,),
            )
            writer.commit()
        finally:
            writer.close()

    def hook(statement: str) -> None:
        if "SUM(estimate - accounted_credits)" in statement:
            settle()

    def intercept_connect(db, *args, **kwargs):
        conn = real_connect(db, *args, **kwargs)
        if Path(db) == path:
            conn.set_trace_callback(hook)
        return conn

    monkeypatch.setattr(sqlite3, "connect", intercept_connect)
    snapshot = await store.usage()
    assert snapshot.used + snapshot.active_reserved == 7

    raw = real_connect(path)
    state = raw.execute(
        "SELECT state FROM reservations WHERE reservation_id = 'r-1'"
    ).fetchone()[0]
    used = raw.execute(
        "SELECT used FROM daily_usage WHERE utc_day = ?", (_DAY,)
    ).fetchone()[0]
    raw.close()
    assert state == "settled"
    assert used == 7.0


@pytest.mark.asyncio
async def test_two_stores_concurrent_reserve_respect_daily_limit(tmp_path):
    path = tmp_path / "budget.db"
    policy = BudgetPolicy(daily_credit_limit=10)
    store_a = make_store(path, policy=policy)
    store_b = make_store(path, policy=policy)
    await asyncio.gather(store_a.initialize(), store_b.initialize())
    attempts = [
        store_a.reserve(1, kind="scrape", request_fingerprint=fp(i))
        if i % 2 == 0
        else store_b.reserve(1, kind="scrape", request_fingerprint=fp(i))
        for i in range(30)
    ]
    results = await asyncio.gather(*attempts, return_exceptions=True)
    succeeded = [r for r in results if not isinstance(r, BaseException)]
    exceeded = [r for r in results if isinstance(r, BudgetExceeded)]
    assert len(succeeded) == 10
    assert len(exceeded) == 20
    assert (await store_a.usage()).active_reserved == 10.0


@pytest.mark.asyncio
async def test_duplicate_explicit_id_rejected_without_overwrite(tmp_path):
    store = make_store(tmp_path / "budget.db")
    await store.initialize()
    await store.reserve(
        3, kind="scrape", request_fingerprint=fp(1), reservation_id="dup"
    )
    with pytest.raises(ValueError, match="预留 ID 已存在"):
        await store.reserve(
            9, kind="search", request_fingerprint=fp(2), reservation_id="dup"
        )
    assert (await store.get("dup")).estimate == 3


@pytest.mark.asyncio
async def test_reserve_rejects_invalid_estimate(tmp_path):
    store = make_store(tmp_path / "budget.db")
    await store.initialize()
    for bad in (-1, True, 1.5):
        with pytest.raises(ValueError):
            await store.reserve(bad, kind="scrape", request_fingerprint=fp(1))
    assert (await store.usage()).active_reserved == 0.0


@pytest.mark.asyncio
async def test_reserve_rejects_invalid_fingerprint(tmp_path):
    store = make_store(tmp_path / "budget.db")
    await store.initialize()
    for bad in ("a" * 63, "a" * 65, "A" * 64, "z" * 64):
        with pytest.raises(ValueError):
            await store.reserve(1, kind="scrape", request_fingerprint=bad)
    assert (await store.usage()).active_reserved == 0.0


@pytest.mark.asyncio
async def test_reserve_rejects_invalid_explicit_id(tmp_path):
    store = make_store(tmp_path / "budget.db")
    await store.initialize()
    for bad in ("", 123):
        with pytest.raises(ValueError):
            await store.reserve(
                1, kind="scrape", request_fingerprint=fp(1), reservation_id=bad
            )
    assert (await store.usage()).active_reserved == 0.0


@pytest.mark.asyncio
async def test_get_unknown_id_raises(tmp_path):
    store = make_store(tmp_path / "budget.db")
    await store.initialize()
    with pytest.raises(KeyError, match="未知预留"):
        await store.get("missing")


@pytest.mark.asyncio
async def test_full_path_submitting_accept_settle_persists(tmp_path):
    path = tmp_path / "budget.db"
    store = make_store(path)
    await store.initialize()
    await store.reserve(
        7, kind="scrape", request_fingerprint=fp(1), reservation_id="r-1"
    )
    submitted = await store.mark_submitting("r-1", key_ref=fp(2))
    assert submitted.state is ReservationState.SUBMITTING
    persisted = await make_store(path).get("r-1")
    assert persisted.state is ReservationState.SUBMITTING
    assert persisted.key_ref == fp(2)
    accepted = await store.accept("r-1", "job-1")
    assert accepted.state is ReservationState.ACCEPTED
    assert accepted.job_id == "job-1"
    settled = await store.settle("r-1", 2.5)
    assert settled.state is ReservationState.SETTLED
    assert settled.actual_credits == 2.5
    assert settled.accounted_credits == 2.5
    snapshot = await store.usage()
    assert snapshot.used == 2.5
    assert snapshot.active_reserved == 0.0


@pytest.mark.asyncio
async def test_accept_keeps_key_ref_and_optional_job_id(tmp_path):
    store = make_store(tmp_path / "budget.db")
    await store.initialize()
    await store.reserve(
        3, kind="scrape", request_fingerprint=fp(1), reservation_id="r-1"
    )
    await store.mark_submitting("r-1", key_ref=fp(2))
    accepted = await store.accept("r-1")
    assert accepted.key_ref == fp(2)
    assert accepted.job_id is None
    for bad in ("", 123):
        with pytest.raises(ValueError):
            await store.accept("r-1", bad)
    assert (await store.get("r-1")).state is ReservationState.ACCEPTED


@pytest.mark.asyncio
async def test_confirmed_cross_key_retry_keeps_id_and_budget(tmp_path):
    store = make_store(tmp_path / "budget.db")
    await store.initialize()
    await store.reserve(
        7, kind="scrape", request_fingerprint=fp(1), reservation_id="r-1"
    )
    await store.mark_submitting("r-1", key_ref=fp(2))
    retried = await store.mark_submitting(
        "r-1", key_ref=fp(3), confirmed_not_accepted=True
    )
    assert retried.reservation_id == "r-1"
    assert retried.key_ref == fp(3)
    assert retried.state is ReservationState.SUBMITTING
    snapshot = await store.usage()
    assert snapshot.active_reserved == 7.0
    assert snapshot.used == 0.0


@pytest.mark.asyncio
async def test_resubmit_requires_confirmation_or_is_rejected(tmp_path):
    store = make_store(tmp_path / "budget.db")
    await store.initialize()
    await store.reserve(
        4, kind="scrape", request_fingerprint=fp(1), reservation_id="r-1"
    )
    await store.mark_submitting("r-1", key_ref=fp(2))
    with pytest.raises(InvalidBudgetTransition):
        await store.mark_submitting("r-1")
    assert (await store.get("r-1")).key_ref == fp(2)
    await store.mark_indeterminate("r-1")
    with pytest.raises(InvalidBudgetTransition):
        await store.mark_submitting("r-1", key_ref=fp(3), confirmed_not_accepted=True)
    assert (await store.get("r-1")).state is ReservationState.INDETERMINATE


@pytest.mark.asyncio
async def test_release_reserved_then_idempotent(tmp_path):
    store = make_store(tmp_path / "budget.db")
    await store.initialize()
    await store.reserve(
        6, kind="scrape", request_fingerprint=fp(1), reservation_id="r-1"
    )
    released = await store.release("r-1")
    assert released.state is ReservationState.RELEASED
    assert released.actual_credits == 0.0
    assert released.accounted_credits == 0.0
    assert await store.release("r-1") == released
    snapshot = await store.usage()
    assert snapshot.used == 0.0
    assert snapshot.active_reserved == 0.0


@pytest.mark.asyncio
async def test_illegal_transitions_leave_record_untouched(tmp_path):
    store = make_store(tmp_path / "budget.db")
    await store.initialize()
    await store.reserve(
        5, kind="scrape", request_fingerprint=fp(1), reservation_id="r-a"
    )
    for call in (
        lambda: store.accept("r-a"),
        lambda: store.settle("r-a"),
        lambda: store.mark_indeterminate("r-a"),
    ):
        with pytest.raises(InvalidBudgetTransition):
            await call()
    assert (await store.get("r-a")).state is ReservationState.RESERVED
    assert (await store.usage()).active_reserved == 5.0
    await store.mark_submitting("r-a")
    with pytest.raises(InvalidBudgetTransition):
        await store.release("r-a")
    assert (await store.get("r-a")).state is ReservationState.SUBMITTING
    await store.mark_indeterminate("r-a")
    await store.settle("r-a", 5)
    for call in (lambda: store.release("r-a"), lambda: store.mark_submitting("r-a")):
        with pytest.raises(InvalidBudgetTransition):
            await call()
    settled = await store.get("r-a")
    assert settled.state is ReservationState.SETTLED
    assert settled.accounted_credits == 5.0
    assert (await store.usage()).used == 5.0


@pytest.mark.asyncio
async def test_settle_fallback_and_fraction_amounts(tmp_path):
    store = make_store(tmp_path / "budget.db")
    await store.initialize()
    await store.reserve(
        7, kind="scrape", request_fingerprint=fp(1), reservation_id="r-1"
    )
    await store.mark_submitting("r-1")
    await store.accept("r-1")
    settled = await store.settle("r-1")
    assert settled.actual_credits == 7.0
    assert (await store.usage()).used == 7.0
    await store.reserve(
        9, kind="scrape", request_fingerprint=fp(2), reservation_id="r-2"
    )
    await store.mark_submitting("r-2")
    await store.mark_indeterminate("r-2")
    partial = await store.settle("r-2", 2.5)
    assert partial.actual_credits == 2.5
    assert partial.accounted_credits == 2.5
    snapshot = await store.usage()
    assert snapshot.used == 9.5
    assert snapshot.active_reserved == 0.0


@pytest.mark.asyncio
async def test_settle_over_limit_still_accounts_and_blocks_new(tmp_path):
    store = make_store(
        tmp_path / "budget.db", policy=BudgetPolicy(daily_credit_limit=5)
    )
    await store.initialize()
    await store.reserve(
        5, kind="scrape", request_fingerprint=fp(1), reservation_id="r-1"
    )
    await store.mark_submitting("r-1")
    await store.accept("r-1")
    settled = await store.settle("r-1", 9)
    assert settled.actual_credits == 9.0
    assert (await store.usage()).used == 9.0
    with pytest.raises(BudgetExceeded):
        await store.reserve(1, kind="scrape", request_fingerprint=fp(2))


@pytest.mark.asyncio
async def test_settle_rejects_invalid_actual_amounts(tmp_path):
    store = make_store(tmp_path / "budget.db")
    await store.initialize()
    await store.reserve(
        4, kind="scrape", request_fingerprint=fp(1), reservation_id="r-1"
    )
    await store.mark_submitting("r-1")
    await store.accept("r-1")
    for bad in (True, float("nan"), float("inf"), -1, -0.5):
        with pytest.raises(ValueError):
            await store.settle("r-1", bad)
    record = await store.get("r-1")
    assert record.state is ReservationState.ACCEPTED
    assert record.actual_credits is None
    assert (await store.usage()).used == 0.0


@pytest.mark.asyncio
async def test_settle_idempotent_same_value_and_conflict(tmp_path):
    store = make_store(tmp_path / "budget.db")
    await store.initialize()
    await store.reserve(
        6, kind="scrape", request_fingerprint=fp(1), reservation_id="r-1"
    )
    await store.mark_submitting("r-1")
    await store.mark_indeterminate("r-1")
    first = await store.settle("r-1", 2.5)
    assert await store.settle("r-1", 2.5) == first
    assert await store.settle("r-1") == first
    assert (await store.usage()).used == 2.5
    with pytest.raises(ValueError, match="冲突"):
        await store.settle("r-1", 9)
    assert (await store.get("r-1")).actual_credits == 2.5
    assert (await store.usage()).used == 2.5


@pytest.mark.asyncio
async def test_concurrent_settle_applies_delta_once(tmp_path):
    path = tmp_path / "budget.db"
    store_a = make_store(path)
    store_b = make_store(path)
    await asyncio.gather(store_a.initialize(), store_b.initialize())
    await store_a.reserve(
        6, kind="scrape", request_fingerprint=fp(1), reservation_id="r-1"
    )
    await store_a.mark_submitting("r-1")
    await store_b.mark_indeterminate("r-1")
    first, second = await asyncio.gather(
        store_a.settle("r-1", 2.5), store_b.settle("r-1", 2.5)
    )
    assert first == second
    assert (await store_a.usage()).used == 2.5


@pytest.mark.asyncio
async def test_settle_refund_on_planted_accounted_pending(tmp_path):
    path = tmp_path / "budget.db"
    store = make_store(path)
    await store.initialize()
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO reservations (reservation_id, utc_day, estimate, kind,"
        " request_fingerprint, created_at, updated_at, state, key_ref, job_id,"
        " actual_credits, accounted_credits)"
        " VALUES ('r-1', ?, 6, 'scrape', ?, ?, ?, 'indeterminate', NULL, NULL,"
        " 4.0, 4.0)",
        (_DAY, fp(1), _STAMP, _STAMP),
    )
    conn.execute("INSERT INTO daily_usage (utc_day, used) VALUES (?, 4.0)", (_DAY,))
    conn.commit()
    conn.close()
    settled = await store.settle("r-1", 1.5)
    assert settled.accounted_credits == 1.5
    snapshot = await store.usage()
    assert snapshot.used == 1.5
    assert snapshot.active_reserved == 0.0


@pytest.mark.asyncio
async def test_release_refund_on_planted_accounted_record(tmp_path):
    path = tmp_path / "budget.db"
    store = make_store(path)
    await store.initialize()
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO reservations (reservation_id, utc_day, estimate, kind,"
        " request_fingerprint, created_at, updated_at, state, key_ref, job_id,"
        " actual_credits, accounted_credits)"
        " VALUES ('r-1', ?, 6, 'scrape', ?, ?, ?, 'reserved', NULL, NULL, 2.0, 2.0)",
        (_DAY, fp(1), _STAMP, _STAMP),
    )
    conn.execute("INSERT INTO daily_usage (utc_day, used) VALUES (?, 2.0)", (_DAY,))
    conn.commit()
    conn.close()
    released = await store.release("r-1")
    assert released.actual_credits == 0.0
    assert released.accounted_credits == 0.0
    snapshot = await store.usage()
    assert snapshot.used == 0.0
    assert snapshot.active_reserved == 0.0


@pytest.mark.asyncio
async def test_submitting_rejects_invalid_key_ref_without_echo(tmp_path):
    store = make_store(tmp_path / "budget.db")
    await store.initialize()
    await store.reserve(
        1, kind="scrape", request_fingerprint=fp(1), reservation_id="r-1"
    )
    for bad in ("a" * 63, "A" * 64, 123):
        with pytest.raises(ValueError) as excinfo:
            await store.mark_submitting("r-1", key_ref=bad)
        assert str(bad) not in str(excinfo.value)
    assert (await store.get("r-1")).state is ReservationState.RESERVED


@pytest.mark.asyncio
async def test_transition_unknown_id_raises(tmp_path):
    store = make_store(tmp_path / "budget.db")
    await store.initialize()
    for call in (
        lambda: store.mark_submitting("missing"),
        lambda: store.accept("missing"),
        lambda: store.mark_indeterminate("missing"),
        lambda: store.settle("missing"),
        lambda: store.release("missing"),
    ):
        with pytest.raises(KeyError):
            await call()


@pytest.mark.asyncio
async def test_reopen_recover_migrates_states_and_keeps_metadata(tmp_path):
    path = tmp_path / "budget.db"
    store = past_store(path)
    await store.initialize()
    await store.reserve(
        3, kind="scrape", request_fingerprint=fp(1), reservation_id="r-r"
    )
    await store.reserve(
        4, kind="scrape", request_fingerprint=fp(2), reservation_id="r-s"
    )
    await store.reserve(
        5, kind="scrape", request_fingerprint=fp(3), reservation_id="r-a"
    )
    await store.mark_submitting("r-s", key_ref=fp(9))
    await store.mark_submitting("r-a")
    await store.accept("r-a", "job-9")
    fresh = make_store(path)
    await fresh.initialize()
    recovered = await fresh.recover()
    assert [r.reservation_id for r in recovered] == ["r-a", "r-s"]
    by_id = {r.reservation_id: r for r in recovered}
    assert by_id["r-s"].state is ReservationState.INDETERMINATE
    assert by_id["r-s"].key_ref == fp(9)
    assert by_id["r-a"].state is ReservationState.ACCEPTED
    assert by_id["r-a"].job_id == "job-9"
    released = await fresh.get("r-r")
    assert released.state is ReservationState.RELEASED
    assert released.accounted_credits == 0.0
    snapshot = await fresh.usage(_PAST)
    assert snapshot.used == 9.0
    assert snapshot.active_reserved == 0.0
    assert await fresh.recover() == recovered
    assert (await fresh.usage(_PAST)).used == 9.0


@pytest.mark.asyncio
async def test_cross_midnight_accept_with_tz_clocks(tmp_path):
    path = tmp_path / "budget.db"
    late = SQLiteStore(path, clock=lambda: datetime(2025, 12, 31, 23, 30, tzinfo=_TZ8))
    await late.initialize()
    await late.reserve(
        7, kind="scrape", request_fingerprint=fp(1), reservation_id="r-a"
    )
    await late.mark_submitting("r-a", key_ref=fp(4))
    dawn = SQLiteStore(path, clock=lambda: datetime(2026, 1, 1, 8, 0, tzinfo=_TZ8))
    await dawn.initialize()
    accepted = await dawn.accept("r-a", "job-tz")
    assert accepted.state is ReservationState.ACCEPTED
    assert accepted.utc_day == _PAST
    assert accepted.job_id == "job-tz"
    assert accepted.created_at.endswith("+00:00")
    assert accepted.updated_at.endswith("+00:00")
    assert (await dawn.usage(_PAST)).used == 7.0
    today = await dawn.usage()
    assert today.utc_day == _DAY
    assert today.used == 0.0
    assert today.active_reserved == 0.0


@pytest.mark.asyncio
async def test_new_day_budget_not_blocked_by_past_pending(tmp_path):
    path = tmp_path / "budget.db"
    policy = BudgetPolicy(daily_credit_limit=5)
    store = past_store(path, policy=policy)
    await store.initialize()
    await store.reserve(
        5, kind="scrape", request_fingerprint=fp(1), reservation_id="r-a"
    )
    await store.mark_submitting("r-a")
    fresh = make_store(path, policy=policy)
    await fresh.initialize()
    record = await fresh.reserve(
        5, kind="scrape", request_fingerprint=fp(2), reservation_id="r-b"
    )
    assert record.utc_day == _DAY
    today = await fresh.usage()
    assert today.used == 0.0
    assert today.active_reserved == 5.0
    past = await fresh.usage(_PAST)
    assert past.used == 5.0
    assert past.active_reserved == 0.0


@pytest.mark.asyncio
async def test_old_reserved_released_on_rollover_without_charge(tmp_path):
    path = tmp_path / "budget.db"
    store = past_store(path)
    await store.initialize()
    await store.reserve(
        7, kind="scrape", request_fingerprint=fp(1), reservation_id="r-a"
    )
    fresh = make_store(path)
    await fresh.initialize()
    await fresh.reserve(
        2, kind="scrape", request_fingerprint=fp(2), reservation_id="r-b"
    )
    released = await fresh.get("r-a")
    assert released.state is ReservationState.RELEASED
    assert released.accounted_credits == 0.0
    past = await fresh.usage(_PAST)
    assert past.used == 0.0
    assert past.active_reserved == 0.0
    with pytest.raises(InvalidBudgetTransition):
        await fresh.mark_submitting("r-a")


@pytest.mark.asyncio
async def test_manual_settle_release_only_adjust_original_day(tmp_path):
    path = tmp_path / "budget.db"
    store = past_store(path)
    await store.initialize()
    plans = (("r-a", 3, 1), ("r-b", 4, 2), ("r-d", 2, 3))
    for rid, estimate, fingerprint in plans:
        await store.reserve(
            estimate,
            kind="scrape",
            request_fingerprint=fp(fingerprint),
            reservation_id=rid,
        )
        await store.mark_submitting(rid)
    await store.accept("r-a")
    await store.mark_indeterminate("r-b")
    await store.mark_indeterminate("r-d")
    fresh = make_store(path)
    await fresh.initialize()
    await fresh.reserve(
        6, kind="scrape", request_fingerprint=fp(4), reservation_id="r-c"
    )
    assert (await fresh.usage(_PAST)).used == 9.0
    settled = await fresh.settle("r-a", 5)
    assert settled.actual_credits == 5.0
    assert settled.accounted_credits == 5.0
    assert (await fresh.usage(_PAST)).used == 11.0
    assert (await fresh.usage()).used == 0.0
    assert await fresh.settle("r-a", 5) == settled
    assert (await fresh.usage(_PAST)).used == 11.0
    fallback = await fresh.settle("r-b")
    assert fallback.actual_credits == 4.0
    assert (await fresh.usage(_PAST)).used == 11.0
    released = await fresh.release("r-d")
    assert released.accounted_credits == 0.0
    assert (await fresh.usage(_PAST)).used == 9.0
    assert await fresh.release("r-d") == released
    assert (await fresh.usage(_PAST)).used == 9.0
    assert (await fresh.usage()).active_reserved == 6.0


@pytest.mark.asyncio
async def test_list_pending_excludes_terminal_and_keeps_accounted(tmp_path):
    path = tmp_path / "budget.db"
    store = past_store(path)
    await store.initialize()
    await store.reserve(
        3, kind="scrape", request_fingerprint=fp(1), reservation_id="r-a"
    )
    await store.mark_submitting("r-a")
    await store.accept("r-a")
    await store.reserve(
        2, kind="scrape", request_fingerprint=fp(2), reservation_id="r-d"
    )
    await store.mark_submitting("r-d")
    await store.mark_indeterminate("r-d")
    fresh = make_store(path)
    await fresh.initialize()
    await fresh.reserve(
        1, kind="scrape", request_fingerprint=fp(3), reservation_id="r-c"
    )
    await fresh.settle("r-a", 2.5)
    pending = await fresh.list_pending()
    assert [r.reservation_id for r in pending] == ["r-d", "r-c"]
    assert pending[0].state is ReservationState.INDETERMINATE
    assert pending[0].accounted_credits == 2.0
    assert pending[1].state is ReservationState.RESERVED
    assert (await fresh.usage(_PAST)).used == 4.5
    assert (await fresh.usage()).active_reserved == 1.0


@pytest.mark.asyncio
async def test_rollover_failure_rolls_back_reserve_and_history(tmp_path):
    path = tmp_path / "budget.db"
    store = past_store(path)
    await store.initialize()
    await store.reserve(
        3, kind="scrape", request_fingerprint=fp(1), reservation_id="r-a"
    )
    await store.mark_submitting("r-a")
    await store.accept("r-a")
    fresh = make_store(path)
    await fresh.initialize()
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TRIGGER fail_rollover BEFORE UPDATE ON reservations"
        " WHEN new.reservation_id = 'r-a'"
        " AND new.accounted_credits > old.accounted_credits"
        " BEGIN SELECT RAISE(ABORT, 'injected rollover failure'); END"
    )
    conn.commit()
    conn.close()
    with pytest.raises(sqlite3.IntegrityError):
        await fresh.reserve(
            6, kind="scrape", request_fingerprint=fp(2), reservation_id="r-c"
        )
    with pytest.raises(KeyError):
        await fresh.get("r-c")
    conn = sqlite3.connect(path)
    accounted = conn.execute(
        "SELECT accounted_credits FROM reservations WHERE reservation_id = 'r-a'"
    ).fetchone()[0]
    used = conn.execute(
        "SELECT used FROM daily_usage WHERE utc_day = ?", (_PAST,)
    ).fetchone()
    conn.close()
    assert accounted == 0.0
    assert used is None
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER fail_rollover")
    conn.commit()
    conn.close()
    record = await fresh.reserve(
        6, kind="scrape", request_fingerprint=fp(3), reservation_id="r-c"
    )
    assert record.estimate == 6
    assert (await fresh.usage(_PAST)).used == 3.0
    assert (await fresh.get("r-a")).accounted_credits == 3.0


@pytest.mark.asyncio
async def test_naive_clock_rejected_before_any_write(tmp_path):
    path = tmp_path / "budget.db"
    store = make_store(path)
    await store.initialize()
    naive = SQLiteStore(path, clock=lambda: datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="timezone-aware"):
        await naive.reserve(
            1, kind="scrape", request_fingerprint=fp(1), reservation_id="r-n"
        )
    with pytest.raises(KeyError):
        await naive.get("r-n")
