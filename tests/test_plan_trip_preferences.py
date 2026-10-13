"""
行程规划智能体偏好 prompt 格式测试（无需 API Key）

覆盖 Issue #2：字符串偏好不能被 ', '.join 拆成单字。

运行：python tests/test_plan_trip_preferences.py
"""
import sys
import os
import asyncio
import json
import importlib.util

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from agentscope.message import Msg


def _load_agent_class():
    path = os.path.join(project_root, ".claude", "skills", "plan-trip", "script", "agent.py")
    spec = importlib.util.spec_from_file_location("skills.plan_trip.agent", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ItineraryPlanningAgent


class _CaptureModel:
    """记录 prompt 并返回固定行程 JSON 的 mock 模型"""

    def __init__(self):
        self.prompts = []

    async def __call__(self, messages):
        self.prompts.append(messages[-1]["content"])

        class _Resp:
            pass

        resp = _Resp()
        resp.text = '{"title": "测试行程", "duration": "3天", "daily_plans": []}'
        return resp


def test_string_preferences_in_prompt():
    agent_class = _load_agent_class()
    model = _CaptureModel()
    agent = agent_class(name="ItineraryPlanningAgent", model=model)

    payload = json.dumps({
        "context": {
            "rewritten_query": "从北京去上海出差",
            "user_preferences": {
                "hotel_brands": "万豪酒店和希尔顿",
                "airlines": "国航和东航",
                "seat_preference": "靠窗",
            },
        },
        "previous_results": [],
    }, ensure_ascii=False)

    asyncio.run(agent.reply(Msg(name="Orchestrator", content=payload, role="user")))

    prompt = model.prompts[0]
    assert "万豪酒店和希尔顿" in prompt, "字符串偏好应保持完整"
    assert "国航和东航" in prompt, "字符串偏好应保持完整"
    assert "万, 豪" not in prompt and "国, 航" not in prompt, "字符串偏好被拆成了单字（Issue #2 复现）"
    print("✓ 行程规划 prompt 中字符串偏好保持完整")


if __name__ == "__main__":
    test_string_preferences_in_prompt()
    print("\n全部通过")
