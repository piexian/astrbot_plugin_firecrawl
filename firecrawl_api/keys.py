"""Firecrawl API Key 池：去重、轮换与冷却。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .errors import ErrorCategory, FirecrawlError

_COOLDOWN_CATEGORIES = frozenset(
    {ErrorCategory.AUTH, ErrorCategory.QUOTA, ErrorCategory.RATE_LIMIT}
)


class KeyPoolExhausted(Exception):
    """本次请求的所有 Key 都已尝试，没有可用 Key。"""


@dataclass
class _KeyState:
    key: str
    cooldown_until: float = 0.0
    cooldown_reason: str = ""


def _masked(key: str) -> str:
    """摘要与报错用打码；不复用 mask_key，因为非 fc- 前缀的 key 也要防泄露。"""
    return key[:4] + "***" if len(key) > 6 else "***"


class KeyPool:
    """异步并发安全的 Key 池；纯 Python，不感知 AstrBot。"""

    def __init__(
        self,
        keys: Iterable[str],
        *,
        cooldown_seconds: int = 600,
        clock: Callable[[], float] = time.monotonic,
        logger: Any | None = None,
    ) -> None:
        self._clock = clock
        self._logger = logger if logger is not None else logging.getLogger(__name__)
        self._cooldown_seconds = cooldown_seconds
        self._lock = asyncio.Lock()
        self._states: list[_KeyState] = []
        self._by_key: dict[str, _KeyState] = {}
        self._duplicates = 0
        seen: set[str] = set()
        for raw in keys:
            key = str(raw or "").strip()
            if not key:
                continue
            if key in seen:
                self._duplicates += 1
                continue
            seen.add(key)
            state = _KeyState(key=key)
            self._states.append(state)
            self._by_key[key] = state
        self._cursor = 0
        if self._duplicates:
            self._logger.warning(
                f"Firecrawl KeyPool 检测到 {self._duplicates} 个重复 Key，已去重。"
            )

    @property
    def key_count(self) -> int:
        return len(self._states)

    async def acquire(self, attempted_keys: Iterable[str] | None = None) -> str:
        """取本次请求下一个未尝试的 Key。

        正常 key 按轮换顺序优先；只剩冷却 key 时返回最早到期的，
        冷却只影响顺序，不会拒绝请求。没有未尝试 key 时抛 KeyPoolExhausted。
        """
        attempted = set(attempted_keys or ())
        async with self._lock:
            now = self._clock()
            candidates = [s for s in self._states if s.key not in attempted]
            if not candidates:
                raise KeyPoolExhausted(
                    f"本次请求的所有 API Key 都已尝试（池内共 {len(self._states)} 个）"
                )
            total = len(self._states)
            for offset in range(total):
                pos = (self._cursor + offset) % total
                state = self._states[pos]
                if state.key in attempted or state.cooldown_until > now:
                    continue
                self._cursor = (pos + 1) % total
                return state.key
            return min(candidates, key=lambda s: s.cooldown_until).key

    async def mark_failure(
        self,
        key: str,
        error: ErrorCategory | FirecrawlError,
        retry_after: int | None = None,
    ) -> None:
        """auth/quota/rate_limit 让 key 进入冷却；Retry-After 优先，否则用默认冷却。"""
        category = error.category if isinstance(error, FirecrawlError) else error
        async with self._lock:
            state = self._require(key)
            if category not in _COOLDOWN_CATEGORIES:
                return
            if retry_after is None and isinstance(error, FirecrawlError):
                retry_after = error.retry_after
            seconds = retry_after if retry_after is not None else self._cooldown_seconds
            state.cooldown_until = self._clock() + max(0, seconds)
            state.cooldown_reason = category.value

    async def mark_success(self, key: str) -> None:
        """成功后清除该 key 的冷却。"""
        async with self._lock:
            state = self._require(key)
            state.cooldown_until = 0.0
            state.cooldown_reason = ""

    def summary(self) -> dict:
        """池状态摘要；key 打码，不泄露完整 key。"""
        now = self._clock()
        keys = []
        for state in self._states:
            cooling = state.cooldown_until > now
            keys.append(
                {
                    "key": _masked(state.key),
                    "cooling": cooling,
                    "cooldown_reason": state.cooldown_reason if cooling else None,
                    "cooldown_seconds_left": round(state.cooldown_until - now)
                    if cooling
                    else 0,
                }
            )
        return {
            "total": len(self._states),
            "duplicates_removed": self._duplicates,
            "keys": keys,
        }

    def _require(self, key: str) -> _KeyState:
        state = self._by_key.get(key)
        if state is None:
            raise ValueError(f"KeyPool 中不存在该 Key：{_masked(key)}")
        return state
