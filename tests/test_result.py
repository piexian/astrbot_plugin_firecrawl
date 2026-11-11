"""tools.result ok/fail 测试：JSON 往返完整性与错误收敛脱敏。"""

from __future__ import annotations

import asyncio
import json

import pytest
from astrbot_plugin_firecrawl.firecrawl_api.budget import BudgetExceeded
from astrbot_plugin_firecrawl.firecrawl_api.errors import (
    ErrorCategory,
    FirecrawlError,
)
from astrbot_plugin_firecrawl.tools.execution import PendingRequestError
from astrbot_plugin_firecrawl.tools.result import fail, ok

# 假敏感标记：不带 fc 前缀，构造错误时不会被 mask_key 打码。
MARKER = "LEAK-MARKER-999"


def test_ok_round_trip_preserves_structure():
    data = {
        "text": "中文结果",
        "flag": True,
        "none": None,
        "nested": [1, [2, {"x": False}], {"y": [None]}],
    }
    out = ok(data)
    assert out.startswith("{")
    assert "```" not in out
    assert "中文结果" in out
    assert "\\u4e2d" not in out
    assert json.loads(out) == {"ok": True, "data": data}


def test_ok_large_object_complete():
    data = {f"key_{i:04d}": {"index": i, "text": "x" * 200} for i in range(2000)}
    out = ok(data)
    assert json.loads(out) == {"ok": True, "data": data}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_ok_rejects_non_finite(value):
    with pytest.raises(ValueError):
        ok({"v": value})


@pytest.mark.parametrize("value", [{1, 2}, object()])
def test_ok_rejects_non_serializable(value):
    with pytest.raises(TypeError):
        ok({"v": value})


def test_pending_with_handle():
    err = PendingRequestError("rid-123", task_handle="crawl://task-x")
    payload = json.loads(fail(err))
    assert payload["ok"] is False
    assert payload["category"] == "indeterminate"
    assert payload["pending"] is True
    assert payload["retryable"] is False
    assert payload["reservation_id"] == "rid-123"
    assert payload["task_handle"] == "crawl://task-x"
    assert "重发" in payload["hint"]
    assert "status" in payload["hint"]


def test_pending_without_handle():
    err = PendingRequestError("rid-456")
    payload = json.loads(fail(err))
    assert payload["reservation_id"] == "rid-456"
    assert "task_handle" not in payload
    assert "重发" in payload["hint"]
    assert "pending" in payload["hint"]


def test_pending_cause_not_leaked():
    cause = FirecrawlError(ErrorCategory.AUTH, f"server key {MARKER}", status=401)
    try:
        raise PendingRequestError("rid-789") from cause
    except PendingRequestError as err:
        payload = json.loads(fail(err))
    blob = json.dumps(payload, ensure_ascii=False)
    assert MARKER not in blob
    assert "server key" not in blob


@pytest.mark.parametrize(
    ("category", "retryable"),
    [
        (ErrorCategory.CONFIG, False),
        (ErrorCategory.AUTH, False),
        (ErrorCategory.QUOTA, False),
        (ErrorCategory.RATE_LIMIT, True),
        (ErrorCategory.USAGE, False),
        (ErrorCategory.TIMEOUT, True),
        (ErrorCategory.SERVER, True),
        (ErrorCategory.NETWORK, True),
    ],
)
def test_firecrawl_error_category_and_retryable(category, retryable):
    err = FirecrawlError(category, "raw", retryable=retryable)
    payload = json.loads(fail(err))
    assert payload["ok"] is False
    assert payload["category"] == category.value
    assert payload["retryable"] is retryable
    assert payload["error"]
    assert payload["hint"]


def test_remote_usage_fixed_hint_no_server_echo():
    err = FirecrawlError(
        ErrorCategory.USAGE,
        f"remote says {MARKER}",
        status=422,
        code=f"code_{MARKER}",
        details={"error": MARKER},
        request_id=MARKER,
    )
    payload = json.loads(fail(err))
    blob = json.dumps(payload, ensure_ascii=False)
    assert MARKER not in blob
    assert payload["category"] == "usage"
    assert "details" not in payload
    assert "code" not in payload
    assert "request_id" not in payload


def test_remote_config_fixed_hint_no_server_echo():
    err = FirecrawlError(ErrorCategory.CONFIG, f"remote cfg {MARKER}", status=403)
    payload = json.loads(fail(err))
    blob = json.dumps(payload, ensure_ascii=False)
    assert MARKER not in blob
    assert payload["category"] == "config"


def test_local_budget_reason_kept():
    err = BudgetExceeded("日预算已用尽，今日已用 10.5 credits")
    payload = json.loads(fail(err))
    assert payload["category"] == "usage"
    assert payload["retryable"] is False
    assert "日预算已用尽" in payload["error"]


