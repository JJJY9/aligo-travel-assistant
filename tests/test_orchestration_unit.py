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
    """agent_done 事件与聚合结果都带上单个智能体耗时（非恒 0）"""
    events: list[dict] = []

    class _SlowAgent(_StubAgent):
        async def reply(self, x):
            await asyncio.sleep(0.05)
            return Msg(name=self.name, content="{}", role="assistant")

    orchestrator = OrchestrationAgent(agent_registry={"a": _SlowAgent("a")})
    message = asyncio.run(
        orchestrator.reply(
            _intent([{"agent_name": "a", "priority": 1}]),
            on_event=events.append,
        )
    )
    result = json.loads(message.content)
    # 桩智能体实际休眠 50ms：duration 恒 0 / 未计时都会在此失败
    assert result["results"][0]["duration"] >= 0.04, (
        f"聚合结果耗时异常：{result['results'][0]['duration']}"
    )
    done_events = [event for event in events if event["type"] == "agent_done"]
    assert done_events and done_events[0]["duration"] >= 0.04
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


class _CaseInsensitiveRegistry(dict):
    """模拟 LazyAgentRegistry 的大小写不敏感名称解析。"""

    def __contains__(self, key):
        return super().__contains__(str(key).casefold())

    def __getitem__(self, key):
        return super().__getitem__(str(key).casefold())


def test_skill_names_resolve_case_insensitively():
    """模型输出 Plan-Trip / EVENT-COLLECTION 时不能当成未注册整轮失败。"""
    memory = _StubMemory()

    class PlanStub(_StubAgent):
        async def reply(self, x):
            return Msg(
                name=self.name,
                content=json.dumps(
                    {"itinerary": {"title": "杭州行程"}}, ensure_ascii=False
                ),
                role="assistant",
            )

    registry = _CaseInsensitiveRegistry({"plan-trip": PlanStub("plan-trip")})
    orchestrator = OrchestrationAgent(
        agent_registry=registry, memory_manager=memory
    )
    result = json.loads(
        asyncio.run(
            orchestrator.reply(
                _intent([{"agent_name": "Plan-Trip", "priority": 1}])
            )
        ).content
    )
    assert result["status"] == "completed", result
    assert result["results"][0]["status"] == "success", result
    assert [item["agent_name"] for item in result["results"]] == [
        "itinerary_planning"
    ]
    print("✓ 大小写不同的调度名解析并规范化为标准名")


def test_legacy_name_variants_dedupe_and_planning_floor():
    """遗留标准名的大小写变体：同一技能只执行一次，且规划排到事项收集之后。"""
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
                content=json.dumps(
                    {"itinerary": {"title": "杭州行程"}}, ensure_ascii=False
                ),
                role="assistant",
            )

    registry = _CaseInsensitiveRegistry(
        {
            "itinerary_planning": PlanStub("itinerary_planning"),
            "event_collection": EventStub("event_collection"),
        }
    )
    orchestrator = OrchestrationAgent(
        agent_registry=registry, memory_manager=memory
    )
    result = json.loads(
        asyncio.run(
            orchestrator.reply(
                _intent(
                    [
                        {"agent_name": "Itinerary_Planning", "priority": 1},
                        {"agent_name": "itinerary_planning", "priority": 1},
                        {"agent_name": "Event_Collection", "priority": 1},
                    ]
                )
            )
        ).content
    )
    names = [item["agent_name"] for item in result["results"]]
    assert names.count("itinerary_planning") == 1, names
    assert "Itinerary_Planning" not in names and "Event_Collection" not in names, names
    assert names.index("event_collection") < names.index("itinerary_planning"), names
    print("✓ 遗留名大小写变体去重并保持规划依赖排序")


def test_preference_name_variants_persist_and_dedupe():
    """模型把偏好智能体写成 Preference：仍落库、参与去重与规划排序。"""
    memory = _StubMemory()

    class PrefStub(_StubAgent):
        async def reply(self, x):
            self.calls += 1
            return Msg(
                name=self.name,
                content=json.dumps(
                    {
                        "preferences": [
                            {
                                "type": "home_location",
                                "value": "上海",
                                "action": "replace",
                            }
                        ],
                        "has_preferences": True,
                    },
                    ensure_ascii=False,
                ),
                role="assistant",
            )

    class PlanStub(_StubAgent):
        async def reply(self, x):
            return Msg(
                name=self.name,
                content=json.dumps(
                    {"itinerary": {"title": "杭州行程"}}, ensure_ascii=False
                ),
                role="assistant",
            )

    pref = PrefStub("preference")
    plan = PlanStub("itinerary_planning")
    registry = _CaseInsensitiveRegistry(
        {"preference": pref, "itinerary_planning": plan}
    )
    orchestrator = OrchestrationAgent(
        agent_registry=registry, memory_manager=memory
    )
    result = json.loads(
        asyncio.run(
            orchestrator.reply(
                _intent(
                    [
                        {"agent_name": "Preference", "priority": 1},
                        {"agent_name": "preference", "priority": 1},
                        {"agent_name": "itinerary_planning", "priority": 1},
                    ]
                )
            )
        ).content
    )
    names = [item["agent_name"] for item in result["results"]]
    assert names.count("preference") == 1, names
    assert "Preference" not in names, names
    assert names.index("preference") < names.index("itinerary_planning"), names
    assert pref.calls == 1, "同一技能的两种拼写只应执行一次"
    assert memory.long_term.get_preference("home_location") == "上海"
    print("✓ Preference 大小写变体落库、去重并保持规划依赖排序")


