"""firecrawl_api.scrape 参数校验、payload 组装与结果裁剪测试。"""

from __future__ import annotations

import pytest

from firecrawl_api.budget import estimate_page
from firecrawl_api.scrape import (
    build_scrape_options,
    build_scrape_payload,
    estimate_scrape_credits,
    normalize_scrape_args,
    trim_scrape_response,
)

BLOCKED = ("x.com", "twitter.com", "mobile.twitter.com", "linkedin.com")


def scrape_response(**data_overrides):
    data = {
        "markdown": "body" * 10,
        "metadata": {"title": "Page", "sourceURL": "https://example.com/a"},
    }
    data.update(data_overrides)
    return {"success": True, "data": data, "warning": "partial render"}


def test_build_scrape_payload_minimal_defaults():
    payload = build_scrape_payload("https://example.com/a")
    assert payload["url"] == "https://example.com/a"
    assert payload["formats"] == ["markdown"]
    assert payload["parsers"] == [{"type": "pdf", "maxPages": 20}]
    assert payload["proxy"] == "auto"
    assert payload["onlyMainContent"] is True
    assert payload["waitFor"] == 0
    assert payload["checkPromptInjection"] is False
    assert "maxAge" not in payload


def test_build_scrape_payload_rejects_non_full_url():
    with pytest.raises(ValueError):
        build_scrape_payload("example.com/a")
    with pytest.raises(ValueError):
        build_scrape_payload("ftp://example.com/a")
    with pytest.raises(ValueError):
        build_scrape_payload("")
    with pytest.raises(ValueError):
        build_scrape_payload(None)  # type: ignore[arg-type]


def test_build_scrape_payload_rejects_blocked_domains_and_subdomains():
    with pytest.raises(ValueError):
        build_scrape_payload("https://x.com/post", blocked_domains=BLOCKED)
    with pytest.raises(ValueError):
        build_scrape_payload(
            "https://mobile.twitter.com/user/status/1", blocked_domains=BLOCKED
        )
    with pytest.raises(ValueError):
        build_scrape_payload(
            "https://www.linkedin.com/in/someone", blocked_domains=BLOCKED
        )
    build_scrape_payload("https://notx.com/post", blocked_domains=BLOCKED)
    with pytest.raises(ValueError):
        build_scrape_payload("https://X.com/post", blocked_domains=("X.com",))


def test_build_scrape_payload_format_whitelist():
    payload = build_scrape_payload("https://example.com/a", output_format="summary")
    assert payload["formats"] == ["summary"]
    with pytest.raises(ValueError):
        build_scrape_payload("https://example.com/a", output_format="highlights")
    with pytest.raises(ValueError):
        build_scrape_payload("https://example.com/a", output_format="extract")


def test_build_scrape_payload_question_requires_question():
    with pytest.raises(ValueError):
        build_scrape_payload("https://example.com/a", output_format="question")
    payload = build_scrape_payload(
        "https://example.com/a", output_format="question", question="这是什么？"
    )
    assert {"type": "question", "question": "这是什么？"} in payload["formats"]
    with pytest.raises(ValueError):
        build_scrape_payload(
            "https://example.com/a", output_format="markdown", question="问"
        )


def test_build_scrape_payload_json_requires_prompt_and_assembles_object():
    with pytest.raises(ValueError):
        build_scrape_payload("https://example.com/a", output_format="json")
    payload = build_scrape_payload(
        "https://example.com/a",
        output_format="json",
        json_prompt="提取标题",
        json_schema={"type": "object"},
    )
    assert {
        "type": "json",
        "prompt": "提取标题",
        "schema": {"type": "object"},
    } in payload["formats"]
    plain = build_scrape_payload(
        "https://example.com/a", output_format="json", json_prompt="提取标题"
    )
    assert {"type": "json", "prompt": "提取标题"} in plain["formats"]
    with pytest.raises(ValueError):
        build_scrape_payload(
            "https://example.com/a",
            output_format="markdown",
            json_prompt="提取标题",
        )
    with pytest.raises(ValueError):
        build_scrape_payload(
            "https://example.com/a",
            output_format="markdown",
            json_schema={"type": "object"},
        )


def test_build_scrape_payload_pdf_parser_bounds():
    payload = build_scrape_payload("https://example.com/doc.pdf", pdf_max_pages=1)
    assert payload["parsers"] == [{"type": "pdf", "maxPages": 1}]
    with pytest.raises(ValueError):
        build_scrape_payload("https://example.com/a", pdf_max_pages=0)
    with pytest.raises(ValueError):
        build_scrape_payload("https://example.com/a", pdf_max_pages=10001)
    with pytest.raises(ValueError):
        build_scrape_payload("https://example.com/a", pdf_max_pages=True)


