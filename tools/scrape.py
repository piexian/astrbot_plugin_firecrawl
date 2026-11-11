"""firecrawl_scrape FunctionTool：参数规范化、费用估算、预算执行与结果裁剪。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from astrbot.api import FunctionTool, logger

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

_PLUGIN_UNAVAILABLE = FirecrawlError(
    ErrorCategory.CONFIG, "插件未初始化，无法调用 firecrawl_scrape。"
)

_SCRAPE_EXAMPLE = {"url": "https://example.com"}

_TOOL_DESCRIPTION = (
    "用 Firecrawl 抓取单个网页，返回 markdown 正文、链接或问答等内容；"
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
                "enum": ["markdown", "summary", "links", "html", "question", "json"],
                "description": (
                    "可选。返回内容格式，默认 markdown；question、json 每页额外计费。"
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
        except Exception as error:
            if not isinstance(error, (FirecrawlError, ValueError, PendingRequestError)):
                logger.warning(f"firecrawl_scrape 未知异常：{type(error).__name__}")
            return fail(error, example=_SCRAPE_EXAMPLE)

    async def _run(self, **kwargs: Any) -> str:
        plugin = self.plugin
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
        return ok(trimmed)
