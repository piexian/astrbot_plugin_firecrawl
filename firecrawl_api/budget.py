"""预算模块：费用上界估算、预算策略、预留数据与唯一合法状态迁移。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from .errors import ErrorCategory, FirecrawlError

# 当前工具只开放这些 format；json、question 每页加价，summary 不加价。
_PAGE_FORMATS = ("markdown", "summary", "links", "html", "question", "json")
_SURCHARGE_FORMATS = ("question", "json")
_SITE_KINDS = ("map", "crawl", "batch")

_BASE_PAGE_COST = 1
_FORMAT_SURCHARGE = 4
_INJECTION_SURCHARGE = 4
_PDF_PAGE_COST = 1
_PDF_MAX_PAGES_LIMITS = (1, 10000)
_SEARCH_LIMITS = (1, 100)
_SOURCE_COUNT_LIMITS = (1, 3)
_SEARCH_BLOCK_RESULTS = 10
_SEARCH_BLOCK_COST = 2


class BudgetExceeded(FirecrawlError):
    """本地预算额度不足。"""

    def __init__(self, message: str) -> None:
        super().__init__(ErrorCategory.USAGE, message)


class InvalidBudgetTransition(FirecrawlError):
    """预留状态迁移不合法。"""

    def __init__(self, message: str) -> None:
        super().__init__(ErrorCategory.USAGE, message)


class ReservationState(str, Enum):
    """预算预留的生命周期状态。"""

    RESERVED = "reserved"
    SUBMITTING = "submitting"
    ACCEPTED = "accepted"
    INDETERMINATE = "indeterminate"
    SETTLED = "settled"
    RELEASED = "released"


_TRANSITIONS = {
    ReservationState.RESERVED: {
        ReservationState.SUBMITTING,
        ReservationState.RELEASED,
    },
    ReservationState.SUBMITTING: {
        ReservationState.ACCEPTED,
        ReservationState.INDETERMINATE,
        ReservationState.RELEASED,
    },
    ReservationState.ACCEPTED: {ReservationState.SETTLED},
    ReservationState.INDETERMINATE: {
        ReservationState.SETTLED,
        ReservationState.RELEASED,
    },
    ReservationState.SETTLED: set(),
    ReservationState.RELEASED: set(),
}


def validate_transition(
    current: ReservationState | str,
    target: ReservationState | str,
    *,
    confirmed_not_accepted: bool = False,
) -> None:
    """唯一状态图：合法迁移静默通过，其余抛 InvalidBudgetTransition。"""
    try:
        cur = ReservationState(current)
        tgt = ReservationState(target)
    except ValueError:
        raise InvalidBudgetTransition(
            f"未知预算状态：current={current!r} target={target!r}"
        ) from None
    if tgt not in _TRANSITIONS[cur]:
        raise InvalidBudgetTransition(f"不允许从 {cur.value} 迁移到 {tgt.value}")
    if (
        cur is ReservationState.SUBMITTING
        and tgt is ReservationState.RELEASED
        and not confirmed_not_accepted
    ):
        raise InvalidBudgetTransition(
            "submitting → released 仅限明确未受理，需 confirmed_not_accepted=True"
        )


@dataclass(frozen=True)
class Reservation:
    """一次计费调用的预留记录；只存指纹与 Key 引用，不存请求正文或 Key。"""

    reservation_id: str
    utc_day: str
    estimate: int
    kind: str
    request_fingerprint: str
    created_at: str
    updated_at: str
    state: ReservationState = ReservationState.RESERVED
    key_ref: str | None = None
    job_id: str | None = None
    actual_credits: float | None = None
    accounted_credits: float = 0.0

    def __post_init__(self) -> None:
        _require_count(self.estimate, "estimate", minimum=0)
        ReservationState(self.state)
        if self.actual_credits is not None:
            _require_amount(self.actual_credits, "actual_credits")
        _require_amount(self.accounted_credits, "accounted_credits")


@dataclass(frozen=True)
class BudgetPolicy:
    """本地预算策略；额度为 0 表示不限，但仍验证账目输入。"""

    max_estimated_credits_per_call: float = 0
    daily_credit_limit: float = 0

    def __post_init__(self) -> None:
        _require_amount(
            self.max_estimated_credits_per_call, "max_estimated_credits_per_call"
        )
        _require_amount(self.daily_credit_limit, "daily_credit_limit")

    def check(self, estimate: int, used: float = 0, active_reserved: float = 0) -> None:
        """校验单次预估与日额 used+active+estimate，超限抛 BudgetExceeded。"""
        _require_count(estimate, "estimate", minimum=0)
        _require_amount(used, "used")
        _require_amount(active_reserved, "active_reserved")
        per_call = self.max_estimated_credits_per_call
        if per_call > 0 and estimate > per_call:
            raise BudgetExceeded(
                f"单次预估 {estimate} credits 超过单次上限 {per_call}。"
            )
        daily = self.daily_credit_limit
        if daily > 0 and used + active_reserved + estimate > daily:
            raise BudgetExceeded(
                f"日预算不足：已用 {used} + 活动预留 {active_reserved}"
                f" + 本次预估 {estimate} 超过日限额 {daily}。"
            )


def _require_count(value: object, name: str, *, minimum: int, maximum: int = 0) -> int:
    """整数计数参数必须是非 bool 的 int，且落在 [minimum, maximum]（maximum=0 不设上限）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数，收到 {value!r}")
    if value < minimum or (maximum and value > maximum):
        bound = f"{minimum}-{maximum}" if maximum else f">= {minimum}"
        raise ValueError(f"{name} 必须是 {bound} 的整数，收到 {value}")
    return value


