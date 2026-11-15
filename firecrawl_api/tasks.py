"""共享任务句柄：crawl/batch/agent 统一编码与解码 <kind>:<firecrawl_id>@<64位小写sha256>。"""

from __future__ import annotations

import re

TASK_KINDS = ("crawl", "batch", "agent")
_TASK_ID_RE = re.compile(r"[A-Za-z0-9_-]+")
_KEY_REF_RE = re.compile(r"[0-9a-f]{64}")
_HANDLE_RE = re.compile(
    rf"({'|'.join(TASK_KINDS)}):({_TASK_ID_RE.pattern})@({_KEY_REF_RE.pattern})"
)
HANDLE_FORMAT = "任务句柄格式必须为 <kind>:<firecrawl_id>@<64位小写sha256 key_ref>"


def validate_task_id(value: object, name: str) -> str:
    """校验任务ID为 [A-Za-z0-9_-]+，失败固定报错不回显输入。"""
    if not isinstance(value, str) or _TASK_ID_RE.fullmatch(value) is None:
        raise ValueError(f"{name} 只允许 ASCII 字母、数字、下划线、连字符")
    return value


def make_task_handle(kind: str, firecrawl_id: str, key_ref: str) -> str:
    """编码任务句柄 <kind>:<firecrawl_id>@<完整64位小写key_ref>，kind 支持 crawl/batch/agent。"""
    if kind not in TASK_KINDS:
        raise ValueError(f"kind 只允许 {'、'.join(TASK_KINDS)}")
    validate_task_id(firecrawl_id, "firecrawl_id")
    if not isinstance(key_ref, str) or not _KEY_REF_RE.fullmatch(key_ref):
        raise ValueError("key_ref 必须是 64 位小写 SHA-256 十六进制摘要")
    return f"{kind}:{firecrawl_id}@{key_ref}"


def parse_task_handle(handle: str) -> dict:
    """解码任务句柄；格式非法时抛 ValueError，错误信息不回显句柄内容。"""
    if not isinstance(handle, str):
        raise ValueError(HANDLE_FORMAT)
    match = _HANDLE_RE.fullmatch(handle)
    if match is None:
        raise ValueError(HANDLE_FORMAT)
    return {
        "kind": match.group(1),
        "firecrawl_id": match.group(2),
        "key_ref": match.group(3),
    }
