"""tools.attachments 测试：File 组件选择、元数据读取与错误路径，全程零网络。"""

from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from astrbot.api.message_components import File, Reply
from astrbot_plugin_firecrawl.tools import attachments
from astrbot_plugin_firecrawl.tools.attachments import (
    AttachmentError,
    EventAttachment,
    select_event_attachment,
)


class _SpyFile(File):
    """记录 get_file 调用次数的 File 替身。"""

    def __init__(self, name: str, path: str) -> None:
        self.name = name
        self.path = path
        self.calls = 0

    async def get_file(self) -> str:
        self.calls += 1
        return self.path


def _event(*components: Any) -> SimpleNamespace:
    """构造带当前消息列表的最小事件替身。"""
    return SimpleNamespace(message_obj=SimpleNamespace(message=list(components)))


def test_attachment_is_frozen():
    """EventAttachment 不可变，字段只读。"""
    attachment = EventAttachment(filename="a.txt", path=Path("/tmp/a.txt"), size=3)
    with pytest.raises(FrozenInstanceError):
        attachment.size = 4


def test_collection_order_and_depth():
    """当前消息文件在前且保持顺序，Reply.chain 文件随后，嵌套 Reply 不收集。"""
    current_first = _SpyFile("b.txt", "")
    current_second = _SpyFile("a.txt", "")
    reply_one_file = _SpyFile("c.txt", "")
    reply_two_file = _SpyFile("e.txt", "")
    nested = _SpyFile("nested.txt", "")
    event = _event(
        current_first,
        Reply(chain=[reply_one_file, Reply(chain=[nested])]),
        current_second,
        Reply(chain=[reply_two_file]),
    )
    names = [item.name for item in attachments._collect_file_components(event)]
    assert names == ["b.txt", "a.txt", "c.txt", "e.txt"]


@pytest.mark.asyncio
async def test_reply_with_none_chain_is_skipped(tmp_path):
    """Reply.chain 为 None 时跳过该引用，其余附件照常选择且顺序不变。"""
    current = _SpyFile("b.txt", str(tmp_path / "b.txt"))
    reply_file = _SpyFile("c.txt", str(tmp_path / "c.txt"))
    (tmp_path / "c.txt").write_text("c\n", encoding="utf-8")
    event = _event(
        current,
        Reply(chain=None),
        Reply(chain=[reply_file]),
    )
    attachment = await select_event_attachment(event, file_name="c.txt")
    assert attachment.filename == "c.txt"
    assert reply_file.calls == 1
    assert current.calls == 0
    names = [item.name for item in attachments._collect_file_components(event)]
    assert names == ["b.txt", "c.txt"]


@pytest.mark.asyncio
async def test_selects_single_current_file(tmp_path):
    """未指定文件名时自动选中唯一当前文件，并返回真实元数据。"""
    target = tmp_path / "报告.pdf"
    target.write_bytes(b"abcdef")
    component = _SpyFile("报告.pdf", str(target))
    attachment = await select_event_attachment(_event(component))
    assert attachment.filename == "报告.pdf"
    assert attachment.path == target
    assert attachment.size == 6
    assert component.calls == 1


@pytest.mark.asyncio
async def test_selects_reply_file_without_touching_current(tmp_path):
    """引用消息中的文件可按名选中，当前消息文件保持未下载。"""
    reply_target = tmp_path / "data.csv"
    reply_target.write_text("a,b\n", encoding="utf-8")
    current = _SpyFile("notes.txt", str(tmp_path / "notes.txt"))
    reply_file = _SpyFile("data.csv", str(reply_target))
    event = _event(current, Reply(chain=[reply_file]))
    attachment = await select_event_attachment(event, file_name="data.csv")
    assert attachment.filename == "data.csv"
    assert attachment.path == reply_target
    assert attachment.size == reply_target.stat().st_size
    assert reply_file.calls == 1
    assert current.calls == 0


@pytest.mark.asyncio
async def test_selects_by_trimmed_name_only_downloads_selected(tmp_path):
    """file_name 去除首尾空白后精确匹配，且只下载选中的组件。"""
    target = tmp_path / "doc.pdf"
    target.write_bytes(b"pdf")
    component = _SpyFile("doc.pdf", str(target))
    sibling = _SpyFile("other.pdf", str(tmp_path / "other.pdf"))
    attachment = await select_event_attachment(
        _event(component, sibling), file_name="  doc.pdf  "
    )
    assert attachment.path == target
    assert component.calls == 1
    assert sibling.calls == 0


@pytest.mark.asyncio
async def test_rejects_empty_event():
    """当前与引用消息都没有文件时明确拒绝。"""
    with pytest.raises(AttachmentError):
        await select_event_attachment(_event())


