"""
行程 CSV 渲染测试（Web / CLI 共用）

运行：python tests/test_trip_export.py
"""
import csv
import io
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.trip_export import (
    _ics_escape,
    render_trip_ics,
    render_trips_csv,
    render_trips_html,
    render_trips_ics,
)


def test_header_and_rows():
    text = render_trips_csv(
        [
            {
                "origin": "北京",
                "destination": "上海",
                "start_date": "2026-12-05",
                "end_date": "2026-12-07",
                "purpose": "出差,客户拜访",
            }
        ]
    )
    assert text.startswith("\ufeff"), "应包含 UTF-8 BOM"
    rows = list(csv.reader(io.StringIO(text[1:])))
    assert rows[0] == ["出发地", "目的地", "出发日期", "返程日期", "目的"]
    assert rows[1] == ["北京", "上海", "2026-12-05", "2026-12-07", "出差,客户拜访"]
    print("✓ CSV 表头 / BOM / 逗号转义")


def test_formula_prefix_neutralized():
    text = render_trips_csv(
        [
            {
                "origin": "=1+1",
                "destination": "@cmd",
                "start_date": "=cmd|' /C calc'!A0",
                "end_date": "-2+3",
                "purpose": "+SUM(A1)",
            }
        ]
    )
    row = list(csv.reader(io.StringIO(text[1:])))[1]
    assert row[0] == "'=1+1"
    assert row[1] == "'@cmd"
    assert row[2] == "'=cmd|' /C calc'!A0", "日期列同样需要防公式注入"
    assert row[3] == "'-2+3"
    assert row[4] == "'+SUM(A1)"
    print("✓ 公式前缀加单引号（含日期列）")


def test_empty_trips():
    rows = list(csv.reader(io.StringIO(render_trips_csv([])[1:])))
    assert len(rows) == 1, "无行程时只输出表头"
    print("✓ 空列表仅表头")


def test_ics_single_trip():
    """单条行程：全天事件、排他结束日期、全天事件前一天提醒。"""
    trip = {
        "trip_id": "trip_1",
        "origin": "北京",
        "destination": "上海",
        "start_date": "2026-12-05",
        "end_date": "2026-12-07",
        "purpose": "出差",
    }
    text = render_trip_ics(trip, "u1")
    assert text.startswith("BEGIN:VCALENDAR") and text.rstrip().endswith("END:VCALENDAR")
    assert "DTSTART;VALUE=DATE:20261205" in text
    assert "DTEND;VALUE=DATE:20261208" in text, "DTEND 应为排他日期（返程日 +1）"
    assert "SUMMARY:北京 → 上海（出差）" in text
    assert "TRIGGER:-PT15H" in text, "应带前一天提醒"
    assert "UID:aligo-" in text
    print("✓ ICS 单条行程（全天事件 / 排他结束 / 提醒）")


def test_ics_missing_or_invalid_dates():
    assert render_trip_ics({"destination": "上海"}, "u1") is None
    assert render_trip_ics({"start_date": "not-a-date"}, "u1") is None
    # 返程早于出发：按单日事件处理
    trip = {"start_date": "2026-12-05", "end_date": "2026-12-01"}
    text = render_trip_ics(trip, "u1")
    assert "DTSTART;VALUE=DATE:20261205" in text
    assert "DTEND;VALUE=DATE:20261206" in text
    print("✓ ICS 缺日期 / 非法日期 / 结束早于开始")


def test_ics_escape_and_multiple_events():
    assert _ics_escape("a;b,c") == "a\\;b\\,c"
    assert _ics_escape("a\rb") == "a\\nb", "孤立的 CR 必须转义"
    trips = [
        {"start_date": "2026-12-05", "end_date": "2026-12-06", "destination": "上海"},
        {"start_date": "2026-12-09", "end_date": "2026-12-10", "destination": "杭州"},
        {"destination": "无日期"},
    ]
    text = render_trips_ics(trips, "u1")
    assert text.count("BEGIN:VEVENT") == 2
    assert text.count("UID:") == 2
    print("✓ ICS 文本转义与多事件组合")


