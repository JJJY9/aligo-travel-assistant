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
import os
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
        # UTF-16 输入按固定大小分块读取（无 size 的 read() 会一直阻塞到 EOF，
        # 交互式管道下第一个 input() 等不到整行就死锁）；解码用增量解码器
        # 处理跨块的多字节字符
        self._utf16_text = ""
        self._utf16_decoder: Any = None
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
            # UTF-16 的换行是两个字节：按字节 readline 会劈开字符。这里只
            # 标记模式，按块增量解码（不能无界 read()——那会阻塞到 EOF）
            self._mode = "utf-16"
            self._utf16_decoder = codecs.getincrementaldecoder("utf-16")(
                errors="replace"
            )
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

    def _try_decode(self, raw: bytes) -> str | None:
        """严格解码（不做替换）；用来判断字节块末尾是否切断了多字节字符。"""
        if self._mode == "utf-8-sig":
            try:
                return raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                return None
        for candidate in ("utf-8", self._fallback):
            try:
                return raw.decode(candidate)
            except UnicodeDecodeError:
                continue
        return None

    def _read_sized(self, read_all: Any, needed: int) -> bytes:
        """按字节读至少 needed 个字符；块尾切断多字节字符时补读（最多 4 字节）。

        直接 `read(needed)` 在 GBK / UTF-8 管道上可能切在多字节字符中间，
        严格解码失败后退回替换字符，会把分块读取变成乱码。
        """
        chunk = read_all(needed) or b""
        extra = 0
        while chunk and extra < 4 and self._try_decode(chunk) is None:
            more = read_all(1) or b""
            if not more:
                break
            chunk += more
            extra += 1
        return chunk

    def close(self) -> None:
        pass

    def flush(self) -> None:
        pass

    # UTF-16 分块读取的字节数：小到不会长时间阻塞在未到齐的行上，
    # 大到一次系统调用就能拿到常见的一行
    _UTF16_CHUNK = 4096

    def _read_utf16_more(self) -> bool:
        """读一块 UTF-16 字节并解码追加到缓冲区；返回是否读到了数据。"""
        data = self._buffer.read(self._UTF16_CHUNK) or b""
        if not data:
            return False
        self._utf16_text += self._utf16_decoder.decode(data)
        return True

    def _next_utf16_line(self) -> str:
        while True:
            index = self._utf16_text.find("\n")
            if index >= 0:
                line = self._utf16_text[: index + 1]
                self._utf16_text = self._utf16_text[index + 1 :]
                return line
            if not self._read_utf16_more():
                line = self._utf16_text
                self._utf16_text = ""
                return line

    def _next_pending_line(self) -> str:
        """从截断余量里取一行；余量里没有换行时继续从底层补读。

        只把余量整段返回会让 `read(2)` 之后的第一次 `readline()` 一次
        返回两行（TextIO 契约应按换行拆分）。
        """
        pending = self._pending
        newline = pending.find("\n")
        if newline >= 0:
            self._pending = pending[newline + 1 :]
            return pending[: newline + 1]
        self._pending = ""
        if self._mode == "utf-16":
            return pending + self._next_utf16_line()
        raw = self._buffer.readline()
        return pending + (self._decode_line(raw) if raw else "")

    def readline(self, size: int | None = -1) -> str:
        self._ensure_mode()
        if self._pending:
            line = self._next_pending_line()
        elif self._lines:
            line = self._lines.pop(0)
        elif self._mode == "utf-16":
            line = self._next_utf16_line()
            if not line:
                return ""
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
        if size == 0:
            # TextIO 契约：size=0 不消费输入，也不能阻塞到 EOF
            return ""
        parts: list[str] = []
        if self._pending:
            parts.append(self._pending)
            self._pending = ""
        parts.extend(self._lines)
        self._lines = []
        unlimited = size is None or size < 0
        target = -1 if unlimited else int(size if size is not None else 0)
        if self._mode == "utf-16":
            if self._utf16_text:
                parts.append(self._utf16_text)
                self._utf16_text = ""
            while (
                target < 0
                or sum(len(part) for part in parts) + len(self._utf16_text) < target
            ):
                if not self._read_utf16_more():
                    break
            if self._utf16_text:
                parts.append(self._utf16_text)
                self._utf16_text = ""
        else:
            read_all = getattr(self._buffer, "read", None)
            if callable(read_all):
                if unlimited:
                    data = read_all() or b""
                    if data:
                        parts.append(self._decode_line(data))
                else:
                    # size 必须传给底层：无参 read() 会阻塞到 EOF，活动管道
                    # 上分块读取（如 shutil.copyfileobj）会死等。可能要多读
                    # 几块才能凑够字符数（多字节编码），最终由下方统一截断
                    needed = target - sum(len(part) for part in parts)
                    while needed > 0:
                        chunk = self._read_sized(read_all, needed)
                        if not chunk:
                            break
                        text_chunk = self._decode_line(chunk)
                        parts.append(text_chunk)
                        needed -= len(text_chunk)
        text = "".join(parts)
        if size is None or size < 0:
            return text
        if size < len(text):
            # 截断后的余量保留，下一次 read/readline 继续返回（size=0 不消费）
            self._pending = text[size:]
            return text[:size]
        return text


def is_pipe_error(exc: OSError) -> bool:
    """管道类写入错误：下游提前关闭（``| head``）在 Windows 上的两种表现。

    Windows 关闭管道抛 ``OSError [Errno 22]`` / ``BrokenPipeError``（读端
    未读取即退出），POSIX 为 EPIPE；其余 OSError（磁盘满等）必须照常抛出。
    """
    return (
        isinstance(exc, BrokenPipeError)
        or getattr(exc, "errno", None) in (22, 32)
        or getattr(exc, "winerror", None) in (109, 232)
    )


class PipeSafeTextIO:
    """包裹 stdout：下游管道提前关闭（如 ``| head``）后写入静默丢弃。

    Windows 上关闭的管道抛 ``OSError [Errno 22]`` 而不是 BrokenPipeError；
    只有管道类错误被吞掉，真实写失败（磁盘满等）照常抛出。交互式 CLI 的
    Rich 输出走这里，管道关闭后不再向 stderr 打 traceback。
    """

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._broken = False

    @staticmethod
    def _is_pipe_error(exc: OSError) -> bool:
        return is_pipe_error(exc)

    def _swallow(self, exc: OSError) -> bool:
        if not self._is_pipe_error(exc):
            return False
        self._broken = True
        # 把真实 stdout 的 fd 接到 devnull：否则解释器退出时 flush
        # sys.stdout 还会再报一次 OSError（进程以 120 退出）
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(devnull, self._stream.fileno())
            finally:
                os.close(devnull)
        except Exception:  # noqa: BLE001 - 重定向失败不影响「已损坏」判定
            pass
        return True

    def write(self, text: str) -> int:
        if self._broken:
            return len(text)
        try:
            return self._stream.write(text)
        except OSError as exc:
            if not self._swallow(exc):
                raise
            return len(text)

    def flush(self) -> None:
        if self._broken:
            return
        try:
            self._stream.flush()
        except OSError as exc:
            if not self._swallow(exc):
                raise

    def isatty(self) -> bool:
        try:
            return bool(self._stream.isatty())
        except Exception:  # noqa: BLE001 - 抽象流判断失败按非终端处理
            return False

    def fileno(self) -> int:
        return self._stream.fileno()

    @property
    def encoding(self) -> str:
        return getattr(self._stream, "encoding", None) or "utf-8"

    @property
    def errors(self) -> str:
        return getattr(self._stream, "errors", None) or "strict"

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


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
