"""智能体名称归一测试（无需 API Key、不访问网络）。

运行：python tests/test_agent_names.py
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.agent_names import canonical_agent_name


def test_canonical_agent_name_cases():
    # 技能目录名任意大小写
    assert canonical_agent_name("Plan-Trip") == "itinerary_planning"
    assert canonical_agent_name("QUERY-INFO") == "information_query"
    # 遗留标准名的大小写变体（去重 / 依赖排序 / 记忆写入依赖它）
    assert canonical_agent_name("Itinerary_Planning") == "itinerary_planning"
    assert canonical_agent_name("RAG_Knowledge") == "rag_knowledge"
    assert canonical_agent_name("Event_Collection") == "event_collection"
    # preference 目录名与标准名同形：标准名变体同样要归一（否则偏好不落库）
    assert canonical_agent_name("Preference") == "preference"
    assert canonical_agent_name("PREFERENCE") == "preference"
    assert canonical_agent_name(" preference ") == "preference"
    # 已是标准名保持原样，首尾空白剥离
    assert canonical_agent_name(" information_query ") == "information_query"
    # 未知名称原样保留
    assert canonical_agent_name("Greeting_Agent") == "Greeting_Agent"
    assert canonical_agent_name("") == ""
    assert canonical_agent_name(None) == ""
    print("✓ 智能体名称归一（目录名 / 遗留名变体 / 未知名称）")


if __name__ == "__main__":
    test_canonical_agent_name_cases()
    print("\n全部通过")
