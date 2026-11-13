"""ConversationRuntime 记忆 IO 线程化测试（无需 API Key、不访问网络）。

Web 端多个 SSE 流共享一个事件循环：``ask`` 里的记忆读取 / 写入都是阻塞
调用（JSON 文件、PostgreSQL、Redis），必须放到线程池执行，否则一个慢
磁盘或慢 Redis 会卡住所有连接。这里用假记忆 / 假智能体验证这些调用
都不在事件循环线程上执行。
"""
import asyncio
import json
import os
import sys
import threading

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from agents.conversation import ConversationRuntime


class _Response:
    def __init__(self, content: str):
        self.content = content


class _FakeMemory:
    def __init__(self):
        self.threads: dict[str, int] = {}
        self.events: list[tuple[str, str]] = []
        self.staged: list[tuple[str, str]] = []
        self.rolled_back: list[str] = []
        self.long_term = self
        self.short_term = self

    def reload_long_term(self):
        self.threads["reload"] = threading.get_ident()

    def get_preference(self):
        self.threads["preferences"] = threading.get_ident()
        return {
            "home_location": "北京",
            "empty_value": "",
            "airlines": ["国航", "东航"],
        }

    def get_recent_context(self, n_turns=5):
        assert n_turns == 5
        self.threads["recent"] = threading.get_ident()
        return [{"role": "user", "content": "之前说过的话"}]

    async def get_long_term_summary_async(self, max_messages=30):
        assert max_messages == 30
        return "历史总结"

    def stage_message(self, role, content, metadata=None):
        self.threads[f"stage-{role}"] = threading.get_ident()
        self.staged.append((role, content))

    def rollback_staged_message(self, content):
        self.threads["rollback"] = threading.get_ident()
        self.rolled_back.append(content)
        if self.staged and self.staged[-1] == ("user", content):
            self.staged.pop()
        return True

    def persist_message(self, role, content):
        self.threads[f"persist-{role}"] = threading.get_ident()
        self.events.append((role, content))

    def persist_turn(self, user_content, assistant_content, should_commit=None):
        # 整轮问答在同一次线程调用里提交（用户 + 助手），不能分两次
        self.threads["persist-user"] = threading.get_ident()
        self.threads["add-assistant"] = threading.get_ident()
        self.persist_guard = should_commit
        if should_commit is not None and not should_commit():
            # 写锁内判定会话已删除：整轮不写（与真实存储层语义一致）
            return False
        self.events.append(("user", user_content))
        self.events.append(("assistant", assistant_content))
        return True

    def add_message(self, role, content, metadata=None):
        self.threads[f"add-{role}"] = threading.get_ident()
        self.events.append((role, content))


class _FakeIntention:
    def __init__(self):
        self.context = None

    async def reply(self, context):
        self.context = context
        return _Response(
            json.dumps(
                {
                    "reasoning": "问候",
                    "intents": ["greeting"],
                    "agent_schedule": [
                        {"agent_name": "GreetingAgent", "priority": 1}
                    ],
                },
                ensure_ascii=False,
            )
        )


class _FakeOrchestrator:
    def __init__(self):
        self.intention = None

    async def reply(self, intention, on_event=None):
        self.intention = intention
        return _Response(
            json.dumps(
                {"results": [{"agent_name": "GreetingAgent", "status": "success"}]},
                ensure_ascii=False,
            )
        )


def _make_runtime():
    runtime = ConversationRuntime.__new__(ConversationRuntime)
    runtime.user_id = "u-ctx"
    runtime.session_id = "s-ctx"
    runtime.model = None
    runtime.circuit_breaker = None
    runtime._persist_guard = None
    runtime.intention_agent = _FakeIntention()
    runtime.orchestrator = _FakeOrchestrator()
    runtime.agent_cache = {}
    memory = _FakeMemory()
    runtime.memory = memory
    return runtime, memory


def test_ask_runs_memory_io_off_event_loop():
    """reload / 偏好 / 最近对话 / 两次 add_message 都不能在事件循环线程执行。"""
    runtime, memory = _make_runtime()
    loop_thread = threading.get_ident()

    result = asyncio.run(runtime.ask("你好"))

    assert result["reply"]
    assert result["elapsed"] >= 0
    assert result["persisted"] is True, "正常回合必须标记为已写入长期记忆"
    for name in ("reload", "preferences", "recent", "stage-user", "persist-user", "add-assistant"):
        assert name in memory.threads, f"{name} 未被调用"
        assert memory.threads[name] != loop_thread, (
            f"{name} 在事件循环线程执行，会阻塞其它连接"
        )
    assert [role for role, _ in memory.events] == ["user", "assistant"]
    assert memory.staged and memory.staged[0] == ("user", "你好"), "编排前先暂存本轮提问"
    # 偏好进了系统提示，最近对话按顺序跟在后面
    system_msg = runtime.intention_agent.context[0]
    assert system_msg.role == "system"
    assert "北京" in system_msg.content
    assert "历史总结" in system_msg.content
    roles = [msg.role for msg in runtime.intention_agent.context]
    assert roles[-2:] == ["user", "user"]
    print("✓ ask 的记忆读写全部离开事件循环线程")


