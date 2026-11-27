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
import hashlib
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


def test_checkpoint_reuses_successful_agents_across_instances(tmp_path):
    """持久化检查点：重启后的新编排器复用已成功智能体，只重跑失败者。"""
    from utils.execution_checkpoint import CheckpointStore

    store = CheckpointStore(tmp_path, ttl_sec=3600)
    first_a = _StubAgent("a", {"ok": True})
    first_b = _StubAgent("b", raise_error=True)
    first = OrchestrationAgent(
        agent_registry={"a": first_a, "b": first_b}
    )
    first._checkpoint_store = store
    schedule = [
        {"agent_name": "a", "priority": 1},
        {"agent_name": "b", "priority": 1},
    ]
    try:
        asyncio.run(
            first.reply(_intent(schedule), checkpoint_key="u:s:t1")
        )
    except Exception:  # noqa: BLE001 - 失败本身不是本用例重点
        pass
    assert first_a.calls == 1
    state = store.load("u:s:t1")
    assert state and state["steps"]["a"]["status"] == "success"
    assert "b" not in state["steps"], "失败智能体不能写入检查点"

    second_a = _StubAgent("a", {"ok": True})
    second_b = _StubAgent("b", {"ok": True})
    second = OrchestrationAgent(
        agent_registry={"a": second_a, "b": second_b}
    )
    second._checkpoint_store = store
    asyncio.run(second.reply(_intent(schedule), checkpoint_key="u:s:t1"))
    assert second_a.calls == 0, "已成功的智能体不应重复执行"
    assert second_b.calls == 1, "失败 / 未完成的智能体应重新执行"
    state = store.load("u:s:t1")
    assert set(state["steps"]) == {"a", "b"}
    print("✓ 执行检查点跨编排器实例复用成功步骤")


def test_clear_checkpoint_removes_state_after_success(tmp_path):
    """成功落库后清理检查点：同一键再次请求不能复用上一问的子结果。"""
    from utils.execution_checkpoint import CheckpointStore

    store = CheckpointStore(tmp_path, ttl_sec=3600)
    agent = _StubAgent("a", {"ok": True})
    orchestrator = OrchestrationAgent(agent_registry={"a": agent})
    orchestrator._checkpoint_store = store
    schedule = [{"agent_name": "a", "priority": 1}]
    asyncio.run(orchestrator.reply(_intent(schedule), checkpoint_key="u:s:t1"))
    assert store.load("u:s:t1") is not None
    orchestrator.clear_checkpoint()
    assert store.load("u:s:t1") is None, "整轮成功后再复用会返回陈旧结果"
    assert orchestrator._checkpoint_key is None
    print("✓ 整轮成功后清理执行检查点")


def test_checkpoint_scope_hash_recorded_for_session_cleanup(tmp_path):
    """检查点记录会话作用域哈希，会话删除可按作用域定向清理。"""
    from utils.execution_checkpoint import CheckpointStore

    store = CheckpointStore(tmp_path, ttl_sec=3600)
    agent = _StubAgent("a", {"ok": True})
    orchestrator = OrchestrationAgent(agent_registry={"a": agent})
    orchestrator._checkpoint_store = store
    schedule = [{"agent_name": "a", "priority": 1}]
    asyncio.run(
        orchestrator.reply(
            _intent(schedule),
            checkpoint_key="u:s:t1:abc",
            checkpoint_scope="u:s",
        )
    )
    state = store.load("u:s:t1:abc")
    assert state is not None
    assert state["scope_hash"] == hashlib.sha256(b"u:s").hexdigest()
    assert store.clear_scope("u:s") == 1
    assert store.load("u:s:t1:abc") is None
    print("✓ 检查点作用域哈希与会话清理")


def test_checkpoint_store_init_is_single_flight(monkeypatch):
    """首个回合并行批次：并发初始化必须收敛到同一实例（否则 update_step 丢写）。"""
    import utils.execution_checkpoint as checkpoint_module

    created: list[object] = []

    class _Store:
        def __init__(self, *args, **kwargs):
            created.append(self)

    monkeypatch.setattr(checkpoint_module, "CheckpointStore", _Store)

    async def scenario():
        agent = OrchestrationAgent(agent_registry={})
        agent._checkpoint_key = "u:s:t"
        return await asyncio.gather(
            agent._checkpoint_store_for_turn(),
            agent._checkpoint_store_for_turn(),
        )

    stores = asyncio.run(scenario())
    assert stores[0] is not None
    assert stores[0] is stores[1], "并发初始化必须共享同一 CheckpointStore"
    assert len(created) == 1
    print("✓ 检查点存储单飞构造（并发共享同一实例）")


def test_checkpoint_reuse_emits_progress_events(tmp_path):
    """复用的已完成步骤也要补发 start/done，新流执行路线不能一直 pending。"""
    import hashlib

    from utils.execution_checkpoint import CheckpointStore

    store = CheckpointStore(tmp_path, ttl_sec=3600)
    schedule = [
        {
            "agent_name": "a",
            "priority": 1,
            "reason": "r",
            "expected_output": "e",
        }
    ]
    digest = hashlib.sha256(b"a|1|r|e").hexdigest()
    store.update_step(
        "u:s:reuse",
        "a",
        {"status": "success", "data": {"ok": True}, "duration": 0.2},
        task_digest=digest,
    )
    stub = _StubAgent("a", {"ok": True})
    orch = OrchestrationAgent(agent_registry={"a": stub})
    orch._checkpoint_store = store
    events: list[dict] = []
    asyncio.run(
        orch.reply(_intent(schedule), on_event=events.append, checkpoint_key="u:s:reuse")
    )
    assert stub.calls == 0, "同计划的已成功步骤应复用而不是重新执行"
    pairs = {(event.get("type"), event.get("agent_name")) for event in events}
    assert ("agent_start", "a") in pairs, events
    assert ("agent_done", "a") in pairs, events
    print("✓ 检查点复用的步骤补发进度事件")


def test_memory_write_failures_are_tracked():
    """超限偏好被跳过时登记类型，供调用方在回复中提示写入失败。"""
    memory = _StubMemory()
    orch = OrchestrationAgent(agent_registry={}, memory_manager=memory)
    results = [
        {
            "agent_name": "preference",
            "priority": 1,
            "result": {
                "status": "success",
                "data": {
                    "preferences": [
                        {"type": "hotel_brands", "value": "x" * 12000, "action": "replace"}
                    ]
                },
            },
        }
    ]
    orch._update_memory({"key_entities": {}}, results)
    assert "hotel_brands" in orch._last_memory_write_failures
    assert "hotel_brands" not in memory.long_term.get_preference()
    print("✓ 偏好写入失败按类型登记")


