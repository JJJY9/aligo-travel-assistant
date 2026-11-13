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


def test_auto_decode_nul_and_bomless_utf16le():
    """NUL 行不能原样穿过（exit\\0 不等于 exit）；BOM-less UTF-16LE 按字符解出。"""
    assert _decode_all(b"exit\x00\n") == "exit\n"
    assert _decode_all("北京\n".encode("utf-16-le")) == "北京\n"
    print("✓ NUL 剔除与 BOM-less UTF-16LE 解码")


def test_stdin_fallback_prefers_locale_under_pythonutf8(monkeypatch):
    """PYTHONUTF8=1 时 stream.encoding 已是 utf-8，回退必须改用 locale 编码。"""
    import sys

    import utils.console as console_module

    class _FakeInput:
        encoding = "utf-8"
        buffer = _PipeBuffer(b"")
        peek = buffer.peek

        def isatty(self):
            return False

    monkeypatch.setattr(sys, "stdin", _FakeInput())
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    monkeypatch.setattr(
        "locale.getpreferredencoding", lambda do_setlocale=False: "cp936"
    )
    console_module.configure_stdio()
    assert isinstance(sys.stdin, console_module._AutoDecodeInput)
    assert sys.stdin._fallback == "cp936", sys.stdin._fallback
    print("✓ PYTHONUTF8 下 stdin 回退使用 locale 编码")


def test_readline_line_by_line():
    from utils.console import _AutoDecodeInput

    payload = ("第一行\n第二行\n").encode("gbk")
    shim = _AutoDecodeInput(_PipeBuffer(payload), "gbk")
    assert shim.readline() == "第一行\n"
    assert shim.readline() == "第二行\n"
    assert shim.readline() == ""
    print("✓ 逐行读取自动解码")


def test_readline_size_preserves_unread_remainder():
    """readline(size) 截断后的余量留待下一次读取（TextIO 契约），size=0 不消费。"""
    from utils.console import _AutoDecodeInput

    payload = "第一行内容\n第二行\n".encode("gbk")
    shim = _AutoDecodeInput(_PipeBuffer(payload), "gbk")
    assert shim.readline(3) == "第一行"
    assert shim.readline() == "内容\n", "截断后的余量必须留给下一次 readline"
    assert shim.readline() == "第二行\n"
    assert shim.readline() == ""

    shim2 = _AutoDecodeInput(_PipeBuffer(payload), "gbk")
    assert shim2.readline(0) == ""
    assert shim2.readline() == "第一行内容\n", "size=0 不消费输入"
    assert shim2.readline(10**6) == "第二行\n", "超大 size 返回整行"
    print("✓ readline(size) 保留未读余量")


def test_readline_size_after_read_keeps_buffered_lines():
    """read() 截断后再 readline(size)：截断余量不能覆盖已缓冲的后续行。"""
    from utils.console import _AutoDecodeInput

    # 用 ASCII 负载：任意 GBK 汉字字节串都可能是合法 UTF-8，解码有歧义
    payload = b"abcdef\nsecond\n"
    shim = _AutoDecodeInput(_PipeBuffer(payload), "gbk")
    assert shim.read(2) == "ab"
    # 余量里已有「cdef\nsecond\n」；readline(3) 再截断第一行
    assert shim.readline(3) == "cde"
    assert shim.readline() == "f\n", "第一行的剩余部分"
    assert shim.readline() == "second\n", "已缓冲的后续行不能丢失"
    assert shim.readline() == ""
    print("✓ readline(size) 截断余量不覆盖已缓冲行")


def test_read_size_preserves_unread_remainder():
    """read(size) 截断后的余量留待下一次读取；分块复制不丢内容。"""
    import shutil

    from utils.console import _AutoDecodeInput

    payload = "第一行内容\n第二行\n".encode("gbk")
    shim = _AutoDecodeInput(_PipeBuffer(payload), "gbk")
    assert shim.read(3) == "第一行"
    assert shim.read() == "内容\n第二行\n", "截断后的余量必须留给下一次 read"
    assert shim.read() == ""

    shim2 = _AutoDecodeInput(_PipeBuffer(payload), "gbk")
    assert shim2.read(0) == ""
    assert shim2.read() == "第一行内容\n第二行\n", "size=0 不消费"

    # 分块复制的真实消费者（如 shutil.copyfileobj）不得截断输入
    src = _AutoDecodeInput(_PipeBuffer(payload), "gbk")
    out = io.StringIO()
    shutil.copyfileobj(src, out, length=8)
    assert out.getvalue() == "第一行内容\n第二行\n"
    print("✓ read(size) 保留未读余量（copyfileobj 不丢内容）")


