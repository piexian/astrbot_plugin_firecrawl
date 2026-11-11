"""firecrawl_site FunctionTool：支持 map 链接映射、crawl 站点爬取与 batch 批量抓取，及 status/cancel 任务查询取消；费用估算、预算执行与结果裁剪。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from astrbot.api import FunctionTool, logger

from ..firecrawl_api.errors import ErrorCategory, FirecrawlError
from ..firecrawl_api.site import (
    build_batch_payload,
    build_crawl_payload,
    build_map_payload,
    estimate_site_credits,
    make_task_handle,
    normalize_site_args,
    parse_task_handle,
    trim_site_response,
)
from .execution import PendingRequestError
from .result import fail, ok
from .schema import object_schema

_PLUGIN_UNAVAILABLE = FirecrawlError(
    ErrorCategory.CONFIG, "插件未初始化，无法调用 firecrawl_site。"
)

_SITE_EXAMPLE = {"action": "map", "url": "https://example.com"}

_TOOL_DESCRIPTION = (
    "用 Firecrawl 获取站点链接映射（map）、爬取站点页面（crawl）或批量抓取（batch）；"
    "map 固定消耗 1 credit；crawl/batch 异步执行，默认仅管理员可用，限制可在配置中关闭。"
    "crawl/batch 费用受页面数量、PDF 页数上限和提示注入检测影响；status/cancel 用 job_id 查询或取消 crawl/batch 任务。"
)


def _site_parameters() -> dict:
    """每次构建全新 schema，保证实例之间互不共享。"""
    return object_schema(
        {
            "action": {
                "type": "string",
                "enum": ["map", "crawl", "batch", "status", "cancel"],
                "description": "必填。map 获取站点链接；crawl 爬取站点；batch 批量抓取；status/cancel 用 job_id 查询或取消 crawl/batch 任务；crawl/batch 默认仅管理员可用，限制可在配置中关闭。",
            },
            "url": {
                "type": "string",
                "description": "仅 map/crawl 必填，值为完整 http/https 站点地址；batch 改用 urls；status/cancel 不要传 url，只传完整 job_id。",
            },
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "仅 batch 必填。要抓取的完整 http/https 地址列表，数量受插件配置上限约束；任一 URL 校验失败或命中 blocked_domains 都会整批拒绝，不自动剔除。",
            },
            "limit": {
                "type": "integer",
                "description": "map 可选；crawl 必填。最多处理的页面数量，受插件配置上限约束。",
            },
            "search": {
                "type": "string",
                "description": "仅 map。按关键词过滤返回的链接。",
            },
            "include_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "仅 crawl。只爬取与路径规则匹配的页面。",
            },
            "exclude_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "仅 crawl。排除与路径规则匹配的页面。",
            },
            "job_id": {
                "type": "string",
                "description": "仅 status/cancel。完整任务句柄，即 crawl/batch 提交结果中的 task_handle。",
            },
        },
        required=["action"],
    )


@dataclass
class FirecrawlSiteTool(FunctionTool):
    """firecrawl_site 工具：未注入 plugin 时零 I/O 返回 config 错误。"""

    plugin: Any = None
    name: str = "firecrawl_site"
    description: str = _TOOL_DESCRIPTION
    parameters: dict = field(default_factory=_site_parameters)

    def __repr__(self) -> str:
        return (
            f"FuncTool(name={self.name}, parameters={self.parameters}, "
            f"description={self.description})"
        )

    async def call(
        self,
        context: Any,
        *,
        action: str | None = None,
        url: str | None = None,
        urls: list[str] | None = None,
        limit: int | None = None,
        search: str | None = None,
        include_paths: list[str] | None = None,
        exclude_paths: list[str] | None = None,
        job_id: str | None = None,
        **unexpected: Any,
    ) -> str:
        """校验 action 与管理员权限后在 request_scope 内执行一次预算请求，异常统一收敛为 JSON。"""
        if self.plugin is None:
            return fail(_PLUGIN_UNAVAILABLE)
        try:
            if unexpected:
                raise ValueError("不支持的站点参数。")
            if action not in ("map", "crawl", "batch", "status", "cancel"):
                raise ValueError("action 只允许 map、crawl、batch、status、cancel。")
            if action in ("crawl", "batch") and self.plugin.config.get(
                "site_admin_only", True
            ):
                event = getattr(getattr(context, "context", None), "event", None)
                if event is None or not event.is_admin():
                    raise ValueError(f"{action} 仅管理员可用。")
            return await self._run(
                action=action,
                url=url,
                urls=urls,
                limit=limit,
                search=search,
                include_paths=include_paths,
                exclude_paths=exclude_paths,
                job_id=job_id,
            )
        except Exception as error:
            if not isinstance(error, (FirecrawlError, ValueError, PendingRequestError)):
                logger.warning(f"firecrawl_site 未知异常：{type(error).__name__}")
            return fail(error, example=_SITE_EXAMPLE)

    async def _run(
        self,
        *,
        action: str,
        url: str | None,
        urls: list[str] | None,
        limit: int | None,
        search: str | None,
        include_paths: list[str] | None,
        exclude_paths: list[str] | None,
        job_id: str | None = None,
    ) -> str:
        plugin = self.plugin
        site_max_pages = plugin.config.get("site_max_pages", 50)
        max_content_chars = plugin.config.get("max_content_chars", 8000)
        blocked_domains = plugin.config.get("blocked_domains", [])
        kind = None
        task_id = job_id
        if action in ("status", "cancel"):
            parsed_handle = parse_task_handle(job_id)
            kind = parsed_handle["kind"]
            task_id = parsed_handle["firecrawl_id"]
        normalize_site_args(
            action,
            url=url,
            urls=urls,
            limit=limit,
            search=search,
            include_paths=include_paths,
            exclude_paths=exclude_paths,
            job_id=task_id,
            kind=kind,
            site_max_pages=site_max_pages,
            blocked_domains=blocked_domains,
        )
        if action in ("status", "cancel"):
            async with plugin.request_scope():
                if action == "status":
                    response = await plugin.executor.status(job_id)
                else:
                    response = await plugin.executor.cancel(job_id)
            trimmed = trim_site_response(
                action,
                response.json(),
                max_list_items=site_max_pages,
                max_content_chars=max_content_chars,
            )
            return ok(trimmed)
        if action == "batch":
            pdf_max_pages = plugin.config.get("pdf_max_pages", 20)
            check_prompt_injection = plugin.config.get("check_prompt_injection", False)
            payload = build_batch_payload(
                urls,
                site_max_pages=site_max_pages,
                blocked_domains=blocked_domains,
                pdf_max_pages=pdf_max_pages,
                check_prompt_injection=check_prompt_injection,
            )
            estimate = estimate_site_credits(
                "batch",
                len(payload["urls"]),
                pdf_max_pages=pdf_max_pages,
                check_prompt_injection=check_prompt_injection,
            )
            return await self._submit_job(
                "batch",
                payload,
                estimate,
                max_list_items=site_max_pages,
                max_content_chars=max_content_chars,
            )
        if action == "crawl":
            pdf_max_pages = plugin.config.get("pdf_max_pages", 20)
            check_prompt_injection = plugin.config.get("check_prompt_injection", False)
            payload = build_crawl_payload(
                url,
                limit=limit,
                include_paths=include_paths,
                exclude_paths=exclude_paths,
                site_max_pages=site_max_pages,
                blocked_domains=blocked_domains,
                pdf_max_pages=pdf_max_pages,
                check_prompt_injection=check_prompt_injection,
            )
            estimate = estimate_site_credits(
                "crawl",
                payload["limit"],
                pdf_max_pages=pdf_max_pages,
                check_prompt_injection=check_prompt_injection,
            )
            return await self._submit_job(
                "crawl",
                payload,
                estimate,
                max_list_items=site_max_pages,
                max_content_chars=max_content_chars,
            )
        payload = build_map_payload(
            url,
            search=search,
            limit=limit,
            site_max_pages=site_max_pages,
            blocked_domains=blocked_domains,
        )
        estimate = estimate_site_credits("map")

        async with plugin.request_scope():
            response = await plugin.executor.execute_sync(
                "map", payload, estimate=estimate
            )

        trimmed = trim_site_response(
            "map",
            response.json(),
            max_list_items=payload["limit"],
            max_content_chars=max_content_chars,
        )
        return ok(trimmed)

    async def _submit_job(
        self,
        action: str,
        payload: dict,
        estimate: int,
        *,
        max_list_items: int,
        max_content_chars: int,
    ) -> str:
        """提交异步任务并封装已受理结果，封装失败时转为 PendingRequestError。"""
        async with self.plugin.request_scope():
            response, accepted = await self.plugin.executor.submit(
                action, payload, estimate=estimate
            )
        handle = make_task_handle(accepted.kind, accepted.job_id, accepted.key_ref)
        try:
            data = trim_site_response(
                action,
                response.json(),
                max_list_items=max_list_items,
                max_content_chars=max_content_chars,
            )
            data["task_handle"] = handle
            return ok(data)
        except Exception as error:
            raise PendingRequestError(
                accepted.reservation_id, task_handle=handle
            ) from error