def test_agent_result_surrogates_sanitized_before_persist():
    """技能结果含孤立代理项：编排器入口转义后偏好仍能落库（不瘫整轮）。"""
    memory = _StubMemory()

    class _SurrogateStub(_StubAgent):
        async def reply(self, x):
            payload = {
                "preferences": [
                    {
                        "type": "hotel_brands",
                        "value": "汉庭" + chr(0xD800),
                        "action": "replace",
                    }
                ],
                "has_preferences": True,
            }
            return Msg(
                name=self.name,
                content=json.dumps(payload, ensure_ascii=False),
                role="assistant",
            )

    registry = _CaseInsensitiveRegistry(
        {"preference": _SurrogateStub("preference")}
    )
    orchestrator = OrchestrationAgent(
        agent_registry=registry, memory_manager=memory
    )
    content = asyncio.run(
        orchestrator.reply(_intent([{"agent_name": "preference", "priority": 1}]))
    ).content
    result = json.loads(content)
    assert result["results"][0]["status"] == "success", result
    saved = memory.long_term.get_preference("hotel_brands")
    assert "汉庭" in saved and "\\ud800" in saved, saved
    saved.encode("utf-8")
    print("✓ 技能结果的孤立代理项在落库前转义")


def test_model_output_nul_stripped_before_memory():
    """模型输出里的 NUL 在落库前剔除（PostgreSQL 会确定性拒绝）。"""
    memory = _StubMemory()

    class _NulStub(_StubAgent):
        async def reply(self, x):
            payload = {
                "preferences": [
                    {
                        "type": "hotel_brands",
                        "value": "汉" + chr(0) + "庭",
                        "action": "replace",
                    }
                ],
                "has_preferences": True,
            }
            return Msg(
                name=self.name,
                content=json.dumps(payload, ensure_ascii=False),
                role="assistant",
            )

    orchestrator = OrchestrationAgent(
        agent_registry={"preference": _NulStub("preference")},
        memory_manager=memory,
    )
    asyncio.run(
        orchestrator.reply(_intent([{"agent_name": "preference", "priority": 1}]))
    )
    assert memory.long_term.get_preference("hotel_brands") == "汉庭"
    print("✓ 技能结果的 NUL 在落库前剔除")


def test_preference_payload_with_extra_keys_does_not_write_garbage():
    """包裹层带 note 等说明键时，不能把 preferences/note 当偏好落库。"""
    memory = _StubMemory()
    orchestrator = OrchestrationAgent(agent_registry={}, memory_manager=memory)
    result = {
        "agent_name": "preference",
        "priority": 1,
        "result": {
            "status": "success",
            "data": {
                "preferences": {
                    "preferences": [{"type": "airlines", "value": "东航"}],
                    "note": "模型说明",
                }
            },
        },
    }
    orchestrator._update_memory({}, [result])
    prefs = memory.long_term.get_preference()
    assert prefs.get("airlines") == "东航", prefs
    assert "preferences" not in prefs and "note" not in prefs, prefs

    ctx = orchestrator._context_with_previous_preferences({}, [result])
    assert ctx["user_preferences"].get("airlines") == "东航", ctx
    assert "note" not in ctx["user_preferences"], ctx
    print("✓ 偏好包裹层的说明键不落库也不进后续上下文")


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


def test_nested_query_failure_is_reported_as_error():
    """query_success=false 嵌在 results 里的失败必须聚合为 error 而不是成功。"""

    class NestedError(_StubAgent):
        async def reply(self, x):
            return Msg(
                name=self.name,
                content=json.dumps(
                    {
                        "query_type": "网络搜索",
                        "query_success": False,
                        "results": {"error": "搜索失败: 403 Forbidden"},
                    },
                    ensure_ascii=False,
                ),
                role="assistant",
            )

    class NestedMessage(_StubAgent):
        async def reply(self, x):
            return Msg(
                name=self.name,
                content=json.dumps(
                    {
                        "query_type": "网络搜索",
                        "query_success": False,
                        "results": {"message": "搜索库未安装"},
                    },
                    ensure_ascii=False,
                ),
                role="assistant",
            )

    orchestrator = OrchestrationAgent(
        agent_registry={"search": NestedError("search"), "kb": NestedMessage("kb")}
    )
    result = json.loads(
        asyncio.run(
            orchestrator.reply(
                _intent(
                    [
                        {"agent_name": "search", "priority": 1},
                        {"agent_name": "kb", "priority": 1},
                    ]
                )
            )
        ).content
    )
    assert result["status"] == "partial_failure"
    by_name = {item["agent_name"]: item["status"] for item in result["results"]}
    assert by_name == {"search": "error", "kb": "error"}, by_name
    payload = json.dumps(result, ensure_ascii=False)
    assert "搜索失败: 403" in payload and "搜索库未安装" in payload
    print("✓ 嵌套 results 的查询失败聚合为 error")


def test_transient_nested_error_triggers_retry():
    """嵌套在 results 里的瞬时错误（timeout / connection）也要触发重试。"""
    from agents import orchestration_agent as module

    class _Reply:
        def __init__(self, content):
            self.content = content

    assert (
        module._transient_agent_error(
            _Reply(
                json.dumps(
                    {"query_success": False, "results": {"error": "搜索失败: timeout"}}
                )
            )
        )
        == "搜索失败: timeout"
    )
    assert (
        module._transient_agent_error(
            _Reply(
                json.dumps(
                    {"query_success": False, "results": {"message": "未找到相关结果"}}
                )
            )
        )
        is None
    )
    print("✓ 嵌套瞬时错误触发重试、永久错误不重试")


def test_all_invalid_schedule_degrades_to_default_query():
    """调度项全部非法时降级为默认信息查询，不能静默 no_agents。"""
    orchestrator = OrchestrationAgent(
        agent_registry={
            "information_query": _StubAgent(
                "information_query", {"answer": "ok"}
            )
        }
    )
    schedule = ["a", {"priority": 1}, None]
    result = json.loads(asyncio.run(orchestrator.reply(_intent(schedule))).content)
    assert result.get("status") != "no_agents", result
    executed = [item["agent_name"] for item in result["results"]]
    assert executed == ["information_query"], result
    print("✓ 非法调度全部丢弃时降级默认信息查询")


