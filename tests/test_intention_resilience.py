"""
意图识别智能体异常传播测试（无需 API Key）

覆盖 Issue #1：
1. 可重试错误（连接失败等）必须向上抛出，供 cli.py 的 retry_with_backoff 与熔断器处理；
2. 不可重试错误（模型返回无法解析的文本）保持降级为默认结果。

运行：python tests/test_intention_resilience.py
"""
import asyncio
import json
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from agentscope.message import Msg

from agents.intention_agent import IntentionAgent


class _ConnectionBoomModel:
    """始终抛连接错误的模型（模拟 LLM 服务不可达）"""

    async def __call__(self, messages):
        raise ConnectionError("connection refused")


class _UnparsableModel:
    """返回无法解析文本的模型（模拟模型输出格式错误）"""

    async def __call__(self, messages):
        return "这不是 JSON，无法解析"


class _TrailingProseModel:
    """合法 JSON 后带说明文字（其中含花括号）的输出"""

    async def __call__(self, messages):
        return (
            '{"reasoning": "用户要出行", "intents": [{"type": "plan_trip", "confidence": 0.9}], '
            '"key_entities": {}, "agent_schedule": [{"agent_name": "itinerary_planning", "priority": 1}]}\n'
            "提示：如 {日期} 未提供可默认明天。"
        )


def _user_msg():
    return Msg(name="user", content="帮我规划去北京的行程", role="user")


def test_retriable_error_is_raised():
    """可重试错误不应被吞掉，否则外层重试与熔断永远不会触发"""
    agent = IntentionAgent(name="IntentionAgent", model=_ConnectionBoomModel())
    try:
        asyncio.run(agent.reply([_user_msg()]))
    except ConnectionError:
        print("✓ 可重试错误已向上抛出（重试/熔断可用）")
        return
    raise AssertionError("可重试错误被吞掉：reply() 应向上抛出 ConnectionError")


def test_unparsable_output_falls_back():
    """模型输出无法解析属于不可重试错误，应保持降级行为"""
    agent = IntentionAgent(name="IntentionAgent", model=_UnparsableModel())
    out = asyncio.run(agent.reply([_user_msg()]))
    data = json.loads(out.content)
    assert "意图识别出错" in data.get("reasoning", ""), "应降级为默认结果"
    assert data.get("agent_schedule"), "降级结果应包含默认调度"
    print("✓ 不可重试错误保持降级为默认结果")


def test_json_with_trailing_prose_braces_parses():
    """结尾说明里的花括号不能影响 JSON 解析（否则规划请求被降级为信息查询）。"""
    agent = IntentionAgent(name="IntentionAgent", model=_TrailingProseModel())
    out = asyncio.run(agent.reply([_user_msg()]))
    data = json.loads(out.content)
    assert data["intents"][0]["type"] == "plan_trip"
    assert data["agent_schedule"][0]["agent_name"] == "itinerary_planning"
    print("✓ JSON 后带花括号说明文字仍能解析")


if __name__ == "__main__":
    test_retriable_error_is_raised()
    test_unparsable_output_falls_back()
    test_json_with_trailing_prose_braces_parses()
    print("\n全部通过")
