"""firecrawl_agent FunctionTool：仅管理员提交、查询与取消 Agent 后台研究任务。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from astrbot.api import FunctionTool, logger

from ..firecrawl_api.agent import build_agent_payload, estimate_agent_credits
from ..firecrawl_api.errors import ErrorCategory, FirecrawlError
from ..firecrawl_api.tasks import parse_task_handle
from .execution import PendingRequestError
from .result import fail, ok
from .schema import object_schema

_PLUGIN_UNAVAILABLE = FirecrawlError(
    ErrorCategory.CONFIG, "插件未初始化，无法调用 firecrawl_agent。"
)
_SERVICE_UNAVAILABLE = FirecrawlError(
    ErrorCategory.CONFIG, "Agent 服务不可用，无法调用 firecrawl_agent。"
)

_ADMIN_ONLY = "firecrawl_agent 仅管理员可用。"
_MISSING_UMO = "缺少会话来源，无法启动 Agent 任务。"
_BAD_KIND = "task_handle 必须是 agent 任务句柄。"
_BAD_ACTION = "action 只允许 start、status、cancel。"
_UNKNOWN_FIELDS = "不支持的 Agent 参数。"
_UNKNOWN_FAILURE = "Agent 任务操作失败。"

_TOOL_DESCRIPTION = "仅管理员使用的 Agent 后台研究任务。"


def _agent_parameters() -> dict:
    """每次构建全新 schema，保证实例之间互不共享。"""
    return object_schema(
        {
            "action": {
                "type": "string",
                "enum": ["start", "status", "cancel"],
                "description": (
                    "必填。start 提交后台研究任务；status 查询任务状态；"
                    "cancel 请求取消后查询最终状态。仅管理员可用。"
                ),
            },
            "prompt": {
                "type": "string",
                "description": "仅 start 必填。研究任务提示词。",
            },
            "max_credits": {
                "type": "integer",
                "description": "start 必填。本次任务费用上界，范围为 1–2500。",
            },
            "task_handle": {
                "type": "string",
                "description": (
                    "仅 status/cancel 必填。start 返回的完整 agent 任务句柄。"
                ),
            },
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "仅 start 可选。限制 Agent 访问的 http/https 地址列表。",
            },
            "schema": {
                "type": "object",
                "description": "仅 start 可选。约束 Agent 输出结构的 JSON 对象。",
            },
            "strict_constrain_to_urls": {
                "type": "boolean",
                "description": (
                    "仅 start 可选。为 true 时必须提供 urls，并强制 Agent 只使用这些地址。"
                ),
            },
        },
        required=["action"],
    )


def _extract_event(context: Any) -> Any:
    """从工具上下文提取当前会话事件；缺失时返回 None。"""
    return getattr(getattr(context, "context", None), "event", None)


def _require_admin(context: Any) -> Any:
    """要求当前事件存在且 is_admin() 为 True；缺少该方法视为无权限。"""
    event = _extract_event(context)
    is_admin = getattr(event, "is_admin", None)
    if event is None or not callable(is_admin) or is_admin() is not True:
        raise ValueError(_ADMIN_ONLY)
    return event


def _require_agent_handle(task_handle: str) -> str:
    """校验句柄为 agent 类型；错误不回显句柄内容。"""
    parsed = parse_task_handle(task_handle)
    if parsed["kind"] != "agent":
        raise ValueError(_BAD_KIND)
    return task_handle


@dataclass
class FirecrawlAgentTool(FunctionTool):
    """firecrawl_agent 工具：未注入 plugin 或 Agent 服务时零 I/O 拒绝。"""

    plugin: Any = None
    name: str = "firecrawl_agent"
    description: str = _TOOL_DESCRIPTION
    parameters: dict = field(default_factory=_agent_parameters)

    def __repr__(self) -> str:
        return (
            f"FuncTool(name={self.name}, parameters={self.parameters}, "
            f"description={self.description})"
        )

    async def call(
        self,
        context: Any,
        action: str | None = None,
        prompt: str = "",
        max_credits: int | None = None,
        task_handle: str = "",
        urls: list[str] | None = None,
        schema: dict | None = None,
        strict_constrain_to_urls: bool = False,
        **unexpected: Any,
    ) -> str:
        """先校验管理员，再按 action 提交、查询或取消；异常统一收敛为 JSON。"""
        try:
            event = _require_admin(context)
            if self.plugin is None:
                return fail(_PLUGIN_UNAVAILABLE)
            if unexpected:
                raise ValueError(_UNKNOWN_FIELDS)
            service = getattr(self.plugin, "agent_service", None)
            if service is None:
                return fail(_SERVICE_UNAVAILABLE)
            if action == "start":
                return await self._start(
                    service,
                    event,
                    prompt=prompt,
                    max_credits=max_credits,
                    urls=urls,
                    schema=schema,
                    strict_constrain_to_urls=strict_constrain_to_urls,
                )
            if action in ("status", "cancel"):
                handle = _require_agent_handle(task_handle)
                if action == "status":
                    trimmed = await service.query(handle)
                    payload = dict(trimmed)
                    payload["task_handle"] = handle
                    return ok(payload)
                return ok(await service.cancel(handle))
            raise ValueError(_BAD_ACTION)
        except Exception as error:
            if isinstance(error, (FirecrawlError, ValueError, PendingRequestError)):
                return fail(error)
            logger.warning(f"firecrawl_agent 未知异常：{type(error).__name__}")
            return fail(ValueError(_UNKNOWN_FAILURE))

    async def _start(
        self,
        service: Any,
        event: Any,
        *,
        prompt: str,
        max_credits: int | None,
        urls: list[str] | None,
        schema: dict | None,
        strict_constrain_to_urls: bool,
    ) -> str:
        """组装 payload 与费用上界后提交，立即返回句柄；后台由服务 track。"""
        raw_umo = getattr(event, "unified_msg_origin", None)
        if not isinstance(raw_umo, str):
            raise ValueError(_MISSING_UMO)
        umo = raw_umo.strip()
        if not umo:
            raise ValueError(_MISSING_UMO)
        plugin = self.plugin
        payload = build_agent_payload(
            prompt=prompt,
            max_credits=max_credits,
            urls=urls,
            schema=schema,
            strict_constrain_to_urls=strict_constrain_to_urls,
            blocked_domains=plugin.config.get("blocked_domains", []),
        )
        estimate = estimate_agent_credits(payload["maxCredits"])
        return ok(await service.submit(payload=payload, estimate=estimate, umo=umo))
