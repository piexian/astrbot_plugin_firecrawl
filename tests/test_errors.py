"""firecrawl_api.errors 分类测试。"""

from __future__ import annotations

import json
import time
from email.utils import formatdate

import httpx
import pytest

from firecrawl_api import (
    ErrorCategory,
    FirecrawlError,
    MissingKeyError,
    classify_error,
    friendly_message,
)


def response(
    status: int,
    body: dict | None = None,
    headers: dict | None = None,
) -> httpx.Response:
    return httpx.Response(
        status,
        json=body if body is not None else {},
        headers=headers,
    )


def error_body(error: str, code: str = "SOME_CODE") -> dict:
    return {"success": False, "error": error, "code": code}


def test_missing_key_is_config():
    err = classify_error(exc=MissingKeyError("api_keys"))
    assert err.category is ErrorCategory.CONFIG
    assert not err.retryable
    assert not err.rotate_key
    assert err.status is None
    assert "API Key" in friendly_message(err)


def test_classify_requires_exactly_one_input():
    with pytest.raises(ValueError):
        classify_error()
    with pytest.raises(ValueError):
        classify_error(response(500), httpx.ConnectError("x"))


def test_unknown_exception_rejected():
    with pytest.raises(ValueError):
        classify_error(exc=RuntimeError("boom"))


def test_firecrawl_error_passthrough():
    original = FirecrawlError(ErrorCategory.AUTH, "x")
    assert classify_error(exc=original) is original


def test_401_rotates_key():
    err = classify_error(
        response(
            401,
            error_body("Unauthorized", "UNAUTHORIZED"),
            {"x-request-id": "req-1"},
        )
    )
    assert err.category is ErrorCategory.AUTH
    assert err.rotate_key
    assert not err.retryable
    assert err.status == 401
    assert err.code == "UNAUTHORIZED"
    assert err.request_id == "req-1"
    assert "Unauthorized" in str(err)


def test_402_quota_rotates_key():
    err = classify_error(
        response(
            402,
            error_body(
                "Payment Required: Insufficient credits",
                "INSUFFICIENT_CREDITS",
            ),
        )
    )
    assert err.category is ErrorCategory.QUOTA
    assert err.rotate_key
    assert not err.retryable
    assert err.code == "INSUFFICIENT_CREDITS"


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("Invalid API key provided", None),
        ("Invalid API key provided", "SOME_CODE"),
        ("This website is not allowed to be scraped", "FORBIDDEN_TARGET"),
        ("Blocked by crawl safe-mode", None),
        ("Unknown policy verdict", "ACCESS_DENIED"),
    ],
)
def test_403_defaults_to_usage_without_explicit_context(message: str, code):
    body = {"success": False, "error": message}
    if code is not None:
        body["code"] = code
    err = classify_error(response(403, body))
    assert err.category is ErrorCategory.USAGE
    assert not err.rotate_key


def test_403_insufficient_permissions_code_is_auth():
    err = classify_error(
        response(403, error_body("Permission denied", "INSUFFICIENT_PERMISSIONS"))
    )
    assert err.category is ErrorCategory.AUTH
    assert err.rotate_key
    assert not err.retryable


def test_403_explicit_credential_context_rotates():
    err = classify_error(
        response(403, error_body("some target message", "WHATEVER")),
        credential_error=True,
    )
    assert err.category is ErrorCategory.AUTH
    assert err.rotate_key


def test_403_explicit_resource_context_stays_usage():
    err = classify_error(
        response(403, error_body("Invalid API key", "UNAUTHORIZED_LIKE")),
        credential_error=False,
    )
    assert err.category is ErrorCategory.USAGE
    assert not err.rotate_key


def test_429_parses_retry_after_seconds():
    err = classify_error(
        response(
            429,
            error_body("Rate limit exceeded", "RATE_LIMITED"),
            {"Retry-After": "45"},
        )
    )
    assert err.category is ErrorCategory.RATE_LIMIT
    assert err.retryable
    assert err.rotate_key
    assert err.retry_after == 45


def test_429_without_retry_after():
    err = classify_error(response(429, error_body("Rate limit exceeded")))
    assert err.retry_after is None


def test_429_parses_http_date():
    headers = {"Retry-After": formatdate(time.time() + 60, usegmt=True)}
    err = classify_error(response(429, error_body("Rate limit exceeded"), headers))
    assert err.retry_after is not None
    assert 0 < err.retry_after <= 60


@pytest.mark.parametrize("status", [400, 404, 409, 413])
def test_usage_statuses(status: int):
    err = classify_error(
        response(status, error_body("Bad Request: invalid url", "BAD_REQUEST"))
    )
    assert err.category is ErrorCategory.USAGE
    assert not err.retryable
    assert not err.rotate_key
    assert "Bad Request: invalid url" in str(err)


