"""实体归一测试（城市名 / 中文日期，纯 Python）。

运行：python tests/test_entity_normalize.py
"""
import os
import sys
from datetime import date

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.entity_normalize import (
    normalize_city,
    normalize_date,
    normalize_event_data,
)

_BASE = date(2026, 3, 4)  # 周三


def test_city_suffix_and_whitespace():
    assert normalize_city("杭州市") == "杭州"
    assert normalize_city("浙江省") == "浙江"
    assert normalize_city(" 北京市 ") == "北京"
    assert normalize_city("香港特别行政区") == "香港"
    assert normalize_city("") == ""
    assert normalize_city("乌鲁木齐") == "乌鲁木齐"
    print("✓ 城市名去行政后缀 / 空白")


def test_date_iso_and_chinese_absolute():
    assert normalize_date("2026-03-04", _BASE) == "2026-03-04"
    assert normalize_date("2026/3/5", _BASE) == "2026-03-05"
    assert normalize_date("3月10日", _BASE) == "2026-03-10"
    assert normalize_date("2026年12月1日", _BASE) == "2026-12-01"
    assert normalize_date("2026-13-01", _BASE) is None
    print("✓ ISO / 中文绝对日期")


def test_date_relative_days_and_weeks():
    assert normalize_date("今天", _BASE) == "2026-03-04"
    assert normalize_date("明天", _BASE) == "2026-03-05"
    assert normalize_date("后天", _BASE) == "2026-03-06"
    assert normalize_date("大后天", _BASE) == "2026-03-07"
    assert normalize_date("本周五", _BASE) == "2026-03-06"
    assert normalize_date("下周三", _BASE) == "2026-03-11"
    assert normalize_date("周三", _BASE) == "2026-03-04"
    # 周四问「周三」：今天已过，取下周
    assert normalize_date("周三", date(2026, 3, 5)) == "2026-03-11"
    assert normalize_date("下下周一", _BASE) == "2026-03-16"
    assert normalize_date("下周八", _BASE) is None
    print("✓ 相对日期 / 周表达")


def test_date_month_end_and_next_month():
    assert normalize_date("下个月5号", _BASE) == "2026-04-05"
    assert normalize_date("下个月5号", date(2026, 12, 10)) == "2027-01-05"
    assert normalize_date("月底", date(2026, 2, 10)) == "2026-02-28"
    assert normalize_date("月初", date(2026, 2, 10)) == "2026-02-01"
    print("✓ 下个月 / 月底 / 月初")


def test_normalize_event_data_copy_and_absolute_dates():
    data = {
        "origin": "上海市",
        "destination": "杭州市",
        "start_date": "2026年3月4日",
        "end_date": "2026-03-06",
        "purpose": "出差",
    }
    result = normalize_event_data(data)
    assert result["origin"] == "上海"
    assert result["destination"] == "杭州"
    assert result["start_date"] == "2026-03-04"
    assert result["end_date"] == "2026-03-06"
    assert data["destination"] == "杭州市", "不能修改调用方原字典"
    assert normalize_event_data(None) == {}
    print("✓ 事项数据归一（副本）")


def test_extract_requirements_uses_normalized_event_data():
    from utils.itinerary_validation import extract_requirements

    requirements = extract_requirements(
        "去杭州市玩3天",
        {"destination": "杭州市", "start_date": "2026年3月4日"},
    )
    assert requirements["start_date"] == "2026-03-04"
    assert requirements["duration_days"] == 3
    print("✓ 行程校验需求使用归一后的日期")


def test_date_year_rollover_for_past_month_day():
    """无年份月日已过去时顺延到明年；显式年份不滚动。"""
    base = date(2026, 11, 23)
    assert normalize_date("1月5日", base) == "2027-01-05"
    assert normalize_date("12月1日", base) == "2026-12-01"
    assert normalize_date("11月23日", base) == "2026-11-23"
    assert normalize_date("2027年1月5日", base) == "2027-01-05"
    assert normalize_date("2026年1月5日", base) == "2026-01-05"
    assert normalize_date("2月29日", date(2026, 3, 1)) is None
    print("✓ 无年份日期跨年顺延")


def test_invalid_event_dates_are_dropped():
    """不可能日期不能原样透传成合法需求。"""
    result = normalize_event_data(
        {
            "start_date": "3月32日",
            "end_date": "不是日期",
            "purpose": "出差",
        }
    )
    assert "start_date" not in result
    assert "end_date" not in result
    assert result["purpose"] == "出差"
    print("✓ 非法事项日期丢弃")


def test_extract_requirements_ignores_unparseable_dates():
    """需求提取只接受可解析的 ISO 日期。"""
    from utils.itinerary_validation import extract_requirements

    requirements = extract_requirements(
        "去北京", {"start_date": "3月32日", "end_date": "2026-02-30"}
    )
    assert "start_date" not in requirements
    assert "end_date" not in requirements
    print("✓ 需求层拒绝非法日期")


if __name__ == "__main__":
    import _stdio

    _stdio.ensure_utf8()
    test_city_suffix_and_whitespace()
    test_date_iso_and_chinese_absolute()
    test_date_relative_days_and_weeks()
    test_date_month_end_and_next_month()
    test_normalize_event_data_copy_and_absolute_dates()
    test_extract_requirements_uses_normalized_event_data()
    test_date_year_rollover_for_past_month_day()
    test_invalid_event_dates_are_dropped()
    test_extract_requirements_ignores_unparseable_dates()
    print("\n全部通过")
