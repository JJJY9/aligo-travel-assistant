"""
编排器单元测试（无需 API Key、不访问网络）

用桩智能体验证 OrchestrationAgent 的调度逻辑（此前只有依赖真实大模型的
端到端测试覆盖）：

1. 同优先级并行执行、不同优先级串行（批次顺序正确）；
2. 前一优先级的执行结果会传给后续优先级的智能体（previous_results）；
3. 单个智能体失败不影响整批（聚合为 partial_failure）；
4. 未注册智能体返回错误结果；
5. 记忆更新：偏好追加 / 覆盖、行程写入。

运行：python tests/test_orchestration_unit.py
"""
import asyncio
import json
import os
import sys
import tempfile

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from agentscope.message import Msg

from agents.orchestration_agent import OrchestrationAgent
from context.long_term_memory import LongTermMemory


class _StubAgent:
    """记录调用行为的基础桩智能体。"""

    def __init__(self, name: str, result: dict | None = None, raise_error: bool = False):
        self.name = name
        self.result = result or {}
        self.raise_error = raise_error
        self.calls = 0

    async def reply(self, x):
        self.calls += 1
        if self.raise_error:
            raise RuntimeError(f"{self.name} boom")
        return Msg(
            name=self.name,
            content=json.dumps(self.result, ensure_ascii=False),
            role="assistant",
        )


class _StubMemory:
    """带真实 LongTermMemory 的桩记忆管理器（写入临时目录）。"""

    def __init__(self):
        self.long_term = LongTermMemory("orchestration_unit_test", tempfile.mkdtemp())


def _intent(schedule: list[dict]) -> Msg:
    return Msg(
        name="IntentionAgent",
        content=json.dumps(
            {
                "reasoning": "unit test",
                "intents": [],
                "key_entities": {},
                "rewritten_query": "测试查询",
                "agent_schedule": schedule,
            },
            ensure_ascii=False,
        ),
        role="assistant",
    )


def test_priority_parallel_and_sequential():
    """同优先级并行、下一个优先级必须等整批完成"""
    order: list[str] = []

    class Recorder(_StubAgent):
        async def reply(self, x):
            order.append(f"start:{self.name}")
            await asyncio.sleep(0.05)
            order.append(f"end:{self.name}")
            return Msg(name=self.name, content="{}", role="assistant")

    orchestrator = OrchestrationAgent(
        agent_registry={"a": Recorder("a"), "b": Recorder("b"), "c": Recorder("c")}
    )
    schedule = [
        {"agent_name": "c", "priority": 2},
        {"agent_name": "a", "priority": 1},
        {"agent_name": "b", "priority": 1},
    ]
    asyncio.run(orchestrator.reply(_intent(schedule)))

    start_a, start_b = order.index("start:a"), order.index("start:b")
    end_a, end_b = order.index("end:a"), order.index("end:b")
    start_c = order.index("start:c")

    assert start_a < end_b and start_b < end_a, f"同优先级应并行执行: {order}"
    assert max(end_a, end_b) < start_c, f"优先级 2 必须在批次 1 全部完成后开始: {order}"
    print("✓ 同优先级并行、不同优先级串行")


def test_previous_results_passed_to_next_priority():
    """前一优先级的结果通过 previous_results 传给后续智能体"""
    captured: dict = {}

    class Capture(_StubAgent):
        async def reply(self, x):
            captured["payload"] = json.loads(x.content)
            return Msg(name=self.name, content="{}", role="assistant")

    orchestrator = OrchestrationAgent(
        agent_registry={"a": _StubAgent("a", {"answer": "A"}), "b": Capture("b")}
    )
    schedule = [
        {"agent_name": "a", "priority": 1},
        {"agent_name": "b", "priority": 2},
    ]
    asyncio.run(orchestrator.reply(_intent(schedule)))

    previous = captured["payload"]["previous_results"]
    assert any(item["agent_name"] == "a" for item in previous), "前序结果应传入后续批次"
    assert captured["payload"]["context"]["rewritten_query"] == "测试查询"
    print("✓ 前序结果传递给后续优先级")


