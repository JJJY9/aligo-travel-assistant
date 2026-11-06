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

from utils.trip_export import _ics_escape, render_trip_ics, render_trips_csv, render_trips_ics


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


if __name__ == "__main__":
    test_header_and_rows()
    test_formula_prefix_neutralized()
    test_empty_trips()
    test_ics_single_trip()
    test_ics_missing_or_invalid_dates()
    test_ics_escape_and_multiple_events()
    print("\n全部通过")
