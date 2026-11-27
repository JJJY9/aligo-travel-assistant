"""Personal Agent 分层评测用例生成（v2.0）。

按 18 类场景模板 + 槽位变体确定性生成评测集（默认 500 条）：
问候、行程规划、事项收集、偏好写入、偏好查询、记忆查询、政策 RAG、
天气、网页搜索、多意图、缺失信息澄清、无关问题，以及 v2 六个扩展
技能（预算 / 行李 / 行前清单 / 市内交通 / 安全提示 / 差旅贴士）。

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
        # 无关问题按「不调度任何智能体」计分（与问候同口径，intention_agent
        # 允许纯闲聊返回空 agent_schedule）。模板必须是明确的闲聊 / 致谢类：
        # 「今天股市怎么样」这类站外信息查询按提示词应调度 information_query，
        # 混进来会让空集合期望必败。也不要期望 "other"：管线没有这个可调度
        # 智能体，未知调度名会被过滤并按默认 information_query 兜底。
        "other",
        ["给我讲个笑话", "你会写代码吗", "今天心情不错，谢谢你"],
        [],
        [],
    ),
    (
        "trip_budget",
        ["帮我算算去{city}玩{days}天的预算", "去{city}出差{days}天大概要花多少钱"],
        ["trip-budget"],
        ["city"],
    ),
    (
        "packing_list",
        ["我{days}天后去{city}，要带什么行李", "去{city}出差需要带些什么"],
        ["packing-list"],
        ["city"],
    ),
    (
        "travel_checklist",
        ["出发去{city}前要做哪些准备", "帮我列个去{city}的行前检查清单"],
        ["travel-checklist"],
        ["city"],
    ),
    (
        "local_transport",
        ["到了{city}市内怎么去景点", "{city}市内交通怎么安排"],
        ["local-transport"],
        ["city"],
    ),
    (
        "safety_tips",
        ["去{city}出差要注意哪些安全事项", "{city}晚上出门安全吗"],
        ["safety-tips"],
        ["city"],
    ),
    (
        "trip_notes",
        ["去{city}出差有什么实用贴士", "第一次去{city}有什么注意事项"],
        ["trip-notes"],
        ["city"],
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
    slot_names: Sequence[str], slots: dict[str, str], template: str = ""
) -> list[str]:
    keywords: list[str] = []
    for name in slot_names:
        if name == "brand_or_airline":
            value = slots.get("brand") or slots.get("airline")
            if value:
                keywords.append(value)
        elif name == "policy_keyword":
            # 两个 policy_rag 模板问的措辞不同：住宿类问「标准」，报销类
            # 问「上限」。统一要求「标准」会把正确回答误报为缺关键词
            keywords.append("上限" if "上限" in template else "标准")
        elif name in slots:
            keywords.append(slots[name])
    return [keyword for keyword in keywords if keyword]


def build_cases(count: int = 500, seed: int = 7) -> list[dict[str, Any]]:
    """确定性生成分层评测集（默认 500 条），覆盖全部 18 类场景。"""
    if count < 1:
        # 0 条不是有效评测集：CLI 已按 1..5000 收窄，程序化调用同样
        # 拒绝（否则「已生成 0 条」会把现有 JSONL 截断成空文件）
        raise ValueError("count 必须至少为 1")
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
    截断覆盖；写入走原子替换，中途失败不会留下半截文件。覆盖判定用
    ``O_CREAT|O_EXCL`` 占名而不是 ``exists()`` 预检：并发两次导出都可能
    在文件尚不存在时通过检查，后者会静默覆盖先写者的完整用例集。
    """
    from utils.atomic_write import write_text_atomic
    from utils.exporter import claim_export_path

    output = Path(path)
    if output.is_dir():
        raise IsADirectoryError(f"目标是一个目录：{output}（--out 需要文件路径）")
    if output.parent.exists() and not output.parent.is_dir():
        raise NotADirectoryError(f"父路径不是目录：{output.parent}")
    text = "".join(
        json.dumps(case, ensure_ascii=False) + "\n" for case in cases
    )
    if force:
        write_text_atomic(output, text, newline="\n")
        return
    if not claim_export_path(output):
        raise FileExistsError(f"文件已存在：{output}（加 --force 覆盖）")
    try:
        stat = output.stat()
    except OSError:
        stat = None
    identity = (stat.st_dev, stat.st_ino, stat.st_size) if stat else None
    try:
        write_text_atomic(output, text, newline="\n")
    except BaseException:
        # 只清理仍是本次占下的空占位：另一进程 --force 已用完整用例集
        # 替换时不能误删他人的文件
        try:
            current = output.stat()
            if identity is None or (
                current.st_dev, current.st_ino, current.st_size
            ) == identity:
                output.unlink()
        except OSError:
            pass
        raise
