"""时间戳归一与排序工具。

存储层可能同时存在两种时间戳：JSON 后端写入的 naive 本地时间
（``datetime.now().isoformat()``）与 PostgreSQL 读回的带时区 ISO。直接按
字符串比较会把不同偏移的同一时刻排错（例如 ``10:00+00:00`` 与
``12:00+08:00`` 是同一时刻，但字符串序不同），这里统一换算成 UTC 再比较。
"""
from datetime import datetime, timezone
from typing import Any


def to_aware(value: Any) -> "datetime | None":
    """解析为带时区 datetime；naive 按本机时区解释，非法 / 缺失返回 None。"""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def chronological_key(value: Any) -> str:
    """可比较的排序键：换算成 UTC 的 ISO 文本；非法 / 缺失排在最前。"""
    aware = to_aware(value)
    if aware is None:
        return ""
    return aware.astimezone(timezone.utc).isoformat()
