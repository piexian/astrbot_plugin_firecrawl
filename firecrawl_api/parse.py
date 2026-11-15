"""firecrawl_parse 参数校验、options 组装、费用上界与结果裁剪。"""

from __future__ import annotations

import os

from . import budget

# Firecrawl 上传硬上限 50 MB；插件按 50_000_000 字节收紧，超限明确要求本地解析。
MAX_PARSE_FILE_BYTES = 50_000_000

SUPPORTED_PARSE_EXTENSIONS = frozenset(
    {
        ".html",
        ".htm",
        ".xhtml",
        ".pdf",
        ".docx",
        ".doc",
        ".docm",
        ".odt",
        ".ods",
        ".odp",
        ".rtf",
        ".xlsx",
        ".xls",
        ".xlsm",
        ".xlsb",
        ".pptx",
        ".ppt",
        ".pptm",
        ".epub",
        ".csv",
    }
)

_PDF_EXTENSION = ".pdf"


def _require_positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数，收到 {value!r}")
    if value < 1:
        raise ValueError(f"{name} 必须是 >= 1 的整数，收到 {value}")
    return value


def _truncate_prose(value: str, max_chars: int) -> tuple[str, bool]:
    if len(value) > max_chars:
        return value[:max_chars], True
    return value, False


def normalize_parse_file(filename: str, size: int) -> dict:
    """校验待解析文件名与大小；超上传上限明确要求本地解析，不当作可上传。"""
    if not isinstance(filename, str):
        raise ValueError(f"filename 必须是字符串，收到 {filename!r}")
    cleaned = filename.strip()
    if not cleaned:
        raise ValueError("filename 不能为空")
    if "/" in cleaned or "\\" in cleaned or "\x00" in cleaned:
        raise ValueError("filename 不能包含路径分隔符或 NUL 字符")
    if cleaned in (".", ".."):
        raise ValueError("filename 不能是单独的 . 或 ..")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError(f"size 必须是非负整数，收到 {size!r}")
    extension = os.path.splitext(cleaned)[1].lower()
    if extension not in SUPPORTED_PARSE_EXTENSIONS:
        raise ValueError(
            f"不支持的文件扩展名，只允许 {'、'.join(sorted(SUPPORTED_PARSE_EXTENSIONS))}"
        )
    if size > MAX_PARSE_FILE_BYTES:
        raise ValueError(
            f"文件大小 {size} 字节超过上传上限 {MAX_PARSE_FILE_BYTES} 字节，"
            "需要本地解析"
        )
    return {"filename": cleaned, "size": size, "is_pdf": extension == _PDF_EXTENSION}


def build_parse_options(
    *,
    pdf_max_pages: int = 20,
    check_prompt_injection: bool = False,
) -> dict:
    """组装 /v2/parse 的 multipart options；参数校验复用 budget.estimate_parse。"""
    budget.estimate_parse(
        is_pdf=True,
        pdf_max_pages=pdf_max_pages,
        check_prompt_injection=check_prompt_injection,
    )
    return {
        "formats": ["markdown"],
        "parsers": [{"type": "pdf", "maxPages": pdf_max_pages}],
        "checkPromptInjection": check_prompt_injection,
    }


def estimate_parse_credits(
    *,
    is_pdf: bool,
    pdf_max_pages: int = 20,
    check_prompt_injection: bool = False,
) -> int:
    """复用 budget.estimate_parse 计算单文件费用上界。"""
    return budget.estimate_parse(
        is_pdf=is_pdf,
        pdf_max_pages=pdf_max_pages,
        check_prompt_injection=check_prompt_injection,
    )


def _optional_page_count(metadata: dict, name: str) -> int | None:
    """页数仅在非 bool 非负整数时投影，其余值一律忽略。"""
    value = metadata.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def trim_parse_response(response: object, *, max_content_chars: int) -> dict:
    """裁剪 parse 响应：markdown 截断、metadata 只投影合法字段并保留顶层 warning。"""
    max_chars = _require_positive_int(max_content_chars, "max_content_chars")
    if not isinstance(response, dict) or not isinstance(response.get("data"), dict):
        raise ValueError("parse 响应结构不符合契约：缺少 data 对象")
    data = response["data"]
    markdown = data.get("markdown")
    if not isinstance(markdown, str):
        raise ValueError("data.markdown 必须是字符串")

    trimmed: dict = {}
    content, truncated = _truncate_prose(markdown, max_chars)
    trimmed["content"] = content
    if truncated:
        trimmed["truncated"] = True

    metadata = data.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict):
            raise ValueError("data.metadata 必须是对象")
        if isinstance(metadata.get("title"), str):
            trimmed["title"] = metadata["title"]
        if isinstance(metadata.get("sourceFile"), str):
            trimmed["sourceFile"] = metadata["sourceFile"]
        num_pages = _optional_page_count(metadata, "numPages")
        total_pages = _optional_page_count(metadata, "totalPages")
        if num_pages is not None:
            trimmed["numPages"] = num_pages
        if total_pages is not None:
            trimmed["totalPages"] = total_pages
        if (
            num_pages is not None
            and total_pages is not None
            and total_pages > num_pages
        ):
            trimmed["pagesTruncated"] = True

    if isinstance(response.get("warning"), str):
        trimmed["warning"] = response["warning"]
    return trimmed
