"""firecrawl_api.budget 预算模块测试。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from firecrawl_api import ErrorCategory, friendly_message
from firecrawl_api.budget import (
    BudgetExceeded,
    BudgetPolicy,
    InvalidBudgetTransition,
    Reservation,
    ReservationState,
    estimate_page,
    estimate_search,
    estimate_site,
    utc_day,
    validate_transition,
)


def make_reservation(**overrides) -> Reservation:
    fields = {
        "reservation_id": "r-1",
        "utc_day": "2026-01-01",
        "estimate": 21,
        "kind": "scrape",
        "request_fingerprint": "fp-1",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    fields.update(overrides)
    return Reservation(**fields)


def test_estimate_page_stacks_format_injection_and_pdf():
    assert estimate_page() == 21
    assert estimate_page(output_format="json") == 25
    assert estimate_page(output_format="question", check_prompt_injection=True) == 29
    assert estimate_page(output_format="summary") == 21
    assert estimate_page(output_format="html") == 21
    assert estimate_page(pdf_max_pages=1) == 2


def test_estimate_page_rejects_invalid_args():
    with pytest.raises(ValueError):
        estimate_page(output_format="extract")
    with pytest.raises(ValueError):
        estimate_page(pdf_max_pages=0)
    with pytest.raises(ValueError):
        estimate_page(pdf_max_pages=10001)
    with pytest.raises(ValueError):
        estimate_page(pdf_max_pages=1.5)
    with pytest.raises(ValueError):
        estimate_page(pdf_max_pages=True)


def test_estimate_search_rounds_up_to_blocks_of_ten():
    assert estimate_search(1) == 2
    assert estimate_search(10) == 2
    assert estimate_search(11) == 4


def test_estimate_search_multiplies_sources_before_rounding():
    assert estimate_search(5, source_count=3) == 4
    assert estimate_search(4, source_count=2) == 2


def test_estimate_search_with_content_bills_pre_trim_upper_bound():
    assert (
        estimate_search(
            3,
            source_count=2,
            with_content=True,
            output_format="json",
            check_prompt_injection=True,
            pdf_max_pages=10,
        )
        == 116
    )
    assert estimate_search(3, source_count=2) == 2


def test_estimate_search_rejects_out_of_range():
    for limit in (0, 101, 2.5, True):
        with pytest.raises(ValueError):
            estimate_search(limit)
    for source_count in (0, 4, True):
        with pytest.raises(ValueError):
            estimate_search(10, source_count=source_count)


def test_estimate_site_map_is_flat():
    assert estimate_site("map") == 1
    assert estimate_site("map", page_count=500) == 1


def test_estimate_site_crawl_and_batch_bill_per_page():
    assert estimate_site("crawl", page_count=3) == 63
    assert estimate_site("batch", page_count=2, pdf_max_pages=4) == 10
    with pytest.raises(ValueError):
        estimate_site("scrape")
    with pytest.raises(ValueError):
        estimate_site("crawl", page_count=0)


def test_estimate_site_crawl_and_batch_require_explicit_page_count():
    with pytest.raises(ValueError):
        estimate_site("crawl")
    with pytest.raises(ValueError):
        estimate_site("batch")


def test_budget_policy_zero_means_unlimited_but_still_validates_inputs():
    policy = BudgetPolicy(max_estimated_credits_per_call=0, daily_credit_limit=0)
    policy.check(10000, used=999999, active_reserved=999999)
    with pytest.raises(ValueError):
        policy.check(-1)
    with pytest.raises(ValueError):
        policy.check(1, used=-0.5)
    with pytest.raises(ValueError):
        policy.check(1, used=True)
    with pytest.raises(ValueError):
        policy.check(1, active_reserved=float("inf"))
    with pytest.raises(ValueError):
        policy.check(1, active_reserved=float("nan"))
    with pytest.raises(ValueError):
        BudgetPolicy(max_estimated_credits_per_call=True)
    with pytest.raises(ValueError):
        BudgetPolicy(daily_credit_limit=float("nan"))


def test_budget_policy_exact_limit_passes_and_over_fails():
    per_call = BudgetPolicy(max_estimated_credits_per_call=10, daily_credit_limit=0)
    per_call.check(10)
    with pytest.raises(BudgetExceeded):
        per_call.check(11)

    daily = BudgetPolicy(max_estimated_credits_per_call=0, daily_credit_limit=100)
    daily.check(estimate=30, used=60, active_reserved=10)
    with pytest.raises(BudgetExceeded):
        daily.check(estimate=31, used=60, active_reserved=10)


def test_budget_exceeded_keeps_local_reason_as_usage_error():
    policy = BudgetPolicy(daily_credit_limit=100)
    with pytest.raises(BudgetExceeded) as excinfo:
        policy.check(101)
    err = excinfo.value
    assert err.category is ErrorCategory.USAGE
    assert "日限额 100" in str(err)
    assert friendly_message(err) == str(err)


def test_legal_transitions_pass():
    validate_transition(ReservationState.RESERVED, ReservationState.SUBMITTING)
    validate_transition(ReservationState.RESERVED, ReservationState.RELEASED)
    validate_transition(ReservationState.SUBMITTING, ReservationState.ACCEPTED)
    validate_transition(ReservationState.SUBMITTING, ReservationState.INDETERMINATE)
    validate_transition(
        ReservationState.SUBMITTING,
        ReservationState.RELEASED,
        confirmed_not_accepted=True,
    )
    validate_transition(ReservationState.ACCEPTED, ReservationState.SETTLED)
    validate_transition(ReservationState.INDETERMINATE, ReservationState.SETTLED)
    validate_transition(ReservationState.INDETERMINATE, ReservationState.RELEASED)
    validate_transition("reserved", "submitting")


def test_submitting_release_requires_confirmed_not_accepted():
    with pytest.raises(InvalidBudgetTransition, match="confirmed_not_accepted"):
        validate_transition(ReservationState.SUBMITTING, ReservationState.RELEASED)


def test_illegal_transitions_raise():
    illegal = [
        (ReservationState.RESERVED, ReservationState.ACCEPTED, {}),
        (ReservationState.RESERVED, ReservationState.INDETERMINATE, {}),
        (ReservationState.RESERVED, ReservationState.SETTLED, {}),
        (ReservationState.SUBMITTING, ReservationState.SETTLED, {}),
        (ReservationState.SUBMITTING, ReservationState.RESERVED, {}),
        (ReservationState.ACCEPTED, ReservationState.RELEASED, {}),
        (ReservationState.ACCEPTED, ReservationState.INDETERMINATE, {}),
        (ReservationState.ACCEPTED, ReservationState.SUBMITTING, {}),
        (ReservationState.INDETERMINATE, ReservationState.ACCEPTED, {}),
        (ReservationState.INDETERMINATE, ReservationState.SUBMITTING, {}),
        (ReservationState.SETTLED, ReservationState.RELEASED, {}),
        (ReservationState.RELEASED, ReservationState.RESERVED, {}),
        ("unknown", ReservationState.RESERVED, {}),
    ]
    for current, target, kwargs in illegal:
        with pytest.raises(InvalidBudgetTransition):
            validate_transition(current, target, **kwargs)


def test_reservation_defaults_and_frozen():
    res = make_reservation()
    assert res.state is ReservationState.RESERVED
    assert res.key_ref is None
    assert res.job_id is None
    assert res.actual_credits is None
    assert res.accounted_credits == 0
    with pytest.raises(FrozenInstanceError):
        res.estimate = 5


def test_reservation_keeps_decimal_credits():
    res = make_reservation(actual_credits=4.5, accounted_credits=2.5)
    assert res.actual_credits == 4.5
    assert res.accounted_credits == 2.5


def test_reservation_rejects_invalid_accounting():
    for bad_estimate in (-1, 1.5, True):
        with pytest.raises(ValueError):
            make_reservation(estimate=bad_estimate)
    for bad_credits in (-1, float("inf"), float("nan"), True):
        with pytest.raises(ValueError):
            make_reservation(actual_credits=bad_credits)
        with pytest.raises(ValueError):
            make_reservation(accounted_credits=bad_credits)
    with pytest.raises(ValueError):
        make_reservation(state="nope")


def test_utc_day_handles_timezone_boundaries():
    assert utc_day(datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc)) == "2026-01-01"
    cst = timezone(timedelta(hours=8))
    assert utc_day(datetime(2026, 1, 2, 7, 0, tzinfo=cst)) == "2026-01-01"
    est = timezone(timedelta(hours=-5))
    assert utc_day(datetime(2026, 1, 1, 20, 0, tzinfo=est)) == "2026-01-02"


def test_utc_day_rejects_naive_datetime():
    with pytest.raises(ValueError):
        utc_day(datetime(2026, 1, 1, 12, 0))
