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
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from utils.console import strip_terminal_controls
from utils.json_safe import sanitize_json

logger = logging.getLogger(__name__)


_FORMULA_CHARS = ("=", "+", "-", "@")
# OWASP 把 CR / LF / 制表符同样列为公式注入前缀：表格软件会先剥掉前导
# 空白 / 换行再把后续内容当公式求值
_FORMULA_PREFIXES = _FORMULA_CHARS + ("\t", "\r", "\n")

# 身份日期形态：分隔符兼容历史数据的 /（2026/3/12），\d 兼容非 ASCII 数字
# （int() 会把它们解析成等价数值，输出再统一格式化为纯 ASCII）
_IDENTITY_DATE_RE = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})")


def _identity_projection(text: str) -> str:
    """导出清洗的投影（身份键与渲染输出共用同一口径）。

    孤立代理项转义（避免写文件时 UnicodeEncodeError），CRLF / CR 归一为
    LF，U+2028 / U+2029 替换为空格（``str.splitlines`` 会在它们处断行），
    剥离终端控制序列（ESC / OSC 会在终端执行剪贴板写入等操作）。保留
    制表符与换行。

    存储里的行程字段是用户 / 模型输入，不可信；CSV / ICS / HTML 与
    ``--stdout`` 输出、以及全部身份键（存储去重 / 导入预览 / trips-dedupe）
    都必须经过这里——导出会把字段清洗成这个投影，身份若不跟着归一，
    含控制字符 / U+2028 的行程导出再导入会被当成另一条行程重复新增。
    """
    text = str(sanitize_json(text))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace(" ", " ").replace(" ", " ")
    return strip_terminal_controls(text)


def normalize_identity_date(value: Any) -> str:
    """日期归一：YYYY-M-D / YYYY/M/D 补零为纯 ASCII 的 YYYY-MM-DD。

    历史数据可能保留未补零写法（2026-1-5）或斜杠写法（2026/3/12：备份
    恢复按保真优先原样保留），存储去重与导入端都按补零后的 ISO 日期比对；
    导出（CSV / ICS）也用它——否则这些日期在导出再导入时会被严格格式
    校验整行 / 整事件跳过，造成静默丢行程。非 ASCII 数字日期（\\d 匹配）
    经 int() 归一为等价 ASCII 日期，避免混合字符集绕过去重口径。

    无法识别或范围非法的值（待定 / 2026-13-40）原样返回（已做导出清洗
    投影）；日期身份是否成立由调用方决定。放在本模块供 trip_import 反向
    复用，避免循环导入。
    """
    text = _identity_projection(str(value or "")).strip()
    match = _IDENTITY_DATE_RE.fullmatch(text)
    if not match:
        return text
    year = int(match.group(1))
    month = int(match.group(2))
    day = int(match.group(3))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return text
    return f"{year:04d}-{month:02d}-{day:02d}"


def normalize_identity_text(value: Any) -> str:
    """行程文本字段的身份归一：与导出清洗使用同一投影。

    ICS 往返会把换行还原成 LF，而 CSV 与手动录入可能保留 CRLF；导出还会
    剥离控制字符 / 把 U+2028 / U+2029 换成空格。存储去重、导入预览与去重
    命令必须用同一口径，否则同一趟行程因换行 / 控制字符写法不同被当成
    两条记录（导入回导时重复新增，且去重命令清不掉）。
    """
    return _identity_projection(str(value or ""))


def _needs_quote_guard(text: str) -> bool:
    """以公式字符开头，或去掉任意个前导引号 / 前导空白后仍需防护。

    逐字符递归在字段包含上千个前导单引号时会触发 RecursionError（导入 /
    导出都会崩溃），这里改为迭代剥离。前导空白后的公式字符同样危险：
    表格软件会忽略前导空白再求值，而导出清洗会把 U+2028 / CR 变成 LF /
    空格，因此判定必须覆盖 lstrip 后的形态（导入端复用同一判定反解）。
    """
    index = 0
    while index < len(text) and text[index] == "'":
        index += 1
    rest = text[index:]
    if rest.startswith(_FORMULA_PREFIXES):
        return True
    return rest.lstrip().startswith(_FORMULA_CHARS)


def _clean_trip_value(value: object) -> str:
    """渲染前的字段清洗（与身份归一同一投影，见 ``_identity_projection``）。"""
    return _identity_projection(str(value if value is not None else ""))


def _csv_safe(value: object) -> str:
    text = _clean_trip_value(value)
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
                _csv_safe(normalize_identity_date(trip.get("start_date"))),
                _csv_safe(normalize_identity_date(trip.get("end_date"))),
                _csv_safe(trip.get("purpose")),
            ]
        )
    return "\ufeff" + buffer.getvalue()


