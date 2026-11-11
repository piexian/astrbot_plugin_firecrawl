"""firecrawl_search 参数校验、payload 组装、费用上界与结果裁剪。"""

from __future__ import annotations

from collections.abc import Iterable

from . import budget
from .domains import (
    host_matches_domain,
    merge_blocked_into_excludes,
    normalize_blocked_domains,
    normalize_host,
)

_SOURCES = ("web", "news", "images")
_CATEGORIES = ("developer", "gov", "research", "pdf")
_LIMITS = (1, 100)
_CONTENT_FORMAT = "markdown"
_QUERY_MAX_LENGTH = 500
_STANDALONE_CATEGORIES = frozenset({"developer", "gov"})
# 各 source 保留的契约字段；正文类字段仅在结果携带时保留并按 max_content_chars 截断。
_WEB_FIELDS = ("title", "url", "description", "markdown")
_NEWS_FIELDS = ("title", "url", "snippet", "date", "imageUrl", "markdown")
_IMAGES_FIELDS = ("title", "url", "imageUrl", "imageWidth", "imageHeight")
_ALLOWED_SOURCES = frozenset({"web", "news", "images"})


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


def _require_positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数，收到 {value!r}")
    if value < 1:
        raise ValueError(f"{name} 必须是 >= 1 的整数，收到 {value}")
    return value


def _require_domain_list(value: object, name: str) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} 必须是字符串列表，收到 {value!r}")
    return [normalize_host(entry) for entry in value]


