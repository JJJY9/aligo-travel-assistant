"""
结果格式化器测试（无需 API Key、不访问网络）

运行：python tests/test_result_formatter.py
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.result_formatter import result_to_markdown


def _wrap(results):
    return {"status": "completed", "results": results}


def test_itinerary():
    data = _wrap([
        {
            "agent_name": "itinerary_planning",
            "priority": 2,
            "status": "success",
            "data": {
                "itinerary": {
                    "title": "北京—杭州3天行程",
                    "duration": "3天",
                    "daily_plans": [
                        {
                            "day": 1,
                            "activities": [
                                {"time": "09:00", "activity": "出发", "description": "高铁", "transport": "地铁"}
                            ],
                            "meals": {"lunch": "简餐"},
                        }
                    ],
                    "notes": ["提前订票"],
                }
            },
        }
    ])
    md = result_to_markdown(data)
    assert "北京—杭州3天行程" in md
    assert "第 1 天" in md
    assert "09:00" in md and "高铁" in md
    assert "提前订票" in md
    print("✓ 行程渲染正常")


def test_preference():
    data = _wrap([
        {
            "agent_name": "preference",
            "priority": 1,
            "status": "success",
            "data": {"preferences": [{"type": "hotel_brands", "value": "汉庭", "action": "replace"}]},
        }
    ])
    md = result_to_markdown(data)
    assert "已更新偏好" in md
    assert "酒店偏好" in md and "汉庭" in md
    print("✓ 偏好更新渲染正常")


def test_rag_answer_with_sources():
    data = _wrap([
        {
            "agent_name": "rag_knowledge",
            "priority": 1,
            "status": "success",
            "data": {
                "answer": "一线城市住宿标准为 500 元/晚。",
                "retrieved_documents": [
                    {
                        "content": "一线城市住宿标准：500 元/晚，凭发票报销。",
                        "metadata": {"parent_doc": "01_travel_standards.txt"},
                    },
                    {
                        "content": "其他城市住宿标准：350 元/晚。",
                        "metadata": {"parent_doc": "01_travel_standards.txt"},
                    },
                ],
            },
        }
    ])
    md = result_to_markdown(data)
    assert "500 元/晚" in md
    assert "参考来源" in md and "01_travel_standards.txt" in md
    assert "参考片段" in md
    assert "凭发票报销" in md and "其他城市" in md
    assert "> " in md
    print("✓ RAG 回答与来源 / 参考片段渲染正常")


def test_information_query_sources():
    data = _wrap([
        {
            "agent_name": "information_query",
            "priority": 1,
            "status": "success",
            "data": {"results": {"summary": "杭州多云 18℃", "sources": [{"url": "https://open-meteo.com"}]}},
        }
    ])
    md = result_to_markdown(data)
    assert "杭州多云" in md
    assert "open-meteo.com" in md
    print("✓ 信息查询渲染正常")


def test_error_result():
    data = _wrap([
        {"agent_name": "memory_query", "priority": 1, "status": "error", "data": {"error": "服务不可用"}}
    ])
    md = result_to_markdown(data)
    assert "执行失败" in md and "服务不可用" in md
    print("✓ 错误渲染正常")


def test_no_agents():
    md = result_to_markdown({"status": "no_agents", "results": []})
    assert "已记录" in md
    print("✓ 无智能体调度时的提示正常")


if __name__ == "__main__":
    test_itinerary()
    test_preference()
    test_rag_answer_with_sources()
    test_information_query_sources()
    test_error_result()
    test_no_agents()
    print("\n全部通过")
