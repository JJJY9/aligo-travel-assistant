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

from utils.trip_export import (
    _MAX_ICS_X_ATTR_CHARS,
    _needs_quote_guard,
    normalize_identity_date,
    normalize_identity_text,
    unescape_ics_machine_field,
)

# 行程字段上限（与 Web / CLI 写入口径一致）：超长字段导入后无法从面板保存
# （PUT 全字段 422），ICS 往返还会被 DESCRIPTION / SUMMARY 截断成另一条行程
_MAX_TRIP_FIELD_CHARS = 100
# 解析期错误列表物化上限：一个 5M 字符的坏文件可能有上百万条错误，全量
# 物化会占用上百 MB 内存；总数仍由 total 精确统计，展示只取前 50 条
_PARSE_ERRORS_CAP = 1000
# 单次导入行数上限：批量保存是 O(N²)（JSON 每条重扫历史 / PG 每条查候选
# 并扫 trip_id），且整批持用户锁；5000 条在本地实测约 10 秒，与备份恢复
# 的 _MAX_TRIPS 同一量级，避免一个文件把对话 / 行程写入阻塞数分钟。
# 只读导出侧的回导检查可显式传 max_rows=None 关闭该上限。
_MAX_IMPORT_ROWS = 5000


class _BoundedErrors(list):
    """只物化前 ``_PARSE_ERRORS_CAP`` 条解析错误，``total`` 记录真实数量。"""

    def __init__(self) -> None:
        super().__init__()
        self.total = 0

    def add(self, item: tuple[int, str]) -> None:
        self.total += 1
        if len(self) < _PARSE_ERRORS_CAP:
            self.append(item)


_HEADER_ALIASES = {
    "origin": {"出发地", "origin"},
    "destination": {"目的地", "destination"},
    "start_date": {"出发日期", "start_date"},
    "end_date": {"返程日期", "end_date"},
    "purpose": {"目的", "purpose"},
}


def _normalize_header(cell: str) -> str:
    # 先去 BOM 再 strip：兼容「﻿ 出发地」这类 BOM 后跟空白的表头
    return str(cell or "").lstrip("﻿").strip()


def _normalize_trip_text(value: Any) -> str:
    """行程字段的行分隔符归一（保留名；口径见 ``normalize_identity_text``）。"""
    return normalize_identity_text(value)


def trip_identity(row: dict[str, Any]) -> tuple[str, str, str, str]:
    """存储层「同路线 + 同起止日期」去重键（返程为空视为与出发同日）。"""
    start = normalize_identity_date(row.get("start_date"))
    return (
        _normalize_trip_text(row.get("destination")),
        _normalize_trip_text(row.get("origin")),
        start,
        normalize_identity_date(row.get("end_date")) or start,
    )


def dedupe_identity(row: dict[str, Any]) -> tuple[str, str, str, str]:
    """去重命令（trips-dedupe）使用的身份：路线字段再折叠大小写。

    存储层的写入去重按原文比对（保留用户对目的地 / 出发地的写法差异），
    但手工去重命令针对的是「视觉上就是同一趟」的历史重复：备份恢复按
    保真保留了「 上海」/「Shanghai」这类原文，与面板后写的同一路线只差
    首尾空白或大小写。首尾空白已由 ``normalize_identity_text`` 折叠，
    这里只再折叠大小写（``casefold``）；内部空白 / 换行不折叠（
    「北京 上海」与「北京\\n上海」仍是两条不同路线）。
    """
    destination, origin, start, end = trip_identity(row)
    return (destination.casefold(), origin.casefold(), start, end)


