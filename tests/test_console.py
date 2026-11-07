"""
configure_stdio 管道探测测试（无需 API Key、不访问网络）

运行：python tests/test_console.py
"""
import os
import subprocess
import sys

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
            # stdin 保持打开且不写入：旧实现会卡在 BufferedReader.peek
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


def test_pick_stdin_encoding_bom_and_ascii_prefix(monkeypatch):
    """BOM 输入用 utf-8-sig；ASCII 前缀后的 UTF-8 中文不能被按 GBK 解码。"""
    from utils import console as console_module

    class _Stdin:
        def __init__(self, payload, encoding="gbk"):
            self.buffer = self

            class _Inner:
                pass

            self._payload = payload
            self.encoding = encoding

        def peek(self, size):
            return self._payload[:size]

        def isatty(self):
            return False

    # 假流没有 fileno：直接视为「管道已有数据」
    monkeypatch.setattr(
        console_module, "_stdin_has_data", lambda stream, timeout_sec=0.1: True
    )
    monkeypatch.setattr(sys, "stdin", _Stdin(b"\xef\xbb\xbf{}"))
    assert console_module._pick_stdin_encoding("gbk") == "utf-8-sig"

    # 前 64 字节全 ASCII、中文在后面的 UTF-8 管道
    payload = b"abcd " + "中文输入".encode("utf-8")
    monkeypatch.setattr(sys, "stdin", _Stdin(payload))
    assert console_module._pick_stdin_encoding("gbk") == "utf-8"

    # GBK 管道：出现 >=0x81 字节且不是合法 UTF-8 → 沿用 locale
    monkeypatch.setattr(sys, "stdin", _Stdin("中文".encode("gbk")))
    assert console_module._pick_stdin_encoding("gbk") == "gbk"
    print("✓ stdin 编码探测：BOM / ASCII 前缀 / GBK")


def test_probe_stdin_false_skips_probe(monkeypatch):
    """一次性命令可显式跳过 stdin 探测。"""
    calls = []

    class _Buffer:
        def peek(self, size):
            calls.append(size)
            return b""

    class _Stdin:
        buffer = _Buffer()
        encoding = "gbk"

        def isatty(self):
            return False

        def reconfigure(self, **kwargs):
            pass

    monkeypatch.setattr(sys, "stdin", _Stdin())
    configure_stdio(probe_stdin=False)
    configure_stdio()  # 抽象流没有 fileno：判断不了可用数据时不 peek
    assert calls == []
    print("✓ probe_stdin=False 跳过 stdin 探测")


if __name__ == "__main__":
    test_idle_pipe_does_not_block_startup()
    import pytest

    mp = pytest.MonkeyPatch()
    try:
        test_probe_stdin_false_skips_probe(mp)
        test_pick_stdin_encoding_bom_and_ascii_prefix(mp)
    finally:
        mp.undo()
    print("\n全部通过")