def test_failed_orchestration_leaves_no_long_term_user_turn():
    """编排失败时长期记忆不留孤儿用户提问，短期暂存提问同步回滚。"""
    runtime, memory = _make_runtime()

    async def _direct(factory, max_retries=0, base_delay=0.0, max_delay=0.0):
        return await factory()

    runtime._call_with_resilience = _direct  # type: ignore[method-assign]

    class _BoomOrchestrator:
        async def reply(self, intention, on_event=None):
            raise RuntimeError("gateway down")

    runtime.orchestrator = _BoomOrchestrator()
    with pytest.raises(RuntimeError):
        asyncio.run(runtime.ask("你好"))
    assert memory.events == [], "失败时不得写长期记忆"
    assert memory.staged == [], "暂存的用户提问必须随失败一起回滚"
    assert memory.rolled_back == ["你好"]
    print("✓ 编排失败不落孤儿用户轮（暂存提问回滚，Web 与 CLI 同契约）")


def test_persist_veto_rolls_back_staged_question():
    """落库守卫否决（会话已删除）后暂存提问也回滚，不留孤立提问。"""
    runtime, memory = _make_runtime()

    def _veto(user_content, assistant_content, should_commit=None):
        return False

    memory.persist_turn = _veto  # type: ignore[method-assign]
    result = asyncio.run(runtime.ask("你好"))
    assert result["reply"]
    assert result["persisted"] is False, "墓碑否决的回合必须透传未落库状态"
    assert memory.rolled_back == ["你好"], "被否决的整轮不能留下孤立提问"
    assert memory.staged == []
    print("✓ 落库守卫否决后回滚暂存提问")


def test_persist_guard_reaches_memory_write():
    """落库守卫必须原样传给存储层（写入锁内判定会话是否已删除）。"""

    def guard():
        return False

    runtime, memory = _make_runtime()
    runtime._persist_guard = guard
    result = asyncio.run(runtime.ask("你好"))
    assert result["reply"]
    assert result["persisted"] is False
    assert memory.persist_guard is guard
    print("✓ 落库守卫传递到记忆写入")


def test_deferred_memory_committed_after_successful_persist():
    """编排器的偏好 / 行程写入延迟到整轮落库成功之后。"""
    runtime, memory = _make_runtime()

    class _DeferredOrch:
        def __init__(self):
            self.deferred = None
            self.commits = 0
            self.pending = False

        async def reply(self, intention, on_event=None, defer_memory=False):
            self.deferred = defer_memory
            self.pending = bool(defer_memory)
            return _Response(json.dumps({"results": []}, ensure_ascii=False))

        async def commit_deferred_memory(self):
            if not self.pending:
                return False
            self.pending = False
            self.commits += 1
            return True

    orch = _DeferredOrch()
    runtime.orchestrator = orch
    result = asyncio.run(runtime.ask("你好"))
    assert orch.deferred is True
    assert orch.commits == 1, "成功落库后必须提交延迟的记忆写入"
    assert result["persisted"] is True
    print("✓ 延迟记忆写入在落库成功后提交")


def test_deferred_memory_skipped_when_persist_vetoed():
    """墓碑否决的回合不能执行偏好 / 行程写入（避免幽灵记录）。"""
    runtime, memory = _make_runtime()

    class _DeferredOrch:
        def __init__(self):
            self.commits = 0

        async def reply(self, intention, on_event=None, defer_memory=False):
            return _Response(json.dumps({"results": []}, ensure_ascii=False))

        async def commit_deferred_memory(self):
            self.commits += 1
            return True

    orch = _DeferredOrch()
    runtime.orchestrator = orch
    memory.persist_turn = lambda *args, **kwargs: False
    result = asyncio.run(runtime.ask("你好"))
    assert result["persisted"] is False
    assert orch.commits == 0, "被否决的回合不能写偏好 / 行程"
    print("✓ 否决回合跳过延迟记忆写入")


def test_deferred_memory_commits_after_cancel_during_persist():
    """取消落在落库线程期间：写线程仍提交整轮，延迟记忆写入必须补提交。"""
    import threading

    runtime, _base = _make_runtime()
    started = threading.Event()
    release = threading.Event()

    class _SlowMemory(_FakeMemory):
        def persist_turn(self, user_content, assistant_content, should_commit=None):
            started.set()
            release.wait(10)
            return True

    runtime.memory = _SlowMemory()

    class _DeferredOrch:
        def __init__(self):
            self.commits = 0
            self.pending = False

        async def reply(self, intention, on_event=None, defer_memory=False):
            self.pending = bool(defer_memory)
            return _Response(json.dumps({"results": []}, ensure_ascii=False))

        async def commit_deferred_memory(self):
            if not self.pending:
                return False
            self.pending = False
            self.commits += 1
            return True

    orch = _DeferredOrch()
    runtime.orchestrator = orch

    async def scenario():
        task = asyncio.ensure_future(runtime.ask("你好"))
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        for _ in range(200):
            if orch.commits:
                break
            await asyncio.sleep(0.02)

    asyncio.run(scenario())
    assert orch.commits == 1, "落库成功后的延迟记忆写入不能因取消而丢失"
    print("✓ 取消落在落库线程期间仍补提交延迟记忆")


