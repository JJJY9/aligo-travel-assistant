"""
偏好智能体输入选择测试（无需 API Key、不访问网络）

偏好抽取应优先使用用户本轮原话（context.user_query），避免把上下文 /
助手建议中的内容（例如行程里提到的舱位、预算）误记为用户偏好；
缺少 user_query 时回退到 rewritten_query。

运行：python tests/test_preference_context.py
"""
import asyncio
import json
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from agentscope.message import Msg

from agents._skill_compat import load_skill_agent

PreferenceAgent = load_skill_agent("preference", "PreferenceAgent")


class _FakeModel:
    def __init__(self):
        self.prompts = []

    async def __call__(self, messages, **kwargs):
        self.prompts.append(messages[0]["content"])
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {"has_preferences": False, "preferences": []},
                        ensure_ascii=False,
                    ),
                }
            ]
        }


class _FakeLongTerm:
    def get_preference(self):
        return {}


class _FakeMemory:
    def __init__(self):
        self.long_term = _FakeLongTerm()


def _payload(context: dict) -> Msg:
    return Msg(
        name="user",
        content=json.dumps({"context": context}, ensure_ascii=False),
        role="user",
    )


def test_prefers_user_query_over_rewritten():
    model = _FakeModel()
    agent = PreferenceAgent(model=model, memory_manager=_FakeMemory())
    asyncio.run(
        agent.reply(
            _payload(
                {
                    "user_query": "我常坐东航",
                    "rewritten_query": "用户想查询航班信息",
                }
            )
        )
    )
    assert model.prompts, "应调用一次模型"
    prompt = model.prompts[0]
    assert "我常坐东航" in prompt
    assert "用户想查询航班信息" not in prompt
    print("✓ 偏好抽取使用用户本轮原话")


def test_falls_back_to_rewritten_query():
    model = _FakeModel()
    agent = PreferenceAgent(model=model, memory_manager=_FakeMemory())
    asyncio.run(agent.reply(_payload({"rewritten_query": "我常坐东航"})))
    assert "我常坐东航" in model.prompts[0]
    print("✓ 缺少 user_query 时回退 rewritten_query")


if __name__ == "__main__":
    test_prefers_user_query_over_rewritten()
    test_falls_back_to_rewritten_query()
    print("\n全部通过")
