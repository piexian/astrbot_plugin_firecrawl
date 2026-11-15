"""screenshot 图片下载器：URL 校验、流式限长下载与临时文件清理。"""

from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from astrbot.api import logger

# 截图文件总大小上限与流式分块大小。
MAX_SCREENSHOT_BYTES = 20 * 1024 * 1024
_CHUNK_SIZE = 64 * 1024


class ScreenshotDeliveryError(Exception):
    """截图本地投递失败：消息为固定文案，不携带 URL、响应体或路径。"""


def _validate_screenshot_url(url: str) -> None:
    """截图 URL 只允许无凭据的 http/https 地址，违规一律拒绝且不回显原文。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        raise ScreenshotDeliveryError("截图 URL 无法解析。") from None
    if parts.scheme not in ("http", "https"):
        raise ScreenshotDeliveryError("截图 URL 协议不受支持，仅允许 http/https。")
    if not parts.hostname:
        raise ScreenshotDeliveryError("截图 URL 缺少主机名。")
    if parts.username or parts.password:
        raise ScreenshotDeliveryError("截图 URL 携带凭据，已拒绝下载。")


def _image_suffix(media_type: str) -> str:
    """按 image 子类型生成文件扩展名，无法识别时用通用后缀。"""
    subtype = media_type.removeprefix("image/")
    if re.fullmatch(r"[a-z0-9]{1,8}", subtype):
        return f".{subtype}"
    return ".img"


def _write_chunks(path: Path, chunks: list[bytes]) -> None:
    with path.open("wb") as file:
        for chunk in chunks:
            file.write(chunk)


def _unlink_quietly(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        logger.warning(f"screenshot 临时文件清理失败：{type(error).__name__}")


async def cleanup_screenshot(path: Path | None) -> None:
    """在线程中删除临时截图文件；清理失败只记录异常类型，不影响调用方结果。"""
    await asyncio.to_thread(_unlink_quietly, path)


async def download_screenshot(
    url: str,
    *,
    data_dir: Path,
    timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Path:
    """下载截图到 data_dir/screenshots/ 下的唯一文件；网络与文件写入错误转为固定投递异常。"""
    _validate_screenshot_url(url)
    target_dir = Path(data_dir) / "screenshots"
    path: Path | None = None
    try:
        await asyncio.to_thread(target_dir.mkdir, parents=True, exist_ok=True)
        async with httpx.AsyncClient(
            timeout=timeout_seconds, transport=transport, follow_redirects=False
        ) as client:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise ScreenshotDeliveryError(
                        "截图下载失败：图片服务返回非成功状态。"
                    )
                content_type = response.headers.get("content-type", "")
                media_type = content_type.split(";")[0].strip().lower()
                if not media_type.startswith("image/"):
                    raise ScreenshotDeliveryError("截图下载失败：响应不是图片内容。")
                suffix = _image_suffix(media_type)
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes(_CHUNK_SIZE):
                    size += len(chunk)
                    if size > MAX_SCREENSHOT_BYTES:
                        raise ScreenshotDeliveryError(
                            "截图下载失败：图片超过 20 MiB 上限。"
                        )
                    chunks.append(chunk)
                if size == 0:
                    raise ScreenshotDeliveryError("截图下载失败：图片内容为空。")
                path = target_dir / f"screenshot-{uuid.uuid4().hex}{suffix}"
                await asyncio.to_thread(_write_chunks, path, chunks)
    except (httpx.HTTPError, OSError) as error:
        await cleanup_screenshot(path)
        raise ScreenshotDeliveryError("截图下载失败：网络或文件写入错误。") from error
    except BaseException:
        await cleanup_screenshot(path)
        raise
    return path