def test_wrapped_preferences_dict_unpacked_on_save_and_context():
    """{"preferences": [...]} 包裹形态不能写成垃圾键，真实偏好照常落库与进上下文。"""
    memory = _StubMemory()
    orchestrator = OrchestrationAgent(agent_registry={}, memory_manager=memory)
    wrapped = {
        "preferences": {
            "preferences": [{"type": "airlines", "value": "东航"}]
        }
    }
    orchestrator._update_memory(
        {},
        [
            {
                "agent_name": "preference",
                "priority": 1,
                "result": {"status": "success", "data": wrapped},
            }
        ],
    )
    prefs = memory.long_term.get_preference()
    assert prefs.get("airlines") == "东航", prefs
    assert "preferences" not in prefs, prefs
    ctx = orchestrator._context_with_previous_preferences(
        {},
        [
            {
                "agent_name": "preference",
                "result": {"status": "success", "data": wrapped},
            }
        ],
    )
    assert ctx["user_preferences"].get("airlines") == "东航"
    assert "preferences" not in ctx["user_preferences"]
    print("✓ 包裹形态偏好被解包（落库与上下文）")


def test_data_data_nested_preferences_unpacked():
    """{"data": {"data": {"preferences": ...}}} 再包一层时偏好照常落库与进上下文。"""
    memory = _StubMemory()
    orchestrator = OrchestrationAgent(agent_registry={}, memory_manager=memory)
    double_wrapped = {
        "data": {"preferences": [{"type": "airlines", "value": "东航"}]}
    }
    result = {
        "agent_name": "preference",
        "priority": 1,
        "result": {"status": "success", "data": double_wrapped},
    }
    orchestrator._update_memory({}, [result])
    prefs = memory.long_term.get_preference()
    assert prefs == {"airlines": "东航"}, prefs
    assert "data" not in prefs and "preferences" not in prefs

    ctx = orchestrator._context_with_previous_preferences({}, [result])
    assert ctx["user_preferences"] == {"airlines": "东航"}, ctx

    # 旧版 {type: value} 字典嵌套在 data.data 里同样解包
    legacy_memory = _StubMemory()
    legacy_orchestrator = OrchestrationAgent(
        agent_registry={}, memory_manager=legacy_memory
    )
    legacy_orchestrator._update_memory(
        {},
        [
            {
                "agent_name": "preference",
                "priority": 1,
                "result": {
                    "status": "success",
                    "data": {"data": {"preferences": {"home_location": "上海"}}},
                },
            }
        ],
    )
    assert legacy_memory.long_term.get_preference() == {"home_location": "上海"}
    print("✓ data.data 嵌套偏好被解包（落库与上下文）")


def test_unmanageable_preference_types_skipped_everywhere():
    """含 "/" / 65 字符 / 非字符串的偏好键在落库与上下文合并处统一丢弃。"""
    from utils.preference_values import is_manageable_pref_type

    assert is_manageable_pref_type("k" * 64) is True
    assert is_manageable_pref_type("k" * 65) is False
    assert is_manageable_pref_type("a/b") is False
    assert is_manageable_pref_type(123) is False
    assert is_manageable_pref_type("") is False

    entries = [
        {"type": "a/b", "value": "x"},
        {"type": "k" * 65, "value": "y"},
        {"type": "k" * 64, "value": "z"},
        {"type": "hotel_brands", "value": "汉庭"},
    ]
    memory = _StubMemory()
    orchestrator = OrchestrationAgent(agent_registry={}, memory_manager=memory)
    list_result = {
        "agent_name": "preference",
        "priority": 1,
        "result": {"status": "success", "data": {"preferences": entries}},
    }
    orchestrator._update_memory({}, [list_result])
    assert memory.long_term.get_preference() == {
        "k" * 64: "z",
        "hotel_brands": "汉庭",
    }

    # 旧版字典分支同样过滤
    legacy_memory = _StubMemory()
    legacy_orchestrator = OrchestrationAgent(
        agent_registry={}, memory_manager=legacy_memory
    )
    dict_result = {
        "agent_name": "preference",
        "priority": 1,
        "result": {
            "status": "success",
            "data": {
                "preferences": {
                    "a/b": "x",
                    "k" * 65: "y",
                    "k" * 64: "z",
                    "hotel_brands": "汉庭",
                }
            },
        },
    }
    legacy_orchestrator._update_memory({}, [dict_result])
    assert legacy_memory.long_term.get_preference() == {
        "k" * 64: "z",
        "hotel_brands": "汉庭",
    }

    # 后续批次上下文：列表与字典形态都不携带非法键
    ctx = orchestrator._context_with_previous_preferences({}, [list_result])
    assert ctx["user_preferences"] == {"k" * 64: "z", "hotel_brands": "汉庭"}
    ctx_dict = legacy_orchestrator._context_with_previous_preferences({}, [dict_result])
    assert ctx_dict["user_preferences"] == {
        "k" * 64: "z",
        "hotel_brands": "汉庭",
    }
    print("✓ 非法偏好键在落库 / 上下文 / 边界判定处同口径过滤")


def test_agent_load_runs_in_dedicated_executor():
    """子智能体懒加载在 aligo-agent-load 专用线程执行，不占默认 asyncio 线程池。"""
    import threading

    records: list[str] = []

    class RecordingRegistry(dict):
        def __getitem__(self, key):
            records.append(threading.current_thread().name)
            return super().__getitem__(key)

    orchestrator = OrchestrationAgent(
        agent_registry=RecordingRegistry({"a": _StubAgent("a", {"reply": "ok"})})
    )
    asyncio.run(orchestrator.reply(_intent([{"agent_name": "a", "priority": 1}])))
    assert records, "懒加载必须经过注册表 __getitem__"
    assert records[0].startswith("aligo-agent-load"), records
    assert records[0] != threading.main_thread().name
    print("✓ 子智能体懒加载在专用执行器线程执行")


