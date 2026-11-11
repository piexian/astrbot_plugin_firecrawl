"""firecrawl_api.search 参数校验、payload 组装与结果裁剪测试。"""

from __future__ import annotations

import pytest

from firecrawl_api.budget import estimate_search
from firecrawl_api.search import (
    build_search_payload,
    estimate_search_credits,
    normalize_search_args,
    trim_search_response,
)

BLOCKED = ("x.com", "twitter.com", "mobile.twitter.com", "linkedin.com")


def web_page(**overrides):
    page = {
        "title": "Example",
        "url": "https://example.com/a",
        "description": "desc",
    }
    page.update(overrides)
    return page


def search_response(**data_overrides):
    data = {
        "web": [web_page(markdown="m" * 20)],
        "news": [
            {
                "title": "News",
                "url": "https://example.com/n",
                "snippet": "flash",
                "date": "2026-01-01",
                "imageUrl": "https://cdn.example.com/n.jpg",
                "markdown": "n" * 20,
            }
        ],
        "images": [
            {
                "title": "img",
                "url": "https://example.com/page",
                "imageUrl": "https://cdn.example.com/img.jpg",
                "imageWidth": 640,
                "imageHeight": 480,
            }
        ],
    }
    data.update(data_overrides)
    return {
        "success": True,
        "data": data,
        "warning": "partial results",
        "creditsUsed": 4,
    }


def test_build_search_payload_minimal_defaults():
    payload = build_search_payload("firecrawl api", search_max_results=10)
    assert payload["query"] == "firecrawl api"
    assert payload["limit"] == 10
    assert payload["sources"] == ["web"]
    assert payload["highlights"] is False
    assert "categories" not in payload
    assert "scrapeOptions" not in payload


def test_build_search_payload_rejects_empty_query():
    with pytest.raises(ValueError):
        build_search_payload("", search_max_results=10)
    with pytest.raises(ValueError):
        build_search_payload("   ", search_max_results=10)
    with pytest.raises(ValueError):
        build_search_payload(None, search_max_results=10)  # type: ignore[arg-type]


def test_build_search_payload_limit_bounds():
    with pytest.raises(ValueError):
        build_search_payload("q", limit=0, search_max_results=10)
    with pytest.raises(ValueError):
        build_search_payload("q", limit=101, search_max_results=100)
    with pytest.raises(ValueError):
        build_search_payload("q", limit=11, search_max_results=10)
    with pytest.raises(ValueError):
        build_search_payload("q", limit=True, search_max_results=10)
    with pytest.raises(ValueError):
        build_search_payload("q", search_max_results=0)
    with pytest.raises(ValueError):
        build_search_payload("q", search_max_results=101)


def test_build_search_payload_sources_whitelist():
    payload = build_search_payload(
        "q", sources=["news", "web", "images", "news"], search_max_results=10
    )
    assert payload["sources"] == ["news", "web", "images"]
    with pytest.raises(ValueError):
        build_search_payload("q", sources=["alexandria"], search_max_results=10)
    with pytest.raises(ValueError):
        build_search_payload("q", sources=["video"], search_max_results=10)
    with pytest.raises(ValueError):
        build_search_payload("q", sources="web", search_max_results=10)
    with pytest.raises(ValueError):
        build_search_payload("q", sources="alexandria", search_max_results=10)


def test_build_search_payload_categories_whitelist():
    payload = build_search_payload(
        "q", categories=["research", "pdf", "research"], search_max_results=10
    )
    assert payload["categories"] == ["research", "pdf"]
    with pytest.raises(ValueError):
        build_search_payload("q", categories=["news"], search_max_results=10)


def test_build_search_payload_geo_and_time_passthrough():
    payload = build_search_payload(
        "q",
        tbs="qdr:w",
        location="US",
        country="us",
        search_max_results=10,
    )
    assert payload["tbs"] == "qdr:w"
    assert payload["location"] == "US"
    assert payload["country"] == "us"
    with pytest.raises(ValueError):
        build_search_payload("q", tbs=7, search_max_results=10)


def test_build_search_payload_include_exclude_mutually_exclusive():
    with pytest.raises(ValueError):
        build_search_payload(
            "q",
            include_domains=["a.com"],
            exclude_domains=["b.com"],
            search_max_results=10,
        )