def _ics_date(value: str) -> str | None:
    """把 YYYY-MM-DD 转为 iCalendar 日期（YYYYMMDD）；非法值返回 None。

    未补零的历史日期（2026-1-5）先归一：否则整条事件被跳过，导出「少行程」。
    """
    try:
        return datetime.strptime(
            normalize_identity_date(value), "%Y-%m-%d"
        ).strftime("%Y%m%d")
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


def escape_ics_machine_field(value: str) -> str:
    """DESCRIPTION 机器可读段的字段转义：把会破坏 ``origin → destination，
    start ~ end，purpose`` 结构的分隔符（反斜杠 / 箭头 / 全角逗号）编码成
    ``\\uXXXX`` 文本，导入端再还原。

    例：出发地「北京 → 天津」不再被导入端按第一个箭头误拆成
    origin=北京、destination=天津 → 上海（去重键变化导致重复导入）。
    """
    return (
        str(value)
        .replace("\\", "\\u005c")
        .replace("→", "\\u2192")
        .replace("，", "\\uff0c")
    )


def unescape_ics_machine_field(value: str) -> str:
    """``escape_ics_machine_field`` 的逆操作（先还原箭头 / 逗号，再还原
    反斜杠，避免把 ``\\u005cu2192`` 误还原成箭头）。"""
    return (
        str(value)
        .replace("\\u2192", "→")
        .replace("\\uff0c", "，")
        .replace("\\u005c", "\\")
    )


def _trip_ics_event_lines(trip: dict[str, Any], user_id: str) -> list[str] | None:
    """单条行程的 VEVENT 行；缺少出发日期时返回 None。"""
    start = _ics_date(trip.get("start_date") or "")
    if not start:
        return None
    raw_end = trip.get("end_date")
    end = _ics_date(raw_end or "")
    if raw_end and not end:
        # 与「结束早于出发」一致留下告警：否则多日行程静默截成单日，
        # 用户把文件导入日历后才发现少了一天
        logger.warning(
            "行程 %s 的返程日期无法解析（%r），日历按单日导出",
            trip.get("trip_id"),
            raw_end,
        )
        end = start
    if not end:
        end = start
    if end < start:
        logger.warning("行程结束日期早于出发日期，按单日处理: %s", trip.get("trip_id"))
        end = start
    end_dt = datetime.strptime(end, "%Y%m%d")
    try:
        # ICS 的 DTEND 是排他的；9999-12-31 无法再加一天
        end_exclusive = (end_dt + timedelta(days=1)).strftime("%Y%m%d")
    except OverflowError:
        end_exclusive = end

    origin = _clean_trip_value(trip.get("origin") or "出发地")
    destination = _clean_trip_value(trip.get("destination") or "目的地")
    purpose = _clean_trip_value(trip.get("purpose") or "出行")
    summary = f"{origin} → {destination}（{purpose}）"
    # DESCRIPTION 是 trips-import 的机器可读来源：必须用原始值（占位符只
    # 出现在给人看的 SUMMARY 里），否则空出发地在往返后会变成字面量「出发地」，
    # 去重键随之改变并重复新增行程。日期：出发用 DTSTART 的解析结果；返程
    # 优先保留可解析的原始日期（含倒置日期，回导后身份不变），只有无法解析
    # 时才写 DTEND 实际使用的单日日期——否则导入端正则失配回退 SUMMARY，
    # 路线 / 目的被改写并按新身份重复新增
    start_iso = f"{start[:4]}-{start[4:6]}-{start[6:8]}"
    end_identity = normalize_identity_date(
        trip.get("end_date") or trip.get("start_date")
    )
    # 机器段只能写 YYYY-MM-DD：无法解析的返程日期（待定 / 旧格式 / 不存在的
    # 日历日期）在这里被改写成单日日期，原值改由 X-ALIGO-TRIP-END 属性
    # 携带——否则回导时身份键的 end 变成出发日，同一账号重复新增一条行程
    # （README 承诺重复导入幂等）。X- 属性按 RFC 5545 是合法 x-name，外部
    # 日历客户端与不认识的解析器都会忽略；旧版导入器行为与今天完全一致。
    restored_end = ""
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", end_identity):
        end_identity = f"{end[:4]}-{end[4:6]}-{end[6:8]}"
    if raw_end and _ics_date(str(raw_end)) is None:
        normalized_raw_end = normalize_identity_date(
            _clean_trip_value(raw_end)
        )
        # 超长脏值与导入端字段上限同口径：超出时宁可不带（回导退化为
        # 单日），也不能让导入端因为一个附加属性拒绝整条事件
        if normalized_raw_end and len(normalized_raw_end) <= 100:
            restored_end = normalized_raw_end
    description = (
        f"Aligo 行程：{escape_ics_machine_field(_clean_trip_value(trip.get('origin')))} → "
        f"{escape_ics_machine_field(_clean_trip_value(trip.get('destination')))}，"
        f"{start_iso} ~ {end_identity}，"
        f"{_clean_trip_value(trip.get('purpose'))}"
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # UID 同时包含行程内容：删除最新行程后 id 可能被复用，仅用 trip_id
    # 会让新行程覆盖日历里已存在的旧事件
    uid_source = ":".join(
        [
            # 全部经 _clean_trip_value：历史脏数据里的孤立代理项会让
            # uid_source.encode("utf-8") 抛 UnicodeEncodeError（整个导出 500）
            _clean_trip_value(user_id),
            _clean_trip_value(trip.get("trip_id") or "trip"),
            # 只用于摘要：转义后的值保证不会把换行带进 UID 行
            _ics_escape(origin),
            _ics_escape(destination),
            _clean_trip_value(trip.get("start_date") or ""),
            _clean_trip_value(trip.get("end_date") or ""),
        ]
    )
    uid_digest = hashlib.sha1(uid_source.encode("utf-8")).hexdigest()[:16]
    uid = f"aligo-{uid_digest}@aligo.local"

    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{stamp}",
        f"DTSTART;VALUE=DATE:{start}",
    ]
    # RFC 5545 §3.8.2.2：DTEND 必须晚于 DTSTART。9999-12-31 各加一天会
    # 溢出，此前写出的 DTEND==DTSTART 是非法零长度事件；单日时按 §3.6.1
    # 省略 DTEND 表示一天，多日时 DTEND=99991231 本身合法（最后一天因
    # 4 位年份上限不可表示，与既有行为一致）
    if end_exclusive > start:
        lines.append(f"DTEND;VALUE=DATE:{end_exclusive}")
    lines.extend(
        [
            f"SUMMARY:{_ics_escape(summary)}",
            f"DESCRIPTION:{_ics_escape(description)}",
        ]
    )
    if restored_end:
        lines.append(
            "X-ALIGO-TRIP-END:"
            + _ics_escape(escape_ics_machine_field(restored_end))
        )
    lines.extend(
        [
            # 全天事件的 DTSTART 是出发日 00:00，提前 15 小时即前一天 09:00 提醒
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{_ics_escape(f'出行提醒：明天 {origin} → {destination}')}",
            "TRIGGER:-PT15H",
            "END:VALARM",
            "END:VEVENT",
        ]
    )
    return lines


