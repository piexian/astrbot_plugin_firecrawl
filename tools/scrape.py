"""firecrawl_scrape FunctionTool：参数规范化、费用估算、预算执行与结果裁剪。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from astrbot.api import FunctionTool, logger
from astrbot.api.event import MessageChain

from ..firecrawl_api.errors import ErrorCategory, FirecrawlError
from ..firecrawl_api.scrape import (
    build_scrape_payload,
    estimate_scrape_credits,
    normalize_scrape_args,
    trim_scrape_response,
)
from .execution import PendingRequestError
from .result import fail, ok
from .schema import object_schema
from .screenshot import ScreenshotDeliveryError, cleanup_screenshot, download_screenshot

_PLUGIN_UNAVAILABLE = FirecrawlError(
    ErrorCategory.CONFIG, "插件未初始化，无法调用 firecrawl_scrape。"
)

_SCRAPE_EXAMPLE = {"url": "https://example.com"}
_SCREENSHOT_DELIVERY_HINT = "页面抓取已完成并结算，未自动重试；重新调用会再次产生费用。"

_TOOL_DESCRIPTION = (
    "用 Firecrawl 抓取单个网页，返回 markdown 正文、链接或问答等内容；"
    "screenshot 格式会把页面截图发送到当前会话；"
    "question、json 格式与提示注入检测每页各加 4 credits，PDF 页数、黑名单域名与"
    "正文长度上限由插件配置控制。"
)


def _scrape_parameters() -> dict:
    """每次构建全新 schema，保证实例之间互不共享。"""
    return object_schema(
        {
            "url": {
                "type": "string",
                "description": "必填。要抓取的完整 http/https 地址。",
            },
            "format": {
                "type": "string",
                "enum": [
                    "markdown",
                    "summary",
                    "links",
                    "html",
                    "question",
                    "json",
                    "screenshot",
                ],
                "description": (
                    "可选。返回内容格式，默认 markdown；screenshot 会把页面截图"
                    "发送到当前会话；question、json 每页额外计费。"
                ),
            },
            "question": {
                "type": "string",
                "description": (
                    "可选。format 为 question 时必填，其他格式不接受，用于对页面内容提问。"
                ),
            },
            "json_prompt": {
                "type": "string",
                "description": (
                    "可选。format 为 json 时必填，其他格式不接受，指定 JSON 抽取要求。"
                ),
            },
            "json_schema": {
                "type": "object",
                "description": (
                    "可选。仅 format 为 json 时允许，约束抽取结果的 JSON 结构。"
                ),
            },
            "only_main_content": {
                "type": "boolean",
                "description": "可选。是否只提取正文，默认 true。",
            },
            "wait_for_ms": {
                "type": "integer",
                "description": "可选。抓取前等待页面加载的毫秒数，默认 0。",
            },
            "max_age_ms": {
                "type": "integer",
                "description": "可选。允许使用缓存内容的最大毫秒数，省略时使用服务端默认策略。",
            },
            "proxy": {
                "type": "string",
                "enum": ["auto", "basic", "enhanced"],
                "description": "可选。代理级别，默认 auto。",
            },
            "check_prompt_injection": {
                "type": "boolean",
                "description": (
                    "可选。是否检测提示注入；完成检测时每页额外 4 credits，"
                    "检测通过时不返回独立结论，命中注入时请求失败，扫描不完整时返回 warning；"
                    "省略时读取插件配置（默认关闭），显式传入 true/false 覆盖配置。"
                ),
            },
        },
        required=["url"],
    )


@dataclass
class FirecrawlScrapeTool(FunctionTool):
    """firecrawl_scrape 工具：未注入 plugin 时零 I/O 返回 config 错误。"""

    plugin: Any = None
    data_dir: Path | None = None
    download_transport: httpx.AsyncBaseTransport | None = None
    name: str = "firecrawl_scrape"
    description: str = _TOOL_DESCRIPTION
    parameters: dict = field(default_factory=_scrape_parameters)

    def __repr__(self) -> str:
        return (
            f"FuncTool(name={self.name}, parameters={self.parameters}, "
            f"description={self.description})"
        )

    async def call(
        self,
        context: Any,
        *,
        url: str | None = None,
        format: str = "markdown",
        question: str | None = None,
        json_prompt: str | None = None,
        json_schema: dict | None = None,
        only_main_content: bool = True,
        wait_for_ms: int = 0,
        max_age_ms: int | None = None,
        proxy: str = "auto",
        check_prompt_injection: bool | None = None,
        **unexpected: Any,
    ) -> str:
        """规范化参数后在 request_scope 内执行一次预算请求，异常统一收敛为 JSON。"""
        if self.plugin is None:
            return fail(_PLUGIN_UNAVAILABLE)
        try:
            if unexpected:
                raise ValueError("不支持的抓取参数。")
            return await self._run(
                context,
                url=url,
                format=format,
                question=question,
                json_prompt=json_prompt,
                json_schema=json_schema,
                only_main_content=only_main_content,
                wait_for_ms=wait_for_ms,
                max_age_ms=max_age_ms,
                proxy=proxy,
                check_prompt_injection=check_prompt_injection,
            )
        except ScreenshotDeliveryError as error:
            cause = error.__cause__
            cause_type = (
                type(cause).__name__ if cause is not None else type(error).__name__
            )
            logger.warning(f"firecrawl_scrape 截图投递失败：{cause_type}")
            return fail(error, hint=_SCREENSHOT_DELIVERY_HINT)
        except Exception as error:
            if not isinstance(error, (FirecrawlError, ValueError, PendingRequestError)):
                logger.warning(f"firecrawl_scrape 未知异常：{type(error).__name__}")
            return fail(error, example=_SCRAPE_EXAMPLE)

    def _require_delivery_context(self, context: Any) -> Any:
        """screenshot 必须先确认会话事件与数据目录，再做任何付费请求。"""
        wrapper = getattr(context, "context", None)
        event = getattr(wrapper, "event", None)
        if event is None:
            raise ValueError("screenshot 格式必须在当前会话事件中调用，无法发送截图。")
        if self.data_dir is None:
            raise ValueError("插件数据目录未注入，无法保存截图。")
        return event

    async def _deliver_screenshot(self, trimmed: dict, event: Any) -> None:
        """在同一 request_scope 内下载截图、发送到当前会话并清理临时文件。"""
        screenshot_url = trimmed.pop("screenshot")
        path = await download_screenshot(
            screenshot_url,
            data_dir=self.data_dir,
            timeout_seconds=float(self.plugin.config.get("timeout_seconds", 60)),
            transport=self.download_transport,
        )
        try:
            await event.send(MessageChain().file_image(str(path)))
        except Exception as error:
            raise ScreenshotDeliveryError(
                "页面截图已抓取并结算，但图片发送失败。"
            ) from error
        finally:
            await cleanup_screenshot(path)
        trimmed["sent_to_user"] = True

    async def _run(self, context: Any, **kwargs: Any) -> str:
        plugin = self.plugin
        is_screenshot = kwargs["format"] == "screenshot"
        event = self._require_delivery_context(context) if is_screenshot else None
        pdf_max_pages = plugin.config.get("pdf_max_pages", 20)
        max_content_chars = plugin.config.get("max_content_chars", 8000)
        config_injection = plugin.config.get("check_prompt_injection", False)
        blocked_domains = plugin.config.get("blocked_domains", [])
        injection = kwargs["check_prompt_injection"]
        if injection is None:
            injection = config_injection
        options = {
            "output_format": kwargs["format"],
            "question": kwargs["question"],
            "json_prompt": kwargs["json_prompt"],
            "json_schema": kwargs["json_schema"],
            "only_main_content": kwargs["only_main_content"],
            "wait_for_ms": kwargs["wait_for_ms"],
            "max_age_ms": kwargs["max_age_ms"],
            "proxy": kwargs["proxy"],
            "check_prompt_injection": injection,
            "pdf_max_pages": pdf_max_pages,
        }
        args = normalize_scrape_args(
            kwargs["url"], **options, blocked_domains=blocked_domains
        )
        payload = build_scrape_payload(
            kwargs["url"], **options, blocked_domains=blocked_domains
        )
        estimate = estimate_scrape_credits(args)

        async with plugin.request_scope():
            response = await plugin.executor.execute_sync(
                "scrape", payload, estimate=estimate
            )
            trimmed = trim_scrape_response(
                response.json(),
                kwargs["format"],
                max_content_chars=max_content_chars,
            )
            if is_screenshot:
                await self._deliver_screenshot(trimmed, event)

        return ok(trimmed)
