"""tools.screenshot 下载器测试：URL 校验、流式限长与临时文件清理（MockTransport）。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from astrbot_plugin_firecrawl.tools.screenshot import (
    MAX_SCREENSHOT_BYTES,
    ScreenshotDeliveryError,
    cleanup_screenshot,
    download_screenshot,
)

IMAGE_URL = "https://img.example/shot.png"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"png-data"


def image_response(content: bytes, media_type: str = "image/png") -> httpx.Response:
    return httpx.Response(200, headers={"content-type": media_type}, content=content)


async def download(tmp_path: Path, url: str = IMAGE_URL, transport=None) -> Path:
    return await download_screenshot(
        url,
        data_dir=tmp_path,
        timeout_seconds=5,
        transport=transport,
    )


def no_files(tmp_path: Path) -> bool:
    directory = tmp_path / "screenshots"
    return not directory.exists() or list(directory.iterdir()) == []


@pytest.mark.asyncio
async def test_download_writes_unique_files_with_bytes(tmp_path):
    transport = httpx.MockTransport(lambda request: image_response(PNG_BYTES))
    first = await download(tmp_path, transport=transport)
    second = await download(tmp_path, transport=transport)
    assert first.read_bytes() == PNG_BYTES
    assert second.read_bytes() == PNG_BYTES
    assert first != second
    assert first.suffix == ".png"
    first.unlink()
    second.unlink()


@pytest.mark.asyncio
async def test_download_rejects_non_http_and_credentials(tmp_path):
    for url in (
        "ftp://img.example/s.png",
        "https://user:pw@img.example/s.png",
        "https:///missing-host",
    ):
        with pytest.raises(ScreenshotDeliveryError):
            await download(tmp_path, url=url)
    assert no_files(tmp_path)


@pytest.mark.asyncio
async def test_download_sends_no_authorization_and_refuses_redirect(tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"location": "https://img.example/real.png"})

    with pytest.raises(ScreenshotDeliveryError):
        await download(tmp_path, transport=httpx.MockTransport(handler))
    assert len(seen) == 1
    assert "authorization" not in seen[0].headers
    assert no_files(tmp_path)


@pytest.mark.asyncio
async def test_download_rejects_non_image_content_type(tmp_path):
    transport = httpx.MockTransport(
        lambda request: image_response(b"<html></html>", "text/html")
    )
    with pytest.raises(ScreenshotDeliveryError):
        await download(tmp_path, transport=transport)
    assert no_files(tmp_path)


@pytest.mark.asyncio
async def test_download_rejects_missing_content_type(tmp_path):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=PNG_BYTES)
    )
    with pytest.raises(ScreenshotDeliveryError):
        await download(tmp_path, transport=transport)
    assert no_files(tmp_path)


@pytest.mark.asyncio
async def test_download_rejects_empty_body(tmp_path):
    transport = httpx.MockTransport(lambda request: image_response(b""))
    with pytest.raises(ScreenshotDeliveryError):
        await download(tmp_path, transport=transport)
    assert no_files(tmp_path)


@pytest.mark.asyncio
async def test_download_rejects_oversize_and_cleans_partial(tmp_path):
    big = b"x" * (MAX_SCREENSHOT_BYTES + 1)
    transport = httpx.MockTransport(lambda request: image_response(big))
    with pytest.raises(ScreenshotDeliveryError):
        await download(tmp_path, transport=transport)
    assert no_files(tmp_path)


@pytest.mark.asyncio
async def test_download_rejects_error_status(tmp_path):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(404, headers={"content-type": "image/png"})
    )
    with pytest.raises(ScreenshotDeliveryError):
        await download(tmp_path, transport=transport)
    assert no_files(tmp_path)


@pytest.mark.asyncio
async def test_download_suffix_follows_media_type(tmp_path):
    transport = httpx.MockTransport(
        lambda request: image_response(PNG_BYTES, "image/jpeg; charset=binary")
    )
    path = await download(tmp_path, transport=transport)
    assert path.suffix == ".jpeg"
    path.unlink()


@pytest.mark.asyncio
async def test_download_rejects_unparsable_url(tmp_path):
    with pytest.raises(ScreenshotDeliveryError):
        await download(tmp_path, url="http://[::1")
    assert no_files(tmp_path)


@pytest.mark.asyncio
async def test_cleanup_failure_logged_not_raised(caplog):
    class UnlinkBoom:
        def unlink(self, missing_ok=False):
            raise OSError("boom-unlink")

    with caplog.at_level("WARNING", logger="astrbot.stub"):
        await cleanup_screenshot(UnlinkBoom())
    assert any("OSError" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_download_cancellation_during_network_wait_propagates(tmp_path):
    async def slow_handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return image_response(PNG_BYTES)

    task = asyncio.create_task(
        download(tmp_path, transport=httpx.MockTransport(slow_handler))
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_download_converts_network_error_to_delivery_error(tmp_path):
    def broken_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom-net")

    with pytest.raises(ScreenshotDeliveryError) as excinfo:
        await download(tmp_path, transport=httpx.MockTransport(broken_handler))
    assert type(excinfo.value.__cause__) is httpx.ConnectError
    assert no_files(tmp_path)


@pytest.mark.asyncio
async def test_download_converts_write_oserror_to_delivery_error(tmp_path, monkeypatch):
    def broken_write(path, chunks):
        raise OSError("boom-write")

    monkeypatch.setattr(
        "astrbot_plugin_firecrawl.tools.screenshot._write_chunks", broken_write
    )
    transport = httpx.MockTransport(lambda request: image_response(PNG_BYTES))
    with pytest.raises(ScreenshotDeliveryError) as excinfo:
        await download(tmp_path, transport=transport)
    assert type(excinfo.value.__cause__) is OSError
    assert no_files(tmp_path)