def test_csv_uses_single_line_endings():
    """CSV 行结束符固定 \\n：Windows 文本模式写入不再产生 \\r\\r\\n 与空记录。"""
    text = render_trips_csv(
        [
            {
                "origin": "北京",
                "destination": "上海",
                "start_date": "2026-12-01",
                "end_date": "2026-12-03",
                "purpose": "出差",
            }
        ]
    )
    body = text.lstrip("﻿")
    assert "\r" not in body, "渲染结果不应含 CR（由写入侧统一翻译）"
    rows = list(csv.reader(io.StringIO(body)))
    assert len(rows) == 2 and all(row for row in rows), "不能出现空记录"
    print("✓ CSV 行结束符不再二次翻译")


def test_ics_lines_folded_at_75_bytes():
    """长文本按 RFC 5545 折行：每个物理行 ≤75 字节，展开后内容完整。"""
    long_purpose = "超长出行注意事项" * 12
    ics = render_trip_ics(
        {
            "trip_id": "t1",
            "origin": "北京",
            "destination": "乌鲁木齐",
            "start_date": "2026-12-01",
            "purpose": long_purpose,
        },
        "u1",
    )
    assert ics is not None
    physical = ics.split("\r\n")
    over = [len(line.encode("utf-8")) for line in physical if len(line.encode("utf-8")) > 75]
    assert not over, f"存在超过 75 字节的行: {over}"
    unfolded = ics.replace("\r\n ", "")
    assert "北京 → 乌鲁木齐" in unfolded
    assert long_purpose[:18] in unfolded
    print("✓ ICS 超过 75 字节的行折行")


def test_ics_fold_large_field_is_linear():
    """超长单字段折行不能 O(n²)：备份恢复不限字段长度，2MB 字段不应卡住。"""
    import time

    from utils.trip_export import _ics_fold

    line = "SUMMARY:" + "很长的行程说明" * 50000  # ≈ 1.2MB UTF-8
    started = time.monotonic()
    folded = _ics_fold(line)
    elapsed = time.monotonic() - started
    assert elapsed < 3, f"折行 1.2MB 字段耗时 {elapsed:.2f}s"
    unfolded = folded.replace("\r\n ", "")
    assert unfolded == line, "折行 / 展开必须无损"
    physical = folded.split("\r\n")
    assert all(len(part.encode("utf-8")) <= 75 for part in physical)
    print("✓ ICS 折行大字段为线性复杂度")


def test_render_trips_html():
    """HTML 台账：内容全部转义、按出发日期倒序、无日期行标注。"""
    from utils.trip_export import render_trips_html

    trips = [
        {
            "trip_id": "trip_2",
            "origin": "<b>北京</b>",
            "destination": "上海",
            "start_date": "2026-12-03",
            "end_date": "2026-12-05",
            "purpose": "出差",
        },
        {
            "trip_id": "trip_1",
            "origin": "",
            "destination": "杭州",
            "start_date": "",
            "end_date": "",
            "purpose": "",
        },
    ]
    html = render_trips_html(trips, "u<1>")
    assert html.startswith("<!doctype html>")
    assert "&lt;b&gt;北京&lt;/b&gt;" in html and "<b>北京</b>" not in html
    assert "u&lt;1&gt;" in html
    assert html.index("2026-12-03") < html.index("日期未记录"), "倒序、无日期在后"
    assert "2026-12-03 ~ 2026-12-05" in html
    assert "共 2 条" in html
    empty = render_trips_html([], "u1")
    assert "（暂无行程记录）" in empty
    print("✓ HTML 台账渲染（转义 / 排序 / 空数据）")