def test_build_scrape_payload_proxy_whitelist_and_fetch_options():
    payload = build_scrape_payload(
        "https://example.com/a",
        proxy="enhanced",
        only_main_content=False,
        wait_for_ms=1500,
        max_age_ms=60000,
        check_prompt_injection=True,
    )
    assert payload["proxy"] == "enhanced"
    assert payload["onlyMainContent"] is False
    assert payload["waitFor"] == 1500
    assert payload["maxAge"] == 60000
    assert payload["checkPromptInjection"] is True
    with pytest.raises(ValueError):
        build_scrape_payload("https://example.com/a", proxy="residential")
    with pytest.raises(ValueError):
        build_scrape_payload("https://example.com/a", wait_for_ms=-1)
    with pytest.raises(ValueError):
        build_scrape_payload("https://example.com/a", max_age_ms=-1)
    with pytest.raises(ValueError):
        build_scrape_payload("https://example.com/a", only_main_content="yes")


def test_estimate_scrape_credits_reuses_budget_estimate_page():
    args = normalize_scrape_args(
        "https://example.com/a",
        output_format="json",
        json_prompt="提取标题",
        check_prompt_injection=True,
        pdf_max_pages=5,
    )
    assert estimate_scrape_credits(args) == estimate_page(
        "json", pdf_max_pages=5, check_prompt_injection=True
    )
    assert estimate_scrape_credits(args) == 1 + 4 + 4 + 5


def test_trim_scrape_response_markdown_truncated_with_marker():
    trimmed = trim_scrape_response(scrape_response(), "markdown", max_content_chars=10)
    assert trimmed["url"] == "https://example.com/a"
    assert trimmed["title"] == "Page"
    assert trimmed["content"] == "bodybodybo"
    assert trimmed["truncated"] is True
    assert trimmed["warning"] == "partial render"
    assert "markdown" not in trimmed


def test_trim_scrape_response_content_formats():
    summary = trim_scrape_response(
        scrape_response(summary="一段摘要"), "summary", max_content_chars=1000
    )
    assert summary["content"] == "一段摘要"
    html = trim_scrape_response(
        scrape_response(markdown=None, html="<p>hi</p>"), "html", max_content_chars=1000
    )
    assert html["content"] == "<p>hi</p>"


def test_trim_scrape_response_links_structured_and_capped():
    response = scrape_response(
        markdown=None, links=[f"https://e.com/{i}" for i in range(8)]
    )
    trimmed = trim_scrape_response(
        response, "links", max_content_chars=100, max_list_items=3
    )
    assert trimmed["links"] == [f"https://e.com/{i}" for i in range(3)]
    assert trimmed["linksTruncated"] is True
    full = trim_scrape_response(response, "links", max_content_chars=100)
    assert len(full["links"]) == 8
    assert "linksTruncated" not in full


def test_trim_scrape_response_answer_and_json_kept_valid():
    answer = trim_scrape_response(
        scrape_response(markdown=None, answer="回答" * 10),
        "question",
        max_content_chars=4,
    )
    assert answer["answer"] == "回答回答"
    assert answer["truncated"] is True
    payload = {"name": "example", "tags": ["a", "b"]}
    json_trimmed = trim_scrape_response(
        scrape_response(markdown=None, json=payload), "json", max_content_chars=4
    )
    assert json_trimmed["json"] == payload
    assert "truncated" not in json_trimmed


def test_trim_scrape_response_rejects_missing_or_bad_fields():
    with pytest.raises(ValueError):
        trim_scrape_response(
            scrape_response(markdown=None), "markdown", max_content_chars=10
        )
    with pytest.raises(ValueError):
        trim_scrape_response(
            scrape_response(markdown=None, json={"a": 1}), "links", max_content_chars=10
        )
    with pytest.raises(ValueError):
        trim_scrape_response(scrape_response(), "markdown", max_content_chars=0)


def test_normalize_scrape_args_returns_normalized_fields():
    args = normalize_scrape_args("https://Example.com/a")
    assert args["url"] == "https://Example.com/a"
    assert args["host"] == "example.com"
    assert args["output_format"] == "markdown"
    assert args["check_prompt_injection"] is False


def test_build_scrape_options_assembles_without_url():
    options = build_scrape_options()
    assert options == {
        "formats": ["markdown"],
        "parsers": [{"type": "pdf", "maxPages": 20}],
        "proxy": "auto",
        "onlyMainContent": True,
        "waitFor": 0,
        "checkPromptInjection": False,
    }
    assert "url" not in options


def test_build_scrape_options_payload_equivalence():
    kwargs = {
        "output_format": "json",
        "json_prompt": "提取标题",
        "pdf_max_pages": 5,
        "max_age_ms": 60000,
        "check_prompt_injection": True,
    }
    options = build_scrape_options(**kwargs)
    payload = build_scrape_payload("https://example.com/a", **kwargs)
    assert {k: v for k, v in payload.items() if k != "url"} == options


def test_build_scrape_options_rejects_invalid_option_combinations():
    with pytest.raises(ValueError):
        build_scrape_options(output_format="highlights")
    with pytest.raises(ValueError):
        build_scrape_options(output_format="question")
    with pytest.raises(ValueError):
        build_scrape_options(output_format="json")
    with pytest.raises(ValueError):
        build_scrape_options(pdf_max_pages=0)
    with pytest.raises(ValueError):
        build_scrape_options(pdf_max_pages=10001)
    with pytest.raises(ValueError):
        build_scrape_options(pdf_max_pages=True)
    with pytest.raises(ValueError):
        build_scrape_options(proxy="residential")
    with pytest.raises(ValueError):
        build_scrape_options(only_main_content="yes")
