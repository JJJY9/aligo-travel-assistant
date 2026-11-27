"""时间戳归一与排序工具。

存储层可能同时存在两种时间戳：JSON 后端写入的 naive 本地时间
（``datetime.now().isoformat()``）与 PostgreSQL 读回的带时区 ISO。直接按
字符串比较会把不同偏移的同一时刻排错（例如 ``10:00+00:00`` 与
``12:00+08:00`` 是同一时刻，但字符串序不同），这里统一换算成 UTC 再比较。

naive 时间戳按「该时刻」的本机时区解释（含夏令时）：不能用
``datetime.now().astimezone().tzinfo``——那是一个定值偏移，夏令时地区在
7 月运行时会用 +02:00 解释 1 月的墙钟时间，排序相差一小时。
"""
import re
from datetime import datetime, timezone
from typing import Any

# ISO 8601 基本格式（``+0800`` / ``+08``）与逗号小数只在 3.11 起被
# ``datetime.fromisoformat`` 接受；项目支持 3.10，同一份备份在 3.10 上
# 解析失败（排序键为空、导入丢时间），3.12 上却正常。这里在解析前做一次
# 受控归一：偏移统一成 ``±HH:MM``、小数统一成点号且补齐 6 位，
# 只改写「整串是带时分秒的本地时间 + 偏移」/「小数后紧跟偏移或结尾」的形态，
# date-only 与 naive 时间戳不受影响。
_OFFSET_DT = re.compile(
    r"^(?P<base>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d*)?)?)"
    r"(?P<off>[+-]\d{2}(?::?\d{2})?)$"
)
_FRACTION = re.compile(r"\.(\d+)(?=[+-]\d{2}|$)")


def _normalize_iso(text: str) -> str:
    """把 3.11+ 才接受的 ISO 8601 写法归一到 3.10 也能解析的形式。"""
    # 逗号小数 → 点小数（只替换数字之间的逗号，避免误伤任意文本）
    text = re.sub(r"(?<=\d),(?=\d)", ".", text)
    match = _OFFSET_DT.match(text)
    if match is not None:
        offset = match.group("off")
        sign, digits = offset[0], offset[1:].replace(":", "")
        hours = digits[:2]
        minutes = digits[2:4] if len(digits) >= 4 else "00"
        text = f"{match.group('base')}{sign}{hours}:{minutes}"
    # 3.10 的 fromisoformat 只接受 3 或 6 位小数：高位截断到 6 位后再补齐
    text = _FRACTION.sub(
        lambda found: "." + found.group(1)[:6].ljust(6, "0"), text
    )
    return text


def parse_iso_datetime(value: Any) -> "datetime | None":
    """解析 ISO 8601 时间戳；非法 / 缺失返回 None。

    Python 3.10 的 ``datetime.fromisoformat`` 不接受 ``Z`` 后缀、
    ``+0800`` / ``+08`` 偏移与逗号小数（3.11 起才支持），而它们都是
    合法的 ISO 8601 写法。先归一（见 :func:`_normalize_iso`）再解析，
    保证同一份数据在受支持的 3.10–3.12 上结果一致。
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    text = _normalize_iso(text)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def localize_naive(parsed: datetime) -> datetime:
    """给 naive 本地墙钟时间补上该时刻的本机 UTC 偏移（按当日 DST 规则）。

    ``datetime.timestamp()`` 用 mktime 语义把 naive 视为本地时间，夏令时
    切换日之前的墙钟时间会得到当时的偏移；再用「本地墙钟 - UTC 墙钟」
    求出该时刻的实际偏移。极端日期（Windows 上早于 1970）回退到当前偏移。
    """
    try:
        epoch = parsed.timestamp()
        local_wall = datetime.fromtimestamp(epoch)
        utc_wall = datetime.fromtimestamp(epoch, tz=timezone.utc).replace(
            tzinfo=None
        )
        return parsed.replace(tzinfo=timezone(local_wall - utc_wall))
    except (OSError, OverflowError, ValueError):
        return parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)


def to_aware(value: Any) -> "datetime | None":
    """解析为带时区 datetime；naive 按本机时区解释，非法 / 缺失返回 None。"""
    parsed = parse_iso_datetime(value)
    if parsed is not None and parsed.tzinfo is None:
        parsed = localize_naive(parsed)
    return parsed


def chronological_key(value: Any) -> str:
    """可比较的排序键：换算成 UTC 的 ISO 文本；非法 / 缺失排在最前。"""
    aware = to_aware(value)
    if aware is None:
        return ""
    try:
        return aware.astimezone(timezone.utc).isoformat()
    except (OverflowError, ValueError, OSError):
        # 极端年份（如 0001-01-01+08:00）换算 UTC 会越出 datetime 范围：
        # 与解析失败同一口径排在最前，不能让排序键把整份导出 / 导入打崩
        return ""
