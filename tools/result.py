"""工具结果统一 JSON 封装：ok/fail 产出可解析、脱敏的 JSON 字符串。"""

from __future__ import annotations

import json
from typing import Any

from astrbot.api import logger

from ..firecrawl_api.errors import (
    ErrorCategory,
    FirecrawlError,
    friendly_message,
    mask_key,
)
from .execution import PendingRequestError

_LOCAL_USAGE_HINT = "请根据上述原因修正配置或输入后重试。"
_REMOTE_USAGE_ERROR = "请求被 Firecrawl 拒绝，请检查目标站点与请求用法。"
_REMOTE_USAGE_HINT = "请确认请求符合 Firecrawl 的用法限制，或更换目标站点后重试。"
_PARAMETER_HINT = "请按参数说明修改输入后重试。"
_CANCEL_COMPLETED_REMOTE_ERROR = "Crawl is already completed"
_CANCEL_COMPLETED_ERROR = "任务已完成，无需取消。"
_CANCEL_COMPLETED_HINT = "请使用 action=status 和原 job_id 查询任务结果。"
_SERVER_ERROR = "插件内部错误。"
_SERVER_HINT = "请检查插件日志并与管理员核对；在途请求可用 fc pending 查询。"
_PENDING_STATUS_HINT = "请勿重发该请求；请先用该句柄查询 status 核对实际结果。"
_PENDING_CHECK_HINT = "请勿重发该请求；请与管理员核对或用 fc pending 查询在途请求。"
_REMOTE_ERROR_LOG_LIMIT = 1000
_REMOTE_ERROR_LOG_TRUNCATED = "…[truncated]"


def _log_remote_error(error: FirecrawlError) -> None:
    """经 AstrBot logger 记录脱敏远端错误，整条单行且最多 1000 字符。"""
    if error.status is None:
        return
    message = (
        f"Firecrawl 远端错误：status={error.status} code={error.code!r} "
        f"request_id={error.request_id!r} error={str(error)!r}"
    )
    message = mask_key(message)
    if len(message) > _REMOTE_ERROR_LOG_LIMIT:
        suffix_length = len(_REMOTE_ERROR_LOG_TRUNCATED)
        message = (
            message[: _REMOTE_ERROR_LOG_LIMIT - suffix_length]
            + _REMOTE_ERROR_LOG_TRUNCATED
        )
    logger.warning(message)


def ok(data: Any) -> str:
    """把成功结果封装为 {"ok": true, "data": ...} 的 JSON 字符串。"""
    return json.dumps({"ok": True, "data": data}, ensure_ascii=False, allow_nan=False)


def fail(error: BaseException, *, example: dict[str, Any] | None = None) -> str:
    """把异常收敛为固定字段 JSON，usage 类别附加静态示例；非 Exception 原样上抛。"""
    if not isinstance(error, Exception):
        raise error
    if isinstance(error, PendingRequestError):
        if isinstance(error.__cause__, FirecrawlError):
            _log_remote_error(error.__cause__)
        payload = _pending(error)
    elif isinstance(error, FirecrawlError):
        _log_remote_error(error)
        payload = _firecrawl(error)
    elif isinstance(error, ValueError):
        payload = _usage(error)
    else:
        payload = _server()
    if payload["category"] == "usage" and example is not None:
        payload["example"] = example
    return json.dumps(payload, ensure_ascii=False, allow_nan=False)


def _pending(error: PendingRequestError) -> dict:
    """pending 异常收敛为 indeterminate，携带预留 ID 与可选任务句柄。"""
    if error.task_handle is not None:
        hint = _PENDING_STATUS_HINT
        handle = error.task_handle
    else:
        hint = _PENDING_CHECK_HINT
        handle = None
    payload = {
        "ok": False,
        "error": str(error),
        "category": "indeterminate",
        "retryable": False,
        "hint": hint,
        "pending": True,
        "reservation_id": error.reservation_id,
    }
    if handle is not None:
        payload["task_handle"] = handle
    return payload


def _firecrawl(error: FirecrawlError) -> dict:
    """FirecrawlError 收敛：远端 USAGE/CONFIG 用固定提示，本地保留脱敏原因。"""
    payload = {
        "ok": False,
        "error": "",
        "category": error.category.value,
        "retryable": error.retryable,
        "hint": "",
    }
    if error.category in (ErrorCategory.USAGE, ErrorCategory.CONFIG):
        if error.status is not None:
            # friendly_message 此时透传服务端原文，改用固定提示防泄露。
            details = error.details
            if (
                error.status == 409
                and isinstance(details, dict)
                and details.get("error") == _CANCEL_COMPLETED_REMOTE_ERROR
            ):
                payload["error"] = _CANCEL_COMPLETED_ERROR
                payload["hint"] = _CANCEL_COMPLETED_HINT
            else:
                payload["error"] = _REMOTE_USAGE_ERROR
                payload["hint"] = _REMOTE_USAGE_HINT
        else:
            payload["error"] = mask_key(str(error))
            payload["hint"] = _LOCAL_USAGE_HINT
        return payload
    message = friendly_message(error)
    payload["error"] = message
    payload["hint"] = message
    return payload


def _usage(error: ValueError) -> dict:
    """参数错误收敛为 usage，保留脱敏后的具体原因；示例由 fail 统一附加。"""
    return {
        "ok": False,
        "error": mask_key(str(error)),
        "category": "usage",
        "retryable": False,
        "hint": _PARAMETER_HINT,
    }


def _server() -> dict:
    """未知异常收敛为固定 server 错误，不回显异常内容。"""
    return {
        "ok": False,
        "error": _SERVER_ERROR,
        "category": "server",
        "retryable": False,
        "hint": _SERVER_HINT,
    }
