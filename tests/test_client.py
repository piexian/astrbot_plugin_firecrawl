"""firecrawl_api.client FirecrawlClient 测试。"""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest
from astrbot_plugin_firecrawl.firecrawl_api.budget import ReservationState
from astrbot_plugin_firecrawl.persistence import SQLiteStore

from firecrawl_api import (
    ErrorCategory,
    FirecrawlClient,
    FirecrawlError,
    KeyPool,
)
from firecrawl_api.client import KEY_REF_EXTENSION
from firecrawl_api.keys import key_reference

K1 = "fc-aaaaaaaa1111"
K2 = "fc-bbbbbbbb2222"
K3 = "fc-cccccccc3333"
K4 = "fc-dddddddd4444"
FP = "b" * 64


def sha_ref(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


class LogRecorder:
    """捕获 warning 调用的极简 logger。"""

    def __init__(self) -> None:
        self.records: list[str] = []

    def warning(self, message: str, *args) -> None:
        self.records.append(message % args if args else message)


def ok() -> httpx.Response:
    return httpx.Response(200, json={"success": True})


def unauthorized() -> httpx.Response:
    return httpx.Response(401, json={"success": False, "error": "Unauthorized"})


def make_transport(responses: list) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """按顺序回放响应或抛出异常，并记录收到的请求。"""
    requests: list[httpx.Request] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.MockTransport(handler), requests


def make_client(keys: list[str], responses: list, **kwargs):
    transport, requests = make_transport(responses)
    pool = KeyPool(keys)
    client = FirecrawlClient(pool, transport=transport, **kwargs)
    return client, pool, requests


@pytest.mark.asyncio
async def test_request_path_auth_body_and_query():
    client, _, requests = make_client([K1], [ok()])
    response = await client.request(
        "POST", "/scrape", json={"url": "https://example.com"}, params={"from": "test"}
    )
    assert response.status_code == 200
    request = requests[0]
    assert str(request.url) == "https://api.firecrawl.dev/v2/scrape?from=test"
    assert request.headers["authorization"] == f"Bearer {K1}"
    assert json.loads(request.content) == {"url": "https://example.com"}
    await client.aclose()


@pytest.mark.asyncio
async def test_injected_client_keeps_configured_base_url_timeout_and_key():
    transport, requests = make_transport([ok()])
    injected = httpx.AsyncClient(
        transport=transport,
        base_url="https://injected.example",
        headers={"Authorization": "Bearer injected-default"},
        timeout=0.001,
    )
    client = FirecrawlClient(
        KeyPool([K1]),
        client=injected,
        base_url="https://configured.example",
        timeout_seconds=5,
    )
    await client.request("POST", "/scrape", json={"url": "https://example.com"})
    request = requests[0]
    assert str(request.url).startswith("https://configured.example/v2")
    assert request.headers["authorization"] == f"Bearer {K1}"
    assert request.extensions["timeout"] == httpx.Timeout(5.0).as_dict()
    await client.aclose()
    assert not injected.is_closed
    await injected.aclose()


@pytest.mark.asyncio
async def test_anonymous_request_strips_injected_default_authorization():
    transport, requests = make_transport([ok()])
    injected = httpx.AsyncClient(
        transport=transport, headers={"Authorization": "Bearer injected-default"}
    )
    client = FirecrawlClient(KeyPool([]), client=injected)
    await client.request("POST", "/search", json={"query": "hello"})
    assert "authorization" not in requests[0].headers
    await injected.aclose()
    assert injected.is_closed


@pytest.mark.asyncio
async def test_each_unique_key_tried_once():
    responses = [unauthorized(), unauthorized(), unauthorized()]
    client, _, requests = make_client([f" {K1} ", K1, K2, K3], responses)
    with pytest.raises(FirecrawlError) as exc_info:
        await client.request("POST", "/crawl")
    assert exc_info.value.category is ErrorCategory.AUTH
    assert len(requests) == 3
    auths = [r.headers["authorization"] for r in requests]
    assert auths == [f"Bearer {K1}", f"Bearer {K2}", f"Bearer {K3}"]
    await client.aclose()


@pytest.mark.asyncio
async def test_401_402_429_rotate_until_success():
    responses = [
        httpx.Response(401, json={"error": "bad key"}),
        httpx.Response(402, json={"error": "no credits"}),
        httpx.Response(429, json={"error": "slow down"}, headers={"Retry-After": "3"}),
        ok(),
    ]
    client, pool, requests = make_client([K1, K2, K3, K4], responses)
    response = await client.request(
        "POST", "/scrape", json={"url": "https://example.com"}
    )
    assert response.status_code == 200
    assert len(requests) == 4
    summary = pool.summary()["keys"]
    assert summary[0]["cooling"] and summary[0]["cooldown_reason"] == "auth"
    assert summary[1]["cooling"] and summary[1]["cooldown_reason"] == "quota"
    assert summary[2]["cooldown_seconds_left"] == 3
    assert not summary[3]["cooling"]
    await client.aclose()


@pytest.mark.asyncio
async def test_last_error_returned_when_all_keys_fail():
    responses = [
        httpx.Response(429, json={"error": "first"}, headers={"Retry-After": "1"}),
        httpx.Response(429, json={"error": "second"}, headers={"Retry-After": "2"}),
    ]
    client, _, requests = make_client([K1, K2], responses)
    with pytest.raises(FirecrawlError) as exc_info:
        await client.request("GET", "/team/credit-usage")
    error = exc_info.value
    assert error.category is ErrorCategory.RATE_LIMIT
    assert error.retry_after == 2
    assert error.details["error"] == "second"
    assert len(requests) == 2
    await client.aclose()


@pytest.mark.parametrize(
    ("status", "category"),
    [
        (400, ErrorCategory.USAGE),
        (409, ErrorCategory.USAGE),
        (413, ErrorCategory.USAGE),
        (422, ErrorCategory.USAGE),
        (500, ErrorCategory.SERVER),
    ],
)
@pytest.mark.asyncio
async def test_usage_and_server_errors_do_not_rotate(status, category):
    client, _, requests = make_client(
        [K1, K2], [httpx.Response(status, json={"error": "nope"})]
    )
    with pytest.raises(FirecrawlError) as exc_info:
        await client.request("POST", "/scrape", json={"url": "https://example.com"})
    assert exc_info.value.category is category
    assert exc_info.value.rotate_key is False
    assert len(requests) == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_403_credential_context_passed_through():
    default_client, _, default_requests = make_client(
        [K1, K2],
        [httpx.Response(403, json={"success": False, "error": "Invalid API key"})],
    )
    with pytest.raises(FirecrawlError) as exc_info:
        await default_client.request("POST", "/scrape", json={"url": "x"})
    assert exc_info.value.category is ErrorCategory.USAGE
    assert len(default_requests) == 1
    await default_client.aclose()

    truthy_client, _, truthy_requests = make_client(
        [K1, K2],
        [
            httpx.Response(403, json={"success": False, "error": "Invalid API key"}),
            ok(),
        ],
    )
    response = await truthy_client.request(
        "POST", "/scrape", json={"url": "x"}, credential_error=True
    )
    assert response.status_code == 200
    auths = [r.headers["authorization"] for r in truthy_requests]
    assert auths == [f"Bearer {K1}", f"Bearer {K2}"]
    await truthy_client.aclose()

    falsy_client, _, falsy_requests = make_client(
        [K1, K2],
        [httpx.Response(403, json={"success": False, "error": "Invalid API key"})],
    )
    with pytest.raises(FirecrawlError) as exc_info:
        await falsy_client.request(
            "POST", "/scrape", json={"url": "x"}, credential_error=False
        )
    assert exc_info.value.category is ErrorCategory.USAGE
    assert len(falsy_requests) == 1
    await falsy_client.aclose()


@pytest.mark.asyncio
async def test_request_rejects_invalid_flags_before_sending():
    client, _, requests = make_client([K1], [ok()])
    with pytest.raises(ValueError):
        await client.request(
            "POST", "/scrape", json={"url": "x"}, credential_error="yes"
        )
    with pytest.raises(ValueError):
        await client.request("POST", "/scrape", json={"url": "x"}, retryable_422=1)
    assert requests == []
    await client.aclose()


@pytest.mark.asyncio
async def test_422_retryable_flag_passed_through_without_rotation():
    client, _, requests = make_client(
        [K1, K2], [httpx.Response(422, json={"error": "extract schema"})]
    )
    with pytest.raises(FirecrawlError) as exc_info:
        await client.request("POST", "/extract", json={"url": "x"}, retryable_422=True)
    assert exc_info.value.retryable
    assert exc_info.value.rotate_key is False
    assert len(requests) == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_timeout_does_not_rotate():
    logs = LogRecorder()
    client, _, requests = make_client(
        [K1, K2], [httpx.ReadTimeout("too slow")], logger=logs
    )
    with pytest.raises(FirecrawlError) as exc_info:
        await client.request("POST", "/scrape", json={"url": "https://example.com"})
    assert exc_info.value.category is ErrorCategory.TIMEOUT
    assert len(requests) == 1
    assert logs.records == [
        f"Firecrawl 请求失败：key=sha256:{sha_ref(K1)} "
        "category=timeout status=transport"
    ]
    await client.aclose()


@pytest.mark.asyncio
async def test_network_error_does_not_rotate():
    client, _, requests = make_client([K1, K2], [httpx.ConnectError("refused")])
    with pytest.raises(FirecrawlError) as exc_info:
        await client.request("POST", "/scrape", json={"url": "https://example.com"})
    assert exc_info.value.category is ErrorCategory.NETWORK
    assert len(requests) == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_200_success_false_is_usage_error():
    client, _, requests = make_client(
        [K1], [httpx.Response(200, json={"success": False, "error": "blocked"})]
    )
    with pytest.raises(FirecrawlError) as exc_info:
        await client.request("POST", "/search", json={"query": "x"})
    assert exc_info.value.category is ErrorCategory.USAGE
    assert len(requests) == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_200_invalid_json_is_not_success():
    client, _, requests = make_client([K1], [httpx.Response(200, text="not-json{")])
    with pytest.raises(FirecrawlError) as exc_info:
        await client.request("POST", "/scrape")
    assert exc_info.value.category is ErrorCategory.USAGE
    assert len(requests) == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_success_clears_cooldown_and_logs_nothing():
    logs = LogRecorder()
    client, pool, requests = make_client([K1], [ok()], logger=logs)
    await pool.mark_failure(
        K1, FirecrawlError(ErrorCategory.RATE_LIMIT, "限流", retry_after=60)
    )
    assert pool.summary()["keys"][0]["cooling"]
    await client.request("POST", "/scrape")
    assert not pool.summary()["keys"][0]["cooling"]
    assert logs.records == []
    await client.aclose()


@pytest.mark.asyncio
async def test_missing_key_non_whitelisted_rejected_without_request():
    client, _, requests = make_client([], [])
    with pytest.raises(FirecrawlError) as exc_info:
        await client.request("GET", "/team/credit-usage")
    assert exc_info.value.category is ErrorCategory.CONFIG
    assert requests == []
    await client.aclose()


@pytest.mark.asyncio
async def test_anonymous_whitelist_only_post_scrape_and_search():
    client, _, requests = make_client([], [ok(), ok()])
    await client.request("POST", "/scrape", json={"url": "https://example.com"})
    await client.request("POST", "/search", json={"query": "x"})
    with pytest.raises(FirecrawlError) as exc_info:
        await client.request("GET", "/scrape")
    assert exc_info.value.category is ErrorCategory.CONFIG
    assert len(requests) == 2
    assert all("authorization" not in r.headers for r in requests)
    await client.aclose()


@pytest.mark.asyncio
async def test_failure_warning_does_not_leak_key_or_body():
    logs = LogRecorder()
    client, _, _ = make_client([K1, K2], [unauthorized(), unauthorized()], logger=logs)
    with pytest.raises(FirecrawlError):
        await client.request("POST", "/crawl")
    joined = "\n".join(logs.records)
    assert len(logs.records) == 2
    assert K1 not in joined and K2 not in joined
    assert "Bearer" not in joined
    assert "Unauthorized" not in joined
    assert f"key=sha256:{sha_ref(K1)}" in logs.records[0]
    assert "category=auth" in logs.records[0]
    assert "status=401" in logs.records[0]
    await client.aclose()


@pytest.mark.asyncio
async def test_aclose_closes_owned_client():
    client, _, _ = make_client([K1], [ok(), ok()])
    await client.request("POST", "/scrape")
    await client.aclose()
    with pytest.raises(RuntimeError):
        await client.request("POST", "/scrape")


@pytest.mark.asyncio
async def test_aclose_keeps_injected_client_open():
    transport, requests = make_transport([ok(), ok()])
    injected = httpx.AsyncClient(transport=transport)
    client = FirecrawlClient(KeyPool([K1]), client=injected)
    await client.request("POST", "/scrape")
    await client.aclose()
    assert not injected.is_closed
    response = await client.request("POST", "/scrape")
    assert response.status_code == 200
    await injected.aclose()
    assert injected.is_closed
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_proxy_path_base_url_joined_with_v2():
    transport, requests = make_transport([ok()])
    client = FirecrawlClient(
        KeyPool([K1]), base_url="https://proxy.example/fc/", transport=transport
    )
    await client.request("POST", "/scrape")
    assert str(requests[0].url) == "https://proxy.example/fc/v2/scrape"
    await client.aclose()


@pytest.mark.parametrize(
    "base_url",
    [
        "https://user:pass@api.firecrawl.dev",
        "https://api.firecrawl.dev/?x=1",
        "https://api.firecrawl.dev/#frag",
        "ftp://api.firecrawl.dev",
        "api.firecrawl.dev",
    ],
)
def test_invalid_base_url_rejected(base_url):
    with pytest.raises(FirecrawlError) as exc_info:
        FirecrawlClient(KeyPool([K1]), base_url=base_url)
    assert exc_info.value.category is ErrorCategory.CONFIG


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://evil.example/v2/scrape",
        "//evil.example/scrape",
        "scrape",
        "/v2/../v1/scrape",
        "/scrape/../../etc",
        "/scrape?x=1",
        "/scrape#frag",
    ],
)
@pytest.mark.asyncio
async def test_invalid_endpoint_rejected_without_request(endpoint):
    client, _, requests = make_client([K1], [])
    with pytest.raises(FirecrawlError) as exc_info:
        await client.request("POST", endpoint)
    assert exc_info.value.category is ErrorCategory.CONFIG
    assert requests == []
    await client.aclose()


