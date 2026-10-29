"""
测试事项收集智能体（无需 API Key，用确定性假模型驱动）

覆盖：
1. 偏好值为字符串时 prompt 保持完整（Issue #2 回归：不被 ', '.join 拆成单字）
2. 偏好值为列表时正常拼接
3. 正常提取流程与结果解析

运行：python tests/test_event_collection_agent.py
"""
import asyncio
import importlib.util
import json
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from agentscope.message import Msg


def _load_agent_class():
    """与 LazyAgentRegistry 一致，从 .claude/skills 动态加载"""
    path = os.path.join(project_root, ".claude", "skills", "event-collection", "script", "agent.py")
    spec = importlib.util.spec_from_file_location("skills.event_collection.agent", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.EventCollectionAgent


class _CaptureModel:
    """记录 prompt 并返回固定 JSON 的确定性假模型（测试替身）"""

    def __init__(self, response_text: str):
        self.response_text = response_text
        self.prompts: list[str] = []

    async def __call__(self, messages):
        self.prompts.append(messages[-1]["content"])

        class _Resp:
            pass

        resp = _Resp()
        resp.text = self.response_text
        return resp


def _reply(agent):
    payload = json.dumps({
        "context": {
            "rewritten_query": "我要从北京去上海出差3天",
            "user_preferences": {
                "home_location": "北京朝阳区",
                "hotel_brands": "万豪酒店和希尔顿",
                "airlines": ["国航", "东航"],
            },
        }
    }, ensure_ascii=False)
    return asyncio.run(agent.reply(Msg(name="Orchestrator", content=payload, role="user")))


def test_string_preference_not_split():
    """Issue #2 回归：字符串偏好不能被拆成单字"""
    agent_class = _load_agent_class()
    model = _CaptureModel('{"origin": "北京", "destination": "上海", "duration_days": 3, "missing_info": []}')
    agent = agent_class(name="EventCollectionAgent", model=model)
    _reply(agent)

    prompt = model.prompts[0]
    assert "万豪酒店和希尔顿" in prompt, "字符串偏好应保持完整"
    assert "万, 豪" not in prompt, "字符串偏好被拆成了单字（Issue #2 复现）"
    print("✓ 字符串偏好保持完整")


def test_list_preference_joined():
    """列表偏好应拼接为 国航, 东航"""
    agent_class = _load_agent_class()
    model = _CaptureModel('{"origin": "北京", "destination": "上海", "missing_info": []}')
    agent = agent_class(name="EventCollectionAgent", model=model)
    _reply(agent)

    prompt = model.prompts[0]
    assert "国航, 东航" in prompt, "列表偏好应以 ', ' 拼接"
    print("✓ 列表偏好正常拼接")


def test_result_parsed():
    """正常提取流程：假模型返回的 JSON 能被解析并返回"""

    # 说明：这里用确定性假模型替代真实 LLM（无需 API Key 与网络）。
    # 假模型只负责返回固定 JSON，被测的是真实的 EventCollectionAgent.reply
    # 与 prompt 组装逻辑，因此断言仍落在生产代码上。
    agent_class = _load_agent_class()
    model = _CaptureModel('{"origin": "北京", "destination": "上海", "duration_days": 3, "missing_info": []}')
    agent = agent_class(name="EventCollectionAgent", model=model)
    result = json.loads(_reply(agent).content)
    assert result["origin"] == "北京"
    assert result["destination"] == "上海"
    assert result["duration_days"] == 3
    print("✓ 提取结果解析正常")


if __name__ == "__main__":
    test_string_preference_not_split()
    test_list_preference_joined()
    test_result_parsed()
    print("\n全部通过")