def test_runtime_wires_epoch_guard_into_persist():
    """回合开始时读取会话纪元，组合守卫传入 persist_turn。"""
    runtime, _base = _make_runtime()

    class _EpochMemory(_FakeMemory):
        def __init__(self):
            super().__init__()
            self.epoch_reads = 0
            self.guard_epoch = None

        def session_epoch(self):
            self.epoch_reads += 1
            return (True, "7")

        def persist_guard(self, epoch_at_start, base=None):
            self.guard_epoch = epoch_at_start

            def guard():
                return True

            return guard

    memory = _EpochMemory()
    runtime.memory = memory
    result = asyncio.run(runtime.ask("你好"))
    assert result["persisted"] is True
    assert memory.epoch_reads == 1, "回合开始应读取一次会话纪元"
    assert memory.guard_epoch == (True, "7"), "组合守卫必须收到纪元快照"
    print("✓ 运行时把会话纪元接入落库守卫")


def test_cancel_during_stage_thread_rolls_back_after_visible():
    """取消落在暂存写入线程期间：回滚必须等写入可见后执行，不留孤儿提问。"""
    import threading

    runtime, _base = _make_runtime()
    started = threading.Event()
    release = threading.Event()

    class _SlowStage(_FakeMemory):
        def stage_message(self, role, content, metadata=None):
            started.set()
            release.wait(10)
            super().stage_message(role, content, metadata)

    memory = _SlowStage()
    runtime.memory = memory

    async def scenario():
        task = asyncio.ensure_future(runtime.ask("你好"))
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await runtime.drain_pending_persist()

    asyncio.run(scenario())
    assert memory.staged == [], "暂存提问必须被回滚（不能留下无回答的孤儿）"
    assert memory.rolled_back == ["你好"]
    print("✓ 暂存写入可见后回滚（取消竞态）")


def test_persist_failure_rolls_back_staged_user_message():
    """落库失败（长期记忆未写入）时暂存提问同样要回滚，两层记忆保持一致。"""
    from utils.errors import MemoryPersistenceError

    runtime, memory = _make_runtime()

    def _boom(user_content, assistant_content, should_commit=None):
        raise MemoryPersistenceError("磁盘已满")

    memory.persist_turn = _boom  # type: ignore[method-assign]
    with pytest.raises(MemoryPersistenceError):
        asyncio.run(runtime.ask("你好"))
    assert memory.events == []
    assert memory.rolled_back == ["你好"], "整轮没有落库时暂存提问不能残留"
    print("✓ 落库失败回滚暂存提问")


def test_malformed_recent_context_entries_are_skipped():
    """可解析但形状不对的短期条目不能让 _build_context 抛 KeyError。"""
    runtime, memory = _make_runtime()
    memory.get_recent_context = lambda n_turns=5: [  # type: ignore[method-assign]
        {"foo": 1},
        "不是字典的条目",
        {"role": "user"},
        {"role": "user", "content": "正常消息"},
    ]
    result = asyncio.run(runtime.ask("你好"))
    assert result["reply"]
    contents = [msg.content for msg in runtime.intention_agent.context]
    assert "正常消息" in contents
    print("✓ 形状非法的短期条目被跳过")


def test_ask_survives_memory_snapshot_shape():
    """记忆快照返回空偏好 / 空最近对话 / 空总结时上下文只包含当前输入。"""
    runtime, memory = _make_runtime()
    memory.get_preference = lambda: {}  # type: ignore[method-assign]
    memory.get_recent_context = lambda n_turns=5: []  # type: ignore[method-assign]

    async def _no_summary(max_messages=30):
        return ""

    memory.get_long_term_summary_async = _no_summary  # type: ignore[method-assign]

    result = asyncio.run(runtime.ask("你好"))
    assert result["reply"]
    assert [msg.role for msg in runtime.intention_agent.context] == ["user"]
    print("✓ 空记忆快照时上下文只有当前输入")


def test_ask_releases_half_open_probe_on_early_failure(monkeypatch):
    """半开试探在调用 LLM 前失败（如写锁超时）：名额必须归还。"""
    from utils.circuit_breaker import CircuitBreaker
    from utils.errors import MemoryPersistenceError

    times = {"now": 100.0}
    monkeypatch.setattr("utils.circuit_breaker.time.monotonic", lambda: times["now"])
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout_sec=60, half_open_successes=2
    )
    breaker.record_failure()
    times["now"] = 161.0

    runtime, memory = _make_runtime()
    runtime.circuit_breaker = breaker

    def boom():
        raise MemoryPersistenceError("磁盘已满")

    memory.reload_long_term = boom  # type: ignore[method-assign]
    with pytest.raises(MemoryPersistenceError):
        asyncio.run(runtime.ask("你好"))
    assert breaker.allow_call() is True, "提前失败后应归还半开试探名额"
    print("✓ 半开试探在提前失败时归还名额")


