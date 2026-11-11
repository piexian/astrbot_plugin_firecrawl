"""firecrawl_site map/crawl/batch/status/cancel 参数、任务句柄与状态裁剪。"""

from __future__ import annotations

import re

from . import budget
from .domains import assert_url_allowed, normalize_url
from .scrape import build_scrape_options

_ACTIONS = ("map", "crawl", "batch", "status", "cancel")
_TASK_KINDS = ("crawl", "batch")
_TRIM_ACTIONS = ("map", "crawl", "batch", "status", "cancel")
_KEY_REF_RE = re.compile(r"[0-9a-f]{64}")
_TASK_ID_RE = re.compile(r"[A-Za-z0-9_-]+")
_HANDLE_RE = re.compile(
    rf"(crawl|batch):({_TASK_ID_RE.pattern})@({_KEY_REF_RE.pattern})"
)
_HANDLE_FORMAT = "任务句柄格式必须为 <kind>:<firecrawl_id>@<64位小写sha256 key_ref>"


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


def _require_str(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} 必须是字符串，收到 {value!r}")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{name} 不能为空")
    return cleaned


def _validate_task_id(value: object, name: str) -> str:
    """校验任务ID为 [A-Za-z0-9_-]+，失败固定报错不回显输入。"""
    if not isinstance(value, str) or _TASK_ID_RE.fullmatch(value) is None:
        raise ValueError(f"{name} 只允许 ASCII 字母、数字、下划线、连字符")
    return value


def _require_path_list(value: object, name: str) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} 必须是字符串列表，收到 {value!r}")
    if not value:
        raise ValueError(f"{name} 不能为空列表")
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(f"{name} 条目必须是非空字符串，收到 {entry!r}")
    return list(value)


def _reject_other(label: str, value: object, expected: str) -> None:
    if value is not None:
        raise ValueError(f"{label} 仅在 {expected} 时允许，收到 {label}={value!r}")


