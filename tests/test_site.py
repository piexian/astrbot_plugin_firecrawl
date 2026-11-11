"""firecrawl_api.site 参数校验、任务句柄与状态裁剪测试。"""

from __future__ import annotations

import copy

import pytest

from firecrawl_api import budget
from firecrawl_api.budget import estimate_site
from firecrawl_api.site import (
    build_batch_payload,
    build_crawl_payload,
    build_map_payload,
    build_task_request,
    estimate_site_credits,
    make_task_handle,
    normalize_site_args,
    parse_task_handle,
    trim_site_response,
)

BLOCKED = ("x.com", "twitter.com", "mobile.twitter.com", "linkedin.com")
KEY_REF = "a" * 64


def map_response(links):
    return {"success": True, "links": links}


def status_response(**overrides):
    body = {
        "success": True,
        "status": "scraping",
        "completed": 2,
        "total": 5,
        "creditsUsed": 3,
        "data": [
            {
                "markdown": "p" * 10,
                "html": "<p>" + "p" * 10 + "</p>",
                "rawBase64": "AAAA",
                "metadata": {
                    "sourceURL": "https://e.com/1",
                    "url": "https://e.com/final-1",
                    "title": "Page 1",
                    "statusCode": 200,
                },
            },
            {
                "markdown": "p" * 10,
                "html": "<p>" + "p" * 10 + "</p>",
                "rawBase64": "AAAA",
                "metadata": {
                    "sourceURL": "https://e.com/2",
                    "url": "https://e.com/final-2",
                    "title": "Page 2",
                    "statusCode": 200,
                },
            },
            {
                "markdown": "p" * 10,
                "html": "<p>" + "p" * 10 + "</p>",
                "rawBase64": "AAAA",
                "metadata": {
                    "sourceURL": "https://e.com/3",
                    "url": "https://e.com/final-3",
                    "title": "Page 3",
                    "statusCode": 200,
                },
            },
        ],
    }
    body.update(overrides)
    return body


def test_normalize_site_args_rejects_unknown_action():
    with pytest.raises(ValueError):
        normalize_site_args("extract")
    with pytest.raises(ValueError):
        normalize_site_args("map:1")


def test_build_map_payload_basic():
    payload = build_map_payload("https://example.com")
    assert payload["url"] == "https://example.com"
    assert payload["limit"] == 50
    assert "search" not in payload
    payload = build_map_payload(
        "https://example.com", search="docs", limit=10, site_max_pages=50
    )
    assert payload["search"] == "docs"
    assert payload["limit"] == 10


def test_build_map_payload_validates_limits_and_combinations():
    with pytest.raises(ValueError):
        build_map_payload("example.com")
    with pytest.raises(ValueError):
        build_map_payload("https://example.com", limit=0)
    with pytest.raises(ValueError):
        build_map_payload("https://example.com", limit=51, site_max_pages=50)
    with pytest.raises(ValueError):
        normalize_site_args(
            "map", url="https://example.com", include_paths=["/blog"], site_max_pages=50
        )
    with pytest.raises(ValueError):
        normalize_site_args(
            "map",
            url="https://example.com",
            exclude_paths=["/admin"],
            site_max_pages=50,
        )


def test_build_map_payload_rejects_blocked_root():
    with pytest.raises(ValueError):
        build_map_payload("https://www.x.com/", blocked_domains=BLOCKED)


def test_build_crawl_payload_explicit_limit_and_no_external_links():
    payload = build_crawl_payload(
        "https://example.com",
        limit=5,
        include_paths=["/blog*"],
        exclude_paths=["/admin*"],
        site_max_pages=50,
    )
    assert payload["url"] == "https://example.com"
    assert payload["limit"] == 5
    assert payload["allowExternalLinks"] is False
    assert payload["includePaths"] == ["/blog*"]
    assert payload["excludePaths"] == ["/admin*"]


