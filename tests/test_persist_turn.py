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

        def add_chat_turn(
            self, user_content, assistant_content, session_id=None, should_commit=None
        ):
            self.turns.append((user_content, assistant_content, session_id))
            return True

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

    def __init__(self, fail=False, veto=False, block_rollback=False):
        self.long_term = self
        self.short_term = self
        self.started = threading.Event()
        self.release = threading.Event()
        self.messages = []
        self.staged = []
        self.stage_calls = 0
        self.events = []
        self.rolled_back = []
        self.rollback_seen = threading.Event()
        self.rollback_started = threading.Event()
        self.allow_rollback = threading.Event()
        if not block_rollback:
            self.rollback_started.set()
        self.fail = fail
        self.veto = veto
        self.block_rollback = block_rollback
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
        self.stage_calls += 1
        self.events.append(("stage", content))
        self.staged.append((role, content))

    def rollback_staged_message(self, content):
        self.rolled_back.append(content)
        self.rollback_seen.set()
        self.events.append(("rollback", content))
        self.rollback_started.set()
        if self.block_rollback:
            self.allow_rollback.wait(5)
        # 与生产 MemoryManager.rollback_staged_message 同语义：仅当末尾仍是
        # 这条未回答的用户提问时才删除（不向前搜索，否则会掩盖串轮缺陷）
        if self.staged and self.staged[-1] == ("user", content):
            self.staged.pop()
            return True
        return False

    def persist_turn(self, user_content, assistant_content, should_commit=None):
        self.threads["persist"] = threading.get_ident()
        self.started.set()
        self.release.wait(10)
        self.events.append(("persist", user_content))
        if self.fail:
            raise MemoryPersistenceError("disk full")
        if self.veto:
            return False
        self.messages.append(("user", user_content))
        self.messages.append(("assistant", assistant_content))
        return True


def _make_runtime(memory):
    runtime = ConversationRuntime.__new__(ConversationRuntime)
    runtime.user_id = "u-turn"
    runtime.session_id = "s-turn"
    runtime.model = None
    runtime.circuit_breaker = None
    runtime._probe_recorded = False
    runtime._pending_persist = None
    runtime._persist_guard = None
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