def normalize_site_args(
    action: str,
    *,
    url: str | None = None,
    urls: list[str] | None = None,
    limit: int | None = None,
    search: str | None = None,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
    job_id: str | None = None,
    kind: str | None = None,
    site_max_pages: int = 50,
    blocked_domains=(),
) -> dict:
    """按 action 校验 site 参数；未知组合抛 ValueError，不发送请求。"""
    if action not in _ACTIONS:
        raise ValueError(f"action 只允许 {'、'.join(_ACTIONS)}，收到 {action!r}")
    _require_positive_int(site_max_pages, "site_max_pages")

    cleaned_url: str | None = None
    host: str | None = None
    if url is not None:
        host = assert_url_allowed(url, blocked_domains, what="url")
        cleaned_url = url.strip()

    cleaned_urls: list[str] | None = None
    if urls is not None:
        if not isinstance(urls, (list, tuple)):
            raise ValueError(f"urls 必须是字符串列表，收到 {urls!r}")
        if not urls:
            raise ValueError("urls 不能为空")
        if len(urls) > site_max_pages:
            raise ValueError(
                f"urls 数量 {len(urls)} 超过 site_max_pages={site_max_pages}"
            )
        cleaned_urls = []
        for entry in urls:
            assert_url_allowed(entry, blocked_domains, what="url")
            cleaned_urls.append(normalize_url(entry))

    cleaned_limit: int | None = None
    if limit is not None:
        _require_positive_int(limit, "limit")
        if limit > site_max_pages:
            raise ValueError(f"limit 必须是 1-{site_max_pages} 的整数，收到 {limit}")
        cleaned_limit = limit

    cleaned_search = _require_str(search, "search") if search is not None else None
    cleaned_include = (
        _require_path_list(include_paths, "include_paths")
        if include_paths is not None
        else None
    )
    cleaned_exclude = (
        _require_path_list(exclude_paths, "exclude_paths")
        if exclude_paths is not None
        else None
    )
    cleaned_job_id = _validate_task_id(job_id, "job_id") if job_id is not None else None
    if kind is not None and kind not in _TASK_KINDS:
        raise ValueError(f"kind 只允许 {'、'.join(_TASK_KINDS)}")

    if action in ("map", "crawl"):
        if cleaned_url is None:
            raise ValueError(f"action 为 {action} 时 url 必填")
        _reject_other("urls", urls, "batch")
        _reject_other("job_id", job_id, "status/cancel")
        _reject_other("kind", kind, "status/cancel")
        if action == "crawl":
            _reject_other("search", search, "map")
            if cleaned_limit is None:
                raise ValueError("action 为 crawl 时 limit 必填")
        if action == "map":
            _reject_other("include_paths", include_paths, "crawl")
            _reject_other("exclude_paths", exclude_paths, "crawl")
    elif action == "batch":
        if cleaned_urls is None:
            raise ValueError("action 为 batch 时 urls 必填")
        _reject_other("url", url, "map/crawl")
        _reject_other("limit", limit, "map/crawl")
        _reject_other("search", search, "map")
        _reject_other("include_paths", include_paths, "crawl")
        _reject_other("exclude_paths", exclude_paths, "crawl")
        _reject_other("job_id", job_id, "status/cancel")
        _reject_other("kind", kind, "status/cancel")
    else:
        if cleaned_job_id is None:
            raise ValueError(f"action 为 {action} 时 job_id 必填")
        if kind is None:
            raise ValueError(f"action 为 {action} 时 kind 必填")
        _reject_other("url", url, "map/crawl")
        _reject_other("urls", urls, "batch")
        _reject_other("limit", limit, "map/crawl")
        _reject_other("search", search, "map")
        _reject_other("include_paths", include_paths, "crawl")
        _reject_other("exclude_paths", exclude_paths, "crawl")

    result: dict = {"action": action, "site_max_pages": site_max_pages}
    if cleaned_url is not None:
        result["url"] = cleaned_url
    if host is not None:
        result["host"] = host
    if cleaned_urls is not None:
        result["urls"] = cleaned_urls
    if cleaned_limit is not None:
        result["limit"] = cleaned_limit
    if cleaned_search is not None:
        result["search"] = cleaned_search
    if cleaned_include is not None:
        result["include_paths"] = cleaned_include
    if cleaned_exclude is not None:
        result["exclude_paths"] = cleaned_exclude
    if cleaned_job_id is not None:
        result["job_id"] = cleaned_job_id
    if kind is not None:
        result["kind"] = kind
    return result


def build_map_payload(
    url: str,
    *,
    search: str | None = None,
    limit: int | None = None,
    site_max_pages: int = 50,
    blocked_domains=(),
) -> dict:
    """组装 /v2/map 请求体：root URL 先过 blocked_domains。"""
    args = normalize_site_args(
        "map",
        url=url,
        search=search,
        limit=limit,
        site_max_pages=site_max_pages,
        blocked_domains=blocked_domains,
    )
    payload: dict = {"url": args["url"], "limit": args.get("limit", site_max_pages)}
    if "search" in args:
        payload["search"] = args["search"]
    return payload


def build_crawl_payload(
    url: str,
    *,
    limit: int,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
    site_max_pages: int = 50,
    blocked_domains=(),
    pdf_max_pages: int = 20,
    check_prompt_injection: bool = False,
) -> dict:
    """组装 /v2/crawl 请求体：limit 必填，显式禁止外部链接。"""
    args = normalize_site_args(
        "crawl",
        url=url,
        limit=limit,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
        site_max_pages=site_max_pages,
        blocked_domains=blocked_domains,
    )
    payload: dict = {
        "url": args["url"],
        "limit": args["limit"],
        "allowExternalLinks": False,
        "scrapeOptions": build_scrape_options(
            output_format="markdown",
            pdf_max_pages=pdf_max_pages,
            check_prompt_injection=check_prompt_injection,
        ),
    }
    if args.get("include_paths") is not None:
        payload["includePaths"] = args["include_paths"]
    if args.get("exclude_paths") is not None:
        payload["excludePaths"] = args["exclude_paths"]
    return payload


