"""行程约束校验（v2.0）。

职责：
1. 从用户需求 / 事项收集结果里提取可校验的约束：天数、预算、必去项；
2. 对 LLM 生成的行程做五项检查：天数、时间冲突、预算、必去项、费用复核；
3. 输出结构化问题列表，供编排层展示或带原因重新生成。

只做确定性校验，不调用模型。
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Iterable

_BUDGET_RE = re.compile(
    r"(?:预算|总预算|花费|费用)[^\d]{0,8}(\d+(?:\.\d+)?)\s*(万元|万|元|块)?"
)
_MUST_SEE_RE = re.compile(
    r"(?:必去|一定要去|必须去|想去|打卡|别错过)[：:\s]*"
    r"([一-鿿A-Za-z0-9·]{2,24})"
)
_DURATION_RE = re.compile(r"(\d+|[一二两三四五六七八九十]+)\s*(?:天|日)")
_TIME_RANGE_RE = re.compile(
    r"(\d{1,2})\s*[:：]\s*(\d{2})\s*(?:-|~|—|至|到)\s*"
    r"(\d{1,2})\s*[:：]\s*(\d{2})"
)
_CN_DIGITS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def _cn_number(value: str) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text[0] == "十":
        number = 10
        if len(text) > 1 and text[1] in _CN_DIGITS:
            number += _CN_DIGITS[text[1]]
        return number
    if text[0] in _CN_DIGITS:
        number = _CN_DIGITS[text[0]]
        if len(text) > 1 and text[1] == "十":
            number = number * 10 + (
                _CN_DIGITS.get(text[2], 0) if len(text) > 2 else 0
            )
        return number
    return None


def _clean_must_see(value: str) -> list[str]:
    """把「西湖和灵隐寺」拆成多个必去项，并去掉尾随动词 / 助词。"""
    parts = re.split(r"[和与及、，,；;/]+", str(value or ""))
    trailing = (
        "逛逛",
        "看看",
        "玩一玩",
        "玩",
        "打卡",
        "参观",
        "游览",
        "的",
        "了",
        "一下",
    )
    names: list[str] = []
    for part in parts:
        name = part.strip().strip("。.!！?？ ")
        changed = True
        while changed and name:
            changed = False
            for suffix in trailing:
                if name.endswith(suffix) and len(name) > len(suffix):
                    name = name[: -len(suffix)].strip()
                    changed = True
        if 2 <= len(name) <= 24 and name not in names:
            names.append(name)
    return names


def extract_requirements(
    user_query: object, event_data: object = None
) -> dict[str, Any]:
    """从用户原话与事项收集结果提取可校验约束（缺失的键不返回）。"""
    requirements: dict[str, Any] = {}
    event = event_data if isinstance(event_data, dict) else {}
    duration = event.get("duration_days")
    if isinstance(duration, (int, float)) and int(duration) > 0:
        requirements["duration_days"] = int(duration)
    for key in ("start_date", "end_date"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            requirements[key] = value.strip()

    text = str(user_query or "")
    budget_match = _BUDGET_RE.search(text)
    if budget_match:
        amount = float(budget_match.group(1))
        if budget_match.group(2) in ("万元", "万"):
            amount *= 10000
        if math.isfinite(amount) and amount > 0:
            requirements["budget"] = amount

    must_see: list[str] = []
    for match in _MUST_SEE_RE.finditer(text):
        for name in _clean_must_see(match.group(1)):
            if name not in must_see:
                must_see.append(name)
    if must_see:
        requirements["must_see"] = must_see

    if "duration_days" not in requirements:
        duration_match = _DURATION_RE.search(text)
        if duration_match:
            number = _cn_number(duration_match.group(1))
            if number and 0 < number <= 365:
                requirements["duration_days"] = number
    return requirements


def _daily_plans(itinerary: object) -> list[dict]:
    if not isinstance(itinerary, dict):
        return []
    plans = itinerary.get("daily_plans")
    if isinstance(plans, list):
        return [plan for plan in plans if isinstance(plan, dict)]
    if isinstance(plans, dict):
        ordered: list[dict] = []
        for key in sorted(plans, key=lambda item: str(item)):
            value = plans[key]
            if isinstance(value, dict):
                ordered.append(value)
        return ordered
    return []


def _activities(plan: dict) -> list[dict]:
    items = plan.get("activities")
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]
    if isinstance(items, dict):
        return [items]
    return []


def _parse_time_range(value: object) -> tuple[int, int] | None:
    match = _TIME_RANGE_RE.search(str(value or ""))
    if not match:
        return None
    hour, minute, end_hour, end_minute = (int(part) for part in match.groups())
    if hour > 23 or end_hour > 23 or minute > 59 or end_minute > 59:
        return None
    return hour * 60 + minute, end_hour * 60 + end_minute


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        match = re.search(r"-?\d+(?:\.\d+)?", value)
        if not match:
            return None
        number = float(match.group(0))
    else:
        return None
    return number if math.isfinite(number) else None


def _activity_cost(activity: dict) -> float | None:
    for key in ("cost", "price", "fee", "estimated_cost", "花费", "费用"):
        if key in activity:
            number = _number(activity.get(key))
            if number is not None:
                return number
    return None


def _estimate(itinerary: dict) -> float | None:
    for key in ("estimated_budget", "total_cost", "budget", "总预算"):
        if key in itinerary:
            number = _number(itinerary.get(key))
            if number is not None:
                return number
    return None


def validate_itinerary(
    itinerary: object, requirements: dict[str, Any] | None = None
) -> list[dict[str, str]]:
    """五项检查；返回 ``[{code, message}, ...]``，空列表表示通过。"""
    requirements = requirements or {}
    issues: list[dict[str, str]] = []
    if not isinstance(itinerary, dict):
        return [
            {
                "code": "missing_itinerary",
                "message": "行程结果不是对象，无法校验",
            }
        ]
    plans = _daily_plans(itinerary)
    if not plans:
        return [
            {
                "code": "missing_daily_plans",
                "message": "行程没有每日安排（daily_plans 为空）",
            }
        ]

    # 1) 天数
    expected_days = requirements.get("duration_days")
    if isinstance(expected_days, int) and expected_days > 0 and len(plans) != expected_days:
        issues.append(
            {
                "code": "day_count_mismatch",
                "message": (
                    f"要求 {expected_days} 天，实际安排了 {len(plans)} 天"
                ),
            }
        )

    # 2) 时间冲突 / 非法时间
    for day_index, plan in enumerate(plans, 1):
        intervals: list[tuple[int, int, str]] = []
        for activity in _activities(plan):
            parsed = _parse_time_range(activity.get("time"))
            if parsed is None:
                continue
            start, end = parsed
            label = str(
                activity.get("title")
                or activity.get("name")
                or activity.get("activity")
                or activity.get("time")
            )
            if end <= start:
                issues.append(
                    {
                        "code": "invalid_time_range",
                        "message": f"第 {day_index} 天「{label}」的结束时间不晚于开始时间",
                    }
                )
                continue
            intervals.append((start, end, label))
        intervals.sort(key=lambda item: (item[0], item[1]))
        for previous, current in zip(intervals, intervals[1:]):
            if current[0] < previous[1]:
                issues.append(
                    {
                        "code": "time_conflict",
                        "message": (
                            f"第 {day_index} 天「{previous[2]}」与「{current[2]}」"
                            "时间重叠"
                        ),
                    }
                )

    # 3) + 5) 费用复核：逐项合法性、合计与申报预算一致性
    costs: list[float] = []
    for plan in plans:
        for activity in _activities(plan):
            cost = _activity_cost(activity)
            if cost is None:
                continue
            label = str(
                activity.get("title")
                or activity.get("name")
                or activity.get("activity")
                or ""
            )
            if cost < 0 or cost > 1_000_000:
                issues.append(
                    {
                        "code": "cost_anomaly",
                        "message": f"「{label}」的费用 {cost:g} 元异常（需为 0–1000000）",
                    }
                )
            costs.append(cost)
    item_total = sum(cost for cost in costs if 0 <= cost <= 1_000_000)
    estimated = _estimate(itinerary)
    if costs and estimated is not None and item_total > 0:
        if abs(estimated - item_total) / item_total > 0.2:
            issues.append(
                {
                    "code": "cost_mismatch",
                    "message": (
                        f"申报总费用 {estimated:g} 元与分项合计 {item_total:g} 元"
                        "差异超过 20%"
                    ),
                }
            )

    # 4) 预算
    budget = requirements.get("budget")
    if isinstance(budget, (int, float)) and budget > 0:
        actual = estimated if estimated is not None else item_total
        if actual > budget * 1.02:
            issues.append(
                {
                    "code": "budget_exceeded",
                    "message": f"预计费用 {actual:g} 元超出预算 {budget:g} 元",
                }
            )

    # 5) 必去项
    must_see = requirements.get("must_see")
    if isinstance(must_see, list):
        text = json.dumps(itinerary, ensure_ascii=False).casefold()
        for name in must_see:
            if str(name).casefold() not in text:
                issues.append(
                    {
                        "code": "must_see_missing",
                        "message": f"必去项「{name}」未出现在行程中",
                    }
                )
    return issues


def format_issues(issues: Iterable[dict[str, str]]) -> str:
    """把校验问题格式化为给模型 / 用户看的编号列表。"""
    lines = [
        f"{index}. [{issue.get('code', 'issue')}] {issue.get('message', '')}"
        for index, issue in enumerate(issues, 1)
    ]
    return "\n".join(lines)
