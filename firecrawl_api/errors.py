"""Firecrawl 错误分类与提示。"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any

import httpx

_KEY_PATTERN = re.compile(r"fc-[A-Za-z0-9_-]{6,}")
_USAGE_STATUSES = (400, 404, 409, 413)
_TIMEOUT_STATUSES = (408, 504)

# 403 在无显式上下文时，仅允许按严格的 code 白名单判定凭据失败。
_CREDENTIAL_CODES = frozenset({"INSUFFICIENT_PERMISSIONS"})

_SENSITIVE_FIELDS = ("api_key", "authorization", "token")


class ErrorCategory(Enum):
    """错误类别。"""

    CONFIG = "config"
    AUTH = "auth"
    QUOTA = "quota"
    RATE_LIMIT = "rate_limit"
    USAGE = "usage"
    TIMEOUT = "timeout"
    SERVER = "server"
    NETWORK = "network"


class MissingKeyError(Exception):
    """端点需要 API key 但没有配置。"""


class FirecrawlError(Exception):
    """结构化异常：携带分类、重试与换 key 语义。"""

    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        details: Any = None,
        request_id: str | None = None,
        retry_after: int | None = None,
        retryable: bool = False,
        rotate_key: bool = False,
    ) -> None:
        self.category = category
        self.status = status
        self.code = code
        self.details = _sanitize_details(details)
        self.request_id = request_id
        self.retry_after = retry_after
        self.retryable = retryable
        self.rotate_key = rotate_key
        super().__init__(mask_key(message))


def mask_key(text: str) -> str:
    """把完整 key 打码为 fc-ab12***，避免错误信息泄露 key。"""
    return _KEY_PATTERN.sub(lambda match: match.group(0)[:7] + "***", text)


def _sanitize_details(value: Any) -> Any:
    """递归脱敏 details：字符串中的 key 打码，敏感字段值替换为 ***。"""
    if isinstance(value, str):
        return mask_key(value)
    if isinstance(value, dict):
        return {
            key: "***" if _is_sensitive_field(key) else _sanitize_details(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(_sanitize_details(item) for item in value)
    return value


def _is_sensitive_field(key: Any) -> bool:
    """字段名包含 api_key、authorization、token 即视为敏感。"""
    lowered = str(key).lower()
    return any(field in lowered for field in _SENSITIVE_FIELDS)


def validate_classify_flags(
    credential_error: bool | None = None, retryable_422: bool = False
) -> None:
    """严格校验分类标志：credential_error 仅 None/bool，retryable_422 仅 bool。"""
    if credential_error is not None and not isinstance(credential_error, bool):
        raise ValueError("credential_error 只能是 None 或布尔值")
    if not isinstance(retryable_422, bool):
        raise ValueError("retryable_422 必须是布尔值")


def classify_error(
    response: httpx.Response | None = None,
    exc: Exception | None = None,
    *,
    credential_error: bool | None = None,
    retryable_422: bool = False,
) -> FirecrawlError:
    """错误分类的唯一入口：按 HTTP 响应或传输异常归类，403/422 语义由显式上下文决定。"""
    validate_classify_flags(credential_error, retryable_422)
    if (response is None) == (exc is None):
        raise ValueError("response 与 exc 必须且只能传一个")
    if exc is not None:
        return _classify_exception(exc)
    return _classify_response(response, credential_error, retryable_422)


def _classify_exception(exc: Exception) -> FirecrawlError:
    """传输层异常分类；FirecrawlError 原样返回。"""
    if isinstance(exc, FirecrawlError):
        return exc
    if isinstance(exc, MissingKeyError):
        return FirecrawlError(
            ErrorCategory.CONFIG,
            "该端点需要 API Key，请在插件配置中填写。",
        )
    if isinstance(exc, httpx.TimeoutException):
        return FirecrawlError(
            ErrorCategory.TIMEOUT,
            f"请求超时：{exc}",
            retryable=True,
        )
    if isinstance(exc, httpx.RequestError):
        return FirecrawlError(
            ErrorCategory.NETWORK,
            f"网络连接失败：{exc}",
            retryable=True,
        )
    raise ValueError(f"无法分类的异常类型：{type(exc).__name__}")


def _classify_response(
    response: httpx.Response,
    credential_error: bool | None = None,
    retryable_422: bool = False,
) -> FirecrawlError:
    """按状态码和错误体分类；错误体格式为 {"success": false, "error", "code"}。"""
    status = response.status_code
    body = _response_body(response)
    message, code = _error_fields(body)
    base = {
        "status": status,
        "code": code,
        "details": body,
        "request_id": response.headers.get("x-request-id"),
    }
    if 200 <= status < 400:
        if status == 200 and isinstance(body, dict) and body.get("success") is False:
            return FirecrawlError(
                ErrorCategory.USAGE, message or "请求被拒绝。", **base
            )
        raise ValueError(f"classify_error 不接受成功响应（HTTP {status}）")
    if status == 401:
        return FirecrawlError(
            ErrorCategory.AUTH,
            message or "API Key 无效或已过期。",
            rotate_key=True,
            **base,
        )
    if status == 402:
        return FirecrawlError(
            ErrorCategory.QUOTA,
            message or "Firecrawl 额度已用完。",
            rotate_key=True,
            **base,
        )
    if status == 403:
        if credential_error is None:
            credential_error = code in _CREDENTIAL_CODES
        if credential_error:
            return FirecrawlError(
                ErrorCategory.AUTH,
                message or "当前 Key 无权限。",
                rotate_key=True,
                **base,
            )
        return FirecrawlError(
            ErrorCategory.USAGE,
            message or "目标站点或功能不可用。",
            **base,
        )
    if status == 429:
        return FirecrawlError(
            ErrorCategory.RATE_LIMIT,
            message or "请求过于频繁。",
            retry_after=_parse_retry_after(response.headers.get("retry-after")),
            retryable=True,
            rotate_key=True,
            **base,
        )
    if status in _TIMEOUT_STATUSES:
        return FirecrawlError(
            ErrorCategory.TIMEOUT,
            message or "请求超时。",
            retryable=True,
            **base,
        )
    if status == 422:
        return FirecrawlError(
            ErrorCategory.USAGE,
            message or "请求参数或用法有误。",
            retryable=retryable_422,
            **base,
        )
    if status in _USAGE_STATUSES:
        return FirecrawlError(
            ErrorCategory.USAGE,
            message or "请求参数或用法有误。",
            **base,
        )
    if status >= 500:
        return FirecrawlError(
            ErrorCategory.SERVER,
            message or "Firecrawl 服务错误。",
            retryable=True,
            **base,
        )
    raise ValueError(f"classify_error 不支持的状态码：HTTP {status}")


def _response_body(response: httpx.Response) -> Any:
    """解析 JSON 错误体，非 JSON 时返回 None。"""
    try:
        return response.json()
    except ValueError:
        return None


def _error_fields(body: Any) -> tuple[str, str | None]:
    """取出 error 文本和 code；body 不是对象时为空。"""
    if not isinstance(body, dict):
        return "", None
    message = str(body.get("error") or "")
    code = body.get("code")
    return message, str(code) if code else None


def _parse_retry_after(value: str | None) -> int | None:
    """Retry-After 支持秒数和 HTTP 日期两种写法。"""
    if not value:
        return None
    try:
        return max(0, int(value))
    except ValueError:
        pass
    try:
        due = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    seconds = (due - datetime.now(timezone.utc)).total_seconds()
    return max(0, int(seconds))


_HINTS = {
    ErrorCategory.CONFIG: "未配置 Firecrawl API Key，请在插件配置中填写。",
    ErrorCategory.AUTH: "API Key 无效或无权限，请检查或更换 Key。",
    ErrorCategory.QUOTA: "Firecrawl 额度已用完，请充值或更换 Key。",
    ErrorCategory.RATE_LIMIT: "请求过于频繁，请稍后重试。",
    ErrorCategory.TIMEOUT: "请求超时，请稍后重试。",
    ErrorCategory.SERVER: "Firecrawl 服务暂时不可用，请稍后重试。",
    ErrorCategory.NETWORK: "网络连接失败，请检查网络。",
}


def friendly_message(err: FirecrawlError) -> str:
    """参数与用法错误保留具体原因，其余类别给出简短提示。"""
    if err.category in (ErrorCategory.USAGE, ErrorCategory.CONFIG):
        return str(err)
    hint = _HINTS[err.category]
    if err.category is ErrorCategory.RATE_LIMIT and err.retry_after:
        return f"{hint}请 {err.retry_after} 秒后重试。"
    return hint