def test_read_size_does_not_read_to_eof():
    """read(size) 必须把 size 传给底层，不能无参读到 EOF（活动管道会死等）。"""
    from utils.console import _AutoDecodeInput

    class _RecordingBuffer(_PipeBuffer):
        def __init__(self, payload: bytes):
            super().__init__(payload)
            self.read_sizes: list[int] = []

        def read(self, size=-1):
            self.read_sizes.append(size)
            return super().read(size)

    buffer = _RecordingBuffer("abcdef\nghij\n".encode("utf-8"))
    shim = _AutoDecodeInput(buffer, "utf-8")
    assert shim.read(2) == "ab"
    assert -1 not in buffer.read_sizes, "read(size) 不能退化成无参读到 EOF"
    # 截断余量里的多行文本也必须按行拆分（不能一次 readline 返回两行）
    assert shim.readline() == "cdef\n"
    assert shim.read() == "ghij\n"
    print("✓ read(size) 不读到 EOF，readline 按行拆分余量")


def test_utf16_readline_uses_single_chunk_reads():
    """UTF-16 分支必须用 read1（一次底层读），活动管道上 read(n) 会阻塞到 EOF。"""
    import codecs

    from utils.console import _AutoDecodeInput

    class _OnlyRead1Buffer(io.BytesIO):
        def __init__(self, payload: bytes):
            super().__init__(payload)
            self.read1_calls: list[int] = []

        def peek(self, size=2):
            position = self.tell()
            data = io.BytesIO.read(self, size)
            self.seek(position)
            return data

        def read(self, size=-1):
            raise AssertionError(
                "UTF-16 分块读取必须用 read1（BufferedReader.read(n) 会凑满 n 字节）"
            )

        def read1(self, size=-1):
            self.read1_calls.append(size)
            return io.BytesIO.read(self, size)

    payload = codecs.BOM_UTF16_LE + "第一行\n".encode("utf-16-le")
    buffer = _OnlyRead1Buffer(payload)
    shim = _AutoDecodeInput(buffer, "utf-8")
    assert shim.readline() == "第一行\n"
    assert buffer.read1_calls, "应通过 read1 读取 UTF-16 块"
    print("✓ UTF-16 分块读取使用 read1")


def test_readline_utf16_lines_and_size():
    """UTF-16 输入的逐行 pop 路径与 size 截断都要覆盖。"""
    from utils.console import _AutoDecodeInput

    payload = "第一行\n第二行\n".encode("utf-16")
    shim = _AutoDecodeInput(_PipeBuffer(payload), "gbk")
    assert shim.readline() == "第一行\n"
    assert shim.readline() == "第二行\n"
    assert shim.readline() == ""

    shim2 = _AutoDecodeInput(_PipeBuffer("第一行\n".encode("utf-16")), "gbk")
    assert shim2.readline(2) == "第一"
    assert shim2.readline() == "行\n"
    print("✓ UTF-16 逐行 readline 与 size 截断")

def test_utf16_readline_is_incremental_not_read_to_eof():
    """UTF-16 首行读取不能无界 read()：管道未 EOF 时 input() 必须立即返回。"""
    from utils.console import _AutoDecodeInput

    class _GuardedBytesIO(io.BytesIO):
        def peek(self, size=2):
            position = self.tell()
            data = self.read(size)
            self.seek(position)
            return data

        def read(self, size=-1):
            if size is None or size < 0:
                raise AssertionError("UTF-16 首行读取不能无界 read()")
            return super().read(size)

    payload = "alice\nbob\n".encode("utf-16")
    shim = _AutoDecodeInput(_GuardedBytesIO(payload), "utf-8")
    assert shim.readline() == "alice\n"
    assert shim.readline() == "bob\n"
    assert shim.readline() == ""

    shim2 = _AutoDecodeInput(_GuardedBytesIO("甲\n".encode("utf-16")), "utf-8")
    assert shim2.read(0) == "", "size=0 不消费输入、也不阻塞"
    assert shim2.readline() == "甲\n"
    print("✓ UTF-16 按块增量解码（readline 不再读到 EOF）")


def test_pipe_safe_stdout_swallows_broken_pipe():
    """`| head` 关闭管道后的写入静默丢弃；真实写失败照常抛出。"""
    from utils.console import PipeSafeTextIO

    class _Broken:
        def write(self, text):
            raise OSError(22, "Invalid argument")

        def flush(self):
            raise OSError(22, "Invalid argument")

        def isatty(self):
            return False

    safe = PipeSafeTextIO(_Broken())
    assert safe.write("hello") == 5
    safe.flush()
    assert safe.write("again") == 5, "管道关闭后后续写入静默丢弃"

    class _DiskFull:
        def write(self, text):
            raise OSError(28, "No space left on device")

    with pytest.raises(OSError):
        PipeSafeTextIO(_DiskFull()).write("x")
    print("✓ PipeSafeTextIO 只吞管道类写入错误")


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


def test_auto_decode_input_textio_properties(tmp_path):
    """TextIO 契约属性：只读流、编码固定 utf-8、close/flush 为空操作。"""
    from utils.console import _AutoDecodeInput

    handle_path = tmp_path / "stdin.bin"
    handle_path.write_bytes(b"")
    handle = open(handle_path, "rb")
    try:
        shim = _AutoDecodeInput(handle)
        assert shim.encoding == "utf-8"
        assert shim.readable() is True
        assert shim.writable() is False
        assert shim.seekable() is False
        assert shim.isatty() is False
        assert isinstance(shim.fileno(), int)
        shim.close()
        shim.flush()
    finally:
        handle.close()
    print("✓ 自动解码 stdin 的 TextIO 属性")


