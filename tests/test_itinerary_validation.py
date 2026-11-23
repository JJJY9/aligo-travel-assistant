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
    test_validate_passes_for_consistent_plan()
    test_validate_detects_all_five_checks()
    test_validate_handles_empty_and_unknown_shapes()
    print("\n全部通过")
