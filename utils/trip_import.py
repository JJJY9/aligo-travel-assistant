"""行程导入（`trips-export` 的逆操作）。

- CSV：解析 trips-export 导出的 CSV（UTF-8 BOM，中文或英文字段名），逐行
  校验日期与必填字段；非法行收集为错误而不是中断整个文件。
- iCalendar：解析 trips-export --ics 导出的事件（RFC 5545 折行 / 转义、
  排他 DTEND），同样尽最大努力收集错误。
重复行程沿用存储层的去重语义（同路线 + 同起止日期更新目的）。
"""
import csv
import io
import re
from datetime import datetime, timedelta
from typing import Any

_HEADER_ALIASES = {
    "origin": {"出发地", "origin"},
    "destination": {"目的地", "destination"},
    "start_date": {"出发日期", "start_date"},
    "end_date": {"返程日期", "end_date"},
    "purpose": {"目的", "purpose"},
}


def _normalize_header(cell: str) -> str:
    return str(cell or "").strip().lstrip("﻿")


def _strict_date(value: str) -> "datetime | None":
    """严格 YYYY-MM-DD：不补零写法（2026-1-1）视为非法，避免绕过去重。"""
    try:
        parsed = datetime.strptime(str(value), "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    if parsed.strftime("%Y-%m-%d") != str(value):
        return None
    return parsed


def parse_trips_csv(text: str) -> tuple[list[dict[str, Any]], list[tuple[int, str]]]:
    """解析并校验行程 CSV，返回 ``(有效行程, [(行号, 原因)])``（行号从 2 起）。"""
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return [], [(1, "文件为空")]

    mapping: dict[str, int] = {}
    for index, header_cell in enumerate(header):
        name = _normalize_header(header_cell)
        for key, aliases in _HEADER_ALIASES.items():
            if name in aliases and key not in mapping:
                mapping[key] = index
                break
    missing = {"destination", "start_date"} - set(mapping)
    if missing:
        return [], [(1, f"缺少必需列：{'、'.join(sorted(missing))}")]

    def cell(row: list[str], key: str) -> str:
        index = mapping.get(key)
        if index is None or index >= len(row):
            return ""
        return str(row[index] or "").strip()

    rows: list[dict[str, Any]] = []
    errors: list[tuple[int, str]] = []
    for line_no, row in enumerate(reader, start=2):
        if not row or all(not str(item or "").strip() for item in row):
            continue
        destination = cell(row, "destination")
        start_date = cell(row, "start_date")
        end_date = cell(row, "end_date")
        if not destination:
            errors.append((line_no, "目的地为空"))
            continue
        start_dt = _strict_date(start_date)
        if start_dt is None:
            errors.append((line_no, f"出发日期格式应为 YYYY-MM-DD：{start_date}"))
            continue
        if end_date:
            end_dt = _strict_date(end_date)
            if end_dt is None:
                errors.append((line_no, f"返程日期格式应为 YYYY-MM-DD：{end_date}"))
                continue
            if end_dt < start_dt:
                errors.append((line_no, "结束日期不能早于出发日期"))
                continue
        rows.append(
            {
                "origin": cell(row, "origin"),
                "destination": destination,
                "start_date": start_date,
                "end_date": end_date,
                "purpose": cell(row, "purpose") or "出差",
            }
        )
    return rows, errors


def _unfold_ics(text: str) -> list[str]:
    """按 RFC 5545 展开折行：续行以单个空格 / 制表符开头，属上一行。"""
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines: list[str] = []
    for line in raw:
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _ics_unescape(value: str) -> str:
    """还原 RFC 5545 文本转义（反斜杠序列还原为字面量或换行）。"""
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            nxt = value[index + 1]
            if nxt in ("n", "N"):
                out.append("\n")
            else:
                # \ ; , 还原为字面量；未知转义同样按字面保留（容错外部日历）
                out.append(nxt)
            index += 2
        else:
            out.append(char)
            index += 1
    return "".join(out)


def _ics_prop_date(value: str) -> "datetime | None":
    """解析 DATE / DATE-TIME 属性值的前 8 位日期（YYYYMMDD）。"""
    text = str(value or "").strip()
    if len(text) >= 8 and text[:8].isdigit():
        try:
            return datetime.strptime(text[:8], "%Y%m%d")
        except ValueError:
            return None
    return None


_ALIGO_DESCRIPTION = re.compile(
    r"^Aligo 行程：(?P<origin>.*?) → (?P<destination>.*?)，"
    r"(?P<start>\d{4}-\d{2}-\d{2}) ~ (?P<end>\d{4}-\d{2}-\d{2})，(?P<purpose>.*)$",
    re.S,
)
_ICS_SUMMARY = re.compile(
    r"^(?P<origin>.*?) → (?P<destination>.*)（(?P<purpose>.*)）$", re.S
)


def _ics_event_row(props: dict[str, str]) -> tuple[dict[str, Any] | None, str | None]:
    """把一个 VEVENT 的属性映射为行程行；无法使用时返回 (None, 原因)。"""
    start_dt = _ics_prop_date(props.get("DTSTART", ""))
    if start_dt is None:
        return None, "缺少或非法的 DTSTART"
    start_date = start_dt.strftime("%Y-%m-%d")
    end_date = ""
    end_dt = _ics_prop_date(props.get("DTEND", ""))
    if end_dt is not None and end_dt >= start_dt:
        # DTEND 是排他日期：单日行程 DTEND=DTSTART+1，记为同日开始结束
        inclusive = end_dt - timedelta(days=1) if end_dt > start_dt else start_dt
        end_date = start_date if inclusive == start_dt else inclusive.strftime("%Y-%m-%d")

    description = _ics_unescape(props.get("DESCRIPTION", "")).strip()
    summary = _ics_unescape(props.get("SUMMARY", "")).strip()
    origin = destination = purpose = ""
    match = _ALIGO_DESCRIPTION.match(description)
    if match:
        origin = match.group("origin").strip()
        destination = match.group("destination").strip()
        purpose = match.group("purpose").strip()
        # 日期以 DTSTART / DTEND 为准：DESCRIPTION 里的时间段可能被外部日历
        # 手工编辑过，与实际事件不一致
    else:
        match = _ICS_SUMMARY.match(summary)
        if match:
            origin = match.group("origin").strip()
            destination = match.group("destination").strip()
            purpose = match.group("purpose").strip()
        else:
            destination = summary
    if not destination:
        return None, "缺少目的地（SUMMARY 为空）"
    return {
        "origin": origin,
        "destination": destination,
        "start_date": start_date,
        "end_date": end_date,
        "purpose": purpose or "出差",
    }, None


def parse_trips_ics(text: str) -> tuple[list[dict[str, Any]], list[tuple[int, str]]]:
    """解析 iCalendar 文本，返回 ``(有效行程, [(事件序号, 原因)])``（序从 1 起）。"""
    props: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    errors: list[tuple[int, str]] = []
    in_event = False
    index = 0
    for line in _unfold_ics(text):
        upper = line.strip().upper()
        if upper == "BEGIN:VEVENT":
            in_event = True
            props = {}
            continue
        if upper == "END:VEVENT":
            in_event = False
            index += 1
            row, reason = _ics_event_row(props)
            if reason:
                errors.append((index, reason))
            elif row is not None:
                rows.append(row)
            continue
        if not in_event or ":" not in line:
            continue
        name = line.partition(":")[0].split(";", 1)[0].strip().upper()
        if name in {"DTSTART", "DTEND", "SUMMARY", "DESCRIPTION"}:
            # VALARM 里也有 DESCRIPTION：setdefault 保留事件级（先出现）的值
            props.setdefault(name, line.partition(":")[2].strip())
    if not rows and not errors:
        return [], [(0, "未找到 VEVENT 事件")]
    return rows, errors