def test_checkpoint_tombstone_probe_only_for_successful_results(tmp_path):
    """墓碑复查只服务检查点写入：失败结果不做无用的 Redis 查询。"""
    from utils.execution_checkpoint import CheckpointStore

    store = CheckpointStore(tmp_path, ttl_sec=3600)
    schedule = [{"agent_name": "a", "priority": 1}]
    probes = {"failed": 0, "success": 0}

    def make_probe(bucket):
        def _probe():
            probes[bucket] += 1
            return False

        return _probe

    failing = OrchestrationAgent(
        agent_registry={"a": _StubAgent("a", raise_error=True)}
    )
    failing._checkpoint_store = store
    failing._session_deleted_now = make_probe("failed")  # type: ignore[method-assign]
    try:
        asyncio.run(failing.reply(_intent(schedule), checkpoint_key="u:s:f"))
    except Exception:  # noqa: BLE001 - 失败本身不是本用例重点
        pass
    assert probes["failed"] == 0, "失败结果不写检查点，不应执行墓碑复查"

    ok = OrchestrationAgent(agent_registry={"a": _StubAgent("a", {"ok": True})})
    ok._checkpoint_store = store
    ok._session_deleted_now = make_probe("success")  # type: ignore[method-assign]
    asyncio.run(ok.reply(_intent(schedule), checkpoint_key="u:s:ok"))
    # 写入前复查一次 + 落盘后再复查一次（防跨实例删除落在两步之间）
    assert probes["success"] == 2, "成功结果写入前与落盘后都必须复查墓碑"
    print("✓ 墓碑复查只在该轮写入检查点（成功）时执行")


def test_checkpoint_cleared_when_session_deleted_between_probe_and_write(tmp_path):
    """墓碑复查与 update_step 之间的跨实例删除：落盘后补删，不留到 TTL。"""
    from utils.execution_checkpoint import CheckpointStore

    store = CheckpointStore(tmp_path, ttl_sec=3600)
    schedule = [{"agent_name": "a", "priority": 1}]
    answers = iter([False, True])

    probe = OrchestrationAgent(agent_registry={"a": _StubAgent("a", {"ok": True})})
    probe._checkpoint_store = store
    probe._session_deleted_now = lambda: next(answers, True)  # type: ignore[method-assign]
    asyncio.run(probe.reply(_intent(schedule), checkpoint_key="u:s:gone"))
    assert store.load("u:s:gone") is None, "落盘后复查到删除必须补删检查点"
    print("✓ 跨实例删除落在复查与落盘之间时检查点被补删")


def test_checkpoint_store_failure_does_not_block_turn(monkeypatch):
    """检查点目录不可用（磁盘 / 权限）只记日志，对话照常完成。"""
    import utils.execution_checkpoint as checkpoint_module

    class _BrokenStore:
        def __init__(self, *args, **kwargs):
            raise OSError("disk unavailable")

    monkeypatch.setattr(checkpoint_module, "CheckpointStore", _BrokenStore)
    agent = _StubAgent("a", {"ok": True})
    orchestrator = OrchestrationAgent(agent_registry={"a": agent})
    schedule = [{"agent_name": "a", "priority": 1}]
    asyncio.run(orchestrator.reply(_intent(schedule), checkpoint_key="u:s:t1"))
    assert agent.calls == 1, "检查点初始化失败不能阻断智能体执行"
    assert orchestrator._checkpoint_store is None
    print("✓ 检查点存储初始化失败不阻断对话")
def test_dag_dependency_overrides_llm_priority():
    """行程规划即使被 LLM 标成更高优先级，也必须排在事项收集之后。"""
    order: list[str] = []

    class Recorder(_StubAgent):
        async def reply(self, x):
            order.append(f"start:{self.name}")
            await asyncio.sleep(0.03)
            order.append(f"end:{self.name}")
            return Msg(name=self.name, content="{}", role="assistant")

    orchestrator = OrchestrationAgent(
        agent_registry={
            "event_collection": Recorder("event_collection"),
            "itinerary_planning": Recorder("itinerary_planning"),
        }
    )
    schedule = [
        {"agent_name": "itinerary_planning", "priority": 1},
        {"agent_name": "event_collection", "priority": 3},
    ]
    asyncio.run(orchestrator.reply(_intent(schedule)))
    assert order.index("start:itinerary_planning") > order.index(
        "end:event_collection"
    ), order
    print("✓ DAG 依赖优先于 LLM 计划优先级")


def test_extension_skills_follow_dag_dependencies():
    """预算 / 市内交通排在行程之后；行前类排在事项收集之后。"""
    order: list[str] = []

    class Recorder(_StubAgent):
        async def reply(self, x):
            order.append(f"start:{self.name}")
            await asyncio.sleep(0.03)
            order.append(f"end:{self.name}")
            return Msg(name=self.name, content="{}", role="assistant")

    orchestrator = OrchestrationAgent(
        agent_registry={
            "event_collection": Recorder("event_collection"),
            "itinerary_planning": Recorder("itinerary_planning"),
            "trip-budget": Recorder("trip-budget"),
            "packing-list": Recorder("packing-list"),
        }
    )
    schedule = [
        {"agent_name": "trip-budget", "priority": 1},
        {"agent_name": "itinerary_planning", "priority": 1},
        {"agent_name": "event_collection", "priority": 1},
        {"agent_name": "packing-list", "priority": 1},
    ]
    asyncio.run(orchestrator.reply(_intent(schedule)))
    assert order.index("start:packing-list") > order.index(
        "end:event_collection"
    ), order
    assert order.index("start:itinerary_planning") > order.index(
        "end:event_collection"
    ), order
    assert order.index("start:trip-budget") > order.index(
        "end:itinerary_planning"
    ), order
    print("✓ 扩展技能按 DAG 依赖排批")


def test_aggregate_results_surfaces_missing_info():
    """事项收集的缺失信息随聚合结果返回，并生成补充提示。"""
    orchestrator = OrchestrationAgent(agent_registry={})
    results = [
        {
            "agent_name": "event_collection",
            "priority": 1,
            "result": {
                "status": "success",
                "data": {
                    "destination": "杭州",
                    "missing_info": ["返程日期", {"field": "预算"}],
                },
            },
        }
    ]
    aggregated = orchestrator._aggregate_results(results, {})
    assert aggregated["missing_info"] == ["返程日期", "预算"]
    assert "请补充" in aggregated["clarification"]
    print("✓ 聚合结果携带缺失信息澄清")


def test_aggregate_missing_info_string_and_failed_result():
    """字符串形缺失信息要保留；失败结果的兜底占位不能当缺失信息。"""
    orchestrator = OrchestrationAgent(agent_registry={})
    string_case = [
        {
            "agent_name": "event_collection",
            "priority": 1,
            "result": {
                "status": "success",
                "data": {"destination": "杭州", "missing_info": "返程日期"},
            },
        }
    ]
    aggregated = orchestrator._aggregate_results(string_case, {})
    assert aggregated["missing_info"] == ["返程日期"], aggregated

    failed_case = [
        {
            "agent_name": "event_collection",
            "priority": 1,
            "result": {
                "status": "error",
                "data": {"missing_info": ["所有信息"], "error": "解析失败"},
            },
        }
    ]
    aggregated = orchestrator._aggregate_results(failed_case, {})
    assert "missing_info" not in aggregated, aggregated
    assert aggregated["status"] == "partial_failure"
    print("✓ 聚合缺失信息兼容字符串且跳过失败结果")


