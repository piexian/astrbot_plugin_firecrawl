"""Firecrawl HTTP 客户端：统一 /v2 传输、key 故障转移与错误分类。"""

from __future__ import annotations

import hashlib
import logging
from typing import Any
from urllib.parse import urlsplit

import httpx

from .errors import (
    ErrorCategory,
    FirecrawlError,
    MissingKeyError,
    classify_error,
    validate_classify_flags,
)
from .keys import KeyPool, KeyPoolExhausted

_API_VERSION = "/v2"
_DEFAULT_BASE_URL = "https://api.firecrawl.dev"
_DEFAULT_TIMEOUT_SECONDS = 60.0

# Key 池为空时仍允许匿名的端点。
_ANONYMOUS_ENDPOINTS = frozenset({("POST", "/scrape"), ("POST", "/search")})


def _config_error(message: str) -> FirecrawlError:
    return FirecrawlError(ErrorCategory.CONFIG, message)


def _validate_base_url(base_url: str) -> str:
    """校验 base_url：http/https 根地址或代理路径，不含 /v2、凭据、query、fragment。"""
    cleaned = base_url.strip()
    parts = urlsplit(cleaned)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise _config_error("base_url 必须以 http:// 或 https:// 开头。")
    if parts.username is not None or parts.password is not None:
        raise _config_error("base_url 不能携带凭据（user:pass@host）。")
    if parts.query or parts.fragment:
        raise _config_error("base_url 不能携带 query 或 fragment。")
    if parts.path.rstrip("/").endswith(_API_VERSION):
        raise _config_error("base_url 不应包含 /v2，插件会自动拼接版本路径。")
    return cleaned.rstrip("/")


def _validate_endpoint(endpoint: str) -> None:
    """校验 endpoint：以单个 / 开头的相对路径，拒绝绝对 URL 与路径穿越。"""
    if not endpoint.startswith("/") or endpoint.startswith("//"):
        raise _config_error("endpoint 必须以单个 / 开头。")
    if "://" in endpoint or "?" in endpoint or "#" in endpoint:
        raise _config_error("endpoint 不能是绝对 URL 或携带 query/fragment。")
    if any(segment == ".." for segment in endpoint.split("/")):
        raise _config_error("endpoint 不允许路径穿越（..）。")


def _response_error(
    response: httpx.Response,
    retryable_422: bool = False,
    credential_error: bool | None = None,
) -> FirecrawlError | None:
    """非 2xx 或 200 success:false 归类为错误；3xx 不跳转，按 usage 报告。"""
    status = response.status_code
    if 300 <= status < 400:
        return FirecrawlError(
            ErrorCategory.USAGE,
            f"已禁止自动跳转，服务端返回 HTTP {status}。",
            status=status,
        )
    if not 200 <= status < 300:
        return classify_error(
            response=response,
            retryable_422=retryable_422,
            credential_error=credential_error,
        )
    try:
        body = response.json()
    except ValueError:
        return FirecrawlError(
            ErrorCategory.USAGE,
            "响应体不是有效的 JSON。",
            status=status,
        )
    if status == 200 and isinstance(body, dict) and body.get("success") is False:
        return classify_error(response=response)
    return None


class FirecrawlClient:
    """通用 Firecrawl /v2 客户端：不解释业务 payload，只管传输与 key 故障转移。"""

    def __init__(
        self,
        keys: KeyPool,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        logger: Any | None = None,
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("client 与 transport 只能注入其一。")
        self._base_url = _validate_base_url(base_url)
        self._timeout = float(timeout_seconds)
        self._pool = keys
        self._logger = logger if logger is not None else logging.getLogger(__name__)
        self._owns_client = client is None
        if client is not None:
            self._client = client
        else:
            self._client = httpx.AsyncClient(transport=transport)

    async def request(
        self,
        method: str,
        endpoint: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        retryable_422: bool = False,
        credential_error: bool | None = None,
    ) -> httpx.Response:
        """发送 /v2 请求；2xx 返回 Response，全部 key 失败抛最后一次错误。"""
        validate_classify_flags(credential_error, retryable_422)
        _validate_endpoint(endpoint)
        url = f"{self._base_url}{_API_VERSION}{endpoint}"
        anonymous = self._pool.key_count == 0
        if anonymous and (method.upper(), endpoint) not in _ANONYMOUS_ENDPOINTS:
            raise classify_error(exc=MissingKeyError())

        attempted: set[str] = set()
        last_error: FirecrawlError | None = None
        while True:
            key = None
            if not anonymous:
                try:
                    key = await self._pool.acquire(attempted)
                except KeyPoolExhausted:
                    break
                attempted.add(key)
            try:
                response = await self._send(method, url, key, json, params, headers)
            except httpx.RequestError as exc:
                error = classify_error(exc=exc)
                if key is not None:
                    self._warn_failure(key, error)
                raise error
            error = _response_error(response, retryable_422, credential_error)
            if error is None:
                if key is not None:
                    await self._pool.mark_success(key)
                return response
            if key is None:
                raise error
            self._warn_failure(key, error)
            if not error.rotate_key:
                raise error
            last_error = error
            await self._pool.mark_failure(key, error)
        raise last_error

    async def aclose(self) -> None:
        """只关闭自己创建的 httpx client；注入的 client 归调用方管理。"""
        if self._owns_client:
            await self._client.aclose()

    async def _send(
        self,
        method: str,
        url: str,
        key: str | None,
        json: Any,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
    ) -> httpx.Response:
        """手动构造 Request 发送：统一剥离 Authorization，关闭注入认证与自动跳转。"""
        request_headers = httpx.Headers(dict(headers or {}))
        if "authorization" in request_headers:
            del request_headers["authorization"]
        if key is not None:
            request_headers["Authorization"] = f"Bearer {key}"
        request = httpx.Request(
            method, url, json=json, params=params, headers=request_headers
        )
        request.extensions["timeout"] = httpx.Timeout(self._timeout).as_dict()
        return await self._client.send(request, auth=None, follow_redirects=False)

    def _warn_failure(self, key: str, error: FirecrawlError) -> None:
        """每次失败写 warning：只含 key 的 SHA-256 引用、category 和 status。"""
        ref = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
        status = error.status if error.status is not None else "transport"
        self._logger.warning(
            f"Firecrawl 请求失败：key=sha256:{ref} "
            f"category={error.category.value} status={status}"
        )
