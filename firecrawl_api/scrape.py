"""firecrawl_scrape 参数校验、payload 组装、费用上界与结果裁剪。"""

from __future__ import annotations

from . import budget
from .domains import assert_url_allowed

_FORMATS = ("markdown", "summary", "links", "html", "question", "json")
_PROXIES = ("auto", "basic", "enhanced")
_PDF_LIMITS = (1, 10000)
_PROSE_FIELDS = {"markdown": "content", "html": "content", "summary": "content"}


def _require_str(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} 必须是字符串，收到 {value!r}")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{name} 不能为空")
    return cleaned


def _require_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} 必须是布尔值，收到 {value!r}")
    return value


def _require_non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数，收到 {value!r}")
    if value < 0:
        raise ValueError(f"{name} 不能为负数，收到 {value}")
    return value


def _require_positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数，收到 {value!r}")
    if value < 1:
        raise ValueError(f"{name} 必须是 >= 1 的整数，收到 {value}")
    return value


def _normalize_scrape_options(
    *,
    output_format: str = "markdown",
    question: str | None = None,
    json_prompt: str | None = None,
    json_schema: dict | None = None,
    only_main_content: bool = True,
    wait_for_ms: int = 0,
    max_age_ms: int | None = None,
    proxy: str = "auto",
    check_prompt_injection: bool = False,
    pdf_max_pages: int = 20,
) -> dict:
    """校验 scrape 每页选项；scrape 与 site 的 crawl/batch 共用同一套规则。"""
    if output_format not in _FORMATS:
        raise ValueError(f"format 只允许 {'、'.join(_FORMATS)}，收到 {output_format!r}")
    if proxy not in _PROXIES:
        raise ValueError(f"proxy 只允许 {'、'.join(_PROXIES)}，收到 {proxy!r}")
    _require_bool(only_main_content, "only_main_content")
    _require_bool(check_prompt_injection, "check_prompt_injection")
    _require_non_negative_int(wait_for_ms, "wait_for_ms")
    if max_age_ms is not None:
        _require_non_negative_int(max_age_ms, "max_age_ms")
    if (
        isinstance(pdf_max_pages, bool)
        or not isinstance(pdf_max_pages, int)
        or pdf_max_pages < _PDF_LIMITS[0]
        or pdf_max_pages > _PDF_LIMITS[1]
    ):
        raise ValueError(
            f"pdf_max_pages 必须是 {_PDF_LIMITS[0]}-{_PDF_LIMITS[1]} 的整数，"
            f"收到 {pdf_max_pages!r}"
        )

    cleaned_question = None
    cleaned_json_prompt = None
    if question is not None:
        cleaned_question = _require_str(question, "question")
    if json_prompt is not None:
        cleaned_json_prompt = _require_str(json_prompt, "json_prompt")
    if json_schema is not None and not isinstance(json_schema, dict):
        raise ValueError(f"json_schema 必须是对象，收到 {json_schema!r}")

    if output_format == "question" and cleaned_question is None:
        raise ValueError("format 为 question 时必须提供 question")
    if output_format != "question" and cleaned_question is not None:
        raise ValueError("question 仅在 format 为 question 时允许")
    if output_format == "json" and cleaned_json_prompt is None:
        raise ValueError("format 为 json 时必须提供 json_prompt")
    if output_format != "json" and (
        cleaned_json_prompt is not None or json_schema is not None
    ):
        raise ValueError("json_prompt 与 json_schema 仅在 format 为 json 时允许")

    return {
        "output_format": output_format,
        "question": cleaned_question,
        "json_prompt": cleaned_json_prompt,
        "json_schema": json_schema,
        "only_main_content": only_main_content,
        "wait_for_ms": wait_for_ms,
        "max_age_ms": max_age_ms,
        "proxy": proxy,
        "check_prompt_injection": check_prompt_injection,
        "pdf_max_pages": pdf_max_pages,
    }


def normalize_scrape_args(
    url: str,
    *,
    blocked_domains=(),
    **options,
) -> dict:
    """校验并规范化 scrape 参数；URL 命中 blocked_domains 直接拒绝。"""
    args = _normalize_scrape_options(**options)
    host = assert_url_allowed(url, blocked_domains, what="url")
    return {"url": url.strip(), "host": host, **args}