def test_retry_cache_reuses_completed_agents():
    """整轮重试复用首轮已完成的智能体：只重跑失败的那个，不重复计费。"""
    calls = {"a": 0, "b": 0}

    class Flaky(_StubAgent):
        async def reply(self, x):
            calls[self.name] += 1
            if self.name == "b" and calls["b"] <= 2:
                raise ConnectionError("transient blip")
            return Msg(name=self.name, content='{"ok": true}', role="assistant")

    orchestrator = OrchestrationAgent(
        agent_registry={"a": Flaky("a"), "b": Flaky("b")}
    )
    schedule = [
        {"agent_name": "a", "priority": 1},
        {"agent_name": "b", "priority": 1},
    ]
    cache: dict = {}
    try:
        asyncio.run(orchestrator.reply(_intent(schedule), retry_cache=cache))
    except ConnectionError:
        pass
    else:
        raise AssertionError("首个可重试错误必须上抛给外层重试")
    assert calls == {"a": 1, "b": 2}, calls
    assert cache, "成功的智能体结果应进入重试缓存"

    result = asyncio.run(orchestrator.reply(_intent(schedule), retry_cache=cache))
    assert calls == {"a": 1, "b": 3}, f"重试只应重跑失败的智能体: {calls}"
    assert result is not None
    print("✓ 整轮重试复用已完成智能体，仅重跑失败者")


def test_retry_cache_reruns_downstream_when_upstream_result_changes():
    """上游由失败重跑为成功时，下游不能复用基于失败上游的旧结果。"""
    calls = {"event": 0, "plan": 0, "budget": 0}
    plan_inputs: list[dict] = []

    class Event(_StubAgent):
        async def reply(self, x):
            calls["event"] += 1
            if calls["event"] == 1:
                return Msg(
                    name="event-collection",
                    content=json.dumps(
                        {"status": "error", "error": "模型返回格式有误"},
                        ensure_ascii=False,
                    ),
                    role="assistant",
                )
            return Msg(
                name="event-collection",
                content=json.dumps(
                    {"destination": "杭州", "start_date": "2026-12-01"},
                    ensure_ascii=False,
                ),
                role="assistant",
            )

    class Plan(_StubAgent):
        async def reply(self, x):
            calls["plan"] += 1
            plan_inputs.append(json.loads(x.content))
            return Msg(
                name="plan-trip",
                content=json.dumps(
                    {"itinerary": {"title": "杭州行"}}, ensure_ascii=False
                ),
                role="assistant",
            )

    class Budget(_StubAgent):
        async def reply(self, x):
            calls["budget"] += 1
            if calls["budget"] <= 2:
                # 首轮 + 内部重试各一次后上抛，触发外层整轮回退
                raise ConnectionError("transient blip")
            return Msg(
                name="trip-budget",
                content=json.dumps(
                    {"budget_advice": "约 2500 元"}, ensure_ascii=False
                ),
                role="assistant",
            )

    class _Registry:
        def __init__(self):
            self.agents = {
                "event-collection": Event("event-collection"),
                "plan-trip": Plan("plan-trip"),
                "trip-budget": Budget("trip-budget"),
            }

        def __contains__(self, name):
            return name in self.agents

        def __getitem__(self, name):
            return self.agents[name]

    orchestrator = OrchestrationAgent(agent_registry=_Registry())
    schedule = [
        {"agent_name": "event-collection", "priority": 1},
        {"agent_name": "plan-trip", "priority": 2},
        {"agent_name": "trip-budget", "priority": 3},
    ]
    cache: dict = {}
    try:
        asyncio.run(orchestrator.reply(_intent(schedule), retry_cache=cache))
    except ConnectionError:
        pass
    else:
        raise AssertionError("瞬时错误必须上抛给外层重试")
    assert calls == {"event": 1, "plan": 1, "budget": 2}, calls

    asyncio.run(orchestrator.reply(_intent(schedule), retry_cache=cache))
    assert calls["event"] == 2, calls
    assert calls["plan"] == 2, "上游由失败变成功后计划必须重跑，不能命中旧缓存"
    event_entry = next(
        item
        for item in plan_inputs[1]["previous_results"]
        if item["agent_name"] == "event_collection"
    )
    status = (event_entry.get("result") or {}).get("status", event_entry.get("status"))
    assert status == "success", event_entry
    print("✓ 重试缓存绑定上游输入：上游结果变化后下游重跑")


def test_missing_info_non_string_items_dropped():
    """missing_info 里的 null / 数字条目不能被聚合 / 展示成 None / 123。"""
    orchestrator = OrchestrationAgent(agent_registry={})
    aggregated = orchestrator._aggregate_results(
        [
            {
                "agent_name": "event_collection",
                "priority": 1,
                "result": {
                    "status": "success",
                    "data": {
                        "destination": "杭州",
                        "missing_info": ["start_date", None, 123],
                    },
                },
            }
        ],
        {},
    )
    assert aggregated["missing_info"] == ["start_date"], aggregated
    assert "None" not in aggregated["clarification"], aggregated
    assert "123" not in aggregated["clarification"], aggregated
    print("✓ 聚合 missing_info 丢弃非字符串条目")


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


def test_structured_skill_error_json_is_redacted():
    """技能把异常吞进 error JSON：回答 / 落库 / 导出前必须脱敏。"""
    canary = "sk-LEAKCANARY0123456789"
    path = "C:\\Users\\alice\\private\\key.txt"
    orchestrator = OrchestrationAgent(
        agent_registry={
            "query_info": _StubAgent(
                "query_info",
                {
                    "query_success": False,
                    "results": {
                        "error": f"搜索失败: 401 {canary} {path}",
                        "message": f"读取 {path} 失败",
                    },
                },
            )
        }
    )
    schedule = [{"agent_name": "query_info", "priority": 1}]
    result = json.loads(asyncio.run(orchestrator.reply(_intent(schedule))).content)
    text = json.dumps(result, ensure_ascii=False)
    assert canary not in text, "API Key 不能进入回答 / 落库数据"
    assert "alice" not in text, "本机路径不能进入回答 / 落库数据"
    assert result["status"] == "partial_failure", result
    print("✓ 结构化技能错误 JSON 在聚合前脱敏")


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


def test_update_memory_batches_long_term_writes(monkeypatch):
    """一轮内的多处落库合并为一次重载 + 一次写盘（write_batch）。"""
    from context.long_term_memory import LongTermMemory

    memory = _StubMemory()
    long_term = memory.long_term
    saves: list[int] = []
    real_save = LongTermMemory._save

    def _counting(self):
        if self is long_term:
            saves.append(1)
        return real_save(self)

    monkeypatch.setattr(LongTermMemory, "_save", _counting)
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
                            {
                                "type": "hotel_brand",
                                "value": ["汉庭"],
                                "action": "replace",
                            },
                            {
                                "type": "airline",
                                "value": ["东航"],
                                "action": "replace",
                            },
                        ]
                    },
                },
            }
        ],
    )
    assert saves == [1], f"整轮落库应只写盘一次，实际 {len(saves)}"
    assert long_term.get_preference("hotel_brand") == ["汉庭"]
    assert long_term.get_preference("airline") == ["东航"]
    print("✓ 一轮多处落库合并为一次写盘")


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


def test_deferred_memory_only_written_after_commit():
    """真实编排器：defer_memory 只暂存，commit 后才写行程。"""
    memory = _StubMemory()

    class _EventAgent(_StubAgent):
        async def reply(self, x):
            return Msg(
                name=self.name,
                content=json.dumps(
                    {
                        "status": "success",
                        "destination": "杭州",
                        "start_date": "2026-03-11",
                    },
                    ensure_ascii=False,
                ),
                role="assistant",
            )

    class _PlanAgent(_StubAgent):
        async def reply(self, x):
            return Msg(
                name=self.name,
                content=json.dumps(
                    {"itinerary": {"title": "T"}}, ensure_ascii=False
                ),
                role="assistant",
            )

    orchestrator = OrchestrationAgent(
        agent_registry={
            "event_collection": _EventAgent("event_collection"),
            "itinerary_planning": _PlanAgent("itinerary_planning"),
        },
        memory_manager=memory,
    )
    schedule = [
        {"agent_name": "event_collection", "priority": 1},
        {"agent_name": "itinerary_planning", "priority": 1},
    ]
    asyncio.run(orchestrator.reply(_intent(schedule), defer_memory=True))
    assert memory.long_term.get_trip_history() == [], (
        "defer_memory=True 时不能立即写台账"
    )
    asyncio.run(orchestrator.commit_deferred_memory())
    trips = memory.long_term.get_trip_history()
    assert len(trips) == 1 and trips[0]["destination"] == "杭州", trips
    print("✓ 延迟记忆写入只在 commit 后落库（真实编排器）")


