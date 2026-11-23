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
from datetime import date as _date
from typing import Any, Iterable

from utils.entity_normalize import normalize_event_data

_BUDGET_RE = re.compile(
    r"(?:预算|总预算|花费|费用)"
    # 冒号后必须紧跟金额（「费用：交通500」是分项不是总预算）；
    # 无冒号时允许「预算大约是」等短填充，但不能跨越顿号 / 逗号 / 句号
    r"(?:[：:]\s*|[^\d。！？；;，,、：:\n]{0,8})"
    # 千分位组（10,000 / 1，500 / 10 000）整体匹配：只认「逗号后恰好三位」，
    # 避免把「预算5000，3000」这类并列数字连成一个巨款
    r"(\d{1,3}(?:[,， ]\d{3})+|\d+)(?:\.\d+)?\s*(万元|万|元|块)?"
)
_MUST_SEE_RE = re.compile(
    r"(?:必去|一定要去|必须去|想去|打卡|别错过)[：:\s]*"
    # 捕获串允许顿号 / 逗号分隔符（交给 _split_must_see 拆分），但惰性
    # 截到下一个触发词 / 句末为止——不能让一个触发词的捕获吞掉下一句
    # 触发词（否则否定语境会连带把后面的正常必去项一起丢掉）。没有边界
    # 的超长列表退回 24 字贪心截断，避免吞掉整段叙述
    r"("
    r"[一-鿿A-Za-z0-9·、，,；;/ ]{2,24}?"
    r"(?=(?:必去|一定要去|必须去|想去|打卡|别错过)|[。！？；;\n]|$)"
    r"|"
    r"[一-鿿A-Za-z0-9·、，,；;/ ]{2,24}"
    r")"
)
# 否定语境（不想去 / 不太想去 / 不一定要去 / 不必去 / 无需去）：触发词
# 前 6 个字里出现否定词、且否定词与触发词之间最多隔 2 个字时不算必去项
_MUST_SEE_NEGATION_RE = re.compile(r"(?:不|别|无需|不用|不要)[^，。！？；\n]{0,2}$")
# 费用字段的数值：千分位组（逗号后恰好三位）整体匹配，支持万单位
_NUMBER_RE = re.compile(
    r"(-?(?:\d{1,3}(?:[,， ]\d{3})+|\d+)(?:\.\d+)?)\s*(万元|万|元|块)?"
)
# 城市名的行政后缀：目的地「杭州市」与必去项「杭州市西湖」按「杭州」比较
_ADMIN_SUFFIXES = ("特别行政区", "自治区", "自治州", "地区", "市", "省", "区", "县")
# 「3月4日」里的「4日」是日期不是行程天数：匹配后再看数字前是否紧邻「月」
_DURATION_RE = re.compile(r"(\d+|[一二两三四五六七八九十]+)\s*(?:天|日)")
# 必去项里混入的天数 / 预算片段：截断后再清洗动词后缀
_MUST_SEE_DURATION_RE = re.compile(r"[0-9一二两三四五六七八九十]+\s*[天日].*$")
_MUST_SEE_BUDGET_RE = re.compile(r"(?:预算|花费|费用|人均|总价|左右|以内).*$")
# 「和 / 与 / 及」既可能是并列连接词，也可能是地名的一部分（颐和园、
# 和平饭店）：只在两侧都像完整名称时才拆
_MUST_SEE_CONNECTOR_RE = re.compile(r"[和与及]")
_MUST_SEE_GENERIC = frozenset(
    {
        "景点",
        "景区",
        "地方",
        "城市",
        "路线",
        "行程",
        "酒店",
        "美食",
        "旅游",
        "旅行",
        "好玩",
    }
)
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


def _strip_admin_suffix(value: str) -> str:
    """去掉城市名末尾的行政后缀（杭州市 → 杭州），太短的保持原样。"""
    text = str(value or "").strip()
    for suffix in _ADMIN_SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix) + 1:
            return text[: -len(suffix)]
    return text


def _strip_leading_admin_suffix(value: str) -> str:
    """去掉「市 / 省」等被前缀剥离后残留在开头的一个行政后缀字。"""
    text = str(value or "").strip()
    for suffix in ("市", "省", "区", "县"):
        if text.startswith(suffix) and len(text) > len(suffix) + 1:
            return text[len(suffix) :].strip()
    return text