@pytest.mark.asyncio
async def test_anonymous_requests_share_error_classification():
    client, _, requests = make_client([], [unauthorized()])
    with pytest.raises(FirecrawlError) as exc_info:
        await client.request("POST", "/scrape")
    assert exc_info.value.category is ErrorCategory.AUTH
    assert len(requests) == 1
    await client.aclose()

    client, _, requests = make_client(
        [], [httpx.Response(200, json={"success": False, "error": "blocked"})]
    )
    with pytest.raises(FirecrawlError) as exc_info:
        await client.request("POST", "/search", json={"query": "x"})
    assert exc_info.value.category is ErrorCategory.USAGE
    await client.aclose()

    client, _, requests = make_client([], [httpx.ReadTimeout("too slow")])
    with pytest.raises(FirecrawlError) as exc_info:
        await client.request("POST", "/scrape")
    assert exc_info.value.category is ErrorCategory.TIMEOUT
    assert len(requests) == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_anonymous_strips_caller_supplied_authorization():
    client, _, requests = make_client([], [ok()])
    await client.request("POST", "/scrape", headers={"Authorization": "Bearer caller"})
    assert "authorization" not in requests[0].headers
    await client.aclose()


@pytest.mark.asyncio
async def test_anonymous_disables_injected_client_auth():
    transport, requests = make_transport([ok()])
    injected = httpx.AsyncClient(transport=transport, auth=("user", "pass"))
    client = FirecrawlClient(KeyPool([]), client=injected)
    await client.request("POST", "/scrape")
    assert "authorization" not in requests[0].headers
    await injected.aclose()
    assert injected.is_closed