def test_auto_decode_peek_failure_uses_auto_mode():
    """底层 peek 抛异常时按逐行 auto 判断，readline 仍可正常工作。"""
    from utils.console import _AutoDecodeInput

    nl = chr(10)

    class _PeekBoom(io.BytesIO):
        def peek(self, size=2):
            raise RuntimeError("peek unsupported")

    shim = _AutoDecodeInput(_PeekBoom(("你好" + nl).encode("utf-8")))
    assert shim.readline() == "你好" + nl
    assert shim._mode == "auto"
    print("✓ peek 失败按逐行 auto 判断")


def test_read_merges_pending_line():
    """readline(size) 截断的余量在下一次 read() 中继续返回。"""
    from utils.console import _AutoDecodeInput

    nl = chr(10)
    shim = _AutoDecodeInput(_PipeBuffer(("第一行内容" + nl).encode("utf-8")))
    assert shim.readline(2) == "第一"
    assert shim.read() == "行内容" + nl
    assert shim.read() == ""
    print("✓ read() 合并 readline 的截断余量")


def test_read_size_truncation_keeps_remainder_utf16():
    """UTF-16 分块解码可能一次解出多字：read(size) 截断后的余量留给下次读取。"""
    from utils.console import _AutoDecodeInput

    nl = chr(10)
    shim = _AutoDecodeInput(_PipeBuffer(("你好" + nl).encode("utf-16")))
    assert shim.read(1) == "你"
    assert shim.read() == "好" + nl
    print("✓ UTF-16 read(size) 截断余量保留")


def test_pipe_safe_textio_delegates_metadata():
    """PipeSafeTextIO 的编码/错误策略/属性访问回退到被包装流。"""
    from utils.console import PipeSafeTextIO

    class _Abstract:
        encoding = None

        def isatty(self):
            raise RuntimeError("no tty")

        def write(self, text):
            return len(text)

        def flush(self):
            pass

        def fileno(self):
            return 3

        custom_attr = "delegated"

    safe = PipeSafeTextIO(_Abstract())
    assert safe.encoding == "utf-8"
    assert safe.errors == "strict"
    assert safe.isatty() is False
    assert safe.fileno() == 3
    assert safe.custom_attr == "delegated"

    # 未标记 broken 之前 flush 抛管道错误：同样静默吞掉
    class _FlushBroken:
        def write(self, text):
            return len(text)

        def flush(self):
            raise OSError(22, "Invalid argument")

    PipeSafeTextIO(_FlushBroken()).flush()
    print("✓ PipeSafeTextIO 元数据与属性代理 / flush 容错")


def test_reconfigure_failure_is_swallowed(monkeypatch):
    """reconfigure 抛异常（已关闭 / 抽象流）不能影响启动。"""
    from utils import console as console_module

    class _Closed:
        def reconfigure(self, **kwargs):
            raise OSError("closed")

    monkeypatch.setattr(sys, "stdout", _Closed())
    console_module._reconfigure("stdout", "utf-8")
    monkeypatch.undo()
    print("✓ reconfigure 失败被吞掉")


def test_configure_stdio_isatty_error_and_wrap_failure(monkeypatch):
    """isatty 抛异常按非终端处理；包装失败保持原流不变。"""
    from utils import console as console_module

    class _Buffer(io.BytesIO):
        def readline(self, *args):
            return b""

    class _Stdin:
        def __init__(self):
            self.buffer = _Buffer(b"")
            self.encoding = "gbk"

        def isatty(self):
            raise RuntimeError("isatty broken")

    stdin = _Stdin()
    monkeypatch.setattr(sys, "stdin", stdin)
    configure_stdio()
    assert isinstance(sys.stdin, console_module._AutoDecodeInput), (
        "isatty 异常应按非终端处理并包装"
    )

    # 包装本身失败：保持原流，不中断启动
    class _WrapBoom:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("wrap failed")

    stdin2 = _Stdin()
    monkeypatch.setattr(sys, "stdin", stdin2)
    monkeypatch.setattr(console_module, "_AutoDecodeInput", _WrapBoom)
    configure_stdio()
    assert sys.stdin is stdin2
    print("✓ configure_stdio isatty 异常与包装失败")


if __name__ == "__main__":
    test_idle_pipe_does_not_block_startup()
    mp = pytest.MonkeyPatch()
    try:
        test_auto_decode_utf8_and_gbk()
        test_auto_decode_bom_encodings()
        test_readline_line_by_line()
        test_readline_size_preserves_unread_remainder()
        test_readline_size_after_read_keeps_buffered_lines()
        test_read_size_preserves_unread_remainder()
        test_readline_utf16_lines_and_size()
        test_utf16_readline_is_incremental_not_read_to_eof()
        test_pipe_safe_stdout_swallows_broken_pipe()
        test_probe_stdin_false_leaves_stream_untouched(mp)
        test_non_tty_pipe_is_wrapped(mp)
    finally:
        mp.undo()
    print("\n全部通过")