def test_deferred_memory_commit_respects_epoch():
    """暂存负载带纪元：迟到的上一轮回调不能提交新一轮的负载。"""
    memory = _StubMemory()

    class _EventAgent(_StubAgent):
        async def reply(self, x):
            return Msg(
                name=self.name,
                content=json.dumps(
                    {
                        "status": "success",
                        "destination": "杭州",
                        "start_date": "2026-03-11",
                    },
                    ensure_ascii=False,
                ),
                role="assistant",
            )

    class _PlanAgent(_StubAgent):
        async def reply(self, x):
            return Msg(
                name=self.name,
                content=json.dumps(
                    {"itinerary": {"title": "T"}}, ensure_ascii=False
                ),
                role="assistant",
            )

    orchestrator = OrchestrationAgent(
        agent_registry={
            "event_collection": _EventAgent("event_collection"),
            "itinerary_planning": _PlanAgent("itinerary_planning"),
        },
        memory_manager=memory,
    )
    schedule = [
        {"agent_name": "event_collection", "priority": 1},
        {"agent_name": "itinerary_planning", "priority": 1},
    ]
    asyncio.run(
        orchestrator.reply(_intent(schedule), defer_memory=True, defer_epoch=1)
    )
    # 迟到的上一轮（纪元 2）不能提交纪元 1 的负载
    assert asyncio.run(orchestrator.commit_deferred_memory(epoch=2)) is False
    assert memory.long_term.get_trip_history() == []
    assert asyncio.run(orchestrator.commit_deferred_memory(epoch=1)) is True
    trips = memory.long_term.get_trip_history()
    assert len(trips) == 1 and trips[0]["destination"] == "杭州", trips

    # 纪元不符时也不能丢弃新一轮的负载
    asyncio.run(
        orchestrator.reply(_intent(schedule), defer_memory=True, defer_epoch=3)
    )
    orchestrator.discard_deferred_memory(epoch=4)
    assert orchestrator._deferred_memory is not None, "纪元不符不能丢弃新一轮负载"
    orchestrator.discard_deferred_memory(epoch=3)
    assert orchestrator._deferred_memory is None
    print("✓ 延迟记忆提交 / 丢弃按负载纪元归属")


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
    assert is_manageable_pref_type("x" + chr(0x0A) + "fake") is False
    assert is_manageable_pref_type("x" + chr(0x00) + "y") is False
    assert is_manageable_pref_type("x" + chr(0x2028) + "y") is False

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


def test_transient_error_json_does_not_escape_for_breaker(monkeypatch):
    """技能持续上报第三方服务瞬时故障：重试耗尽后按错误结果返回，不上抛。

    此前识别到瞬时标记后重抛的是普通 ConnectionError，外层会把它当作
    LLM 调用失败上抛给 ConversationRuntime 并计入全局熔断——连续几个
    搜索 / 天气型回合就会让所有用户 503，即使模型完全健康。
    """
    from agents import orchestration_agent as module

    monkeypatch.setattr(module, "_AGENT_RETRY_BASE_DELAY", 0.01)
    monkeypatch.setattr(module, "_AGENT_RETRY_MAX_DELAY", 0.02)

    class _AlwaysDown:
        name = "down"

        def __init__(self):
            self.calls = 0

        async def reply(self, message):
            self.calls += 1
            return Msg(
                name="down",
                content=json.dumps(
                    {"error": "Connection refused (upstream search service)"},
                    ensure_ascii=False,
                ),
                role="assistant",
            )

    down = _AlwaysDown()
    orchestrator = OrchestrationAgent(agent_registry={"down": down})
    reply = asyncio.run(
        orchestrator.reply(_intent([{"agent_name": "down", "priority": 1}]))
    )
    assert down.calls == module._AGENT_RETRY_MAX + 1, down.calls
    result = json.loads(reply.content)
    assert result["status"] == "partial_failure", result
    text = json.dumps(result, ensure_ascii=False)
    assert "Connection refused" in text, "错误信息应保留给用户"
    print("✓ 技能上报的瞬时错误不进全局熔断（按错误结果返回）")


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


def test_agent_timeout_not_replayed_across_turns(monkeypatch):
    """超时只在本轮整轮回退重试内复用：下一轮（全新 retry_cache）必须重新执行。"""
    import agents.orchestration_agent as module

    class Slow(_StubAgent):
        async def reply(self, x):
            self.calls += 1
            await asyncio.sleep(0.5)
            return Msg(name=self.name, content='{"ok": true}', role="assistant")

    monkeypatch.setattr(module, "_AGENT_TIMEOUT_SEC", 0.05)
    agent = Slow("a", {})
    orchestrator = OrchestrationAgent(agent_registry={"a": agent})
    schedule = [{"agent_name": "a", "priority": 1}]

    # 同一轮整轮回退重试：同一个 retry_cache 对象 → 超时结果复用、不再跑
    shared: dict = {}
    first = json.loads(
        asyncio.run(orchestrator.reply(_intent(schedule), retry_cache=shared)).content
    )
    assert first["status"] == "partial_failure", first
    assert agent.calls == 1, agent.calls
    again = json.loads(
        asyncio.run(orchestrator.reply(_intent(schedule), retry_cache=shared)).content
    )
    assert again["status"] == "partial_failure"
    assert agent.calls == 1, "同一 retry_cache 内的重试应复用超时结果"

    # 下一轮：调用方新建空 dict → 必须重新执行（超时不能跨轮毒化）
    nxt = json.loads(
        asyncio.run(orchestrator.reply(_intent(schedule), retry_cache={})).content
    )
    assert nxt["status"] == "partial_failure"
    assert agent.calls == 2, "下一轮必须重新执行同键任务"
    print("✓ 智能体超时只在本轮重试内复用")


def test_outer_cancel_cancels_running_skill():
    """外层取消（用户停止 / 客户端断连）必须终止正在执行的技能任务。"""
    started = asyncio.Event()
    state = {"finished": False, "cancelled": False}

    class Waiting(_StubAgent):
        async def reply(self, x):
            started.set()
            try:
                await asyncio.sleep(30)
                state["finished"] = True
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise
            return Msg(name=self.name, content='{"ok": true}', role="assistant")

    orchestrator = OrchestrationAgent(agent_registry={"a": Waiting("a")})

    async def _run():
        task = asyncio.ensure_future(
            orchestrator.reply(_intent([{"agent_name": "a", "priority": 1}]))
        )
        await started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0.05)

    asyncio.run(_run())
    assert state["cancelled"] is True, state
    assert state["finished"] is False
    print("✓ 外层取消终止在途技能")


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