def test_build_crawl_payload_requires_explicit_limit():
    with pytest.raises(ValueError):
        build_crawl_payload("https://example.com", limit=None, site_max_pages=50)
    with pytest.raises(ValueError):
        build_crawl_payload("https://example.com", limit=51, site_max_pages=50)
    with pytest.raises(ValueError):
        build_crawl_payload("https://example.com", limit=True, site_max_pages=50)
    with pytest.raises(ValueError):
        normalize_site_args(
            "crawl",
            url="https://example.com",
            limit=5,
            search="docs",
            site_max_pages=50,
        )
    with pytest.raises(ValueError):
        build_crawl_payload("https://x.com/a", limit=5, blocked_domains=BLOCKED)


def test_build_batch_payload_sends_urls_without_limit():
    urls = ["https://example.com/1", "https://example.com/2"]
    payload = build_batch_payload(urls, site_max_pages=50)
    assert payload["urls"] == urls
    assert "scrapeOptions" not in payload
    assert "limit" not in payload


def test_build_batch_payload_validates_urls_and_page_cap():
    with pytest.raises(ValueError):
        build_batch_payload([], site_max_pages=50)
    with pytest.raises(ValueError):
        build_batch_payload(["https://example.com/1", "notaurl"], site_max_pages=50)
    with pytest.raises(ValueError):
        build_batch_payload(
            ["https://e.com/" + str(i) for i in range(51)], site_max_pages=50
        )
    with pytest.raises(ValueError):
        build_batch_payload(
            ["https://e.com/1", "https://x.com/2"],
            site_max_pages=50,
            blocked_domains=BLOCKED,
        )
    with pytest.raises(ValueError):
        normalize_site_args(
            "batch", urls=["https://e.com/1"], limit=5, site_max_pages=50
        )


def test_build_task_request_validates_status_and_cancel():
    request = build_task_request("status", kind="crawl", job_id="job-1")
    assert request == {"action": "status", "kind": "crawl", "job_id": "job-1"}
    request = build_task_request("cancel", kind="batch", job_id="job-2")
    assert request == {"action": "cancel", "kind": "batch", "job_id": "job-2"}
    with pytest.raises(ValueError):
        build_task_request("status", kind="crawl", job_id=None)
    with pytest.raises(ValueError):
        build_task_request("status", kind="crawl", job_id="")
    with pytest.raises(ValueError):
        build_task_request("status", kind="map", job_id="job-1")
    with pytest.raises(ValueError):
        build_task_request("cancel", kind="scrape", job_id="job-1")
    with pytest.raises(ValueError):
        normalize_site_args("status", job_id="job-1")


def test_task_handle_round_trip():
    handle = make_task_handle("crawl", "job-123", KEY_REF)
    assert handle == f"crawl:job-123@{KEY_REF}"
    parsed = parse_task_handle(handle)
    assert parsed == {"kind": "crawl", "firecrawl_id": "job-123", "key_ref": KEY_REF}
    parsed = parse_task_handle(make_task_handle("batch", "b-1", "f" * 64))
    assert parsed["kind"] == "batch"
    assert parsed["key_ref"] == "f" * 64


def test_task_handle_make_rejects_bad_inputs():
    with pytest.raises(ValueError):
        make_task_handle("map", "job-1", KEY_REF)
    with pytest.raises(ValueError):
        make_task_handle("", "job-1", KEY_REF)
    with pytest.raises(ValueError):
        make_task_handle("crawl", "", KEY_REF)
    with pytest.raises(ValueError):
        make_task_handle("crawl", None, KEY_REF)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        make_task_handle("crawl", "job-1", "a" * 63)
    with pytest.raises(ValueError):
        make_task_handle("crawl", "job-1", "A" * 64)
    with pytest.raises(ValueError):
        make_task_handle("crawl", "job-1", "z" * 64)


