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
import time

import pytest

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
        self.session_id = "unit-session"
        self.long_term = LongTermMemory("orchestration_unit_test", tempfile.mkdtemp())


class _StubShortTerm:
    """只提供最近一条用户消息的短期记忆桩。"""

    def __init__(self, user_text: str):
        self.user_text = user_text

    def get_recent_context(self, n_turns: int = 1):
        return [{"role": "user", "content": self.user_text}]


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


def test_failed_itinerary_is_not_saved_as_trip():
    """规划失败的兜底 itinerary 不能写入行程历史。"""
    memory = _StubMemory()
    orchestrator = OrchestrationAgent(agent_registry={}, memory_manager=memory)
    orchestrator._update_memory(
        {},
        [
            {
                "agent_name": "itinerary_planning",
                "priority": 1,
                "result": {
                    "status": "error",
                    "data": {
                        "itinerary": {"title": "兜底行程"},
                        "error": "Failed to parse JSON",
                    },
                },
            }
        ],
    )
    assert memory.long_term.get_trip_history(limit=None) == [], "失败行程不应入库"
    print("✓ 规划失败不写入行程历史")


def test_skill_folder_names_are_canonicalized():
    """调度里使用技能目录名时，结果与行程写入仍按标准智能体名处理。"""
    memory = _StubMemory()

    class EventStub(_StubAgent):
        async def reply(self, x):
            return Msg(
                name=self.name,
                content=json.dumps(
                    {
                        "origin": "北京",
                        "destination": "杭州",
                        "start_date": "2026-12-01",
                        "purpose": "出差",
                    },
                    ensure_ascii=False,
                ),
                role="assistant",
            )

    class PlanStub(_StubAgent):
        async def reply(self, x):
            return Msg(
                name=self.name,
                content=json.dumps({"itinerary": {"title": "杭州行程"}}, ensure_ascii=False),
                role="assistant",
            )

    orchestrator = OrchestrationAgent(
        agent_registry={
            "event-collection": EventStub("event-collection"),
            "plan-trip": PlanStub("plan-trip"),
        },
        memory_manager=memory,
    )
    schedule = [
        {"agent_name": "event-collection", "priority": 1},
        {"agent_name": "plan-trip", "priority": 2},
    ]
    result = json.loads(asyncio.run(orchestrator.reply(_intent(schedule))).content)
    assert [item["agent_name"] for item in result["results"]] == [
        "event_collection",
        "itinerary_planning",
    ]
    trips = memory.long_term.get_trip_history(limit=None)
    assert trips and trips[0]["destination"] == "杭州"
    print("✓ 技能目录名规范化为标准智能体名并正常写入行程")


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
    assert trips[-1]["session_id"] == "unit-session", "行程应记录来源会话"
    print("✓ 偏好覆盖/追加与行程写入正常")