def test_parse_failure_is_user_facing_error():
    """意图 / 编排解析失败使用 UserFacingError（仅这类文案可原样透出）。"""
    from utils.errors import UserFacingError

    runtime, _ = _make_runtime()

    class _BadIntention:
        async def reply(self, context):
            return _Response("这不是 JSON")

    runtime.intention_agent = _BadIntention()
    with pytest.raises(UserFacingError):
        asyncio.run(runtime.ask("你好"))
    print("✓ 解析失败使用 UserFacingError")


def test_summary_inputs_off_event_loop():
    """跨会话总结的聊天 / 行程查询不能在事件循环线程执行。"""
    from context.memory_manager import MemoryManager

    class _FakeLongTerm:
        def __init__(self):
            self.threads: list[int] = []
            self.calls: list[tuple[str | None, str | None, int | None]] = []

        def get_chat_history(
            self, session_id=None, limit=None, exclude_session_id=None
        ):
            self.threads.append(threading.get_ident())
            self.calls.append((session_id, exclude_session_id, limit))
            messages = [
                {
                    "session_id": "other",
                    "role": "user",
                    "content": "另一会话的消息",
                    "timestamp": "t1",
                }
            ]
            return messages[-limit:] if limit else messages

        def get_trip_history(self, limit=None):
            self.threads.append(threading.get_ident())
            return []

    class _FakeModel:
        async def __call__(self, messages, **kwargs):
            return {"content": [{"type": "text", "text": "历史摘要"}]}

    manager = MemoryManager.__new__(MemoryManager)
    manager.user_id = "u-summary"
    manager.session_id = "s-summary"
    manager.llm_model = _FakeModel()
    manager.long_term = _FakeLongTerm()
    manager._summary_cache = None
    manager._summary_failure_until = 0.0

    loop_thread = threading.get_ident()
    summary = asyncio.run(manager.get_long_term_summary_async(max_messages=30))
    assert summary
    assert manager.long_term.threads, "应查询聊天历史 / 行程"
    assert all(tid != loop_thread for tid in manager.long_term.threads), (
        "总结的同步查询在事件循环线程执行，会冻结所有 SSE 流"
    )
    assert manager.long_term.calls == [(None, "s-summary", 50)], (
        "跨会话历史应在存储层按「排除当前会话 + 固定窗口」一次取回"
    )
    print("✓ 总结查询离开事件循环线程")


def test_model_output_with_lone_surrogate_is_sanitized():
    """模型输出含转义孤立代理项（截断 emoji）：事件 / 回复 / 落库都必须可编码。

    真实约束：SSE 帧与 JSONResponse 都按 UTF-8 编码，孤立代理项会让 200
    流无 error/done 直接中断，或写入 JSON 文件时抛 UnicodeEncodeError。
    """
    lonely = chr(0xD800)
    runtime, memory = _make_runtime()

    class _SurrogateIntention:
        async def reply(self, context):
            return _Response(
                json.dumps(
                    {
                        "reasoning": "推理" + lonely,
                        "intents": ["greeting"],
                        "agent_schedule": [{"agent_name": "GreetingAgent"}],
                    }
                )
            )

    class _SurrogateOrchestrator:
        def __init__(self):
            self.intention = None

        async def reply(self, intention, on_event=None):
            self.intention = intention
            return _Response(
                json.dumps(
                    {
                        "answer": "回答" + lonely,
                        "results": [
                            {
                                "agent_name": "GreetingAgent",
                                "status": "success",
                                "message": "细节" + lonely,
                            }
                        ],
                    }
                )
            )

    runtime.intention_agent = _SurrogateIntention()
    runtime.orchestrator = _SurrogateOrchestrator()
    events: list[dict] = []

    result = asyncio.run(runtime.ask("你好", on_event=events.append))

    for event in events:
        json.dumps(event, ensure_ascii=False).encode("utf-8")
    result["reply"].encode("utf-8")
    assert lonely not in result["reply"], "回复里的孤立代理项必须已转义"
    assistant_payloads = [
        content for role, content in memory.events if role == "assistant"
    ]
    assert assistant_payloads, "整轮应已落库"
    for payload in assistant_payloads:
        payload.encode("utf-8")
    passed_intention = runtime.orchestrator.intention
    assert passed_intention is not None, "意图结果应传给编排器"
    passed_intention.content.encode("utf-8")
    assert lonely not in passed_intention.content, (
        "传给编排器的意图 Msg 必须已转义（否则落库 / prompt 编码失败）"
    )
    print("✓ 模型输出的孤立代理项在事件 / 回复 / 落库 / 编排输入前转义")