@pytest.mark.asyncio
async def test_duplicate_mixed_case_authorization_replaced_by_pool_key():
    transport, requests = make_transport([ok()])
    injected = httpx.AsyncClient(transport=transport, auth=("user", "pass"))
    client = FirecrawlClient(KeyPool([K1]), client=injected)
    await client.request(
        "POST",
        "/scrape",
        headers={
            "aUthOrIzAtIoN": "Bearer stale1",
            "Authorization": "Bearer stale2",
        },
    )
    assert requests[0].headers.get_list("authorization") == [f"Bearer {K1}"]
    await injected.aclose()
    assert injected.is_closed


@pytest.mark.asyncio
async def test_redirect_not_followed_and_reported_as_usage():
    client, _, requests = make_client(
        [K1],
        [httpx.Response(301, headers={"Location": "https://evil.example/v2/scrape"})],
    )
    with pytest.raises(FirecrawlError) as exc_info:
        await client.request("POST", "/scrape")
    error = exc_info.value
    assert error.category is ErrorCategory.USAGE
    assert error.status == 301
    assert error.rotate_key is False
    assert len(requests) == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_injected_follow_redirects_cannot_trigger_second_request():
    transport, requests = make_transport(
        [httpx.Response(302, headers={"Location": "https://evil.example/v2/scrape"})]
    )
    injected = httpx.AsyncClient(transport=transport, follow_redirects=True)
    client = FirecrawlClient(KeyPool([K1]), client=injected)
    with pytest.raises(FirecrawlError) as exc_info:
        await client.request("POST", "/scrape")
    assert exc_info.value.status == 302
    assert len(requests) == 1
    assert requests[0].url.host == "api.firecrawl.dev"
    await injected.aclose()
    assert injected.is_closed


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.firecrawl.dev/v2",
        "https://api.firecrawl.dev/v2/",
        "https://proxy.example/fc/v2",
    ],
)
def test_base_url_with_v2_suffix_rejected(base_url):
    with pytest.raises(FirecrawlError) as exc_info:
        FirecrawlClient(KeyPool([K1]), base_url=base_url)
    assert exc_info.value.category is ErrorCategory.CONFIG