def test_cancel_during_persist_does_not_rollback_staged():
    """取消落在在途落库时：不回滚暂存提问，写线程继续提交整轮问答。"""
    memory = _BlockingMemory()
    runtime = _make_runtime(memory)

    async def scenario():
        task = asyncio.create_task(runtime.ask("你好"))
        await asyncio.to_thread(memory.started.wait, 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # 落库线程仍在途（done callback 还没清空引用）：回滚必须被跳过，
        # 否则写线程提交的整轮会与回滚线程互相竞态
        assert runtime._pending_persist is not None
        assert memory.rollback_seen.wait(0.2) is False, "在途落库期间不得触发回滚"
        assert memory.rolled_back == []
        assert ("user", "你好") in memory.staged, "暂存提问必须保留给写线程提交"
        memory.release.set()
        assert await runtime.drain_pending_persist(timeout_sec=10) is True
        assert memory.rolled_back == []

    asyncio.run(scenario())
    assert [role for role, _ in memory.messages[-2:]] == ["user", "assistant"], (
        "整轮问答必须由写线程完整提交"
    )
    print("✓ 取消落在在途落库时回滚被跳过")


def test_json_add_chat_turn_guard_vetoes_write():
    """落库守卫返回 False（会话刚被删除）：整轮不写入。"""
    memory = LongTermMemory("u-guard", tempfile.mkdtemp(prefix="chailv_turn_"))
    assert (
        memory.add_chat_turn("问题", "回答", "s-del", should_commit=lambda: False)
        is False
    )
    assert memory.get_chat_history(limit=None) == []
    assert memory.get_statistics()["total_messages"] == 0
    print("✓ 落库守卫否决时整轮不写入（JSON）")


def test_persist_turn_veto_skips_assistant_stage():
    """守卫否决时不补暂存助手副本（短期记忆不留孤立回答）。"""
    manager = MemoryManager("u-guard2", "s-del", storage_path=tempfile.mkdtemp())
    assert (
        manager.persist_turn("问题", "回答", should_commit=lambda: False) is False
    )
    assert manager.long_term.get_chat_history(limit=None) == []
    assert manager.short_term.get_recent_context() == []
    print("✓ 守卫否决时短期记忆不补助手副本")


def test_persist_turn_defaults_to_shared_tombstone_guard(monkeypatch):
    """CLI 直接调用（未注入守卫）时默认查共享墓碑：已删除会话不写回。"""
    from utils import tombstones

    calls = []

    def deleted(user_id, session_id):
        calls.append((user_id, session_id))
        return True

    monkeypatch.setattr(tombstones, "is_session_deleted", deleted)
    manager = MemoryManager("u-cli", "s-del", storage_path=tempfile.mkdtemp())
    assert manager.persist_turn("在途提问", "在途回答") is False
    assert calls and calls[0] == ("u-cli", "s-del"), (
        "默认守卫必须用本会话的 user/session"
    )
    assert manager.long_term.get_chat_history(limit=None) == []
    assert manager.short_term.get_recent_context() == []
    print("✓ 默认回退共享墓碑：已删除会话不被 CLI 在途轮次写回")


def test_persist_turn_default_guard_commits_when_not_deleted(monkeypatch):
    """墓碑不存在时默认守卫放行，正常一次写入整轮。"""
    from utils import tombstones

    monkeypatch.setattr(tombstones, "is_session_deleted", lambda u, s, force=False: False)
    manager = MemoryManager("u-ok", "s-ok", storage_path=tempfile.mkdtemp())
    assert manager.persist_turn("问题", "回答") is True
    assert [item["role"] for item in manager.long_term.get_chat_history()] == [
        "user",
        "assistant",
    ]
    print("✓ 默认守卫在未删除时正常写入")


def test_drain_without_pending_returns_immediately():
    memory = _BlockingMemory()
    runtime = _make_runtime(memory)
    assert asyncio.run(runtime.drain_pending_persist()) is True
    print("✓ 无在途落库时 drain 立即返回")


def test_cancel_with_failed_persist_rolls_back_staged():
    """取消后在途落库失败：完成回调补回滚，不留下无回答的孤儿提问。"""
    memory = _BlockingMemory(fail=True)
    runtime = _make_runtime(memory)

    async def scenario():
        task = asyncio.create_task(runtime.ask("你好"))
        await asyncio.to_thread(memory.started.wait, 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        memory.release.set()
        # 先只等完成回调补回滚（不调用 drain），确认回调路径独立生效；
        # 回滚线程在 set() 之后才移除暂存项，落地断言放到 drain 之后
        assert await asyncio.to_thread(memory.rollback_seen.wait, 5)
        assert memory.rolled_back == ["你好"]
        # drain 只确认在途落库 / 回滚结束；回滚已由回调完成，不得重复回滚
        assert await runtime.drain_pending_persist(timeout_sec=10) is True
        assert ("user", "你好") not in memory.staged, "drain 返回前回滚必须已落地"

    asyncio.run(scenario())
    assert memory.rolled_back == ["你好"]
    assert ("user", "你好") not in memory.staged
    assert memory.messages == []
    print("✓ 取消 + 在途落库失败补回滚暂存提问")


def test_cancel_with_vetoed_persist_rolls_back_staged():
    """取消后在途落库被守卫否决（会话已删除）：同样回滚暂存提问。"""
    memory = _BlockingMemory(veto=True)
    runtime = _make_runtime(memory)

    async def scenario():
        task = asyncio.create_task(runtime.ask("你好"))
        await asyncio.to_thread(memory.started.wait, 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        memory.release.set()
        assert await asyncio.to_thread(memory.rollback_seen.wait, 5)
        assert memory.rolled_back == ["你好"]
        assert await runtime.drain_pending_persist(timeout_sec=10) is True
        assert ("user", "你好") not in memory.staged, "drain 返回前回滚必须已落地"

    asyncio.run(scenario())
    assert memory.rolled_back == ["你好"]
    assert ("user", "你好") not in memory.staged
    print("✓ 取消 + 落库守卫否决补回滚暂存提问")


def test_next_turn_waits_for_previous_rollback():
    """迟到的回滚不能删掉下一轮文本相同的提问：新回合先等上一轮回滚。"""
    memory = _BlockingMemory(veto=True, block_rollback=True)
    runtime = _make_runtime(memory)

    async def scenario():
        first = asyncio.create_task(runtime.ask("同一个问题"))
        await asyncio.to_thread(memory.started.wait, 10)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        memory.release.set()
        # 等完成回调触发的回滚真正开始（随后被 allow_rollback 挡住）
        assert await asyncio.to_thread(memory.rollback_started.wait, 5)
        second = asyncio.create_task(runtime.ask("同一个问题"))
        await asyncio.sleep(0.2)
        assert memory.stage_calls == 1, "上一轮回滚结束前不能暂存新提问"
        memory.allow_rollback.set()
        await second

    asyncio.run(scenario())
    stages = [i for i, event in enumerate(memory.events) if event[0] == "stage"]
    rollbacks = [
        i for i, event in enumerate(memory.events) if event[0] == "rollback"
    ]
    assert len(stages) == 2 and len(rollbacks) == 2, memory.events
    assert rollbacks[0] < stages[1], "第二次暂存必须排在第一次回滚之后"
    assert ("user", "同一个问题") not in memory.staged
    print("✓ 新回合等待上一轮回滚完成")


def test_next_turn_waits_for_inflight_persist_after_drain_timeout():
    """drain 超时（Web 入口随后释放用户锁）后旧落库仍在途：

    下一轮开始前必须先等它收尾并补回滚，再暂存本轮提问——否则旧回滚按
    内容匹配会删掉新一轮同一文本的暂存提问。
    """
    memory = _BlockingMemory(veto=True)
    runtime = _make_runtime(memory)

    async def scenario():
        first = asyncio.create_task(runtime.ask("同一个问题"))
        await asyncio.to_thread(memory.started.wait, 10)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        # 落库线程仍被挡住：drain 只能超时返回 False（模拟 Web 入口丢弃返回值）
        assert await runtime.drain_pending_persist(timeout_sec=0.05) is False
        second = asyncio.create_task(runtime.ask("同一个问题"))
        await asyncio.sleep(0.2)
        assert memory.stage_calls == 1, "上一轮落库未收尾前不能暂存新提问"
        memory.release.set()
        await second

    asyncio.run(scenario())
    stages = [i for i, event in enumerate(memory.events) if event[0] == "stage"]
    rollbacks = [
        i for i, event in enumerate(memory.events) if event[0] == "rollback"
    ]
    assert len(stages) == 2, memory.events
    assert rollbacks and rollbacks[0] < stages[1], "旧回滚必须在新暂存之前完成"
    assert ("user", "同一个问题") not in memory.staged
    print("✓ drain 超时后下一轮仍等待旧落库 / 回滚")


def test_memory_manager_persist_guard_vetoes_on_epoch_change(monkeypatch):
    """删除递增的会话纪元：删除前启动的回合即使墓碑已撤销也不能落库。"""
    from context.memory_manager import MemoryManager
    from utils import tombstones

    manager = object.__new__(MemoryManager)
    manager.user_id = "u1"
    manager.session_id = "s1"

    epochs = {"value": "2"}
    monkeypatch.setattr(tombstones, "is_session_deleted", lambda u, s, force=False: False)
    monkeypatch.setattr(
        tombstones, "get_session_epoch", lambda u, s: epochs["value"]
    )
    assert manager.persist_guard("1", None)() is False, (
        "回合开始后会话被删除（纪元变化）必须否决"
    )
    assert manager.persist_guard("2", None)() is True
    # Web 注入的 base 守卫仍然优先
    assert manager.persist_guard("2", lambda: False)() is False
    # 无 Redis（纪元一直 None）：行为与旧版一致
    epochs["value"] = None
    assert manager.persist_guard(None, None)() is True
    print("✓ 落库守卫按会话纪元变化否决")


if __name__ == "__main__":
    _mp = pytest.MonkeyPatch()
    try:
        test_json_add_chat_turn_writes_pair_once()
        test_json_add_chat_turn_rolls_back_pair_on_persist_failure(_mp)
        test_json_add_chat_turn_guard_vetoes_write()
        test_memory_manager_persist_turn_delegates_and_stages_assistant()
        test_persist_turn_veto_skips_assistant_stage()
        test_persist_turn_defaults_to_shared_tombstone_guard(_mp)
        test_persist_turn_default_guard_commits_when_not_deleted(_mp)
        test_memory_manager_persist_guard_vetoes_on_epoch_change(_mp)
        test_cancel_during_persist_drains_with_both_messages()
        test_cancel_during_persist_does_not_rollback_staged()
        test_cancel_with_failed_persist_rolls_back_staged()
        test_cancel_with_vetoed_persist_rolls_back_staged()
        test_next_turn_waits_for_previous_rollback()
        test_next_turn_waits_for_inflight_persist_after_drain_timeout()
        test_drain_without_pending_returns_immediately()
    finally:
        _mp.undo()
    print("\n全部通过")