def test_task_handle_parse_rejects_without_echoing_key_ref():
    bad_ref = "B" * 64
    with pytest.raises(ValueError) as exc_info:
        parse_task_handle(f"crawl:job-1@{bad_ref}")
    assert bad_ref not in str(exc_info.value)
    with pytest.raises(ValueError) as exc_info:
        parse_task_handle(f"crawl:job-1@{'a' * 20}")
    assert "a" * 20 not in str(exc_info.value)
    with pytest.raises(ValueError):
        parse_task_handle(f"map:job-1@{KEY_REF}")
    with pytest.raises(ValueError):
        parse_task_handle(f"scrape:job-1@{KEY_REF}")
    with pytest.raises(ValueError):
        parse_task_handle(f"crawl:@{KEY_REF}")
    with pytest.raises(ValueError):
        parse_task_handle(f"crawljob-1@{KEY_REF}")
    with pytest.raises(ValueError):
        parse_task_handle(f"crawl:job-1{KEY_REF}")
    with pytest.raises(ValueError):
        parse_task_handle("crawl:job-1@")
    with pytest.raises(ValueError):
        parse_task_handle("")


def test_task_id_charset_shared_across_entries():
    bad_ids = [
        "job 1",
        "job\t1",
        "job\n1",
        "job\r1",
        " job-1",
        "job-1 ",
        "job@1",
        "a/b",
        "a\\b",
        "id%20",
        "id?x=1",
        "id#frag",
        "id:x",
        "id+1",
        "任务-1",
    ]
    for bad in bad_ids:
        with pytest.raises(ValueError):
            make_task_handle("crawl", bad, KEY_REF)
        with pytest.raises(ValueError):
            parse_task_handle(f"crawl:{bad}@{KEY_REF}")
        with pytest.raises(ValueError) as exc_info:
            build_task_request("status", kind="crawl", job_id=bad)
        assert bad not in str(exc_info.value)
        with pytest.raises(ValueError):
            normalize_site_args("status", job_id=bad, kind="crawl")


def test_task_id_allows_uuid_and_ascii_jobs():
    valid_ids = ["job-123", "550e8400-e29b-41d4-a716-446655440000", "Ab-_9"]
    for valid in valid_ids:
        handle = make_task_handle("crawl", valid, KEY_REF)
        assert parse_task_handle(handle)["firecrawl_id"] == valid
        request = build_task_request("status", kind="crawl", job_id=valid)
        assert request["job_id"] == valid
        args = normalize_site_args("status", job_id=valid, kind="crawl")
        assert args["job_id"] == valid


def test_estimate_site_credits_reuses_budget_estimate_site():
    assert estimate_site_credits("map") == estimate_site("map")
    assert estimate_site_credits("crawl", 5) == estimate_site("crawl", 5)
    assert estimate_site_credits("batch", 3) == estimate_site("batch", 3)
    assert estimate_site_credits("map") == 1
    assert estimate_site_credits("crawl", 5) == 5 * 21
    with pytest.raises(ValueError):
        estimate_site_credits("crawl")
    with pytest.raises(ValueError):
        estimate_site_credits("status")
    with pytest.raises(ValueError):
        estimate_site_credits("cancel", 1)


def test_trim_site_response_map_links():
    trimmed = trim_site_response(
        "map",
        map_response(["https://e.com/1", "https://e.com/2"]),
        max_list_items=10,
        max_content_chars=100,
    )
    assert trimmed["links"] == ["https://e.com/1", "https://e.com/2"]
    assert "linksTruncated" not in trimmed
    capped = trim_site_response(
        "map",
        map_response([f"https://e.com/{i}" for i in range(5)]),
        max_list_items=2,
        max_content_chars=100,
    )
    assert capped["links"] == ["https://e.com/0", "https://e.com/1"]
    assert capped["linksTruncated"] is True
    with pytest.raises(ValueError):
        trim_site_response(
            "map",
            {"success": True, "links": "nope"},
            max_list_items=2,
            max_content_chars=100,
        )


def test_trim_site_response_map_v2_link_objects():
    links = [
        {"url": f"https://e.com/{i}", "title": f"T{i}", "description": f"D{i}"}
        for i in range(4)
    ]
    trimmed = trim_site_response(
        "map", map_response(links), max_list_items=2, max_content_chars=100
    )
    assert trimmed["links"] == links[:2]
    assert trimmed["linksTruncated"] is True
    uncapped = trim_site_response(
        "map", map_response(links[:2]), max_list_items=2, max_content_chars=100
    )
    assert uncapped["links"] == links[:2]
    assert "linksTruncated" not in uncapped


