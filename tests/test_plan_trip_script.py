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
    test_plan_trip_uses_shared_model_factory()
    print("\n全部通过")