@pytest.mark.asyncio
async def test_pinned_key_sends_exact_key_without_touching_cursor():
    client, _, requests = make_client([K1, K2], [ok(), ok()])
    response = await client.request("POST", "/scrape", key_ref=key_reference(K2))
    assert requests[0].headers["authorization"] == f"Bearer {K2}"
    assert response.extensions[KEY_REF_EXTENSION] == key_reference(K2)
    await client.request("POST", "/scrape")
    assert requests[1].headers["authorization"] == f"Bearer {K1}"
    await client.aclose()


@pytest.mark.asyncio
async def test_pinned_cooled_key_is_still_tried_and_clears_on_success():
    client, pool, requests = make_client([K1, K2], [ok()])
    await pool.mark_failure(
        K2, FirecrawlError(ErrorCategory.RATE_LIMIT, "限流", retry_after=60)
    )
    assert pool.summary()["keys"][1]["cooling"]
    response = await client.request("POST", "/scrape", key_ref=key_reference(K2))
    assert requests[0].headers["authorization"] == f"Bearer {K2}"
    assert not pool.summary()["keys"][1]["cooling"]
    assert response.extensions[KEY_REF_EXTENSION] == key_reference(K2)
    await client.aclose()


def bad_key() -> httpx.Response:
    return httpx.Response(401, json={"error": "bad key"})