def test_memory_update_legacy_preferences_and_custom_backend():
    """旧字典格式偏好，以及后端没有原子追加时的兜底合并。"""

    class _CustomLongTerm:
        """模拟没有 append_preference 的自定义后端。"""

        def __init__(self):
            self.preferences: dict[str, object] = {}

        def get_preference(self, pref_type=None):
            if pref_type is None:
                return dict(self.preferences)
            return self.preferences.get(pref_type)

        def save_preference(self, pref_type, value):
            self.preferences[pref_type] = value

    memory = _StubMemory()
    memory.long_term = _CustomLongTerm()
    orchestrator = OrchestrationAgent(agent_registry={}, memory_manager=memory)

    # 旧格式：preferences 是字典，跳过 has_preferences / error / 空值
    orchestrator._update_memory(
        {},
        [
            {
                "agent_name": "preference",
                "priority": 1,
                "result": {
                    "status": "success",
                    "data": {
                        "preferences": {
                            "home_location": "上海",
                            "has_preferences": True,
                            "error": "x",
                            "empty": "",
                        }
                    },
                },
            }
        ],
    )
    assert memory.long_term.get_preference() == {"home_location": "上海"}

    # 无原子追加：字符串 + 新值 → 列表
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
                            {
                                "type": "home_location",
                                "value": "北京",
                                "action": "append",
                            }
                        ]
                    },
                },
            }
        ],
    )
    assert memory.long_term.get_preference("home_location") == ["上海", "北京"]

    # 追加已存在的值不产生重复
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
                            {
                                "type": "home_location",
                                "value": "北京",
                                "action": "add",
                            }
                        ]
                    },
                },
            }
        ],
    )
    assert memory.long_term.get_preference("home_location") == ["上海", "北京"]

    # 字符串与新值相同：生成单元素列表而不是重复
    same = _StubMemory()
    same.long_term = _CustomLongTerm()
    same.long_term.save_preference("seat_preference", "靠窗")
    OrchestrationAgent(agent_registry={}, memory_manager=same)._update_memory(
        {},
        [
            {
                "agent_name": "preference",
                "priority": 1,
                "result": {
                    "status": "success",
                    "data": {
                        "preferences": [
                            {
                                "type": "seat_preference",
                                "value": "靠窗",
                                "action": "append",
                            }
                        ]
                    },
                },
            }
        ],
    )
    assert same.long_term.get_preference("seat_preference") == ["靠窗"]

    # 列表形式的 append 值要逐项展开，不能整体塞进列表
    flat = _StubMemory()
    flat.long_term = _CustomLongTerm()
    flat.long_term.save_preference("airlines", ["东航"])
    OrchestrationAgent(agent_registry={}, memory_manager=flat)._update_memory(
        {},
        [
            {
                "agent_name": "preference",
                "priority": 1,
                "result": {
                    "status": "success",
                    "data": {
                        "preferences": [
                            {
                                "type": "airlines",
                                "value": ["国航", "东航"],
                                "action": "append",
                            }
                        ]
                    },
                },
            }
        ],
    )
    assert flat.long_term.get_preference("airlines") == ["东航", "国航"]
    print("✓ 旧格式偏好字典 / 无原子追加后端兜底")


def test_failed_preference_result_not_persisted():
    """status=error 的偏好结果不得落库（与行程分支同一防护）。"""
    memory = _StubMemory()
    OrchestrationAgent(agent_registry={}, memory_manager=memory)._update_memory(
        {},
        [
            {
                "agent_name": "preference",
                "priority": 1,
                "result": {
                    "status": "error",
                    "data": {
                        "preferences": [
                            {
                                "type": "home_location",
                                "value": "上海",
                                "action": "replace",
                            }
                        ],
                        "error": "partial failure",
                    },
                },
            }
        ],
    )
    assert memory.long_term.get_preference() == {}
    print("✓ 失败的偏好结果不落库")


def test_trip_destination_list_saved_as_text():
    """事项收集给出列表型目的地（多城市）时拼成文本落库，不能让整轮失败。"""
    memory = _StubMemory()
    OrchestrationAgent(agent_registry={}, memory_manager=memory)._update_memory(
        {},
        [
            {
                "agent_name": "event_collection",
                "priority": 1,
                "result": {
                    "status": "success",
                    "data": {
                        "origin": "北京",
                        "destination": ["上海", "杭州"],
                        "start_date": "2026-11-09",
                    },
                },
            },
            {
                "agent_name": "itinerary_planning",
                "priority": 2,
                "result": {
                    "status": "success",
                    "data": {"itinerary": {"title": "沪杭行程", "daily_plans": []}},
                },
            },
        ],
    )
    trips = memory.long_term.get_trip_history(limit=5)
    assert trips[-1]["destination"] == "上海、杭州"
    assert trips[-1]["origin"] == "北京"
    print("✓ 列表型目的地拼成文本落库")


def test_transient_error_json_triggers_retry(monkeypatch):
    """技能把瞬时错误吞成 error JSON 时，仍然触发一次退避重试。"""
    from agents import orchestration_agent as module

    monkeypatch.setattr(module, "_AGENT_RETRY_BASE_DELAY", 0.01)
    monkeypatch.setattr(module, "_AGENT_RETRY_MAX_DELAY", 0.02)

    class _FlakyAgent:
        name = "flaky"

        def __init__(self):
            self.calls = 0

        async def reply(self, message):
            self.calls += 1
            if self.calls == 1:
                return Msg(
                    name="flaky",
                    content=json.dumps(
                        {"error": "HTTP 503 service unavailable"},
                        ensure_ascii=False,
                    ),
                    role="assistant",
                )
            return Msg(
                name="flaky",
                content=json.dumps(
                    {"answer": "ok"},
                    ensure_ascii=False,
                ),
                role="assistant",
            )

    flaky = _FlakyAgent()
    orchestrator = OrchestrationAgent(agent_registry={"flaky": flaky})
    result = asyncio.run(
        orchestrator.reply(_intent([{"agent_name": "flaky", "priority": 1}]))
    )
    assert flaky.calls == 2, "瞬时 error JSON 应触发重试"
    assert '"success"' in result.content
    print("✓ 瞬时 error JSON 触发逐智能体重试")