def test_rag_load_uses_isolated_executor():
    """RAG 冷加载独占执行器：模型加载不把其它技能加载排到超时。"""
    import threading

    records: list[str] = []

    class RecordingRegistry(dict):
        def __getitem__(self, key):
            records.append(threading.current_thread().name)
            return super().__getitem__(key)

    orchestrator = OrchestrationAgent(
        agent_registry=RecordingRegistry(
            {"ask-question": _StubAgent("ask-question", {"answer": "ok"})}
        )
    )
    asyncio.run(
        orchestrator.reply(
            _intent([{"agent_name": "ask-question", "priority": 1}])
        )
    )
    assert records and records[0].startswith("aligo-rag-load"), records
    print("✓ RAG 知识库加载在隔离执行器线程执行")


def test_rag_load_executor_accepts_standard_name():
    """兼容标准名 / 大小写写法都路由到 RAG 专用执行器。"""
    from agents import orchestration_agent as module
    from agents.orchestration_agent import _load_executor_for

    for name in ("ask-question", "rag_knowledge", "ASK-QUESTION", "RAG_KNOWLEDGE"):
        assert _load_executor_for(name) is module._RAG_LOAD_EXECUTOR, name
    for name in ("preference", "event-collection", "memory-query"):
        assert _load_executor_for(name) is module._AGENT_LOAD_EXECUTOR, name
    print("✓ 执行器路由覆盖技能目录名 / 标准名 / 大小写")


def test_preference_render_skips_unmanageable_keys():
    """回显与落库同口径：含 "/" 的键不出现「已更新偏好」。"""
    from utils.result_formatter import result_to_markdown

    text = result_to_markdown(
        {
            "results": [
                {
                    "agent_name": "preference",
                    "status": "success",
                    "data": {
                        "preferences": [
                            {"type": "a/b", "value": "x"},
                            {"type": "airlines", "value": "东航"},
                        ]
                    },
                }
            ]
        }
    )
    assert "东航" in text
    assert "a/b" not in text
    print("✓ 偏好回显跳过不可管理的键")


def test_trip_saved_from_nested_data_payload():
    """{"data": {...}} 嵌套形状（展示层已兼容）同样写进行程历史。"""
    memory = _StubMemory()
    orchestrator = OrchestrationAgent(agent_registry={}, memory_manager=memory)
    orchestrator._update_memory(
        {},
        [
            {
                "agent_name": "event_collection",
                "priority": 1,
                "result": {
                    "status": "success",
                    "data": {
                        "data": {
                            "origin": "北京",
                            "destination": "杭州",
                            "start_date": "2026-12-01",
                            "trip_purpose": "出差",
                        }
                    },
                },
            },
            {
                "agent_name": "itinerary_planning",
                "priority": 2,
                "result": {
                    "status": "success",
                    "data": {
                        "data": {"itinerary": {"title": "杭州行", "daily_plans": []}}
                    },
                },
            },
        ],
    )
    trips = memory.long_term.get_trip_history(limit=5)
    assert trips and trips[-1]["destination"] == "杭州", trips
    print("✓ data.data 嵌套的行程同样落库")


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
                "status": "success",
                "data": {
                    "preferences": [
                        {"type": "airlines", "value": ["国航", "东航"], "action": "append"}
                    ]
                },
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
                "status": "success",
                "data": {
                    "preferences": [
                        {"type": "budget_min", "value": 500, "action": "append"},
                        {"type": "window_seat", "value": True, "action": "append"},
                    ]
                },
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


def test_duplicate_schedule_agents_deduped():
    """同一智能体以别名 / 重复条目出现时只执行一次（避免重复调用与双份结果）。"""
    plan = _StubAgent("plan-trip", {"itinerary": {"title": "杭州行程"}})
    orchestrator = OrchestrationAgent(
        agent_registry={"plan-trip": plan, "itinerary_planning": plan},
    )
    schedule = [
        {"agent_name": "plan-trip", "priority": 1},
        {"agent_name": "itinerary_planning", "priority": 1},
        {"agent_name": "plan-trip", "priority": 2},
    ]
    result = json.loads(asyncio.run(orchestrator.reply(_intent(schedule))).content)
    assert plan.calls == 1, f"同一智能体应只执行一次，实际 {plan.calls} 次"
    assert [item["agent_name"] for item in result["results"]] == [
        "itinerary_planning"
    ]
    print("✓ 调度计划按 canonical 名去重")


def test_failed_preference_not_merged_into_later_context():
    """status=error 的偏好结果不能进入后续批次上下文（与落库守卫一致）。"""
    captured: dict = {}

    class Capture(_StubAgent):
        async def reply(self, x):
            captured["payload"] = json.loads(x.content)
            return Msg(name=self.name, content="{}", role="assistant")

    class FailingPreference(_StubAgent):
        async def reply(self, x):
            return Msg(
                name=self.name,
                content=json.dumps(
                    {
                        "status": "error",
                        "message": "boomed",
                        "data": {
                            "preferences": [
                                {"type": "hotel_brands", "value": ["错误品牌"]}
                            ]
                        },
                    },
                    ensure_ascii=False,
                ),
                role="assistant",
            )

    orchestrator = OrchestrationAgent(
        agent_registry={
            "preference": FailingPreference("preference"),
            "a": Capture("a"),
        },
    )
    asyncio.run(
        orchestrator.reply(
            _intent(
                [
                    {"agent_name": "preference", "priority": 1},
                    {"agent_name": "a", "priority": 2},
                ]
            )
        )
    )
    preferences = captured["payload"]["context"]["user_preferences"]
    assert "hotel_brands" not in preferences, "失败偏好的残缺值不能进入上下文"
    print("✓ 失败偏好不进入后续批次上下文")


