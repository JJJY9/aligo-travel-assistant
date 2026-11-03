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
    assert meta["agents"] == [{"name": "preference", "status": "success", "duration": 1.23}]
    assert meta["elapsed"] == 3.14
    assert meta["usage"] == _USAGE

    payload = attach_turn_meta(_RESULT, meta)
    assert "_meta" not in _RESULT, "attach 不应修改原对象"

    stored = json.dumps(payload, ensure_ascii=False)
    assert extract_turn_meta(stored) == meta
    assert "results" in json.loads(strip_turn_meta(stored))
    assert "_meta" not in json.loads(strip_turn_meta(stored))
    print("✓ 元数据构建 / 附加 / 提取 / 剥离")


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
    print("✓ 非 JSON / 空内容安全兜底")


if __name__ == "__main__":
    test_build_and_roundtrip()
    test_markdown_ignores_meta()
    test_invalid_content()
    print("\n全部通过")