def test_non_dict_model_outputs_are_user_facing_errors():
    """意图 / 编排返回合法 JSON 但不是对象：UserFacingError 而不是崩溃。"""
    from utils.errors import UserFacingError

    runtime, _ = _make_runtime()

    class _ListIntention:
        async def reply(self, context):
            return _Response('["greeting"]')

    runtime.intention_agent = _ListIntention()
    with pytest.raises(UserFacingError, match="意图识别结果格式错误"):
        asyncio.run(runtime.ask("你好"))

    class _BadJsonIntention:
        async def reply(self, context):
            return _Response("这不是 JSON")

    runtime, _ = _make_runtime()
    runtime.intention_agent = _BadJsonIntention()
    with pytest.raises(UserFacingError, match="意图识别结果解析失败"):
        asyncio.run(runtime.ask("你好"))

    runtime, _ = _make_runtime()

    class _ListOrchestrator:
        async def reply(self, intention, on_event=None):
            return _Response("[1, 2]")

    runtime.orchestrator = _ListOrchestrator()
    with pytest.raises(UserFacingError, match="编排结果格式错误"):
        asyncio.run(runtime.ask("你好"))

    runtime, _ = _make_runtime()

    class _BadJsonOrchestrator:
        async def reply(self, intention, on_event=None):
            return _Response("这不是 JSON")

    runtime.orchestrator = _BadJsonOrchestrator()
    with pytest.raises(UserFacingError, match="编排结果解析失败"):
        asyncio.run(runtime.ask("你好"))
    print("✓ 非对象 / 非法 JSON 模型输出使用 UserFacingError")


def test_non_list_schedule_does_not_crash():
    """agent_schedule / intents 不是列表：plan 事件按空调度发出，对话继续。"""
    runtime, _ = _make_runtime()

    class _BadScheduleIntention:
        async def reply(self, context):
            return _Response(
                json.dumps({"agent_schedule": 123, "intents": "greeting"})
            )

    runtime.intention_agent = _BadScheduleIntention()
    events: list[dict] = []
    result = asyncio.run(runtime.ask("你好", on_event=events.append))
    assert result["reply"]
    plan = next(event for event in events if event.get("type") == "plan")
    assert plan["schedule"] == []
    assert plan["intents"] == []
    print("✓ 非列表 agent_schedule / intents 归一为空")


def test_progress_callback_failure_does_not_break_ask():
    """进度回调抛异常不影响对话主流程。"""
    runtime, memory = _make_runtime()

    def boom(event):
        raise RuntimeError("SSE broken")

    result = asyncio.run(runtime.ask("你好", on_event=boom))
    assert result["reply"]
    assert memory.events, "落库不受进度回调影响"
    print("✓ 进度回调异常被吞掉")


def test_close_swallows_backend_close_failure():
    """长期记忆 close 抛异常不影响会话退出。"""
    runtime, memory = _make_runtime()

    def boom():
        raise RuntimeError("connection gone")

    memory.long_term = type("Backend", (), {"close": staticmethod(boom)})()
    runtime.close()  # 不抛异常
    print("✓ close 失败被吞掉")


def test_summary_failure_degrades_to_empty():
    """历史总结失败时上下文仍可构建（只缺总结段）。"""
    runtime, memory = _make_runtime()

    async def boom(max_messages=30):
        raise RuntimeError("summary backend down")

    memory.get_long_term_summary_async = boom  # type: ignore[method-assign]
    result = asyncio.run(runtime.ask("你好"))
    assert result["reply"]
    print("✓ 历史总结失败时降级为空")


def test_rollback_failure_does_not_mask_original_error():
    """暂存回滚自身失败时保留原始错误（编排异常照常上抛）。"""
    runtime, memory = _make_runtime()

    def boom(content):
        raise RuntimeError("rollback blew up")

    memory.rollback_staged_message = boom  # type: ignore[method-assign]

    class _BoomOrchestrator:
        async def reply(self, intention, on_event=None):
            raise RuntimeError("gateway down")

    runtime.orchestrator = _BoomOrchestrator()
    with pytest.raises(RuntimeError, match="gateway down"):
        asyncio.run(runtime.ask("你好"))
    print("✓ 回滚失败不掩盖原始错误")


def test_rollback_slot_cleared_after_completion():
    """完成的回滚任务不能把槽位永远钉住（后续回滚要靠它登记 / drain 等待）。"""
    runtime, memory = _make_runtime()

    async def scenario():
        await runtime._rollback_staged_user_message("旧提问")
        await asyncio.sleep(0)
        assert runtime._pending_rollback is None

    asyncio.run(scenario())
    assert memory.rolled_back == ["旧提问"]
    print("✓ 回滚任务完成后清空槽位")


def test_drain_waits_for_rollback_after_stale_slot():
    """陈旧已完成任务不阻挡新回滚登记：drain 仍等到新回滚落地。"""
    from utils.errors import MemoryPersistenceError

    runtime, memory = _make_runtime()

    async def scenario():
        loop = asyncio.get_running_loop()
        stale = loop.create_future()
        stale.set_result(None)
        runtime._pending_rollback = stale
        runtime._pending_persist = None
        pending = loop.create_future()
        pending.set_exception(MemoryPersistenceError("落库失败"))
        turn_state = {"rollback_requested": False}
        runtime._clear_pending_persist(pending, "你好", turn_state)
        drained = await runtime.drain_pending_persist(timeout_sec=2.0)
        assert drained is True
        assert memory.rolled_back == ["你好"], "drain 返回前回滚必须落地"

    asyncio.run(scenario())
    print("✓ 陈旧回滚任务不再让 drain 提前放行")
