"""firecrawl_agent 参数校验、payload 组装、费用上界与任务状态裁剪。"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable

from .domains import assert_url_allowed, normalize_url

# /agent 契约上限：prompt 长度、maxCredits 范围与 urls 数量。
MAX_AGENT_PROMPT_CHARS = 10000
MAX_AGENT_CREDITS = 2500
MAX_AGENT_URLS = 100

_AGENT_STATUSES = frozenset({"processing", "completed", "failed", "cancelled"})
_AGENT_STRING_FIELDS = ("expiresAt", "message", "model", "stopReason", "error")


def estimate_agent_credits(max_credits: int) -> int:
    """校验 maxCredits 并原值返回，作为 /agent 请求的费用上界。"""
    if isinstance(max_credits, bool) or not isinstance(max_credits, int):
        raise ValueError(f"max_credits 必须是整数，收到 {max_credits!r}")
    if not 1 <= max_credits <= MAX_AGENT_CREDITS:
        raise ValueError(
            f"max_credits 必须在 1 到 {MAX_AGENT_CREDITS} 之间，收到 {max_credits}"
        )
    return max_credits


def _normalize_agent_urls(urls: object, blocked_domains: Iterable[str]) -> list[str]:
    if not isinstance(urls, (list, tuple)):
        raise ValueError("urls 必须是字符串列表，不接受单个字符串")
    seen: dict[str, None] = {}
    for raw in urls:
        cleaned = normalize_url(raw)
        assert_url_allowed(cleaned, blocked_domains, what="url")
        seen.setdefault(cleaned, None)
    if len(seen) > MAX_AGENT_URLS:
        raise ValueError(f"urls 去重后最多 {MAX_AGENT_URLS} 条，收到 {len(seen)} 条")
    return list(seen)


def _canonical_json(source: dict) -> str:
    """按契约键序序列化对象；NaN/Infinity 与非 JSON 类型统一按 ValueError 拒绝。"""
    try:
        return json.dumps(source, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"对象包含无法 JSON 序列化的值：{exc}") from None


def _normalize_agent_schema(schema: object) -> dict:
    if not isinstance(schema, dict):
        raise ValueError("schema 必须是 JSON 对象")
    return json.loads(_canonical_json(schema))


def build_agent_payload(
    *,
    prompt: str,
    max_credits: int,
    urls: Iterable[str] | None = None,
    schema: dict | None = None,
    strict_constrain_to_urls: bool = False,
    blocked_domains: Iterable[str] = (),
) -> dict:
    """组装 /agent 请求体，只包含 prompt/maxCredits 与实际提供的字段。"""
    if not isinstance(prompt, str):
        raise ValueError(f"prompt 必须是字符串，收到 {prompt!r}")
    cleaned_prompt = prompt.strip()
    if not cleaned_prompt:
        raise ValueError("prompt 不能为空")
    if len(cleaned_prompt) > MAX_AGENT_PROMPT_CHARS:
        raise ValueError(
            f"prompt 去首尾空白后最多 {MAX_AGENT_PROMPT_CHARS} 字符，"
            f"收到 {len(cleaned_prompt)} 字符"
        )
    credits = estimate_agent_credits(max_credits)
    if not isinstance(strict_constrain_to_urls, bool):
        raise ValueError(
            f"strict_constrain_to_urls 必须是布尔值，收到 {strict_constrain_to_urls!r}"
        )

    payload: dict = {"prompt": cleaned_prompt, "maxCredits": credits}
    if urls is not None:
        payload["urls"] = _normalize_agent_urls(urls, blocked_domains)
    if schema is not None:
        payload["schema"] = _normalize_agent_schema(schema)
    if strict_constrain_to_urls:
        if not payload.get("urls"):
            raise ValueError("strict_constrain_to_urls=true 时必须至少提供一个 url")
        payload["strictConstrainToURLs"] = True
    return payload


def _require_positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数，收到 {value!r}")
    if value < 1:
        raise ValueError(f"{name} 必须是 >= 1 的整数，收到 {value}")
    return value


def _credits_used(value: object) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("creditsUsed 必须是非负有限数字")
    if not math.isfinite(value) or value < 0:
        raise ValueError("creditsUsed 必须是非负有限数字")
    return value


def trim_agent_response(response: object, *, max_content_chars: int) -> dict:
    """裁剪 /agent/{jobId} 状态响应：白名单投影，data/partial 序列化为 content。"""
    _require_positive_int(max_content_chars, "max_content_chars")
    if not isinstance(response, dict):
        raise ValueError("agent 响应结构不符合契约：响应必须是对象")
    status = response.get("status")
    if not isinstance(status, str) or status not in _AGENT_STATUSES:
        raise ValueError("agent 响应结构不符合契约：status 不合法")

    data = response.get("data")
    partial = response.get("partial")
    if data is not None and partial is not None:
        raise ValueError("agent 响应结构不符合契约：data 与 partial 不能同时出现")
    if status == "completed":
        if not isinstance(data, dict):
            raise ValueError("agent 响应结构不符合契约：completed 必须携带 data 对象")
    elif status == "processing":
        if data is not None or partial is not None:
            raise ValueError(
                "agent 响应结构不符合契约：processing 不携带 data 或 partial"
            )
    elif data is not None:
        raise ValueError("agent 响应结构不符合契约：failed/cancelled 不携带 data")
    elif status == "cancelled" and partial is not None:
        raise ValueError("agent 响应结构不符合契约：cancelled 不携带 partial")
    elif partial is not None and not isinstance(partial, dict):
        raise ValueError("agent 响应结构不符合契约：partial 必须是对象")

    trimmed: dict = {"status": status}
    credits_used = response.get("creditsUsed")
    if credits_used is not None:
        trimmed["creditsUsed"] = _credits_used(credits_used)
    for name in _AGENT_STRING_FIELDS:
        value = response.get(name)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError(f"agent 响应结构不符合契约：{name} 必须是字符串")
        trimmed[name] = value
    partial_schema_valid = response.get("partialSchemaValid")
    if partial_schema_valid is not None:
        if not isinstance(partial_schema_valid, bool):
            raise ValueError(
                "agent 响应结构不符合契约：partialSchemaValid 必须是布尔值"
            )
        trimmed["partialSchemaValid"] = partial_schema_valid

    source = data if data is not None else partial
    if source is not None:
        content = _canonical_json(source)
        if len(content) > max_content_chars:
            content = content[:max_content_chars]
            trimmed["truncated"] = True
        trimmed["content"] = content
        if source is partial:
            trimmed["partial"] = True
    return trimmed
