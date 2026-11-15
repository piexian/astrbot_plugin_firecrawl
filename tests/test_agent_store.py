"""AgentStore 任务存储测试：创建、受理、终态、通知、列表与回滚。"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from astrbot_plugin_firecrawl.agent.store import AgentStore, AgentTask
from astrbot_plugin_firecrawl.firecrawl_api.agent import MAX_AGENT_PROMPT_CHARS
from astrbot_plugin_firecrawl.firecrawl_api.tasks import make_task_handle

_STAMP = "2026-01-01T12:00:00+00:00"
_TZ8 = timezone(timedelta(hours=8))


def href(n: int) -> str:
    """生成第 n 个合法的 64 位小写 key_ref。"""
    return format(n, "064x")


def agent_handle(task_id: str = "job-1", n: int = 1) -> str:
    """构造 agent 任务句柄。"""
    return make_task_handle("agent", task_id, href(n))


def fixed_clock() -> datetime:
    """返回固定的 UTC 时刻。"""
    return datetime.fromisoformat(_STAMP)


class _TickClock:
    """可手动推进的 timezone-aware clock。"""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> None:
        self._now += timedelta(**kwargs)


def make_store(db_path: str | Path, clock=None) -> AgentStore:
    """在指定路径创建 AgentStore，缺省使用固定 clock。"""
    return AgentStore(db_path, clock=clock or fixed_clock)


async def ready(path: str | Path, clock=None) -> AgentStore:
    """初始化后的 AgentStore。"""
    store = make_store(path, clock=clock)
    await store.initialize()
    return store


def result_json(payload: dict) -> str:
    """与存储层相同的规范 JSON。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)


@pytest.mark.asyncio
async def test_initialize_uses_shared_schema_and_is_idempotent(tmp_path):
    path = tmp_path / "agent.db"
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
    assert version == 3
    assert {
        "daily_usage",
        "reservations",
        "watch_subscriptions",
        "agent_tasks",
    } <= tables


@pytest.mark.asyncio
async def test_create_get_reopen_and_get_by_handle(tmp_path):
    path = tmp_path / "agent.db"
    store = await ready(path)
    record = await store.create(
        reservation_id="r-1",
        umo="  qq:group:1  ",
        prompt="  调研 X  ",
    )
    assert record == AgentTask(
        reservation_id="r-1",
        umo="qq:group:1",
        prompt="调研 X",
        status="submitting",
        task_handle=None,
        result=None,
        credits_used=None,
        error=None,
        notified_at=None,
        created_at=_STAMP,
        updated_at=_STAMP,
    )
    assert await store.get("r-1") == record
    with pytest.raises(KeyError, match="未知任务句柄"):
        await store.get_by_handle(agent_handle())
    with pytest.raises(KeyError, match="未知任务"):
        await store.get("missing")

    handle = agent_handle("job-9")
    accepted = await store.mark_accepted("r-1", handle)
    assert accepted.status == "processing"
    assert accepted.task_handle == handle
    assert await store.get_by_handle(handle) == accepted

    reopened = await ready(path)
    restored = await reopened.get("r-1")
    assert restored == accepted
    assert await reopened.get_by_handle(handle) == accepted


@pytest.mark.asyncio
async def test_create_stores_utc_iso_from_offset_clock(tmp_path):
    def clock() -> datetime:
        return datetime(2026, 1, 1, 20, 0, tzinfo=_TZ8)

    store = await ready(tmp_path / "agent.db", clock=clock)
    record = await store.create(reservation_id="r-tz", umo="u", prompt="p")
    assert record.created_at == "2026-01-01T12:00:00+00:00"
    assert record.updated_at == record.created_at


