"""直跑清单的 UTF-8 输出守卫。

Windows 控制台 / 重定向管道默认 GBK：清单里的 ✓ 等字符会抛
UnicodeEncodeError，把执行截断在第一个用例。``ensure_utf8()`` 把
stdout / stderr 切到 UTF-8（流不支持 reconfigure 时保持原状）。
"""
import sys


def ensure_utf8() -> None:
    try:
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if callable(reconfigure):
                reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        pass