def build_batch_payload(
    urls: list[str],
    *,
    site_max_pages: int = 50,
    blocked_domains=(),
    pdf_max_pages: int = 20,
    check_prompt_injection: bool = False,
) -> dict:
    """组装 /v2/batch/scrape 请求体：逐条校验 URL，不发送 limit，每页选项在顶层。"""
    args = normalize_site_args(
        "batch",
        urls=urls,
        site_max_pages=site_max_pages,
        blocked_domains=blocked_domains,
    )
    payload = {"urls": args["urls"]}
    payload.update(
        build_scrape_options(
            output_format="markdown",
            pdf_max_pages=pdf_max_pages,
            check_prompt_injection=check_prompt_injection,
        )
    )
    return payload


def build_task_request(action: str, *, kind: str, job_id: str) -> dict:
    """校验 status/cancel 请求：job_id 与 kind（crawl/batch）必填。"""
    args = normalize_site_args(action, job_id=job_id, kind=kind)
    return {"action": args["action"], "kind": args["kind"], "job_id": args["job_id"]}


def estimate_site_credits(
    kind: str,
    page_count: int | None = None,
    *,
    pdf_max_pages: int = 20,
    check_prompt_injection: bool = False,
) -> int:
    """复用 budget.estimate_site：map 固定 1，crawl/batch 必须显式页数。"""
    return budget.estimate_site(
        kind,
        page_count,
        pdf_max_pages=pdf_max_pages,
        check_prompt_injection=check_prompt_injection,
    )


def make_task_handle(kind: str, firecrawl_id: str, key_ref: str) -> str:
    """编码 crawl/batch 任务句柄 <kind>:<firecrawl_id>@<完整64位小写key_ref>。"""
    if kind not in _TASK_KINDS:
        raise ValueError(f"kind 只允许 {'、'.join(_TASK_KINDS)}")
    _validate_task_id(firecrawl_id, "firecrawl_id")
    if not isinstance(key_ref, str) or not _KEY_REF_RE.fullmatch(key_ref):
        raise ValueError("key_ref 必须是 64 位小写 SHA-256 十六进制摘要")
    return f"{kind}:{firecrawl_id}@{key_ref}"


def parse_task_handle(handle: str) -> dict:
    """解码任务句柄；格式非法时抛 ValueError，错误信息不回显句柄内容。"""
    if not isinstance(handle, str):
        raise ValueError(_HANDLE_FORMAT)
    match = _HANDLE_RE.fullmatch(handle)
    if match is None:
        raise ValueError(_HANDLE_FORMAT)
    return {
        "kind": match.group(1),
        "firecrawl_id": match.group(2),
        "key_ref": match.group(3),
    }


def _cap_list(items: list, max_items: int | None) -> tuple[list, bool]:
    if max_items is not None and len(items) > max_items:
        return items[:max_items], True
    return items, False


def _project_status_page(page: object, max_content_chars: int) -> dict:
    """把 status 单页投影为 url/title/content/error 白名单，坏结构抛 ValueError。"""
    if not isinstance(page, dict):
        raise ValueError("status 单页结构不符合契约：页必须是对象")
    projected: dict = {}
    truncated = False
    metadata = page.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict):
            raise ValueError("status 单页结构不符合契约：metadata 必须是对象")
        url = metadata.get("url")
        if url is None:
            url = metadata.get("sourceURL")
        if url is not None:
            if not isinstance(url, str):
                raise ValueError("status 单页结构不符合契约：url 必须是字符串")
            projected["url"] = url
        title = metadata.get("title")
        if title is not None:
            if isinstance(title, str):
                projected["title"] = title
            elif isinstance(title, list) and all(isinstance(t, str) for t in title):
                projected["title"] = list(title)
            else:
                raise ValueError(
                    "status 单页结构不符合契约：title 必须是字符串或字符串列表"
                )
    markdown = page.get("markdown")
    if markdown is not None:
        if not isinstance(markdown, str):
            raise ValueError("status 单页结构不符合契约：markdown 必须是字符串")
        content = markdown
        if len(content) > max_content_chars:
            content = content[:max_content_chars]
            truncated = True
        projected["content"] = content
    error = page.get("error")
    if error is None and isinstance(metadata, dict):
        error = metadata.get("error")
    if error is not None:
        if not isinstance(error, str):
            raise ValueError("status 单页结构不符合契约：error 必须是字符串")
        if len(error) > max_content_chars:
            error = error[:max_content_chars]
            truncated = True
        projected["error"] = error
    if truncated:
        projected["truncated"] = True
    return projected


