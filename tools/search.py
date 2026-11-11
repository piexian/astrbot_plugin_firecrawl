"""firecrawl_search FunctionTool：参数规范化、费用估算、预算执行与结果裁剪。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from astrbot.api import FunctionTool, logger

from ..firecrawl_api.errors import ErrorCategory, FirecrawlError
from ..firecrawl_api.search import (
    build_search_payload,
    estimate_search_credits,
    normalize_search_args,
    trim_search_response,
)
from .execution import PendingRequestError
from .result import fail, ok
from .schema import object_schema

_PLUGIN_UNAVAILABLE = FirecrawlError(
    ErrorCategory.CONFIG, "插件未初始化，无法调用 firecrawl_search。"
)

_SEARCH_EXAMPLE = {"query": "firecrawl api", "sources": ["web"]}

_TOOL_DESCRIPTION = (
    "用 Firecrawl 搜索网页。limit 是每个 source 各自的条数上限（1-100，"
    "不超过插件配置的搜索条数上限），最终总条数还受插件配置上限裁剪；"
    "with_content=true 会为每条结果抓取正文，按 scrape 单页费逐条额外计费。"
)


def _search_parameters() -> dict:
    """每次构建全新 schema，保证实例之间互不共享。"""
    return object_schema(
        {
            "query": {
                "type": "string",
                "description": "必填。搜索关键词，最长 500 字符。",
            },
            "limit": {
                "type": "integer",
                "description": (
                    "可选。每个 source 各自返回的条数上限（1-100，且不超过插件"
                    "配置的搜索条数上限），省略时用配置上限。"
                ),
            },
            "sources": {
                "type": "array",
                "items": {"type": "string", "enum": ["web", "news", "images"]},
                "description": (
                    "可选。只接受数组，每项为 web、news、images；"
                    "省略或空数组时搜索 web。"
                ),
            },
            "categories": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["developer", "gov", "research", "pdf"],
                },
                "description": (
                    "可选。只接受数组，每项为 developer、gov、research、pdf；"
                    "developer、gov 必须单独使用，不能与其他类目组合。"
                ),
            },
            "tbs": {
                "type": "string",
                "description": "可选。Google 时间范围或按日期排序，例如 qdr:d。",
            },
            "location": {
                "type": "string",
                "description": "可选。搜索地理位置，例如 New York, NY, United States。",
            },
            "country": {
                "type": "string",
                "description": "可选。偏向结果的国家或地区代码，例如 us。",
            },
            "include_domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "可选。只返回这些域名（含子域名）的结果；"
                    "与 exclude_domains 互斥，只能传其一。"
                ),
            },
            "exclude_domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "可选。从结果中排除这些域名（含子域名）；"
                    "与 include_domains 互斥，只能传其一。"
                ),
            },
            "with_content": {
                "type": "boolean",
                "description": (
                    "可选。是否为每条结果抓取 markdown 正文；开启后按 scrape"
                    " 单页费逐条额外计费，命中插件高价域名列表时会在请求前拒绝。"
                ),
            },
        },
        required=["query"],
    )


@dataclass
class FirecrawlSearchTool(FunctionTool):
    """firecrawl_search 工具：未注入 plugin 时零 I/O 返回 config 错误。"""

    plugin: Any = None
    name: str = "firecrawl_search"
    description: str = _TOOL_DESCRIPTION
    parameters: dict = field(default_factory=_search_parameters)

    def __repr__(self) -> str:
        return (
            f"FuncTool(name={self.name}, parameters={self.parameters}, "
            f"description={self.description})"
        )

    async def call(
        self,
        context: Any,
        *,
        query: str | None = None,
        limit: int | None = None,
        sources: list[str] | None = None,
        categories: list[str] | None = None,
        tbs: str | None = None,
        location: str | None = None,
        country: str | None = None,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        with_content: bool = False,
        **unexpected: Any,
    ) -> str:
        """规范化参数后在 request_scope 内执行一次预算请求，异常统一收敛为 JSON。"""
        if self.plugin is None:
            return fail(_PLUGIN_UNAVAILABLE)
        try:
            if unexpected:
                raise ValueError("不支持的搜索参数。")
            return await self._run(
                query=query,
                limit=limit,
                sources=sources,
                categories=categories,
                tbs=tbs,
                location=location,
                country=country,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                with_content=with_content,
            )
        except Exception as error:
            if not isinstance(error, (FirecrawlError, ValueError, PendingRequestError)):
                logger.warning(f"firecrawl_search 未知异常：{type(error).__name__}")
            return fail(error, example=_SEARCH_EXAMPLE)

    async def _run(self, **kwargs: Any) -> str:
        plugin = self.plugin
        search_max_results = plugin.config.get("search_max_results", 10)
        max_content_chars = plugin.config.get("max_content_chars", 8000)
        pdf_max_pages = plugin.config.get("pdf_max_pages", 20)
        check_prompt_injection = plugin.config.get("check_prompt_injection", False)
        blocked_domains = plugin.config.get("blocked_domains", [])
        options = {
            "limit": kwargs.get("limit"),
            "sources": kwargs.get("sources"),
            "categories": kwargs.get("categories"),
            "tbs": kwargs.get("tbs"),
            "location": kwargs.get("location"),
            "country": kwargs.get("country"),
            "include_domains": kwargs.get("include_domains"),
            "exclude_domains": kwargs.get("exclude_domains"),
            "with_content": kwargs.get("with_content", False),
        }
        args = normalize_search_args(
            kwargs.get("query"),
            **options,
            search_max_results=search_max_results,
            blocked_domains=blocked_domains,
            pdf_max_pages=pdf_max_pages,
            check_prompt_injection=check_prompt_injection,
        )
        payload = build_search_payload(
            kwargs.get("query"),
            **options,
            search_max_results=search_max_results,
            blocked_domains=blocked_domains,
            pdf_max_pages=pdf_max_pages,
            check_prompt_injection=check_prompt_injection,
        )
        estimate = estimate_search_credits(args)

        async with plugin.request_scope():
            response = await plugin.executor.execute_sync(
                "search", payload, estimate=estimate
            )

        trimmed = trim_search_response(
            response.json(),
            max_content_chars=max_content_chars,
            max_total_results=search_max_results,
        )
        return ok(trimmed)