def _ics_fold(line: str) -> str:
    """按 RFC 5545 把超过 75 字节的行折行（续行以单个空格开头）。

    折行位置不能劈开 UTF-8 字符：回退到完整字符边界。中文 SUMMARY 等
    长文本不折行会被部分日历客户端拒收或截断。按下标推进而不是每折一次
    复制剩余字节：备份恢复不限制字段长度，单条超长字段的反复拷贝是
    O(n²)，日历导出会卡死数分钟。
    """
    data = line.encode("utf-8")
    if len(data) <= 75:
        return line
    chunks: list[bytes] = []
    start = 0
    limit = 75
    while len(data) - start > limit:
        cut = start + limit
        while cut > start and (data[cut] & 0xC0) == 0x80:
            cut -= 1
        chunks.append(data[start:cut])
        start = cut
        limit = 74  # 续行开头的空格占 1 字节
    chunks.append(data[start:])
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
    if not events:
        # RFC 5545 §3.6：iCalendar 对象 MUST 至少含一个组件（component = 1*）。
        # 保持既有 200 / exit 0 的空导出语义，输出一个客户端必须忽略的
        # 扩展组件（x-comp 是 component 的合法取值；不能只写 BEGIN/END
        # 两行，x-comp 的 ABNF 要求至少一条内容行）
        lines.extend(
            [
                "BEGIN:X-ALIGO-EMPTY",
                "X-ALIGO-EMPTY:1",
                "END:X-ALIGO-EMPTY",
            ]
        )
    lines.extend(["END:VCALENDAR", ""])
    return "\r\n".join(_ics_fold(line) for line in lines)