def test_preference_non_string_type_skipped():
    """模型把偏好 type 包成数组 / 对象 / 空白串：跳过不落库，不抛 TypeError。"""
    memory = _StubMemory()
    OrchestrationAgent(agent_registry={}, memory_manager=memory)._update_memory(
        {},
        [
            {
                "agent_name": "preference",
                "priority": 1,
                "result": {
                    "status": "success",
                    "data": {
                        "preferences": [
                            {"type": ["hotel_brands"], "value": "汉庭"},
                            {"type": {"key": "airlines"}, "value": "东航"},
                            {"type": "   ", "value": "如家"},
                            {"type": "airlines", "value": "国航"},
                        ]
                    },
                },
            }
        ],
    )
    assert memory.long_term.get_preference() == {"airlines": "国航"}
    print("✓ 非字符串偏好 type 不落库")


def test_context_merge_skips_non_string_pref_type():
    """上下文合并路径同样跳过非字符串 type（不可哈希值会抛 TypeError）。"""
    orchestrator = OrchestrationAgent(agent_registry={})
    context = orchestrator._context_with_previous_preferences(
        {},
        [
            {
                "agent_name": "preference",
                "result": {
                    "status": "success",
                    "data": {
                        "preferences": [
                            {"type": ["hotel_brands"], "value": "汉庭"},
                            {"type": " airlines ", "value": "国航"},
                        ]
                    },
                },
            }
        ],
    )
    assert context["user_preferences"] == {"airlines": "国航"}, context
    print("✓ 上下文合并跳过非字符串 type 并 strip")


def test_trip_without_start_date_not_saved():
    """无出发日期的行程无法参与去重，不落库（复述 / 重新生成不再重复累加）。"""
    memory = _StubMemory()
    OrchestrationAgent(agent_registry={}, memory_manager=memory)._update_memory(
        {},
        [
            {
                "agent_name": "event_collection",
                "priority": 1,
                "result": {
                    "status": "success",
                    "data": {"destination": "杭州", "start_date": None},
                },
            },
            {
                "agent_name": "itinerary_planning",
                "priority": 2,
                "result": {
                    "status": "success",
                    "data": {"itinerary": {"title": "杭州行程", "daily_plans": []}},
                },
            },
        ],
    )
    assert memory.long_term.get_trip_history(limit=5) == []
    print("✓ 无出发日期的行程不落库")


def test_trip_destination_dict_uses_scalar_field():
    """对象型目的地取标量字段（{"city": "杭州"} → 杭州），不用 Python repr。"""
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
                        "destination": {"city": "杭州"},
                        "start_date": "2026-12-01",
                    },
                },
            },
            {
                "agent_name": "itinerary_planning",
                "priority": 2,
                "result": {
                    "status": "success",
                    "data": {"itinerary": {"title": "杭州行程", "daily_plans": []}},
                },
            },
        ],
    )
    trips = memory.long_term.get_trip_history(limit=5)
    assert trips and trips[-1]["destination"] == "杭州", trips
    assert "{" not in trips[-1]["destination"]
    print("✓ 对象型目的地取标量字段落库")


def test_reply_input_edges_and_registry_management():
    """reply(None) / 非法意图输出 / 空调度容错；注册与注销；文本辅助函数。"""
    from agents.orchestration_agent import _as_text, _transient_agent_error

    orchestrator = OrchestrationAgent(agent_registry={})

    assert json.loads(asyncio.run(orchestrator.reply(None)).content) == {
        "error": "No input provided"
    }

    for bad in ("这不是 JSON", json.dumps([1, 2])):
        out = asyncio.run(orchestrator.reply(Msg(name="i", content=bad, role="user")))
        assert json.loads(out.content) == {"error": "Invalid intention format"}, bad

    # 列表输入取最后一条；空调度返回空结果
    out = asyncio.run(
        orchestrator.reply(
            [
                Msg(name="i", content="{}", role="user"),
                Msg(
                    name="i",
                    content=json.dumps({"agent_schedule": []}),
                    role="user",
                ),
            ]
        )
    )
    data = json.loads(out.content)
    assert data.get("status") == "no_agents", data

    # 调度项写成字符串 / null：归一为空调度；写成单个对象：按 1 项调度
    for schedule in ("itinerary_planning", None):
        out = asyncio.run(
            orchestrator.reply(
                Msg(
                    name="i",
                    content=json.dumps({"agent_schedule": schedule}),
                    role="user",
                )
            )
        )
        assert json.loads(out.content).get("status") == "no_agents", schedule
    out = asyncio.run(
        orchestrator.reply(
            Msg(
                name="i",
                content=json.dumps(
                    {"agent_schedule": {"agent_name": "x", "priority": 1}}
                ),
                role="user",
            )
        )
    )
    data = json.loads(out.content)
    assert data.get("status") != "no_agents", data
    assert "x" in out.content, data

    class _PlainAgent:
        async def reply(self, x):
            return Msg(name="a", content="{}", role="assistant")

    agent = _PlainAgent()
    orchestrator.register_agent("demo", agent)
    assert orchestrator.agent_registry["demo"] is agent
    orchestrator.unregister_agent("demo")
    assert "demo" not in orchestrator.agent_registry
    orchestrator.unregister_agent("missing")  # 注销不存在的智能体不抛异常

    # 智能体返回非 JSON 文本：data 里保留原文（不丢回答）
    class _TextAgent:
        async def reply(self, x):
            return Msg(name="t", content="直接文本回答", role="assistant")

    orchestrator.register_agent("demo2", _TextAgent())
    out = asyncio.run(
        orchestrator.reply(
            Msg(
                name="i",
                content=json.dumps(
                    {"agent_schedule": [{"agent_name": "demo2", "priority": 1}]}
                ),
                role="user",
            )
        )
    )
    assert "直接文本回答" in out.content

    # 智能体返回非字符串 content：原样作为 data
    class _RawAgent:
        async def reply(self, x):
            return type("R", (), {"content": {"raw": True}})()

    orchestrator.register_agent("demo3", _RawAgent())
    out = asyncio.run(
        orchestrator.reply(
            Msg(
                name="i",
                content=json.dumps(
                    {"agent_schedule": [{"agent_name": "demo3", "priority": 1}]}
                ),
                role="user",
            )
        )
    )
    assert '"raw": true' in out.content.replace("True", "true"), out.content

    # _as_text：没有标量键的字典回退 JSON 文本
    assert _as_text({"k": 1}) == json.dumps({"k": 1}, ensure_ascii=False)

    # _transient_agent_error：非字符串 / 非法 JSON / 非对象内容返回 None
    class _Resp:
        def __init__(self, content):
            self.content = content

    assert _transient_agent_error(_Resp(None)) is None
    assert _transient_agent_error(_Resp("不是 JSON")) is None
    assert _transient_agent_error(_Resp(json.dumps([1]))) is None
    print("✓ reply 输入边界 / 调度容错 / 注册注销 / 文本与瞬时错误辅助")


