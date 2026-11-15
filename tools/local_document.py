"""MarkItDown 本地文档转换器：文件名校验与离线 Markdown 提取。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from markitdown import MarkItDown

# MarkItDown 0.1.9 内置转换器在本安装真实支持的本地文档扩展名。
SUPPORTED_LOCAL_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".docx",
        ".xlsx",
        ".xls",
        ".pptx",
        ".epub",
        ".csv",
        ".html",
        ".htm",
        ".xhtml",
    }
)


class LocalDocumentError(Exception):
    """本地文档转换失败：消息为固定文案，不携带路径或文件内容。"""


def _validate_filename(filename: str) -> str:
    """校验纯文件名并返回带点的小写扩展名，不支持的类型一律拒绝。"""
    if not isinstance(filename, str) or not filename:
        raise LocalDocumentError("本地文档文件名无效。")
    if "/" in filename or "\\" in filename or "\0" in filename:
        raise LocalDocumentError("本地文档文件名不能包含路径分隔符或 NUL。")
    extension = Path(filename).suffix.lower()
    if extension not in SUPPORTED_LOCAL_EXTENSIONS:
        raise LocalDocumentError("不支持的本地文档类型。")
    return extension


def _load_and_convert(path: Path, extension: str) -> str:
    """在线程中检查文件并执行 MarkItDown 离线转换，返回 Markdown 文本。"""
    if not path.is_file():
        raise LocalDocumentError("本地文档不存在或不是普通文件。")
    converter = MarkItDown(enable_builtins=True, enable_plugins=False)
    result = converter.convert_local(path, file_extension=extension)
    content = result.text_content
    if not isinstance(content, str):
        raise LocalDocumentError("本地文档转换结果不是文本。")
    return content


async def convert_local_document(
    path: Path, filename: str, max_content_chars: int
) -> dict:
    """离线转换本地文档为 Markdown，超长内容按 max_content_chars 截断。"""
    if not isinstance(path, Path):
        raise LocalDocumentError("本地文档路径必须是 Path 对象。")
    extension = _validate_filename(filename)
    try:
        content = await asyncio.to_thread(_load_and_convert, path, extension)
    except asyncio.CancelledError:
        raise
    except LocalDocumentError:
        raise
    except Exception as error:
        raise LocalDocumentError("本地文档转换失败。") from error
    truncated = len(content) > max_content_chars
    if truncated:
        content = content[:max_content_chars]
    payload = {"parser": "markitdown", "sourceFile": filename, "content": content}
    if truncated:
        payload["truncated"] = True
    return payload