def no_credits() -> httpx.Response:
    return httpx.Response(402, json={"error": "no credits"})


def no_permission() -> httpx.Response:
    return httpx.Response(
        403,
        json={
            "success": False,
            "error": "Invalid API key",
            "code": "INSUFFICIENT_PERMISSIONS",
        },
    )


def slow_down() -> httpx.Response:
    return httpx.Response(
        429, json={"error": "slow down"}, headers={"Retry-After": "5"}
    )


@pytest.mark.parametrize(
    ("respond", "category", "reason"),
    [
        (bad_key, ErrorCategory.AUTH, "auth"),
        (no_credits, ErrorCategory.QUOTA, "quota"),
        (no_permission, ErrorCategory.AUTH, "auth"),
        (slow_down, ErrorCategory.RATE_LIMIT, "rate_limit"),
    ],
)
@pytest.mark.asyncio
async def test_pinned_key_fails_fast_without_rotation(respond, category, reason):
    client, pool, requests = make_client([K1, K2], [respond()])
    with pytest.raises(FirecrawlError) as exc_info:
        await client.request("POST", "/scrape", key_ref=key_reference(K2))
    error = exc_info.value
    assert error.category is category
    assert len(requests) == 1
    assert requests[0].headers["authorization"] == f"Bearer {K2}"
    summary = pool.summary()["keys"]
    assert summary[1]["cooling"] and summary[1]["cooldown_reason"] == reason
    assert not summary[0]["cooling"]
    await client.aclose()


