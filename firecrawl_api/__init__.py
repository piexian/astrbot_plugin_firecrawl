"""Firecrawl API 核心包。"""

from .client import FirecrawlClient
from .errors import (
    ErrorCategory,
    FirecrawlError,
    MissingKeyError,
    classify_error,
    friendly_message,
    mask_key,
)
from .keys import KeyPool, KeyPoolExhausted

__all__ = [
    "ErrorCategory",
    "FirecrawlClient",
    "FirecrawlError",
    "KeyPool",
    "KeyPoolExhausted",
    "MissingKeyError",
    "classify_error",
    "friendly_message",
    "mask_key",
]
