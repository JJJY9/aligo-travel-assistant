"""utils.atomic_write 测试（无需 API Key、不访问网络）。

覆盖：原子替换成功、编码失败时旧文件保持不变且临时文件被清理、
Windows 共享冲突的有界重试、目录 fsync（POSIX）与陈旧临时文件清理。

运行：python tests/test_atomic_write.py
"""
import errno
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


def test_cleanup_stale_temp_files_keeps_non_tmp_same_prefix(tmp_path):
    """同前缀但不是本模块临时文件（无 .tmp 后缀）的用户文件不能被误删。"""
    user_copy = tmp_path / ".backup.json.keep"
    user_copy.write_text("user hand-made copy", encoding="utf-8")
    os.utime(user_copy, (1_000_000.0, 1_000_000.0))
    user_bak = tmp_path / ".backup.json.bak"
    user_bak.write_text("user note", encoding="utf-8")
    os.utime(user_bak, (1_000_000.0, 1_000_000.0))
    stale_tmp = tmp_path / ".backup.json.deadbeef.tmp"
    stale_tmp.write_text("crashed writer", encoding="utf-8")
    os.utime(stale_tmp, (1_000_000.0, 1_000_000.0))

    write_text_atomic(tmp_path / "backup.json", "new")

    assert user_copy.exists(), "非 .tmp 的同前缀用户文件必须保留"
    assert user_bak.exists(), "非 .tmp 的同前缀用户文件必须保留"
    assert not stale_tmp.exists(), "旧格式 .tmp 遗留仍要清理"


def test_cleanup_stale_temp_files_cleans_hashed_prefix(tmp_path):
    """新哈希前缀的遗留临时文件被清理；其它目标 / 新鲜文件保留。"""
    import utils.atomic_write as aw

    target = tmp_path / "backup.json"
    prefix = aw._temp_prefix(target)
    stale = tmp_path / f"{prefix}deadbeef.tmp"
    stale.write_text("crashed writer", encoding="utf-8")
    os.utime(stale, (1_000_000.0, 1_000_000.0))
    fresh = tmp_path / f"{prefix}cafebabe.tmp"
    fresh.write_text("active writer", encoding="utf-8")
    other = tmp_path / f"{aw._temp_prefix(tmp_path / 'other.json')}deadbeef.tmp"
    other.write_text("other target", encoding="utf-8")
    os.utime(other, (1_000_000.0, 1_000_000.0))

    write_text_atomic(target, "new")

    assert not stale.exists(), "超过阈值的本目标临时文件应被清理"
    assert fresh.exists(), "新鲜临时文件可能是活跃写者，不能删"
    assert other.exists(), "不同目标前缀的临时文件不能删"


def test_temp_prefix_length_is_independent_of_target_name(tmp_path):
    """临时前缀用哈希：超长目标名不会把临时文件名推过 NAME_MAX。"""
    import utils.atomic_write as aw

    short = aw._temp_prefix(tmp_path / "a.json")
    long = aw._temp_prefix(tmp_path / ("a" * 250))
    assert len(short) == len(long) <= 31


@pytest.mark.skipif(os.name != "posix", reason="NAME_MAX 仅在 POSIX 可稳定复现")
def test_write_atomic_long_target_name_fits_name_max(tmp_path):
    """目标名接近 NAME_MAX 时原子写仍成功（临时名不能比目标名更长）。"""
    limit = os.pathconf(tmp_path, "PC_NAME_MAX")
    name = "a" * (limit - 5)
    target = tmp_path / name

    write_text_atomic(target, "text")
    assert target.read_text(encoding="utf-8") == "text"
    write_bytes_atomic(target, b"bytes")
    assert target.read_bytes() == b"bytes"
    assert [path.name for path in tmp_path.iterdir()] == [name]


def test_replace_does_not_retry_permanent_error(monkeypatch, tmp_path):
    """ENOENT / ENOSPC 等永久错误立即抛出，不再空转重试预算。"""
    import utils.atomic_write as aw

    src = tmp_path / "payload.tmp"
    src.write_text("x", encoding="utf-8")
    dst = tmp_path / "payload"
    calls = {"n": 0}
    sleeps = {"n": 0}

    def missing(*args, **kwargs):
        calls["n"] += 1
        raise FileNotFoundError(errno.ENOENT, "gone")

    monkeypatch.setattr(aw.os, "replace", missing)
    monkeypatch.setattr(aw.time, "sleep", lambda _seconds: sleeps.__setitem__("n", sleeps["n"] + 1))
    with pytest.raises(FileNotFoundError):
        replace_with_retry(src, dst)
    assert calls["n"] == 1, "永久错误不应重试"
    assert sleeps["n"] == 0, "永久错误不应进入 sleep"


def test_replace_retries_oserror_without_errno(monkeypatch, tmp_path):
    """无 errno 的裸 OSError 仍按瞬时冲突重试（与 llm_resilience 同口径）。"""
    import utils.atomic_write as aw

    src = tmp_path / "payload.tmp"
    src.write_text("x", encoding="utf-8")
    dst = tmp_path / "payload"
    real_replace = os.replace
    calls = {"n": 0}

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise OSError("transient")
        return real_replace(a, b)

    monkeypatch.setattr(aw.os, "replace", flaky)
    replace_with_retry(src, dst)
    assert dst.read_text(encoding="utf-8") == "x"
    assert calls["n"] == 3
