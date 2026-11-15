"""Agent 任务存储：共享 schema 上的 agent_tasks 短事务 CRUD。"""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

from ..firecrawl_api.agent import MAX_AGENT_PROMPT_CHARS
from ..firecrawl_api.tasks import parse_task_handle
from ..persistence import SQLiteStore

_T = TypeVar("_T")

_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_RECORD_STATUSES = frozenset({"processing", *_TERMINAL_STATUSES})

_COLUMNS = (
    "reservation_id, umo, prompt, status, task_handle,"
    " result_json, credits_used, error, notified_at, created_at, updated_at"
)

_SET_COLUMNS = (
    "umo = ?, prompt = ?, status = ?, task_handle = ?,"
    " result_json = ?, credits_used = ?, error = ?, notified_at = ?,"
    " created_at = ?, updated_at = ?"
)

_INSERT = (
    "INSERT INTO agent_tasks ("
    " reservation_id, umo, prompt, status, task_handle,"
    " result_json, credits_used, error, notified_at, created_at, updated_at)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


@dataclass(frozen=True)
class AgentTask:
    """一条 agent 任务：Python 层 result 为 dict，库内以规范 JSON 存储。"""

    reservation_id: str
    umo: str
    prompt: str
    status: str
    task_handle: str | None
    result: dict | None
    credits_used: float | None
    error: str | None
    notified_at: str | None
    created_at: str
    updated_at: str


def _utc_now() -> datetime:
    """默认 clock：当前 UTC 时刻。"""
    return datetime.now(timezone.utc)


def _require_aware(now: datetime) -> datetime:
    """clock 输出必须是 timezone-aware datetime。"""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now 必须是 timezone-aware datetime")
    return now


def _utc_stamp(now: datetime) -> str:
    """新时间戳统一换算为 UTC ISO 字符串。"""
    return now.astimezone(timezone.utc).isoformat()


def _require_reservation_id(value: object) -> str:
    """reservation_id 必须是非空字符串。"""
    if not isinstance(value, str) or not value:
        raise ValueError(f"reservation_id 必须是非空字符串，收到 {value!r}")
    return value


def _require_text(value: object, name: str) -> str:
    """非空文本参数，去除首尾空白。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空字符串")
    return value.strip()


def _require_prompt(value: object) -> str:
    """prompt 去首尾空白后非空且不超过 agent 契约上限。"""
    if not isinstance(value, str):
        raise ValueError(f"prompt 必须是字符串，收到 {value!r}")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("prompt 不能为空")
    if len(cleaned) > MAX_AGENT_PROMPT_CHARS:
        raise ValueError(
            f"prompt 去首尾空白后最多 {MAX_AGENT_PROMPT_CHARS} 字符，"
            f"收到 {len(cleaned)} 字符"
        )
    return cleaned


def _require_credits(value: object) -> float:
    """credits_used 必须是非 bool 有限非负数字。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("credits_used 必须是非 bool 有限非负数字")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError("credits_used 必须是非 bool 有限非负数字") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError("credits_used 必须是非 bool 有限非负数字")
    return number


def _canonical_json(source: dict) -> str:
    """按键序序列化对象；NaN/Infinity 与非 JSON 类型按 ValueError 拒绝。"""
    try:
        return json.dumps(source, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"对象包含无法 JSON 序列化的值：{exc}") from None


def _normalize_result(result: object) -> dict | None:
    """终态 result 可空；非空时必须是可规范 JSON 序列化的对象。"""
    if result is None:
        return None
    if not isinstance(result, dict):
        raise ValueError("result 必须是 JSON 对象或 None")
    return json.loads(_canonical_json(result))


def _task_from_row(row: tuple) -> AgentTask:
    """由存储行还原任务记录。"""
    raw_json = row[5]
    if raw_json is None:
        result = None
    else:
        loaded = json.loads(raw_json)
        if not isinstance(loaded, dict):
            raise ValueError("result_json 必须是 JSON 对象")
        result = loaded
    credits = row[6]
    return AgentTask(
        reservation_id=row[0],
        umo=row[1],
        prompt=row[2],
        status=row[3],
        task_handle=row[4],
        result=result,
        credits_used=None if credits is None else float(credits),
        error=row[7],
        notified_at=row[8],
        created_at=row[9],
        updated_at=row[10],
    )


def _task_row(record: AgentTask) -> tuple:
    """按存储列顺序展开任务记录。"""
    result_json = None if record.result is None else _canonical_json(record.result)
    return (
        record.reservation_id,
        record.umo,
        record.prompt,
        record.status,
        record.task_handle,
        result_json,
        record.credits_used,
        record.error,
        record.notified_at,
        record.created_at,
        record.updated_at,
    )


def _load(conn: sqlite3.Connection, reservation_id: str) -> AgentTask:
    """按 reservation_id 读取一行，未知则 KeyError。"""
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM agent_tasks WHERE reservation_id = ?",
        (reservation_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"未知任务：{reservation_id}")
    return _task_from_row(row)


def _save(conn: sqlite3.Connection, record: AgentTask) -> None:
    """按存储列写回任务记录。"""
    conn.execute(
        f"UPDATE agent_tasks SET {_SET_COLUMNS} WHERE reservation_id = ?",
        _task_row(record)[1:] + (record.reservation_id,),
    )


class AgentStore:
    """agent_tasks 存储：每次操作独立连接的短事务，写操作用 BEGIN IMMEDIATE。"""

    def __init__(
        self,
        db_path: str | Path,
        clock: Callable[[], datetime] = _utc_now,
    ):
        self._db_path = Path(db_path)
        self._clock = clock

    async def initialize(self) -> None:
        """用同一路径的 SQLiteStore 执行共享 schema 迁移。"""
        await SQLiteStore(self._db_path).initialize()

    async def create(self, *, reservation_id: str, umo: str, prompt: str) -> AgentTask:
        """创建 submitting 任务，无句柄、结果、费用、错误与通知。"""
        cleaned_id = _require_reservation_id(reservation_id)
        cleaned_umo = _require_text(umo, "umo")
        cleaned_prompt = _require_prompt(prompt)
        return await asyncio.to_thread(
            self._create_sync, cleaned_id, cleaned_umo, cleaned_prompt
        )

    async def get(self, reservation_id: str) -> AgentTask:
        """按 reservation_id 读取任务，未知 ID 抛 KeyError。"""
        return await asyncio.to_thread(self._get_sync, reservation_id)

    async def get_by_handle(self, task_handle: str) -> AgentTask:
        """按任务句柄读取，未知句柄抛 KeyError。"""
        return await asyncio.to_thread(self._get_by_handle_sync, task_handle)

    async def mark_accepted(self, reservation_id: str, task_handle: str) -> AgentTask:
        """submitting→processing，并写入经校验的 agent 句柄；同句柄幂等。"""
        parsed = parse_task_handle(task_handle)
        if parsed["kind"] != "agent":
            raise ValueError("task_handle 必须是 agent 类型")
        return await asyncio.to_thread(
            self._mark_accepted_sync, reservation_id, task_handle
        )

    async def record_status(
        self,
        reservation_id: str,
        status: str,
        *,
        result: dict | None = None,
        credits_used: float | None = None,
        error: str | None = None,
    ) -> AgentTask:
        """processing 可保持或进入终态；终态同值幂等，冲突与回退拒绝。"""
        if status not in _RECORD_STATUSES:
            raise ValueError("status 必须是 processing、completed、failed 或 cancelled")
        if error is not None and not isinstance(error, str):
            raise ValueError("error 必须是字符串或 None")
        if status == "processing":
            if result is not None:
                raise ValueError("processing 不接受 result")
            if credits_used is not None:
                raise ValueError("processing 不接受 credits_used")
            if error is not None:
                raise ValueError("processing 不接受 error")
            normalized = None
            credits = None
        else:
            credits = _require_credits(credits_used)
            normalized = _normalize_result(result)
        return await asyncio.to_thread(
            self._record_status_sync,
            reservation_id,
            status,
            normalized,
            credits,
            error,
        )

    async def mark_notification_sent(self, reservation_id: str) -> AgentTask:
        """仅终态设置 notified_at，重复调用幂等。"""
        return await asyncio.to_thread(
            self._mark_notification_sent_sync, reservation_id
        )

    async def list_active(self) -> list[AgentTask]:
        """返回 submitting/processing，按 created_at 与 reservation_id 稳定排序。"""
        return await asyncio.to_thread(self._list_active_sync)

    async def list_unnotified_terminal(self) -> list[AgentTask]:
        """返回未通知的终态任务，按 created_at 与 reservation_id 稳定排序。"""
        return await asyncio.to_thread(self._list_unnotified_sync)

    async def delete_unaccepted(self, reservation_id: str) -> None:
        """删除 submitting 且尚无句柄的任务，其他状态拒绝。"""
        await asyncio.to_thread(self._delete_unaccepted_sync, reservation_id)

    def _run_write(self, action: Callable[[sqlite3.Connection, str], _T]) -> _T:
        """BEGIN IMMEDIATE 短事务：异常回滚并关闭连接。"""
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            stamp = _utc_stamp(_require_aware(self._clock()))
            result = action(conn, stamp)
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _run_read(self, action: Callable[[sqlite3.Connection], _T]) -> _T:
        """独立短连接只读，关闭连接。"""
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        try:
            return action(conn)
        finally:
            conn.close()

    def _create_sync(self, reservation_id: str, umo: str, prompt: str) -> AgentTask:
        def action(conn: sqlite3.Connection, stamp: str) -> AgentTask:
            exists = conn.execute(
                "SELECT 1 FROM agent_tasks WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            if exists is not None:
                raise ValueError(f"任务 ID 已存在：{reservation_id}")
            record = AgentTask(
                reservation_id=reservation_id,
                umo=umo,
                prompt=prompt,
                status="submitting",
                task_handle=None,
                result=None,
                credits_used=None,
                error=None,
                notified_at=None,
                created_at=stamp,
                updated_at=stamp,
            )
            conn.execute(_INSERT, _task_row(record))
            return record

        return self._run_write(action)

    def _get_sync(self, reservation_id: str) -> AgentTask:
        return self._run_read(lambda conn: _load(conn, reservation_id))

    def _get_by_handle_sync(self, task_handle: str) -> AgentTask:
        def action(conn: sqlite3.Connection) -> AgentTask:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM agent_tasks WHERE task_handle = ?",
                (task_handle,),
            ).fetchone()
            if row is None:
                raise KeyError("未知任务句柄")
            return _task_from_row(row)

        return self._run_read(action)

    def _mark_accepted_sync(self, reservation_id: str, task_handle: str) -> AgentTask:
        def action(conn: sqlite3.Connection, stamp: str) -> AgentTask:
            old = _load(conn, reservation_id)
            if old.status == "processing":
                if old.task_handle == task_handle:
                    return old
                raise ValueError("任务句柄冲突")
            if old.status != "submitting":
                raise ValueError(f"当前状态不允许 mark_accepted：{old.status}")
            taken = conn.execute(
                "SELECT reservation_id FROM agent_tasks WHERE task_handle = ?",
                (task_handle,),
            ).fetchone()
            if taken is not None:
                raise ValueError("任务句柄已被其他任务使用")
            record = replace(
                old,
                status="processing",
                task_handle=task_handle,
                updated_at=stamp,
            )
            _save(conn, record)
            return record

        return self._run_write(action)

    def _record_status_sync(
        self,
        reservation_id: str,
        status: str,
        result: dict | None,
        credits_used: float | None,
        error: str | None,
    ) -> AgentTask:
        def action(conn: sqlite3.Connection, stamp: str) -> AgentTask:
            old = _load(conn, reservation_id)
            if old.status == "submitting":
                raise ValueError("submitting 不允许 record_status")
            if status == "processing":
                if old.status != "processing":
                    raise ValueError("不允许从终态回到 processing")
                record = replace(old, updated_at=stamp)
                _save(conn, record)
                return record
            if old.status in _TERMINAL_STATUSES:
                if (
                    old.status == status
                    and old.credits_used == credits_used
                    and old.result == result
                    and old.error == error
                ):
                    return old
                if old.status != status:
                    raise ValueError(f"状态 {status} 与已记录值 {old.status} 冲突")
                if old.credits_used != credits_used:
                    raise ValueError(
                        f"费用 {credits_used} 与已记录值 {old.credits_used} 冲突"
                    )
                if old.result != result:
                    raise ValueError("结果与已记录值冲突")
                raise ValueError("error 与已记录值冲突")
            record = replace(
                old,
                status=status,
                result=result,
                credits_used=credits_used,
                error=error,
                updated_at=stamp,
            )
            _save(conn, record)
            return record

        return self._run_write(action)

    def _mark_notification_sent_sync(self, reservation_id: str) -> AgentTask:
        def action(conn: sqlite3.Connection, stamp: str) -> AgentTask:
            old = _load(conn, reservation_id)
            if old.status not in _TERMINAL_STATUSES:
                raise ValueError("仅终态可标记通知")
            if old.notified_at is not None:
                return old
            record = replace(old, notified_at=stamp, updated_at=stamp)
            _save(conn, record)
            return record

        return self._run_write(action)

    def _list_active_sync(self) -> list[AgentTask]:
        def action(conn: sqlite3.Connection) -> list[AgentTask]:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM agent_tasks"
                " WHERE status IN ('submitting', 'processing')"
                " ORDER BY created_at, reservation_id"
            ).fetchall()
            return [_task_from_row(row) for row in rows]

        return self._run_read(action)

    def _list_unnotified_sync(self) -> list[AgentTask]:
        def action(conn: sqlite3.Connection) -> list[AgentTask]:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM agent_tasks"
                " WHERE status IN ('completed', 'failed', 'cancelled')"
                " AND notified_at IS NULL"
                " ORDER BY created_at, reservation_id"
            ).fetchall()
            return [_task_from_row(row) for row in rows]

        return self._run_read(action)

    def _delete_unaccepted_sync(self, reservation_id: str) -> None:
        def action(conn: sqlite3.Connection, _stamp: str) -> None:
            old = _load(conn, reservation_id)
            if old.status != "submitting" or old.task_handle is not None:
                raise ValueError("只能删除未受理的 submitting 任务")
            conn.execute(
                "DELETE FROM agent_tasks WHERE reservation_id = ?",
                (reservation_id,),
            )

        self._run_write(action)
