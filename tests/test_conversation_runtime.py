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
        return {"home_location": "北京", "empty_value": ""}

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
    assert memory.persist_guard is guard
    print("✓ 落库守卫传递到记忆写入")


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
        async def reply(self, intention, on_event=None):
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
    print("✓ 模型输出的孤立代理项在事件 / 回复 / 落库前转义")


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
    test_persist_veto_rolls_back_staged_question()
    test_persist_failure_rolls_back_staged_user_message()
    test_summary_inputs_off_event_loop()
    test_model_output_with_lone_surrogate_is_sanitized()
    print("\n全部通过")