def test_trim_site_response_map_invalid_links_are_not_echoed():
    marker = "LEAK-MAP-SECRET-MARKER"
    with pytest.raises(ValueError) as caught:
        trim_site_response(
            "map",
            {"success": True, "links": {"private": marker}},
            max_list_items=2,
            max_content_chars=100,
        )
    assert marker not in str(caught.value)


def test_trim_site_response_status_keeps_progress_and_caps_pages():
    trimmed = trim_site_response(
        "status", status_response(), max_list_items=10, max_content_chars=100
    )
    assert trimmed["status"] == "scraping"
    assert trimmed["completed"] == 2
    assert trimmed["total"] == 5
    assert trimmed["creditsUsed"] == 3
    assert len(trimmed["pages"]) == 3
    assert trimmed["pages"][0] == {
        "url": "https://e.com/final-1",
        "title": "Page 1",
        "content": "p" * 10,
    }
    assert "pagesTruncated" not in trimmed
    capped = trim_site_response(
        "status", status_response(), max_list_items=2, max_content_chars=100
    )
    assert len(capped["pages"]) == 2
    assert capped["pagesTruncated"] is True


def test_trim_site_response_status_pagination_hint():
    response = status_response(next="https://api.firecrawl.dev/v2/crawl/j?next=1")
    trimmed = trim_site_response(
        "status", response, max_list_items=10, max_content_chars=100
    )
    assert trimmed["next"] == "https://api.firecrawl.dev/v2/crawl/j?next=1"
    assert trimmed["hasNext"] is True
    done = trim_site_response(
        "status", status_response(), max_list_items=10, max_content_chars=100
    )
    assert "hasNext" not in done


def test_trim_site_response_status_shape_guard():
    with pytest.raises(ValueError):
        trim_site_response(
            "status",
            status_response(data="nope"),
            max_list_items=2,
            max_content_chars=100,
        )
    empty = trim_site_response(
        "status", status_response(data=None), max_list_items=2, max_content_chars=100
    )
    assert empty["pages"] == []


def test_trim_site_response_status_projects_fixed_fields_only():
    page = {
        "markdown": "body",
        "html": "<p>body</p>",
        "rawHtml": "<p>body</p>",
        "rawBase64": "AAAA",
        "links": ["https://e.com/x"],
        "metadata": {
            "url": "https://e.com/final",
            "sourceURL": "https://e.com/src",
            "title": "T",
            "statusCode": 200,
            "language": "en",
        },
    }
    trimmed = trim_site_response(
        "status", status_response(data=[page]), max_list_items=10, max_content_chars=100
    )
    assert trimmed["pages"] == [
        {"url": "https://e.com/final", "title": "T", "content": "body"}
    ]


def test_trim_site_response_status_falls_back_to_source_url():
    page = {"markdown": "m", "metadata": {"sourceURL": "https://e.com/src"}}
    trimmed = trim_site_response(
        "status", status_response(data=[page]), max_list_items=10, max_content_chars=100
    )
    assert trimmed["pages"] == [{"url": "https://e.com/src", "content": "m"}]


def test_trim_site_response_status_title_shapes():
    listed = {"markdown": "m", "metadata": {"title": ["A", "B"]}}
    trimmed = trim_site_response(
        "status",
        status_response(data=[listed]),
        max_list_items=10,
        max_content_chars=100,
    )
    assert trimmed["pages"][0]["title"] == ["A", "B"]
    bad = {"markdown": "m", "metadata": {"title": [1, 2]}}
    with pytest.raises(ValueError):
        trim_site_response(
            "status",
            status_response(data=[bad]),
            max_list_items=10,
            max_content_chars=100,
        )


def test_trim_site_response_status_caps_each_page_body():
    long_page = {"markdown": "x" * 1000000, "metadata": {"url": "https://e.com/1"}}
    short_page = {"markdown": "ok", "metadata": {"url": "https://e.com/2"}}
    trimmed = trim_site_response(
        "status",
        status_response(data=[long_page, short_page]),
        max_list_items=10,
        max_content_chars=200,
    )
    first, second = trimmed["pages"]
    assert len(first["content"]) == 200
    assert first["truncated"] is True
    assert second == {"url": "https://e.com/2", "content": "ok"}
    assert "truncated" not in second


