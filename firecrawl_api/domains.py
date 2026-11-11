"""共享域名匹配：URL/host 规范化与 blocked_domains 护栏。"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from urllib.parse import urlsplit

import idna

# 规范化后的域名只允许 LDH 字符（字母、数字、点、连字符）；IP 字面量单独处理。
_LD_HOST_RE = re.compile(r"[a-z0-9.-]+")


def _split_url(url: str):
    """拆出 http/https URL，拒绝反斜杠、用户名/密码与非法端口；异常不回显净区。"""
    if "\\" in url:
        raise ValueError("url 不能包含反斜杠")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise ValueError("url 净区不合法") from None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("url 必须是完整的 http/https 地址")
    if "@" in parts.netloc:
        raise ValueError("url 不能包含用户名或密码")
    try:
        parts.port
    except ValueError:
        raise ValueError("url 端口不合法") from None
    return parts


def _canonical_host(raw_host: str | None) -> str:
    """唯一 host 规范化入口：IP 字面量用 ipaddress，域名经 UTS46/IDNA2008 转 ASCII 小写。"""
    host = (raw_host or "").strip().rstrip(".")
    if not host:
        raise ValueError("host 不能为空")
    if "%" in host:
        raise ValueError("host 不能包含百分号编码")
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        ascii_host = (
            idna.encode(host, uts46=True, transitional=False, std3_rules=True)
            .decode("ascii")
            .lower()
        )
    except (idna.IDNAError, UnicodeError, ValueError):
        raise ValueError("域名无法转换为 IDNA ASCII 形式") from None
    ascii_host = ascii_host.rstrip(".")
    if not ascii_host:
        raise ValueError("host 不能为空")
    if not _LD_HOST_RE.fullmatch(ascii_host):
        raise ValueError("域名包含非法字符")
    return ascii_host


def normalize_url(url: str) -> str:
    """校验完整 http/https URL 且 host 可规范化，返回去首尾空白的原值。"""
    if not isinstance(url, str):
        raise ValueError("url 必须是字符串")
    cleaned = url.strip()
    if not cleaned or any(ch.isspace() for ch in cleaned):
        raise ValueError("url 必须是完整且不含空白的 http/https 地址")
    parts = _split_url(cleaned)
    _canonical_host(parts.hostname)
    return cleaned


def normalize_host(value: str) -> str:
    """把 URL 或裸 host（可带端口）规范化为小写 IP 或 canonical 域名；失败不回显输入。"""
    if not isinstance(value, str):
        raise ValueError("域名必须是字符串")
    cleaned = value.strip()
    if not cleaned or any(ch.isspace() for ch in cleaned):
        raise ValueError("无法从输入解析出域名")
    if "://" in cleaned:
        return _canonical_host(_split_url(cleaned).hostname)
    try:
        ipaddress.ip_address(cleaned)
    except ValueError:
        parts = _split_url(f"https://{cleaned}")
        return _canonical_host(parts.hostname)
    return _canonical_host(cleaned)


def host_matches_domain(host: str, domain: str) -> bool:
    """host 与域名精确相等或为其子域名时匹配。"""
    return host == domain or host.endswith(f".{domain}")


def normalize_blocked_domains(blocked_domains: Iterable[str]) -> list[str]:
    """把 blocked_domains 规范化为 host 列表并去重，保持首次出现顺序。"""
    seen: dict[str, None] = {}
    for raw in blocked_domains:
        seen.setdefault(normalize_host(raw), None)
    return list(seen)


def is_blocked_host(host: str, blocked_domains: Iterable[str]) -> bool:
    """host 是否命中 blocked_domains（含子域名匹配，大小写与 IDNA 折叠不敏感）。"""
    canonical = normalize_host(host)
    return any(
        host_matches_domain(canonical, domain)
        for domain in normalize_blocked_domains(blocked_domains)
    )


def assert_url_allowed(
    url: str, blocked_domains: Iterable[str], *, what: str = "url"
) -> str:
    """校验完整 URL 且未命中 blocked_domains；违规抛 ValueError，返回规范化 host。"""
    cleaned = normalize_url(url)
    host = normalize_host(cleaned)
    if is_blocked_host(host, blocked_domains):
        raise ValueError(f"{what} 命中 blocked_domains，已在请求前拒绝")
    return host


def merge_blocked_into_excludes(
    exclude_domains: Iterable[str], blocked_domains: Iterable[str]
) -> list[str]:
    """把 blocked_domains 规范化后合并进 exclude_domains，按规范化 host 去重。"""
    merged: dict[str, None] = {}
    for raw in exclude_domains:
        merged.setdefault(normalize_host(raw), None)
    for raw in blocked_domains:
        merged.setdefault(normalize_host(raw), None)
    return list(merged)
