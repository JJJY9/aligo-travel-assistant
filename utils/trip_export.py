"""
行程导出渲染（Web API 与 CLI 共用）

- CSV：输出 UTF-8 BOM，便于 Excel 直接打开中文表头；对以 = + - @ 等开头的
  内容加前导单引号，防止 CSV 公式注入；
- iCalendar（ICS）：全天事件 + 前一天提醒，Web 的行程日历接口与
  `python cli.py trips-export --ics` 共用同一实现。
"""
import csv
import hashlib
import io
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _needs_quote_guard(text: str) -> bool:
    """以公式字符开头，或去掉一个前导引号后仍需防护（保证导入端可逆）。"""
    if text.startswith(_FORMULA_PREFIXES):
        return True
    return text.startswith("'") and _needs_quote_guard(text[1:])


def _csv_safe(value: object) -> str:
    text = str(value or "")
    return "'" + text if _needs_quote_guard(text) else text


def render_trips_csv(trips: list[dict[str, Any]]) -> str:
    """把行程列表渲染为带 BOM 的 CSV 文本（含表头）。"""
    buffer = io.StringIO()
    # 固定用 \n 作行结束符：Windows 文本模式写文件时会把 \n 翻译成 \r\n，
    # csv.writer 默认的 \r\n 会被二次翻译成 \r\r\n 并多出空记录
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["出发地", "目的地", "出发日期", "返程日期", "目的"])
    for trip in trips:
        writer.writerow(
            [
                _csv_safe(trip.get("origin")),
                _csv_safe(trip.get("destination")),
                _csv_safe(trip.get("start_date")),
                _csv_safe(trip.get("end_date")),
                _csv_safe(trip.get("purpose")),
            ]
        )
    return "\ufeff" + buffer.getvalue()


def _ics_date(value: str) -> str | None:
    """把 YYYY-MM-DD 转为 iCalendar 日期（YYYYMMDD）；非法值返回 None。"""
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").strftime("%Y%m%d")
    except (TypeError, ValueError):
        return None


def _ics_escape(value: str) -> str:
    """按 RFC 5545 转义文本字段（含孤立的 CR，避免破坏行结构）。"""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\r", "\\n")
        .replace("\n", "\\n")
    )


def _trip_ics_event_lines(trip: dict[str, Any], user_id: str) -> list[str] | None:
    """单条行程的 VEVENT 行；缺少出发日期时返回 None。"""
    start = _ics_date(trip.get("start_date") or "")
    if not start:
        return None
    end = _ics_date(trip.get("end_date") or "") or start
    if end < start:
        logger.warning("行程结束日期早于出发日期，按单日处理: %s", trip.get("trip_id"))
        end = start
    end_dt = datetime.strptime(end, "%Y%m%d")
    try:
        # ICS 的 DTEND 是排他的；9999-12-31 无法再加一天
        end_exclusive = (end_dt + timedelta(days=1)).strftime("%Y%m%d")
    except OverflowError:
        end_exclusive = end

    origin = str(trip.get("origin") or "出发地")
    destination = str(trip.get("destination") or "目的地")
    purpose = str(trip.get("purpose") or "出行")
    summary = f"{origin} → {destination}（{purpose}）"
    # DESCRIPTION 是 trips-import 的机器可读来源：必须用原始值（占位符只
    # 出现在给人看的 SUMMARY 里），否则空出发地在往返后会变成字面量「出发地」，
    # 去重键随之改变并重复新增行程
    description = (
        f"Aligo 行程：{str(trip.get('origin') or '')} → "
        f"{str(trip.get('destination') or '')}，"
        f"{trip.get('start_date')} ~ {trip.get('end_date') or trip.get('start_date')}，"
        f"{str(trip.get('purpose') or '')}"
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # UID 同时包含行程内容：删除最新行程后 id 可能被复用，仅用 trip_id
    # 会让新行程覆盖日历里已存在的旧事件
    uid_source = ":".join(
        [
            user_id,
            str(trip.get("trip_id") or "trip"),
            origin,
            destination,
            str(trip.get("start_date") or ""),
            str(trip.get("end_date") or ""),
        ]
    )
    uid_digest = hashlib.sha1(uid_source.encode("utf-8")).hexdigest()[:16]
    uid = f"aligo-{uid_digest}@aligo.local"

    return [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{stamp}",
        f"DTSTART;VALUE=DATE:{start}",
        f"DTEND;VALUE=DATE:{end_exclusive}",
        f"SUMMARY:{_ics_escape(summary)}",
        f"DESCRIPTION:{_ics_escape(description)}",
        # 全天事件的 DTSTART 是出发日 00:00，提前 15 小时即前一天 09:00 提醒
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        f"DESCRIPTION:{_ics_escape(f'出行提醒：明天 {origin} → {destination}')}",
        "TRIGGER:-PT15H",
        "END:VALARM",
        "END:VEVENT",
    ]


def _ics_fold(line: str) -> str:
    """按 RFC 5545 把超过 75 字节的行折行（续行以单个空格开头）。

    折行位置不能劈开 UTF-8 字符：回退到完整字符边界。中文 SUMMARY 等
    长文本不折行会被部分日历客户端拒收或截断。
    """
    data = line.encode("utf-8")
    if len(data) <= 75:
        return line
    chunks: list[bytes] = []
    limit = 75
    while len(data) > limit:
        cut = limit
        while cut > 0 and (data[cut] & 0xC0) == 0x80:
            cut -= 1
        chunks.append(data[:cut])
        data = data[cut:]
        limit = 74  # 续行开头的空格占 1 字节
    chunks.append(data)
    return "\r\n ".join(chunk.decode("utf-8") for chunk in chunks)


def _ics_calendar(events: list[list[str]]) -> str:
    """把若干 VEVENT 行组合为完整的 iCalendar 文本。"""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Aligo//Travel Assistant//CN",
        "CALSCALE:GREGORIAN",
    ]
    for event in events:
        lines.extend(event)
    lines.extend(["END:VCALENDAR", ""])
    return "\r\n".join(_ics_fold(line) for line in lines)


def render_trip_ics(trip: dict[str, Any], user_id: str) -> str | None:
    """把一条行程渲染为全天事件的 iCalendar 文本；缺少出发日期时返回 None。"""
    event = _trip_ics_event_lines(trip, user_id)
    if event is None:
        return None
    return _ics_calendar([event])


def render_trips_ics(trips: list[dict[str, Any]], user_id: str) -> str:
    """把全部行程渲染为一个 iCalendar 文件（缺少出发日期的行程跳过）。"""
    events = [
        event
        for event in (_trip_ics_event_lines(trip, user_id) for trip in trips)
        if event is not None
    ]
    return _ics_calendar(events)