def test_memory_io_runs_off_event_loop():
    """编排器的记忆读写不能在事件循环线程上执行（会卡住所有 SSE）。"""
    import threading

    threads: dict[str, int] = {}
    memory = _StubMemory()
    inner = memory.long_term

    class _RecordingLongTerm:
        def get_preference(self, pref_type=None):
            threads["read"] = threading.get_ident()
            return inner.get_preference(pref_type)

        def save_preference(self, pref_type, value):
            threads["write"] = threading.get_ident()
            return inner.save_preference(pref_type, value)

        def __getattr__(self, name):
            return getattr(inner, name)

    memory.long_term = _RecordingLongTerm()
    agent = _StubAgent(
        "preference",
        {
            "preferences": [
                {"type": "home_location", "value": "上海", "action": "replace"}
            ]
        },
    )
    orchestrator = OrchestrationAgent(
        agent_registry={"preference": agent}, memory_manager=memory
    )
    loop_thread = threading.get_ident()
    asyncio.run(
        orchestrator.reply(_intent([{"agent_name": "preference", "priority": 1}]))
    )
    assert threads["read"] != loop_thread, "记忆读取应在工作线程"
    assert threads["write"] != loop_thread, "记忆写入应在工作线程"
    print("✓ 编排器记忆读写离开事件循环线程")


def test_prepare_context_includes_user_query():
    """上下文携带用户本轮原话，供偏好等抽取使用。"""
    memory = _StubMemory()
    memory.short_term = _StubShortTerm("我常坐东航")
    orchestrator = OrchestrationAgent(agent_registry={}, memory_manager=memory)

    context = orchestrator._prepare_context(
        {"rewritten_query": "用户提到航空公司偏好"}
    )
    assert context["user_query"] == "我常坐东航"
    assert context["rewritten_query"] == "用户提到航空公司偏好"

    # 没有短期记忆的桩（或独立使用）不报错
    bare = _StubMemory()
    context = OrchestrationAgent(agent_registry={}, memory_manager=bare)._prepare_context({})
    assert context["user_query"] == ""
    print("✓ 上下文包含用户本轮原话（user_query）")


def test_agent_status_error_and_null_error():
    """status=error 视为失败；error 为 null 视为正常输出。"""

    class StatusError(_StubAgent):
        async def reply(self, x):
            return Msg(
                name=self.name,
                content=json.dumps({"status": "error", "message": "记忆查询失败"}),
                role="assistant",
            )

    class NullError(_StubAgent):
        async def reply(self, x):
            return Msg(
                name=self.name,
                content=json.dumps({"error": None, "itinerary": {"title": "行程"}}),
                role="assistant",
            )

    failed = OrchestrationAgent(agent_registry={"mem": StatusError("mem")})
    result = json.loads(
        asyncio.run(failed.reply(_intent([{"agent_name": "mem", "priority": 1}]))).content
    )
    assert result["status"] == "partial_failure"
    assert result["results"][0]["status"] == "error"
    assert "记忆查询失败" in json.dumps(result, ensure_ascii=False)

    ok = OrchestrationAgent(agent_registry={"itin": NullError("itin")})
    result_ok = json.loads(
        asyncio.run(ok.reply(_intent([{"agent_name": "itin", "priority": 1}]))).content
    )
    assert result_ok["status"] == "completed"
    assert result_ok["results"][0]["status"] == "success", "error=null 不应判为失败"
    print("✓ status=error / error=null 判定正确")