@pytest.mark.parametrize(
    "key_ref",
    [
        "0" * 64,
        "abc123",
        key_reference(K1).upper(),
        "",
        123,
    ],
)
@pytest.mark.asyncio
async def test_invalid_key_ref_rejected_without_network_or_echo(key_ref):
    client, _, requests = make_client([K1, K2], [])
    with pytest.raises(FirecrawlError) as exc_info:
        await client.request("POST", "/scrape", key_ref=key_ref)
    error = exc_info.value
    assert error.category is ErrorCategory.CONFIG
    assert requests == []
    text = str(error)
    ref_text = str(key_ref)
    if ref_text:
        assert ref_text not in text
    assert K1 not in text and K2 not in text
    await client.aclose()


@pytest.mark.asyncio
async def test_pinned_ref_on_empty_pool_rejected_without_network():
    client, _, requests = make_client([], [])
    with pytest.raises(FirecrawlError) as exc_info:
        await client.request("POST", "/scrape", key_ref=key_reference(K1))
    assert exc_info.value.category is ErrorCategory.CONFIG
    assert requests == []
    await client.aclose()


@pytest.mark.asyncio
async def test_rotation_success_reports_final_actual_key_summary():
    responses = [
        unauthorized(),
        httpx.Response(402, json={"error": "no credits"}),
        ok(),
    ]
    client, _, requests = make_client([K1, K2, K3], responses)
    response = await client.request("POST", "/scrape")
    assert requests[2].headers["authorization"] == f"Bearer {K3}"
    assert response.extensions[KEY_REF_EXTENSION] == key_reference(K3)
    await client.aclose()


@pytest.mark.asyncio
async def test_anonymous_success_extension_is_none_without_authorization():
    client, _, requests = make_client([], [ok()])
    response = await client.request("POST", "/scrape")
    assert response.extensions[KEY_REF_EXTENSION] is None
    assert "authorization" not in requests[0].headers
    await client.aclose()


@pytest.mark.asyncio
async def test_success_keeps_response_and_other_extensions_without_key_leak():
    served = ok()
    served.extensions["transport.custom"] = "keep"
    client, _, requests = make_client([K2], [served])
    response = await client.request("POST", "/scrape")
    assert isinstance(response, httpx.Response)
    assert json.loads(response.content) == {"success": True}
    assert response.request is requests[0]
    assert response.extensions["transport.custom"] == "keep"
    assert response.extensions[KEY_REF_EXTENSION] == key_reference(K2)
    assert K2 not in str(response.extensions)
    await client.aclose()


@pytest.mark.asyncio
async def test_before_send_persists_submitting_across_rotation(tmp_path):
    store = SQLiteStore(tmp_path / "budget.db")
    await store.initialize()
    await store.reserve(5, kind="scrape", request_fingerprint=FP, reservation_id="r-1")
    calls: list[tuple[str | None, bool]] = []
    order: list[str] = []

    async def on_send(ref, confirmed):
        calls.append((ref, confirmed))
        order.append("callback")
        await store.mark_submitting(
            "r-1", key_ref=ref, confirmed_not_accepted=confirmed
        )

    queue = [bad_key(), no_credits(), ok()]

    async def handler(request: httpx.Request) -> httpx.Response:
        record = await store.get("r-1")
        actual = request.headers["authorization"].removeprefix("Bearer ")
        assert record.state is ReservationState.SUBMITTING
        assert record.key_ref == key_reference(actual)
        order.append("transport")
        return queue.pop(0)

    client = FirecrawlClient(
        KeyPool([K1, K2, K3]), transport=httpx.MockTransport(handler)
    )
    response = await client.request(
        "POST", "/scrape", json={"url": "https://example.com"}, before_send=on_send
    )
    await client.aclose()
    assert response.status_code == 200
    assert order == ["callback", "transport"] * 3
    assert calls == [
        (key_reference(K1), False),
        (key_reference(K2), True),
        (key_reference(K3), True),
    ]
    assert response.extensions[KEY_REF_EXTENSION] == key_reference(K3)
    record = await store.get("r-1")
    assert record.state is ReservationState.SUBMITTING
    assert record.key_ref == key_reference(K3)
    snapshot = await store.usage()
    assert snapshot.active_reserved == 5.0
    assert snapshot.used == 0.0
    with pytest.raises(ValueError, match="预留 ID 已存在"):
        await store.reserve(
            5, kind="scrape", request_fingerprint=FP, reservation_id="r-1"
        )
    assert all(key not in str(calls) for key in (K1, K2, K3))