def trim_site_response(
    action: str, response: object, *, max_list_items: int, max_content_chars: int
) -> dict:
    """裁剪 site 响应：map links、batch invalidURLs 与 status 页列表按条数截断，status 页投影白名单，crawl/batch 提交响应白名单保留。"""
    if action not in _TRIM_ACTIONS:
        raise ValueError(
            f"trim_site_response 只支持 {'、'.join(_TRIM_ACTIONS)}，收到 {action!r}"
        )
    _require_positive_int(max_list_items, "max_list_items")
    _require_positive_int(max_content_chars, "max_content_chars")
    if not isinstance(response, dict):
        raise ValueError("site 响应结构不符合契约：响应必须是对象")

    trimmed: dict = {}
    if "success" in response:
        trimmed["success"] = response["success"]
    if isinstance(response.get("warning"), str):
        trimmed["warning"] = response["warning"]

    if isinstance(response.get("warnings"), list):
        trimmed["warnings"] = response["warnings"]
    if "creditsUsed" in response:
        trimmed["creditsUsed"] = response["creditsUsed"]
    if action == "map":
        links = response.get("links")
        if not isinstance(links, list):
            raise ValueError("map 响应结构不符合契约：links 必须是列表")
        kept, capped = _cap_list(links, max_list_items)
        trimmed["links"] = kept
        if capped:
            trimmed["linksTruncated"] = True
        return trimmed

    if action in ("crawl", "batch"):
        firecrawl_id = response.get("id")
        if not isinstance(firecrawl_id, str):
            raise ValueError("crawl/batch 提交响应结构不符合契约：id 必须是字符串")
        trimmed["id"] = _validate_task_id(firecrawl_id, "id")
        url = response.get("url")
        if url is not None:
            if not isinstance(url, str):
                raise ValueError("crawl/batch 提交响应结构不符合契约：url 必须是字符串")
            trimmed["url"] = url
        if action == "batch" and "invalidURLs" in response:
            invalid_urls = response["invalidURLs"]
            if invalid_urls is None:
                trimmed["invalidURLs"] = None
            elif isinstance(invalid_urls, list):
                for entry in invalid_urls:
                    if not isinstance(entry, str):
                        raise ValueError(
                            "crawl/batch 提交响应结构不符合契约：invalidURLs 条目必须是字符串"
                        )
                kept, capped = _cap_list(invalid_urls, max_list_items)
                trimmed["invalidURLs"] = kept
                if capped:
                    trimmed["invalidURLsTruncated"] = True
            else:
                raise ValueError(
                    "crawl/batch 提交响应结构不符合契约：invalidURLs 必须是字符串列表或 null"
                )
        return trimmed

    if action == "status":
        if isinstance(response.get("status"), str):
            trimmed["status"] = response["status"]
        for field in ("completed", "total"):
            if field in response:
                trimmed[field] = response[field]
        data = response.get("data")
        if data is None:
            raw_pages: list = []
        elif isinstance(data, list):
            raw_pages = data
        else:
            raise ValueError("status 响应结构不符合契约：data 必须是列表")
        pages = [_project_status_page(page, max_content_chars) for page in raw_pages]
        kept, capped = _cap_list(pages, max_list_items)
        trimmed["pages"] = kept
        if capped:
            trimmed["pagesTruncated"] = True
        next_url = response.get("next")
        if next_url:
            if not isinstance(next_url, str):
                raise ValueError("status 响应结构不符合契约：next 必须是字符串")
            trimmed["next"] = next_url
            trimmed["hasNext"] = True
        return trimmed

    if isinstance(response.get("status"), str):
        trimmed["status"] = response["status"]
    return trimmed