def test_build_search_payload_with_content_merges_blocked_into_excludes():
    payload = build_search_payload(
        "q",
        exclude_domains=["blog.example.com"],
        blocked_domains=BLOCKED,
        with_content=True,
        search_max_results=10,
    )
    assert payload["excludeDomains"] == [
        "blog.example.com",
        "x.com",
        "twitter.com",
        "mobile.twitter.com",
        "linkedin.com",
    ]
    assert "includeDomains" not in payload
    assert payload["scrapeOptions"]["formats"] == ["markdown"]
    assert payload["scrapeOptions"]["onlyMainContent"] is True
    assert payload["scrapeOptions"]["parsers"] == [{"type": "pdf", "maxPages": 20}]
    assert payload["scrapeOptions"]["checkPromptInjection"] is False


def test_build_search_payload_without_content_keeps_caller_domains():
    payload = build_search_payload(
        "q",
        exclude_domains=["X.com"],
        blocked_domains=BLOCKED,
        search_max_results=10,
    )
    assert payload["excludeDomains"] == ["x.com"]


def test_build_search_payload_include_domains_rejected_before_request():
    with pytest.raises(ValueError):
        build_search_payload(
            "q",
            include_domains=["www.x.com"],
            blocked_domains=BLOCKED,
            with_content=True,
            search_max_results=10,
        )
    payload = build_search_payload(
        "q",
        include_domains=["docs.example.com"],
        blocked_domains=BLOCKED,
        with_content=True,
        search_max_results=10,
    )
    assert payload["includeDomains"] == ["docs.example.com"]
    assert "excludeDomains" not in payload


def test_normalize_search_args_rejects_bad_with_content_type():
    with pytest.raises(ValueError):
        normalize_search_args("q", with_content="yes", search_max_results=10)


def test_estimate_search_credits_uses_pre_trim_server_volume():
    args = normalize_search_args(
        "q",
        limit=10,
        sources=["web", "news", "images"],
        with_content=True,
        search_max_results=10,
        pdf_max_pages=20,
    )
    assert estimate_search_credits(args) == estimate_search(
        10, source_count=3, with_content=True, pdf_max_pages=20
    )
    assert estimate_search_credits(args) > 0


def test_trim_search_response_keeps_top_level_warning_and_credits():
    trimmed = trim_search_response(search_response(), max_content_chars=10)
    assert trimmed["warning"] == "partial results"
    assert trimmed["creditsUsed"] == 4
    assert trimmed["success"] is True


def test_trim_search_response_web_keeps_contract_fields():
    trimmed = trim_search_response(search_response(), max_content_chars=1000)
    web = trimmed["data"]["web"][0]
    assert web == {
        "title": "Example",
        "url": "https://example.com/a",
        "description": "desc",
        "markdown": "m" * 20,
    }


def test_trim_search_response_truncates_markdown_only():
    trimmed = trim_search_response(search_response(), max_content_chars=10)
    web = trimmed["data"]["web"][0]
    assert web["markdown"] == "m" * 10
    assert web["truncated"] is True
    assert web["description"] == "desc"
    assert trimmed["data"]["images"][0]["imageUrl"] == "https://cdn.example.com/img.jpg"


def test_trim_search_response_news_keeps_contract_fields():
    trimmed = trim_search_response(search_response(), max_content_chars=1000)
    news = trimmed["data"]["news"][0]
    assert news == {
        "title": "News",
        "url": "https://example.com/n",
        "snippet": "flash",
        "date": "2026-01-01",
        "imageUrl": "https://cdn.example.com/n.jpg",
        "markdown": "n" * 20,
    }


def test_trim_search_response_images_keeps_url_and_image_url_separate():
    trimmed = trim_search_response(search_response(), max_content_chars=1000)
    images = trimmed["data"]["images"][0]
    assert images["url"] == "https://example.com/page"
    assert images["imageUrl"] == "https://cdn.example.com/img.jpg"
    assert images["imageWidth"] == 640
    assert images["imageHeight"] == 480