@pytest.mark.asyncio
async def test_mark_accepted_idempotent_and_conflict(tmp_path):
    clock = _TickClock(datetime.fromisoformat(_STAMP))
    store = await ready(tmp_path / "agent.db", clock=clock)
    await store.create(reservation_id="r-1", umo="u", prompt="p")
    first_handle = agent_handle("job-1", 1)
    first = await store.mark_accepted("r-1", first_handle)
    assert first.status == "processing"
    clock.advance(minutes=5)
    again = await store.mark_accepted("r-1", first_handle)
    assert again == first
    assert again.updated_at == _STAMP
    with pytest.raises(ValueError, match="任务句柄冲突"):
        await store.mark_accepted("r-1", agent_handle("job-2", 2))
    assert (await store.get("r-1")).task_handle == first_handle

    await store.create(reservation_id="r-2", umo="u", prompt="p")
    with pytest.raises(ValueError, match="agent 类型"):
        await store.mark_accepted("r-2", make_task_handle("crawl", "job-1", href(1)))
    with pytest.raises(ValueError, match="agent 类型"):
        await store.mark_accepted("r-2", make_task_handle("batch", "job-1", href(1)))
    with pytest.raises(ValueError, match="任务句柄格式"):
        await store.mark_accepted("r-2", "not-a-handle")
    still = await store.get("r-2")
    assert still.status == "submitting"
    assert still.task_handle is None

    await store.record_status("r-1", "completed", credits_used=1)
    with pytest.raises(ValueError, match="不允许 mark_accepted"):
        await store.mark_accepted("r-1", first_handle)
    with pytest.raises(KeyError, match="未知任务"):
        await store.mark_accepted("missing", first_handle)


@pytest.mark.asyncio
async def test_unique_handle_rejected_without_touching_other_row(tmp_path):
    store = await ready(tmp_path / "agent.db")
    await store.create(reservation_id="r-1", umo="u", prompt="p")
    await store.create(reservation_id="r-2", umo="u", prompt="p")
    handle = agent_handle("job-1")
    first = await store.mark_accepted("r-1", handle)
    with pytest.raises(ValueError, match="已被其他任务使用"):
        await store.mark_accepted("r-2", handle)
    second = await store.get("r-2")
    assert second.status == "submitting"
    assert second.task_handle is None
    assert await store.get("r-1") == first
    other = agent_handle("job-2", 2)
    accepted = await store.mark_accepted("r-2", other)
    assert accepted.task_handle == other
    assert await store.get_by_handle(handle) == first
    assert await store.get_by_handle(other) == accepted


@pytest.mark.asyncio
async def test_record_status_processing_and_terminals(tmp_path):
    clock = _TickClock(datetime.fromisoformat(_STAMP))
    store = await ready(tmp_path / "agent.db", clock=clock)
    await store.create(reservation_id="r-1", umo="u", prompt="p")
    with pytest.raises(ValueError, match="submitting 不允许"):
        await store.record_status("r-1", "processing")
    with pytest.raises(ValueError, match="submitting 不允许"):
        await store.record_status("r-1", "completed", credits_used=1)

    await store.mark_accepted("r-1", agent_handle())
    clock.advance(minutes=1)
    heartbeat = await store.record_status("r-1", "processing")
    assert heartbeat.status == "processing"
    assert heartbeat.result is None
    assert heartbeat.credits_used is None
    assert heartbeat.updated_at == "2026-01-01T12:01:00+00:00"
    with pytest.raises(ValueError, match="processing 不接受 result"):
        await store.record_status("r-1", "processing", result={"a": 1})
    with pytest.raises(ValueError, match="processing 不接受 credits_used"):
        await store.record_status("r-1", "processing", credits_used=0)
    with pytest.raises(ValueError, match="processing 不接受 error"):
        await store.record_status("r-1", "processing", error="x")

    completed = await store.record_status(
        "r-1",
        "completed",
        result={"ok": True},
        credits_used=2,
    )
    assert completed.status == "completed"
    assert completed.result == {"ok": True}
    assert completed.credits_used == 2.0
    clock.advance(minutes=1)
    same = await store.record_status(
        "r-1",
        "completed",
        result={"ok": True},
        credits_used=2,
    )
    assert same == completed
    assert same.updated_at == completed.updated_at
    with pytest.raises(ValueError, match="状态 failed"):
        await store.record_status("r-1", "failed", credits_used=2, error="x")
    with pytest.raises(ValueError, match="费用"):
        await store.record_status(
            "r-1", "completed", result={"ok": True}, credits_used=3
        )
    with pytest.raises(ValueError, match="结果"):
        await store.record_status(
            "r-1", "completed", result={"ok": False}, credits_used=2
        )
    with pytest.raises(ValueError, match="不允许从终态回到 processing"):
        await store.record_status("r-1", "processing")
    with pytest.raises(KeyError, match="未知任务"):
        await store.record_status("missing", "processing")

    await store.create(reservation_id="r-fail", umo="u", prompt="p")
    await store.mark_accepted("r-fail", agent_handle("fail-1", 3))
    failed = await store.record_status("r-fail", "failed", credits_used=0, error="boom")
    assert failed.status == "failed"
    assert failed.result is None
    assert failed.error == "boom"
    assert failed.credits_used == 0.0

    await store.create(reservation_id="r-cancel", umo="u", prompt="p")
    await store.mark_accepted("r-cancel", agent_handle("cancel-1", 4))
    cancelled = await store.record_status("r-cancel", "cancelled", credits_used=1.5)
    assert cancelled.status == "cancelled"
    assert cancelled.result is None
    assert cancelled.error is None
    assert cancelled.credits_used == 1.5