def test_failed_turn_usage_settled_in_next_turn():
    """失败回合的 token 记账到下一轮成功回合（用量面板不再永久少记）。"""
    import types

    from config_agentscope import UsageTracker
    from utils.turn_meta import extract_turn_meta

    runtime, memory = _make_runtime()
    tracker = UsageTracker()
    runtime.model = types.SimpleNamespace(usage=tracker)

    class _BoomOrchestrator:
        async def reply(self, intention, on_event=None):
            tracker.calls += 1
            tracker.input_tokens += 100
            tracker.output_tokens += 10
            raise RuntimeError("boom")

    runtime.orchestrator = _BoomOrchestrator()
    with pytest.raises(RuntimeError):
        asyncio.run(runtime.ask("第一轮"))

    class _OkOrchestrator:
        async def reply(self, intention, on_event=None):
            tracker.calls += 1
            tracker.input_tokens += 50
            tracker.output_tokens += 5
            return _Response(json.dumps({"results": []}))

    runtime.orchestrator = _OkOrchestrator()
    result = asyncio.run(runtime.ask("第二轮"))
    assert result["usage"]["total_tokens"] == 165, result["usage"]
    assistant = [content for role, content in memory.events if role == "assistant"][-1]
    meta = extract_turn_meta(assistant)
    assert meta and meta["usage"]["total_tokens"] == 165, meta
    print("✓ 失败回合的 token 用量并入下一轮落库元数据")


def test_cancelled_persist_failure_settles_usage():
    """取消 + 在途落库失败：本轮用量必须记账（不能假定写线程一定成功）。"""
    import types

    from config_agentscope import UsageTracker
    from utils.errors import MemoryPersistenceError

    class _BlockingMemory(_FakeMemory):
        def __init__(self, fail: bool = False, veto: bool = False) -> None:
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()
            self.fail = fail
            self.veto = veto

        def persist_turn(self, user_content, assistant_content, should_commit=None):
            self.started.set()
            self.release.wait(10)
            if self.fail:
                raise MemoryPersistenceError("disk full")
            if self.veto:
                return False
            return super().persist_turn(user_content, assistant_content, should_commit)

    def _run(fail: bool = False, veto: bool = False) -> dict:
        runtime, _ = _make_runtime()
        memory = _BlockingMemory(fail=fail, veto=veto)
        runtime.memory = memory
        tracker = UsageTracker()
        runtime.model = types.SimpleNamespace(usage=tracker)

        class _Orchestrator:
            async def reply(self, intention, on_event=None):
                tracker.calls += 1
                tracker.input_tokens += 100
                tracker.output_tokens += 10
                return _Response(json.dumps({"results": []}))

        runtime.orchestrator = _Orchestrator()

        async def scenario():
            task = asyncio.create_task(runtime.ask("第一轮"))
            await asyncio.to_thread(memory.started.wait, 10)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            memory.release.set()
            assert await runtime.drain_pending_persist(timeout_sec=10) is True
            await asyncio.sleep(0)

        asyncio.run(scenario())
        return runtime._unsettled_usage or {}

    assert _run(fail=True).get("total_tokens") == 110, "落库失败时必须补记用量"
    assert _run(fail=False, veto=True).get("total_tokens") == 110, (
        "墓碑否决同样必须补记用量"
    )
    print("✓ 取消 + 落库失败 / 否决时用量记账不漏")