def _require_amount(value: object, name: str) -> float:
    """账目数额必须是非 bool 的有限非负数，保留小数不截断。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是数字，收到 {value!r}")
    if not math.isfinite(value):
        raise ValueError(f"{name} 必须是有限数值，收到 {value!r}")
    if value < 0:
        raise ValueError(f"{name} 不能为负数，收到 {value!r}")
    return value


def estimate_page(
    output_format: str = "markdown",
    pdf_max_pages: int = 20,
    check_prompt_injection: bool = False,
) -> int:
    """scrape 单页费用上界：基础 1 + format 附加 + 注入检测 + PDF 页数上限。"""
    if output_format not in _PAGE_FORMATS:
        raise ValueError(
            f"不支持的 output_format：{output_format!r}，可选 {_PAGE_FORMATS}"
        )
    _require_count(
        pdf_max_pages,
        "pdf_max_pages",
        minimum=_PDF_MAX_PAGES_LIMITS[0],
        maximum=_PDF_MAX_PAGES_LIMITS[1],
    )
    if not isinstance(check_prompt_injection, bool):
        raise ValueError("check_prompt_injection 必须是布尔值")
    cost = _BASE_PAGE_COST
    if output_format in _SURCHARGE_FORMATS:
        cost += _FORMAT_SURCHARGE
    if check_prompt_injection:
        cost += _INJECTION_SURCHARGE
    return cost + pdf_max_pages * _PDF_PAGE_COST


def estimate_search(
    limit: int,
    source_count: int = 1,
    with_content: bool = False,
    *,
    output_format: str = "markdown",
    pdf_max_pages: int = 20,
    check_prompt_injection: bool = False,
) -> int:
    """search 费用上界：每 10 条结果 2 credits，with_content 按裁剪前的结果上限另计单页费。"""
    _require_count(limit, "limit", minimum=_SEARCH_LIMITS[0], maximum=_SEARCH_LIMITS[1])
    _require_count(
        source_count,
        "source_count",
        minimum=_SOURCE_COUNT_LIMITS[0],
        maximum=_SOURCE_COUNT_LIMITS[1],
    )
    if not isinstance(with_content, bool):
        raise ValueError("with_content 必须是布尔值")
    billed_results = limit * source_count
    blocks = (billed_results + _SEARCH_BLOCK_RESULTS - 1) // _SEARCH_BLOCK_RESULTS
    cost = blocks * _SEARCH_BLOCK_COST
    if with_content:
        cost += billed_results * estimate_page(
            output_format, pdf_max_pages, check_prompt_injection
        )
    return cost


def estimate_site(
    kind: str,
    page_count: int | None = None,
    *,
    output_format: str = "markdown",
    pdf_max_pages: int = 20,
    check_prompt_injection: bool = False,
) -> int:
    """site 任务费用上界：map 固定 1，crawl/batch 必须显式提供页数并按其乘单页费。"""
    if kind not in _SITE_KINDS:
        raise ValueError(f"不支持的 site kind：{kind!r}，可选 {_SITE_KINDS}")
    if kind == "map":
        return 1
    if page_count is None:
        raise ValueError("crawl/batch 必须显式提供 page_count")
    _require_count(page_count, "page_count", minimum=1)
    per_page = estimate_page(output_format, pdf_max_pages, check_prompt_injection)
    return page_count * per_page


def utc_day(now: datetime | None = None) -> str:
    """按 UTC 自然日返回 YYYY-MM-DD；输入 datetime 必须是 timezone-aware。"""
    moment = now if now is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("now 必须是 timezone-aware datetime")
    return moment.astimezone(timezone.utc).date().isoformat()
