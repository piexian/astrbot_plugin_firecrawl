"""watch.core 请求构造、changeTracking 解析与通知消息构造测试。"""

from __future__ import annotations

import re

import pytest
from astrbot_plugin_firecrawl.watch.core import (
    WATCH_MAX_MESSAGE_CHARS,
    WatchCheckResult,
    append_watch_summary,
    build_watch_notification,
    build_watch_request,
    notification_identity,
    parse_watch_response,
)

_BLOCKED = ("x.com", "twitter.com", "mobile.twitter.com", "linkedin.com")
_PREVIOUS = "2026-01-01T00:00:00+00:00"

_HEX64 = re.compile(r"[0-9a-f]{64}")


def tracking_response(**overrides):
    """构造带合法 changeTracking 的 scrape 响应，可按字段覆盖。"""
    data = {
        "markdown": "body",
        "changeTracking": {
            "previousScrapeAt": _PREVIOUS,
            "changeStatus": "same",
            "visibility": "visible",
        },
    }
    data.update(overrides)
    return {"success": True, "data": data}


def test_build_watch_request_appends_change_tracking_format():
    request = build_watch_request("https://example.com/a", tag="page-a")
    assert request.payload["url"] == "https://example.com/a"
    assert request.payload["formats"] == [
        "markdown",
        "summary",
        {"type": "changeTracking", "modes": ["git-diff"], "tag": "page-a"},
    ]
    assert request.payload["parsers"] == [{"type": "pdf", "maxPages": 20}]
    assert request.payload["onlyMainContent"] is True
    assert "maxAge" not in request.payload
    assert request.estimate == 21


def test_build_watch_request_estimate_tracks_injection_option():
    plain = build_watch_request("https://example.com/a", tag="t")
    checked = build_watch_request(
        "https://example.com/a", tag="t", check_prompt_injection=True
    )
    assert plain.estimate == 21
    assert checked.estimate == 25


def test_build_watch_request_passes_through_page_options():
    request = build_watch_request(
        "https://example.com/a",
        tag="t",
        only_main_content=False,
        wait_for_ms=100,
        proxy="basic",
        pdf_max_pages=5,
    )
    assert request.payload["onlyMainContent"] is False
    assert request.payload["waitFor"] == 100
    assert request.payload["proxy"] == "basic"
    assert request.payload["parsers"] == [{"type": "pdf", "maxPages": 5}]


def test_build_watch_request_rejects_max_age_and_format_override():
    with pytest.raises(ValueError, match="max_age_ms"):
        build_watch_request("https://example.com/a", tag="t", max_age_ms=5000)
    with pytest.raises(ValueError, match="markdown"):
        build_watch_request("https://example.com/a", tag="t", output_format="summary")


def test_build_watch_request_rejects_bad_url_and_tag():
    with pytest.raises(ValueError):
        build_watch_request("example.com/a", tag="t")
    with pytest.raises(ValueError):
        build_watch_request("https://x.com/post", tag="t", blocked_domains=_BLOCKED)
    for bad_tag in ("", "   ", None, 7):
        with pytest.raises(ValueError, match="tag"):
            build_watch_request("https://example.com/a", tag=bad_tag)


def test_build_watch_request_blocked_domains_allow_and_reject():
    request = build_watch_request(
        "https://notx.com/post", tag="page-a", blocked_domains=_BLOCKED
    )
    assert request.payload["url"] == "https://notx.com/post"
    assert request.payload["formats"] == [
        "markdown",
        "summary",
        {"type": "changeTracking", "modes": ["git-diff"], "tag": "page-a"},
    ]
    assert request.estimate == 21
    with pytest.raises(ValueError):
        build_watch_request(
            "https://mobile.twitter.com/user/status/1",
            tag="t",
            blocked_domains=_BLOCKED,
        )


def test_build_watch_request_summary_format_order_and_estimate_unchanged():
    request = build_watch_request("https://example.com/a", tag="t")
    assert request.payload["formats"] == [
        "markdown",
        "summary",
        {"type": "changeTracking", "modes": ["git-diff"], "tag": "t"},
    ]
    assert request.estimate == 21


def test_parse_watch_response_four_statuses():
    for status in ("same", "removed"):
        result = parse_watch_response(
            tracking_response(
                changeTracking={
                    "previousScrapeAt": _PREVIOUS,
                    "changeStatus": status,
                    "visibility": "visible",
                }
            )
        )
        assert result.status == status
        assert result.previous_scrape_at == _PREVIOUS
        assert result.diff_text is None
        assert result.notification_hash is None


