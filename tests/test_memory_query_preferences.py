"""
记忆查询智能体偏好格式化测试（无需 API Key）

覆盖 Issue #3：
1. 实际存储的偏好类型（hotel_brands / airlines / seat_preference / home_location 等）
   必须全部出现在格式化结果中（修复前被类型映射表全部过滤掉）；
2. 列表值以 ', ' 拼接，未知/自定义类型不再丢弃；
3. 完整 reply() 流程中偏好信息进入 prompt。

运行：python tests/test_memory_query_preferences.py
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
    path = os.path.join(project_root, ".claude", "skills", "memory-query", "script", "agent.py")
    spec = importlib.util.spec_from_file_location("skills.memory_query.agent", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.MemoryQueryAgent


class _StubLongTerm:
    def __init__(self, preferences):
        self._preferences = preferences

    def get_trip_history(self, limit=10):
        return [{"origin": "北京", "destination": "上海", "start_date": "2026-03-01",
                 "end_date": "2026-03-03", "purpose": "出差", "timestamp": "2026-03-01T09:00:00"}]

    def get_preference(self, pref_type=None):
        if pref_type is None:
            return dict(self._preferences)
        return self._preferences.get(pref_type)


class _StubMemoryManager:
    def __init__(self, preferences):
        self.long_term = _StubLongTerm(preferences)

    async def get_long_term_summary_async(self, max_messages=50):
        return ""


class _CaptureModel:
    def __init__(self):
        self.prompts = []

    async def __call__(self, messages):
        self.prompts.append(messages[-1]["content"])

        class _Resp:
            pass

        resp = _Resp()
        resp.text = "你在北京，喜欢住万豪和希尔顿。"
        return resp


REAL_PREFS = {
    "hotel_brands": "万豪酒店和希尔顿",
    "airlines": "国航和东航",
    "seat_preference": "靠窗",
    "home_location": "北京朝阳区",
    "custom_note": ["自定义类型", "也应展示"],
}


def test_format_preferences_shows_real_types():
    """Issue #3 回归：真实存储的偏好类型不能再次被过滤掉"""
    agent_class = _load_agent_class()
    agent = agent_class(name="MemoryQueryAgent", model=None, memory_manager=None)

    out = agent._format_preferences(REAL_PREFS)
    assert out != "（暂无偏好记录）", "真实偏好被全部过滤（Issue #3 复现）"
    for expected in ["酒店偏好: 万豪酒店和希尔顿", "航空公司偏好: 国航和东航",
                     "座位偏好: 靠窗", "常驻地: 北京朝阳区"]:
        assert expected in out, f"缺少: {expected}\n实际输出:\n{out}"
    print("✓ 真实偏好类型全部正确展示")


def test_format_preferences_list_and_custom():
    """列表值拼接、未知类型不丢弃"""
    agent_class = _load_agent_class()
    agent = agent_class(name="MemoryQueryAgent", model=None, memory_manager=None)

    out = agent._format_preferences(REAL_PREFS)
    assert "自定义类型, 也应展示" in out, "列表值应以 ', ' 拼接"
    assert "custom_note" not in out or "自定义类型" in out, "未知类型应展示而不是丢弃"
    print("✓ 列表值拼接、自定义类型保留")


def test_reply_prompt_contains_preferences():
    """完整流程：偏好信息进入 LLM prompt"""
    agent_class = _load_agent_class()
    model = _CaptureModel()
    memory = _StubMemoryManager(REAL_PREFS)
    agent = agent_class(name="MemoryQueryAgent", model=model, memory_manager=memory)

    payload = json.dumps({"context": {"rewritten_query": "我之前说过什么偏好？"}}, ensure_ascii=False)
    out = asyncio.run(agent.reply(Msg(name="Orchestrator", content=payload, role="user")))
    result = json.loads(out.content)

    assert result["status"] == "success"
    assert "万豪酒店和希尔顿" in model.prompts[0], "偏好未进入 prompt"
    print("✓ reply() 流程中偏好进入 prompt")


if __name__ == "__main__":
    test_format_preferences_shows_real_types()
    test_format_preferences_list_and_custom()
    test_reply_prompt_contains_preferences()
    print("\n全部通过")