def test_trim_site_response_status_keeps_page_error_message():
    page = {"error": "Page failed to scrape"}
    trimmed = trim_site_response(
        "status", status_response(data=[page]), max_list_items=10, max_content_chars=100
    )
    assert trimmed["pages"] == [{"error": "Page failed to scrape"}]


def test_trim_site_response_status_rejects_bad_page_shapes():
    with pytest.raises(ValueError):
        trim_site_response(
            "status",
            status_response(data=["nope"]),
            max_list_items=10,
            max_content_chars=100,
        )
    with pytest.raises(ValueError):
        trim_site_response(
            "status",
            status_response(data=[{"markdown": 1}]),
            max_list_items=10,
            max_content_chars=100,
        )
    with pytest.raises(ValueError):
        trim_site_response(
            "status",
            status_response(data=[{"markdown": "m", "metadata": "bad"}]),
            max_list_items=10,
            max_content_chars=100,
        )


def test_trim_site_response_status_keeps_input_unchanged():
    page = {
        "markdown": "m" * 500,
        "html": "<p>x</p>",
        "metadata": {"url": "https://e.com/1", "title": "T"},
    }
    response = status_response(data=[page])
    snapshot = copy.deepcopy(response)
    trim_site_response("status", response, max_list_items=10, max_content_chars=100)
    assert response == snapshot


def test_trim_site_response_status_projects_metadata_error():
    page = {
        "markdown": "m",
        "metadata": {"url": "https://e.com/1", "error": "rate limited"},
    }
    trimmed = trim_site_response(
        "status", status_response(data=[page]), max_list_items=10, max_content_chars=100
    )
    assert trimmed["pages"] == [
        {"url": "https://e.com/1", "content": "m", "error": "rate limited"}
    ]
    top = {"error": "top wins", "metadata": {"error": "from metadata"}}
    trimmed = trim_site_response(
        "status", status_response(data=[top]), max_list_items=10, max_content_chars=100
    )
    assert trimmed["pages"] == [{"error": "top wins"}]


def test_trim_site_response_status_truncates_long_error():
    page = {"error": "e" * 300}
    trimmed = trim_site_response(
        "status", status_response(data=[page]), max_list_items=10, max_content_chars=100
    )
    assert trimmed["pages"] == [{"error": "e" * 100, "truncated": True}]


def test_trim_site_response_status_rejects_non_string_next():
    with pytest.raises(ValueError):
        trim_site_response(
            "status",
            status_response(next=123),
            max_list_items=10,
            max_content_chars=100,
        )
    empty = trim_site_response(
        "status", status_response(next=""), max_list_items=10, max_content_chars=100
    )
    assert "next" not in empty
    assert "hasNext" not in empty


def test_trim_site_response_status_applies_both_truncation_levels():
    pages = [
        {"markdown": "x" * 300, "metadata": {"url": f"https://e.com/{i}"}}
        for i in range(3)
    ]
    trimmed = trim_site_response(
        "status", status_response(data=pages), max_list_items=2, max_content_chars=100
    )
    assert trimmed["pagesTruncated"] is True
    assert len(trimmed["pages"]) == 2
    for page in trimmed["pages"]:
        assert page["truncated"] is True
        assert len(page["content"]) == 100


def test_trim_site_response_cancel_and_warning():
    trimmed = trim_site_response(
        "cancel",
        {"success": True, "status": "cancelled"},
        max_list_items=5,
        max_content_chars=100,
    )
    assert trimmed["status"] == "cancelled"
    assert trimmed["success"] is True
    with pytest.raises(ValueError):
        trim_site_response(
            "scrape", {"success": True}, max_list_items=5, max_content_chars=100
        )


def submit_response(**overrides):
    body = {"success": True, "id": "job-123", "url": "https://e.com/crawl/1"}
    body.update(overrides)
    return body


