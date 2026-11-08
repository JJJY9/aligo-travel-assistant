"""
执行元数据（Turn Metadata）工具测试（无需 API Key）

运行：python tests/test_turn_meta.py
"""
import json
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.result_formatter import result_to_markdown
from utils.turn_meta import (
    attach_turn_meta,
    build_turn_meta,
    extract_turn_meta,
    strip_turn_meta,
)

_INTENTION = {
    "intents": [{"type": "preference", "confidence": 0.9}],
    "reasoning": "用户表达了酒店偏好",
    "agent_schedule": [
        {
            "agent_name": "preference",
            "priority": 1,
            "reason": "记录酒店品牌偏好",
        }
    ],
}

_RESULT = {
    "status": "completed",
    "results": [
        {
            "agent_name": "preference",
            "priority": 1,
            "status": "success",
            "duration": 1.23,
            "data": {
                "preferences": [
                    {"type": "hotel_brands", "value": "汉庭", "action": "replace"}
                ]
            },
        }
    ],
}

_USAGE = {
    "calls": 2,
    "input_tokens": 60,
    "output_tokens": 40,
    "total_tokens": 100,
    "time": 1.25,
}


def test_build_and_roundtrip():
    meta = build_turn_meta(_INTENTION, _RESULT, usage=_USAGE, elapsed=3.14159)
    assert meta["schedule"][0]["agent_name"] == "preference"
    assert meta["agents"] == [
        {"name": "preference", "status": "success", "duration": 1.23}
    ]
    assert meta["elapsed"] == 3.14
    assert meta["usage"] == _USAGE

    payload = attach_turn_meta(_RESULT, meta)
    assert "_meta" not in _RESULT, "attach 不应修改原对象"

    stored = json.dumps(payload, ensure_ascii=False)
    assert extract_turn_meta(stored) == meta
    assert "results" in json.loads(strip_turn_meta(stored))
    assert "_meta" not in json.loads(strip_turn_meta(stored))
    print("✓ 元数据构建 / 附加 / 提取 / 剥离")


def test_schedule_names_are_canonicalized():
    """技能目录名在元数据里归一为标准名：执行路线匹配不到 results 时会误显示成功。"""
    intention = {
        "agent_schedule": [
            {"agent_name": "plan-trip", "priority": 2, "reason": "规划"},
            {"agent_name": " ask-question ", "priority": 1, "reason": "查询"},
        ]
    }
    meta = build_turn_meta(intention, {"results": []})
    assert [item["agent_name"] for item in meta["schedule"]] == [
        "itinerary_planning",
        "rag_knowledge",
    ], "技能目录名（含空白）归一为标准名"
    print("✓ 执行路线的调度名按标准名归一")


def test_markdown_ignores_meta():
    payload = attach_turn_meta(_RESULT, build_turn_meta(_INTENTION, _RESULT))
    markdown = result_to_markdown(payload)
    assert "汉庭" in markdown
    print("✓ 渲染 Markdown 时忽略元数据")


def test_invalid_content():
    assert extract_turn_meta("不是 JSON") is None
    assert extract_turn_meta("[]") is None
    assert extract_turn_meta("") is None
    assert strip_turn_meta("普通文本") == "普通文本"
    # 深度嵌套的旧数据：json.loads 可能抛 RecursionError，必须原样返回
    deep = "[" * 3000 + "]" * 3000
    assert strip_turn_meta(deep) == deep
    assert extract_turn_meta(deep) is None
    print("✓ 非 JSON / 空内容 / 深嵌套内容安全兜底")


def test_huge_integer_literal_tolerated():
    """超 4300 位整数字面量：json.loads 抛普通 ValueError，不能逃逸为 500。"""
    from utils.stored_message import render_stored_message

    content = '{"n": ' + "1" * 5000 + "}"
    assert extract_turn_meta(content) is None
    assert strip_turn_meta(content) == content
    message = {"role": "assistant", "content": content}
    assert render_stored_message(message) == content
    print("✓ 超长整数字面量按旧文本兜底")


def test_build_turn_meta_filters_unexecuted_schedule():
    """执行路线只保留真正执行过的智能体（去重 / 丢弃 / 幽灵项不出现）。"""
    meta = build_turn_meta(
        {
            "agent_schedule": [
                {"agent_name": "a", "priority": 1},
                {"agent_name": "plan-trip", "priority": 2},
                {"agent_name": "plan-trip", "priority": 2},
                {"agent_name": "", "priority": 3},
                {"agent_name": "ghost", "priority": 4},
            ]
        },
        {"results": [{"agent_name": "a", "status": "success"}]},
    )
    names = [item["agent_name"] for item in meta["schedule"]]
    assert names == ["a"], meta["schedule"]
    print("✓ 执行路线过滤未执行 / 重复的调度项")


def test_build_turn_meta_tolerates_non_list_sources():
    """results / agent_schedule 非列表、条目非对象、重复调度项都不抛异常。"""
    meta = build_turn_meta(
        {
            "agent_schedule": [
                "坏条目",
                {"agent_name": ""},
                {"agent_name": "ask-question", "priority": 1},
                {"agent_name": " ask-question "},
            ]
        },
        {
            "results": [
                "坏条目",
                {"agent_name": "rag_knowledge", "status": "success"},
            ]
        },
    )
    assert [item["agent_name"] for item in meta["schedule"]] == ["rag_knowledge"], (
        "重复调度项归一后只保留一条"
    )
    assert meta["agents"] == [
        {"name": "rag_knowledge", "status": "success", "duration": None}
    ]

    meta = build_turn_meta({"agent_schedule": {"a": 1}}, {"results": 5})
    assert meta["schedule"] == [] and meta["agents"] == [], (
        "非列表来源按空处理（真值非可迭代此前会 TypeError）"
    )
    print("✓ 非列表 / 非对象来源与重复调度项容错")


def test_strip_turn_meta_non_mapping_returns_original():
    """非对象 JSON / 不含 _meta 的对象：strip 原样返回（旧数据兼容）。"""
    assert strip_turn_meta("[1, 2]") == "[1, 2]"
    assert strip_turn_meta('{"reply": "x"}') == '{"reply": "x"}'
    print("✓ strip_turn_meta 对无元数据内容原样返回")


if __name__ == "__main__":
    test_build_and_roundtrip()
    test_markdown_ignores_meta()
    test_invalid_content()
    test_schedule_names_are_canonicalized()
    test_huge_integer_literal_tolerated()
    test_build_turn_meta_filters_unexecuted_schedule()
    test_build_turn_meta_tolerates_non_list_sources()
    test_strip_turn_meta_non_mapping_returns_original()
    print("\n全部通过")