@pytest.mark.asyncio
async def test_first_before_send_failure_propagates_raw_without_network():
    boom = httpx.ConnectError("callback boom")

    async def on_send(ref, confirmed):
        raise boom

    transport, requests = make_transport([ok()])
    client = FirecrawlClient(KeyPool([K1]), transport=transport)
    with pytest.raises(httpx.ConnectError) as exc_info:
        await client.request("POST", "/scrape", before_send=on_send)
    assert exc_info.value is boom
    assert requests == []
    await client.aclose()


@pytest.mark.asyncio
async def test_second_before_send_failure_stops_rotation_without_third_key():
    boom = httpx.ConnectError("callback boom")
    calls: list[tuple[str | None, bool]] = []

    async def on_send(ref, confirmed):
        calls.append((ref, confirmed))
        if confirmed:
            raise boom

    transport, requests = make_transport([bad_key(), ok()])
    client = FirecrawlClient(KeyPool([K1, K2, K3]), transport=transport)
    with pytest.raises(httpx.ConnectError) as exc_info:
        await client.request("POST", "/scrape", before_send=on_send)
    assert exc_info.value is boom
    assert calls == [
        (key_reference(K1), False),
        (key_reference(K2), True),
    ]
    assert len(requests) == 1
    assert requests[0].headers["authorization"] == f"Bearer {K1}"
    await client.aclose()


@pytest.mark.asyncio
async def test_construction_failure_skips_callback_and_network():
    calls: list[tuple[str | None, bool]] = []

    async def on_send(ref, confirmed):
        calls.append((ref, confirmed))

    client, _, requests = make_client([K1], [ok()])
    with pytest.raises(TypeError):
        await client.request(
            "POST", "/scrape", json={"url": object()}, before_send=on_send
        )
    with pytest.raises(FirecrawlError) as exc_info:
        await client.request("POST", "/scrape", key_ref="abc123", before_send=on_send)
    assert exc_info.value.category is ErrorCategory.CONFIG
    assert calls == []
    assert requests == []
    await client.aclose()


@pytest.mark.asyncio
async def test_anonymous_before_send_receives_none_without_authorization():
    calls: list[tuple[str | None, bool]] = []

    async def on_send(ref, confirmed):
        calls.append((ref, confirmed))

    transport, requests = make_transport([ok()])
    client = FirecrawlClient(KeyPool([]), transport=transport)
    response = await client.request("POST", "/scrape", before_send=on_send)
    assert calls == [(None, False)]
    assert "authorization" not in requests[0].headers
    assert response.extensions[KEY_REF_EXTENSION] is None
    await client.aclose()


@pytest.mark.asyncio
async def test_pinned_before_send_called_once_with_false():
    calls: list[tuple[str | None, bool]] = []

    async def on_send(ref, confirmed):
        calls.append((ref, confirmed))

    client, _, requests = make_client([K1, K2], [bad_key()])
    with pytest.raises(FirecrawlError):
        await client.request(
            "POST", "/scrape", key_ref=key_reference(K2), before_send=on_send
        )
    assert calls == [(key_reference(K2), False)]
    assert len(requests) == 1
    await client.aclose()


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(500, json={"error": "boom"}),
        httpx.ReadTimeout("too slow"),
        httpx.ConnectError("refused"),
    ],
)
@pytest.mark.asyncio
async def test_single_attempt_failures_call_before_send_once(failure):
    calls: list[tuple[str | None, bool]] = []

    async def on_send(ref, confirmed):
        calls.append((ref, confirmed))

    client, _, requests = make_client([K1, K2], [failure])
    with pytest.raises(FirecrawlError):
        await client.request("POST", "/scrape", before_send=on_send)
    assert calls == [(key_reference(K1), False)]
    assert len(requests) == 1
    await client.aclose()