def _dedup_keep_order(entries: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for entry in entries:
        seen.setdefault(entry, None)
    return list(seen)


def normalize_search_args(
    query: str,
    *,
    limit: int | None = None,
    sources: Iterable[str] | None = None,
    categories: Iterable[str] | None = None,
    tbs: str | None = None,
    location: str | None = None,
    country: str | None = None,
    include_domains: Iterable[str] | None = None,
    exclude_domains: Iterable[str] | None = None,
    with_content: bool = False,
    search_max_results: int = 10,
    blocked_domains: Iterable[str] = (),
    pdf_max_pages: int = 20,
    check_prompt_injection: bool = False,
) -> dict:
    """校验并规范化 search 参数；参数错误抛 ValueError，不发送请求。"""
    cleaned_query = _require_str(query, "query")
    if len(cleaned_query) > _QUERY_MAX_LENGTH:
        raise ValueError(
            f"query 长度不能超过 {_QUERY_MAX_LENGTH}，收到 {len(cleaned_query)} 字符"
        )
    _require_bool(with_content, "with_content")
    _require_bool(check_prompt_injection, "check_prompt_injection")
    _require_positive_int(search_max_results, "search_max_results")
    budget.estimate_page(_CONTENT_FORMAT, pdf_max_pages, check_prompt_injection)
    if limit is None:
        limit = search_max_results
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError(f"limit 必须是整数，收到 {limit!r}")
    if limit < _LIMITS[0] or limit > _LIMITS[1] or limit > search_max_results:
        raise ValueError(
            f"limit 必须在 {_LIMITS[0]}-{min(_LIMITS[1], search_max_results)} 之间，收到 {limit}"
        )

    if sources is None:
        cleaned_sources = ["web"]
    else:
        if isinstance(sources, str) or not isinstance(sources, (list, tuple)):
            raise ValueError(f"sources 必须是字符串列表，收到 {sources!r}")
        if not sources:
            cleaned_sources = ["web"]
        else:
            for entry in sources:
                if entry not in _SOURCES:
                    raise ValueError(
                        f"sources 只允许 {'、'.join(_SOURCES)}，收到 {entry!r}；"
                        "alexandria 暂不开放"
                    )
            cleaned_sources = _dedup_keep_order(sources)
    cleaned_categories: list[str] = []
    if categories is not None:
        if isinstance(categories, str) or not isinstance(categories, (list, tuple)):
            raise ValueError(f"categories 必须是字符串列表，收到 {categories!r}")
        for entry in categories:
            if entry not in _CATEGORIES:
                raise ValueError(
                    f"categories 只允许 {'、'.join(_CATEGORIES)}，收到 {entry!r}"
                )
        cleaned_categories = _dedup_keep_order(categories)

    standalone = [
        entry for entry in cleaned_categories if entry in _STANDALONE_CATEGORIES
    ]
    if standalone and len(cleaned_categories) > 1:
        raise ValueError(
            f"{'、'.join(standalone)} 类目必须单独使用，不能与其他类目组合"
        )

    cleaned_tbs = _require_str(tbs, "tbs") if tbs is not None else None
    cleaned_location = (
        _require_str(location, "location") if location is not None else None
    )
    cleaned_country = _require_str(country, "country") if country is not None else None

    cleaned_include = (
        _require_domain_list(include_domains, "include_domains")
        if include_domains is not None
        else []
    )
    cleaned_exclude = (
        _require_domain_list(exclude_domains, "exclude_domains")
        if exclude_domains is not None
        else []
    )
    if cleaned_include and cleaned_exclude:
        raise ValueError("include_domains 与 exclude_domains 互斥，只能传其一")

    if with_content:
        if cleaned_include:
            blocked = normalize_blocked_domains(blocked_domains)
            for domain in cleaned_include:
                if any(host_matches_domain(domain, banned) for banned in blocked):
                    raise ValueError(
                        f"include_domains 命中 blocked_domains（{domain}），"
                        "已在请求前拒绝"
                    )
        else:
            cleaned_exclude = merge_blocked_into_excludes(
                cleaned_exclude, blocked_domains
            )

    return {
        "query": cleaned_query,
        "limit": limit,
        "sources": cleaned_sources,
        "categories": cleaned_categories,
        "tbs": cleaned_tbs,
        "location": cleaned_location,
        "country": cleaned_country,
        "include_domains": cleaned_include,
        "exclude_domains": cleaned_exclude,
        "with_content": with_content,
        "pdf_max_pages": pdf_max_pages,
        "check_prompt_injection": check_prompt_injection,
    }


def build_search_payload(query: str, **kwargs) -> dict:
    """组装 /v2/search 请求体；固定 highlights=false，with_content 时显式携带 scrapeOptions。"""
    args = normalize_search_args(query, **kwargs)
    payload: dict = {
        "query": args["query"],
        "limit": args["limit"],
        "sources": args["sources"],
        "highlights": False,
    }
    if args["categories"]:
        payload["categories"] = args["categories"]
    if args["tbs"] is not None:
        payload["tbs"] = args["tbs"]
    if args["location"] is not None:
        payload["location"] = args["location"]
    if args["country"] is not None:
        payload["country"] = args["country"]
    if args["include_domains"]:
        payload["includeDomains"] = args["include_domains"]
    elif args["exclude_domains"]:
        payload["excludeDomains"] = args["exclude_domains"]
    if args["with_content"]:
        payload["scrapeOptions"] = {
            "formats": [_CONTENT_FORMAT],
            "onlyMainContent": True,
            "parsers": [{"type": "pdf", "maxPages": args["pdf_max_pages"]}],
            "checkPromptInjection": args["check_prompt_injection"],
        }
    return payload


def estimate_search_credits(args: dict) -> int:
    """按裁剪前的服务端请求上限复用 budget.estimate_search 估算费用上界。"""
    return budget.estimate_search(
        args["limit"],
        len(args["sources"]),
        args["with_content"],
        pdf_max_pages=args["pdf_max_pages"],
        check_prompt_injection=args["check_prompt_injection"],
    )


def _trim_search_item(item: object, fields: tuple[str, ...], max_chars: int) -> dict:
    """按契约字段裁剪单条结果；仅 markdown 正文截断并标注 truncated。"""
    if not isinstance(item, dict):
        raise ValueError(f"搜索结果条目必须是对象，收到 {item!r}")
    trimmed: dict = {}
    for field in fields:
        if field not in item:
            continue
        value = item[field]
        if field == "markdown":
            if value is None:
                trimmed[field] = None
                continue
            if not isinstance(value, str):
                raise ValueError(f"markdown 必须是字符串，收到 {value!r}")
            if len(value) > max_chars:
                trimmed[field] = value[:max_chars]
                trimmed["truncated"] = True
            else:
                trimmed[field] = value
            continue
        if field in ("imageWidth", "imageHeight"):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{field} 必须是数字，收到 {value!r}")
            trimmed[field] = value
            continue
        if not isinstance(value, str):
            raise ValueError(f"{field} 必须是字符串，收到 {value!r}")
        trimmed[field] = value
    return trimmed


def trim_search_response(
    response: object,
    *,
    max_content_chars: int,
    max_total_results: int | None = None,
) -> dict:
    """按 source 契约裁剪搜索响应，保留顶层 warning 与 creditsUsed；不做降费。"""
    max_chars = _require_positive_int(max_content_chars, "max_content_chars")
    if max_total_results is not None:
        _require_positive_int(max_total_results, "max_total_results")
    if not isinstance(response, dict) or not isinstance(response.get("data"), dict):
        raise ValueError("搜索响应结构不符合契约：缺少 data 对象")

    data = response["data"]
    if "research" in data:
        raise ValueError(
            "搜索响应包含 data.research：官方已将 research 迁移为论文记录结构，"
            "当前不支持该论文记录结构；请改用 include_domains 限定来源"
        )
    unknown = set(data) - _ALLOWED_SOURCES
    if unknown:
        raise ValueError(f"搜索响应包含契约之外的 source：{'、'.join(sorted(unknown))}")

    trimmed: dict = {}
    fields_by_source = {
        "web": _WEB_FIELDS,
        "news": _NEWS_FIELDS,
        "images": _IMAGES_FIELDS,
    }
    trimmed_data: dict[str, list] = {}
    total = 0
    for source in _SOURCES:
        items = data.get(source)
        if items is None:
            continue
        if not isinstance(items, list):
            raise ValueError(f"data.{source} 必须是列表，收到 {items!r}")
        trimmed_items = [
            _trim_search_item(item, fields_by_source[source], max_chars)
            for item in items
        ]
        trimmed_data[source] = trimmed_items
        total += len(trimmed_items)

    if max_total_results is not None and total > max_total_results:
        remaining = max_total_results
        for source in _SOURCES:
            if source not in trimmed_data:
                continue
            kept = trimmed_data[source][: max(remaining, 0)]
            trimmed_data[source] = kept
            remaining -= len(kept)
        trimmed["resultsTruncated"] = True

    if "success" in response:
        trimmed["success"] = response["success"]
    if isinstance(response.get("warning"), str):
        trimmed["warning"] = response["warning"]
    if "warnings" in response:
        trimmed["warnings"] = response["warnings"]
    if "creditsUsed" in response:
        trimmed["creditsUsed"] = response["creditsUsed"]
    trimmed["data"] = trimmed_data
    return trimmed