class _AliasAwareRegistry(dict):
    """模拟 LazyAgentRegistry：frontmatter 别名解析到技能目录名。"""

    def resolve_agent_name(self, agent_name):
        if str(agent_name).casefold() in {"旅行助手", "travel-helper"}:
            return "travel-helper"
        return None

    def __contains__(self, key):
        resolved = self.resolve_agent_name(key)
        return dict.__contains__(self, resolved or key)

    def __getitem__(self, key):
        resolved = self.resolve_agent_name(key)
        return dict.__getitem__(self, resolved or key)


def test_frontmatter_alias_dedupes_with_directory_name():
    """frontmatter 别名与目录名指向同一技能时只执行一次。"""
    agent = _StubAgent("travel-helper", {"itinerary": {"title": "x"}})
    registry = _AliasAwareRegistry({"travel-helper": agent})
    orchestrator = OrchestrationAgent(agent_registry=registry)
    schedule = [
        {"agent_name": "旅行助手", "priority": 1},
        {"agent_name": "travel-helper", "priority": 1},
    ]
    asyncio.run(orchestrator.reply(_intent(schedule)))
    assert agent.calls == 1, f"别名与目录名应去重，实际 {agent.calls} 次"
    print("✓ frontmatter 别名与目录名去重")


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


def test_run_agent_reraised_llm_errors_feed_breaker():
    """子智能体的可重试 / 永久 LLM 错误必须上抛，外层才能记熔断失败。"""

    class _ConnBoomAgent:
        async def reply(self, msg):
            raise ConnectionError("upstream 503")

    class _AuthBoomAgent:
        async def reply(self, msg):
            err = RuntimeError("Error code: 401 - Authentication Fails")
            err.status_code = 401
            raise err

    class _ValueBoomAgent:
        async def reply(self, msg):
            raise ValueError("bad skill output")

    async def call(agent):
        orchestrator = OrchestrationAgent(agent_registry={"demo": agent})
        return await orchestrator._run_agent(
            agent_name="demo",
            context={},
            reason="",
            expected_output="",
            previous_results=[],
        )

    for agent, label in ((_ConnBoomAgent(), "可重试"), (_AuthBoomAgent(), "永久")):
        try:
            asyncio.run(call(agent))
        except (ConnectionError, RuntimeError):
            pass
        else:
            raise AssertionError(f"{label} LLM 错误被吞成 status=error，熔断不可达")
    result = asyncio.run(call(_ValueBoomAgent()))
    assert result["status"] == "error", result
    print("✓ 子智能体 LLM 错误上抛（可重试 / 4xx），普通错误仍按结果降级")


def test_parallel_batch_reraises_llm_errors():
    """并行批次不能把 LLM 错误重新吞成 status=error（熔断 / 重试不可达）。"""

    class _ConnBoom(_StubAgent):
        async def reply(self, x):
            raise ConnectionError("upstream 503")

    orchestrator = OrchestrationAgent(
        agent_registry={
            "preference": _ConnBoom("preference"),
            "event_collection": _ConnBoom("event_collection"),
        }
    )
    schedule = [
        {"agent_name": "preference", "priority": 1},
        {"agent_name": "event_collection", "priority": 1},
    ]
    try:
        asyncio.run(orchestrator.reply(_intent(schedule)))
    except ConnectionError:
        pass
    else:
        raise AssertionError("并行批次的 LLM 错误被吞成 partial_failure")
    print("✓ 并行批次的 LLM 错误整批上抛（熔断器可记账）")


def test_parallel_batch_non_llm_errors_stay_redacted():
    """并行批次的普通异常仍聚合为结果，但错误文本先脱敏。"""

    class _PathBoom(_StubAgent):
        async def reply(self, x):
            raise ValueError(r"failed at C:\Users\alice\secret\file")

    orchestrator = OrchestrationAgent(
        agent_registry={
            "preference": _PathBoom("preference"),
            "event_collection": _StubAgent("event_collection", {"ok": True}),
        }
    )
    schedule = [
        {"agent_name": "preference", "priority": 1},
        {"agent_name": "event_collection", "priority": 1},
    ]
    out = asyncio.run(orchestrator.reply(_intent(schedule)))
    assert "alice" not in out.content, out.content
    assert "<路径>" in out.content, out.content
    print("✓ 并行批次普通异常聚合但脱敏")


def test_reply_clears_stale_deferred_memory():
    """空调度轮次不能提交上一失败回合遗留的暂存负载。"""
    memory = _StubMemory()
    orchestrator = OrchestrationAgent(agent_registry={}, memory_manager=memory)
    orchestrator._deferred_memory = (
        {},
        [
            {
                "agent_name": "preference",
                "priority": 1,
                "result": {
                    "status": "success",
                    "data": {
                        "preferences": [
                            {"type": "hotel_brands", "value": "汉庭"}
                        ]
                    },
                },
            }
        ],
    )
    out = asyncio.run(orchestrator.reply(_intent([])))
    assert "no_agents" in out.content
    assert orchestrator._deferred_memory is None
    assert asyncio.run(orchestrator.commit_deferred_memory()) is False
    assert "hotel_brands" not in memory.long_term.get_preference()
    print("✓ 空调度轮次清空上一回合的暂存负载")


def test_commit_deferred_memory_single_flight():
    """并发提交等待同一个写入任务（后到者不能立即返回 False）。"""
    memory = _StubMemory()
    orchestrator = OrchestrationAgent(agent_registry={}, memory_manager=memory)
    calls: list[int] = []

    def slow_update(intention, results):
        calls.append(1)
        time.sleep(0.2)

    orchestrator._update_memory = slow_update  # type: ignore[method-assign]
    orchestrator._deferred_memory = ({}, [])

    async def run():
        return await asyncio.gather(
            orchestrator.commit_deferred_memory(),
            orchestrator.commit_deferred_memory(),
        )

    results = asyncio.run(run())
    assert calls == [1], calls
    assert results == [True, True]
    print("✓ 延迟记忆提交单飞（并发调用等待同一写入）")


def test_update_memory_key_entities_date_duration_fallback():
    """意图 key_entities 的 date / duration 别名能兜底落库行程。"""
    memory = _StubMemory()
    orchestrator = OrchestrationAgent(agent_registry={}, memory_manager=memory)
    results = [
        {
            "agent_name": "event_collection",
            "priority": 1,
            "result": {
                "status": "success",
                "data": {
                    "origin": None,
                    "destination": None,
                    "start_date": None,
                    "end_date": None,
                    "trip_purpose": None,
                },
            },
        },
        {
            "agent_name": "itinerary_planning",
            "priority": 2,
            "result": {
                "status": "success",
                "data": {"itinerary": {"title": "trip"}},
            },
        },
    ]
    intention = {
        "key_entities": {
            "origin": "北京",
            "destination": "杭州",
            "date": "2026-03-11",
            "duration": "3天",
        }
    }
    orchestrator._update_memory(intention, results)
    trips = memory.long_term.get_trip_history(limit=None)
    assert len(trips) == 1, trips
    assert trips[0]["destination"] == "杭州"
    assert trips[0]["start_date"] == "2026-03-11"
    assert trips[0]["end_date"] == "2026-03-13", trips
    print("✓ key_entities 的 date / duration 别名兜底落库")


