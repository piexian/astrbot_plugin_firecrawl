"""SQLite 存储：预算账本与 watch 订阅；schema 原子迁移、短事务与 UTC 时间处理。"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .firecrawl_api.budget import (
    BudgetPolicy,
    Reservation,
    ReservationState,
    utc_day,
    validate_transition,
)
from .firecrawl_api.domains import normalize_url

_SCHEMA_VERSION = 2
_TERMINAL_STATES = (ReservationState.SETTLED.value, ReservationState.RELEASED.value)
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")

_COLUMNS = (
    "reservation_id, utc_day, estimate, kind, request_fingerprint,"
    " created_at, updated_at, state, key_ref, job_id, actual_credits,"
    " accounted_credits"
)

_DDL_DAILY_USAGE = (
    "CREATE TABLE IF NOT EXISTS daily_usage ("
    " utc_day TEXT PRIMARY KEY,"
    " used REAL NOT NULL CHECK (used >= 0)"
    ")"
)

_DDL_RESERVATIONS = (
    "CREATE TABLE IF NOT EXISTS reservations ("
    " reservation_id TEXT PRIMARY KEY,"
    " utc_day TEXT NOT NULL,"
    " estimate INTEGER NOT NULL CHECK (typeof(estimate) = 'integer' AND estimate >= 0),"
    " kind TEXT NOT NULL,"
    " request_fingerprint TEXT NOT NULL,"
    " created_at TEXT NOT NULL,"
    " updated_at TEXT NOT NULL,"
    " state TEXT NOT NULL CHECK (state IN ('reserved','submitting','accepted',"
    " 'indeterminate','settled','released')),"
    " key_ref TEXT,"
    " job_id TEXT,"
    " actual_credits REAL CHECK (actual_credits IS NULL OR actual_credits >= 0),"
    " accounted_credits REAL NOT NULL CHECK (accounted_credits >= 0)"
    ")"
)

_DDL_PENDING_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_reservations_pending ON reservations (utc_day)"
    " WHERE state NOT IN ('settled', 'released')"
)

_MIN_INTERVAL_MINUTES = 5

_DDL_WATCH_SUBSCRIPTIONS = (
    "CREATE TABLE IF NOT EXISTS watch_subscriptions ("
    " id TEXT PRIMARY KEY,"
    " umo TEXT NOT NULL,"
    " url TEXT NOT NULL,"
    " interval_minutes INTEGER NOT NULL CHECK ("
    f" typeof(interval_minutes) = 'integer' AND interval_minutes >= {_MIN_INTERVAL_MINUTES}),"
    " tag TEXT NOT NULL,"
    " cron_job_id TEXT,"
    " next_check_at TEXT NOT NULL,"
    " last_status TEXT,"
    " last_notification_hash TEXT,"
    " created_at TEXT NOT NULL,"
    " updated_at TEXT NOT NULL,"
    " UNIQUE (umo, url),"
    " UNIQUE (tag)"
    ")"
)

_MIGRATIONS = {
    1: (_DDL_DAILY_USAGE, _DDL_RESERVATIONS, _DDL_PENDING_INDEX),
    2: (_DDL_WATCH_SUBSCRIPTIONS,),
}
_INSERT_RESERVATION = (
    f"INSERT INTO reservations ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

_SET_COLUMNS = (
    "utc_day = ?, estimate = ?, kind = ?, request_fingerprint = ?,"
    " created_at = ?, updated_at = ?, state = ?, key_ref = ?, job_id = ?,"
    " actual_credits = ?, accounted_credits = ?"
)

_UPDATE_RESERVATION = f"UPDATE reservations SET {_SET_COLUMNS} WHERE reservation_id = ?"


def _utc_now() -> datetime:
    """默认 clock：当前 UTC 时刻。"""
    return datetime.now(timezone.utc)


def _require_id(reservation_id: str | None) -> str:
    """显式 ID 必须是非空字符串，缺省生成新 UUID。"""
    if reservation_id is None:
        return str(uuid.uuid4())
    if not isinstance(reservation_id, str) or not reservation_id:
        raise ValueError(f"reservation_id 必须是非空字符串，收到 {reservation_id!r}")
    return reservation_id


def _require_fingerprint(fingerprint: str) -> None:
    """指纹必须是 64 位小写 SHA-256 十六进制串。"""
    if not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint):
        raise ValueError(
            "request_fingerprint 必须是 64 位小写 SHA-256 十六进制串，"
            f"收到 {fingerprint!r}"
        )


def _require_key_ref(key_ref: str | None) -> None:
    """key_ref 只能为 None 或 64 位小写 SHA-256 摘要，异常不回显传入值。"""
    if key_ref is None:
        return
    if not isinstance(key_ref, str) or not _FINGERPRINT.fullmatch(key_ref):
        raise ValueError("key_ref 必须是 64 位小写 SHA-256 十六进制摘要或 None")


def _require_job_id(job_id: str | None) -> None:
    """job_id 缺省为 None，显式提供时必须是非空字符串。"""
    if job_id is None:
        return
    if not isinstance(job_id, str) or not job_id:
        raise ValueError(f"job_id 必须是非空字符串，收到 {job_id!r}")


def _read_usage(conn: sqlite3.Connection, day: str) -> tuple[float, float]:
    """读取某 UTC 日的已用量与非终态预留净额。"""
    row = conn.execute(
        "SELECT used FROM daily_usage WHERE utc_day = ?", (day,)
    ).fetchone()
    used = float(row[0]) if row else 0.0
    row = conn.execute(
        "SELECT COALESCE(SUM(estimate - accounted_credits), 0) FROM reservations"
        " WHERE utc_day = ? AND state NOT IN (?, ?)",
        (day, *_TERMINAL_STATES),
    ).fetchone()
    return used, float(row[0])


def _reservation_row(record: Reservation) -> tuple:
    """按存储列顺序展开预留记录。"""
    return (
        record.reservation_id,
        record.utc_day,
        record.estimate,
        record.kind,
        record.request_fingerprint,
        record.created_at,
        record.updated_at,
        record.state.value,
        record.key_ref,
        record.job_id,
        record.actual_credits,
        record.accounted_credits,
    )


def _reservation_from_row(row: tuple) -> Reservation:
    """由存储行还原预留记录并触发字段校验。"""
    return Reservation(
        reservation_id=row[0],
        utc_day=row[1],
        estimate=row[2],
        kind=row[3],
        request_fingerprint=row[4],
        created_at=row[5],
        updated_at=row[6],
        state=ReservationState(row[7]),
        key_ref=row[8],
        job_id=row[9],
        actual_credits=row[10],
        accounted_credits=row[11],
    )


def _apply_usage_delta(conn: sqlite3.Connection, day: str, delta: float) -> None:
    """先确保当日存在 used=0 行，再以 UPDATE 应用正负增量，避免负增量先触发 CHECK。"""
    conn.execute(
        "INSERT OR IGNORE INTO daily_usage (utc_day, used) VALUES (?, 0)", (day,)
    )
    conn.execute(
        "UPDATE daily_usage SET used = used + ? WHERE utc_day = ?", (delta, day)
    )


def _mutate_submitting(key_ref: str | None, confirmed_not_accepted: bool):
    """reserved→submitting；submitting 中仅在明确未受理确认下改记 key_ref。"""

    def mutate(old, stamp):
        reattempt = old.state is ReservationState.SUBMITTING and confirmed_not_accepted
        if not reattempt:
            validate_transition(old.state, ReservationState.SUBMITTING)
        record = replace(
            old,
            state=old.state if reattempt else ReservationState.SUBMITTING,
            key_ref=key_ref if key_ref is not None else old.key_ref,
            updated_at=stamp,
        )
        return record, 0.0

    return mutate


def _mutate_accept(job_id: str | None):
    """submitting→accepted，缺省保留已有 job_id。"""

    def mutate(old, stamp):
        validate_transition(old.state, ReservationState.ACCEPTED)
        record = replace(
            old,
            state=ReservationState.ACCEPTED,
            job_id=job_id if job_id is not None else old.job_id,
            updated_at=stamp,
        )
        return record, 0.0

    return mutate


def _mutate_indeterminate():
    """submitting→indeterminate，保留元数据。"""

    def mutate(old, stamp):
        validate_transition(old.state, ReservationState.INDETERMINATE)
        record = replace(old, state=ReservationState.INDETERMINATE, updated_at=stamp)
        return record, 0.0

    return mutate


def _mutate_settle(actual_credits: float | None):
    """accepted/indeterminate→settled：缺省按 estimate，同值幂等，冲突拒绝。"""

    def mutate(old, stamp):
        if old.state is ReservationState.SETTLED:
            if actual_credits is None:
                return old, 0.0
            probe = replace(old, actual_credits=actual_credits)
            if probe.actual_credits == old.actual_credits:
                return old, 0.0
            raise ValueError(
                f"实际费用 {probe.actual_credits} 与已结算值 {old.actual_credits} 冲突"
            )
        validate_transition(old.state, ReservationState.SETTLED)
        actual = actual_credits if actual_credits is not None else old.estimate
        record = replace(
            old,
            state=ReservationState.SETTLED,
            actual_credits=actual,
            accounted_credits=actual,
            updated_at=stamp,
        )
        return record, float(actual) - old.accounted_credits

    return mutate


def _mutate_release(confirmed_not_accepted: bool):
    """按状态图释放并清零账目，已 released 幂等，submitting 需确认。"""

    def mutate(old, stamp):
        if old.state is ReservationState.RELEASED:
            return old, 0.0
        validate_transition(
            old.state,
            ReservationState.RELEASED,
            confirmed_not_accepted=confirmed_not_accepted,
        )
        record = replace(
            old,
            state=ReservationState.RELEASED,
            actual_credits=0.0,
            accounted_credits=0.0,
            updated_at=stamp,
        )
        return record, -old.accounted_credits

    return mutate


def _utc_stamp(now: datetime) -> str:
    """新时间戳统一换算为 UTC ISO 字符串。"""
    return now.astimezone(timezone.utc).isoformat()


def _migrate_sync(db_path: Path) -> None:
    """按 user_version 逐步迁移到最新 schema，重复执行幂等，更高版本拒绝。"""
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > _SCHEMA_VERSION:
            raise ValueError(
                f"数据库 schema 版本 {version} 高于支持的 {_SCHEMA_VERSION}，拒绝初始化"
            )
        if version < _SCHEMA_VERSION:
            for step in range(version + 1, _SCHEMA_VERSION + 1):
                for ddl in _MIGRATIONS[step]:
                    conn.execute(ddl)
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        conn.commit()
        conn.execute("PRAGMA journal_mode=WAL")
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _select_pending_rows(conn: sqlite3.Connection) -> list[tuple]:
    """按 utc_day 与 ID 稳定排序读取全部非终态记录行。"""
    return conn.execute(
        f"SELECT {_COLUMNS} FROM reservations"
        " WHERE state NOT IN (?, ?) ORDER BY utc_day, reservation_id",
        (*_TERMINAL_STATES,),
    ).fetchall()


def _update_reservation(conn: sqlite3.Connection, record: Reservation) -> None:
    """按存储列写回预留记录。"""
    conn.execute(
        _UPDATE_RESERVATION,
        _reservation_row(record)[1:] + (record.reservation_id,),
    )


def _rollover(conn: sqlite3.Connection, now: datetime) -> None:
    """UTC 日切：旧 reserved 释放不计费，旧提交中/已受理/不确定按预估入账一次。"""
    current = utc_day(now)
    stamp = _utc_stamp(now)
    for row in conn.execute(
        f"SELECT {_COLUMNS} FROM reservations"
        " WHERE utc_day < ? AND state NOT IN (?, ?)"
        " ORDER BY utc_day, reservation_id",
        (current, *_TERMINAL_STATES),
    ).fetchall():
        old = _reservation_from_row(row)
        if old.state is ReservationState.RESERVED:
            record, delta = _mutate_release(False)(old, stamp)
        else:
            delta = old.estimate - old.accounted_credits
            if not delta:
                continue
            record = replace(
                old, accounted_credits=float(old.estimate), updated_at=stamp
            )
        if delta:
            _apply_usage_delta(conn, old.utc_day, delta)
        _update_reservation(conn, record)


@dataclass(frozen=True)
class UsageSnapshot:
    """某 UTC 日的已用量与活动预留。"""

    utc_day: str
    used: float
    active_reserved: float


class SQLiteStore:
    """预算账本存储：每次操作独立连接的短事务，写操作用 BEGIN IMMEDIATE。"""

    def __init__(
        self,
        db_path: str | Path,
        policy: BudgetPolicy = BudgetPolicy(),
        clock: Callable[[], datetime] = _utc_now,
    ):
        self._db_path = Path(db_path)
        self._policy = policy
        self._clock = clock

    async def initialize(self) -> None:
        """按 user_version 逐步迁移 schema（当前 v2），幂等，更高版本拒绝。"""
        await asyncio.to_thread(_migrate_sync, self._db_path)

    async def reserve(
        self,
        estimate: int,
        *,
        kind: str,
        request_fingerprint: str,
        reservation_id: str | None = None,
    ) -> Reservation:
        """原子预留：同事务读取当日用量、按策略校验后写入 RESERVED 记录。"""
        _require_fingerprint(request_fingerprint)
        reservation_id = _require_id(reservation_id)
        return await asyncio.to_thread(
            self._reserve_sync, estimate, kind, request_fingerprint, reservation_id
        )

    async def get(self, reservation_id: str) -> Reservation:
        """按 ID 读取预留记录，未知 ID 抛 KeyError。"""
        return await asyncio.to_thread(self._get_sync, reservation_id)

    async def usage(self, day: str | None = None) -> UsageSnapshot:
        """返回某 UTC 日的 used 与 active_reserved，缺省当前日，无记录返回 0。"""
        return await asyncio.to_thread(self._usage_sync, day)

    async def mark_submitting(
        self,
        reservation_id: str,
        key_ref: str | None = None,
        *,
        confirmed_not_accepted: bool = False,
    ) -> Reservation:
        """reserved→submitting 并先持久化；submitting 中仅明确未受理后改记 key_ref。"""
        _require_key_ref(key_ref)
        return await asyncio.to_thread(
            self._mutate_sync,
            reservation_id,
            _mutate_submitting(key_ref, confirmed_not_accepted),
        )

    async def accept(
        self, reservation_id: str, job_id: str | None = None
    ) -> Reservation:
        """submitting→accepted，可无 job_id，保留已有 key_ref。"""
        _require_job_id(job_id)
        return await asyncio.to_thread(
            self._mutate_sync, reservation_id, _mutate_accept(job_id)
        )

    async def mark_indeterminate(self, reservation_id: str) -> Reservation:
        """submitting→indeterminate，保留元数据。"""
        return await asyncio.to_thread(
            self._mutate_sync, reservation_id, _mutate_indeterminate()
        )

    async def settle(
        self, reservation_id: str, actual_credits: float | None = None
    ) -> Reservation:
        """结算入账：缺省按 estimate，实际超预算仍如实记账，同值幂等。"""
        return await asyncio.to_thread(
            self._mutate_sync, reservation_id, _mutate_settle(actual_credits)
        )

    async def release(
        self, reservation_id: str, *, confirmed_not_accepted: bool = False
    ) -> Reservation:
        """按状态图释放并退回已入账金额，已 released 幂等。"""
        return await asyncio.to_thread(
            self._mutate_sync, reservation_id, _mutate_release(confirmed_not_accepted)
        )

    async def recover(self) -> list[Reservation]:
        """启动恢复：释放未发送、提交中转不确定，日切入账后返回待决记录。"""
        return await asyncio.to_thread(self._recover_sync)

    async def list_pending(self) -> list[Reservation]:
        """只读返回全部非终态预留，按 utc_day 与 ID 稳定排序。"""
        return await asyncio.to_thread(self._list_pending_sync)

    def _reserve_sync(
        self, estimate: int, kind: str, request_fingerprint: str, reservation_id: str
    ) -> Reservation:
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = self._clock()
            _rollover(conn, now)
            day = utc_day(now)
            stamp = _utc_stamp(now)
            used, active = _read_usage(conn, day)
            self._policy.check(estimate, used, active)
            row = conn.execute(
                "SELECT reservation_id FROM reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            if row is not None:
                raise ValueError(f"预留 ID 已存在：{reservation_id}")
            record = Reservation(
                reservation_id=reservation_id,
                utc_day=day,
                estimate=estimate,
                kind=kind,
                request_fingerprint=request_fingerprint,
                created_at=stamp,
                updated_at=stamp,
                state=ReservationState.RESERVED,
            )
            conn.execute(_INSERT_RESERVATION, _reservation_row(record))
            conn.commit()
            return record
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _get_sync(self, reservation_id: str) -> Reservation:
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        try:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise KeyError(f"未知预留：{reservation_id}")
        return _reservation_from_row(row)

    def _usage_sync(self, day: str | None) -> UsageSnapshot:
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        try:
            target = day if day is not None else utc_day(self._clock())
            conn.execute("BEGIN")
            try:
                used, active = _read_usage(conn, target)
                return UsageSnapshot(target, used, active)
            finally:
                conn.rollback()
        finally:
            conn.close()

    def _recover_sync(self) -> list[Reservation]:
        """独立写事务中迁移状态、日切入账并返回恢复后的待决记录。"""
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = self._clock()
            _rollover(conn, now)
            stamp = _utc_stamp(now)
            for row in _select_pending_rows(conn):
                old = _reservation_from_row(row)
                if old.state is ReservationState.RESERVED:
                    record, delta = _mutate_release(False)(old, stamp)
                elif old.state is ReservationState.SUBMITTING:
                    record, delta = _mutate_indeterminate()(old, stamp)
                else:
                    continue
                if delta:
                    _apply_usage_delta(conn, record.utc_day, delta)
                _update_reservation(conn, record)
            pending = [_reservation_from_row(r) for r in _select_pending_rows(conn)]
            conn.commit()
            return pending
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _list_pending_sync(self) -> list[Reservation]:
        """只读读取全部非终态记录。"""
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        try:
            return [_reservation_from_row(r) for r in _select_pending_rows(conn)]
        finally:
            conn.close()

    def _mutate_sync(
        self,
        reservation_id: str,
        mutation: Callable[[Reservation, str], tuple[Reservation, float]],
    ) -> Reservation:
        """独立写事务中读旧行、应用迁移与用量增量并提交，异常回滚上抛。"""
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = self._clock()
            _rollover(conn, now)
            stamp = _utc_stamp(now)
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"未知预留：{reservation_id}")
            record, delta = mutation(_reservation_from_row(row), stamp)
            if delta:
                _apply_usage_delta(conn, record.utc_day, delta)
            _update_reservation(conn, record)
            conn.commit()
            return record
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


@dataclass(frozen=True)
class WatchSubscription:
    """一个 watch 订阅：绑定会话、规范化 URL、检查周期与调度元数据。"""

    id: str
    umo: str
    url: str
    interval_minutes: int
    tag: str
    cron_job_id: str | None
    next_check_at: str
    last_status: str | None
    last_notification_hash: str | None
    created_at: str
    updated_at: str


_WATCH_COLUMNS = (
    "id, umo, url, interval_minutes, tag, cron_job_id, next_check_at,"
    " last_status, last_notification_hash, created_at, updated_at"
)

_WATCH_SET_COLUMNS = (
    "umo = ?, url = ?, interval_minutes = ?, tag = ?, cron_job_id = ?,"
    " next_check_at = ?, last_status = ?, last_notification_hash = ?,"
    " created_at = ?, updated_at = ?"
)

_INSERT_WATCH_SUBSCRIPTION = (
    "INSERT INTO watch_subscriptions"
    " (id, umo, url, interval_minutes, tag, cron_job_id, next_check_at,"
    "  last_status, last_notification_hash, created_at, updated_at)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def _require_text(value: object, name: str) -> str:
    """非空文本参数，去除首尾空白。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空字符串")
    return value.strip()