def _strict_date(value: str) -> "datetime | None":
    """严格 YYYY-MM-DD：不补零写法（2026-1-1）视为非法，避免绕过去重。"""
    try:
        parsed = datetime.strptime(str(value), "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    if parsed.strftime("%Y-%m-%d") != str(value):
        return None
    return parsed


def parse_trips_csv(
    text: str, *, max_rows: int | None = _MAX_IMPORT_ROWS
) -> tuple[list[dict[str, Any]], list[tuple[int, str]]]:
    """解析并校验行程 CSV，返回 ``(有效行程, [(行号, 原因)])``（行号从 2 起）。

    ``max_rows=None`` 关闭行数上限：只读导出侧的回导检查需要解析超过
    单次导入上限的全量文件。
    """
    # strict=True：引号错位（如 `C,"D,2026-01-02` 后跟另一行带引号）时
    # 非严格模式会把后续多行静默合并成一条记录、零错误地导入错数据；
    # 严格模式走下面的 csv.Error 分支，保留已解析行并报出行号
    reader = csv.reader(io.StringIO(text), strict=True)
    header: list[str] = []
    header_line = 1
    while True:
        try:
            candidate = next(reader)
        except StopIteration:
            return [], [(1, "文件为空")]
        except csv.Error as exc:
            # 单字段超过 field_size_limit（默认 131072 字符）时 csv 直接抛错：
            # 按解析错误返回，不能穿透成 traceback、让已解析行全部丢失
            return [], [(max(1, reader.line_num), f"CSV 解析失败：{exc}")]
        header_line = max(1, reader.line_num)
        # 表头前的空行 / 纯空白 / 仅 BOM 记录跳过：数据区本就跳过空行，
        # 表头区不应因一个空行把整份文件判成「缺少必需列」
        if any(_normalize_header(cell) for cell in candidate):
            header = candidate
            break

    mapping: dict[str, int] = {}
    for index, header_cell in enumerate(header):
        # 英文别名按 casefold 比较（Origin / START_DATE 等同小写），中文
        # 不含大小写、不受影响
        name = _normalize_header(header_cell).casefold()
        for key, aliases in _HEADER_ALIASES.items():
            if name in aliases and key not in mapping:
                mapping[key] = index
                break
    missing = {"destination", "start_date"} - set(mapping)
    if missing:
        # 回显实际表头（限量、去控制字符）：只报内部键名时用户无法判断
        # 是列名拼错还是文件选错
        shown: list[str] = []
        for header_cell_value in header[:10]:
            text_cell = " ".join(_normalize_header(header_cell_value).split())
            if text_cell:
                shown.append(
                    "".join(ch if ch.isprintable() else " " for ch in text_cell)[:40]
                )
        actual = "、".join(shown) or "空行"
        return [], [
            (
                header_line,
                f"缺少必需列：{'、'.join(sorted(missing))}"
                f"（实际表头：{actual}；支持中文表头或 "
                "origin/destination/start_date/end_date/purpose，"
                "英文不区分大小写）",
            )
        ]

    def cell(row: list[str], key: str, *, trim: bool = False) -> str:
        index = mapping.get(key)
        if index is None or index >= len(row):
            return ""
        raw = str(row[index] or "")
        # 反转 trips-export 为防 CSV 公式注入加的前导单引号：导出端永远
        # 加在字段首字符，因此只在原始值上判断（先 strip 会吞掉受保护
        # 字段的前导空白，身份随之漂移）
        if raw.startswith("'") and _needs_quote_guard(raw[1:]):
            raw = raw[1:]
        # 日期字段容忍首尾空白；文本字段保留原文（与导出清洗的保真口径
        # 一致），纯空白由调用方归一
        return raw.strip() if trim else raw

    rows: list[dict[str, Any]] = []
    errors = _BoundedErrors()
    # 合法但可疑的跨行记录（错位引号被后文引号「闭合」时 csv 会把多条
    # 物理行解析成一条合法记录）：只告警、不拒绝，由调用方展示给用户
    warnings = _BoundedErrors()
    last_error_line = -1
    while True:
        # 记录起始物理行：csv.reader.line_num 是上一条成功记录的最后一行
        record_start = reader.line_num + 1
        try:
            row = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            if "unexpected end of data" in str(exc):
                # 引号未闭合：csv 一直读到 EOF 才报错，reader.line_num
                # 已是文件末行，record_start..末行全部被这条坏记录吞掉，
                # 之后不会再有可解析行。报出行号必须指向记录起点（末行
                # 本身通常是合法行），并按丢失的物理行数计入跳过总数
                lost = max(1, reader.line_num - record_start + 1)
                errors.add(
                    (
                        record_start,
                        f"CSV 解析失败：第 {record_start} 行开始的记录中"
                        f"引号未闭合，读到文件末尾第 {reader.line_num} 行，"
                        f"共 {lost} 个物理行未导入"
                        "（可能含多行字段，请检查该记录的引号是否成对）",
                    )
                )
                errors.total += lost - 1
                break
            # 错位引号 / 超长字段之后 csv.reader 仍能继续返回后续行：
            # 只跳过当前记录，不能 break 丢弃余下可解析的行（README 承诺
            # 「非法行收集后列出并跳过、不中断其余项」）。以行号是否推进
            # 作为继续条件，避免同一行上报错死循环
            errors.add((reader.line_num, f"CSV 解析失败：{exc}"))
            if reader.line_num <= last_error_line:
                break
            last_error_line = reader.line_num
            continue
        # csv.reader 把带引号的多行字段算作一条记录：enumerate 的序号不是
        # 物理行号，用 reader.line_num 才能让报错指向用户看到的行
        line_no = reader.line_num
        if not row or all(not str(item or "").strip() for item in row):
            continue
        destination = cell(row, "destination")
        start_date = cell(row, "start_date", trim=True)
        end_date = cell(row, "end_date", trim=True)
        origin = cell(row, "origin")
        purpose = cell(row, "purpose")
        if not destination.strip():
            errors.add((line_no, "目的地为空"))
            continue
        start_dt = _strict_date(start_date)
        if start_dt is None:
            errors.add((line_no, f"出发日期格式应为 YYYY-MM-DD：{start_date}"))
            continue
        if end_date:
            end_dt = _strict_date(end_date)
            if end_dt is None:
                errors.add((line_no, f"返程日期格式应为 YYYY-MM-DD：{end_date}"))
                continue
            if end_dt < start_dt:
                errors.add((line_no, "结束日期不能早于出发日期"))
                continue
        oversize = next(
            (
                field
                for field, value in (
                    ("origin", origin),
                    ("destination", destination),
                    ("purpose", purpose),
                )
                if len(str(value or "").strip()) > _MAX_TRIP_FIELD_CHARS
            ),
            None,
        )
        if oversize:
            errors.add((line_no, f"字段 {oversize} 超过 {_MAX_TRIP_FIELD_CHARS} 字"))
            continue
        # 跨行记录里除目的外的列出现换行：极可能是错位引号把下一条行程
        # 吞进了本行（合并后的路线 / 日期仍然「合法」，不告警则零错误）
        span_lines = reader.line_num - record_start
        if span_lines > 0:
            newline_fields = [
                key
                for key, index in mapping.items()
                if index < len(row) and "\n" in str(row[index] or "")
            ]
            suspicious = [key for key in newline_fields if key != "purpose"]
            if not suspicious and "purpose" in newline_fields:
                purpose_raw = str(row[mapping["purpose"]] or "")
                if re.search(r"\n[^\n]*,\s*\d{4}-\d{2}-\d{2}", purpose_raw):
                    suspicious = ["purpose"]
            if suspicious:
                warnings.add(
                    (
                        record_start,
                        f"第{record_start}-{reader.line_num}行被解析为同一条"
                        f"行程（{'、'.join(suspicious)} 跨行）；若本意每行"
                        "一条行程，请检查引号是否成对",
                    )
                )
        rows.append(
            {
                "origin": origin if origin.strip() else "",
                "destination": destination,
                "start_date": start_date,
                "end_date": end_date,
                # 缺省 / 纯空白目的保留 None：去重更新时存储层保留原值
                "purpose": purpose if purpose.strip() else None,
            }
        )
        if max_rows is not None and len(rows) > max_rows:
            raise ValueError(
                f"单次最多导入 {max_rows} 条行程，请拆分文件后再导入"
            )
    errors.warnings = warnings  # type: ignore[attr-defined]
    return rows, errors


def _unfold_ics(text: str) -> list[str]:
    """按 RFC 5545 展开折行：续行以单个空格 / 制表符开头，属上一行。

    按逻辑行累积片段再一次 ``join``：此前逐段 ``lines[-1] += ...`` 拼接
    不断变长的字符串，单条 5M 字符的超长折行要 20 多秒（O(n²) 复制），
    用户会以为程序卡死。
    """
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines: list[str] = []
    parts: list[str] | None = None
    for line in raw:
        if line[:1] in (" ", "\t") and parts is not None:
            parts.append(line[1:])
            continue
        if parts is not None:
            lines.append("".join(parts))
        parts = [line]
    if parts is not None:
        lines.append("".join(parts))
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
            elif nxt in ("\\", ";", ","):
                # RFC 5545 定义的转义（\\\\ \\; \\,）：还原为字面量
                out.append(nxt)
            else:
                # 未知转义（非 RFC 写法，手工编辑 / 损坏文件）：保留反斜杠，
                # 不能静默吞掉——C:\\temp 的 \\t 曾被改写成 C:temp
                out.append("\\" + nxt)
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
    value: str, tzid: str = "", user_tz: str | None = None
) -> "tuple[datetime, bool] | None":
    """解析 DATE / DATE-TIME 属性值，返回 (本机墙钟时间, 是否含时间成分)。

    Z / ±HHMM / ``TZID`` 的 DATE-TIME 先换算到目标时区再取日期：UTC 23:30
    在东八区已是第二天，只截前 8 位日期会把行程整体记错一天；带 TZID 的
    浮动写法（Outlook）与同一时刻的 UTC 写法（Google）必须得到同一天。
    ``user_tz`` 是 Web 面板上报的浏览器时区：容器常以 UTC 部署，不传时
    服务端按自身时区换算会把同一文件记成与 CLI（用户机器时区）不同的
    日期；CLI / 旧客户端不传，行为与今天完全一致。
    """
    target_zone = None
    if user_tz:
        try:
            target_zone = ZoneInfo(str(user_tz))
        except (KeyError, ValueError, OSError):
            # 浏览器上报了未安装 / 非法时区：回退进程本机时区，不中断导入
            target_zone = None
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
            aware = parsed.replace(tzinfo=offset)
            converted = (
                aware.astimezone(target_zone)
                if target_zone is not None
                else aware.astimezone()
            )
            parsed = converted.replace(tzinfo=None)
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
            aware = parsed.replace(tzinfo=zone)
            converted = (
                aware.astimezone(target_zone)
                if target_zone is not None
                else aware.astimezone()
            )
            parsed = converted.replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None
    return parsed, has_time