def test_dict_registry_resolves_name_variants():
    """纯 dict 注册表存标准名时，模型输出的别名 / 大小写变体也能调度。"""
    agent = _StubAgent("itinerary_planning", {"itinerary": {"title": "T"}})
    orchestrator = OrchestrationAgent(
        agent_registry={"itinerary_planning": agent}
    )
    result = asyncio.run(
        orchestrator._run_agent(
            agent_name="Itinerary_Planning",
            context={},
            reason="",
            expected_output="",
            previous_results=[],
        )
    )
    assert result["status"] == "success", result
    assert agent.calls == 1
    print("✓ dict 注册表按标准名解析别名 / 大小写变体")


def test_update_memory_normalizes_trip_fields_and_dates():
    """行程字段截断到 100 字；ISO 时间戳归一为日期，非法日期按空处理。"""
    memory = _StubMemory()
    results = [
        {
            "agent_name": "itinerary_planning",
            "status": "success",
            "result": {"status": "success", "data": {"itinerary": {"title": "T"}}},
        },
        {
            "agent_name": "event_collection",
            "status": "success",
            "result": {
                "status": "success",
                "data": {
                    "origin": "北" * 300,
                    "destination": "杭州",
                    "start_date": "2026-03-11T00:00:00",
                    "end_date": "2026/03/17",
                    "trip_purpose": "出差",
                },
            },
        },
    ]
    OrchestrationAgent(
        agent_registry={}, memory_manager=memory
    )._update_memory({}, results)
    trips = memory.long_term.get_trip_history()
    assert len(trips) == 1, trips
    trip = trips[0]
    assert trip["destination"] == "杭州"
    assert len(trip["origin"]) == 100, len(trip["origin"])
    assert trip["start_date"] == "2026-03-11"
    assert trip["end_date"] == "", "无法解析的结束日期按空处理"
    print("✓ 行程字段截断与日期归一")


def test_update_memory_rejects_unparseable_start_date():
    """出发日期无法解析时整条跳过，避免落库后被统计 / 日历静默忽略。"""
    memory = _StubMemory()
    results = [
        {
            "agent_name": "itinerary_planning",
            "status": "success",
            "result": {"status": "success", "data": {"itinerary": {"title": "T"}}},
        },
        {
            "agent_name": "event_collection",
            "status": "success",
            "result": {
                "status": "success",
                "data": {"destination": "杭州", "start_date": "下周一"},
            },
        },
    ]
    OrchestrationAgent(
        agent_registry={}, memory_manager=memory
    )._update_memory({}, results)
    assert memory.long_term.get_trip_history() == []
    print("✓ 无法解析的出发日期整条跳过保存")


def test_preference_values_not_logged(caplog):
    """偏好值可能是证件号等隐私内容：INFO 日志只记录类型。"""
    import logging

    memory = _StubMemory()
    results = [
        {
            "agent_name": "preference",
            "status": "success",
            "result": {
                "status": "success",
                "data": {
                    "preferences": [
                        {
                            "type": "passport",
                            "value": "E12345678",
                            "action": "append",
                        }
                    ]
                },
            },
        },
    ]
    with caplog.at_level(logging.INFO):
        OrchestrationAgent(
            agent_registry={}, memory_manager=memory
        )._update_memory({}, results)
    assert "E12345678" not in caplog.text
    print("✓ 偏好值不写入 INFO 日志")


def _isolated_results():
    return [
        {
            "agent_name": "preference",
            "status": "success",
            "result": {
                "status": "success",
                "data": {
                    "preferences": [
                        {"type": "home_location", "value": "上海", "action": "append"},
                        {"type": "airlines", "value": "东航", "action": "append"},
                        {"type": "hotel_brands", "value": "汉庭", "action": "append"},
                    ]
                },
            },
        },
        {
            "agent_name": "itinerary_planning",
            "status": "success",
            "result": {"status": "success", "data": {"itinerary": {"title": "T"}}},
        },
        {
            "agent_name": "event_collection",
            "status": "success",
            "result": {
                "status": "success",
                "data": {
                    "destination": "杭州",
                    "start_date": "2026-12-01",
                    "end_date": "2026-12-02",
                },
            },
        },
    ]


