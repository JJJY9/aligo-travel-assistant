"""
控制台编码兼容

Windows 中文环境的默认控制台编码为 GBK，直接 print("✓ ...") 或 emoji 会抛
UnicodeEncodeError（cmd / PowerShell、未设置 PYTHONIOENCODING 时必现）。
各入口（CLI / Web / 初始化脚本）启动时调用 configure_stdio() 统一切换为 UTF-8。

管道输入按内容自动判断：UTF-8 字节（Git Bash / CI / PowerShell 7）用 UTF-8，
其它（如 cmd 下 GBK 管道）保持 locale 编码，两种输入都不乱码。
"""
import sys

_UTF8_BOM = b"\xef\xbb\xbf"


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
    try:
        head = bytes(peek(4)[:4])
    except Exception:
        return default
    if not head or head in (_UTF8_BOM[: len(head)],):
        return "utf-8"
    if head.isascii():
        # ASCII 在两种编码下相同，沿用默认
        return default
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


def configure_stdio() -> None:
    """输出统一 UTF-8；stdin 管道按内容判断 UTF-8 / locale（无法切换的流跳过）。"""
    _reconfigure("stdout", "utf-8")
    _reconfigure("stderr", "utf-8")
    stream = getattr(sys, "stdin", None)
    default = getattr(stream, "encoding", None) or "utf-8"
    _reconfigure("stdin", _pick_stdin_encoding(default))
