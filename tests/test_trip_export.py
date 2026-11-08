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
    """上千个前导单引号不能触发 RecursionError（导入 / 导出共用该判定）。"""
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
    assert errors == []
    assert rows[0]["destination"] == tricky, "往返必须保真"
    print("✓ 超多前导单引号不再递归溢出")


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


if __name__ == "__main__":
    test_header_and_rows()
    test_formula_prefix_neutralized()
    test_empty_trips()
    test_ics_single_trip()
    test_ics_missing_or_invalid_dates()
    test_ics_escape_and_multiple_events()
    test_csv_uses_single_line_endings()
    test_ics_lines_folded_at_75_bytes()
    test_render_trips_html()
    test_many_leading_quotes_do_not_recurse()
    test_unpadded_dates_normalized_in_exports()
    test_trips_html_normalizes_unpadded_dates()
    print("\n全部通过")
