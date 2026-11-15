"""firecrawl_parse FunctionTool：事件附件选择、云端解析与本地回退路由。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import FunctionTool, logger

from ..firecrawl_api.errors import ErrorCategory, FirecrawlError
from ..firecrawl_api.parse import (
    MAX_PARSE_FILE_BYTES,
    build_parse_options,
    estimate_parse_credits,
    normalize_parse_file,
    trim_parse_response,
)
from .attachments import AttachmentError, select_event_attachment
from .execution import PendingRequestError
from .local_document import (
    SUPPORTED_LOCAL_EXTENSIONS,
    LocalDocumentError,
    convert_local_document,
)
from .result import fail, ok
from .schema import object_schema

_PLUGIN_UNAVAILABLE = FirecrawlError(
    ErrorCategory.CONFIG, "插件未初始化，无法调用 firecrawl_parse。"
)

_TOOL_DESCRIPTION = "将当前或引用消息中的文档转为 Markdown 供当前 LLM 回答。"

_NO_CLOUD_WARNING = "Firecrawl 云端不可用，已本地解析。"
_CLOUD_FAILED_WARNING = "云端解析失败，已本地解析。"
_SETTLED_INVALID_WARNING = "云端响应无效，已本地解析。"
_PENDING_LOCAL_WARNING = "云端结果不确定，已本地解析；管理员需核对原预留。"

_OVERSIZE_UNSUPPORTED_TEXT = (
    f"文件大小超过上传上限 {MAX_PARSE_FILE_BYTES} 字节，且该文件类型不支持本地解析。"
)
_NO_CLOUD_UNSUPPORTED_TEXT = "尚未配置 Firecrawl API Key，且该文件类型不支持本地解析。"
_SETTLED_INVALID_TEXT = "云端已结算但返回的解析结果无效，重新调用会再次计费。"
_PENDING_LOCAL_HINT = "本地回退解析失败；请勿重发该请求，请管理员核对原预留。"


def _parse_parameters() -> dict:
    """每次构建全新 schema，保证实例之间互不共享。"""
    return object_schema(
        {
            "file_name": {
                "type": "string",
                "description": (
                    "可选。当前或引用消息中要解析的文件名，"
                    "不要传本地路径、网址或问题文本；省略时自动选用唯一附件。"
                ),
            },
        }
    )


def _extract_event(context: Any) -> Any:
    """从工具上下文提取当前会话事件；缺失时返回 None。"""
    return getattr(getattr(context, "context", None), "event", None)


@dataclass
class FirecrawlParseTool(FunctionTool):
    """firecrawl_parse 工具：未注入 plugin 时零 I/O 返回 config 错误。"""

    plugin: Any = None
    name: str = "firecrawl_parse"
    description: str = _TOOL_DESCRIPTION
    parameters: dict = field(default_factory=_parse_parameters)

    def __repr__(self) -> str:
        return (
            f"FuncTool(name={self.name}, parameters={self.parameters}, "
            f"description={self.description})"
        )

    async def call(
        self,
        context: Any,
        file_name: str = "",
        **unexpected: Any,
    ) -> str:
        """先定位会话事件，再在 request_scope 内完成附件选择、云端解析与本地回退。"""
        if self.plugin is None:
            return fail(_PLUGIN_UNAVAILABLE)
        try:
            if unexpected:
                raise ValueError("不支持的解析参数。")
            if not isinstance(file_name, str):
                raise ValueError("file_name 必须是字符串。")
            event = _extract_event(context)
            if event is None:
                raise ValueError("当前没有可用的会话事件，无法读取文件附件。")
            return await self._run(event, file_name)
        except (AttachmentError, LocalDocumentError) as error:
            # 附件选择与本地转换失败属于输入问题，按 usage 收敛固定文案。
            return fail(ValueError(str(error)))
        except Exception as error:
            if isinstance(error, (FirecrawlError, ValueError, PendingRequestError)):
                return fail(error)
            logger.warning(f"firecrawl_parse 未知异常：{type(error).__name__}")
            return fail(ValueError("文件解析失败。"))

    async def _run(self, event: Any, file_name: str) -> str:
        plugin = self.plugin
        max_content_chars = plugin.config.get("max_content_chars", 8000)
        pdf_max_pages = plugin.config.get("pdf_max_pages", 20)
        check_prompt_injection = plugin.config.get("check_prompt_injection", False)

        if (
            isinstance(max_content_chars, bool)
            or not isinstance(max_content_chars, int)
            or max_content_chars < 1
        ):
            raise ValueError("max_content_chars 必须是 >= 1 的整数。")
        async with plugin.request_scope():
            attachment = await select_event_attachment(event, file_name)
            extension = Path(attachment.filename).suffix.lower()
            local_supported = extension in SUPPORTED_LOCAL_EXTENSIONS
            if attachment.size > MAX_PARSE_FILE_BYTES:
                if not local_supported:
                    raise ValueError(_OVERSIZE_UNSUPPORTED_TEXT)
                return ok(await self._convert_locally(attachment, max_content_chars))

            info = normalize_parse_file(attachment.filename, attachment.size)
            if not self._cloud_ready():
                if not local_supported:
                    raise ValueError(_NO_CLOUD_UNSUPPORTED_TEXT)
                payload = await self._convert_locally(attachment, max_content_chars)
                payload["warning"] = _NO_CLOUD_WARNING
                return ok(payload)

            options = build_parse_options(
                pdf_max_pages=pdf_max_pages,
                check_prompt_injection=check_prompt_injection,
            )
            estimate = estimate_parse_credits(
                is_pdf=info["is_pdf"],
                pdf_max_pages=pdf_max_pages,
                check_prompt_injection=check_prompt_injection,
            )
            content = await self._read_attachment(attachment)
            try:
                response = await plugin.executor.execute_parse(
                    filename=attachment.filename,
                    content=content,
                    options=options,
                    estimate=estimate,
                )
            except PendingRequestError as error:
                return await self._fallback_or_pending(
                    attachment, local_supported, max_content_chars, error
                )
            except FirecrawlError as error:
                return await self._fallback_or_raise(
                    attachment,
                    local_supported,
                    max_content_chars,
                    error,
                    _CLOUD_FAILED_WARNING,
                )

            try:
                trimmed = trim_parse_response(
                    response.json(), max_content_chars=max_content_chars
                )
            except ValueError:
                logger.warning("firecrawl_parse 云端响应投影失败：ValueError")
                if not local_supported:
                    raise ValueError(_SETTLED_INVALID_TEXT) from None
                try:
                    payload = await self._convert_locally(attachment, max_content_chars)
                except LocalDocumentError as error:
                    logger.warning(
                        f"firecrawl_parse 本地回退失败：{type(error).__name__}"
                    )
                    raise ValueError(_SETTLED_INVALID_TEXT) from error
                payload["warning"] = _SETTLED_INVALID_WARNING
                return ok(payload)

            trimmed["parser"] = "firecrawl"
            trimmed["sourceFile"] = attachment.filename
            return ok(trimmed)

    async def _fallback_or_pending(
        self,
        attachment: Any,
        local_supported: bool,
        max_content_chars: int,
        error: PendingRequestError,
    ) -> str:
        """pending 时本地回退一次；成功保留 pending 语义，失败仍按 pending 返回。"""
        if not local_supported:
            return fail(error)
        try:
            payload = await self._convert_locally(attachment, max_content_chars)
        except LocalDocumentError as local_error:
            logger.warning(
                f"firecrawl_parse 本地回退失败：{type(local_error).__name__}"
            )
            return fail(error, hint=_PENDING_LOCAL_HINT)
        payload["cloud_pending"] = True
        payload["reservation_id"] = error.reservation_id
        payload["warning"] = _PENDING_LOCAL_WARNING
        return ok(payload)

    async def _fallback_or_raise(
        self,
        attachment: Any,
        local_supported: bool,
        max_content_chars: int,
        error: FirecrawlError,
        warning: str,
    ) -> str:
        """云端失败时本地回退一次；本地也失败则保留原错误类别。"""
        if not local_supported:
            raise error
        try:
            payload = await self._convert_locally(attachment, max_content_chars)
        except LocalDocumentError as local_error:
            logger.warning(
                f"firecrawl_parse 本地回退失败：{type(local_error).__name__}"
            )
            raise error from local_error
        payload["warning"] = warning
        return ok(payload)

    async def _convert_locally(self, attachment: Any, max_content_chars: int) -> dict:
        """本地离线转换附件为 markitdown 结果载荷。"""
        return await convert_local_document(
            attachment.path, attachment.filename, max_content_chars
        )

    async def _read_attachment(self, attachment: Any) -> bytes:
        """在线程中读取附件字节；失败收敛为固定 AttachmentError，不回显路径。"""
        try:
            return await asyncio.to_thread(attachment.path.read_bytes)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise AttachmentError("读取附件文件失败。") from error

    def _cloud_ready(self) -> bool:
        """executor 与非空 Key 池同时就绪才允许云端解析。"""
        pool = getattr(self.plugin, "key_pool", None)
        return (
            self.plugin.executor is not None and pool is not None and pool.key_count > 0
        )