@pytest.mark.asyncio
async def test_credits_zero_fractional_and_over_estimate(tmp_path):
    store = await ready(tmp_path / "agent.db")
    cases = (("r-0", 0, 0.0), ("r-frac", 0.25, 0.25), ("r-over", 99999, 99999.0))
    for reservation_id, raw, expected in cases:
        await store.create(reservation_id=reservation_id, umo="u", prompt="p")
        await store.mark_accepted(reservation_id, agent_handle(reservation_id, 5))
        record = await store.record_status(
            reservation_id, "completed", credits_used=raw
        )
        assert record.credits_used == expected
        assert type(record.credits_used) is float

    await store.create(reservation_id="r-bad", umo="u", prompt="p")
    await store.mark_accepted("r-bad", agent_handle("bad", 6))
    for bad in (True, False, -1, math.inf, math.nan, "1", None, object()):
        with pytest.raises(ValueError, match="credits_used"):
            await store.record_status("r-bad", "completed", credits_used=bad)
    assert (await store.get("r-bad")).status == "processing"


@pytest.mark.asyncio
async def test_result_json_roundtrip_and_rejects_invalid(tmp_path):
    path = tmp_path / "agent.db"
    store = await ready(path)
    payload = {
        "标题": "内容",
        "items": [{"id": 1}, {"id": 2}],
        "meta": {"ok": True, "n": 1.5},
        "empty": {},
    }
    await store.create(reservation_id="r-1", umo="u", prompt="p")
    await store.mark_accepted("r-1", agent_handle())
    record = await store.record_status(
        "r-1", "completed", result=payload, credits_used=1
    )
    assert record.result == {
        "empty": {},
        "items": [{"id": 1}, {"id": 2}],
        "meta": {"n": 1.5, "ok": True},
        "标题": "内容",
    }
    conn = sqlite3.connect(path)
    stored = conn.execute(
        "SELECT result_json FROM agent_tasks WHERE reservation_id = 'r-1'"
    ).fetchone()[0]
    conn.close()
    assert stored == result_json(payload)
    reopened = await ready(path)
    assert (await reopened.get("r-1")).result == record.result

    await store.create(reservation_id="r-empty", umo="u", prompt="p")
    await store.mark_accepted("r-empty", agent_handle("empty", 7))
    empty = await store.record_status("r-empty", "completed", result={}, credits_used=0)
    assert empty.result == {}

    await store.create(reservation_id="r-none", umo="u", prompt="p")
    await store.mark_accepted("r-none", agent_handle("none", 8))
    none = await store.record_status("r-none", "completed", credits_used=0)
    assert none.result is None

    await store.create(reservation_id="r-bad", umo="u", prompt="p")
    await store.mark_accepted("r-bad", agent_handle("bad-json", 9))
    with pytest.raises(ValueError, match="无法 JSON 序列化"):
        await store.record_status(
            "r-bad",
            "completed",
            result={"x": float("nan")},
            credits_used=1,
        )
    with pytest.raises(ValueError, match="无法 JSON 序列化"):
        await store.record_status(
            "r-bad",
            "completed",
            result={"x": datetime(2026, 1, 1)},
            credits_used=1,
        )
    with pytest.raises(ValueError, match="JSON 对象"):
        await store.record_status(
            "r-bad",
            "completed",
            result=["not", "object"],  # type: ignore[arg-type]
            credits_used=1,
        )
    assert (await store.get("r-bad")).status == "processing"
    assert (await store.get("r-bad")).result is None