def _assemble_options(args: dict) -> dict:
    """由规范化选项组装每页选项字典；formats/parsers 只在此一处组装。"""
    formats: list = []
    for name in ("markdown", "summary", "links", "html"):
        if args["output_format"] == name:
            formats.append(name)
    if args["output_format"] == "question":
        formats.append({"type": "question", "question": args["question"]})
    if args["output_format"] == "json":
        json_format: dict = {"type": "json", "prompt": args["json_prompt"]}
        if args["json_schema"] is not None:
            json_format["schema"] = args["json_schema"]
        formats.append(json_format)
    payload: dict = {
        "formats": formats,
        "parsers": [{"type": "pdf", "maxPages": args["pdf_max_pages"]}],
        "proxy": args["proxy"],
        "onlyMainContent": args["only_main_content"],
        "waitFor": args["wait_for_ms"],
        "checkPromptInjection": args["check_prompt_injection"],
    }
    if args["max_age_ms"] is not None:
        payload["maxAge"] = args["max_age_ms"]
    return payload


def build_scrape_options(**options) -> dict:
    """不需要 URL 的公共每页选项入口；crawl 在 scrapeOptions、batch 在顶层复用。"""
    return _assemble_options(_normalize_scrape_options(**options))


def build_scrape_payload(url: str, **kwargs) -> dict:
    """组装 /v2/scrape 请求体；每页选项与 crawl/batch 共用 build_scrape_options。"""
    args = normalize_scrape_args(url, **kwargs)
    return {"url": args["url"], **_assemble_options(args)}


def estimate_scrape_credits(args: dict) -> int:
    """复用 budget.estimate_page 计算单页费用上界。"""
    return budget.estimate_page(
        args["output_format"],
        args["pdf_max_pages"],
        args["check_prompt_injection"],
    )


def _truncate_prose(value: str, max_chars: int) -> tuple[str, bool]:
    if len(value) > max_chars:
        return value[:max_chars], True
    return value, False


def trim_scrape_response(
    response: object,
    output_format: str,
    *,
    max_content_chars: int,
    max_list_items: int | None = None,
) -> dict:
    """裁剪 scrape 响应：正文按字符截断，links/json 保持合法结构并按字段级标记。"""
    if output_format not in _FORMATS:
        raise ValueError(f"format 只允许 {'、'.join(_FORMATS)}，收到 {output_format!r}")
    max_chars = _require_positive_int(max_content_chars, "max_content_chars")
    if max_list_items is not None:
        _require_positive_int(max_list_items, "max_list_items")
    if not isinstance(response, dict) or not isinstance(response.get("data"), dict):
        raise ValueError("scrape 响应结构不符合契约：缺少 data 对象")
    data = response["data"]

    trimmed: dict = {}
    metadata = data.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict):
            raise ValueError("data.metadata 必须是对象")
        if isinstance(metadata.get("title"), str):
            trimmed["title"] = metadata["title"]
        if isinstance(metadata.get("sourceURL"), str):
            trimmed["url"] = metadata["sourceURL"]

    if output_format in _PROSE_FIELDS:
        value = data.get(output_format)
        if not isinstance(value, str):
            raise ValueError(f"data.{output_format} 必须是字符串，收到 {value!r}")
        content, truncated = _truncate_prose(value, max_chars)
        trimmed["content"] = content
        if truncated:
            trimmed["truncated"] = True
    elif output_format == "links":
        links = data.get("links")
        if not isinstance(links, list):
            raise ValueError(f"data.links 必须是列表，收到 {links!r}")
        for entry in links:
            if not isinstance(entry, str):
                raise ValueError(f"data.links 条目必须是字符串，收到 {entry!r}")
        if max_list_items is not None and len(links) > max_list_items:
            trimmed["links"] = links[:max_list_items]
            trimmed["linksTruncated"] = True
        else:
            trimmed["links"] = links
    elif output_format == "question":
        answer = data.get("answer")
        if not isinstance(answer, str):
            raise ValueError(f"data.answer 必须是字符串，收到 {answer!r}")
        content, truncated = _truncate_prose(answer, max_chars)
        trimmed["answer"] = content
        if truncated:
            trimmed["truncated"] = True
    else:
        extracted = data.get("json")
        if not isinstance(extracted, dict):
            raise ValueError(f"data.json 必须是对象，收到 {extracted!r}")
        trimmed["json"] = extracted

    if isinstance(response.get("warning"), str):
        trimmed["warning"] = response["warning"]
    return trimmed
