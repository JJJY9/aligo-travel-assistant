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
    for name in ("reload", "preferences", "recent", "add-user", "add-assistant"):
        assert name in memory.threads, f"{name} 未被调用"
        assert memory.threads[name] != loop_thread, (
            f"{name} 在事件循环线程执行，会阻塞其它连接"
        )
    assert [role for role, _ in memory.events] == ["user", "assistant"]
    # 偏好进了系统提示，最近对话按顺序跟在后面
    system_msg = runtime.intention_agent.context[0]
    assert system_msg.role == "system"
    assert "北京" in system_msg.content
    assert "历史总结" in system_msg.content
    roles = [msg.role for msg in runtime.intention_agent.context]
    assert roles[-2:] == ["user", "user"]
    print("✓ ask 的记忆读写全部离开事件循环线程")


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


if __name__ == "__main__":
    test_ask_runs_memory_io_off_event_loop()
    test_ask_survives_memory_snapshot_shape()
    print("\n全部通过")