@pytest.mark.asyncio
async def test_notification_idempotent(tmp_path):
    clock = _TickClock(datetime.fromisoformat(_STAMP))
    store = await ready(tmp_path / "agent.db", clock=clock)
    await store.create(reservation_id="r-1", umo="u", prompt="p")
    with pytest.raises(ValueError, match="仅终态"):
        await store.mark_notification_sent("r-1")
    await store.mark_accepted("r-1", agent_handle())
    with pytest.raises(ValueError, match="仅终态"):
        await store.mark_notification_sent("r-1")
    await store.record_status("r-1", "completed", credits_used=1)
    clock.advance(minutes=3)
    first = await store.mark_notification_sent("r-1")
    assert first.notified_at == "2026-01-01T12:03:00+00:00"
    clock.advance(minutes=3)
    again = await store.mark_notification_sent("r-1")
    assert again == first
    assert again.notified_at == first.notified_at
    with pytest.raises(KeyError, match="未知任务"):
        await store.mark_notification_sent("missing")


@pytest.mark.asyncio
async def test_list_active_and_unnotified_terminal(tmp_path):
    store = await ready(tmp_path / "agent.db")
    await store.create(reservation_id="r-z", umo="u", prompt="p")
    await store.create(reservation_id="r-a", umo="u", prompt="p")
    await store.create(reservation_id="r-m", umo="u", prompt="p")
    await store.create(reservation_id="r-done", umo="u", prompt="p")
    active = await store.list_active()
    assert [row.reservation_id for row in active] == ["r-a", "r-done", "r-m", "r-z"]
    assert all(row.status == "submitting" for row in active)
    assert await store.list_unnotified_terminal() == []

    await store.mark_accepted("r-m", agent_handle("m", 1))
    await store.mark_accepted("r-done", agent_handle("done", 2))
    await store.record_status("r-done", "completed", credits_used=1)
    await store.mark_accepted("r-z", agent_handle("z", 3))
    await store.record_status("r-z", "failed", credits_used=0, error="x")
    await store.create(reservation_id="r-c", umo="u", prompt="p")
    await store.mark_accepted("r-c", agent_handle("c", 4))
    await store.record_status("r-c", "cancelled", credits_used=2)

    active = await store.list_active()
    assert [row.reservation_id for row in active] == ["r-a", "r-m"]
    assert [row.status for row in active] == ["submitting", "processing"]

    unnotified = await store.list_unnotified_terminal()
    assert [row.reservation_id for row in unnotified] == ["r-c", "r-done", "r-z"]
    await store.mark_notification_sent("r-done")
    unnotified = await store.list_unnotified_terminal()
    assert [row.reservation_id for row in unnotified] == ["r-c", "r-z"]


@pytest.mark.asyncio
async def test_delete_unaccepted_guards(tmp_path):
    store = await ready(tmp_path / "agent.db")
    await store.create(reservation_id="r-keep", umo="u", prompt="p")
    await store.create(reservation_id="r-del", umo="u", prompt="p")
    await store.delete_unaccepted("r-del")
    with pytest.raises(KeyError, match="未知任务"):
        await store.get("r-del")
    with pytest.raises(KeyError, match="未知任务"):
        await store.delete_unaccepted("r-del")
    assert (await store.get("r-keep")).status == "submitting"

    await store.mark_accepted("r-keep", agent_handle())
    with pytest.raises(ValueError, match="未受理"):
        await store.delete_unaccepted("r-keep")
    assert (await store.get("r-keep")).status == "processing"

    await store.record_status("r-keep", "completed", credits_used=1)
    with pytest.raises(ValueError, match="未受理"):
        await store.delete_unaccepted("r-keep")
    assert (await store.get("r-keep")).status == "completed"

    for status, reservation_id, n in (
        ("failed", "r-fail", 8),
        ("cancelled", "r-cancel", 9),
    ):
        await store.create(reservation_id=reservation_id, umo="u", prompt="p")
        await store.mark_accepted(reservation_id, agent_handle(reservation_id, n))
        kwargs = {"credits_used": 1}
        if status == "failed":
            kwargs["error"] = "x"
        await store.record_status(reservation_id, status, **kwargs)
        with pytest.raises(ValueError, match="未受理"):
            await store.delete_unaccepted(reservation_id)


