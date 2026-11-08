"""行程导入（`trips-export` 的逆操作）。

- CSV：解析 trips-export 导出的 CSV（UTF-8 BOM，中文或英文字段名），逐行
  校验日期与必填字段；非法行收集为错误而不是中断整个文件。
- iCalendar：解析 trips-export --ics 导出的事件（RFC 5545 折行 / 转义、
  排他 DTEND），Z / ±HHMM 的 DATE-TIME 先换算本机时区再取日期，同样尽最大
  努力收集错误。
重复行程沿用存储层的去重语义（同路线 + 同起止日期更新目的）。
"""
import csv
import io
import re
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from utils.trip_export import _needs_quote_guard

_HEADER_ALIASES = {
    "origin": {"出发地", "origin"},
    "destination": {"目的地", "destination"},
    "start_date": {"出发日期", "start_date"},
    "end_date": {"返程日期", "end_date"},
    "purpose": {"目的", "purpose"},
}


def _normalize_header(cell: str) -> str:
    return str(cell or "").strip().lstrip("﻿")


def normalize_identity_date(value: Any) -> str:
    """去重键里的日期归一：YYYY-M-D 补零到 YYYY-MM-DD。

    历史数据可能保留未补零写法（如 2026-1-5），导入端会补零；不归一
    比对会把同一行程当成新记录重复插入。其它格式原样保留。
    """
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if not match:
        return text
    month = int(match.group(2))
    day = int(match.group(3))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return text
    return f"{match.group(1)}-{month:02d}-{day:02d}"


def trip_identity(row: dict[str, Any]) -> tuple[str, str, str, str]:
    """存储层「同路线 + 同起止日期」去重键（返程为空视为与出发同日）。"""
    start = normalize_identity_date(row.get("start_date"))
    return (
        str(row.get("destination") or ""),
        str(row.get("origin") or ""),
        start,
        normalize_identity_date(row.get("end_date")) or start,
    )


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
    # strict=True：引号错位（如 `C,"D,2026-01-02` 后跟另一行带引号）时
    # 非严格模式会把后续多行静默合并成一条记录、零错误地导入错数据；
    # 严格模式走下面的 csv.Error 分支，保留已解析行并报出行号
    reader = csv.reader(io.StringIO(text), strict=True)
    try:
        header = next(reader)
    except StopIteration:
        return [], [(1, "文件为空")]
    except csv.Error as exc:
        # 单字段超过 field_size_limit（默认 131072 字符）时 csv 直接抛错：
        # 按解析错误返回，不能穿透成 traceback、让已解析行全部丢失
        return [], [(1, f"CSV 解析失败：{exc}")]

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
        value = str(row[index] or "").strip()
        # 反转 trips-export 为防 CSV 公式注入加的前导单引号
        if value.startswith("'") and _needs_quote_guard(value[1:]):
            value = value[1:]
        return value

    rows: list[dict[str, Any]] = []
    errors: list[tuple[int, str]] = []
    while True:
        try:
            row = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            # 超长字段 / 未闭合引号把余下内容吞成同一字段：保留已解析行，
            # 记录错误后停止（与「非法行收集后列出并跳过」的既有契约一致）
            errors.append((reader.line_num, f"CSV 解析失败：{exc}"))
            break
        # csv.reader 把带引号的多行字段算作一条记录：enumerate 的序号不是
        # 物理行号，用 reader.line_num 才能让报错指向用户看到的行
        line_no = reader.line_num
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
                # 缺省 / 空白目的保留 None：去重更新时存储层保留原值
                "purpose": cell(row, "purpose") or None,
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


_ICS_DT = re.compile(
    r"^(?P<date>\d{8})"
    r"(?:T(?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2})?)?"
    r"(?P<tz>Z|[+-]\d{4})?$"
)