def render_trip_ics(trip: dict[str, Any], user_id: str) -> str | None:
    """把一条行程渲染为全天事件的 iCalendar 文本；缺少出发日期时返回 None。"""
    event = _trip_ics_event_lines(trip, user_id)
    if event is None:
        return None
    return _ics_calendar([event])


def render_trips_html(trips: list[dict[str, Any]], user_id: str) -> str:
    """把行程列表渲染为可打印 / 分享的独立 HTML 报销台账。

    与 CSV / ICS 同一数据口径（全部行程，按出发日期倒序），所有动态文本
    经 ``html.escape`` 转义；缺少出发日期的行程按「日期未记录」展示。
    """
    import html as _html

    def esc(value: object) -> str:
        return _html.escape(_clean_trip_value(value))

    def sort_key(trip: dict[str, Any]) -> tuple[str, str]:
        # trip_id 是 "trip_N" 字符串：同一天内按 id 文本倒序即可，不能 int()。
        # 出发日期先归一再排序：未补零的历史日期（2026-1-5）按字典序会排到
        # 2026-01-06 之后
        return (
            normalize_identity_date(trip.get("start_date")),
            str(trip.get("trip_id") or ""),
        )

    ordered = sorted(trips, key=sort_key, reverse=True)
    rows: list[str] = []
    for trip in ordered:
        start = normalize_identity_date(trip.get("start_date")) or "日期未记录"
        end = normalize_identity_date(trip.get("end_date"))
        period = start if not end or end == start else f"{start} ~ {end}"
        rows.append(
            "<tr>"
            f"<td>{esc(trip.get('origin') or '—')}</td>"
            f"<td>{esc(trip.get('destination') or '—')}</td>"
            f"<td>{esc(period)}</td>"
            f"<td>{esc(trip.get('purpose') or '—')}</td>"
            "</tr>"
        )
    table = (
        "<table><thead><tr><th>出发地</th><th>目的地</th><th>日期</th>"
        "<th>目的</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
        if rows
        else '<p class="empty">（暂无行程记录）</p>'
    )
    exported_at = datetime.now().isoformat(timespec="seconds")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Aligo 行程台账</title>
<style>
  body {{ margin: 0; background: #f2f5f4; color: #14262e;
    font: 15px/1.6 "PingFang SC", "Microsoft YaHei", system-ui, sans-serif; }}
  .page {{ max-width: 860px; margin: 24px auto; padding: 28px 32px;
    background: #fff; border-radius: 14px; box-shadow: 0 10px 32px rgba(15,36,42,.08); }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .meta {{ color: #7b8d93; font-size: 12.5px; margin-bottom: 16px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #e2e9e8;
    overflow-wrap: anywhere; word-break: break-word; }}
  th {{ font-size: 12.5px; color: #7b8d93; font-weight: 600; }}
  .empty {{ color: #7b8d93; }}
  @media print {{
    body {{ background: #fff; }}
    .page {{ margin: 0; padding: 0; box-shadow: none; }}
  }}
</style>
</head>
<body>
<div class="page">
<h1>Aligo 行程台账</h1>
<div class="meta">用户 {esc(user_id)} · 共 {len(ordered)} 条 · 导出于 {esc(exported_at)}</div>
{table}
</div>
</body>
</html>
"""


def render_trips_ics(trips: list[dict[str, Any]], user_id: str) -> str:
    """把全部行程渲染为一个 iCalendar 文件（缺少出发日期的行程跳过）。"""
    events = [
        event
        for event in (_trip_ics_event_lines(trip, user_id) for trip in trips)
        if event is not None
    ]
    return _ics_calendar(events)


def count_ics_events(text: str) -> int:
    """统计 iCalendar 文本里的真实事件数。

    必须先按 RFC 5545 展开折行（续行以单个空格开头）再按整行匹配：字段
    内容折行后可能恰好产生续行 `` BEGIN:VEVENT``，``splitlines`` +
    ``strip()`` 会把它算成第二个事件，导出条数被凭空放大、跳过提示被吞掉。
    """
    unfolded = text.replace("\r\n ", "")
    # 只按真实换行切分：``str.splitlines`` 还会在 U+2028 / U+2029 处断行，
    # 字段里残留的行分隔符会被算成额外事件、跳过提示被吞
    return sum(
        1
        for line in unfolded.split("\n")
        if line.strip() == "BEGIN:VEVENT"
    )


def trip_ics_end_unparsable(trip: dict[str, Any]) -> bool:
    """返程日期存在但无法解析：ICS 会按单日导出，CLI 需要提示用户。"""
    raw_end = trip.get("end_date")
    return bool(raw_end) and _ics_date(str(raw_end)) is None
