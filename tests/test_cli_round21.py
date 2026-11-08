"""第二十一轮 CLI 健壮性测试（无需 API Key、不访问网络）。

覆盖：一次性命令的 stdout 管道安全包装（| head 提前关闭不再 traceback）。
"""
import io
import os
import sys

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

import cli
from utils.console import PipeSafeTextIO, is_pipe_error


def test_onetime_command_wraps_stdout_for_pipe_safety(monkeypatch):
    """一次性命令运行后 stdout 走管道安全包装（列表 / JSON 输出不再打 traceback）。"""
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "argv", ["cli.py", "--version"])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 0
    assert isinstance(sys.stdout, PipeSafeTextIO)
    assert "Aligo" in stream.getvalue()
    # 包装后的写入照常落到底层流
    sys.stdout.write("tail")
    assert stream.getvalue().endswith("tail")
    print("✓ 一次性命令 stdout 走管道安全包装")


def test_is_pipe_error_predicate():
    """管道类错误识别：Errno 22/32 / BrokenPipe / winerror；磁盘错误不吞。"""
    assert is_pipe_error(BrokenPipeError()) is True
    assert is_pipe_error(OSError(22, "invalid argument")) is True
    assert is_pipe_error(OSError(32, "broken pipe")) is True
    disk_full = OSError(28, "no space left on device")
    assert is_pipe_error(disk_full) is False

    class _WinErr(OSError):
        winerror = 109

    assert is_pipe_error(_WinErr(22, "invalid argument")) is True
    print("✓ 管道错误识别（磁盘错误照常抛出）")


if __name__ == "__main__":
    _mp = pytest.MonkeyPatch()
    try:
        test_onetime_command_wraps_stdout_for_pipe_safety(_mp)
        _mp.undo()
        test_is_pipe_error_predicate()
    finally:
        pass
    print("\n全部通过")