def test_cancel_before_persist_settles_usage():
    """取消发生在记账 try 之前（意图调用中）：用量同样不能丢。"""
    import types

    from config_agentscope import UsageTracker

    runtime, memory = _make_runtime()
    tracker = UsageTracker()
    runtime.model = types.SimpleNamespace(usage=tracker)

    class _SlowIntention:
        def __init__(self) -> None:
            self.entered = asyncio.Event()

        async def reply(self, context):
            tracker.calls += 1
            tracker.input_tokens += 70
            tracker.output_tokens += 7
            self.entered.set()
            await asyncio.sleep(30)
            raise AssertionError("不应走到这里")

    intention = _SlowIntention()
    runtime.intention_agent = intention

    async def scenario():
        task = asyncio.create_task(runtime.ask("你好"))
        await asyncio.wait_for(intention.entered.wait(), timeout=10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert (runtime._unsettled_usage or {}).get("total_tokens") == 77
    assert memory.staged == [], "未暂存提问，不应有残留"
    print("✓ 意图阶段取消的用量同样记账")


def test_cancel_during_deferred_commit_does_not_double_usage():
    """落库已成功后取消落在延迟记忆提交期间：用量不能再记入下一轮。"""
    import types

    from config_agentscope import UsageTracker

    runtime, memory = _make_runtime()
    tracker = UsageTracker()
    runtime.model = types.SimpleNamespace(usage=tracker)
    commit_entered = asyncio.Event()
    release = asyncio.Event()

    class _SlowCommitOrch:
        async def reply(self, intention, on_event=None, defer_memory=False):
            tracker.calls += 1
            tracker.input_tokens += 100
            tracker.output_tokens += 10
            return _Response(json.dumps({"results": []}))

        async def commit_deferred_memory(self):
            commit_entered.set()
            await release.wait()
            return True

    runtime.orchestrator = _SlowCommitOrch()

    async def scenario():
        task = asyncio.create_task(runtime.ask("你好"))
        await asyncio.wait_for(commit_entered.wait(), timeout=10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert not (getattr(runtime, "_unsettled_usage", None) or {}).get(
        "total_tokens"
    ), "助手消息已含 _meta.usage，取消延迟提交不能再记一次"
    print("✓ 取消延迟记忆提交不重复记账用量")


def test_memory_without_rollback_hook_is_tolerated():
    """记忆实现没有 rollback_staged_message 时失败路径照常返回。"""
    runtime, memory = _make_runtime()
    memory.rollback_staged_message = None  # type: ignore[method-assign]

    class _BoomOrchestrator:
        async def reply(self, intention, on_event=None):
            raise RuntimeError("gateway down")

    runtime.orchestrator = _BoomOrchestrator()
    with pytest.raises(RuntimeError, match="gateway down"):
        asyncio.run(runtime.ask("你好"))
    print("✓ 缺少回滚钩子的记忆实现兼容")


def test_memory_persistence_error_not_counted_as_model_failure():
    """记忆写盘错误不记入熔断（多次磁盘故障不能触发全局「服务不可用」）。"""
    from utils.circuit_breaker import CircuitBreaker
    from utils.errors import MemoryPersistenceError

    breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout_sec=60, half_open_successes=1
    )
    runtime, _ = _make_runtime()
    runtime.circuit_breaker = breaker

    class _DiskBoomOrchestrator:
        async def reply(self, intention, on_event=None):
            raise MemoryPersistenceError("磁盘已满")

    runtime.orchestrator = _DiskBoomOrchestrator()
    with pytest.raises(MemoryPersistenceError):
        asyncio.run(runtime.ask("你好"))
    assert breaker.allow_call() is True, "磁盘错误不计入熔断"
    print("✓ 记忆写盘错误不计入熔断")


def test_open_circuit_short_circuits_ask():
    """熔断打开时 ask 立即抛 CircuitOpenError（不再发起任何调用）。"""
    from utils.circuit_breaker import CircuitBreaker, CircuitOpenError

    breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout_sec=60, half_open_successes=1
    )
    breaker.record_failure()
    runtime, memory = _make_runtime()
    runtime.circuit_breaker = breaker
    with pytest.raises(CircuitOpenError):
        asyncio.run(runtime.ask("你好"))
    assert memory.events == []
    print("✓ 熔断打开时快速失败")


def test_model_call_failure_counts_towards_breaker(monkeypatch):
    """模型调用失败要计入熔断（达到阈值后全局快速失败）。"""
    from config import RESILIENCE_CONFIG
    from utils.circuit_breaker import CircuitBreaker

    monkeypatch.setitem(RESILIENCE_CONFIG, "max_retries", 0)
    monkeypatch.setitem(RESILIENCE_CONFIG, "retry_base_delay_sec", 0.0)
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout_sec=60, half_open_successes=1
    )
    runtime, _ = _make_runtime()
    runtime.circuit_breaker = breaker

    class _BoomIntention:
        async def reply(self, context):
            raise RuntimeError("gateway down")

    runtime.intention_agent = _BoomIntention()
    with pytest.raises(RuntimeError):
        asyncio.run(runtime.ask("你好"))
    assert breaker.allow_call() is False, "模型调用失败要计入熔断"
    print("✓ 模型调用失败计入熔断")


def test_normalized_schedule_filters_invalid_entries():
    """plan 事件的调度计划：跳过非对象 / 空名称，非法优先级归 0。"""
    runtime, _ = _make_runtime()
    schedule = runtime._normalized_schedule(
        [
            "不是对象",
            {"agent_name": "  "},
            {"agent_name": "Plan-Trip", "priority": "high"},
            {"agent_name": "query-info", "priority": 2, "reason": "查询"},
        ]
    )
    assert len(schedule) == 2, schedule
    assert [item["priority"] for item in schedule] == [0, 2], schedule
    assert all(item["agent_name"] for item in schedule)
    assert schedule[1]["reason"] == "查询"
    print("✓ 调度计划过滤非法条目")


