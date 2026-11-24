"""Personal Agent 分层评测用例生成（v2.0）。

按 12 类场景模板 + 槽位变体确定性生成评测集（默认 500 条）：
问候、行程规划、事项收集、偏好写入、偏好查询、记忆查询、政策 RAG、
天气、网页搜索、多意图、缺失信息澄清、无关问题。

生成结果只是「待评测用例」；回答由调用方注入的 predict 函数产生。
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Sequence

_CITIES = [
    "北京",
    "上海",
    "杭州",
    "苏州",
    "南京",
    "成都",
    "西安",
    "广州",
    "深圳",
    "武汉",
]
_ORIGINS = ["上海", "北京", "南京", "杭州", "苏州", "无锡", "合肥", "武汉"]
_BRANDS = ["汉庭", "如家", "全季", "万豪", "希尔顿", "亚朵"]
_AIRLINES = ["东航", "国航", "南航", "海航", "吉祥"]
_MONTHS = ["一月", "三月", "五月", "七月", "九月", "十一月"]
_DAYS = ["2", "3", "4", "5"]
_SECTORS = ["市内交通", "住宿", "餐饮", "长途交通"]

# (分类, 模板列表, 期望智能体, 需要出现在回答里的关键词槽位)
_TEMPLATES: list[tuple[str, list[str], list[str], list[str]]] = [
    ("greeting", ["你好", "早上好，在吗", "嗨"], [], []),
    (
        "plan_trip",
        [
            "帮我规划下{month}去{city}的{days}天行程",
            "我{days}天后想去{city}玩{days}天",
        ],
        ["itinerary_planning"],
        ["city"],
    ),
    (
        "event_collection",
        [
            "我从{origin}出发去{city}，{month}出发",
            "下个月从{origin}去{city}出差",
        ],
        ["event_collection"],
        ["origin", "city"],
    ),
    (
        "preference_set",
        ["我喜欢住{brand}", "我常坐{airline}", "我出差都住{brand}"],
        ["preference"],
        ["brand_or_airline"],
    ),
    (
        "preference_query",
        ["我住酒店有什么偏好", "我通常坐哪家航空公司"],
        ["memory_query"],
        [],
    ),
    (
        "memory_trip",
        ["我上次去{city}是什么时候", "我去过哪些地方"],
        ["memory_query"],
        [],
    ),
    (
        "policy_rag",
        ["{city}的住宿标准是多少", "出差{sector}报销上限是多少"],
        ["rag_knowledge"],
        ["policy_keyword"],
    ),
    (
        "weather",
        ["{city}明天天气怎么样", "{city}这周末会下雨吗"],
        ["information_query"],
        ["city"],
    ),
    (
        "web_search",
        ["查一下{city}有什么景点", "帮我搜搜{city}的特色小吃"],
        ["information_query"],
        ["city"],
    ),
    (
        "multi_intent",
        [
            "从{origin}去{city}玩{days}天，顺便查下天气并规划行程",
            "我{days}天后从{origin}去{city}，有什么好玩的，帮我安排一下",
        ],
        ["event_collection", "information_query", "itinerary_planning"],
        ["origin", "city"],
    ),
    (
        "clarify",
        ["帮我规划一下行程", "帮我安排一次旅行"],
        ["itinerary_planning"],
        [],
    ),
    (
        "other",
        ["今天股市怎么样", "给我讲个笑话", "你会写代码吗"],
        ["other"],
        [],
    ),
]


def _slot_values() -> dict[str, Sequence[str]]:
    return {
        "city": _CITIES,
        "origin": _ORIGINS,
        "brand": _BRANDS,
        "airline": _AIRLINES,
        "month": _MONTHS,
        "days": _DAYS,
        "sector": _SECTORS,
    }


def _pick_slots(template: str, rng: random.Random) -> dict[str, str]:
    slots: dict[str, str] = {}
    for name, values in _slot_values().items():
        if "{" + name + "}" in template:
            slots[name] = rng.choice(list(values))
    return slots


def _required_keywords(
    slot_names: Sequence[str], slots: dict[str, str]
) -> list[str]:
    keywords: list[str] = []
    for name in slot_names:
        if name == "brand_or_airline":
            value = slots.get("brand") or slots.get("airline")
            if value:
                keywords.append(value)
        elif name == "policy_keyword":
            keywords.append("标准")
        elif name in slots:
            keywords.append(slots[name])
    return [keyword for keyword in keywords if keyword]


def build_cases(count: int = 500, seed: int = 7) -> list[dict[str, Any]]:
    """确定性生成分层评测集（默认 500 条），覆盖全部 12 类场景。"""
    if count < 0:
        raise ValueError("count 不能为负数")
    rng = random.Random(seed)
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    index = 0
    while len(cases) < count:
        category, templates, agents, keyword_slots = _TEMPLATES[
            index % len(_TEMPLATES)
        ]
        template = templates[(index // len(_TEMPLATES)) % len(templates)]
        slots = _pick_slots(template, rng)
        query = template.format(**slots)
        index += 1
        if query in seen:
            # 重复查询加一个确定性的时间限定词，保证评测集不重复计分
            query = f"{query}（第 {index} 次确认）"
        if query in seen:
            continue
        seen.add(query)
        cases.append(
            {
                "id": f"case_{len(cases) + 1:04d}",
                "category": category,
                "query": query,
                "expected_intents": list(agents),
                "required_keywords": _required_keywords(
                    keyword_slots, slots
                ),
                "forbidden_keywords": [],
            }
        )
    return cases


def dump_cases(
    path: str | Path,
    cases: Sequence[dict[str, Any]],
    *,
    force: bool = False,
) -> None:
    """把评测用例写成 JSONL（一行一条；默认拒绝覆盖已有文件）。

    与其它导出命令（trips-export / session-export / kb-export）同一守卫：
    ``--out`` 指向用户记忆文件、旧备份或人工维护的用例集时不能被静默
    截断覆盖；写入走原子替换，中途失败不会留下半截文件。
    """
    from utils.atomic_write import write_text_atomic

    output = Path(path)
    if output.exists() and not force:
        raise FileExistsError(f"文件已存在：{output}（加 --force 覆盖）")
    text = "".join(
        json.dumps(case, ensure_ascii=False) + "\n" for case in cases
    )
    write_text_atomic(output, text, newline="\n")
