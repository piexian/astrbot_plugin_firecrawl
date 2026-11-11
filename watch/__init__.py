"""watch 订阅包：纯核心入口；调度与检查服务由 main 直接从子模块导入。"""

from .core import (
    WATCH_MAX_MESSAGE_CHARS,
    WatchCheckResult,
    WatchRequest,
    build_watch_notification,
    build_watch_request,
    notification_identity,
    parse_watch_response,
)

__all__ = [
    "WATCH_MAX_MESSAGE_CHARS",
    "WatchCheckResult",
    "WatchRequest",
    "build_watch_notification",
    "build_watch_request",
    "notification_identity",
    "parse_watch_response",
]
