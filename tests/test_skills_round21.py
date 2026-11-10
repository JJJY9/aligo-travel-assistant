"""第二十一轮技能层修复测试（无需网络）。

覆盖：
1. query-info 城市提取不再把「我要 / 最近」等片段当城市；
2. plan-trip 前言里的小数组（[1] / [ ]）不再被误判为顶层数组；
3. plan-trip 模型返回裸行程对象时补齐 itinerary 包装；
4. milvus 批量插入后 id 水位越过整批（删除整批后不复用旧 id）；
5. 编排聚合保留技能失败原因（不再退化成「未知错误」）；
6. RAG 检索路径先检查知识库是否存在、再加载嵌入模型。

运行：python tests/test_skills_round21.py
"""
import asyncio
import importlib.util
import json
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from agentscope.message import Msg


def _load_skill(rel_path: str, name: str):
    path = os.path.join(project_root, ".claude", "skills", rel_path)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"无法加载技能脚本：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_weather_city_extraction_rejects_pronoun_fragments():
    mod = _load_skill(os.path.join("query-info", "script", "agent.py"), "skill_qinfo_r21")
    agent = mod.InformationQueryAgent(model=None)
    assert agent._extract_city_from_query("我明天要出差，天气怎么样") == ""
    assert agent._extract_city_from_query("最近天气怎么样") == ""
    assert agent._extract_city_from_query("杭州明天天气怎么样") == "杭州"
    assert agent._extract_city_from_query("北京到上海的天气") == "上海"
    print("✓ 城市提取拒绝「我要 / 最近」等伪城市")


def test_starts_with_json_array_ignores_preamble_arrays():
    mod = _load_skill(os.path.join("plan-trip", "script", "agent.py"), "skill_plan_r21")
    assert mod.starts_with_json_array('[{"title": "x"}]') is True
    assert (
        mod.starts_with_json_array('根据[1]的说明：\n{"itinerary": {"title": "x"}}')
        is False
    )
    assert (
        mod.starts_with_json_array('- [ ] 待办\n{"itinerary": {"title": "x"}}')
        is False
    )
    assert mod.starts_with_json_array('[{"a": 1}] 以上是行程') is True
    print("✓ 前言小数组不再被误判为顶层数组")


def test_plan_trip_wraps_bare_itinerary_object():
    mod = _load_skill(os.path.join("plan-trip", "script", "agent.py"), "skill_plan_r21b")
    payload = {
        "title": "北京三日游",
        "duration": "3天",
        "daily_plans": [{"day": 1, "activities": []}],
    }

    class _Model:
        async def __call__(self, messages, **kwargs):
            return {
                "content": [
                    {"type": "text", "text": json.dumps(payload, ensure_ascii=False)}
                ]
            }

    agent = mod.ItineraryPlanningAgent(model=_Model())
    message = asyncio.run(
        agent.reply(Msg(name="u", content="帮我规划北京三日游", role="user"))
    )
    data = json.loads(message.content)
    assert data["itinerary"]["title"] == "北京三日游", data
    print("✓ 裸行程对象自动补齐 itinerary 包装")


def test_batch_insert_advances_entity_watermark():
    from utils import milvus_client as client_module

    class _Client:
        def load_collection(self, name):
            pass

        def query(self, **kwargs):
            return []

    client = _Client()
    first = client_module.next_entity_id(client, "asset-round21")
    client_module.record_used_entity_id(client, "asset-round21", first + 2)
    assert client_module.next_entity_id(client, "asset-round21") == first + 3
    print("✓ 批量插入后 id 水位越过整批")


def test_aggregate_keeps_skill_error_message():
    from agents.orchestration_agent import OrchestrationAgent
    from utils.result_formatter import result_to_markdown

    agent = OrchestrationAgent(agent_registry={})
    aggregated = agent._aggregate_results(
        [
            {
                "agent_name": "information_query",
                "priority": 1,
                "result": {
                    "status": "error",
                    "data": {"query_success": False},
                    "message": "搜索库未安装",
                },
            }
        ],
        {"intents": [], "key_entities": {}},
    )
    assert aggregated["results"][0]["message"] == "搜索库未安装"
    rendered = result_to_markdown(aggregated)
    assert "搜索库未安装" in rendered
    assert "未知错误" not in rendered
    print("✓ 聚合结果保留技能失败原因")


def test_rag_agent_checks_db_before_loading_embeddings(tmp_path, monkeypatch):
    mod = _load_skill(os.path.join("ask-question", "script", "agent.py"), "skill_ask_r21")
    if not mod.DEPENDENCIES_AVAILABLE:
        print("○ 跳过：RAG 依赖未安装")
        return
    calls = {"embeddings": 0}

    def fake_get_embedding_model(model_id):
        calls["embeddings"] += 1
        raise AssertionError("未初始化知识库不应加载嵌入模型")

    monkeypatch.setattr(mod, "get_embedding_model", fake_get_embedding_model)
    agent = mod.RAGKnowledgeAgent(
        knowledge_base_path=str(tmp_path),
        embedding_model="BAAI/bge-small-zh-v1.5",
    )
    assert agent.initialized is False
    assert "尚未初始化" in (agent.init_error or "")
    assert calls["embeddings"] == 0
    print("✓ 知识库未初始化时不加载嵌入模型")


if __name__ == "__main__":
    _mp = None
    test_weather_city_extraction_rejects_pronoun_fragments()
    test_starts_with_json_array_ignores_preamble_arrays()
    test_plan_trip_wraps_bare_itinerary_object()
    test_batch_insert_advances_entity_watermark()
    test_aggregate_keeps_skill_error_message()
    import tempfile
    import pytest

    _mp = pytest.MonkeyPatch()
    try:
        test_rag_agent_checks_db_before_loading_embeddings(
            __import__("pathlib").Path(tempfile.mkdtemp()), _mp
        )
    finally:
        _mp.undo()
    print("\n全部通过")