_ALIGO_DESCRIPTION = re.compile(
    r"^Aligo 行程：(?P<origin>.*?) → (?P<destination>.*?)，"
    r"(?P<start>\d{4}-\d{2}-\d{2}) ~ (?P<end>\d{4}-\d{2}-\d{2})，(?P<purpose>.*)$",
    re.S,
)
def _split_ics_summary(summary: str) -> dict[str, str] | None:
    """把 ``出发地 → 目的地（目的）`` 拆开；括号按配对从尾部解析。

    目的与目的地都可能含全角括号（``会议（重要）`` / ``上海（浦东）``），
    贪婪或非贪婪正则会固定拆错一侧；从结尾做括号配对扫描，最外层括号
    始终归目的，其余括号留在目的地里。
    """
    text = summary.strip()
    if not text.endswith("）"):
        return None
    depth = 0
    open_index = -1
    for index in range(len(text) - 1, -1, -1):
        char = text[index]
        if char == "）":
            depth += 1
        elif char == "（":
            depth -= 1
            if depth == 0:
                open_index = index
                break
    if open_index <= 0:
        return None
    head = text[:open_index]
    purpose = text[open_index + 1 : -1].strip()
    if " → " not in head:
        return None
    origin, _, destination = head.partition(" → ")
    return {
        "origin": origin.strip(),
        "destination": destination.strip(),
        "purpose": purpose,
    }