def test_many_leading_quotes_do_not_recurse():
    """上千个前导单引号不能触发 RecursionError（导入 / 导出共用该判定）。

    超长字段在新口径下按行跳过（100 字上限）：关键是解析有界返回，
    不能抛 RecursionError / 崩溃。
    """
    from utils.trip_import import parse_trips_csv

    tricky = "'" * 1200 + "=1+1"
    text = render_trips_csv(
        [
            {
                "origin": "北京",
                "destination": tricky,
                "start_date": "2026-01-01",
            }
        ]
    )
    rows, errors = parse_trips_csv(text)
    assert rows == []
    assert any("超过 100 字" in reason for _, reason in errors), errors
    print("✓ 超多前导单引号不再递归溢出（超长行按行拒绝）")


def test_trips_html_normalizes_unpadded_dates():
    """台账按归一日期倒序、显示补零，与 CSV / ICS 同一口径。"""
    html = render_trips_html(
        [
            {"trip_id": "t1", "destination": "上海", "start_date": "2026-1-5"},
            {
                "trip_id": "t2",
                "destination": "杭州",
                "start_date": "2026-01-06",
            },
        ],
        "u1",
    )
    assert "2026-01-05" in html and "2026-1-5" not in html
    # 倒序：1-06 的行必须排在 1-05 之前
    assert html.index("杭州") < html.index("上海")
    print("✓ 台账日期归一与排序")


def test_unpadded_dates_normalized_in_exports():
    """历史未补零日期导出时补零：CSV 回导不被严格校验跳过、ICS 不再丢事件。"""
    trip = {
        "trip_id": "t9",
        "origin": "北京",
        "destination": "上海",
        "start_date": "2026-1-5",
        "end_date": "2026-1-6",
        "purpose": "出差",
    }
    csv_text = render_trips_csv([trip])
    assert "2026-01-05" in csv_text and "2026-01-06" in csv_text
    from utils.trip_import import parse_trips_csv

    rows, errors = parse_trips_csv(csv_text)
    assert errors == [], errors
    assert rows[0]["start_date"] == "2026-01-05", rows
    assert rows[0]["end_date"] == "2026-01-06", rows

    ics = render_trip_ics(trip, "u1")
    assert ics and "DTSTART;VALUE=DATE:20260105" in ics, "未补零日期不能丢掉事件"
    print("✓ 未补零日期导出补零（CSV / ICS 往返不丢行程）")


def test_render_strips_controls_and_surrogates():
    """ESC / OSC 与孤立代理项字段：渲染不崩溃、输出无控制序列且可编码。"""
    trip = {
        "trip_id": "t-ctrl",
        "origin": "\ud800北京",
        "destination": "上海\x1b]52;c;QUJD\x07",
        "start_date": "2026-12-05",
        "end_date": "2026-12-06",
        "purpose": "出差",
    }
    for text in (
        render_trips_csv([trip]),
        render_trips_ics([trip], "u1"),
        render_trips_html([trip], "u1"),
    ):
        assert "\x1b" not in text, "终端控制序列必须剥离"
        assert "\x07" not in text
        text.encode("utf-8")  # 历史数据里的孤立代理项不能抛 UnicodeEncodeError
    print("✓ 导出渲染剥离控制序列并转义孤立代理项")


def test_html_table_wraps_long_unbroken_fields():
    """超长不可断字段不能把台账表格撑出页面 / 打印纸张。"""
    trip = {
        "trip_id": "t-long",
        "origin": "北京",
        "destination": "上海",
        "start_date": "2026-12-05",
        "end_date": "2026-12-06",
        "purpose": "A" * 5000,
    }
    html = render_trips_html([trip], "u1")
    assert "overflow-wrap: anywhere" in html
    assert "word-break: break-word" in html
    assert "A" * 5000 in html
    print("✓ 台账长字段换行样式")