def test_empty_short_term_backfills_current_session_history():
    """重启 / LRU 淘汰后短期记忆为空：从长期记忆补回本会话最近几轮并写回短期记忆。"""
    runtime, memory = _make_runtime()

    class _EmptyShortTerm:
        def __init__(self):
            self.added = []

        def get_recent_context(self, n_turns=5):
            return list(self.added[-n_turns * 2 :])

        def add_message(self, role, content, metadata=None):
            self.added.append({"role": role, "content": content})

    class _LongTerm:
        def __init__(self):
            self.history_reads = 0

        def get_preference(self):
            return {}

        def get_chat_history(
            self, limit=None, session_id=None, exclude_session_id=None
        ):
            assert limit == 10
            assert session_id == runtime.session_id
            self.history_reads += 1
            return [
                {"role": "user", "content": "帮我把行程改到周五"},
                {"role": "assistant", "content": "已改到周五"},
            ]

    short_term = _EmptyShortTerm()
    long_term = _LongTerm()
    memory.short_term = short_term
    memory.long_term = long_term
    asyncio.run(runtime.ask("那住宿呢"))
    contents = "\n".join(
        m.content for m in runtime.intention_agent.context
    )
    assert "帮我把行程改到周五" in contents, contents
    assert "已改到周五" in contents, contents
    assert [item["content"] for item in short_term.added] == [
        "帮我把行程改到周五",
        "已改到周五",
    ], "回填内容必须写回短期记忆（编排器直接从那里读取）"

    # 第二轮：短期记忆已非空，不能再读长期历史，且上下文仍包含回填内容
    asyncio.run(runtime.ask("第二天呢"))
    assert long_term.history_reads == 1, "回填写回后下一轮不应重复读取长期历史"
    second_context = "\n".join(m.content for m in runtime.intention_agent.context)
    assert "帮我把行程改到周五" in second_context, second_context
    print("✓ 短期记忆为空时从长期记忆回填本会话上下文并写回")


def test_non_empty_short_term_skips_long_term_backfill():
    """短期记忆有内容时不额外读长期历史（不放大每轮记忆 IO）。"""
    runtime, memory = _make_runtime()
    calls = {"history": 0}

    class _LongTerm:
        def get_preference(self):
            return {}

        def get_chat_history(self, **kwargs):
            calls["history"] += 1
            return []

    memory.long_term = _LongTerm()
    asyncio.run(runtime.ask("你好"))
    assert calls["history"] == 0
    print("✓ 短期记忆非空时不回填长期历史")


def test_runtime_uses_default_data_root_for_long_term(monkeypatch, tmp_path):
    """对话运行时的长期记忆必须落在 default_data_root()/memory。

    MemoryManager 默认 "data/memory" 锚定包根（wheel 下为 site-packages），
    与 Web 面板 / CLI 使用的数据目录分叉：对话写入的会话、偏好、行程在
    面板 / 导出 / 删除里都不可见。
    """
    from context import memory_manager as memory_module
    from utils.paths import default_data_root

    monkeypatch.setenv("ALIGO_DATA_DIR", str(tmp_path))
    captured = {}

    class _Store:
        pass

    def _fake_long_term(user_id, storage_path):
        captured["user_id"] = user_id
        captured["storage_path"] = storage_path
        return _Store()

    monkeypatch.setattr(
        memory_module, "create_long_term_memory", _fake_long_term
    )
    monkeypatch.setattr(
        memory_module, "create_short_term_memory", lambda *args, **kwargs: object()
    )
    ConversationRuntime(user_id="u-root", session_id="s-root", model=None)
    assert captured["user_id"] == "u-root"
    assert captured["storage_path"] == str(default_data_root() / "memory")
    assert captured["storage_path"].startswith(str(tmp_path))
    print("✓ 对话运行时长期记忆跟随 default_data_root")

if __name__ == "__main__":
    mp = pytest.MonkeyPatch()
    try:
        test_ask_runs_memory_io_off_event_loop()
        test_failed_orchestration_leaves_no_long_term_user_turn()
        test_malformed_recent_context_entries_are_skipped()
        test_ask_survives_memory_snapshot_shape()
        test_ask_releases_half_open_probe_on_early_failure(mp)
    finally:
        mp.undo()
    test_parse_failure_is_user_facing_error()
    test_persist_guard_reaches_memory_write()
    test_deferred_memory_committed_after_successful_persist()
    test_deferred_memory_skipped_when_persist_vetoed()
    test_persist_veto_rolls_back_staged_question()
    test_persist_failure_rolls_back_staged_user_message()
    test_summary_inputs_off_event_loop()
    test_model_output_with_lone_surrogate_is_sanitized()
    test_non_dict_model_outputs_are_user_facing_errors()
    test_non_list_schedule_does_not_crash()
    test_progress_callback_failure_does_not_break_ask()
    test_close_swallows_backend_close_failure()
    test_summary_failure_degrades_to_empty()
    test_empty_short_term_backfills_current_session_history()
    test_non_empty_short_term_skips_long_term_backfill()
    test_rollback_failure_does_not_mask_original_error()
    test_memory_without_rollback_hook_is_tolerated()
    test_cancel_during_deferred_commit_does_not_double_usage()
    test_rollback_slot_cleared_after_completion()
    test_drain_waits_for_rollback_after_stale_slot()
    test_failed_turn_usage_settled_in_next_turn()
    test_memory_persistence_error_not_counted_as_model_failure()
    test_open_circuit_short_circuits_ask()
    test_model_call_failure_counts_towards_breaker(mp)
    test_normalized_schedule_filters_invalid_entries()
    print("\n全部通过")