def test_usage_error_masks_key_and_attaches_example():
    err = ValueError("query 含非法凭据 fc-FAKE-SECRET-MARKER")
    example = {"query": "hello"}
    payload = json.loads(fail(err, example=example))
    assert payload["category"] == "usage"
    assert payload["retryable"] is False
    assert "fc-FAKE***" in payload["error"]
    assert "SECRET-MARKER" not in json.dumps(payload, ensure_ascii=False)
    assert payload["example"] == {"query": "hello"}
    assert example == {"query": "hello"}


@pytest.mark.parametrize(
    "err_factory",
    [
        lambda: BudgetExceeded("日预算已用尽"),
        lambda: FirecrawlError(ErrorCategory.USAGE, "bad parameter"),
        lambda: FirecrawlError(
            ErrorCategory.USAGE, f"remote says {MARKER}", status=400
        ),
    ],
    ids=["budget", "local_usage", "remote_usage"],
)
def test_usage_category_attaches_example(err_factory):
    example = {"query": "example"}
    payload = json.loads(fail(err_factory(), example=example))
    assert payload["category"] == "usage"
    assert payload["example"] == {"query": "example"}
    assert example == {"query": "example"}


@pytest.mark.parametrize(
    "category",
    [
        ErrorCategory.CONFIG,
        ErrorCategory.AUTH,
        ErrorCategory.QUOTA,
        ErrorCategory.RATE_LIMIT,
        ErrorCategory.TIMEOUT,
        ErrorCategory.SERVER,
        ErrorCategory.NETWORK,
    ],
)
def test_non_usage_ignores_example(category):
    payload = json.loads(fail(FirecrawlError(category, "x"), example={"query": "x"}))
    assert payload["category"] != "usage"
    assert "example" not in payload


def test_pending_ignores_example():
    payload = json.loads(fail(PendingRequestError("rid-x"), example={"a": 1}))
    assert "example" not in payload


def test_unknown_exception_opaque():
    err = RuntimeError(f"boom {MARKER} at /etc/secret")
    payload = json.loads(fail(err))
    blob = json.dumps(payload, ensure_ascii=False)
    assert payload["ok"] is False
    assert payload["category"] == "server"
    assert payload["retryable"] is False
    assert MARKER not in blob
    assert "boom" not in blob
    assert "管理员" in payload["hint"]
    assert "重试" not in payload["error"]
    assert "重试" not in payload["hint"]


def test_cancelled_error_propagates():
    with pytest.raises(asyncio.CancelledError):
        fail(asyncio.CancelledError())


def test_other_remote_409_keeps_generic_hint():
    """非完成任务的 409 保持通用远端提示，不误判为无需取消。"""
    err = FirecrawlError(
        ErrorCategory.USAGE,
        "duplicate request",
        status=409,
        code="duplicate_request",
        details={"error": "duplicate request"},
    )
    payload = json.loads(fail(err))
    assert payload["error"] == "请求被 Firecrawl 拒绝，请检查目标站点与请求用法。"
    assert (
        payload["hint"] == "请确认请求符合 Firecrawl 的用法限制，或更换目标站点后重试。"
    )


def test_remote_error_log_masked_single_line_and_bounded(caplog):
    """远端日志保留原文前段，Key 脱敏、控制字符转义且整条最多 1000 字符。"""
    raw_error = "bad fc-FAKE-SECRET-MARKER\n" + "x" * 2000
    err = FirecrawlError(
        ErrorCategory.USAGE,
        raw_error,
        status=409,
        code="CONFLICT-fc-FAKE-SECRET-MARKER\nDETAIL",
        details={"error": raw_error},
        request_id="req-log-1",
    )
    with caplog.at_level("WARNING", logger="astrbot.stub"):
        out = fail(err)
    records = [record for record in caplog.records if record.name == "astrbot.stub"]
    assert len(records) == 1
    message = records[0].getMessage()
    assert len(message) == 1000
    assert message.endswith("…[truncated]")
    assert "\n" not in message
    assert "fc-FAKE***" in message
    assert "SECRET-MARKER" not in message
    assert "request_id='req-log-1'" in message
    assert "SECRET-MARKER" not in out


def test_pending_logs_direct_remote_cause(caplog):
    """pending 记录直接远端原因，返回 JSON 仍不包含远端原文。"""
    raw_error = "prompt injection detected in raw page"
    remote = FirecrawlError(
        ErrorCategory.USAGE,
        raw_error,
        status=403,
        code="SCRAPE_PROMPT_INJECTION_DETECTED",
        details={"error": raw_error},
        request_id="req-injection-1",
    )
    pending = PendingRequestError("rid-log")
    pending.__cause__ = remote
    with caplog.at_level("WARNING", logger="astrbot.stub"):
        out = fail(pending)
    payload = json.loads(out)
    assert payload["category"] == "indeterminate"
    assert raw_error not in out
    records = [record for record in caplog.records if record.name == "astrbot.stub"]
    assert len(records) == 1
    message = records[0].getMessage()
    assert raw_error in message
    assert "SCRAPE_PROMPT_INJECTION_DETECTED" in message
    assert "request_id='req-injection-1'" in message