def test_422_retryable_only_when_explicit():
    default = classify_error(response(422, error_body("Unprocessable entity")))
    assert default.category is ErrorCategory.USAGE
    assert not default.retryable
    assert not default.rotate_key
    explicit = classify_error(
        response(422, error_body("Unprocessable entity")), retryable_422=True
    )
    assert explicit.category is ErrorCategory.USAGE
    assert explicit.retryable
    assert not explicit.rotate_key


@pytest.mark.parametrize("status", [400, 409, 500])
def test_422_flag_does_not_affect_other_statuses(status: int):
    err = classify_error(
        response(status, error_body("other failure")), retryable_422=True
    )
    if status == 500:
        assert err.retryable
    else:
        assert not err.retryable


@pytest.mark.parametrize("status", [408, 504])
def test_timeout_statuses(status: int):
    err = classify_error(response(status, error_body("Request timed out")))
    assert err.category is ErrorCategory.TIMEOUT
    assert err.retryable
    assert not err.rotate_key


@pytest.mark.parametrize("status", [500, 502, 503, 599])
def test_server_statuses(status: int):
    err = classify_error(response(status))
    assert err.category is ErrorCategory.SERVER
    assert err.retryable
    assert not err.rotate_key


@pytest.mark.parametrize(
    "exc", [httpx.ReadTimeout("timed out"), httpx.ConnectTimeout("timed out")]
)
def test_httpx_timeout(exc: Exception):
    err = classify_error(exc=exc)
    assert err.category is ErrorCategory.TIMEOUT
    assert err.retryable
    assert err.status is None


def test_httpx_request_error_is_network():
    err = classify_error(exc=httpx.ConnectError("connection refused"))
    assert err.category is ErrorCategory.NETWORK
    assert err.retryable
    assert not err.rotate_key


def test_200_success_false_is_usage():
    body = error_body("Scrape failed: page timed out", "SCRAPE_TIMEOUT")
    err = classify_error(response(200, body))
    assert err.category is ErrorCategory.USAGE
    assert not err.retryable
    assert not err.rotate_key
    assert err.code == "SCRAPE_TIMEOUT"
    assert err.details == body


def test_success_response_rejected():
    with pytest.raises(ValueError):
        classify_error(response(200, {"success": True, "data": {}}))
    with pytest.raises(ValueError):
        classify_error(response(302))


def test_key_is_masked_in_message():
    err = FirecrawlError(ErrorCategory.QUOTA, "failed with key fc-ab12cd34ef56gh78")
    text = str(err)
    assert "fc-ab12cd34ef56gh78" not in text
    assert "fc-ab12***" in text


def test_details_sanitized_recursively():
    details = {
        "error": "Invalid key fc-ab12cd34ef56gh78",
        "context": {
            "api_key": "fc-zyxwvu9876543210",
            "authorization": "Bearer fc-kjhgfd11223344",
            "urls": ("https://a.com/?token=fc-qwerty12345678",),
            "attempts": 2,
        },
    }
    err = FirecrawlError(ErrorCategory.AUTH, "failed", details=details)
    rendered = json.dumps(err.details)
    assert "fc-ab12cd34ef56gh78" not in rendered
    assert "fc-zyxwvu9876543210" not in rendered
    assert err.details["context"]["api_key"] == "***"
    assert err.details["context"]["authorization"] == "***"
    assert err.details["context"]["urls"][0] == "https://a.com/?token=fc-qwer***"
    assert err.details["context"]["attempts"] == 2
    assert isinstance(err.details["context"]["urls"], tuple)


def test_friendly_message():
    usage = classify_error(response(400, error_body("URL is not valid", "BAD_REQUEST")))
    assert friendly_message(usage) == "URL is not valid"

    rate = classify_error(
        response(
            429,
            error_body("Rate limit exceeded", "RATE_LIMITED"),
            {"Retry-After": "30"},
        )
    )
    assert "30 秒" in friendly_message(rate)

    quota = classify_error(response(402, error_body("Insufficient credits")))
    assert "额度" in friendly_message(quota)


@pytest.mark.parametrize(
    ("status", "kwargs"),
    [
        (403, {"credential_error": "yes"}),
        (403, {"credential_error": 1}),
        (403, {"credential_error": "true"}),
        (422, {"retryable_422": 1}),
        (422, {"retryable_422": None}),
        (422, {"retryable_422": "true"}),
    ],
)
def test_classify_flags_reject_non_bool(status: int, kwargs):
    with pytest.raises(ValueError):
        classify_error(response(status, error_body("body")), **kwargs)


def test_classify_flags_validated_even_for_exc_path():
    with pytest.raises(ValueError):
        classify_error(exc=httpx.ConnectError("x"), retryable_422=1)


def test_classify_flags_accept_bool_and_none():
    err = classify_error(
        response(403, error_body("body")), credential_error=True, retryable_422=False
    )
    assert err.category is ErrorCategory.AUTH
    err = classify_error(
        response(403, error_body("body")), credential_error=None, retryable_422=True
    )
    assert err.category is ErrorCategory.USAGE
    timeout = classify_error(exc=httpx.ReadTimeout("timed out"))
    assert friendly_message(timeout) == "请求超时，请稍后重试。"