def test_update_memory_isolates_preference_failures(caplog=None):
    """单条偏好写入失败只跳过该条，其余偏好与同一轮行程照常落库。"""
    import logging

    from utils.errors import MemoryPersistenceError

    class _FailingPrefs:
        def __init__(self, inner):
            self.inner = inner

        def save_preference(self, pref_type, value):
            if pref_type == "airlines":
                raise MemoryPersistenceError("disk full")
            return self.inner.save_preference(pref_type, value)

        def append_preference(self, pref_type, value):
            if pref_type == "airlines":
                raise MemoryPersistenceError("disk full")
            return self.inner.append_preference(pref_type, value)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    memory = _StubMemory()
    memory.long_term = _FailingPrefs(memory.long_term)
    if caplog is not None:
        with caplog.at_level(logging.WARNING):
            OrchestrationAgent(
                agent_registry={}, memory_manager=memory
            )._update_memory({}, _isolated_results())
        assert "保存偏好失败" in caplog.text
    else:
        OrchestrationAgent(
            agent_registry={}, memory_manager=memory
        )._update_memory({}, _isolated_results())

    prefs = memory.long_term.inner.get_preference()
    assert prefs.get("home_location") == ["上海"]
    assert prefs.get("hotel_brands") == ["汉庭"]
    assert "airlines" not in prefs
    assert len(memory.long_term.inner.get_trip_history()) == 1
    print("✓ 单条偏好失败不影响其余偏好与行程落库")


def test_update_memory_skips_failed_event_collection():
    """被判失败的事项抽取不能作为行程信息来源落库。"""
    memory = _StubMemory()
    results = [
        {
            "agent_name": "itinerary_planning",
            "status": "success",
            "result": {"status": "success", "data": {"itinerary": {"title": "T"}}},
        },
        {
            "agent_name": "event_collection",
            "status": "error",
            "result": {
                "status": "error",
                "data": {"destination": "杭州", "start_date": "2026-12-01"},
            },
        },
    ]
    OrchestrationAgent(
        agent_registry={}, memory_manager=memory
    )._update_memory({}, results)
    assert memory.long_term.get_trip_history() == []
    print("✓ 失败的事项收集不写入行程")


def test_update_memory_isolates_trip_save_failure(caplog=None):
    """行程写入失败：记录告警，同轮偏好照常落库（不中断整轮）。"""
    import logging

    from utils.errors import MemoryPersistenceError

    class _FailingTrips:
        def __init__(self, inner):
            self.inner = inner

        def save_trip_history(self, trip):
            raise MemoryPersistenceError("disk full")

        def __getattr__(self, name):
            return getattr(self.inner, name)

    memory = _StubMemory()
    memory.long_term = _FailingTrips(memory.long_term)
    orchestrator = OrchestrationAgent(
        agent_registry={}, memory_manager=memory
    )
    if caplog is not None:
        with caplog.at_level(logging.WARNING):
            orchestrator._update_memory({}, _isolated_results())
        assert "保存行程失败" in caplog.text
    else:
        orchestrator._update_memory({}, _isolated_results())
    prefs = memory.long_term.get_preference()
    assert prefs.get("airlines") == ["东航"], "行程失败不能中断同轮偏好写入"
    assert prefs.get("hotel_brands") == ["汉庭"], prefs
    print("✓ 行程写入失败不中断整轮（偏好仍落库 + 记录告警）")


def test_update_memory_derives_end_date_from_duration():
    """只给出 duration_days 时按天数补出返程日期；非法时长不补。"""
    memory = _StubMemory()
    results = [
        {
            "agent_name": "itinerary_planning",
            "status": "success",
            "result": {"status": "success", "data": {"itinerary": {"title": "T"}}},
        },
        {
            "agent_name": "event_collection",
            "status": "success",
            "result": {
                "status": "success",
                "data": {
                    "destination": "杭州",
                    "start_date": "2026-03-11",
                    "duration_days": 7,
                },
            },
        },
    ]
    OrchestrationAgent(
        agent_registry={}, memory_manager=memory
    )._update_memory({}, results)
    trips = memory.long_term.get_trip_history()
    assert len(trips) == 1, trips
    assert trips[0]["end_date"] == "2026-03-17", trips[0]

    invalid = _StubMemory()
    OrchestrationAgent(
        agent_registry={}, memory_manager=invalid
    )._update_memory(
        {},
        [
            results[0],
            {
                "agent_name": "event_collection",
                "status": "success",
                "result": {
                    "status": "success",
                    "data": {
                        "destination": "杭州",
                        "start_date": "2026-03-11",
                        "duration_days": 0,
                    },
                },
            },
        ],
    )
    assert invalid.long_term.get_trip_history()[0]["end_date"] == ""
    print("✓ duration_days 补出返程日期（非法时长不补）")


def _trip_orchestrator(memory):
    return OrchestrationAgent(agent_registry={}, memory_manager=memory)


def _trip_results(event: dict) -> list[dict]:
    return [
        {
            "agent_name": "itinerary_planning",
            "status": "success",
            "result": {"status": "success", "data": {"itinerary": {"title": "T"}}},
        },
        {
            "agent_name": "event_collection",
            "status": "success",
            "result": {"status": "success", "data": event},
        },
    ]


def test_trip_save_does_not_overwrite_purpose_with_default():
    """未抽取到 trip_purpose 时不能把默认「旅游」写回既有行程。"""
    memory = _StubMemory()
    orchestrator = _trip_orchestrator(memory)
    base = {
        "origin": "北京",
        "destination": "杭州",
        "start_date": "2026-03-11",
        "end_date": "2026-03-18",
    }
    orchestrator._update_memory({}, _trip_results({**base, "trip_purpose": "出差"}))
    orchestrator._update_memory({}, _trip_results(base))
    trips = memory.long_term.get_trip_history()
    assert len(trips) == 1, trips
    assert trips[0]["purpose"] == "出差", "未提交的目的不能被模板默认值覆盖"
    print("✓ 行程目的缺失时不覆盖既有值")


def test_preference_single_object_is_wrapped():
    """preferences 返回单个 {type,value,action} 对象时按单条偏好处理。"""
    memory = _StubMemory()
    orchestrator = _trip_orchestrator(memory)
    orchestrator._update_memory(
        {},
        [
            {
                "agent_name": "preference",
                "status": "success",
                "result": {
                    "status": "success",
                    "data": {
                        "preferences": {
                            "type": "hotel_brands",
                            "value": "汉庭",
                            "action": "append",
                        }
                    },
                },
            }
        ],
    )
    prefs = memory.long_term.get_preference()
    assert prefs.get("hotel_brands") == ["汉庭"], prefs
    assert "type" not in prefs and "value" not in prefs and "action" not in prefs
    print("✓ 单对象偏好按单条落库（不写垃圾键）")


