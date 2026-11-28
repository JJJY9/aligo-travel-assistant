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
import re
import sys
from typing import Any

# 终端控制字符（C0 除 \t \n、DEL 与 C1 区）：存储的用户内容与模型输出都不
# 可信，原样打印会让终端执行 OSC 52（写剪贴板）、CSI 清屏 / 隐藏光标、
# OSC 8 伪造链接等序列（rich 的 escape 只转义方括号，不处理 ESC）
_TERMINAL_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def strip_terminal_controls(text: object) -> str:
    """去除文本中的终端控制序列（保留换行与制表符）。"""
    return _TERMINAL_CONTROLS.sub("", str(text))


# 终端显示专用的不可见格式符：双向嵌入 / 覆盖 / 隔离、方向标记、零宽空格、
# 词连接符、BOM、软连字符、行 / 段分隔符。零宽连接符 U+200C / U+200D 故意
# 保留：ZWJ 是 emoji 组合序列与部分文字整形的一部分，剥掉会改变正常渲染。
# 存储与导出保持原值（exporter / session_html 需要保真），只在终端显示层剥除。
_DISPLAY_FORMAT_CONTROLS = re.compile(
    "[\\u061c\\u00ad\\u200b\\u200e\\u200f\\u2028\\u2029"
    "\\u202a-\\u202e\\u2060\\u2066-\\u2069\\ufeff]"
)


def strip_display_format_controls(text: object) -> str:
    """终端显示层清洗：剥终端控制序列 + 不可见格式符（含双向覆盖）。

    RLO / LRI 等双向控制符与零宽字符可以把「详见 C:\\x」在终端里伪装成
    「详见 /etc/passwd」之类的另一个字节序列；所有把不可信文本送上终端的
    路径（CLI 字段 / doctor 表格）都应先过本函数。
    """
    return _DISPLAY_FORMAT_CONTROLS.sub("", strip_terminal_controls(text))