def test_update_memory_rejects_inverted_and_placeholder_dates():
    """倒置返程与 9999-12-31 占位日期不能写进台账。"""
    memory = _StubMemory()
    orchestrator = OrchestrationAgent(agent_registry={}, memory_manager=memory)

    def plan(event_data):
        return [
            {
                "agent_name": "event_collection",
                "priority": 1,
                "result": {"status": "success", "data": event_data},
            },
            {
                "agent_name": "itinerary_planning",
                "priority": 2,
                "result": {
                    "status": "success",
                    "data": {"itinerary": {"title": "trip"}},
                },
            },
        ]

    orchestrator._update_memory(
        {},
        plan(
            {
                "origin": "北京",
                "destination": "杭州",
                "start_date": "2026-03-11",
                "end_date": "2026-03-01",
            }
        ),
    )
    trips = memory.long_term.get_trip_history(limit=None)
    assert len(trips) == 1, trips
    assert not trips[0].get("end_date"), "倒置返程应按未知处理"

    orchestrator._update_memory(
        {},
        plan(
            {
                "origin": "北京",
                "destination": "上海",
                "start_date": "2026-03-20",
                "end_date": "9999-12-31",
            }
        ),
    )
    trips = memory.long_term.get_trip_history(limit=None)
    assert len(trips) == 2, "占位返程应按未知处理，行程照常落库"
    placeholder_trip = next(
        trip for trip in trips if trip["destination"] == "上海"
    )
    assert placeholder_trip["start_date"] == "2026-03-20"
    assert not placeholder_trip.get("end_date"), "9999-12-31 占位返程不能落库"
    print("✓ 倒置 / 占位日期不写入台账")


def test_update_memory_backfills_origin_on_existing_trip():
    """后续轮次补出发地时补齐旧行程，而不是插入第二条同趟行程。"""
    memory = _StubMemory()
    memory.long_term.save_trip_history(
        {
            "origin": "",
            "destination": "杭州",
            "start_date": "2026-03-11",
            "end_date": "2026-03-12",
        }
    )
    orchestrator = OrchestrationAgent(agent_registry={}, memory_manager=memory)
    results = [
        {
            "agent_name": "event_collection",
            "priority": 1,
            "result": {
                "status": "success",
                "data": {
                    "origin": "北京",
                    "destination": "杭州",
                    "start_date": "2026-03-11",
                    "end_date": "2026-03-12",
                },
            },
        },
        {
            "agent_name": "itinerary_planning",
            "priority": 2,
            "result": {
                "status": "success",
                "data": {"itinerary": {"title": "trip"}},
            },
        },
    ]
    orchestrator._update_memory({}, results)
    trips = memory.long_term.get_trip_history(limit=None)
    assert len(trips) == 1, f"补出发地不应新增行程：{trips}"
    assert trips[0]["origin"] == "北京"
    print("✓ 后续补出发地时补齐旧行程（不重复建档）")


def test_agent_load_executors_are_daemon_and_unregistered():
    """技能加载执行器为 daemon 且不登记 atexit join（停服不被冷加载拖住）。"""
    from concurrent.futures.thread import _threads_queues

    from agents import orchestration_agent as module

    for executor in (module._AGENT_LOAD_EXECUTOR, module._RAG_LOAD_EXECUTOR):
        assert executor.submit(lambda: 1).result(timeout=5) == 1
        threads = list(executor._threads)
        assert threads and all(thread.daemon for thread in threads)
        assert all(thread not in _threads_queues for thread in threads), (
            "登记到 _threads_queues 会让 atexit 对加载线程逐个 join"
        )
    print("✓ 技能加载执行器线程为 daemon 且不参与 atexit join")


def test_update_memory_legacy_dict_skips_explanatory_keys():
    """旧字典格式偏好：note / message 等说明键不能当偏好类型落库。"""
    memory = _StubMemory()
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
                        "preferences": {
                            "hotel_brands": "汉庭",
                            "note": "已记录",
                            "message": "ok",
                            "has_preferences": True,
                        }
                    },
                },
            }
        ],
    )
    preferences = memory.long_term.get_preference()
    assert preferences.get("hotel_brands") == "汉庭", preferences
    for key in ("note", "message", "has_preferences"):
        assert key not in preferences, preferences
    print("✓ 旧字典偏好跳过说明键")


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


def test_update_memory_isolates_preference_failures(caplog):
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
    with caplog.at_level(logging.WARNING):
        OrchestrationAgent(
            agent_registry={}, memory_manager=memory
        )._update_memory({}, _isolated_results())
    assert "保存偏好失败" in caplog.text

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


def test_update_memory_isolates_trip_save_failure(caplog):
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
    with caplog.at_level(logging.WARNING):
        orchestrator._update_memory({}, _isolated_results())
    assert "保存行程失败" in caplog.text
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


def test_retry_cache_does_not_cache_error_results():
    """整轮重试只复用成功结果：返回 status=error 的智能体应重新执行。"""
    calls = {"a": 0, "b": 0}

    class Flaky(_StubAgent):
        async def reply(self, x):
            calls[self.name] += 1
            if self.name == "a" and calls["a"] == 1:
                return Msg(
                    name=self.name,
                    content='{"status": "error", "message": "模型返回格式有误"}',
                    role="assistant",
                )
            if self.name == "b" and calls["b"] <= 2:
                raise ConnectionError("transient blip")
            return Msg(name=self.name, content='{"ok": true}', role="assistant")

    orchestrator = OrchestrationAgent(
        agent_registry={"a": Flaky("a"), "b": Flaky("b")}
    )
    schedule = [
        {"agent_name": "a", "priority": 1},
        {"agent_name": "b", "priority": 1},
    ]
    cache: dict = {}
    try:
        asyncio.run(orchestrator.reply(_intent(schedule), retry_cache=cache))
    except ConnectionError:
        pass
    else:
        raise AssertionError("首个可重试错误必须上抛给外层重试")
    assert calls["a"] == 1
    assert all(
        item.get("status") == "success" for item in cache.values()
    ), "status=error 的结果不能进重试缓存"

    asyncio.run(orchestrator.reply(_intent(schedule), retry_cache=cache))
    assert calls["a"] == 2, f"error 结果应随整轮重试重新执行: {calls}"
    assert all(
        item.get("status") == "success" for item in cache.values()
    ), cache
    print("✓ 重试缓存只复用成功结果")


def test_commit_deferred_memory_commits_payload_after_waiting():
    """旧提交在途时到达的新负载必须在等待后继续提交，而不是直接丢弃。"""
    import threading

    memory = _StubMemory()
    orchestrator = OrchestrationAgent(agent_registry={}, memory_manager=memory)
    calls: list[int] = []
    first_started = threading.Event()
    release = threading.Event()

    def slow_update(intention, results):
        turn = intention.get("turn")
        calls.append(turn)
        if turn == 1:
            first_started.set()
            release.wait(5)

    orchestrator._update_memory = slow_update  # type: ignore[method-assign]
    orchestrator._deferred_memory = ({"turn": 1}, [])

    async def run():
        first = asyncio.ensure_future(orchestrator.commit_deferred_memory())
        ready = await asyncio.to_thread(first_started.wait, 5)
        assert ready, "首个提交未启动"
        orchestrator._deferred_memory = ({"turn": 2}, [])
        second = asyncio.ensure_future(orchestrator.commit_deferred_memory())
        await asyncio.sleep(0.05)
        release.set()
        return await asyncio.gather(first, second)

    results = asyncio.run(run())
    assert calls == [1, 2], f"新负载必须落库: {calls}"
    assert results == [True, True], results
    assert orchestrator._deferred_memory is None
    print("✓ 延迟提交等待旧任务后继续提交新负载")