def _split_connected(part: str) -> list[str]:
    """递归按连接词拆分：取第一个两侧都 ≥2 字的连接点。"""
    connectors = [
        match
        for match in _MUST_SEE_CONNECTOR_RE.finditer(part)
        if 0 < match.start() < len(part) - 1
    ]
    for match in connectors:
        left = part[: match.start()].strip()
        right = part[match.end() :].strip()
        if len(left) >= 2 and len(right) >= 2:
            return [left] + _split_connected(right)
    return [part]


def _split_must_see(value: str) -> list[str]:
    """按顿号 / 逗号与「和 / 与 / 及」拆分必去项，保护地名里的连接字。

    例：「西湖和灵隐寺」→ 西湖 / 灵隐寺；「颐和园」「和平饭店」保持完整
    （连接字两侧不是两个完整名称时不拆）；「颐和园和圆明园」能跳过
    园名里的「和」在后面的连接点拆分。
    """
    parts = [
        part.strip()
        for part in re.split(
            r"[、，,；;/\s]+",
            # 千分位逗号不是枚举分隔符（10,000 不能被拆成「10」「000元」）
            re.sub(r"(?<=\d)[,，](?=\d{3}(?:\D|$))", "", str(value or "")),
        )
        if part.strip()
    ]
    names: list[str] = []
    for part in parts:
        names.extend(_split_connected(part))
    return names


def _clean_must_see(value: str) -> list[str]:
    """把「西湖和灵隐寺」拆成多个必去项，并去掉尾随动词 / 助词。"""
    parts = _split_must_see(value)
    trailing = (
        "逛逛",
        "看看",
        "玩一玩",
        "玩",
        "打卡",
        "参观",
        "游览",
        "旅游",
        "旅行",
        "游玩",
        "出差",
        "好玩",
        "有哪些",
        "有什么",
        "哪些",
        "什么",
        "哪个",
        "哪里",
        "地点",
        "地方",
        "景点",
        "景区",
        "转转",
        "游",
        "的",
        "了",
        "一下",
    )
    names: list[str] = []
    for part in parts:
        name = part.strip().strip("。.!！?？ ")
        # 触发词后面的前置助词 / 疑问词不是地名的一部分（「西湖的哪些景点」）
        name = re.sub(
            r"^(?:的|了|哪些|什么|哪个|哪儿|哪里|地点|地方|景点|还有|大概|大约)+",
            "",
            name,
        ).strip()
        # 「西湖玩3天」「故宫预算5000」：先截掉混入的天数 / 预算片段
        name = _MUST_SEE_DURATION_RE.sub("", name)
        name = _MUST_SEE_BUDGET_RE.sub("", name)
        changed = True
        while changed and name:
            changed = False
            for suffix in trailing:
                if name.endswith(suffix) and len(name) > len(suffix):
                    name = name[: -len(suffix)].strip()
                    changed = True
        if name.isdigit() or name in _MUST_SEE_GENERIC:
            continue
        # 拆分千分位后残留的纯金额片段（「000元」）不是地名
        if re.fullmatch(r"[\d,，.\s]*元?", name):
            continue
        if 2 <= len(name) <= 24 and name not in names:
            names.append(name)
    return names


def _iso_date(value: object) -> _date | None:
    text = str(value or "").strip()
    if len(text) < 10:
        return None
    try:
        return _date.fromisoformat(text[:10])
    except ValueError:
        return None


def _date_range_days(requirements: dict[str, Any]) -> int | None:
    """起止日期都在时用日期算天数（比模型抽取的 duration_days 可靠）。"""
    start = _iso_date(requirements.get("start_date"))
    end = _iso_date(requirements.get("end_date"))
    if start is None or end is None or end < start:
        return None
    days = (end - start).days + 1
    return days if 0 < days <= 366 else None


def _text_duration(text: str) -> int | None:
    """「N 天 / 日」天数；跳过「3月4日」「第一天」「5日到7日」这类日期。

    序数（第 N 天）与日期区间（N 日到 M 日）不是行程总天数：把「第一天」
    读成 1 天会让天数校验把正确行程判失败。
    """
    for match in _DURATION_RE.finditer(text):
        prefix = text[: match.start()].rstrip()
        if prefix.endswith(("月", "第", "到", "至", "~", "—", "-")):
            continue
        suffix = text[match.end() :].lstrip()
        if suffix[:1] in ("到", "至", "~", "—", "-"):
            continue
        number = _cn_number(match.group(1))
        if number and 0 < number <= 365:
            return number
    return None


