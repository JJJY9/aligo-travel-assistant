"""utils.atomic_write 测试（无需 API Key、不访问网络）。

覆盖：原子替换成功、编码失败时旧文件保持不变且临时文件被清理。

运行：python tests/test_atomic_write.py
"""
import os
import sys

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.atomic_write import write_bytes_atomic, write_text_atomic


def test_write_text_atomic_replaces_and_preserves_on_failure(tmp_path):
    """编码失败 / 写入异常时旧备份保持原样，临时文件不残留。"""
    target = tmp_path / "backup.json"
    target.write_text("old", encoding="utf-8")

    write_text_atomic(target, "new")
    assert target.read_text(encoding="utf-8") == "new"

    with pytest.raises(UnicodeEncodeError):
        write_text_atomic(target, "bad \ud800")
    assert target.read_text(encoding="utf-8") == "new"
    assert [path.name for path in tmp_path.iterdir()] == ["backup.json"]
    print("✓ 文本原子写入：失败保留旧文件并清理临时文件")


def test_write_bytes_atomic_roundtrip(tmp_path):
    target = tmp_path / "sub" / "calendar.ics"
    write_bytes_atomic(target, b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")
    assert target.read_bytes() == b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
    print("✓ 二进制原子写入（自动创建目录、保留 CRLF）")