def test_trip_origin_backfilled_from_existing_trip():
    """后续轮次缺出发地时回填既有同趟行程的 origin，避免拆成两条。"""
    memory = _StubMemory()
    orchestrator = _trip_orchestrator(memory)
    orchestrator._update_memory(
        {},
        _trip_results(
            {
                "origin": "北京",
                "destination": "杭州",
                "start_date": "2026-03-11",
                "end_date": "2026-03-18",
            }
        ),
    )
    orchestrator._update_memory(
        {},
        _trip_results(
            {
                "destination": "杭州",
                "start_date": "2026-03-11",
                "end_date": "2026-03-18",
            }
        ),
    )
    trips = memory.long_term.get_trip_history()
    assert len(trips) == 1, trips
    assert trips[0]["origin"] == "北京"
    print("✓ 缺出发地时回填既有行程 origin")


def test_trip_duration_overflow_does_not_abort_turn():
    """9999-12-31 + 长时长推导返程越界：不抛异常，按单日落库。"""
    memory = _StubMemory()
    orchestrator = _trip_orchestrator(memory)
    orchestrator._update_memory(
        {},
        _trip_results(
            {
                "destination": "杭州",
                "start_date": "9999-12-31",
                "duration_days": 7,
            }
        ),
    )
    trips = memory.long_term.get_trip_history()
    assert len(trips) == 1, trips
    assert (trips[0]["end_date"] or trips[0]["start_date"]) == "9999-12-31"
    print("✓ 返程日期推导越界时按单日落库")


def test_trip_saved_from_intention_entities_when_event_empty():
    """本轮无事项抽取结果时用意图 key_entities 兜底保存行程。"""
    memory = _StubMemory()
    orchestrator = _trip_orchestrator(memory)
    results = [
        {
            "agent_name": "itinerary_planning",
            "status": "success",
            "result": {"status": "success", "data": {"itinerary": {"title": "T"}}},
        }
    ]
    orchestrator._update_memory(
        {
            "key_entities": {
                "origin": "北京",
                "destination": "杭州",
                "start_date": "2026-03-11",
            }
        },
        results,
    )
    trips = memory.long_term.get_trip_history()
    assert len(trips) == 1, trips
    assert trips[0]["destination"] == "杭州"
    print("✓ 事项抽取缺失时回退意图实体落库")
def test_agent_failure_message_redacts_internal_paths():
    """智能体异常文本会渲染进回答并落库：本机路径 / 上游 URL 必须遮蔽。"""
    memory = _StubMemory()

    class _Boom(_StubAgent):
        async def reply(self, x):
            raise RuntimeError(
                "无法加载 C:\\Users\\alice\\.cache\\huggingface\\model"
                " from https://huggingface.co/model"
            )

    orchestrator = OrchestrationAgent(
        agent_registry={"query": _Boom("query")}, memory_manager=memory
    )
    content = asyncio.run(
        orchestrator.reply(_intent([{"agent_name": "query", "priority": 1}]))
    ).content
    result = json.loads(content)
    entry = result["results"][0]
    assert entry["status"] == "error"
    assert "<路径>" in entry["message"], entry
    assert "C:\\Users" not in entry["message"], entry
    assert "huggingface.co" not in entry["data"]["error"], entry
    print("✓ 智能体错误信息遮蔽本机路径 / 上游 URL")


if __name__ == "__main__":
    test_priority_parallel_and_sequential()
    test_previous_results_passed_to_next_priority()
    test_failure_aggregated_as_partial_failure()
    test_agent_duration_and_progress_events()
    test_unregistered_agent_reports_error()
    test_agent_status_error_and_null_error()
    test_nested_query_failure_is_reported_as_error()
    test_transient_nested_error_triggers_retry()
    test_malformed_schedule_is_sanitized()
    test_registry_load_failure_reports_agent_error()
    test_failed_itinerary_is_not_saved_as_trip()
    test_skill_folder_names_are_canonicalized()
    test_skill_names_resolve_case_insensitively()
    test_legacy_name_variants_dedupe_and_planning_floor()
    test_preference_name_variants_persist_and_dedupe()
    test_agent_result_surrogates_sanitized_before_persist()
    test_preference_payload_with_extra_keys_does_not_write_garbage()
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
    test_duplicate_schedule_agents_deduped()
    test_failed_preference_not_merged_into_later_context()
    test_preference_non_string_type_skipped()
    test_context_merge_skips_non_string_pref_type()
    test_trip_without_start_date_not_saved()
    test_trip_destination_dict_uses_scalar_field()
    test_data_data_nested_preferences_unpacked()
    test_unmanageable_preference_types_skipped_everywhere()
    test_reply_input_edges_and_registry_management()
    test_agent_load_runs_in_dedicated_executor()
    test_rag_load_uses_isolated_executor()
    test_rag_load_executor_accepts_standard_name()
    test_update_memory_isolates_preference_failures()
    test_update_memory_skips_failed_event_collection()
    test_update_memory_isolates_trip_save_failure()
    test_update_memory_derives_end_date_from_duration()
    test_trip_save_does_not_overwrite_purpose_with_default()
    test_preference_single_object_is_wrapped()
    test_trip_origin_backfilled_from_existing_trip()
    test_trip_duration_overflow_does_not_abort_turn()
    test_trip_saved_from_intention_entities_when_event_empty()
    test_model_output_nul_stripped_before_memory()
    test_agent_failure_message_redacts_internal_paths()
    _mp_load = pytest.MonkeyPatch()
    try:
        test_agent_load_timeout_is_bounded(_mp_load)
    finally:
        _mp_load.undo()
    print("\n全部通过")
