"""firecrawl_api.domains 共享域名匹配测试。"""

from __future__ import annotations

import pytest

from firecrawl_api.domains import (
    assert_url_allowed,
    host_matches_domain,
    is_blocked_host,
    merge_blocked_into_excludes,
    normalize_blocked_domains,
    normalize_host,
    normalize_url,
)
from firecrawl_api.scrape import build_scrape_payload
from firecrawl_api.search import build_search_payload
from firecrawl_api.site import (
    build_batch_payload,
    build_crawl_payload,
    build_map_payload,
)


def test_normalize_host_from_full_url():
    assert normalize_host("https://Example.COM/path?q=1") == "example.com"
    assert normalize_host("http://sub.x.com:8443/page") == "sub.x.com"
    assert normalize_host("https://www.linkedin.com.") == "www.linkedin.com"


def test_normalize_host_from_bare_host():
    assert normalize_host("  X.com  ") == "x.com"
    assert normalize_host("blog.example.org") == "blog.example.org"


def test_normalize_host_rejects_invalid_input():
    with pytest.raises(ValueError):
        normalize_host("")
    with pytest.raises(ValueError):
        normalize_host("https://")
    with pytest.raises(ValueError):
        normalize_host(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        normalize_host(123)  # type: ignore[arg-type]


def test_normalize_url_accepts_full_http_https():
    assert (
        normalize_url("https://example.com/a?b=1#frag")
        == "https://example.com/a?b=1#frag"
    )
    assert normalize_url("  http://example.com  ") == "http://example.com"


def test_normalize_url_rejects_partial_or_wrong_scheme():
    with pytest.raises(ValueError):
        normalize_url("example.com/page")
    with pytest.raises(ValueError):
        normalize_url("ftp://example.com/file")
    with pytest.raises(ValueError):
        normalize_url("https:///no-host")
    with pytest.raises(ValueError):
        normalize_url("https://exa mple.com")
    with pytest.raises(ValueError):
        normalize_url("")


def test_host_matches_domain_exact_and_subdomain():
    assert host_matches_domain("x.com", "x.com")
    assert host_matches_domain("www.x.com", "x.com")
    assert host_matches_domain("a.b.x.com", "x.com")
    assert not host_matches_domain("notx.com", "x.com")
    assert not host_matches_domain("x.com.evil.io", "x.com")


def test_is_blocked_host_covers_subdomains_case_insensitive():
    blocked = ("X.com", "linkedin.com")
    assert is_blocked_host("x.com", blocked)
    assert is_blocked_host("mobile.twitter.com", ("twitter.com",))
    assert is_blocked_host("www.mobile.twitter.com", ("twitter.com",))
    assert not is_blocked_host("twitterx.com", blocked)
    assert not is_blocked_host("example.com", blocked)


def test_normalize_blocked_domains_normalizes_and_dedups():
    assert normalize_blocked_domains(["X.com", "https://www.X.com/", "x.com"]) == [
        "x.com",
        "www.x.com",
    ]


def test_assert_url_allowed_rejects_blocked_and_subdomain():
    blocked = ("x.com", "linkedin.com")
    assert_url_allowed("https://example.com/page", blocked)
    with pytest.raises(ValueError):
        assert_url_allowed("https://x.com/post", blocked)
    with pytest.raises(ValueError):
        assert_url_allowed("https://www.linkedin.com/in/someone", blocked)


def test_merge_blocked_into_excludes_normalizes_and_dedups():
    merged = merge_blocked_into_excludes(
        ["blog.example.com", "X.com"], ["x.com", "linkedin.com"]
    )
    assert merged == ["blog.example.com", "x.com", "linkedin.com"]


def test_normalize_url_rejects_whatwg_ambiguous_urls():
    with pytest.raises(ValueError):
        normalize_url("https://x.com\\@example.com/a")
    with pytest.raises(ValueError):
        normalize_url("https://%78.com/a")
    with pytest.raises(ValueError):
        normalize_url("https://exa\nmple.com/a")
    with pytest.raises(ValueError):
        normalize_url("https://example.com:99999/a")
    with pytest.raises(ValueError):
        normalize_url("https://example.com:abc/a")
    with pytest.raises(ValueError):
        normalize_url("https://user:pass@example.com/a")


def test_reject_messages_do_not_echo_credentials():
    with pytest.raises(ValueError) as exc_info:
        assert_url_allowed("https://user:pass@x.com/a", ("x.com",))
    assert "user:pass" not in str(exc_info.value)


def test_normalize_host_idna_canonicalization():
    assert normalize_host("ｘ.com") == "x.com"
    assert normalize_host("bücher.de") == "xn--bcher-kva.de"
    assert normalize_host("XN--BCHER-KVA.DE") == "xn--bcher-kva.de"
    assert normalize_host("example.com.") == "example.com"


def test_blocked_guard_catches_encoded_and_idna_bypasses():
    blocked = ("x.com",)
    with pytest.raises(ValueError):
        assert_url_allowed("https://ｘ.com/a", blocked)
    with pytest.raises(ValueError):
        assert_url_allowed("https://%78.com/a", blocked)
    with pytest.raises(ValueError):
        assert_url_allowed("https://x.com\\@example.com/a", blocked)
    assert is_blocked_host("www.ｘ.com", blocked)


def test_normalize_url_preserves_legal_url_parts():
    url = "https://example.com:8443/a%20b?q=%E4%BD%A0#frag"
    assert normalize_url(url) == url
    assert normalize_url("https://xn--bcher-kva.de/x") == "https://xn--bcher-kva.de/x"


def test_normalize_host_uses_idna2008_uts46():
    assert normalize_host("https://faß.de/a") == "xn--fa-hia.de"
    with pytest.raises(ValueError):
        assert_url_allowed("https://faß.de/a", ("xn--fa-hia.de",))
    assert normalize_host("ｘ.com") == "x.com"
    assert normalize_host("bücher.de") == "xn--bcher-kva.de"


def test_ip_literal_and_bare_host_port():
    assert normalize_host("https://[::1]:8080/a") == "::1"
    assert normalize_url("https://[::1]:8080/a") == "https://[::1]:8080/a"
    assert normalize_host("example.com:8443") == "example.com"
    with pytest.raises(ValueError):
        normalize_host("example.com:99999")
    with pytest.raises(ValueError):
        normalize_host("user@example.com")


def test_error_does_not_echo_netloc_credentials():
    with pytest.raises(ValueError) as exc_info:
        normalize_url("https://private-user：dummy-secret@example.com/a")
    message = str(exc_info.value)
    assert "private-user" not in message
    assert "dummy-secret" not in message


@pytest.mark.parametrize(
    "raw",
    [
        "https://[2001:db8::1]:8443/a",
        "2001:db8::1",
        "::1",
        "https://example.com:8443/p",
        "example.com",
        "https://faß.de/a",
        "https://ｘ.com/a",
        "https://x.com。/p",
        "https://x.com．/p",
        "https://x.com｡/p",
        "https://x.com./p",
    ],
)
def test_normalize_host_idempotent(raw):
    once = normalize_host(raw)
    assert normalize_host(once) == once


def test_public_entries_accept_ipv6_targets():
    blocked = ("x.com",)
    build_scrape_payload("https://[::1]:8080/a", blocked_domains=blocked)
    build_map_payload("https://[2001:db8::1]/", blocked_domains=blocked)
    build_crawl_payload(
        "https://[::1]/", limit=3, site_max_pages=50, blocked_domains=blocked
    )
    build_batch_payload(
        ["https://[::1]/a", "https://[2001:db8::1]/b"],
        site_max_pages=50,
        blocked_domains=blocked,
    )
    payload = build_search_payload(
        "q",
        include_domains=["2001:db8::1"],
        blocked_domains=blocked,
        search_max_results=10,
    )
    assert payload["includeDomains"] == ["2001:db8::1"]


def test_public_entries_reject_blocked_ipv6_via_canonical_chain():
    with pytest.raises(ValueError):
        build_scrape_payload(
            "https://[2001:db8::1]:8443/a", blocked_domains=("2001:db8::1",)
        )


@pytest.mark.parametrize(
    "host_form",
    ["x.com。", "x.com．", "x.com｡", "ｘ.com"],
)
def test_public_entries_reject_unicode_dot_and_fullwidth_hosts(host_form):
    blocked = ("x.com",)
    with pytest.raises(ValueError):
        build_scrape_payload(f"https://{host_form}/a", blocked_domains=blocked)
    with pytest.raises(ValueError):
        build_map_payload(f"https://{host_form}/", blocked_domains=blocked)
    with pytest.raises(ValueError):
        build_crawl_payload(
            f"https://{host_form}/",
            limit=1,
            site_max_pages=50,
            blocked_domains=blocked,
        )
    with pytest.raises(ValueError):
        build_batch_payload(
            [f"https://{host_form}/a"], site_max_pages=50, blocked_domains=blocked
        )
    with pytest.raises(ValueError):
        build_search_payload(
            "q",
            include_domains=[host_form],
            blocked_domains=blocked,
            with_content=True,
            search_max_results=10,
        )