def test_malformed_schedule_is_sanitized():
    """字符串项 / 非法 priority 的调度计划不应让整轮对话崩溃"""
    orchestrator = OrchestrationAgent(
        agent_registry={"a": _StubAgent("a", {"answer": "ok"})}
    )
    schedule = [
        "a",
        {"agent_name": "a", "priority": "2"},
        {"agent_name": "", "priority": 1},
        {"priority": None},
        None,
    ]
    message = Msg(
        name="IntentionAgent",
        content=json.dumps(
            {"reasoning": "x", "intents": [], "rewritten_query": "q", "agent_schedule": schedule},
            ensure_ascii=False,
        ),
        role="assistant",
    )
    result = json.loads(asyncio.run(orchestrator.reply(message)).content)
    executed = [item["agent_name"] for item in result["results"]]
    assert executed == ["a"], "只执行有效的调度项，且 priority 字符串已转换"
    print("✓ 非法调度计划被清洗（不再整轮 500）")


def test_registry_load_failure_reports_agent_error():
    """懒加载失败应变成单个智能体的错误，而不是整轮 500"""
    from typing import Any, cast

    class FlakyRegistry:
        def __contains__(self, name):
            return True

        def __getitem__(self, name):
            raise ImportError("模型文件缺失")

    events: list[dict] = []
    orchestrator = OrchestrationAgent(agent_registry=cast(Any, FlakyRegistry()))
    result = json.loads(
        asyncio.run(
            orchestrator.reply(
                _intent([{"agent_name": "rag_knowledge", "priority": 1}]),
                on_event=events.append,
            )
        ).content
    )
    assert result["status"] == "partial_failure"
    assert "模型文件缺失" in json.dumps(result, ensure_ascii=False)
    done = [event for event in events if event["type"] == "agent_done"]
    assert done and done[0]["status"] == "error", "加载失败也要发出 agent_done"
    print("✓ 懒加载失败转为智能体错误并发出完成事件")


def test_planning_runs_after_collection_and_sees_new_preferences():
    """行程规划必须排在事项收集 / 偏好抽取之后，并看到本轮新偏好。"""
    order: list[str] = []
    captured: dict = {}

    class Recorder(_StubAgent):
        async def reply(self, x):
            order.append(f"start:{self.name}")
            if self.name == "preference":
                payload = {
                    "preferences": [
                        {"type": "home_location", "value": "上海", "action": "replace"}
                    ]
                }
            elif self.name == "plan-trip":
                captured["payload"] = json.loads(x.content)
                payload = {"itinerary": "ok"}
            else:
                payload = {"origin": "北京"}
            await asyncio.sleep(0.02)
            order.append(f"end:{self.name}")
            return Msg(
                name=self.name,
                content=json.dumps(payload, ensure_ascii=False),
                role="assistant",
            )

    orchestrator = OrchestrationAgent(
        agent_registry={
            "event-collection": Recorder("event-collection"),
            "preference": Recorder("preference"),
            "plan-trip": Recorder("plan-trip"),
        }
    )
    schedule = [
        {"agent_name": "plan-trip", "priority": 1},
        {"agent_name": "event-collection", "priority": 1},
        {"agent_name": "preference", "priority": 1},
    ]
    asyncio.run(orchestrator.reply(_intent(schedule)))

    planning_start = order.index("start:plan-trip")
    assert planning_start > order.index("end:event-collection")
    assert planning_start > order.index("end:preference")
    prefs = captured["payload"]["context"]["user_preferences"]
    assert prefs["home_location"] == "上海"
    print("✓ 行程规划自动后置并看到本轮新偏好")


def test_preference_append_action_variants():
    """Append / add / 追加 等大小写或中文变体都应走追加，不能覆盖已有列表。"""
    memory = _StubMemory()
    memory.long_term.save_preference("hotel_brands", ["汉庭"])
    orchestrator = OrchestrationAgent(agent_registry={}, memory_manager=memory)
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
                            {"type": "hotel_brands", "value": "如家", "action": "Append"}
                        ]
                    },
                },
            }
        ],
    )
    assert memory.long_term.get_preference("hotel_brands") == ["汉庭", "如家"]
    print("✓ 偏好追加动作兼容大小写变体")


