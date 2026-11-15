"""事件附件选择：从当前或引用消息定位 File 组件并读取本地元数据。"""

from __future__ import annotations

import asyncio
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api.message_components import File, Reply


class AttachmentError(Exception):
    """附件选择或读取失败：消息为固定文案，不携带路径或文件内容。"""


@dataclass(frozen=True)
class EventAttachment:
    """选中的事件附件：组件文件名、本地路径与字节大小。"""

    filename: str
    path: Path
    size: int


def _collect_file_components(event: Any) -> list:
    """按当前消息顺序、再按引用 chain 顺序收集 File 组件，不进入更深层 Reply。"""
    components = event.message_obj.message
    files = [item for item in components if isinstance(item, File)]
    for item in components:
        if isinstance(item, Reply) and item.chain:
            files.extend(sub for sub in item.chain if isinstance(sub, File))
    return files


def _select_component(files: list, file_name: str) -> Any:
    """按文件名精确选择唯一附件；未指定且仅一个附件时自动选中。"""
    if not files:
        raise AttachmentError("当前消息和引用消息中都没有文件附件。")
    wanted = file_name.strip()
    if not wanted:
        if len(files) > 1:
            raise AttachmentError("消息中有多个文件附件，请指定要使用的文件名。")
        return files[0]
    matches = [item for item in files if item.name == wanted]
    if not matches:
        raise AttachmentError("消息中没有与指定文件名匹配的附件。")
    if len(matches) > 1:
        raise AttachmentError("消息中有多个同名文件附件，无法确定要使用的附件。")
    return matches[0]


def _stat_size(path: Path) -> int:
    """确认普通文件并返回字节大小。"""
    stat_result = path.stat()
    if not stat.S_ISREG(stat_result.st_mode):
        raise AttachmentError("附件不是可读取的普通文件。")
    return stat_result.st_size


async def select_event_attachment(event: Any, file_name: str = "") -> EventAttachment:
    """从事件消息选择一个文件附件，仅对选中组件下载并返回名称、路径与大小。"""
    component = _select_component(_collect_file_components(event), file_name)
    try:
        path = Path(str(await component.get_file()))
    except asyncio.CancelledError:
        raise
    except AttachmentError:
        raise
    except Exception as error:
        raise AttachmentError("读取附件文件失败。") from error
    try:
        size = await asyncio.to_thread(_stat_size, path)
    except asyncio.CancelledError:
        raise
    except AttachmentError:
        raise
    except Exception as error:
        raise AttachmentError("读取附件文件信息失败。") from error
    return EventAttachment(filename=str(component.name), path=path, size=size)
