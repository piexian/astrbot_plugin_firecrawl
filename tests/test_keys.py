"""firecrawl_api.keys KeyPool 测试。"""

from __future__ import annotations

import asyncio
import json

import pytest

from firecrawl_api import (
    ErrorCategory,
    FirecrawlError,
    KeyPool,
    KeyPoolExhausted,
)

K1 = "fc-aaaaaaaa1111"
K2 = "fc-bbbbbbbb2222"
K3 = "fc-cccccccc3333"


class FakeClock:
    """可手动推进的 monotonic clock。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_pool(keys: list[str], **kwargs) -> tuple[KeyPool, FakeClock]:
    clock = kwargs.pop("clock", None) or FakeClock()
    return KeyPool(keys, clock=clock, **kwargs), clock


def rate_limit_error(retry_after: int | None = None) -> FirecrawlError:
    return FirecrawlError(
        ErrorCategory.RATE_LIMIT, "请求过于频繁", retry_after=retry_after
    )


async def gather_acquires(pool: KeyPool, count: int) -> list[str]:
    return list(await asyncio.gather(*(pool.acquire() for _ in range(count))))


def test_dedupe_trim_and_filter_empty():
    pool, _ = make_pool([f" {K1} ", K1, "", "   ", K2, K1, None])
    assert pool.key_count == 2
    summary = pool.summary()
    assert summary["total"] == 2
    assert summary["duplicates_removed"] == 2
    assert [item["key"] for item in summary["keys"]] == ["fc-a***", "fc-b***"]


def test_empty_pool_constructs_and_acquire_exhausted():
    pool, _ = make_pool([])
    assert pool.key_count == 0
    with pytest.raises(KeyPoolExhausted):
        asyncio.run(pool.acquire())


@pytest.mark.asyncio
async def test_round_robin():
    pool, _ = make_pool([K1, K2, K3])
    picked = [await pool.acquire() for _ in range(4)]
    assert picked == [K1, K2, K3, K1]


@pytest.mark.asyncio
async def test_attempted_keys_never_repeated():
    pool, _ = make_pool([K1, K2, K3])
    first = await pool.acquire()
    second = await pool.acquire(attempted_keys=[first])
    third = await pool.acquire(attempted_keys=[first, second])
    assert {first, second, third} == {K1, K2, K3}
    with pytest.raises(KeyPoolExhausted):
        await pool.acquire(attempted_keys=[K1, K2, K3])


@pytest.mark.asyncio
async def test_attempted_key_is_skipped():
    pool, _ = make_pool([K1, K2])
    assert await pool.acquire(attempted_keys=[K2]) == K1
    assert await pool.acquire(attempted_keys=[K1]) == K2


@pytest.mark.asyncio
async def test_ready_keys_before_cooldown_keys():
    pool, _ = make_pool([K1, K2, K3])
    await pool.mark_failure(K1, rate_limit_error(retry_after=5))
    await pool.mark_failure(K2, rate_limit_error(retry_after=100))
    assert await pool.acquire() == K3
    assert await pool.acquire(attempted_keys=[K3]) == K1
    assert await pool.acquire(attempted_keys=[K3, K1]) == K2


@pytest.mark.asyncio
async def test_all_cooling_returns_earliest_expiry():
    pool, clock = make_pool([K1, K2])
    await pool.mark_failure(K1, rate_limit_error(retry_after=50))
    await pool.mark_failure(K2, rate_limit_error(retry_after=5))
    assert await pool.acquire() == K2
    assert await pool.acquire(attempted_keys=[K2]) == K1
    clock.advance(5)
    assert await pool.acquire() == K2


@pytest.mark.asyncio
async def test_retry_after_param_wins_over_error_field():
    pool, clock = make_pool([K1], cooldown_seconds=600)
    await pool.mark_failure(K1, rate_limit_error(retry_after=99), retry_after=5)
    assert pool.summary()["keys"][0]["cooldown_seconds_left"] == 5
    clock.advance(5)
    assert pool.summary()["keys"][0]["cooling"] is False


@pytest.mark.asyncio
async def test_error_field_retry_after_used_when_param_missing():
    pool, clock = make_pool([K1], cooldown_seconds=600)
    await pool.mark_failure(K1, rate_limit_error(retry_after=42))
    assert pool.summary()["keys"][0]["cooldown_seconds_left"] == 42
    clock.advance(42)
    assert pool.summary()["keys"][0]["cooling"] is False


@pytest.mark.asyncio
async def test_default_cooldown_applied():
    pool, clock = make_pool([K1, K2], cooldown_seconds=7)
    await pool.mark_failure(K1, ErrorCategory.AUTH)
    await pool.mark_failure(K2, FirecrawlError(ErrorCategory.QUOTA, "额度用完"))
    assert pool.summary()["keys"][0]["cooldown_seconds_left"] == 7
    assert pool.summary()["keys"][1]["cooldown_seconds_left"] == 7
    clock.advance(7)
    for item in pool.summary()["keys"]:
        assert item["cooling"] is False


@pytest.mark.asyncio
async def test_non_cooldown_categories_do_not_cool_down():
    pool, _ = make_pool([K1])
    for category in (
        ErrorCategory.USAGE,
        ErrorCategory.SERVER,
        ErrorCategory.TIMEOUT,
        ErrorCategory.NETWORK,
    ):
        await pool.mark_failure(K1, FirecrawlError(category, "与 key 无关"))
        assert pool.summary()["keys"][0]["cooling"] is False


@pytest.mark.asyncio
async def test_success_clears_cooldown():
    pool, _ = make_pool([K1, K2])
    await pool.mark_failure(K1, rate_limit_error(retry_after=100))
    assert await pool.acquire() == K2
    await pool.mark_success(K1)
    assert pool.summary()["keys"][0]["cooling"] is False
    assert pool.summary()["keys"][0]["cooldown_reason"] is None
    assert await pool.acquire(attempted_keys=[K2]) == K1


@pytest.mark.asyncio
async def test_unknown_key_raises():
    pool, _ = make_pool([K1])
    with pytest.raises(ValueError):
        await pool.mark_failure(K2, ErrorCategory.AUTH)
    with pytest.raises(ValueError):
        await pool.mark_success(K2)


@pytest.mark.asyncio
async def test_concurrent_acquire_rotates_without_corruption():
    pool, _ = make_pool([K1, K2, K3])
    picked = await gather_acquires(pool, 9)
    assert set(picked) == {K1, K2, K3}
    assert pool.key_count == 3
    assert all(not item["cooling"] for item in pool.summary()["keys"])


@pytest.mark.asyncio
async def test_concurrent_full_request_flow():
    pool, _ = make_pool([K1, K2, K3])
    first_keys = await gather_acquires(pool, 3)
    assert set(first_keys) == {K1, K2, K3}

    retry_overs = dict(zip(first_keys, [30, 10, 20]))
    await asyncio.gather(
        *(
            pool.mark_failure(key, rate_limit_error(retry_after=retry_after))
            for key, retry_after in retry_overs.items()
        )
    )
    assert all(item["cooling"] for item in pool.summary()["keys"])

    # 全部冷却时按到期时间取最早到期的；全部已尝试时报 KeyPoolExhausted
    assert await pool.acquire() == first_keys[1]
    assert await pool.acquire(attempted_keys=[first_keys[1]]) == first_keys[2]
    with pytest.raises(KeyPoolExhausted):
        await pool.acquire(attempted_keys=first_keys)

    await asyncio.gather(*(pool.mark_success(key) for key in first_keys))
    assert all(not item["cooling"] for item in pool.summary()["keys"])
    assert await pool.acquire() in first_keys


def test_summary_masks_full_keys():
    long_key = "fc-ZzYyXxWwVv1234567890"
    pool, _ = make_pool([long_key, "short", "midsize_key_0001", long_key])
    dumped = json.dumps(pool.summary())
    assert long_key not in dumped
    assert "short" not in dumped
    assert "midsize_key_0001" not in dumped
    assert "fc-Z***" in dumped
    assert "mids***" in dumped


def test_exhausted_message_contains_no_keys():
    pool, _ = make_pool([K1, K2])
    with pytest.raises(KeyPoolExhausted) as exc_info:
        asyncio.run(pool.acquire(attempted_keys=[K1, K2]))
    assert K1 not in str(exc_info.value)
    assert K2 not in str(exc_info.value)


class WarningRecorder:
    """捕获 warning 调用的极简 logger。"""

    def __init__(self) -> None:
        self.records: list[str] = []

    def warning(self, message: str, *args) -> None:
        self.records.append(message % args if args else message)


def test_duplicate_keys_warn_once_without_leaking():
    logs = WarningRecorder()
    pool, _ = make_pool([f" {K1} ", K1, "", "   ", K2, K1], logger=logs)
    assert pool.key_count == 2
    assert len(logs.records) == 1
    warning = logs.records[0]
    assert "2" in warning
    assert K1 not in warning
    assert K2 not in warning
    assert "Bearer" not in warning
    assert "fc-bbbbbbbb2222" not in warning


def test_unique_keys_write_no_warning():
    logs = WarningRecorder()
    make_pool([K1, K2, K3], logger=logs)
    assert logs.records == []


def test_duplicate_warning_survives_concurrent_acquire():
    logs = WarningRecorder()
    pool, _ = make_pool([K1, K1, K2, K2], logger=logs)
    picked = list(asyncio.run(gather_acquires(pool, 4)))
    assert set(picked) == {K1, K2}
    assert len(logs.records) == 1
