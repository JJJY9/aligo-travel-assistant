"""整轮问答原子落库测试（取消 / 失败不产生孤儿提问）

覆盖：
1. JSON 后端 add_chat_turn：用户 + 助手一次写回，写盘失败两条一起回滚；
2. MemoryManager.persist_turn：委托后端原子提交，只补暂存助手副本；
3. ConversationRuntime：取消落在落库阶段时写线程仍完成，
   drain_pending_persist 等它结束才返回（SSE / HTTP 释放用户锁前调用）。

运行：python tests/test_persist_turn.py
"""
import asyncio
import json
import os
import sys
import tempfile
import threading

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from agents.conversation import ConversationRuntime
from context.long_term_memory import LongTermMemory
from context.memory_manager import MemoryManager
from utils.errors import MemoryPersistenceError


def test_json_add_chat_turn_writes_pair_once():
    tmp = tempfile.mkdtemp(prefix="chailv_turn_")
    memory = LongTermMemory("u-turn", tmp)
    memory.add_chat_turn("去上海出差", "已为你规划", "s-turn")

    history = memory.get_chat_history(session_id="s-turn")
    assert [item["role"] for item in history] == ["user", "assistant"]
    assert [item["content"] for item in history] == ["去上海出差", "已为你规划"]
    assert memory.get_statistics()["total_messages"] == 2

    reopened = LongTermMemory("u-turn", tmp)
    assert len(reopened.get_chat_history(session_id="s-turn")) == 2, "两条必须一起落盘"
    print("✓ JSON add_chat_turn 用户 + 助手一次写回")


def test_json_add_chat_turn_rolls_back_pair_on_persist_failure(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="chailv_turn_")
    memory = LongTermMemory("u-turn", tmp)
    memory.add_chat_message("user", "旧问题", "s-turn")

    monkeypatch.setattr(memory, "_save", lambda: False)
    with pytest.raises(MemoryPersistenceError):
        memory.add_chat_turn("新问题", "新回答", "s-turn")

    history = memory.get_chat_history(session_id="s-turn")
    assert [item["content"] for item in history] == ["旧问题"], (
        "写盘失败时用户与助手两条都必须回滚，不能留下孤儿提问"
    )
    assert memory.get_statistics()["total_messages"] == 1
    print("✓ JSON add_chat_turn 写盘失败两条一起回滚")


class _ShortTerm:
    def __init__(self):
        self.messages = []

    def add_message(self, role, content, metadata=None):
        self.messages.append((role, content))


def test_memory_manager_persist_turn_delegates_and_stages_assistant():
    class _TurnStore:
        snapshot_based = False

        def __init__(self):
            self.turns = []

        def add_chat_turn(self, user_content, assistant_content, session_id=None):
            self.turns.append((user_content, assistant_content, session_id))

    manager = MemoryManager("u1", "s1", storage_path=tempfile.mkdtemp())
    store = _TurnStore()
    manager.long_term = store
    manager.short_term = _ShortTerm()

    manager.persist_turn("问题", "回答")

    assert store.turns == [("问题", "回答", "s1")], "必须委托后端一次提交整轮"
    assert manager.short_term.messages == [("assistant", "回答")], (
        "用户消息已在编排前暂存，这里只补助手副本"
    )
    print("✓ MemoryManager.persist_turn 一次提交并暂存助手副本")


class _BlockingMemory:
    """persist_turn 阻塞在事件上，模拟落库线程仍在执行时协程被取消。"""

    def __init__(self):
        self.long_term = self
        self.short_term = self
        self.started = threading.Event()
        self.release = threading.Event()
        self.messages = []
        self.threads = {}

    def reload_long_term(self):
        pass

    def get_preference(self):
        return {}

    def get_recent_context(self, n_turns=5):
        return []

    async def get_long_term_summary_async(self, max_messages=30):
        return ""

    def stage_message(self, role, content, metadata=None):
        self.messages.append((role, content))

    def persist_turn(self, user_content, assistant_content):
        self.threads["persist"] = threading.get_ident()
        self.started.set()
        self.release.wait(10)
        self.messages.append(("user", user_content))
        self.messages.append(("assistant", assistant_content))


def _make_runtime(memory):
    runtime = ConversationRuntime.__new__(ConversationRuntime)
    runtime.user_id = "u-turn"
    runtime.session_id = "s-turn"
    runtime.model = None
    runtime.circuit_breaker = None
    runtime._probe_recorded = False
    runtime._pending_persist = None
    runtime.agent_cache = {}

    class _Intention:
        async def reply(self, context):
            return type(
                "Msg",
                (),
                {"content": json.dumps({"agent_schedule": []}, ensure_ascii=False)},
            )()

    class _Orchestrator:
        async def reply(self, intention, on_event=None):
            return type(
                "Msg",
                (),
                {
                    "content": json.dumps(
                        {"results": [{"agent_name": "GreetingAgent", "status": "success"}]},
                        ensure_ascii=False,
                    )
                },
            )()

    runtime.intention_agent = _Intention()
    runtime.orchestrator = _Orchestrator()
    runtime.memory = memory
    return runtime


def test_cancel_during_persist_drains_with_both_messages():
    """取消落在落库阶段：写线程仍完成整轮，drain 等它结束才返回。"""
    memory = _BlockingMemory()
    runtime = _make_runtime(memory)
    loop_thread = threading.get_ident()

    async def scenario():
        task = asyncio.create_task(runtime.ask("你好"))
        await asyncio.to_thread(memory.started.wait, 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runtime._pending_persist is not None, "在途落库任务必须可被 drain 到"
        # 写线程未结束前 drain 不能提前返回
        assert await runtime.drain_pending_persist(timeout_sec=0.05) is False
        memory.release.set()
        assert await runtime.drain_pending_persist(timeout_sec=10) is True
        await asyncio.sleep(0)  # 让 done callback 清理引用
        assert runtime._pending_persist is None

    asyncio.run(scenario())
    assert memory.threads["persist"] != loop_thread, "落库必须在事件循环线程之外执行"
    assert [role for role, _ in memory.messages[-2:]] == ["user", "assistant"], (
        "取消不能把整轮问答拆成只有提问的孤儿轮"
    )
    print("✓ 取消落在落库阶段：drain 等待整轮写完")


def test_drain_without_pending_returns_immediately():
    memory = _BlockingMemory()
    runtime = _make_runtime(memory)
    assert asyncio.run(runtime.drain_pending_persist()) is True
    print("✓ 无在途落库时 drain 立即返回")


if __name__ == "__main__":
    _mp = pytest.MonkeyPatch()
    try:
        test_json_add_chat_turn_writes_pair_once()
        test_json_add_chat_turn_rolls_back_pair_on_persist_failure(_mp)
        test_memory_manager_persist_turn_delegates_and_stages_assistant()
        test_cancel_during_persist_drains_with_both_messages()
        test_drain_without_pending_returns_immediately()
    finally:
        _mp.undo()
    print("\n全部通过")