def test_trim_site_response_submit_keeps_official_fields():
    trimmed = trim_site_response(
        "crawl",
        submit_response(jobId="extra"),
        max_list_items=10,
        max_content_chars=100,
    )
    assert trimmed == {
        "success": True,
        "id": "job-123",
        "url": "https://e.com/crawl/1",
    }
    batched = trim_site_response(
        "batch", submit_response(), max_list_items=10, max_content_chars=100
    )
    assert batched["id"] == "job-123"
    assert "invalidURLs" not in batched


def test_trim_site_response_submit_invalid_urls_shape():
    null_urls = trim_site_response(
        "batch",
        submit_response(invalidURLs=None),
        max_list_items=10,
        max_content_chars=100,
    )
    assert null_urls["invalidURLs"] is None
    empty_urls = trim_site_response(
        "batch",
        submit_response(invalidURLs=[]),
        max_list_items=10,
        max_content_chars=100,
    )
    assert empty_urls["invalidURLs"] == []
    partial = submit_response(invalidURLs=["https://e.com/bad", "https://x.com/worse"])
    kept = trim_site_response(
        "batch", partial, max_list_items=10, max_content_chars=100
    )
    assert kept["invalidURLs"] == ["https://e.com/bad", "https://x.com/worse"]
    assert "invalidURLsTruncated" not in kept
    capped = trim_site_response(
        "batch",
        submit_response(invalidURLs=["u1", "u2", "u3"]),
        max_list_items=2,
        max_content_chars=100,
    )
    assert capped["invalidURLs"] == ["u1", "u2"]
    assert capped["invalidURLsTruncated"] is True
    with pytest.raises(ValueError):
        trim_site_response(
            "batch",
            submit_response(invalidURLs=[1]),
            max_list_items=10,
            max_content_chars=100,
        )


def test_trim_site_response_submit_rejects_missing_or_bad_id():
    with pytest.raises(ValueError):
        trim_site_response(
            "crawl", {"success": True}, max_list_items=10, max_content_chars=100
        )
    with pytest.raises(ValueError):
        trim_site_response(
            "batch",
            {"success": True, "jobId": "job-123"},
            max_list_items=10,
            max_content_chars=100,
        )
    with pytest.raises(ValueError):
        trim_site_response(
            "crawl", submit_response(id=123), max_list_items=10, max_content_chars=100
        )
    with pytest.raises(ValueError) as exc_info:
        trim_site_response(
            "crawl",
            submit_response(id="job 1"),
            max_list_items=10,
            max_content_chars=100,
        )
    assert "job 1" not in str(exc_info.value)
    with pytest.raises(ValueError):
        trim_site_response(
            "crawl", submit_response(id="a/b"), max_list_items=10, max_content_chars=100
        )
    with pytest.raises(ValueError):
        trim_site_response(
            "crawl", submit_response(url=123), max_list_items=10, max_content_chars=100
        )


def test_trim_site_response_keeps_common_fields():
    common = {"warning": "w", "warnings": ["a"], "creditsUsed": 1}
    trimmed = trim_site_response(
        "crawl", submit_response(**common), max_list_items=10, max_content_chars=100
    )
    assert trimmed["warning"] == "w"
    assert trimmed["warnings"] == ["a"]
    assert trimmed["creditsUsed"] == 1
    cancelled = trim_site_response(
        "cancel",
        {"success": True, "status": "cancelled", **common},
        max_list_items=5,
        max_content_chars=100,
    )
    assert cancelled["warning"] == "w"
    assert cancelled["warnings"] == ["a"]
    assert cancelled["creditsUsed"] == 1


def test_trim_site_response_rejects_bad_max_content_chars():
    for bad in (0, -1, True, "100", None):
        with pytest.raises(ValueError):
            trim_site_response(
                "map", map_response([]), max_list_items=10, max_content_chars=bad
            )


def test_build_map_payload_always_sends_explicit_limit():
    payload = build_map_payload("https://example.com", site_max_pages=30)
    assert payload["limit"] == 30
    with pytest.raises(ValueError):
        build_map_payload("https://example.com", limit=31, site_max_pages=30)