def _restored_end_from_props(props: dict[str, str]) -> str | None:
    """读取 X-ALIGO-TRIP-END（无法解析的原始返程日期）；非法值返回 None。

    值来自不可信文件：超长 / NUL / 孤立代理项 / 控制字符一律忽略该属性，
    回退到 DESCRIPTION 机器段与 DTEND 推导（不能因为一个附加属性把整条
    事件丢掉，否则第三方日历的脏属性会让行程静默消失）。
    """
    raw = props.get("X-ALIGO-TRIP-END")
    if raw is None:
        return None
    value = unescape_ics_machine_field(_ics_unescape(raw)).strip()
    # 与导出端 _MAX_ICS_X_ATTR_CHARS 同口径（宽于 100 字字段上限）：超限的
    # 原始返程日期仍需带回身份键，否则回导按新身份重复新增行程
    if not value or len(value) > _MAX_ICS_X_ATTR_CHARS:
        return None
    if _bad_import_text(value):
        return None
    if any(ord(char) < 0x20 and char != "\t" for char in value):
        return None
    if any(0x7F <= ord(char) <= 0x9F for char in value):
        return None
    if " " in value or " " in value:
        return None
    return value


def _ics_event_row(
    props: dict[str, str], user_tz: str | None = None
) -> tuple[dict[str, Any] | None, str | None]:
    """把一个 VEVENT 的属性映射为行程行；无法使用时返回 (None, 原因)。"""
    start_parsed = _ics_local_datetime(
        props.get("DTSTART", ""), props.get("DTSTART_TZID", ""), user_tz
    )
    if start_parsed is None:
        return None, "缺少或非法的 DTSTART"
    start_dt, _start_has_time = start_parsed
    start_day = start_dt.date()
    start_date = start_dt.strftime("%Y-%m-%d")
    end_date = ""
    end_parsed = _ics_local_datetime(
        props.get("DTEND", ""), props.get("DTEND_TZID", ""), user_tz
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

    # 文本字段不做 strip：导出端保留原文（含首尾空白 / 制表符），导入
    # 若改写字段，身份键随之漂移，同账号回导会重复新增
    description = _ics_unescape(props.get("DESCRIPTION", ""))
    summary = _ics_unescape(props.get("SUMMARY", ""))
    origin = destination = purpose = ""
    # 正则只扫描前缀：两个模式都含非贪婪 .*? 加尾部字面量，攻击者用大量
    # 「 → 」/缺少闭合括号的文本可触发二次方回溯（1M 字符可卡住数十分钟）。
    # 本工具生成的 DESCRIPTION / SUMMARY 都在数百字符内，超长前缀不可能是
    # 自己的格式。
    match = _ALIGO_DESCRIPTION.match(description[:2000].lstrip(" \t"))
    if match:
        # 导出端把出发地 / 目的地里的箭头与全角逗号转义成 \uXXXX：
        # 还原后才能得到原始字段（否则含「→」的出发地被误拆成两段）
        origin = unescape_ics_machine_field(match.group("origin"))
        destination = unescape_ics_machine_field(match.group("destination"))
        purpose = match.group("purpose")
        # 日期默认以 DTSTART / DTEND 为准：DESCRIPTION 里的时间段可能被外部
        # 日历手工编辑过，与实际事件不一致。只有导出端无法用 DTEND 表达
        # 机器段 end 时才以机器段为准：① 无 DTEND（缺失 / 无法解析）时
        # 机器段是唯一日期来源；② end<start 的脏数据被导出压成单日；
        # ③ end=9999-12-31 无法 +1 天。其余差异视为外部日历对事件日期的
        # 编辑（只改 DTEND），必须尊重——否则旧机器段会把用户的改期回滚。
        # 机器段日期必须是 ASCII 真实日历日期：非 ASCII 数字 / 2026-02-31
        # 之类的手工值不能借这条通道落库（CSV 导入同口径拒绝）。
        raw_machine_end = match.group("end")
        machine_start = normalize_identity_date(match.group("start"))
        machine_end = normalize_identity_date(raw_machine_end)
        machine_end_wins = bool(machine_end) and (
            end_date == ""
            or machine_end == "9999-12-31"
            or (machine_end < machine_start and end_date == start_date)
        )
        if (
            machine_start == start_date
            and machine_end != end_date
            and machine_end_wins
            and raw_machine_end.isascii()
            and _strict_date(machine_end) is not None
        ):
            end_date = machine_end
        # X-ALIGO-TRIP-END 是导出端为「无法解析的返程日期」保留原值的专用
        # 属性（DESCRIPTION 机器段只能写 YYYY-MM-DD）：只在 DTSTART 与机器段
        # 一致时信任，回导后 identity 的 end 与原行闭合，不会重复新增。
        # 导出端写该属性时 DTEND 恒为单日占位（end_date == start_date）或
        # 缺失：DTEND 已给出不同的多日区间说明外部日历改过日期，与上面
        # 机器段的「只改 DTEND 的编辑必须尊重」同一口径，以 DTEND 为准
        if machine_start == start_date and end_date in {"", start_date}:
            restored_end = _restored_end_from_props(props)
            if restored_end is not None:
                end_date = restored_end
    else:
        parts = _split_ics_summary(summary[:400])
        if parts:
            origin = unescape_ics_machine_field(parts["origin"])
            destination = unescape_ics_machine_field(parts["destination"])
            purpose = parts["purpose"]
        else:
            destination = summary[:400]
    if not destination.strip():
        return None, "缺少目的地（SUMMARY 为空）"
    for field, value in (
        ("origin", origin),
        ("destination", destination),
        ("purpose", purpose),
    ):
        if len(str(value or "").strip()) > _MAX_TRIP_FIELD_CHARS:
            return None, f"字段 {field} 超过 {_MAX_TRIP_FIELD_CHARS} 字"
    return {
        "origin": origin if origin.strip() else "",
        "destination": destination,
        "start_date": start_date,
        "end_date": end_date,
        # 未提供目的时保留 None：去重更新时存储层保留原值，不会把用户
        # 手动改过的目的覆盖成默认「出差」
        "purpose": purpose if purpose.strip() else None,
    }, None


def _split_ics_property(line: str) -> tuple[str, str]:
    """把 ``NAME;PARAM=...:VALUE`` 拆成 ``(左部, 值)``；引号内的冒号不切分。

    TZID 参数允许带引号（``TZID="UTC+05:30"``），按第一个冒号切分会把
    参数值劈开、属性名变形。
    """
    in_quotes = False
    for index, char in enumerate(line):
        if char == '"':
            in_quotes = not in_quotes
        elif char == ":" and not in_quotes:
            return line[:index], line[index + 1 :]
    return line, ""


def parse_trips_ics(
    text: str,
    *,
    max_rows: int | None = _MAX_IMPORT_ROWS,
    user_tz: str | None = None,
) -> tuple[list[dict[str, Any]], list[tuple[int, str]]]:
    """解析 iCalendar 文本，返回 ``(有效行程, [(事件序号, 原因)])``（序从 1 起）。

    ``max_rows=None`` 关闭行数上限（只读导出侧的回导检查需要解析超过单次
    导入上限的全量文件）；``user_tz`` 是 Web 面板上报的浏览器 IANA 时区，
    定时事件换算日期时优先使用（CLI 不传，按本机时区，行为不变）。
    """
    props: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    errors = _BoundedErrors()
    in_event = False
    in_alarm = False
    index = 0
    for line in _unfold_ics(text):
        upper = line.strip().upper()
        if upper == "BEGIN:VALARM":
            in_alarm = True
            continue
        if upper == "END:VALARM":
            in_alarm = False
            continue
        if upper == "BEGIN:VEVENT":
            if in_event:
                # 嵌套 BEGIN：外层事件缺少 END，属性已被重置；按错误跳过
                # 外层并继续解析内层
                index += 1
                errors.add((index, "事件嵌套：缺少 END:VEVENT"))
            in_event = True
            in_alarm = False
            props = {}
            continue
        if upper == "END:VEVENT":
            if not in_event:
                # 多余的 END 不能拿上一份 props 再生成一条行程
                index += 1
                errors.add((index, "多余的 END:VEVENT"))
                continue
            in_event = False
            in_alarm = False
            index += 1
            row, reason = _ics_event_row(props, user_tz)
            props = {}
            if reason:
                errors.add((index, reason))
            elif row is not None:
                rows.append(row)
                if max_rows is not None and len(rows) > max_rows:
                    raise ValueError(
                        f"单次最多导入 {max_rows} 条行程，请拆分文件后再导入"
                    )
            continue
        if not in_event or in_alarm or ":" not in line:
            # VALARM 区块（闹钟）里也有 DESCRIPTION：整块跳过，不能顶替
            # 事件级描述（RFC 5545 不规定属性顺序）
            continue
        left, raw_value = _split_ics_property(line)
        params = left.split(";")
        name = params[0].strip().upper()
        if name in {
            "DTSTART",
            "DTEND",
            "SUMMARY",
            "DESCRIPTION",
            "X-ALIGO-TRIP-END",
        }:
            # 事件级属性以首个出现为准（导出端保证事件级在前）。文本属性
            # 保留原文（导出保留首尾空白 / 制表符，strip 会让身份漂移）；
            # 日期与 X 属性由各自解析函数在内部 strip
            props.setdefault(
                name,
                raw_value
                if name in {"SUMMARY", "DESCRIPTION", "X-ALIGO-TRIP-END"}
                else raw_value.strip(),
            )
            if name in {"DTSTART", "DTEND"}:
                # 属性参数里的 TZID 决定 DATE-TIME 的时区（Outlook 导出
                # 常见）；丢弃它会把同一时刻记成不同的日期。参数值可能
                # 带引号（TZID="Asia/Shanghai"），查询时区前去掉
                for param in params[1:]:
                    key, _, param_value = param.partition("=")
                    if key.strip().upper() == "TZID":
                        props.setdefault(
                            f"{name}_TZID", param_value.strip().strip('"')
                        )
    if in_event:
        index += 1
        errors.add((index, "缺少 END:VEVENT，事件被跳过"))
    if not rows and not errors:
        return [], [(0, "未找到 VEVENT 事件")]
    return rows, errors


# 账号内行程总量上限（与 Web 新增行程的 MAX_TRIP_ENTRIES、备份恢复的
# _MAX_TRIPS 一致）：越过该上限后全量导出会被拒绝，必须先清理
MAX_TOTAL_TRIPS = 5000
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
    user_tz: str | None = None,
) -> dict[str, Any]:
    """解析 CSV / iCalendar 文本并写入行程（与 CLI ``trips-import`` 同语义）。

    - 内容以 ``BEGIN:VCALENDAR`` 开头（容忍 BOM / 空白）时按 ICS 解析，否则按 CSV；
    - 重复行程沿用存储层去重（同路线 + 同起止日期更新目的）；导入未提供目的
      时不覆盖已有行程的目的，新建行程补默认「出差」；
    - 单次最多 5000 行；字段含 NUL / 孤立代理项时整份拒绝（与其它写接口同口径）；
    - ``dry_run=True`` 只按同一去重键模拟新增 / 更新，不写库；
    - ``user_tz`` 为 Web 面板上报的浏览器 IANA 时区（定时事件取日期用，
      缺省按服务端本机时区，CLI / 旧客户端行为不变）。

    Returns:
        ``{"dry_run", "format": "ics"|"csv", "rows", "created", "updated",
        "skipped": [{"line", "reason"}]（最多 50 条）, "skipped_total",
        "warnings": [{"line", "reason"}]（最多 50 条）, "warnings_total"}``。
        CSV 的 ``line`` 是物理行号；ICS 的 ``line`` 是事件序号（从 1 起）。
    """
    if len(str(text or "")) > _MAX_IMPORT_CHARS:
        raise ValueError(
            f"导入文件过大（超过 {_MAX_IMPORT_CHARS} 字符），请拆分后再导入"
        )
    normalized = str(text or "").lstrip("﻿ \t\r\n")
    is_ics = force_ics or normalized.upper().startswith("BEGIN:VCALENDAR")
    if is_ics:
        rows, errors = parse_trips_ics(text, user_tz=user_tz)
    else:
        rows, errors = parse_trips_csv(text)
    warnings = getattr(errors, "warnings", None)
    result: dict[str, Any] = {
        "dry_run": dry_run,
        "format": "ics" if is_ics else "csv",
        "rows": len(rows),
        "created": 0,
        "updated": 0,
        "skipped": [
            {"line": line, "reason": reason} for line, reason in errors[:_MAX_SKIPPED]
        ],
        "skipped_total": getattr(errors, "total", len(errors)),
        # 合法但可疑（跨行记录疑似错位引号合并）：只提示，不影响计数
        "warnings": [
            {"line": line, "reason": reason}
            for line, reason in (warnings or [])[:_MAX_SKIPPED]
        ],
        "warnings_total": getattr(warnings, "total", len(warnings or [])),
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

    # 新建行程的默认目的不在锁外按陈旧快照预填：存储层在写锁内重载后只对
    # 「本批新增」行补默认值。锁外预填会把并发期间用户刚编辑的 purpose
    # 当成显式更新覆盖掉（同身份行在快照之后才出现时）。
    history = store.get_trip_history(limit=None)
    existing_keys = {trip_identity(trip) for trip in history}
    # 与 Web 新增行程的上限同口径：超过总量后 user-export / GET /api/export
    # 会拒绝导出全量备份，账号将无法再产生可恢复的备份
    planned_keys = set(existing_keys)
    planned_created = 0
    for row in rows:
        key = trip_identity(row)
        if key in planned_keys:
            continue
        planned_keys.add(key)
        planned_created += 1
    # 只拦「导入前未越限、本批会推过上限」：已越限的历史账号恢复自身
    # 备份（含新增行）不再被永久拒绝，与导入侧按轴基线豁免一致
    if (
        len(history) <= MAX_TOTAL_TRIPS
        and len(history) + planned_created > MAX_TOTAL_TRIPS
    ):
        raise ValueError(
            f"导入后行程总数将超过上限 {MAX_TOTAL_TRIPS}"
            f"（现有 {len(history)} + 新增 {planned_created}），"
            "请先清理历史行程后重试"
        )

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
    created, updated = store.save_trip_histories(
        rows, default_new_purpose="出差"
    )
    result["created"], result["updated"] = int(created), int(updated)
    return result


def trip_skip_label(line: int, fmt: str) -> str:
    """跳过项的位置标签（Web 400 / CLI / 前端共用，避免口径漂移）。

    CSV 的 ``line`` 是物理行号，标成「第N行」；ICS 的 ``line`` 是事件序号
    （从 1 起），标成「事件N」；ICS line<=0（如「未找到 VEVENT」这类整体
    错误）不标序号，只显示原因。
    """
    try:
        value = int(line)
    except (TypeError, ValueError):
        value = 0
    if fmt == "csv":
        return f"第{value}行"
    return f"事件{value}" if value > 0 else ""


def trip_export_roundtrip_problems(
    text: str, *, is_ics: bool
) -> tuple[int, list[tuple[int, str]]]:
    """导出文件用真实导入解析器复核：返回 ``(会被跳过的总数, 前几条原因)``。

    与 CLI trips-export 的 CSV 自检同源（README 承诺「重复导入幂等」）：
    导出端渲染完立刻用同一解析器跑一遍，日期非法 / 倒置、字段超长等在
    回导时会被整行 / 整事件拒绝的数据必须提前暴露，否则迁移 / 换机恢复
    时静默丢行程。只读、不写库；``max_rows=None`` 兼容超过单次导入上限
    的历史账号（上限只约束导入，不能把成功导出变成异常）。
    """
    if is_ics:
        from utils.trip_export import count_ics_events

        events = count_ics_events(text)
        if events <= 0:
            # 空日历没有可回导事件（「未找到 VEVENT」不是一条被跳过的数据）
            return 0, []
        _, errors = parse_trips_ics(text, max_rows=None)
    else:
        _, errors = parse_trips_csv(text, max_rows=None)
    total = int(getattr(errors, "total", len(errors)))
    return total, list(errors[:3])


# 面板导入阈值（与 webapp/server.py 的 TripImportPayload / app.js 一致）：
# 导出端按合法数据渲染、不做导入上限预检，超过阈值时导出仍成功，但同一
# 面板或 CLI 回导会被整份拒绝——必须提前告知，不能把「导出成功」当成
# 「备份可用」。
WEB_IMPORT_MAX_CHARS = 500_000
WEB_IMPORT_MAX_BYTES = 2 * 1024 * 1024


def trip_export_import_limit(text: str) -> str:
    """导出文本超过哪一级导入上限：``"cli"`` / ``"panel"`` / ``""``。

    只读判断，不改变导出字节，也不抛异常（超限导出仍必须成功）。
    行数上限（5000）由导入解析器在回导时检查；这里按字符与字节的最严
    门槛提示，避免为提示再解析一遍超大文件。
    """
    chars = len(text)
    if chars > _MAX_IMPORT_CHARS:
        return "cli"
    if chars > WEB_IMPORT_MAX_CHARS or len(
        text.encode("utf-8")
    ) > WEB_IMPORT_MAX_BYTES:
        return "panel"
    return ""


def trip_export_import_limit_note(level: str) -> str:
    """把 :func:`trip_export_import_limit` 的级别转成 CLI / 面板提示文案。"""
    if level == "cli":
        return (
            f"导出文件超过 CLI 单次导入上限（{_MAX_IMPORT_CHARS} 字符），"
            "回导会被整份拒绝，请拆分后用 CLI 导入，或用 JSON 备份恢复"
        )
    return (
        f"导出文件超过面板导入上限（{WEB_IMPORT_MAX_CHARS} 字符 / 2MB），"
        "面板回导会被拒收，请用 CLI 导入或 JSON 备份恢复"
    )