def test_update_memory_backfills_end_date_instead_of_duplicate():
    """后续轮次补出返程日期时更新既有行程，不插入重复行。"""
    memory = _StubMemory()
    orchestrator = OrchestrationAgent(agent_registry={}, memory_manager=memory)

    def results(start, end):
        return [
            {
                "agent_name": "event_collection",
                "priority": 1,
                "result": {
                    "status": "success",
                    "data": {
                        "origin": "北京",
                        "destination": "上海",
                        "start_date": start,
                        "end_date": end,
                    },
                },
            },
            {
                "agent_name": "itinerary_planning",
                "priority": 2,
                "result": {
                    "status": "success",
                    "data": {"itinerary": {"title": "上海出差"}},
                },
            },
        ]

    orchestrator._update_memory({}, results("2026-03-10", None))
    trips = memory.long_term.get_trip_history(limit=None)
    assert len(trips) == 1 and not trips[0].get("end_date"), trips

    orchestrator._update_memory({}, results("2026-03-10", "2026-03-14"))
    trips = memory.long_term.get_trip_history(limit=None)
    assert len(trips) == 1, trips
    assert trips[0]["end_date"] == "2026-03-14"
    assert memory.long_term.get_statistics()["total_trips"] == 1
    print("✓ 补出返程日期更新既有行程而非重复落库")


def test_update_memory_backfills_origin_and_end_together():
    """同一轮既补出发地又补返程日期时，两字段一起写入旧行程，不重复建档。"""
    memory = _StubMemory()
    orchestrator = OrchestrationAgent(agent_registry={}, memory_manager=memory)

    def results(origin, end):
        return [
            {
                "agent_name": "event_collection",
                "priority": 1,
                "result": {
                    "status": "success",
                    "data": {
                        "origin": origin,
                        "destination": "杭州",
                        "start_date": "2026-03-11",
                        "end_date": end,
                    },
                },
            },
            {
                "agent_name": "itinerary_planning",
                "priority": 2,
                "result": {
                    "status": "success",
                    "data": {"itinerary": {"title": "杭州出差"}},
                },
            },
        ]

    orchestrator._update_memory({}, results("", None))
    trips = memory.long_term.get_trip_history(limit=None)
    assert len(trips) == 1 and not trips[0].get("origin"), trips

    orchestrator._update_memory({}, results("上海", "2026-03-15"))
    trips = memory.long_term.get_trip_history(limit=None)
    assert len(trips) == 1, f"同时补出发地/返程不应新增行程：{trips}"
    assert trips[0]["origin"] == "上海"
    assert trips[0]["end_date"] == "2026-03-15"
    assert memory.long_term.get_statistics()["total_trips"] == 1
    print("✓ 同轮补出发地与返程更新既有行程，不重复建档")


def test_trip_duration_days_week_word_does_not_inflate_day_counts():
    """「周末 3 天」「假期 3 天」按 3 天解释；纯周表达才 ×7。"""
    from agents.orchestration_agent import _trip_duration_days

    assert _trip_duration_days("周末3天") == 3
    assert _trip_duration_days("假期3天") == 3
    assert _trip_duration_days("3天2晚") == 3
    assert _trip_duration_days("一周") == 7
    assert _trip_duration_days("两周") == 14
    assert _trip_duration_days("3个星期") == 21
    print("✓ 时长解析：带天/日的文本不按周放大")


def test_previous_preferences_context_skips_explanatory_keys():
    """旧格式偏好字典的说明性键不进入后续批次上下文。"""
    orchestrator = OrchestrationAgent(agent_registry={})
    result = {
        "agent_name": "preference",
        "priority": 1,
        "result": {
            "status": "success",
            "data": {
                "preferences": {
                    "hotel_brands": ["汉庭"],
                    "note": "用户还提到下周去上海",
                    "message": "已记录",
                    "reason": "模型推理",
                    "has_preferences": True,
                }
            },
        },
    }
    context = orchestrator._context_with_previous_preferences({}, [result])
    assert context["user_preferences"] == {"hotel_brands": ["汉庭"]}
    print("✓ 说明性键不进入后续批次偏好上下文")

def test_update_memory_strips_nul_from_key_entities():
    """意图 key_entities 里的 NUL 不能写进行程（PG 静默失败 / JSON 不可编辑）。"""
    memory = _StubMemory()
    orchestrator = OrchestrationAgent(agent_registry={}, memory_manager=memory)
    results = [
        {
            "agent_name": "event_collection",
            "priority": 1,
            "result": {
                "status": "success",
                "data": {
                    "origin": None,
                    "destination": None,
                    "start_date": None,
                    "end_date": None,
                    "trip_purpose": None,
                },
            },
        },
        {
            "agent_name": "itinerary_planning",
            "priority": 2,
            "result": {
                "status": "success",
                "data": {"itinerary": {"title": "trip"}},
            },
        },
    ]
    intention = {
        "key_entities": {
            "destination": "\u0000杭州",
            "date": "2026-03-11",
        }
    }
    orchestrator._update_memory(intention, results)
    trips = memory.long_term.get_trip_history(limit=None)
    assert len(trips) == 1, trips
    assert trips[0]["destination"] == "杭州", trips
    assert "\x00" not in json.dumps(trips, ensure_ascii=False), trips
    print("✓ key_entities 的 NUL 被剔除")


def test_oversized_preference_skipped_on_apply():
    """LLM 写入超过 10000 字符的偏好按条跳过，其余偏好照常写入。"""
    memory = _StubMemory()
    orchestrator = OrchestrationAgent(agent_registry={}, memory_manager=memory)
    orchestrator._apply_single_preference(
        "hotel_brands", "x" * 10001, "replace"
    )
    assert "hotel_brands" not in memory.long_term.get_preference()
    orchestrator._apply_single_preference("hotel_brands", "汉庭", "replace")
    assert memory.long_term.get_preference().get("hotel_brands") == "汉庭"
    # 存储层兜底：直接调用 save / append 同样拒绝越限值
    with pytest.raises(ValueError):
        memory.long_term.save_preference("airlines", "y" * 10001)
    # 上限按 json.dumps 后的长度计（字符串多两个引号）
    memory.long_term.save_preference("airlines", "y" * 9998)
    with pytest.raises(ValueError):
        memory.long_term.append_preference("airlines", "z")
    print("✓ 超限偏好被跳过 / 拒绝")