def _require_optional_text(value: object, name: str) -> str | None:
    """None 或非空文本参数。"""
    if value is None:
        return None
    return _require_text(value, name)


def _require_interval(value: object) -> int:
    """检查周期必须是整数且至少 5 分钟。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"interval_minutes 必须是整数，收到 {value!r}")
    if value < _MIN_INTERVAL_MINUTES:
        raise ValueError(
            f"interval_minutes 必须是 >= {_MIN_INTERVAL_MINUTES} 的整数，收到 {value}"
        )
    return value


def _require_notification_hash(value: object) -> None:
    """notification_hash 必须是 64 位小写 SHA-256 十六进制串。"""
    if not isinstance(value, str) or not _FINGERPRINT.fullmatch(value):
        raise ValueError("notification_hash 必须是 64 位小写 SHA-256 十六进制串")


def _require_aware(now: datetime) -> datetime:
    """clock 输出必须是 timezone-aware datetime，与预算存储一致。"""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now 必须是 timezone-aware datetime")
    return now


def _watch_row(record: WatchSubscription) -> tuple:
    """按存储列顺序展开订阅记录。"""
    return (
        record.id,
        record.umo,
        record.url,
        record.interval_minutes,
        record.tag,
        record.cron_job_id,
        record.next_check_at,
        record.last_status,
        record.last_notification_hash,
        record.created_at,
        record.updated_at,
    )


def _watch_from_row(row: tuple) -> WatchSubscription:
    """由存储行还原订阅记录。"""
    return WatchSubscription(
        id=row[0],
        umo=row[1],
        url=row[2],
        interval_minutes=row[3],
        tag=row[4],
        cron_job_id=row[5],
        next_check_at=row[6],
        last_status=row[7],
        last_notification_hash=row[8],
        created_at=row[9],
        updated_at=row[10],
    )


class WatchStore:
    """watch 订阅存储：每次操作独立连接的短事务，写操作用 BEGIN IMMEDIATE。"""

    def __init__(
        self,
        db_path: str | Path,
        clock: Callable[[], datetime] = _utc_now,
    ):
        self._db_path = Path(db_path)
        self._clock = clock

    async def initialize(self) -> None:
        """按 user_version 逐步迁移 schema（当前 v2），幂等，更高版本拒绝。"""
        await asyncio.to_thread(_migrate_sync, self._db_path)

    async def create(
        self, *, umo: str, url: str, interval_minutes: int, tag: str
    ) -> WatchSubscription:
        """创建订阅：URL 经规范化校验，(umo, url) 与 tag 唯一，首次检查即时到期。"""
        cleaned_umo = _require_text(umo, "umo")
        cleaned_url = normalize_url(url)
        cleaned_tag = _require_text(tag, "tag")
        interval = _require_interval(interval_minutes)
        return await asyncio.to_thread(
            self._create_sync, cleaned_umo, cleaned_url, interval, cleaned_tag
        )

    async def get(self, subscription_id: str) -> WatchSubscription:
        """按 ID 读取订阅，未知 ID 抛 KeyError。"""
        return await asyncio.to_thread(self._get_sync, subscription_id)

    async def list_all(self) -> list[WatchSubscription]:
        """只读返回全部订阅，按创建时间与 ID 稳定排序。"""
        return await asyncio.to_thread(self._list_all_sync)

    async def delete(self, subscription_id: str) -> None:
        """删除订阅；未知 ID 抛 KeyError。"""
        await asyncio.to_thread(self._delete_sync, subscription_id)

    async def set_cron_job_id(
        self, subscription_id: str, cron_job_id: str | None
    ) -> WatchSubscription:
        """绑定或清除调度 job_id；显式提供时必须是非空字符串。"""
        _require_job_id(cron_job_id)

        def mutate(old: WatchSubscription, stamp: str, now: datetime):
            return replace(old, cron_job_id=cron_job_id, updated_at=stamp)

        return await asyncio.to_thread(self._mutate_watch, subscription_id, mutate)

    async def record_check(
        self, subscription_id: str, status: str | None
    ) -> WatchSubscription:
        """完成一次检查：记录状态并按订阅周期推进 next_check_at。"""
        cleaned = _require_optional_text(status, "status")

        def mutate(old: WatchSubscription, stamp: str, now: datetime):
            return replace(
                old,
                last_status=cleaned,
                next_check_at=_utc_stamp(now + timedelta(minutes=old.interval_minutes)),
                updated_at=stamp,
            )

        return await asyncio.to_thread(self._mutate_watch, subscription_id, mutate)

    async def save_notification_hash(
        self, subscription_id: str, notification_hash: str
    ) -> WatchSubscription:
        """成功推送后保存通知身份，避免同一变更重复推送。"""
        _require_notification_hash(notification_hash)

        def mutate(old: WatchSubscription, stamp: str, now: datetime):
            return replace(
                old, last_notification_hash=notification_hash, updated_at=stamp
            )

        return await asyncio.to_thread(self._mutate_watch, subscription_id, mutate)

    def _get_sync(self, subscription_id: str) -> WatchSubscription:
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        try:
            row = conn.execute(
                f"SELECT {_WATCH_COLUMNS} FROM watch_subscriptions WHERE id = ?",
                (subscription_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise KeyError(f"未知订阅：{subscription_id}")
        return _watch_from_row(row)

    def _create_sync(
        self, umo: str, url: str, interval_minutes: int, tag: str
    ) -> WatchSubscription:
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM watch_subscriptions WHERE umo = ? AND url = ?",
                (umo, url),
            ).fetchone():
                raise ValueError("同一会话已订阅此 URL")
            if conn.execute(
                "SELECT 1 FROM watch_subscriptions WHERE tag = ?", (tag,)
            ).fetchone():
                raise ValueError("tag 已被其他订阅使用")
            now = _require_aware(self._clock())
            stamp = _utc_stamp(now)
            record = WatchSubscription(
                id=str(uuid.uuid4()),
                umo=umo,
                url=url,
                interval_minutes=interval_minutes,
                tag=tag,
                cron_job_id=None,
                next_check_at=stamp,
                last_status=None,
                last_notification_hash=None,
                created_at=stamp,
                updated_at=stamp,
            )
            conn.execute(_INSERT_WATCH_SUBSCRIPTION, _watch_row(record))
            conn.commit()
            return record
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _list_all_sync(self) -> list[WatchSubscription]:
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        try:
            rows = conn.execute(
                f"SELECT {_WATCH_COLUMNS} FROM watch_subscriptions"
                " ORDER BY created_at, id"
            ).fetchall()
        finally:
            conn.close()
        return [_watch_from_row(row) for row in rows]

    def _delete_sync(self, subscription_id: str) -> None:
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "DELETE FROM watch_subscriptions WHERE id = ?", (subscription_id,)
            )
            if cursor.rowcount != 1:
                raise KeyError(f"未知订阅：{subscription_id}")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _mutate_watch(
        self,
        subscription_id: str,
        mutation: Callable[[WatchSubscription, str, datetime], WatchSubscription],
    ) -> WatchSubscription:
        """独立写事务中读旧行、应用迁移并整行写回，异常回滚上抛。"""
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT {_WATCH_COLUMNS} FROM watch_subscriptions WHERE id = ?",
                (subscription_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"未知订阅：{subscription_id}")
            now = _require_aware(self._clock())
            stamp = _utc_stamp(now)
            record = mutation(_watch_from_row(row), stamp, now)
            conn.execute(
                f"UPDATE watch_subscriptions SET {_WATCH_SET_COLUMNS} WHERE id = ?",
                _watch_row(record)[1:] + (record.id,),
            )
            conn.commit()
            return record
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