def test_count_ics_events_unfolds_before_counting():
    """折行续行恰好是「 BEGIN:VEVENT」时按展开后的整行计数，不虚增。"""
    from utils.trip_export import count_ics_events

    trip = {
        "trip_id": "t-fold",
        "origin": "北京",
        "destination": "上海",
        "start_date": "2026-12-05",
        "end_date": "2026-12-07",
        "purpose": "AABEGIN:VEVENT",
    }
    text = render_trips_ics([trip], "u1")
    raw = sum(1 for line in text.splitlines() if line.strip() == "BEGIN:VEVENT")
    assert raw >= 2, "前提：折行续行恰好等于事件标记（旧口径会数成 2 个）"
    assert count_ics_events(text) == 1
    print("✓ ICS 事件计数先展开折行再按整行匹配")


def test_ics_unparsable_end_warns(caplog):
    """返程日期无法解析：日历按单日导出，但必须留告警并不丢原始值。"""
    import logging

    from utils.trip_export import trip_ics_end_unparsable

    trip = {
        "trip_id": "t-end",
        "origin": "北京",
        "destination": "上海",
        "start_date": "2026-12-05",
        "end_date": "待定",
    }
    assert trip_ics_end_unparsable(trip)
    with caplog.at_level(logging.WARNING):
        text = render_trips_ics([trip], "u1")
    assert "无法解析" in caplog.text
    assert "DTSTART;VALUE=DATE:20261205" in text
    assert "DTEND;VALUE=DATE:20261206" in text
    # 原始返程日期经专用属性保真携带：回导时身份才能与原行闭合
    assert "X-ALIGO-TRIP-END:待定" in text
    print("✓ 返程日期无法解析时告警并按单日导出（原值经 X 属性保留）")


def test_ics_slash_dates_parse_like_iso():
    """YYYY/M/D 旧格式日期与 ISO 同口径：导出为合法事件且不再告警。"""
    from utils.trip_export import trip_ics_end_unparsable

    trip = {
        "trip_id": "t-slash",
        "origin": "北京",
        "destination": "上海",
        "start_date": "2026/3/12",
        "end_date": "2026/3/13",
    }
    assert not trip_ics_end_unparsable(trip)
    text = render_trips_ics([trip], "u1")
    assert "DTSTART;VALUE=DATE:20260312" in text
    assert "DTEND;VALUE=DATE:20260314" in text
    assert "X-ALIGO-TRIP-END" not in text
    csv_text = render_trips_csv([trip])
    assert "2026-03-12" in csv_text and "2026-03-13" in csv_text
    print("✓ 斜杠旧格式日期导出归一为 ISO（事件不再被跳过）")


def test_ics_9999_single_day_omits_dtend():
    """9999-12-31 单日：排他 DTEND 无法表示，按 RFC 5545 §3.6.1 省略。"""
    single = {
        "trip_id": "t-max",
        "origin": "北京",
        "destination": "上海",
        "start_date": "9999-12-31",
        "end_date": "9999-12-31",
    }
    text = render_trip_ics(single, "u1")
    assert text and "DTSTART;VALUE=DATE:99991231" in text
    assert "DTEND" not in text, "DTEND==DTSTART 是非法零长度事件"
    multi = {
        "trip_id": "t-max2",
        "origin": "北京",
        "destination": "上海",
        "start_date": "2026-01-01",
        "end_date": "9999-12-31",
    }
    multi_text = render_trip_ics(multi, "u1")
    assert multi_text and "DTEND;VALUE=DATE:99991231" in multi_text
    print("✓ 9999-12-31 单日省略 DTEND，多日终点保留合法 DTEND")