def test_failure_aggregated_as_partial_failure():
    """单个智能体失败不影响整批，状态聚合为 partial_failure"""
    orchestrator = OrchestrationAgent(
        agent_registry={
            "ok_agent": _StubAgent("ok_agent", {"answer": "fine"}),
            "bad_agent": _StubAgent("bad_agent", raise_error=True),
        }
    )
    schedule = [
        {"agent_name": "ok_agent", "priority": 1},
        {"agent_name": "bad_agent", "priority": 1},
    ]
    result = json.loads(asyncio.run(orchestrator.reply(_intent(schedule))).content)

    assert result["status"] == "partial_failure"
    by_name = {item["agent_name"]: item["status"] for item in result["results"]}
    assert by_name == {"ok_agent": "success", "bad_agent": "error"}
    print("✓ 单智能体失败聚合为 partial_failure")


def test_agent_duration_and_progress_events():
    """agent_done 事件与聚合结果都带上单个智能体耗时"""
    events: list[dict] = []
    orchestrator = OrchestrationAgent(agent_registry={"a": _StubAgent("a", {"answer": "ok"})})
    message = asyncio.run(
        orchestrator.reply(
            _intent([{"agent_name": "a", "priority": 1}]),
            on_event=events.append,
        )
    )
    result = json.loads(message.content)
    assert result["results"][0]["duration"] >= 0
    done_events = [event for event in events if event["type"] == "agent_done"]
    assert done_events and done_events[0]["duration"] >= 0
    print("✓ 智能体耗时写入进度事件与聚合结果")


def test_unregistered_agent_reports_error():
    orchestrator = OrchestrationAgent(agent_registry={})
    result = json.loads(
        asyncio.run(orchestrator.reply(_intent([{"agent_name": "missing", "priority": 1}]))).content
    )
    assert result["status"] == "partial_failure"
    assert "未注册" in json.dumps(result, ensure_ascii=False)
    print("✓ 未注册智能体返回错误结果")


def test_memory_update_preference_and_trip():
    """偏好覆盖 / 追加与行程写入"""
    memory = _StubMemory()
    orchestrator = OrchestrationAgent(agent_registry={}, memory_manager=memory)

    # 覆盖：hotel_brands -> 汉庭
    orchestrator._update_memory(
        {},
        [
            {
                "agent_name": "preference",
                "priority": 1,
                "result": {
                    "status": "success",
                    "data": {"preferences": [{"type": "hotel_brands", "value": "汉庭", "action": "replace"}]},
                },
            }
        ],
    )
    assert memory.long_term.get_preference("hotel_brands") == "汉庭"

    # 追加同值：存量是字符串且与新值相同，不应产生重复列表
    orchestrator._update_memory(
        {},
        [
            {
                "agent_name": "preference",
                "priority": 1,
                "result": {
                    "status": "success",
                    "data": {
                        "preferences": [
                            {"type": "hotel_brands", "value": "汉庭", "action": "append"}
                        ]
                    },
                },
            }
        ],
    )
    assert memory.long_term.get_preference("hotel_brands") == ["汉庭"]

    # 追加：seat_preference 已有值，action=append 生成列表
    memory.long_term.save_preference("seat_preference", "靠窗")
    orchestrator._update_memory(
        {},
        [
            {
                "agent_name": "preference",
                "priority": 1,
                "result": {
                    "status": "success",
                    "data": {
                        "preferences": [{"type": "seat_preference", "value": "过道", "action": "append"}]
                    },
                },
            }
        ],
    )
    assert memory.long_term.get_preference("seat_preference") == ["靠窗", "过道"]

    # 行程：行程规划 + 事项收集结果写入行程历史
    orchestrator._update_memory(
        {},
        [
            {
                "agent_name": "event_collection",
                "priority": 1,
                "result": {
                    "status": "success",
                    "data": {
                        "origin": "北京",
                        "destination": "上海",
                        "start_date": "2026-11-09",
                        "end_date": "2026-11-11",
                        "trip_purpose": "出差",
                    },
                },
            },
            {
                "agent_name": "itinerary_planning",
                "priority": 2,
                "result": {
                    "status": "success",
                    "data": {"itinerary": {"title": "测试行程", "daily_plans": []}},
                },
            },
        ],
    )
    trips = memory.long_term.get_trip_history(limit=5)
    assert trips[-1]["destination"] == "上海"
    assert trips[-1]["origin"] == "北京"
    print("✓ 偏好覆盖/追加与行程写入正常")


if __name__ == "__main__":
    test_priority_parallel_and_sequential()
    test_previous_results_passed_to_next_priority()
    test_failure_aggregated_as_partial_failure()
    test_agent_duration_and_progress_events()
    test_unregistered_agent_reports_error()
    test_memory_update_preference_and_trip()
    print("\n全部通过")
