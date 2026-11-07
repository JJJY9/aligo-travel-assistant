"""configure_stdio / stdin 自动解码测试（无需 API Key、不访问网络）。

运行：python tests/test_console.py
"""
import io
import os
import subprocess
import sys

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.console import configure_stdio


def test_idle_pipe_does_not_block_startup():
    """已连接但暂时没有数据的管道不能让 configure_stdio 永久阻塞。"""
    script = (
        "import sys; sys.path.insert(0, r'%s'); "
        "from utils.console import configure_stdio; configure_stdio(); print('ok')"
        % project_root
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=project_root,
    )
    try:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise AssertionError("configure_stdio 在无数据的空闲管道上阻塞") from None
    finally:
        if proc.stdin:
            proc.stdin.close()
    output = (proc.stdout.read() + proc.stderr.read()).decode("utf-8", errors="replace")
    assert proc.returncode == 0, output
    assert "ok" in output
    print("✓ 空闲管道不会阻塞 configure_stdio")


class _PipeBuffer(io.BytesIO):
    def peek(self, size=2):
        position = self.tell()
        data = self.read(size)
        self.seek(position)
        return data


def _decode_all(payload: bytes, fallback: str = "gbk") -> str:
    from utils.console import _AutoDecodeInput

    shim = _AutoDecodeInput(_PipeBuffer(payload), fallback)
    return shim.read()


def test_auto_decode_utf8_and_gbk():
    text = "我要去北京"
    assert _decode_all(text.encode("utf-8")) == text
    assert _decode_all(text.encode("gbk")) == text
    # ASCII 前缀的 GBK 行：整行按 UTF-8 失败后退回 locale，不能变乱码
    prefixed = "abc " + text
    assert _decode_all(prefixed.encode("gbk")) == prefixed
    # ASCII 前缀的 UTF-8 行：按 UTF-8 成功
    assert _decode_all(prefixed.encode("utf-8")) == prefixed
    print("✓ 管道自动解码：UTF-8 / GBK / ASCII 前缀")


def test_auto_decode_bom_encodings():
    text = "中文输入"
    assert _decode_all(text.encode("utf-8-sig")) == text
    assert _decode_all(text.encode("utf-16")) == text
    assert _decode_all((text + "\n第二行\n").encode("utf-16")) == text + "\n第二行\n"
    print("✓ BOM 输入（utf-8-sig / UTF-16）")


def test_readline_line_by_line():
    from utils.console import _AutoDecodeInput

    payload = ("第一行\n第二行\n").encode("gbk")
    shim = _AutoDecodeInput(_PipeBuffer(payload), "gbk")
    assert shim.readline() == "第一行\n"
    assert shim.readline() == "第二行\n"
    assert shim.readline() == ""
    print("✓ 逐行读取自动解码")


def test_probe_stdin_false_leaves_stream_untouched(monkeypatch):
    """一次性命令跳过 stdin：不包装、不触碰（也不会 peek 阻塞）。"""

    class _Buffer(io.BytesIO):
        def peek(self, size=2):
            raise AssertionError("probe_stdin=False 不应触碰 stdin")

    class _Stdin:
        buffer = _Buffer(b"x")
        encoding = "gbk"

        def isatty(self):
            return False

        def readline(self):
            return "x"

    stdin = _Stdin()
    monkeypatch.setattr(sys, "stdin", stdin)
    configure_stdio(probe_stdin=False)
    assert sys.stdin is stdin

    # 抽象流（无 buffer.readline）不包装，保持原样
    class _Abstract:
        encoding = "gbk"

        def isatty(self):
            return False

    abstract = _Abstract()
    monkeypatch.setattr(sys, "stdin", abstract)
    configure_stdio()
    assert sys.stdin is abstract
    print("✓ probe_stdin=False / 不可包装流保持原样")


def test_non_tty_pipe_is_wrapped(monkeypatch):
    class _Stdin:
        def __init__(self):
            self.buffer = _PipeBuffer("中文".encode("gbk"))
            self.encoding = "gbk"

        def isatty(self):
            return False

    stdin = _Stdin()
    monkeypatch.setattr(sys, "stdin", stdin)
    configure_stdio()
    wrapper = sys.stdin
    assert wrapper is not stdin, "非终端 stdin 应被自动解码包装"
    assert wrapper.readline() == "中文"
    # 重复调用不会二次包装
    configure_stdio()
    assert sys.stdin is wrapper
    print("✓ 非终端管道自动包装（幂等）")


if __name__ == "__main__":
    test_idle_pipe_does_not_block_startup()
    mp = pytest.MonkeyPatch()
    try:
        test_auto_decode_utf8_and_gbk()
        test_auto_decode_bom_encodings()
        test_readline_line_by_line()
        test_probe_stdin_false_leaves_stream_untouched(mp)
        test_non_tty_pipe_is_wrapped(mp)
    finally:
        mp.undo()
    print("\n全部通过")
