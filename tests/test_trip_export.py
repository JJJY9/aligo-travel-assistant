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

from utils.trip_export import render_trips_csv


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
        [{"origin": "=1+1", "destination": "@cmd", "purpose": "+SUM(A1)"}]
    )
    row = list(csv.reader(io.StringIO(text[1:])))[1]
    assert row[0] == "'=1+1"
    assert row[1] == "'@cmd"
    assert row[4] == "'+SUM(A1)"
    print("✓ 公式前缀加单引号")


def test_empty_trips():
    rows = list(csv.reader(io.StringIO(render_trips_csv([])[1:])))
    assert len(rows) == 1, "无行程时只输出表头"
    print("✓ 空列表仅表头")


if __name__ == "__main__":
    test_header_and_rows()
    test_formula_prefix_neutralized()
    test_empty_trips()
    print("\n全部通过")