def _ics_local_datetime(
    value: str, tzid: str = ""
) -> "tuple[datetime, bool] | None":
    """解析 DATE / DATE-TIME 属性值，返回 (本机墙钟时间, 是否含时间成分)。

    Z / ±HHMM / ``TZID`` 的 DATE-TIME 先换算到本机时区再取日期：UTC 23:30
    在东八区已是第二天，只截前 8 位日期会把行程整体记错一天；带 TZID 的
    浮动写法（Outlook）与同一时刻的 UTC 写法（Google）必须得到同一天。
    """
    text = str(value or "").strip()
    match = _ICS_DT.match(text)
    if match is None:
        return None
    has_time = match.group("hour") is not None
    try:
        if has_time:
            parsed = datetime(
                int(match.group("date")[:4]),
                int(match.group("date")[4:6]),
                int(match.group("date")[6:8]),
                int(match.group("hour")),
                int(match.group("minute")),
                int(match.group("second") or 0),
            )
        else:
            parsed = datetime.strptime(match.group("date"), "%Y%m%d")
    except ValueError:
        return None
    tz = match.group("tz")
    if tz and has_time:
        if tz == "Z":
            offset = timezone.utc
        else:
            hours = int(tz[1:3])
            minutes = int(tz[3:5])
            if hours > 23 or minutes > 59:
                # RFC 5545 偏移最大 ±23:59：timezone() 对 ≥24 小时直接抛
                # ValueError，一个坏事件会让整份 ICS 导入崩溃；按浮动时间处理
                return parsed, has_time
            sign = 1 if tz[0] == "+" else -1
            offset = timezone(sign * timedelta(hours=hours, minutes=minutes))
        try:
            parsed = parsed.replace(tzinfo=offset).astimezone().replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            # 极端年份（如 0001-01-01+14:00）换算本机时区越界：按非法
            # 时间跳过该事件，整份导入不中断
            return None
    elif tzid and has_time:
        try:
            zone = ZoneInfo(tzid)
        except (KeyError, ValueError, OSError):
            # 未安装的时区库 / 非法 TZID：按浮动时间处理（不中断整份导入）
            return parsed, has_time
        try:
            parsed = parsed.replace(tzinfo=zone).astimezone().replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None
    return parsed, has_time


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
    start_parsed = _ics_local_datetime(
        props.get("DTSTART", ""), props.get("DTSTART_TZID", "")
    )
    if start_parsed is None:
        return None, "缺少或非法的 DTSTART"
    start_dt, _start_has_time = start_parsed
    start_day = start_dt.date()
    start_date = start_dt.strftime("%Y-%m-%d")
    end_date = ""
    end_parsed = _ics_local_datetime(
        props.get("DTEND", ""), props.get("DTEND_TZID", "")
    )
    if end_parsed is not None:
        end_dt, end_has_time = end_parsed
        if end_dt >= start_dt:
            if end_has_time:
                # DATE-TIME 的结束时刻包含在内：只有恰好 00:00 才是
                # 「排他日期」，按全天规则减一天会提前结束跨日定时事件
                if end_dt.date() > start_day and end_dt.time() == time(0, 0):
                    inclusive = end_dt.date() - timedelta(days=1)
                else:
                    inclusive = end_dt.date()
            elif end_dt > start_dt:
                # VALUE=DATE 的 DTEND 是排他日期：单日行程 DTEND=DTSTART+1
                inclusive = end_dt.date() - timedelta(days=1)
            else:
                inclusive = start_day
            if inclusive < start_day:
                inclusive = start_day
            end_date = inclusive.strftime("%Y-%m-%d")

    description = _ics_unescape(props.get("DESCRIPTION", "")).strip()
    summary = _ics_unescape(props.get("SUMMARY", "")).strip()
    origin = destination = purpose = ""
    # 正则只扫描前缀：两个模式都含非贪婪 .*? 加尾部字面量，攻击者用大量
    # 「 → 」/缺少闭合括号的文本可触发二次方回溯（1M 字符可卡住数十分钟）。
    # 本工具生成的 DESCRIPTION / SUMMARY 都在数百字符内，超长前缀不可能是
    # 自己的格式。
    match = _ALIGO_DESCRIPTION.match(description[:2000])
    if match:
        origin = match.group("origin").strip()
        destination = match.group("destination").strip()
        purpose = match.group("purpose").strip()
        # 日期以 DTSTART / DTEND 为准：DESCRIPTION 里的时间段可能被外部日历
        # 手工编辑过，与实际事件不一致
    else:
        match = _ICS_SUMMARY.match(summary[:400])
        if match:
            origin = match.group("origin").strip()
            destination = match.group("destination").strip()
            purpose = match.group("purpose").strip()
        else:
            destination = summary[:400]
    if not destination:
        return None, "缺少目的地（SUMMARY 为空）"
    return {
        "origin": origin,
        "destination": destination,
        "start_date": start_date,
        "end_date": end_date,
        # 未提供目的时保留 None：去重更新时存储层保留原值，不会把用户
        # 手动改过的目的覆盖成默认「出差」
        "purpose": purpose or None,
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
        left, _, raw_value = line.partition(":")
        params = left.split(";")
        name = params[0].strip().upper()
        if name in {"DTSTART", "DTEND", "SUMMARY", "DESCRIPTION"}:
            # VALARM 里也有 DESCRIPTION：setdefault 保留事件级（先出现）的值
            props.setdefault(name, raw_value.strip())
            if name in {"DTSTART", "DTEND"}:
                # 属性参数里的 TZID 决定 DATE-TIME 的时区（Outlook 导出
                # 常见）；丢弃它会把同一时刻记成不同的日期
                for param in params[1:]:
                    key, _, param_value = param.partition("=")
                    if key.strip().upper() == "TZID":
                        props.setdefault(f"{name}_TZID", param_value.strip())
    if not rows and not errors:
        return [], [(0, "未找到 VEVENT 事件")]
    return rows, errors


# 单次导入行数上限：批量保存是 O(N²)（JSON 每条重扫历史 / PG 每条查候选
# 并扫 trip_id），且整批持用户锁；5000 条在本地实测约 10 秒，与备份恢复
# 的 _MAX_TRIPS 同一量级，避免一个文件把对话 / 行程写入阻塞数分钟。
_MAX_IMPORT_ROWS = 5000
# 解析错误只在结果里保留前 50 条（HTTP 400 只用前 5 条原因）：一个 1MB
# 的坏文件可能有数十万条错误，全量物化会占用上百 MB 内存。
_MAX_SKIPPED = 50
# 原始文本上限：Web 端有 50 万字符的 content 上限，但 CLI 直接读文件；
# 解析前先拒绝超大文本，避免几十 MB 文件在解析 / 错误列表上占满内存。
_MAX_IMPORT_CHARS = 5_000_000


def _bad_import_text(value: Any) -> bool:
    """行程字段是否含不能入库的文本（NUL / 孤立代理项）。"""
    if not isinstance(value, str):
        return False
    if "\x00" in value:
        return True
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def import_trips_from_text(
    store: Any,
    text: str,
    dry_run: bool = False,
    force_ics: bool = False,
) -> dict[str, Any]:
    """解析 CSV / iCalendar 文本并写入行程（与 CLI ``trips-import`` 同语义）。

    - 内容以 ``BEGIN:VCALENDAR`` 开头（容忍 BOM / 空白）时按 ICS 解析，否则按 CSV；
    - 重复行程沿用存储层去重（同路线 + 同起止日期更新目的）；导入未提供目的
      时不覆盖已有行程的目的，新建行程补默认「出差」；
    - 单次最多 5000 行；字段含 NUL / 孤立代理项时整份拒绝（与其它写接口同口径）；
    - ``dry_run=True`` 只按同一去重键模拟新增 / 更新，不写库。

    Returns:
        ``{"dry_run", "format": "ics"|"csv", "rows", "created", "updated",
        "skipped": [{"line", "reason"}]（最多 50 条）, "skipped_total"}``。
    """
    if len(str(text or "")) > _MAX_IMPORT_CHARS:
        raise ValueError(
            f"导入文件过大（超过 {_MAX_IMPORT_CHARS} 字符），请拆分后再导入"
        )
    normalized = str(text or "").lstrip("﻿ \t\r\n")
    is_ics = force_ics or normalized.upper().startswith("BEGIN:VCALENDAR")
    if is_ics:
        rows, errors = parse_trips_ics(text)
    else:
        rows, errors = parse_trips_csv(text)
    result: dict[str, Any] = {
        "dry_run": dry_run,
        "format": "ics" if is_ics else "csv",
        "rows": len(rows),
        "created": 0,
        "updated": 0,
        "skipped": [
            {"line": line, "reason": reason} for line, reason in errors[:_MAX_SKIPPED]
        ],
        "skipped_total": len(errors),
    }
    if not rows:
        return result
    if len(rows) > _MAX_IMPORT_ROWS:
        raise ValueError(
            f"单次最多导入 {_MAX_IMPORT_ROWS} 条行程，请拆分文件后再导入"
        )
    for row in rows:
        for field in ("origin", "destination", "purpose"):
            if _bad_import_text(row.get(field)):
                raise ValueError("行程内容包含 NUL 或非法 Unicode 字符，已拒绝导入")

    # 新建行程才补默认目的；已存在（含本批前一行刚新建）的行程保留原目的。
    # 只在写入前扫一次库；批内同身份行必须按顺序加入 pending，否则后一行
    # 会被误判为「新行程」补默认值，覆盖前一行显式给出的目的。
    existing_keys = {
        trip_identity(trip) for trip in store.get_trip_history(limit=None)
    }
    pending_keys = set(existing_keys)
    for row in rows:
        key = trip_identity(row)
        if row.get("purpose") is None and (key is None or key not in pending_keys):
            row["purpose"] = "出差"
        if key is not None:
            pending_keys.add(key)

    if dry_run:
        pending = set(existing_keys)
        created = updated = 0
        for row in rows:
            key = trip_identity(row)
            if key in pending:
                updated += 1
            else:
                created += 1
                pending.add(key)
        result["created"], result["updated"] = created, updated
        return result
    created, updated = store.save_trip_histories(rows)
    result["created"], result["updated"] = int(created), int(updated)
    return result