def test_ics_uid_stable_with_uid_token():
    """有 uid_token 的行程：编辑日期 / 路线后 UID 不变（日历更新而非新增）。"""
    token = "0123456789abcdef"
    trip = {
        "trip_id": "trip_7",
        "uid_token": token,
        "origin": "北京",
        "destination": "上海",
        "start_date": "2026-12-05",
        "end_date": "2026-12-07",
    }

    def uid_of(value):
        text = render_trip_ics(value, "u1")
        for line in text.replace("\r\n ", "").split("\r\n"):
            if line.startswith("UID:"):
                return line[4:]
        raise AssertionError("缺少 UID 行")

    base_uid = uid_of(trip)
    for change in (
        {"start_date": "2026-12-08", "end_date": "2026-12-10"},
        {"origin": "杭州"},
        {"destination": "广州"},
        {"purpose": "休假"},
    ):
        assert uid_of({**trip, **change}) == base_uid, change
    # 不同 token（同一 trip_id）必须得到不同 UID：删除后编号复用时不能
    # 覆盖日历里已存在的旧事件
    other_uid = uid_of({**trip, "uid_token": "fedcba9876543210"})
    assert other_uid != base_uid
    print("✓ 有 uid_token 的行程 UID 对内容编辑稳定")


def test_ics_uid_without_token_keeps_legacy_formula():
    """历史行程（无 token）UID 逐字保持旧公式：升级不改变导出字节。"""
    trip = {
        "trip_id": "trip_7",
        "origin": "北京",
        "destination": "上海",
        "start_date": "2026-12-05",
        "end_date": "2026-12-07",
    }
    text = render_trip_ics(trip, "u1")
    # 旧公式（user:trip:origin:destination:start:end 的 sha1 前 16 位）
    assert "UID:aligo-b263681c4cbdb96e@aligo.local" in text
    # 非法 token（长度 / 字符不符）同样按旧公式处理
    for bad in ("short", "0123456789ABCDEF", " " + "0" * 16, "0" * 15, "0" * 17):
        bad_text = render_trip_ics({**trip, "uid_token": bad}, "u1")
        assert "UID:aligo-b263681c4cbdb96e@aligo.local" in bad_text, bad
    print("✓ 无 / 非法 uid_token 时 UID 保持旧公式（字节兼容）")


def test_empty_calendar_has_placeholder_component():
    """空日历满足 RFC 5545「至少一个组件」：输出可被忽略的扩展组件。"""
    text = render_trips_ics([], "u1")
    assert "BEGIN:VEVENT" not in text
    assert "BEGIN:X-ALIGO-EMPTY" in text
    assert "X-ALIGO-EMPTY:1" in text
    assert "END:X-ALIGO-EMPTY" in text
    from utils.trip_import import parse_trips_ics

    rows, errors = parse_trips_ics(text)
    assert rows == [] and errors[0][1].startswith("未找到 VEVENT"), errors
    print("✓ 空日历含可忽略的扩展组件（仍无 VEVENT）")


def test_csv_formula_guard_covers_newlines_and_leading_space():
    """CSV 注入防护覆盖换行 / 前导空白，且导入端可精确反解。"""
    from utils.trip_import import parse_trips_csv

    sep = chr(0x2028)
    trip = {
        "trip_id": "t-inject",
        "origin": "\r=cmd|' /C calc'!A0",
        "destination": "\n=HYPERLINK(\"http://evil\",\"x\")",
        "start_date": "2026-12-05",
        "end_date": "2026-12-06",
        "purpose": " =SUM(A1)",
    }
    text = render_trips_csv([trip])
    rows, errors = parse_trips_csv(text)
    assert errors == [], errors
    row = rows[0]
    # CR 按身份口径归一为 LF；保护引号在字段首字符，导入可逆
    assert row["origin"] == "\n=cmd|' /C calc'!A0", repr(row["origin"])
    assert row["destination"].startswith("\n=HYPERLINK"), repr(row["destination"])
    assert row["purpose"] == " =SUM(A1)", repr(row["purpose"])
    # U+2028 清洗成空格后仍要防护（否则前导空白绕过）
    trip2 = dict(trip, purpose=sep + "=cmd")
    rows2, _ = parse_trips_csv(render_trips_csv([trip2]))
    assert rows2[0]["purpose"] == " =cmd", repr(rows2[0]["purpose"])
    print("✓ CSV 公式防护覆盖换行 / 前导空白且可逆")