def test_trim_search_response_research_reads_data_web():
    response = search_response(web=[web_page(markdown="r" * 30)], news=[], images=[])
    trimmed = trim_search_response(response, max_content_chars=10)
    research = trimmed["data"]["web"][0]
    assert research["markdown"] == "r" * 10
    assert research["truncated"] is True
    assert trimmed["creditsUsed"] == 4


def test_trim_search_response_rejects_data_research_only_shape():
    response = search_response(
        web=None, news=None, images=None, research=[{"url": "u"}]
    )
    with pytest.raises(ValueError):
        trim_search_response(response, max_content_chars=100)


def test_trim_search_response_rejects_contract_mismatch():
    with pytest.raises(ValueError):
        trim_search_response(search_response(web="not-a-list"), max_content_chars=100)
    with pytest.raises(ValueError):
        trim_search_response(
            search_response(videos=[{"url": "u"}]), max_content_chars=100
        )
    with pytest.raises(ValueError):
        trim_search_response(search_response(web=[{"title": 1}]), max_content_chars=100)


def test_trim_search_response_total_cap_does_not_touch_credits():
    response = search_response(
        web=[
            web_page(url="https://example.com/1"),
            web_page(url="https://example.com/2"),
        ],
    )
    response["creditsUsed"] = 30
    trimmed = trim_search_response(response, max_content_chars=100, max_total_results=2)
    total = sum(len(items) for items in trimmed["data"].values())
    assert total == 2
    assert trimmed["creditsUsed"] == 30


def test_trim_search_response_rejects_bad_limits():
    with pytest.raises(ValueError):
        trim_search_response(search_response(), max_content_chars=0)
    with pytest.raises(ValueError):
        trim_search_response(search_response(), max_content_chars=True)
    with pytest.raises(ValueError):
        trim_search_response(
            search_response(), max_content_chars=100, max_total_results=0
        )


def test_build_search_payload_rejects_overlong_query():
    with pytest.raises(ValueError):
        build_search_payload("x" * 501, search_max_results=10)
    build_search_payload("x" * 500, search_max_results=10)


def test_build_search_payload_standalone_categories():
    with pytest.raises(ValueError):
        build_search_payload(
            "q", categories=["developer", "research"], search_max_results=10
        )
    with pytest.raises(ValueError):
        build_search_payload("q", categories=["gov", "pdf"], search_max_results=10)
    with pytest.raises(ValueError):
        build_search_payload(
            "q", categories=["developer", "gov"], search_max_results=10
        )
    payload = build_search_payload("q", categories=["developer"], search_max_results=10)
    assert payload["categories"] == ["developer"]


def test_build_search_payload_include_domains_idna_blocked():
    with pytest.raises(ValueError):
        build_search_payload(
            "q",
            include_domains=["ｘ.com"],
            blocked_domains=("x.com",),
            with_content=True,
            search_max_results=10,
        )


def test_trim_search_response_preserves_nullable_markdown():
    response = search_response(web=[web_page(markdown=None)], news=[], images=[])
    trimmed = trim_search_response(response, max_content_chars=10)
    assert trimmed["data"]["web"][0]["markdown"] is None


def test_trim_search_response_keeps_warnings_array():
    response = search_response()
    response["warnings"] = ["research category is migrating"]
    trimmed = trim_search_response(response, max_content_chars=10)
    assert trimmed["warnings"] == ["research category is migrating"]


def test_trim_search_response_marks_results_truncated():
    response = search_response(
        web=[
            web_page(url="https://example.com/1"),
            web_page(url="https://example.com/2"),
            web_page(url="https://example.com/3"),
        ],
        news=[],
        images=[],
    )
    trimmed = trim_search_response(response, max_content_chars=100, max_total_results=2)
    assert len(trimmed["data"]["web"]) == 2
    assert trimmed["resultsTruncated"] is True
    full = trim_search_response(response, max_content_chars=100)
    assert len(full["data"]["web"]) == 3
    assert "resultsTruncated" not in full


def test_trim_search_response_research_migration_shape():
    response = search_response(
        web=None, news=None, images=None, research=[{"url": "u"}]
    )
    with pytest.raises(ValueError) as exc_info:
        trim_search_response(response, max_content_chars=100)
    assert "include_domains" in str(exc_info.value)