@pytest.mark.asyncio
async def test_rejects_multiple_without_name(tmp_path):
    """多个附件且未指定文件名时拒绝，且不下载任何文件。"""
    one = _SpyFile("one.txt", str(tmp_path / "one.txt"))
    two = _SpyFile("two.txt", str(tmp_path / "two.txt"))
    with pytest.raises(AttachmentError):
        await select_event_attachment(_event(one, two))
    assert one.calls == 0
    assert two.calls == 0


@pytest.mark.asyncio
async def test_rejects_ambiguous_same_name(tmp_path):
    """同名附件即使指定文件名也拒绝。"""
    first = _SpyFile("same.txt", str(tmp_path / "first.txt"))
    second = _SpyFile("same.txt", str(tmp_path / "second.txt"))
    with pytest.raises(AttachmentError):
        await select_event_attachment(_event(first, second), file_name="same.txt")
    assert first.calls == 0
    assert second.calls == 0


@pytest.mark.asyncio
async def test_rejects_unknown_name_and_local_path(tmp_path):
    """未知文件名与模型传入的本地路径都拒绝，不触发下载。"""
    component = _SpyFile("doc.pdf", str(tmp_path / "doc.pdf"))
    for name in ("missing.pdf", "/etc/passwd", "C:\\tmp\\secret.txt"):
        with pytest.raises(AttachmentError):
            await select_event_attachment(_event(component), file_name=name)
    assert component.calls == 0


@pytest.mark.asyncio
async def test_get_file_failure_wraps_fixed_message(tmp_path):
    """get_file 异常转为固定文案，不回显组件名与原始错误。"""

    class _BrokenFile(_SpyFile):
        async def get_file(self) -> str:
            self.calls += 1
            raise RuntimeError("secret disk error for /tmp/xyz")

    component = _BrokenFile("doc.pdf", str(tmp_path / "doc.pdf"))
    with pytest.raises(AttachmentError) as excinfo:
        await select_event_attachment(_event(component))
    message = str(excinfo.value)
    assert "doc.pdf" not in message
    assert "secret" not in message
    assert isinstance(excinfo.value.__cause__, RuntimeError)


@pytest.mark.asyncio
async def test_missing_file_stat_wraps_fixed_message(tmp_path):
    """get_file 返回的路径不存在时同样收敛为固定文案。"""
    component = _SpyFile("doc.pdf", str(tmp_path / "missing.pdf"))
    with pytest.raises(AttachmentError) as excinfo:
        await select_event_attachment(_event(component))
    assert "missing.pdf" not in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, OSError)


@pytest.mark.asyncio
async def test_rejects_non_regular_file_target(tmp_path):
    """get_file 返回目录等非普通文件路径时拒绝。"""
    component = _SpyFile("dir.txt", str(tmp_path))
    with pytest.raises(AttachmentError):
        await select_event_attachment(_event(component))


@pytest.mark.asyncio
async def test_cancellation_from_get_file_propagates():
    """get_file 取消时 CancelledError 原样传播。"""

    class _CancellingFile(_SpyFile):
        async def get_file(self) -> str:
            self.calls += 1
            raise asyncio.CancelledError()

    component = _CancellingFile("doc.pdf", "unused")
    with pytest.raises(asyncio.CancelledError):
        await select_event_attachment(_event(component))


@pytest.mark.asyncio
async def test_event_owned_file_is_kept(tmp_path):
    """选中附件只读取元数据，AstrBot 拥有的本地文件保持原样。"""
    target = tmp_path / "keep.txt"
    target.write_bytes(b"keep")
    component = _SpyFile("keep.txt", str(target))
    attachment = await select_event_attachment(_event(component))
    assert target.read_bytes() == b"keep"
    assert attachment.path == target


def test_loader_restores_planted_components_module(sdk_tool_loader, monkeypatch):
    """loader 退出后精确还原预先存在的 message_components 模块。"""
    fake = types.ModuleType("astrbot.api.message_components")
    monkeypatch.setitem(sys.modules, "astrbot.api.message_components", fake)
    with sdk_tool_loader("attachments", "select_event_attachment") as select:
        assert callable(select)
    assert sys.modules["astrbot.api.message_components"] is fake


def test_loader_uninstalls_components_stub_on_exit(sdk_tool_loader, monkeypatch):
    """message_components 原本缺失时，loader 退出后恢复缺失状态。"""
    monkeypatch.delitem(sys.modules, "astrbot.api.message_components", raising=False)
    with sdk_tool_loader("attachments", "select_event_attachment") as select:
        assert callable(select)
    assert "astrbot.api.message_components" not in sys.modules
