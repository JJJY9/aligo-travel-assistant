"""行程按出发日期排序的共享口径（MemoryManager / memory-query 技能共用）。

两个后端的 ``get_trip_history`` 都按插入序返回，而备份恢复会把旧行程追加
到表尾：直接取尾部会把真实近期行程挤出窗口（跨会话总结与记忆查询都会
把用户描述成近期都在旧年份出行）。
"""
import re
from datetime import date

from utils.timestamps import chronological_key

_DATE_TEXT_RE = re.compile(r"^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})")


def normalized_date_key(value: object) -> str:
    """把常见历史日期写法（ISO / 未补零 / 斜杠 / 中文）归一成可比字符串。

    无法解析返回空串（排最前）：备份恢复允许原样保留旧格式日期，直接按
    字符串比较会把 3 月排到 12 月之后、斜杠日期排到最后。
    """
    text = str(value or "").strip()
    match = _DATE_TEXT_RE.match(text)
    if not match:
        return ""
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return ""


def trip_sort_key(trip: dict) -> tuple[str, str, str]:
    """按出发日期排序（缺失 / 非法日期排最前），最近行程取窗口尾部。

    第二键用归一后的时间序：JSON 后端实时写入 naive 本地时间，备份恢复 /
    同身份更新写入带偏移的 aware 串，两种格式混存时原始字符串的字典序
    不是时间序，同日行程的入选与提示词顺序会错。
    """
    return (
        normalized_date_key(trip.get("start_date")),
        chronological_key(trip.get("timestamp")),
        str(trip.get("trip_id") or ""),
    )
