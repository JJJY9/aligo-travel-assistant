"""实体归一（v2.0）：城市名与中文日期。

用途：事项收集 / 行程校验拿到的城市与日期可能是「杭州市」「2026年3月4日」
「下周三」「月底」等写法；统一归一后再进入调度、求解与校验，避免同一实体
在不同环节被当成不同值。
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

_CITY_SUFFIXES = ("特别行政区", "自治区", "省", "市")
_CN_WEEKDAYS = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
}
_RELATIVE_DAYS = {
    "今天": 0,
    "今日": 0,
    "明天": 1,
    "明日": 1,
    "后天": 2,
    "大后天": 3,
}
_WEEK_PREFIX_OFFSET = {
    "下下周": 14,
    "下周": 7,
    "本周": 0,
    "这周": 0,
    "周": 0,
    "星期": 0,
}


def normalize_city(value: object) -> str:
    """去掉城市名常见行政后缀与首尾空白（「杭州市」→「杭州」）。"""
    text = str(value or "").replace("﻿", "").strip()
    if not text:
        return ""
    for suffix in _CITY_SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
            break
    return text.strip()


def _parse_iso(text: str) -> str | None:
    match = re.fullmatch(
        r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text[:10]
    )
    if not match:
        return None
    try:
        return date(
            int(match.group(1)), int(match.group(2)), int(match.group(3))
        ).isoformat()
    except ValueError:
        return None


def normalize_date(
    value: object, today: date | None = None
) -> str | None:
    """把常见中文 / ISO 日期归一为 ``YYYY-MM-DD``；无法解析返回 None。

    支持：ISO（含斜杠）、``3月4日`` / ``2026年3月4日``、今天 / 明天 /
    后天 / 大后天、本周 / 下周 / 下下周 / 周X / 星期X、下个月N号、月底。
    未写年份的日期按 ``today`` 所在年、已过去的月日顺延到明年；
    ``周X`` 未指明周次时取未来最近一天。
    """
    base = today or date.today()
    text = str(value or "").strip()
    if not text:
        return None

    iso = _parse_iso(text)
    if iso:
        return iso

    if text in _RELATIVE_DAYS:
        return (base + timedelta(days=_RELATIVE_DAYS[text])).isoformat()

    match = re.fullmatch(
        r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})[日号]?", text
    )
    if match:
        explicit_year = match.group(1)
        year = int(explicit_year) if explicit_year else base.year
        try:
            parsed = date(year, int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
        # 无年份的月日已过去时按明年算（11 月说「1月5日」指的是明年），
        # 与上面「周X 取未来最近一天」一致；跨年输出过去日期会让规划 /
        # 校验拿到早已结束的行程
        if explicit_year is None and parsed < base:
            try:
                parsed = parsed.replace(year=year + 1)
            except ValueError:  # 2月29日落到非闰年
                return None
        return parsed.isoformat()

    match = re.fullmatch(
        r"(下下周|下周|本周|这周|周|星期)([一二三四五六日天])", text
    )
    if match:
        prefix, weekday_text = match.group(1), match.group(2)
        monday = base - timedelta(days=base.weekday())
        target = monday + timedelta(
            days=_WEEK_PREFIX_OFFSET[prefix] + _CN_WEEKDAYS[weekday_text]
        )
        # 「周三」未指明周次：今天已过则取下周
        if prefix in ("周", "星期") and target < base:
            target += timedelta(days=7)
        return target.isoformat()

    match = re.fullmatch(r"下个?月(\d{1,2})[日号]?", text)
    if match:
        if base.month == 12:
            year, month = base.year + 1, 1
        else:
            year, month = base.year, base.month + 1
        try:
            return date(year, month, int(match.group(1))).isoformat()
        except ValueError:
            return None

    if text in ("月底", "月末"):
        if base.month == 12:
            first_next = date(base.year + 1, 1, 1)
        else:
            first_next = date(base.year, base.month + 1, 1)
        return (first_next - timedelta(days=1)).isoformat()
    if text == "月初":
        return date(base.year, base.month, 1).isoformat()
    return None


def normalize_event_data(data: object) -> dict[str, Any]:
    """归一事项收集结果里的 origin / destination / 起止日期（副本）。"""
    if not isinstance(data, dict):
        return {}
    result = dict(data)
    for key in ("origin", "destination"):
        if key in result:
            result[key] = normalize_city(result[key])
    for key in ("start_date", "end_date"):
        if key in result:
            raw = result[key]
            normalized = normalize_date(raw)
            if normalized:
                result[key] = normalized
            elif isinstance(raw, str) and raw.strip():
                # 非法 / 不可能日期（非闰年 2月29日、3月32日）不能原样
                # 透传：下游会把任何非空字符串当成合法日期需求
                result.pop(key, None)
    return result
