"""
控制台编码兼容

Windows 中文环境的默认控制台编码为 GBK，直接 print("✓ ...") 或 emoji 会抛
UnicodeEncodeError（cmd / PowerShell、未设置 PYTHONIOENCODING 时必现）。
各入口（CLI / Web / 初始化脚本）启动时调用 configure_stdio() 统一切换为 UTF-8。

管道输入按内容自动判断：UTF-8 字节（Git Bash / CI / PowerShell 7）用 UTF-8，
其它（如 cmd 下 GBK 管道）保持 locale 编码，两种输入都不乱码。探测只在管道
已有数据（或有界等待内到达）时进行，避让已连接但暂时无数据的管道把启动卡住。
"""
import select
import sys
import time

_UTF8_BOM = b"\xef\xbb\xbf"
_PROBE_TIMEOUT_SEC = 0.1


def _stdin_has_data(stream, timeout_sec: float = _PROBE_TIMEOUT_SEC) -> bool:
    """有界等待管道出现数据；判断不了时按「没有数据」处理（不能无限阻塞）。

    Windows 没有可等待的 select，用 PeekNamedPipe 轮询；重定向的常规文件
    视为总是可读。stdin 是已连接但暂时没有写入的管道时（IDE / 守护进程
    拉起 CLI 的常见形态），直接 peek 会永久阻塞，连 --version 都跑不出来。
    """
    try:
        fileno = stream.fileno()
    except Exception:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            import msvcrt

            kernel32 = ctypes.windll.kernel32
            handle = ctypes.c_void_p(msvcrt.get_osfhandle(fileno))
            file_type = kernel32.GetFileType(handle)
            if file_type == 0x0001:  # FILE_TYPE_DISK：重定向的常规文件
                return True
            # MSYS / Git Bash 的匿名管道会报告 FILE_TYPE_CHAR，
            # 不能按类型判断，直接 PeekNamedPipe 报告可读字节数
            deadline = time.monotonic() + timeout_sec
            available = ctypes.c_ulong(0)
            while True:
                ok = kernel32.PeekNamedPipe(
                    handle, None, 0, None, ctypes.byref(available), None
                )
                if not ok:
                    return False
                if available.value > 0:
                    return True
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.005)
        except Exception:
            return False
    try:
        ready, _, _ = select.select([stream], [], [], timeout_sec)
        return bool(ready)
    except (OSError, ValueError):
        return False


def _pick_stdin_encoding(default: str) -> str:
    """按管道前几个字节判断输入编码；无法判断时沿用默认（locale）。"""
    stream = getattr(sys, "stdin", None)
    if stream is None:
        return default
    # 真实控制台（PEP 528）本来就是 UTF-8
    if getattr(stream, "isatty", lambda: False)():
        return "utf-8"
    buffer = getattr(stream, "buffer", None)
    peek = getattr(buffer, "peek", None)
    if not callable(peek):
        return default
    if not _stdin_has_data(stream):
        return default
    try:
        head = bytes(peek(64)[:64])
    except Exception:
        return default
    if not head:
        return "utf-8"
    if head.startswith(_UTF8_BOM):
        # BOM 输入按 utf-8-sig 解码：否则 U+FEFF 残留在首行
        return "utf-8-sig"
    if head.isascii():
        # 前 64 字节全 ASCII 只是 UTF-8 的有效前缀（ASCII 子集），
        # 中文可能出现在后面；按 locale 处理 UTF-8 管道会把中文解成乱码。
        # 真正的 GBK 管道通常很快出现 >=0x81 字节，会走下面的判定
        return "utf-8"

    try:
        head.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.reason == "unexpected end of data":
            # 多字节字符被截断：只可能是 UTF-8（GBK 双字节不会出现该错误）
            return "utf-8"
        return default
    return "utf-8"


def _reconfigure(stream_name: str, encoding: str) -> None:
    stream = getattr(sys, stream_name, None)
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding=encoding, errors="replace")
        except Exception:
            pass


def configure_stdio(probe_stdin: bool = True) -> None:
    """输出统一 UTF-8；stdin 管道按内容判断 UTF-8 / locale（无法切换的流跳过）。

    Args:
        probe_stdin: 是否探测 stdin 编码。一次性 CLI 命令不读 stdin，
            传 False 可避免任何管道等待。
    """
    _reconfigure("stdout", "utf-8")
    _reconfigure("stderr", "utf-8")
    if not probe_stdin:
        return
    stream = getattr(sys, "stdin", None)
    default = getattr(stream, "encoding", None) or "utf-8"
    _reconfigure("stdin", _pick_stdin_encoding(default))
