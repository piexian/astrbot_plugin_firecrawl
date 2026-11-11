"""watch 订阅纯核心：检查请求构造、changeTracking 解析与通知消息构造。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ..firecrawl_api.scrape import (
    build_scrape_options,
    estimate_scrape_credits,
    normalize_scrape_args,
)

# 通知消息总长上限；diff 超出时截断并标注完整长度。
WATCH_MAX_MESSAGE_CHARS = 6000

_WATCH_STATUSES = ("new", "same", "changed", "removed")
_UNAVAILABLE_STATUS = "unavailable"
_CHANGED_STATUS = "changed"


@dataclass(frozen=True)
class WatchRequest:
    """一次 watch 检查的请求体与费用预估。"""

    payload: dict
    estimate: int


@dataclass(frozen=True)
class WatchCheckResult:
    """一次检查的 changeTracking 解析结果。"""

    status: str
    previous_scrape_at: str | None = None
    diff_text: str | None = None
    notification_hash: str | None = None


def _require_tag(tag: object) -> str:
    """订阅 tag 必须是非空字符串。"""
    if not isinstance(tag, str) or not tag.strip():
        raise ValueError("tag 必须是非空字符串")
    return tag.strip()


def build_watch_request(url: str, *, tag: str, **options) -> WatchRequest:
    """构造 watch 检查请求：markdown 基线追加 git-diff changeTracking，含费用预估。"""
    cleaned_tag = _require_tag(tag)
    if "max_age_ms" in options:
        raise ValueError("watch 检查不接受 max_age_ms：changeTracking 绕过缓存")
    if "output_format" in options:
        raise ValueError("watch 检查固定使用 markdown 输出")
    args = normalize_scrape_args(url, output_format="markdown", **options)
    page_options = {
        key: value for key, value in options.items() if key != "blocked_domains"
    }
    payload = {
        "url": args["url"],
        **build_scrape_options(output_format="markdown", **page_options),
    }
    payload["formats"].append(
        {"type": "changeTracking", "modes": ["git-diff"], "tag": cleaned_tag}
    )
    return WatchRequest(payload=payload, estimate=estimate_scrape_credits(args))


def notification_identity(previous_scrape_at: str, diff_text: str) -> str:
    """changed 通知身份：完整状态时间与 diff 文本的 SHA-256，长度前缀消除拼接歧义。"""
    prefixed = f"{len(previous_scrape_at)}\x00{previous_scrape_at}{diff_text}"
    return hashlib.sha256(prefixed.encode("utf-8")).hexdigest()


def parse_watch_response(response: object) -> WatchCheckResult:
    """解析 scrape 响应的 changeTracking；缺失返回 unavailable，坏结构抛固定 ValueError。"""
    if not isinstance(response, dict) or not isinstance(response.get("data"), dict):
        raise ValueError("scrape 响应结构不符合契约：缺少 data 对象")
    tracking = response["data"].get("changeTracking")
    if tracking is None:
        return WatchCheckResult(status=_UNAVAILABLE_STATUS)
    if not isinstance(tracking, dict):
        raise ValueError("changeTracking 必须是对象")
    status = tracking.get("changeStatus")
    if status not in _WATCH_STATUSES:
        raise ValueError("changeTracking.changeStatus 不符合契约")
    previous = tracking.get("previousScrapeAt")
    if status == "new":
        # 官方契约：首次抓取 changeStatus=new 且 previousScrapeAt=null。
        if previous is not None:
            raise ValueError("changeStatus=new 的 previousScrapeAt 必须为 null")
    elif not isinstance(previous, str) or not previous:
        raise ValueError("changeTracking.previousScrapeAt 必须是非空字符串")
    if status != _CHANGED_STATUS:
        return WatchCheckResult(status=status, previous_scrape_at=previous)
    diff = tracking.get("diff")
    if not isinstance(diff, dict) or not isinstance(diff.get("text"), str):
        raise ValueError("changed 状态缺少 diff.text")
    diff_text = diff["text"]
    return WatchCheckResult(
        status=status,
        previous_scrape_at=previous,
        diff_text=diff_text,
        notification_hash=notification_identity(previous, diff_text),
    )


def build_watch_notification(
    url: str,
    result: WatchCheckResult,
    *,
    max_chars: int = WATCH_MAX_MESSAGE_CHARS,
) -> str:
    """构造变更通知：订阅 URL、状态时间与有界 diff，整条不超过 max_chars。"""
    if result.status != _CHANGED_STATUS:
        raise ValueError("仅 changed 状态构造通知")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
        raise ValueError("max_chars 必须是 >= 1 的整数")
    header = (
        f"[Firecrawl watch] 页面已变更\n{url}\n"
        f"上次抓取：{result.previous_scrape_at}\n\n"
    )
    diff = result.diff_text or ""
    limit = max_chars - len(header)
    note = f"\n\n[diff 已截断：完整 {len(diff)} 字符]"
    if limit < len(note):
        raise ValueError("max_chars 容纳不下通知头，请增大 max_chars")
    if len(diff) <= limit:
        return header + diff
    return header + diff[: limit - len(note)] + note
