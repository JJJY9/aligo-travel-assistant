"""大小写不敏感的关键词查找（CLI 与 Web 共用同一口径）。

Web 会话搜索与 CLI `sessions --query` 曾各用一套实现：CLI 直接
``needle in content.lower()``，遇土耳其语 ``İ`` 这样小写后长度变化的字符
会漏掉整条会话；Web 的 ``_ci_find`` 在长度变化时回退到 ``re.IGNORECASE``
在原文定位。这里把 Web 实现抽成共享模块，两个入口保持一致。
"""
from __future__ import annotations

import re


def ci_find(haystack: str, needle: str) -> int:
    """大小写不敏感查找，返回**原文**下标；未命中返回 -1。

    ``lower()`` 可能改变字符串长度（如土耳其语 ``İ`` 小写后是两个码点），
    用 lower 串里的下标去切原文会让窗口整体错位；长度变化时改用正则
    在原文上定位（re.IGNORECASE 返回的就是原文下标）。

    无大小写区分的关键字（中文 / 数字 / 符号）忽略大小写等价于精确查找：
    直接走 C 层 ``str.find``，不再对每条未命中的消息做 re.escape+re.search
    （会话搜索逐条扫描时这是主要开销）。
    """
    if not needle:
        return -1
    if needle == needle.lower() == needle.upper():
        return haystack.find(needle)
    lowered = haystack.lower()
    index = lowered.find(needle.lower())
    if index >= 0 and len(lowered) == len(haystack):
        return index
    match = re.search(re.escape(needle), haystack, re.IGNORECASE)
    return match.start() if match else -1
