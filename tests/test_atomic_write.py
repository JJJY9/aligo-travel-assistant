"""utils.atomic_write 测试（无需 API Key、不访问网络）。

覆盖：原子替换成功、编码失败时旧文件保持不变且临时文件被清理、
Windows 共享冲突的有界重试、目录 fsync（POSIX）与陈旧临时文件清理。

运行：python tests/test_atomic_write.py
"""
import os
import stat
import sys

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.atomic_write import (
    cleanup_stale_temp_files,
    fsync_parent_dir,
    replace_with_retry,
    write_bytes_atomic,
    write_text_atomic,
)


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


def test_replace_retries_transient_permission_error(monkeypatch, tmp_path):
    """Windows 目标被短暂占用：前两次 PermissionError 后应重试成功。"""
    import utils.atomic_write as aw

    real_replace = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError("target busy")
        return real_replace(src, dst)

    monkeypatch.setattr(aw.os, "replace", flaky)
    target = tmp_path / "backup.json"
    write_text_atomic(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
    assert calls["n"] == 3, "前两次冲突应重试而不是直接失败"
    assert [path.name for path in tmp_path.iterdir()] == ["backup.json"]
    print("✓ replace 瞬时冲突有界重试后成功")


def test_replace_with_retry_gives_up(monkeypatch, tmp_path):
    """持续冲突：超时后抛出，临时文件由调用方清理逻辑回收。"""
    import utils.atomic_write as aw

    src = tmp_path / "payload.tmp"
    src.write_text("x", encoding="utf-8")
    dst = tmp_path / "payload"

    def always_busy(*args, **kwargs):
        raise PermissionError("target busy")

    monkeypatch.setattr(aw.os, "replace", always_busy)
    monkeypatch.setattr(aw.time, "sleep", lambda _seconds: None)
    with pytest.raises(PermissionError):
        replace_with_retry(src, dst, timeout=0.05)
    assert not dst.exists()
    print("✓ replace 持续冲突超时后上抛")


@pytest.mark.skipif(os.name != "posix", reason="目录 fsync 仅 POSIX 生效")
def test_write_text_atomic_fsyncs_parent_dir(monkeypatch, tmp_path):
    """replace 之后必须 fsync 父目录（rename 元数据掉电后可能丢失）。"""
    import utils.atomic_write as aw

    real_fsync = os.fsync
    dir_syncs = {"n": 0}

    def recording_fsync(fd):
        try:
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                dir_syncs["n"] += 1
        finally:
            return real_fsync(fd)

    monkeypatch.setattr(aw.os, "fsync", recording_fsync)
    write_text_atomic(tmp_path / "backup.json", "new")
    assert dir_syncs["n"] >= 1, "replace 后应有目录 fsync"
    print("✓ 原子写后 fsync 父目录")


def test_fsync_parent_dir_is_noop_on_windows(tmp_path):
    """Windows 不能以普通方式打开目录：helper 必须静默跳过而不是抛异常。"""
    fsync_parent_dir(tmp_path / "missing.txt")
    print("✓ fsync_parent_dir 平台降级不抛异常")


def test_write_text_atomic_cleans_stale_temp_files(tmp_path):
    """崩溃遗留的旧临时文件被清掉；活跃 / 其它前缀的临时文件保留。"""
    old = tmp_path / ".backup.json.deadbeef.tmp"
    old.write_text("stale snapshot", encoding="utf-8")
    os.utime(old, (1_000_000.0, 1_000_000.0))
    fresh = tmp_path / ".backup.json.cafebabe.tmp"
    fresh.write_text("active writer", encoding="utf-8")
    other = tmp_path / ".other.json.deadbeef.tmp"
    other.write_text("other target", encoding="utf-8")
    os.utime(other, (1_000_000.0, 1_000_000.0))

    write_text_atomic(tmp_path / "backup.json", "new")

    assert not old.exists(), "超过阈值的同前缀临时文件应被清理"
    assert fresh.exists(), "新鲜临时文件可能是活跃写者，不能删"
    assert other.exists(), "不同目标前缀的临时文件不能删"


def test_cleanup_stale_temp_files_swallows_missing_dir(tmp_path):
    assert cleanup_stale_temp_files(tmp_path / "nope", ".x.") == 0