def test_parse_watch_response_new_allows_null_previous():
    result = parse_watch_response(
        tracking_response(
            changeTracking={"changeStatus": "new", "visibility": "visible"}
        )
    )
    assert result.status == "new"
    assert result.previous_scrape_at is None
    assert result.notification_hash is None
    # 仅 new 允许 null；官方首次响应不会携带非空 previousScrapeAt。
    with pytest.raises(ValueError, match="previousScrapeAt"):
        parse_watch_response(
            tracking_response(
                changeTracking={
                    "changeStatus": "new",
                    "previousScrapeAt": _PREVIOUS,
                }
            )
        )


def test_parse_watch_response_changed_carries_diff_and_hash():
    result = parse_watch_response(
        tracking_response(
            changeTracking={
                "previousScrapeAt": _PREVIOUS,
                "changeStatus": "changed",
                "visibility": "visible",
                "diff": {"text": "- old\n+ new"},
            }
        )
    )
    assert result.status == "changed"
    assert result.diff_text == "- old\n+ new"
    assert result.notification_hash == notification_identity(_PREVIOUS, "- old\n+ new")
    assert _HEX64.fullmatch(result.notification_hash)


def test_parse_watch_response_missing_tracking_is_unavailable():
    result = parse_watch_response({"success": True, "data": {"markdown": "body"}})
    assert result.status == "unavailable"
    assert result.previous_scrape_at is None
    assert (
        parse_watch_response(tracking_response(changeTracking=None)).status
        == "unavailable"
    )


def test_parse_watch_response_rejects_bad_structure():
    with pytest.raises(ValueError, match="缺少 data"):
        parse_watch_response({"success": True})
    with pytest.raises(ValueError, match="缺少 data"):
        parse_watch_response({"success": True, "data": "nope"})
    with pytest.raises(ValueError, match="必须是对象"):
        parse_watch_response(tracking_response(changeTracking="changed"))


def test_parse_watch_response_rejects_bad_tracking_fields():
    good = tracking_response()["data"]["changeTracking"]
    with pytest.raises(ValueError, match="changeStatus"):
        parse_watch_response(
            tracking_response(changeTracking={**good, "changeStatus": "stale"})
        )
    with pytest.raises(ValueError, match="changeStatus"):
        parse_watch_response(
            tracking_response(changeTracking={"previousScrapeAt": _PREVIOUS})
        )
    for bad_previous in (None, 123, ""):
        with pytest.raises(ValueError, match="previousScrapeAt"):
            parse_watch_response(
                tracking_response(
                    changeTracking={**good, "previousScrapeAt": bad_previous}
                )
            )


def test_parse_watch_response_changed_requires_diff_text():
    good = tracking_response()["data"]["changeTracking"]
    changed = {**good, "changeStatus": "changed"}
    with pytest.raises(ValueError, match="diff.text"):
        parse_watch_response(tracking_response(changeTracking=changed))
    with pytest.raises(ValueError, match="diff.text"):
        parse_watch_response(
            tracking_response(changeTracking={**changed, "diff": "text"})
        )
    with pytest.raises(ValueError, match="diff.text"):
        parse_watch_response(
            tracking_response(changeTracking={**changed, "diff": {"text": 1}})
        )


def test_parse_watch_response_summary_passes_through_all_statuses():
    for status in ("new", "same", "changed", "removed"):
        tracking = {"changeStatus": status, "visibility": "visible"}
        if status != "new":
            tracking["previousScrapeAt"] = _PREVIOUS
        if status == "changed":
            tracking["diff"] = {"text": "- old\n+ new"}
        result = parse_watch_response(
            tracking_response(changeTracking=tracking, summary="页面摘要")
        )
        assert result.status == status
        assert result.summary == "页面摘要"
    unavailable = parse_watch_response(
        tracking_response(changeTracking=None, summary="页面摘要")
    )
    assert unavailable.status == "unavailable"
    assert unavailable.summary == "页面摘要"


def test_parse_watch_response_summary_missing_or_null_is_none():
    assert parse_watch_response(tracking_response()).summary is None
    assert parse_watch_response(tracking_response(summary=None)).summary is None


def test_parse_watch_response_rejects_non_string_summary():
    with pytest.raises(ValueError, match="summary"):
        parse_watch_response(tracking_response(summary=123))
    with pytest.raises(ValueError, match="summary"):
        parse_watch_response(tracking_response(changeTracking=None, summary=["a"]))


def test_notification_identity_is_stable_and_boundary_safe():
    assert notification_identity(_PREVIOUS, "- a") == notification_identity(
        _PREVIOUS, "- a"
    )
    # 拼接歧义：状态时间与 diff 的不同切分不得产生相同身份。
    assert notification_identity("a", "bcd") != notification_identity("ab", "cd")
    # 相同前缀、不同后缀必须不同。
    assert notification_identity(_PREVIOUS, "- a\n+ b") != notification_identity(
        _PREVIOUS, "- a\n+ c"
    )
    assert _HEX64.fullmatch(notification_identity(_PREVIOUS, "- a"))


