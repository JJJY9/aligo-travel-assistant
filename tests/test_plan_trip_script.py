"""plan_trip_execution 脚本辅助测试（无需 API Key、不访问网络）。

运行：python tests/test_plan_trip_script.py
"""
import importlib.util
import os
import sys
from pathlib import Path

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)


def _load_module():
    path = (
        Path(project_root)
        / ".claude"
        / "skills"
        / "plan-trip"
        / "script"
        / "plan_trip_execution.py"
    )
    spec = importlib.util.spec_from_file_location("_test_plan_trip_execution", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_test_plan_trip_execution"] = module
    spec.loader.exec_module(module)
    return module


def test_extract_itinerary_from_partial_failure():
    """partial_failure 时仍提取成功的行程；失败 / 形状非法不提取。"""
    module = _load_module()
    data = {
        "status": "partial_failure",
        "results": [
            {"agent_name": "event_collection", "status": "error"},
            {
                "agent_name": "itinerary_planning",
                "status": "success",
                "data": {"itinerary": {"title": "杭州行"}},
            },
        ],
    }
    assert module._extract_itinerary(data) == {"itinerary": {"title": "杭州行"}}

    assert (
        module._extract_itinerary(
            {"results": [{"agent_name": "itinerary_planning", "data": {"error": "x"}}]}
        )
        is None
    )
    assert module._extract_itinerary("not a dict") is None
    assert module._extract_itinerary({"results": "bad"}) is None
    assert module._extract_itinerary({"results": [None, "x", 3]}) is None
    print("✓ partial_failure 时仍提取成功行程")


def test_extract_itinerary_skips_failed_fallback():
    """失败条目的兜底 itinerary 不能被当成成功行程（主流程会打印「规划完成」）。"""
    module = _load_module()
    failed = {
        "status": "partial_failure",
        "results": [
            {
                "agent_name": "itinerary_planning",
                "status": "error",
                "data": {
                    "itinerary": {
                        "title": "行程规划",
                        "duration": "待完善",
                        "daily_plans": [],
                    },
                    "planning_complete": False,
                    "error": "模型返回的数据格式有误",
                },
            }
        ],
    }
    assert module._extract_itinerary(failed) is None

    # 状态为 success 但 data 自带 error 的旧形状同样不提取
    inconsistent = {
        "results": [
            {
                "agent_name": "itinerary_planning",
                "status": "success",
                "data": {"itinerary": {"title": "x"}, "error": "上游超时"},
            }
        ]
    }
    assert module._extract_itinerary(inconsistent) is None
    print("✓ 失败条目的兜底行程不被当作成功结果")


def test_plan_trip_script_project_root():
    """脚本把仓库根（第 5 层父目录）加入 sys.path，可直接运行。"""
    module = _load_module()
    assert Path(module.project_root).resolve() == Path(project_root).resolve()
    print("✓ 行程脚本 project_root 指向仓库根")


def test_compact_recent_dialogue_trims_assistant_json():
    """追问时最近对话里的整轮结果 JSON 不能原样进入规划 prompt。"""
    import importlib.util
    import json

    path = (
        Path(project_root)
        / ".claude"
        / "skills"
        / "plan-trip"
        / "script"
        / "agent.py"
    )
    spec = importlib.util.spec_from_file_location("_test_plan_trip_agent", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    messages = [
        {"role": "user", "content": "第一问"},
        {
            "role": "assistant",
            "content": json.dumps(
                {"answer": "规划好了", "results": [{"data": "x" * 4000}], "_meta": {}},
                ensure_ascii=False,
            ),
        },
        {"role": "user", "content": "改成周五"},
        {
            "role": "assistant",
            "content": json.dumps({"results": [], "_meta": {}}),
        },
        {"role": "user", "content": "x" * 10000},
    ]
    compacted = module._compact_recent_dialogue(messages)
    assert len(compacted) == 4
    assert compacted[0]["content"] == "规划好了"
    assert compacted[1]["content"] == "改成周五"
    assert "已省略" in compacted[2]["content"]
    assert len(compacted[3]["content"]) == 4000
    print("✓ 最近对话压缩为摘要并截断")


def test_plan_trip_uses_shared_model_factory():
    """模型必须经 create_chat_model 工厂创建（stream=False + generate_kwargs）。"""
    import inspect

    module = _load_module()
    assert module.create_chat_model is not None
    source = inspect.getsource(module.plan_trip)
    assert "create_chat_model()" in source
    assert "OpenAIChatModel(" not in source
    print("✓ 行程脚本使用共享模型工厂")


if __name__ == "__main__":
    test_extract_itinerary_from_partial_failure()
    test_extract_itinerary_skips_failed_fallback()
    test_plan_trip_script_project_root()


    test_compact_recent_dialogue_trims_assistant_json()
    test_plan_trip_uses_shared_model_factory()
    print("\n全部通过")