def test_identity_projection_matches_export_cleaning():
    """身份归一与导出清洗同一投影：控制字符 / U+2028 不再制造重复身份。"""
    from utils.trip_export import normalize_identity_text

    sep = chr(0x2028)
    assert normalize_identity_text("上" + sep + "海") == "上 海"
    assert normalize_identity_text("上\x1b[31m海") == "上[31m海"
    assert normalize_identity_text("北京\r\n上海") == "北京\n上海"
    assert normalize_identity_text("北京 上海") != normalize_identity_text(
        "北京\n上海"
    ), "空格与换行仍是不同身份"
    print("✓ 身份投影 = 导出清洗投影（空格 ≠ 换行不变）")


def test_export_strips_unicode_line_separators_and_counts_events():
    """U+2028/U+2029 清洗：CSV 注入前缀不被绕过、ICS 计数不受行分隔符影响。"""
    from utils.trip_export import count_ics_events

    sep = chr(0x2028)
    sep2 = chr(0x2029)
    trip = {
        "trip_id": "t-sep",
        "origin": "上海" + sep + "=cmd|' /C calc'!A0",
        "destination": "北京" + sep2 + "第二行",
        "start_date": "2026-12-05",
        "end_date": "2026-12-07",
        "purpose": "A" + sep + "BEGIN:VEVENT",
    }
    csv_text = render_trips_csv([trip])
    assert sep not in csv_text and sep2 not in csv_text
    assert "上海 =cmd" in csv_text, "载荷保留为单元格内部文本"
    assert "'=cmd" not in csv_text, "公式字符不再处于单元格开头"
    ics = render_trips_ics([trip], "u1")
    assert sep not in ics and sep2 not in ics
    assert count_ics_events(ics) == 1
    # 计数只按真实换行切分：直接构造含 U+2028 的行不能虚增事件数
    crafted = "BEGIN:VEVENT\r\nX:a" + sep + "BEGIN:VEVENT\r\nEND:VEVENT\r\n"
    assert count_ics_events(crafted) == 1
    print("✓ 导出清洗 U+2028/U+2029，事件计数不受行分隔符影响")


if __name__ == "__main__":
    import _tmp_registry

    _tmp_registry.install()
    import sys

    # Windows 控制台 / 重定向管道默认 GBK：✓ 等字符会抛 UnicodeEncodeError，
    # 断言通过也会以 traceback 退出；先切 UTF-8（不支持时保持原状）
    try:
        for _stream in (sys.stdout, sys.stderr):
            _reconfigure = getattr(_stream, "reconfigure", None)
            if callable(_reconfigure):
                _reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        pass
    test_header_and_rows()
    test_formula_prefix_neutralized()
    test_empty_trips()
    test_ics_single_trip()
    test_ics_missing_or_invalid_dates()
    test_ics_escape_and_multiple_events()
    test_csv_uses_single_line_endings()
    test_ics_lines_folded_at_75_bytes()
    test_ics_fold_large_field_is_linear()
    test_render_trips_html()
    test_many_leading_quotes_do_not_recurse()
    test_unpadded_dates_normalized_in_exports()
    test_trips_html_normalizes_unpadded_dates()
    test_render_strips_controls_and_surrogates()
    test_html_table_wraps_long_unbroken_fields()
    test_count_ics_events_unfolds_before_counting()
    # 日志断言用例：直跑用 caplog 替身真正执行
    import _caplog_shim

    _shim = _caplog_shim.CaplogShim()
    try:
        test_ics_unparsable_end_warns(_shim)
    finally:
        _shim.close()
    test_ics_slash_dates_parse_like_iso()
    test_ics_uid_stable_with_uid_token()
    test_ics_uid_without_token_keeps_legacy_formula()
    test_ics_9999_single_day_omits_dtend()
    test_empty_calendar_has_placeholder_component()
    test_csv_formula_guard_covers_newlines_and_leading_space()
    test_identity_projection_matches_export_cleaning()
    test_export_strips_unicode_line_separators_and_counts_events()
    print("\n全部通过")