def test_build_watch_notification_contains_header_and_diff():
    result = WatchCheckResult(
        status="changed",
        previous_scrape_at=_PREVIOUS,
        diff_text="- old\n+ new",
        notification_hash="a" * 64,
    )
    message = build_watch_notification("https://example.com/a", result)
    assert "https://example.com/a" in message
    assert _PREVIOUS in message
    assert "- old\n+ new" in message
    assert len(message) <= WATCH_MAX_MESSAGE_CHARS


def test_build_watch_notification_short_diff_kept_intact():
    diff = "x" * 100
    result = WatchCheckResult(
        status="changed", previous_scrape_at=_PREVIOUS, diff_text=diff
    )
    message = build_watch_notification("https://example.com/a", result)
    assert message.endswith(diff)
    assert "截断" not in message


def test_build_watch_notification_truncates_to_limit():
    diff = "x" * (WATCH_MAX_MESSAGE_CHARS + 500)
    result = WatchCheckResult(
        status="changed", previous_scrape_at=_PREVIOUS, diff_text=diff
    )
    message = build_watch_notification("https://example.com/a", result)
    assert len(message) == WATCH_MAX_MESSAGE_CHARS
    assert f"完整 {len(diff)} 字符" in message
    assert message.count("x") < len(diff)


def test_append_watch_summary_preserves_status_and_bounds_text():
    base = "检查完成：页面无变化（same）。"
    assert append_watch_summary(base, None) == base
    assert append_watch_summary(base, "") == base
    message = append_watch_summary(base, "简短摘要")
    assert message == f"{base}\n\n页面摘要：\n简短摘要"
    assert append_watch_summary(base, "简短摘要", max_chars=len(message)) == message

    truncated = append_watch_summary(base, "摘" * 200, max_chars=100)
    assert len(truncated) == 100
    assert truncated.startswith(base)
    assert "摘要已截断" in truncated


def test_append_watch_summary_rejects_invalid_or_tiny_limit():
    for bad in (0, -1, True, "6000", None):
        with pytest.raises(ValueError, match="max_chars"):
            append_watch_summary("状态", "摘要", max_chars=bad)
    with pytest.raises(ValueError, match="容纳不下"):
        append_watch_summary("状态" * 20, "摘要", max_chars=20)


def test_build_watch_notification_places_summary_before_diff():
    result = WatchCheckResult(
        status="changed",
        previous_scrape_at=_PREVIOUS,
        diff_text="- old\n+ new",
        summary="页面摘要内容",
    )
    message = build_watch_notification("https://example.com/a", result)
    assert "页面摘要：\n页面摘要内容" in message
    assert message.index("页面摘要内容") < message.index("- old")


def test_build_watch_notification_bounds_summary_and_diff():
    result = WatchCheckResult(
        status="changed",
        previous_scrape_at=_PREVIOUS,
        diff_text="d" * 7000,
        summary="s" * 2500,
    )
    message = build_watch_notification("https://example.com/a", result)
    assert len(message) == WATCH_MAX_MESSAGE_CHARS
    assert message.count("s") <= 2000
    assert "摘要已截断" in message
    assert "diff 已截断：完整 7000 字符" in message
    assert message.index("摘要已截断") < message.index("diff 已截断")


def test_build_watch_notification_empty_summary_keeps_original_format():
    without_summary = WatchCheckResult(
        status="changed", previous_scrape_at=_PREVIOUS, diff_text="diff"
    )
    empty_summary = WatchCheckResult(
        status="changed", previous_scrape_at=_PREVIOUS, diff_text="diff", summary=""
    )
    assert build_watch_notification(
        "https://example.com/a", empty_summary
    ) == build_watch_notification("https://example.com/a", without_summary)


def test_build_watch_notification_rejects_non_changed():
    result = WatchCheckResult(status="same", previous_scrape_at=_PREVIOUS)
    with pytest.raises(ValueError, match="changed"):
        build_watch_notification("https://example.com/a", result)


def test_build_watch_notification_rejects_invalid_max_chars():
    result = WatchCheckResult(
        status="changed", previous_scrape_at=_PREVIOUS, diff_text="diff"
    )
    for bad in (0, -1, True, "6000", None):
        with pytest.raises(ValueError, match="max_chars"):
            build_watch_notification("https://example.com/a", result, max_chars=bad)


def test_build_watch_notification_rejects_tiny_max_chars():
    result = WatchCheckResult(
        status="changed", previous_scrape_at=_PREVIOUS, diff_text="diff"
    )
    with pytest.raises(ValueError, match="容纳不下"):
        build_watch_notification("https://example.com/a", result, max_chars=40)