def test_infinite_priority_is_sanitized():
    """priority 为 Infinity 时 int() 抛 OverflowError，不能带崩整轮。"""
    orchestrator = OrchestrationAgent(
        agent_registry={"a": _StubAgent("a", {"answer": "ok"})}
    )
    result = json.loads(
        asyncio.run(
            orchestrator.reply(_intent([{"agent_name": "a", "priority": float("inf")}]))
        ).content
    )
    assert result["status"] == "completed"
    print("✓ Infinity 优先级被清洗")


def test_agent_timeout_returns_error(monkeypatch):
    """单个智能体卡住时应超时失败，而不是无限占用整轮。"""
    from agents import orchestration_agent as module

    class Slow(_StubAgent):
        async def reply(self, x):
            await asyncio.sleep(0.2)
            return Msg(name=self.name, content="{}", role="assistant")

    monkeypatch.setattr(module, "_AGENT_TIMEOUT_SEC", 0.05)
    orchestrator = OrchestrationAgent(agent_registry={"slow": Slow("slow")})
    result = json.loads(
        asyncio.run(
            orchestrator.reply(_intent([{"agent_name": "slow", "priority": 1}]))
        ).content
    )
    assert result["status"] == "partial_failure"
    assert "超时" in json.dumps(result, ensure_ascii=False)
    print("✓ 单智能体超时转为错误结果")


def test_subagent_transient_error_retried(monkeypatch):
    """子智能体的瞬时连接错误应重试一次，而不是直接失败。"""
    from agents import orchestration_agent as module

    class Flaky(_StubAgent):
        def __init__(self, name):
            super().__init__(name)
            self.failures = 0

        async def reply(self, x):
            self.calls += 1
            if self.failures == 0:
                self.failures = 1
                raise ConnectionError("temporary")
            return Msg(
                name=self.name,
                content=json.dumps({"answer": "ok"}),
                role="assistant",
            )

    monkeypatch.setattr(module, "_AGENT_RETRY_BASE_DELAY", 0.01)
    agent = Flaky("a")
    orchestrator = OrchestrationAgent(agent_registry={"a": agent})
    result = json.loads(
        asyncio.run(
            orchestrator.reply(_intent([{"agent_name": "a", "priority": 1}]))
        ).content
    )
    assert result["status"] == "completed"
    assert agent.calls == 2, "瞬时错误应重试一次"
    print("✓ 子智能体瞬时错误重试一次")


def test_conversation_schedule_normalization():
    """进度事件的调度计划应统一名称、过滤空白项与非法优先级。"""
    from agents.conversation import ConversationRuntime

    normalized = ConversationRuntime._normalized_schedule(
        [
            {"agent_name": "plan-trip", "priority": float("inf")},
            {"agent_name": "  ", "priority": 1},
            "bad",
            {"agent_name": "query-info"},
        ]
    )
    assert normalized == [
        {"agent_name": "itinerary_planning", "priority": 0, "reason": ""},
        {"agent_name": "information_query", "priority": 0, "reason": ""},
    ]
    print("✓ 进度调度计划规范化")


def test_partial_failure_does_not_trip_breaker():
    """partial_failure 是子智能体故障，不计入 LLM 熔断（避免误伤所有用户）。"""
    from agents.conversation import ConversationRuntime
    from utils.circuit_breaker import CircuitBreaker

    runtime = object.__new__(ConversationRuntime)
    runtime.circuit_breaker = CircuitBreaker(failure_threshold=2)

    async def _result():
        return Msg(
            name="orchestrator",
            content=json.dumps({"status": "partial_failure"}),
            role="assistant",
        )

    for _ in range(5):
        asyncio.run(runtime._call_with_resilience(_result, 0, 0.0, 0.0))
    status = runtime.circuit_breaker.get_status()
    assert status["state"] == "closed", status
    assert status["failure_count"] == 0, status
    print("✓ 部分失败不计入 LLM 熔断")