def test_build_crawl_payload_carries_scrape_options_with_real_pdf_cap():
    payload = build_crawl_payload(
        "https://example.com",
        limit=5,
        site_max_pages=50,
        pdf_max_pages=1,
        check_prompt_injection=True,
    )
    options = payload["scrapeOptions"]
    assert options["formats"] == ["markdown"]
    assert options["parsers"] == [{"type": "pdf", "maxPages": 1}]
    assert options["checkPromptInjection"] is True
    assert options["onlyMainContent"] is True
    assert options["proxy"] == "auto"
    assert payload["allowExternalLinks"] is False
    default = build_crawl_payload("https://example.com", limit=5, site_max_pages=50)
    assert default["scrapeOptions"]["parsers"] == [{"type": "pdf", "maxPages": 20}]
    assert default["scrapeOptions"]["checkPromptInjection"] is False
    large = build_crawl_payload(
        "https://example.com", limit=5, site_max_pages=50, pdf_max_pages=10000
    )
    assert large["scrapeOptions"]["parsers"] == [{"type": "pdf", "maxPages": 10000}]


def test_build_crawl_payload_rejects_invalid_option_values():
    with pytest.raises(ValueError):
        build_crawl_payload(
            "https://example.com", limit=5, site_max_pages=50, pdf_max_pages=0
        )
    with pytest.raises(ValueError):
        build_crawl_payload(
            "https://example.com", limit=5, site_max_pages=50, pdf_max_pages=10001
        )
    with pytest.raises(ValueError):
        build_crawl_payload(
            "https://example.com", limit=5, site_max_pages=50, pdf_max_pages=True
        )
    with pytest.raises(ValueError):
        build_crawl_payload(
            "https://example.com",
            limit=5,
            site_max_pages=50,
            check_prompt_injection="yes",
        )


def test_build_batch_payload_options_at_top_level_without_limit():
    payload = build_batch_payload(
        ["https://example.com/1", "https://example.com/2"],
        site_max_pages=50,
        pdf_max_pages=3,
        check_prompt_injection=True,
    )
    assert payload["urls"] == ["https://example.com/1", "https://example.com/2"]
    assert payload["formats"] == ["markdown"]
    assert payload["parsers"] == [{"type": "pdf", "maxPages": 3}]
    assert payload["checkPromptInjection"] is True
    assert "limit" not in payload
    assert "scrapeOptions" not in payload
    default = build_batch_payload(["https://example.com/1"], site_max_pages=50)
    assert default["parsers"] == [{"type": "pdf", "maxPages": 20}]
    large = build_batch_payload(
        ["https://example.com/1"], site_max_pages=50, pdf_max_pages=10000
    )
    assert large["parsers"] == [{"type": "pdf", "maxPages": 10000}]


def test_build_batch_payload_rejects_invalid_option_values():
    with pytest.raises(ValueError):
        build_batch_payload(
            ["https://example.com/1"], site_max_pages=50, pdf_max_pages=0
        )
    with pytest.raises(ValueError):
        build_batch_payload(
            ["https://example.com/1"], site_max_pages=50, check_prompt_injection=1
        )


def test_estimate_matches_sent_options_for_crawl_and_batch():
    assert estimate_site_credits(
        "crawl", 5, pdf_max_pages=3, check_prompt_injection=True
    ) == budget.estimate_site("crawl", 5, pdf_max_pages=3, check_prompt_injection=True)
    payload = build_crawl_payload(
        "https://example.com",
        limit=5,
        site_max_pages=50,
        pdf_max_pages=3,
        check_prompt_injection=True,
    )
    assert payload["scrapeOptions"]["parsers"] == [{"type": "pdf", "maxPages": 3}]
    assert payload["scrapeOptions"]["checkPromptInjection"] is True
    assert estimate_site_credits(
        "batch", 2, pdf_max_pages=7, check_prompt_injection=False
    ) == budget.estimate_site("batch", 2, pdf_max_pages=7, check_prompt_injection=False)
