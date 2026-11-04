"""
控制台编码兼容

Windows 中文环境的默认控制台编码为 GBK，直接 print("✓ ...") 或 emoji 会抛
UnicodeEncodeError（cmd / PowerShell、未设置 PYTHONIOENCODING 时必现）。
各入口（CLI / Web / 初始化脚本）启动时调用 configure_stdio() 统一切换为 UTF-8。
"""
import sys


def configure_stdio() -> None:
    """将 stdin / stdout / stderr 切换为 UTF-8（无法切换的流自动跳过）。

    Windows 下管道输入默认按 locale（GBK）解码，UTF-8 管道（Git Bash / CI）
    会变成乱码；与输出统一为 UTF-8，真实控制台（PEP 528）本来就是 UTF-8。
    """
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
