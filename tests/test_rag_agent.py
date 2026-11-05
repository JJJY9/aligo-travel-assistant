#!/usr/bin/env python
"""
测试RAG知识库智能体

使用方法：
  # 先初始化知识库
  python scripts/init_knowledge_base.py

  # 然后运行测试
  python tests/test_rag_agent.py
"""
import asyncio
import json
import os
import sys
import time

import pytest

# 添加项目根目录到 Python 路径
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from agentscope.message import Msg

from agents.rag_knowledge_agent import RAGKnowledgeAgent
from config import LLM_CONFIG
from config_agentscope import create_chat_model, init_agentscope

requires_llm_key = pytest.mark.skipif(
    not LLM_CONFIG["api_key"],
    reason="需要真实模型密钥（LLM_API_KEY 或 config_local.API_KEY）；未配置时如实跳过",
)


@requires_llm_key
async def test_rag_agent():
    """测试RAG知识库智能体"""

    print("="*70)
    print("RAG知识库智能体测试")
    print("="*70)
    print()

    # 初始化
    print("初始化 AgentScope...")
    init_agentscope()
    print("✓ AgentScope initialized")
    print()

    print("创建模型...")
    model = create_chat_model()
    print("✓ 模型创建成功")
    print()

    print("创建RAG Knowledge Agent...")
    agent = RAGKnowledgeAgent(
        name="RAGKnowledgeAgent",
        model=model,
        collection_name="business_travel_knowledge",
        top_k=3,
    )

    if not agent.initialized:
        print("❌ RAG Agent未正确初始化")
        print("   请先运行: python scripts/init_knowledge_base.py")
        return

    print("✓ RAG Agent创建成功")
    stats = agent.get_stats()
    if stats["status"] == "success":
        print(f"   - 知识库文档数: {stats['total_documents']}")
        print(f"   - 存储路径: {stats['knowledge_base_path']}")
    print()

    # 测试用例 - 涵盖8个文档类别
    test_cases = [
        {
            "name": "住宿标准查询",
            "query": "北京出差的住宿标准是多少？",
            "expected": ["500", "一线"]
        },
        {
            "name": "报销规定查询",
            "query": "差旅费用应该在什么时候报销？",
            "expected": ["30", "天"]
        },
        {
            "name": "预订建议查询",
            "query": "机票应该提前多久预订比较好？",
            "expected": ["7", "14"]
        },
        {
            "name": "FAQ查询",
            "query": "出差可以携带家属吗？",
            "expected": ["不可以"]
        },
        {
            "name": "紧急情况查询",
            "query": "航班延误了应该怎么办？",
            "expected": ["改签", "凭证"]
        },
        {
            "name": "城市指南查询",
            "query": "北京有哪些机场？",
            "expected": ["首都", "大兴"]
        },
        {
            "name": "平台功能查询",
            "query": "阿里商旅平台有哪些功能？",
            "expected": ["申请", "预订"]
        },
        {
            "name": "环保建议查询",
            "query": "出差怎么做到环保？",
            "expected": ["高铁", "公共交通"]
        },
    ]

    passed = 0
    failed = 0
    total_time = 0

    for i, test_case in enumerate(test_cases, 1):
        print("\n" + "="*70)
        print(f"测试 {i}/{len(test_cases)}: {test_case['name']}")
        print("="*70)
        print(f"用户查询: {test_case['query']}")
        print()

        start_time = time.time()
        try:
            # 创建消息
            user_msg = Msg(name="User", content=test_case['query'], role="user")

            # 调用RAG Agent
            result = await agent(user_msg)

            # 解析结果
            result_data = json.loads(result.content)

            elapsed = time.time() - start_time
            total_time += elapsed

            # 显示结果
            status = result_data.get('status', 'unknown')
            print(f"【状态】{status}")
            print()

            if status == 'success':
                answer = result_data.get('answer', '')
                print("【AI回答】")
                print("-" * 60)
                print(answer)
                print("-" * 60)
                print()

                # 验证关键词
                found_keywords = [kw for kw in test_case.get('expected', []) if kw in answer]
                if found_keywords:
                    print(f"✓ 关键词验证通过: {', '.join(found_keywords)}")
                    success = True
                else:
                    print(f"⚠️  未找到预期关键词: {', '.join(test_case.get('expected', []))}")
                    success = False

                # 显示检索到的文档
                retrieved_docs = result_data.get('retrieved_documents', [])
                if retrieved_docs:
                    print(f"✓ 检索到 {len(retrieved_docs)} 篇参考文档")
                    for j, doc in enumerate(retrieved_docs, 1):
                        metadata = doc.get('metadata', {})
                        print(f"  [{j}] {metadata.get('title', 'Unknown')} ({metadata.get('category', 'N/A')})")

                print(f"\n⏱️  响应时间: {elapsed:.1f}秒")

                if success:
                    print("✅ 测试通过")
                    passed += 1
                else:
                    print("⚠️  测试未完全通过")
                    failed += 1

            else:
                error_msg = result_data.get('message', result_data.get('answer', ''))
                print(f"❌ 查询失败: {error_msg}")
                failed += 1

        except Exception as e:
            print(f"❌ 测试失败: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

        print()

    # 打印统计
    print("\n" + "="*70)
    print("测试结果汇总")
    print("="*70)
    print(f"总测试数: {len(test_cases)}")
    print(f"✓ 通过: {passed} ({passed/len(test_cases)*100:.0f}%)")
    print(f"✗ 失败: {failed} ({failed/len(test_cases)*100:.0f}%)")
    print(f"平均响应时间: {total_time/len(test_cases):.1f}秒")
    print(f"总耗时: {total_time:.1f}秒")
    print("="*70)


def test_rag_query_extraction_falls_back_to_raw_query():
    """rewritten_query 缺失 / 为空 / null 时用原始提问检索。"""
    import importlib.util
    from pathlib import Path

    path = Path(project_root) / ".claude/skills/ask-question/script/agent.py"
    spec = importlib.util.spec_from_file_location("_test_ask_question_agent", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    extract = getattr(module, "extract_user_query")

    assert (
        extract(
            json.dumps({"context": {"rewritten_query": "", "user_query": "住宿标准是多少"}})
        )
        == "住宿标准是多少"
    )
    assert (
        extract(
            json.dumps({"context": {"rewritten_query": None, "user_query": "出差报销流程"}})
        )
        == "出差报销流程"
    )
    assert (
        extract(
            json.dumps(
                {"context": {"rewritten_query": "改写后问题", "user_query": "原始问题"}}
            )
        )
        == "改写后问题"
    )
    assert extract(json.dumps({"rewritten_query": "顶层改写"})) == "顶层改写"
    assert extract("直接提问") == "直接提问"
    assert extract("{不是JSON") == "{不是JSON"
    print("✓ RAG 使用原始提问兜底（空 / null 改写结果）")


def test_strict_search_error_has_top_level_message():
    """strict 检索失败的错误载荷要带顶层 error/message，编排聚合才能展示原因。"""
    agent = object.__new__(RAGKnowledgeAgent)
    agent.name = "RAGKnowledgeAgent"
    agent.initialized = True

    def _boom(*args, **kwargs):
        raise RuntimeError("milvus locked")

    agent.search_knowledge = _boom
    # 绕过 AgentScope 需要完整实例 hook 的 reply 包装器，直接调用原始实现
    reply_impl = RAGKnowledgeAgent.reply.__wrapped__
    message = asyncio.run(
        reply_impl(agent, Msg(name="user", content="住宿标准是多少", role="user"))
    )
    payload = json.loads(message.content)
    assert payload["status"] == "error"
    assert payload.get("error"), "缺少顶层 error，编排聚合会丢弃原因"
    assert "milvus locked" in str(payload.get("message") or "")
    print("✓ strict 检索失败带顶层 error/message")


if __name__ == "__main__":
    asyncio.run(test_rag_agent())
    print("\n测试完成！")