@pytest.mark.asyncio
async def test_illegal_status_params_and_naive_clock(tmp_path):
    path = tmp_path / "agent.db"
    store = await ready(path)
    with pytest.raises(ValueError, match="reservation_id"):
        await store.create(reservation_id="", umo="u", prompt="p")
    with pytest.raises(ValueError, match="umo"):
        await store.create(reservation_id="r-1", umo="  ", prompt="p")
    with pytest.raises(ValueError, match="prompt 不能为空"):
        await store.create(reservation_id="r-1", umo="u", prompt="  \n")
    with pytest.raises(ValueError, match="prompt 必须是字符串"):
        await store.create(reservation_id="r-1", umo="u", prompt=1)  # type: ignore[arg-type]
    padded = "  " + "a" * MAX_AGENT_PROMPT_CHARS + "\n"
    created = await store.create(reservation_id="r-long", umo="u", prompt=padded)
    assert created.prompt == "a" * MAX_AGENT_PROMPT_CHARS
    with pytest.raises(ValueError, match="最多"):
        await store.create(
            reservation_id="r-too-long",
            umo="u",
            prompt="a" * (MAX_AGENT_PROMPT_CHARS + 1),
        )
    duplicate = await store.create(reservation_id="r-dup", umo="u", prompt="p")
    with pytest.raises(ValueError, match="已存在"):
        await store.create(reservation_id="r-dup", umo="other", prompt="other")
    assert await store.get("r-dup") == duplicate
    with pytest.raises(ValueError, match="status 必须是"):
        await store.record_status("r-dup", "submitting")
    with pytest.raises(ValueError, match="status 必须是"):
        await store.record_status("r-dup", "done")
    with pytest.raises(ValueError, match="error 必须是字符串"):
        await store.record_status(
            "r-dup",
            "failed",
            credits_used=1,
            error=1,  # type: ignore[arg-type]
        )

    naive = make_store(path, clock=lambda: datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="timezone-aware"):
        await naive.create(reservation_id="r-naive", umo="u", prompt="p")
    with pytest.raises(KeyError):
        await store.get("r-naive")
    await store.create(reservation_id="r-n", umo="u", prompt="p")
    handle = agent_handle("naive", 11)
    with pytest.raises(ValueError, match="timezone-aware"):
        await naive.mark_accepted("r-n", handle)
    assert (await store.get("r-n")).status == "submitting"
    await store.mark_accepted("r-n", handle)
    with pytest.raises(ValueError, match="timezone-aware"):
        await naive.record_status("r-n", "completed", credits_used=1)
    assert (await store.get("r-n")).status == "processing"
    await store.record_status("r-n", "completed", credits_used=1)
    with pytest.raises(ValueError, match="timezone-aware"):
        await naive.mark_notification_sent("r-n")
    assert (await store.get("r-n")).notified_at is None
    await store.create(reservation_id="r-del", umo="u", prompt="p")
    with pytest.raises(ValueError, match="timezone-aware"):
        await naive.delete_unaccepted("r-del")
    assert (await store.get("r-del")).status == "submitting"


@pytest.mark.asyncio
async def test_write_failure_rolls_back(tmp_path):
    path = tmp_path / "agent.db"
    store = await ready(path)
    first = await store.create(reservation_id="r-ok", umo="u", prompt="p")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TRIGGER fail_insert BEFORE INSERT ON agent_tasks"
        " WHEN new.reservation_id = 'r-fail'"
        " BEGIN SELECT RAISE(ABORT, 'injected insert failure'); END"
    )
    conn.execute(
        "CREATE TRIGGER fail_update BEFORE UPDATE ON agent_tasks"
        " BEGIN SELECT RAISE(ABORT, 'injected update failure'); END"
    )
    conn.commit()
    conn.close()
    with pytest.raises(sqlite3.IntegrityError):
        await store.create(reservation_id="r-fail", umo="u2", prompt="p2")
    with pytest.raises(KeyError):
        await store.get("r-fail")
    assert await store.get("r-ok") == first
    with pytest.raises(sqlite3.IntegrityError):
        await store.mark_accepted("r-ok", agent_handle())
    still = await store.get("r-ok")
    assert still.status == "submitting"
    assert still.task_handle is None
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER fail_insert")
    conn.execute("DROP TRIGGER fail_update")
    conn.commit()
    conn.close()
    accepted = await store.mark_accepted("r-ok", agent_handle())
    assert accepted.status == "processing"
    assert accepted.task_handle == agent_handle()