class _AutoDecodeInput:
    """非终端 stdin 的按行自动解码包装。

    ``input()`` / ``sys.stdin.readline()`` 会走到这里：读原始字节后逐行解码
    （UTF-8 优先、失败回退 locale 编码），首行 BOM 决定 utf-8-sig / UTF-16。
    编码判断按整行内容进行，ASCII 前缀不会把 GBK 中文误判成 UTF-8。
    """

    # builtins.input 只需要 readline；不继承 TextIOBase（其 read/readline
    # 的字节语义与方法签名与文本包装冲突）
    closed = False

    def __init__(
        self,
        buffer: Any,
        fallback_encoding: str = "utf-8",
        forced_encoding: str | None = None,
    ) -> None:
        self._buffer = buffer
        self._fallback = fallback_encoding or "utf-8"
        # ALIGO_STDIN_ENCODING：显式指定逐行解码的编码，跳过自动判定
        # （GBK「唯一 / 一一 / 十」等双字节序列恰好也是合法 UTF-8，
        # 自动判定无法区分；显式编码是唯一可靠的出路）
        self._forced = forced_encoding or ""
        self._first_line_checked = False
        self._ambiguity_warned = False
        # None=尚未判定；"auto"=逐行判断；"utf-8-sig"=BOM；"utf-16"=行已拆好；
        # "forced"=按显式编码逐行解码
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
        if self._forced:
            self._mode = "forced"
            return
        head = b""
        peek = getattr(self._buffer, "peek", None)
        if callable(peek):
            try:
                # 64 字节窗口会把较长的 UTF-16LE 首行（>31 个汉字）的换行
                # 高字节切出样本；peek 不会为凑满窗口阻塞，放大到 4KB
                head = bytes(peek(4096)[:4096])
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
        if self._looks_like_bomless_utf16le(head):
            # 无 BOM 的 UTF-16LE（Node Buffer utf16le、部分 .NET 写出）：
            # 必须按流级模式增量解码——按字节行切分会在 0x0A 处把码元
            # 劈开，行尾残留一个 0x00 让后续全部错位
            self._mode = "utf-16-le"
            self._utf16_decoder = codecs.getincrementaldecoder("utf-16-le")(
                errors="replace"
            )
            return
        self._mode = "auto"

    @staticmethod
    def _looks_like_bomless_utf16le(head: bytes) -> bool:
        """无 BOM 的 UTF-16LE 探测。

        强信号是 UTF-16LE 的换行码元 ``0x0A 0x00``（GBK / UTF-8 文本不会
        包含 0x00 字节）。没有换行时退回「奇数位零字节不少于偶数位」：
        ASCII 文本是「字符 + 0x00」，中文常用字（一 U+4E00、开 U+5F00…）
        的低字节也是 0x00 但落在偶数位——因此不能再要求偶数位没有零，
        否则含这些字的首行会被漏判、后续按字节切行整体错位。
        """
        if len(head) < 4:
            # 短前缀里出现 NUL 只可能是 UTF-16（GBK / UTF-8 不含 0x00）
            return b"\x00" in head
        if b"\n\x00" in head:
            return True
        sample = head[0::2]
        high = head[1::2]
        return high.count(0) >= 1 and high.count(0) >= sample.count(0)

    def _is_utf16(self) -> bool:
        return self._mode in ("utf-16", "utf-16-le")

    def _decode_line(self, raw: bytes) -> str:
        if self._mode == "forced":
            # 显式编码（ALIGO_STDIN_ENCODING）：不做任何自动判定
            line = raw.decode(self._forced, errors="replace")
        elif self._mode == "utf-8-sig":
            line = raw.decode("utf-8-sig", errors="replace")
        else:
            line = ""
            if b"\x00" in raw and len(raw) % 2 == 0:
                # BOM-less UTF-16LE（Node 的 Buffer utf16le、部分 .NET 写出）：
                # ASCII 字符高位是 0x00，按 UTF-8 解出来全是 NUL。只有解出的
                # 文本全为可打印字符（含换行）才认定为 UTF-16LE——否则像
                # "exit\0\n" 这种普通 UTF-8 行会被错误配对成 CJK 乱码
                try:
                    candidate = raw.decode("utf-16-le")
                except UnicodeDecodeError:
                    candidate = ""
                if candidate and "\x00" not in candidate and all(
                    char in "\n\r\t" or char.isprintable() for char in candidate
                ):
                    line = candidate
            if not line:
                for candidate_encoding in ("utf-8", self._fallback):
                    try:
                        line = raw.decode(candidate_encoding)
                        break
                    except UnicodeDecodeError:
                        continue
                else:
                    line = raw.decode("utf-8", errors="replace")
        # NUL 不是命令行文本：残留时剔除（否则 "exit\0" 不匹配 exit，
        # 会被当成自然语言查询连同 NUL 发给模型）
        return line.replace("\x00", "")

    def _check_encoding_ambiguity(self, raw: bytes, first_line: bool) -> None:
        """GBK 与 UTF-8 同时「严格可解」但结果不同：拒绝静默改字。

        GBK 有大量双字节序列恰好是合法 UTF-8（「唯一 / 一一 / 十 / 没 /
        说」等），逐行「UTF-8 优先」会把它们静默解成西里尔 / 希腊字母——
        用户 ID 行落到另一个合法账号。仅当回退编码不是 UTF-8 家族、且
        两种解码都成功但结果不同才算歧义：首行抛 ValueError（CLI 的
        用户 ID 提示路径会给出两种候选与 ALIGO_STDIN_ENCODING 指引），
        后续行向 stderr 警告一次并维持 UTF-8 优先。
        """
        if self._mode != "auto" or self._forced:
            return
        normalized = (
            self._fallback.lower().replace("-", "").replace("_", "")
        )
        if normalized in {"utf8", "utf"}:
            return
        try:
            as_utf8 = raw.decode("utf-8")
        except UnicodeDecodeError:
            return
        try:
            as_fallback = raw.decode(self._fallback)
        except (UnicodeDecodeError, LookupError):
            return
        if as_utf8 == as_fallback:
            return
        if first_line:
            raise ValueError(
                "stdin 编码歧义：同一行按 UTF-8 解码为 "
                f"{as_utf8.rstrip()!r}、按 {self._fallback} 解码为 "
                f"{as_fallback.rstrip()!r}；请设置 "
                "ALIGO_STDIN_ENCODING=utf-8 或 "
                f"ALIGO_STDIN_ENCODING={self._fallback} 后重试"
            )
        if not self._ambiguity_warned:
            self._ambiguity_warned = True
            print(
                "警告：stdin 行存在编码歧义（UTF-8 / "
                f"{self._fallback}），已按 UTF-8 解析；"
                "可设置 ALIGO_STDIN_ENCODING 指定编码",
                file=sys.stderr,
            )

    def _needs_more_bytes(self, raw: bytes) -> bool:
        """块尾是否切断了多字节字符（UTF-8 与回退编码分别判定）。

        不能用「回退编码能否解码整块」判断：GBK 常能把截断的 UTF-8 前缀
        解成合法字符（E4 BD -> 浣），提前停止补读后整块按 GBK 解码、在
        中文 locale 下大面积乱码。这里只认「错误恰好位于块尾」的不完整
        序列——UTF-8 与回退编码各自检查，中段非法字节是真正的其它编码
        数据，补读只会继续吞字节。
        """

        def _tail_cut(encoding: str) -> bool:
            try:
                raw.decode(encoding)
                return False
            except UnicodeDecodeError as exc:
                return exc.start >= len(raw) - 4 and exc.end == len(raw)
            except LookupError:
                return False

        if _tail_cut("utf-8"):
            return True
        fallback = str(self._fallback or "")
        if fallback and fallback.lower().replace("-", "").replace("_", "") != "utf8":
            return _tail_cut(fallback)
        return False

    def _read_sized(self, read_all: Any, needed: int) -> bytes:
        """按字节读至少 needed 个字符；块尾切断多字节字符时补读（最多 4 字节）。

        直接 `read(needed)` 在 GBK / UTF-8 管道上可能切在多字节字符中间，
        严格解码失败后退回替换字符，会把分块读取变成乱码。
        """
        chunk = read_all(needed) or b""
        extra = 0
        while chunk and extra < 4 and self._needs_more_bytes(chunk):
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
        read1 = getattr(self._buffer, "read1", None)
        if callable(read1):
            # read1 只做一次底层读：BufferedReader.read(n) 在活动管道上会
            # 反复读到底层凑满 n 字节或 EOF——控制器写一行后保持管道打开的
            # 场景下 input() 会阻塞到 EOF（与非 UTF-16 分支同一类问题）
            data = read1(self._UTF16_CHUNK) or b""
        else:
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
        if self._is_utf16():
            return pending + self._next_utf16_line()
        raw = self._buffer.readline()
        return pending + (self._decode_line(raw) if raw else "")

    def readline(self, size: int | None = -1) -> str:
        if size == 0:
            # TextIO 契约：size=0 不消费输入，也不能触发首次探测阻塞
            return ""
        self._ensure_mode()
        if self._pending:
            line = self._next_pending_line()
        elif self._lines:
            line = self._lines.pop(0)
        elif self._is_utf16():
            line = self._next_utf16_line()
            if not line:
                return ""
        else:
            raw = self._buffer.readline()
            if not raw:
                return ""
            first_line = not self._first_line_checked
            self._first_line_checked = True
            self._check_encoding_ambiguity(raw, first_line)
            line = self._decode_line(raw)
        if size is None or size < 0:
            return line
        if size < len(line):
            # 截断的剩余部分留待下次读取（size=0 时不消费任何输入）；
            # 必须拼在已有缓冲之前：_next_pending_line 已把后续缓冲行留在
            # _pending 里，直接覆盖会静默丢弃那些行
            self._pending = line[size:] + self._pending
            return line[:size]
        return line

    def read(self, size: int | None = -1) -> str:
        if size == 0:
            # TextIO 契约：size=0 不消费输入，也不能阻塞到 EOF / 首次探测
            return ""
        self._ensure_mode()
        parts: list[str] = []
        if self._pending:
            parts.append(self._pending)
            self._pending = ""
        parts.extend(self._lines)
        self._lines = []
        unlimited = size is None or size < 0
        target = -1 if unlimited else int(size if size is not None else 0)
        if self._is_utf16():
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
        # stdout 可能是 None（pythonw / fd 1 已关闭 / 分离的服务进程）：
        # 按「已损坏的接收端」处理，写入静默丢弃而不是每次写都 AttributeError
        self._stream = stream
        self._broken = stream is None

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
        if self._stream is None:
            raise OSError("stdout 不可用")
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
    forced = os.environ.get("ALIGO_STDIN_ENCODING", "").strip()
    if forced:
        try:
            codecs.lookup(forced)
        except LookupError:
            print(
                f"警告：ALIGO_STDIN_ENCODING={forced!r} 不是有效编码，已忽略",
                file=sys.stderr,
            )
            forced = ""
    fallback = getattr(stream, "encoding", None) or ""
    normalized = fallback.lower().replace("-", "").replace("_", "")
    if not normalized or normalized in {"utf8", "utf"}:
        # PYTHONUTF8 / PYTHONIOENCODING=utf-8 时 stream.encoding 已是 utf-8：
        # 拿它当回退等于没有回退，GBK 管道字节永远解不出来、被替换成乱码。
        # 回退改为系统 locale 编码（Windows 常见 cp936）。注意 PEP 540
        # UTF-8 模式下 locale.getpreferredencoding(False) 同样返回 utf-8，
        # 必须用 locale.getencoding()（3.11+，忽略 UTF-8 模式）或 GetACP
        try:
            import locale

            get_locale_encoding = getattr(locale, "getencoding", None)
            if callable(get_locale_encoding):
                locale_fallback = get_locale_encoding() or ""
            elif sys.platform == "win32":
                import ctypes

                locale_fallback = f"cp{ctypes.windll.kernel32.GetACP()}"
            else:
                locale_fallback = locale.getpreferredencoding(False) or ""
        except Exception:  # noqa: BLE001 - 探测失败退回 utf-8
            locale_fallback = ""
        locale_normalized = locale_fallback.lower().replace("-", "").replace("_", "")
        if locale_fallback and locale_normalized not in {"utf8", "utf"}:
            fallback = locale_fallback
    if not fallback:
        fallback = "utf-8"
    try:
        sys.stdin = _AutoDecodeInput(buffer, fallback, forced or None)
    except Exception:  # noqa: BLE001 - 包装失败保持原流
        pass
