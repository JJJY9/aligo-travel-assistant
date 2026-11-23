"""行程约束校验测试（纯 Python，无需模型 / Milvus）。

覆盖：需求提取（预算 / 必去项 / 天数）、五项校验、问题格式化。

运行：python tests/test_itinerary_validation.py
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.itinerary_validation import (
    extract_requirements,
    format_issues,
    validate_itinerary,
)


def test_extract_requirements_budget_must_see_duration():
    """从自然语言里提取预算、必去项（含「和」拆分）与天数。"""
    requirements = extract_requirements(
        "下周三去杭州玩3天，预算5000元，一定要去西湖和灵隐寺逛逛"
    )
    assert requirements["budget"] == 5000
    assert requirements["duration_days"] == 3
    assert "西湖" in requirements["must_see"]
    assert "灵隐寺" in requirements["must_see"]
    print("✓ 需求提取：预算 / 必去项 / 天数")


def test_extract_requirements_wan_and_chinese_duration():
    """万元预算与中文天数归一。"""
    requirements = extract_requirements("预算 2 万元，玩三天，必去故宫")
    assert requirements["budget"] == 20000
    assert requirements["duration_days"] == 3
    assert requirements["must_see"] == ["故宫"]
    print("✓ 万元预算 / 中文天数")


def test_date_day_is_not_duration_and_dates_drive_day_count():
    """「3月4日」的「4日」不是天数；起止日期完整时按日期算天数。"""
    requirements = extract_requirements("3月4日出发去杭州玩3天")
    assert requirements["duration_days"] == 3
    requirements = extract_requirements(
        "",
        {
            "start_date": "2026-03-04",
            "end_date": "2026-03-06",
            "duration_days": 5,
        },
    )
    assert requirements["duration_days"] == 3, "应按起止日期算天数（含首尾）"
    print("✓ 日期不再被当作天数，起止日期决定天数")


def test_budget_does_not_cross_clause_punctuation():
    """预算关键词不能越过逗号 / 句号去匹配后面无关的金额。"""
    assert "budget" not in extract_requirements("预算还没定，酒店 500 元")
    assert extract_requirements("预算大概是5000元")["budget"] == 5000
    print("✓ 预算提取不跨标点")


def test_must_see_trims_duration_and_budget_fragments():
    """必去项捕获里的天数 / 预算片段要截掉，不能变成完整地名。"""
    requirements = extract_requirements("必去西湖玩3天，预算5000元")
    assert requirements["must_see"] == ["西湖"], requirements
    requirements = extract_requirements("想去故宫预算5000")
    assert requirements["must_see"] == ["故宫"], requirements
    assert requirements["budget"] == 5000
    print("✓ 必去项截掉天数 / 预算片段")


def test_must_see_skips_narrative_and_preserves_connector_names():
    """「北京旅游」「景点有哪些」不是地名；颐和园 / 和平饭店不能被拆坏。"""
    assert extract_requirements("我想去北京旅游")["must_see"] == ["北京"]
    assert "must_see" not in extract_requirements("北京必去景点有哪些")
    assert extract_requirements("想去颐和园")["must_see"] == ["颐和园"]
    assert extract_requirements("必去和平饭店")["must_see"] == ["和平饭店"]
    assert extract_requirements("必去颐和园和圆明园")["must_see"] == [
        "颐和园",
        "圆明园",
    ]
    print("✓ 必去项保护连接字地名并过滤叙述词")


def test_must_see_strips_destination_and_purpose():
    """出行目的（北京出差）不是景点；城市前缀后的景点保留。"""
    requirements = extract_requirements(
        "想去北京出差3天", {"destination": "北京市"}
    )
    assert "must_see" not in requirements, requirements
    requirements = extract_requirements(
        "想去杭州西湖游玩", {"destination": "杭州市"}
    )
    assert requirements["must_see"] == ["西湖"], requirements
    print("✓ 必去项剥离城市前缀与出行目的")


def test_budget_does_not_cross_enumeration_punctuation():
    """顿号 / 冒号后的分项金额不能被当成总预算。"""
    assert "budget" not in extract_requirements(
        "预算还没定、酒店500元左右"
    )
    assert "budget" not in extract_requirements("费用：交通500，住宿600")
    assert extract_requirements("预算：5000元")["budget"] == 5000
    print("✓ 预算不跨顿号 / 冒号")


def test_must_see_only_checks_daily_plans():
    """备注里提到的地名不算已安排：必须出现在每日行程里。"""
    itinerary = {
        "notes": "本行程参考雷峰塔夜景攻略",
        "daily_plans": [
            {
                "day": 1,
                "activities": [
                    {"time": "09:00-12:00", "title": "西湖"}
                ],
            }
        ],
    }
    issues = validate_itinerary(itinerary, {"must_see": ["雷峰塔"]})
    assert [issue["code"] for issue in issues] == ["must_see_missing"], issues
    print("✓ 必去项只核对每日安排")


def test_validate_passes_for_consistent_plan():
    """天数、时间、预算、必去项、费用一致时无问题。"""
    itinerary = {
        "estimated_budget": 900,
        "daily_plans": [
            {
                "day": 1,
                "activities": [
                    {"time": "09:00-12:00", "title": "西湖", "cost": 600},
                    {"time": "13:00-17:00", "title": "灵隐寺", "cost": 300},
                ],
            }
        ],
    }
    requirements = {
        "duration_days": 1,
        "budget": 1000,
        "must_see": ["西湖", "灵隐寺"],
    }
    assert validate_itinerary(itinerary, requirements) == []
    print("✓ 合规行程五项校验通过")


def test_validate_detects_all_five_checks():
    """天数 / 时间冲突 / 预算 / 必去项 / 费用异常都能被检出。"""
    itinerary = {
        "estimated_budget": 5000,
        "daily_plans": [
            {
                "day": 1,
                "activities": [
                    {"time": "09:00-12:00", "title": "西湖", "cost": 1000},
                    {"time": "11:00-13:00", "title": "灵隐寺", "cost": -5},
                    {"time": "15:00-09:00", "title": "夜游", "cost": 100},
                ],
            }
        ],
    }
    requirements = {
        "duration_days": 2,
        "budget": 1000,
        "must_see": ["雷峰塔"],
    }
    issues = validate_itinerary(itinerary, requirements)
    codes = {issue["code"] for issue in issues}
    assert {
        "day_count_mismatch",
        "time_conflict",
        "invalid_time_range",
        "budget_exceeded",
        "must_see_missing",
        "cost_anomaly",
        "cost_mismatch",
    } <= codes, issues
    assert "雷峰塔" in format_issues(issues)
    print("✓ 五项校验：天数 / 时间 / 预算 / 必去项 / 费用")


def test_validate_handles_empty_and_unknown_shapes():
    """空行程 / 非对象输入返回明确问题而不是抛异常。"""
    assert validate_itinerary(None) [0]["code"] == "missing_itinerary"
    assert validate_itinerary({})[0]["code"] == "missing_daily_plans"
    assert validate_itinerary({"daily_plans": []})[0]["code"] == "missing_daily_plans"
    print("✓ 空行程 / 非法形状的兜底")


if __name__ == "__main__":
    import _stdio

    _stdio.ensure_utf8()
    test_extract_requirements_budget_must_see_duration()
    test_extract_requirements_wan_and_chinese_duration()
    test_date_day_is_not_duration_and_dates_drive_day_count()
    test_budget_does_not_cross_clause_punctuation()
    test_must_see_trims_duration_and_budget_fragments()
    test_must_see_skips_narrative_and_preserves_connector_names()
    test_must_see_strips_destination_and_purpose()
    test_budget_does_not_cross_enumeration_punctuation()
    test_must_see_only_checks_daily_plans()
    test_validate_passes_for_consistent_plan()
    test_validate_detects_all_five_checks()
    test_validate_handles_empty_and_unknown_shapes()
    print("\n全部通过")