def test_checkpoint_reuse_requires_same_task_digest(tmp_path):
    """同键重试的计划变化（reason 不同）不复用旧检查点步骤。"""
    from utils.execution_checkpoint import CheckpointStore

    def _run(store, key, schedule, agent):
        orchestrator = OrchestrationAgent(agent_registry={"a": agent})
        orchestrator._checkpoint_store = store
        try:
            asyncio.run(orchestrator.reply(_intent(schedule), checkpoint_key=key))
        except Exception:  # noqa: BLE001 - 失败步骤本身不是本用例重点
            pass

    store = CheckpointStore(tmp_path, ttl_sec=3600)
    schedule = [
        {"agent_name": "a", "priority": 1, "reason": "r1"},
        {"agent_name": "b", "priority": 1},
    ]
    first_a = _StubAgent("a", {"ok": True})
    _run(store, "u:s:t9", schedule, first_a)
    # 已写入步骤摘要
    state = store.load("u:s:t9")
    assert state and state.get("step_keys", {}).get("a"), state

    # 计划变化：a 的 reason 不同 → 必须重新执行
    changed = [
        {"agent_name": "a", "priority": 1, "reason": "r2"},
        {"agent_name": "b", "priority": 1, "raise_error": True},
    ]

    class _BoomAgent(_StubAgent):
        async def reply(self, x):
            self.calls += 1
            raise RuntimeError("b boom")

    orchestrator = OrchestrationAgent(
        agent_registry={"a": _StubAgent("a", {"ok": True}), "b": _BoomAgent("b")}
    )
    orchestrator._checkpoint_store = store
    try:
        asyncio.run(orchestrator.reply(_intent(changed), checkpoint_key="u:s:t9"))
    except Exception:  # noqa: BLE001
        pass
    assert orchestrator.agent_registry["a"].calls == 1, "计划变化后不能复用旧步骤"
    # 相同计划：复用 a，不再执行
    same = [
        {"agent_name": "a", "priority": 1, "reason": "r2"},
        {"agent_name": "b", "priority": 1, "raise_error": True},
    ]
    again_a = _StubAgent("a", {"ok": True})
    orchestrator2 = OrchestrationAgent(
        agent_registry={"a": again_a, "b": _BoomAgent("b")}
    )
    orchestrator2._checkpoint_store = store
    try:
        asyncio.run(
            orchestrator2.reply(_intent(same), checkpoint_key="u:s:t9")
        )
    except Exception:  # noqa: BLE001
        pass
    assert again_a.calls == 0, "任务摘要一致时应复用检查点步骤"
    print("✓ 检查点复用绑定任务摘要")


def test_checkpoint_reruns_downstream_when_upstream_input_changes(tmp_path):
    """检查点摘要绑定真实输入：上游结果变化后，下游计划措辞不变也要重跑。"""
    from utils.execution_checkpoint import CheckpointStore

    store = CheckpointStore(tmp_path, ttl_sec=3600)

    def schedule(a_reason: str) -> list[dict]:
        return [
            {
                "agent_name": "a",
                "priority": 1,
                "reason": a_reason,
                "expected_output": "ea",
            },
            {
                "agent_name": "b",
                "priority": 2,
                "reason": "r2",
                "expected_output": "eb",
            },
        ]

    first_a = _StubAgent("a", {"ver": 1})
    first_b = _StubAgent("b", {"ok": True})
    first = OrchestrationAgent(agent_registry={"a": first_a, "b": first_b})
    first._checkpoint_store = store
    asyncio.run(
        first.reply(_intent(schedule("r1")), checkpoint_key="u:s:down")
    )
    assert store.load("u:s:down")["steps"].keys() >= {"a", "b"}

    second_a = _StubAgent("a", {"ver": 2})
    second_b = _StubAgent("b", {"ok": True})
    second = OrchestrationAgent(agent_registry={"a": second_a, "b": second_b})
    second._checkpoint_store = store
    asyncio.run(
        second.reply(
            _intent(schedule("r1-changed")), checkpoint_key="u:s:down"
        )
    )
    assert second_a.calls == 1, "计划漂移的上游必须重跑"
    assert second_b.calls == 1, "上游输入变化后下游不能复用旧结果"

    third_a = _StubAgent("a", {"ver": 3})
    third_b = _StubAgent("b", {"ok": True})
    third = OrchestrationAgent(agent_registry={"a": third_a, "b": third_b})
    third._checkpoint_store = store
    asyncio.run(
        third.reply(
            _intent(schedule("r1-changed")), checkpoint_key="u:s:down"
        )
    )
    assert third_a.calls == 0 and third_b.calls == 0, "同输入重试仍应复用检查点"


if __name__ == "__main__":
    # 用例常用 tempfile.mkdtemp() 自建记忆目录：直跑没有 pytest 的
    # conftest 回收，登记后在进程退出时统一清理
    import _tmp_registry

    _tmp_registry.install()
    import sys

    # Windows 控制台 / 重定向管道默认 GBK：✓ 等字符会抛 UnicodeEncodeError，
    # 断言通过也会以 traceback 退出；先切 UTF-8（不支持时保持原状）
    try:
        for _stream in (sys.stdout, sys.stderr):
            _reconfigure = getattr(_stream, "reconfigure", None)
            if callable(_reconfigure):
                _reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        pass

    # 自动发现：手写清单曾漏登记 12 个用例（其中 9 个无需任何夹具）
    # 却仍打印「全部通过」；caplog 用直跑替身真正执行，其余需 pytest
    # 专用夹具的用例计入跳过并列出名单
    import asyncio
    import inspect
    import pathlib
    import shutil
    import tempfile

    ran = 0
    skipped = 0
    skipped_names: list[str] = []
    for name, func in sorted(globals().items()):
        if not name.startswith("test_") or not callable(func):
            continue
        params = inspect.signature(func).parameters
        fixture = None
        caplog_shim = None
        kwargs: dict = {}
        tmp_dir_paths: list[pathlib.Path] = []
        unsupported = False
        for param, spec in params.items():
            if param == "monkeypatch":
                fixture = pytest.MonkeyPatch()
                kwargs[param] = fixture
            elif param == "tmp_path":
                _tmp_dir = pathlib.Path(tempfile.mkdtemp())
                tmp_dir_paths.append(_tmp_dir)
                kwargs[param] = _tmp_dir
            elif param == "caplog":
                import _caplog_shim

                caplog_shim = _caplog_shim.CaplogShim()
                kwargs[param] = caplog_shim
            else:
                unsupported = True
                break
        if unsupported:
            if caplog_shim is not None:
                caplog_shim.close()
            if fixture is not None:
                fixture.undo()
            for _tmp_dir in tmp_dir_paths:
                shutil.rmtree(_tmp_dir, ignore_errors=True)
            skipped += 1
            skipped_names.append(name)
            continue
        storage_snapshot: dict | None = None
        try:
            from config import STORAGE_CONFIG

            storage_snapshot = dict(STORAGE_CONFIG)
        except Exception:  # noqa: BLE001 - 配置不可用时不快照
            storage_snapshot = None
        try:
            if inspect.iscoroutinefunction(func):
                asyncio.run(func(**kwargs))
            else:
                func(**kwargs)
            ran += 1
        except pytest.skip.Exception:
            skipped += 1
            skipped_names.append(name)
        finally:
            if caplog_shim is not None:
                caplog_shim.close()
            if storage_snapshot is not None:
                from typing import cast

                from config import STORAGE_CONFIG

                _cfg = cast(dict, STORAGE_CONFIG)
                _cfg.clear()
                _cfg.update(storage_snapshot)
            if fixture is not None:
                fixture.undo()
            # 直跑模式自建的临时目录要自己清：否则每跑一次清单就在系统
            # 临时目录里留下一批带记忆文件的目录
            for _tmp_dir in tmp_dir_paths:
                shutil.rmtree(_tmp_dir, ignore_errors=True)
    total = sum(
        1
        for name, func in globals().items()
        if name.startswith("test_") and callable(func)
    )
    if skipped_names:
        print("直跑跳过（需 pytest 专用夹具）：" + "、".join(skipped_names))
    if skipped:
        print(
            f"\n执行完成（{ran} 个通过；{skipped} 个按条件跳过 / "
            "需 pytest 专用夹具）"
        )
    else:
        print(f"\n全部通过（{ran}/{total} 个用例）")