def test_previous_preference_append_flattens_list():
    """前序批次的 append 偏好值是列表时应逐项合并，不能产生嵌套列表。"""
    orchestrator = OrchestrationAgent(agent_registry={})
    context = {"user_preferences": {"airlines": ["东航"]}}
    previous = [
        {
            "agent_name": "preference",
            "result": {
                "data": {
                    "preferences": [
                        {"type": "airlines", "value": ["国航", "东航"], "action": "append"}
                    ]
                }
            },
        }
    ]
    merged = orchestrator._context_with_previous_preferences(context, previous)
    assert merged["user_preferences"]["airlines"] == ["东航", "国航"]

    # 0 / False 是合法偏好，append 时不能被真值判断丢掉
    falsy = {
        "user_preferences": {"budget_min": 0, "window_seat": False},
    }
    falsy_previous = [
        {
            "agent_name": "preference",
            "result": {
                "data": {
                    "preferences": [
                        {"type": "budget_min", "value": 500, "action": "append"},
                        {"type": "window_seat", "value": True, "action": "append"},
                    ]
                }
            },
        }
    ]
    merged_falsy = orchestrator._context_with_previous_preferences(falsy, falsy_previous)
    assert merged_falsy["user_preferences"]["budget_min"] == [0, 500]
    assert merged_falsy["user_preferences"]["window_seat"] == [False, True]
    print("✓ 前序偏好 append 列表逐项合并（含假值偏好）")


def test_schedule_names_are_trimmed():
    """调度计划里的名称带空白时要裁剪，否则注册表查找与依赖判断都会失效。"""
    calls = []

    class Echo(_StubAgent):
        async def reply(self, x):
            calls.append(self.name)
            return Msg(name=self.name, content="{}", role="assistant")

    orchestrator = OrchestrationAgent(agent_registry={"a": Echo("a")})
    asyncio.run(
        orchestrator.reply(_intent([{"agent_name": "  a  ", "priority": 1}]))
    )
    assert calls == ["a"], f"空白名称未被裁剪：{calls}"
    print("✓ 调度计划名称裁剪")


def test_agent_load_timeout_is_bounded(monkeypatch):
    """懒加载卡住也要受单智能体超时约束（不无限占用会话锁）。"""
    import agents.orchestration_agent as module

    class SlowRegistry(dict):
        def __getitem__(self, name):
            time.sleep(0.5)
            return super().__getitem__(name)

    monkeypatch.setattr(module, "_AGENT_TIMEOUT_SEC", 0.05)
    orchestrator = OrchestrationAgent(
        agent_registry=SlowRegistry({"a": _StubAgent("a", {"answer": "ok"})})
    )
    result = asyncio.run(
        orchestrator.reply(_intent([{"agent_name": "a", "priority": 1}]))
    )
    payload = json.loads(result.content)
    assert payload["status"] == "partial_failure", payload
    first = payload["results"][0]
    assert first["status"] == "error"
    error_text = (first.get("data") or {}).get("error") or ""
    assert "超时" in error_text, error_text
    print("✓ 懒加载受智能体超时约束")


if __name__ == "__main__":
    test_priority_parallel_and_sequential()
    test_previous_results_passed_to_next_priority()
    test_failure_aggregated_as_partial_failure()
    test_agent_duration_and_progress_events()
    test_unregistered_agent_reports_error()
    test_agent_status_error_and_null_error()
    test_malformed_schedule_is_sanitized()
    test_registry_load_failure_reports_agent_error()
    test_failed_itinerary_is_not_saved_as_trip()
    test_skill_folder_names_are_canonicalized()
    test_memory_update_preference_and_trip()
    test_memory_update_legacy_preferences_and_custom_backend()
    test_failed_preference_result_not_persisted()
    test_trip_destination_list_saved_as_text()
    _mp_retry = pytest.MonkeyPatch()
    try:
        test_transient_error_json_triggers_retry(_mp_retry)
    finally:
        _mp_retry.undo()
    test_memory_io_runs_off_event_loop()
    test_prepare_context_includes_user_query()
    test_planning_runs_after_collection_and_sees_new_preferences()
    test_preference_append_action_variants()
    test_infinite_priority_is_sanitized()
    test_partial_failure_does_not_trip_breaker()
    test_previous_preference_append_flattens_list()
    test_schedule_names_are_trimmed()
    _mp_load = pytest.MonkeyPatch()
    try:
        test_agent_load_timeout_is_bounded(_mp_load)
    finally:
        _mp_load.undo()
    print("\n全部通过")