def extract_requirements(
    user_query: object, event_data: object = None
) -> dict[str, Any]:
    """从用户原话与事项收集结果提取可校验约束（缺失的键不返回）。"""
    requirements: dict[str, Any] = {}
    event = normalize_event_data(event_data)
    duration = event.get("duration_days")
    if isinstance(duration, (int, float)) and int(duration) > 0:
        requirements["duration_days"] = int(duration)
    for key in ("start_date", "end_date"):
        value = event.get(key)
        # 只接受能被解析成 ISO 日期的值：归一失败的原串不能当合法需求
        if isinstance(value, str) and _iso_date(value) is not None:
            requirements[key] = value.strip()
    # 起止日期完整时以日期算出的天数为准：模型抽取的 duration_days 与
    # 日期冲突时（或根本没抽）会漏掉天数校验
    date_days = _date_range_days(requirements)
    if date_days is not None:
        requirements["duration_days"] = date_days

    text = str(user_query or "")
    budget_match = _BUDGET_RE.search(text)
    if budget_match:
        raw_amount = re.sub(r"[,，\s]", "", budget_match.group(1))
        try:
            amount = float(raw_amount)
        except ValueError:
            amount = 0.0
        if budget_match.group(2) in ("万元", "万"):
            amount *= 10000
        if math.isfinite(amount) and amount > 0:
            requirements["budget"] = amount

    must_see: list[str] = []
    for match in _MUST_SEE_RE.finditer(text):
        # 「不想去 / 不太想去 / 不一定要去 / 不必去」里的地点不是必去项
        negated = text[max(0, match.start() - 6) : match.start()]
        if _MUST_SEE_NEGATION_RE.search(negated):
            continue
        for name in _clean_must_see(match.group(1)):
            if name not in must_see:
                must_see.append(name)
    destination = str(event.get("destination") or "").strip()
    if must_see and destination:
        dest_core = _strip_admin_suffix(destination)
        forms = sorted(
            {form for form in (destination, dest_core) if form},
            key=len,
            reverse=True,
        )
        refined: list[str] = []
        for name in must_see:
            name_core = _strip_admin_suffix(name)
            # 「北京出差」这类只剩城市名的，是出行目的不是景点
            if name == destination or name_core == dest_core:
                continue
            # 「杭州市西湖」「杭州西湖」都剥掉城市前缀后保留「西湖」
            stripped = name
            for form in forms:
                if stripped.startswith(form) and len(stripped) >= len(form) + 2:
                    stripped = _strip_leading_admin_suffix(
                        stripped[len(form) :].strip()
                    )
                    break
            name = stripped
            if name and name not in _MUST_SEE_GENERIC and name not in refined:
                refined.append(name)
        must_see = refined
    if must_see:
        requirements["must_see"] = must_see

    if "duration_days" not in requirements:
        duration_days = _text_duration(text)
        if duration_days:
            requirements["duration_days"] = duration_days
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
    """一天的活动列表：支持列表、「键→活动」字典与 schedule 等别名键。

    LLM 输出偶尔把 activities 写成 ``{"09:00": {...}, "14:00": {...}}``，
    或把安排放在 schedule 键下——只认 list / 单个 dict 会让时间冲突与
    费用检查整体静默跳过。
    """
    for key in ("activities", "schedule", "安排", "计划"):
        items = plan.get(key)
        if isinstance(items, list):
            activities = [item for item in items if isinstance(item, dict)]
            if activities:
                return activities
        elif isinstance(items, dict):
            if any(
                field in items
                for field in ("time", "title", "name", "activity", "cost", "price", "fee")
            ):
                return [items]
            nested = [
                value for value in items.values() if isinstance(value, dict)
            ]
            return nested or [items]
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
        # 费用字段可能是「约1.5万元」「10,000元」：支持千分位与万单位，
        # 否则「1.5万」会被读成 1.5、「10,000」只读到 10
        match = _NUMBER_RE.search(value)
        if not match:
            return None
        raw = re.sub(r"[,，\s]", "", match.group(1))
        try:
            number = float(raw)
        except ValueError:
            return None
        if match.group(2) in ("万元", "万"):
            number *= 10000
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

    # 5) 必去项：只核对每日安排。整份 JSON 会把备注 / 用户原话里提到的
    # 地名也算作「已安排」，行程实际没去也判通过
    must_see = requirements.get("must_see")
    if isinstance(must_see, list):
        text = json.dumps(plans, ensure_ascii=False).casefold()
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
