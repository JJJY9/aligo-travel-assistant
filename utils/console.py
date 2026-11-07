"""控制台编码兼容

Windows 中文环境的默认控制台编码为 GBK，直接 print("✓ ...") 或 emoji 会抛
UnicodeEncodeError（cmd / PowerShell、未设置 PYTHONIOENCODING 时必现）。
各入口（CLI / Web / 初始化脚本）启动时调用 configure_stdio() 统一切换为 UTF-8：

- 输出（stdout / stderr）一律切换 UTF-8；
- 输入不做「前 N 字节」猜测（ASCII 前缀会把 GBK 误判成 UTF-8、0.1 秒内无
  数据会回落 locale）：非终端 stdin 包装为按行自动解码的
  :class:`_AutoDecodeInput`，每一行依次尝试 UTF-8 → locale，首行带 BOM 时
  按 utf-8-sig / UTF-16 解码——GBK / UTF-8 / UTF-16 管道都能正确读取，
  且不会因为管道暂时没有数据而拖慢或阻塞启动。
"""
import codecs
import sys
from typing import Any


class _AutoDecodeInput:
    """非终端 stdin 的按行自动解码包装。

    ``input()`` / ``sys.stdin.readline()`` 会走到这里：读原始字节后逐行解码
    （UTF-8 优先、失败回退 locale 编码），首行 BOM 决定 utf-8-sig / UTF-16。
    编码判断按整行内容进行，ASCII 前缀不会把 GBK 中文误判成 UTF-8。
    """

    # builtins.input 只需要 readline；不继承 TextIOBase（其 read/readline
    # 的字节语义与方法签名与文本包装冲突）
    closed = False

    def __init__(self, buffer: Any, fallback_encoding: str = "utf-8") -> None:
        self._buffer = buffer
        self._fallback = fallback_encoding or "utf-8"
        # None=尚未判定；"auto"=逐行判断；"utf-8-sig"=BOM；"utf-16"=行已拆好
        self._mode: str | None = None
        self._lines: list[str] = []
        # readline(size) / read(size) 截断后剩余的文本：TextIO 契约要求
        # 未返回的部分留在流里供下一次读取，不能丢弃（输入会被静默截断）
        self._pending = ""

    @property
    def encoding(self) -> str:
        return "utf-8"

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        return self._buffer.fileno()

    def _ensure_mode(self) -> None:
        if self._mode is not None:
            return
        head = b""
        peek = getattr(self._buffer, "peek", None)
        if callable(peek):
            try:
                head = bytes(peek(3)[:3])
            except Exception:  # noqa: BLE001 - peek 失败按逐行判断
                head = b""
        if head[:2] in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE):
            # UTF-16 的换行是两个字节：按字节 readline 会劈开字符，
            # 这里一次性读入并按文本行拆分
            data = self._buffer.read() or b""
            text = data.decode("utf-16", errors="replace")
            self._lines = text.splitlines(keepends=True)
            self._mode = "utf-16"
            return
        if head[:3] == codecs.BOM_UTF8:
            self._mode = "utf-8-sig"
            return
        self._mode = "auto"

    def _decode_line(self, raw: bytes) -> str:
        if self._mode == "utf-8-sig":
            return raw.decode("utf-8-sig", errors="replace")
        for candidate in ("utf-8", self._fallback):
            try:
                return raw.decode(candidate)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    def close(self) -> None:
        pass

    def flush(self) -> None:
        pass

    def readline(self, size: int | None = -1) -> str:
        self._ensure_mode()
        if self._pending:
            line = self._pending
            self._pending = ""
        elif self._lines:
            line = self._lines.pop(0)
        else:
            raw = self._buffer.readline()
            if not raw:
                return ""
            line = self._decode_line(raw)
        if size is None or size < 0:
            return line
        if size < len(line):
            # 截断的剩余部分留待下次读取（size=0 时不消费任何输入）
            self._pending = line[size:]
            return line[:size]
        return line

    def read(self, size: int | None = -1) -> str:
        self._ensure_mode()
        parts: list[str] = []
        if self._pending:
            parts.append(self._pending)
            self._pending = ""
        parts.extend(self._lines)
        self._lines = []
        read_all = getattr(self._buffer, "read", None)
        if callable(read_all):
            data = read_all() or b""
            if data:
                if self._mode == "utf-16":
                    parts.append(data.decode("utf-16", errors="replace"))
                else:
                    parts.append(self._decode_line(data))
        text = "".join(parts)
        if size is None or size < 0:
            return text
        if size < len(text):
            # 截断后的余量保留，下一次 read/readline 继续返回（size=0 不消费）
            self._pending = text[size:]
            return text[:size]
        return text


def _reconfigure(stream_name: str, encoding: str) -> None:
    stream = getattr(sys, stream_name, None)
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding=encoding, errors="replace")
        except Exception:
            pass


def configure_stdio(probe_stdin: bool = True) -> None:
    """输出统一 UTF-8；stdin 非终端时按行自动解码（见 _AutoDecodeInput）。

    Args:
        probe_stdin: 是否处理 stdin。一次性 CLI 命令不读 stdin，传 False
            可完全跳过（不触碰、也不包装 stdin）。
    """
    _reconfigure("stdout", "utf-8")
    _reconfigure("stderr", "utf-8")
    if not probe_stdin:
        return
    stream = getattr(sys, "stdin", None)
    if stream is None or isinstance(stream, _AutoDecodeInput):
        return
    try:
        is_tty = bool(getattr(stream, "isatty", lambda: False)())
    except Exception:  # noqa: BLE001 - 抽象流判断失败按非终端处理
        is_tty = False
    if is_tty:
        # Windows 终端（PEP 528）本就是 UTF-8；POSIX 终端保留 locale，
        # 不把非 UTF-8 locale 的终端输入强行按 UTF-8 解码
        if sys.platform == "win32":
            _reconfigure("stdin", "utf-8")
        return
    buffer = getattr(stream, "buffer", None)
    if buffer is None or not callable(getattr(buffer, "readline", None)):
        return
    fallback = getattr(stream, "encoding", None) or "utf-8"
    try:
        sys.stdin = _AutoDecodeInput(buffer, fallback)
    except Exception:  # noqa: BLE001 - 包装失败保持原流
        pass
