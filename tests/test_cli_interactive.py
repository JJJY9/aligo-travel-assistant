"""
交互式 CLI 健壮性测试（无需 API Key、不访问网络）

覆盖：stdin EOF 正常退出、UTF-8 管道输入、富文本转义（方括号 / 关闭标签）、
非标准行程结构渲染、读命令刷新长期记忆快照。

运行：python tests/test_cli_interactive.py
"""
import asyncio
import io
import json
import os
import subprocess
import sys

import pytest
from rich.console import Console

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

import cli


def _run_cli(stdin_bytes: bytes) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "cli.py"],
        input=stdin_bytes,
        cwd=project_root,
        capture_output=True,
        timeout=60,
    )


def test_eof_at_user_prompt_exits_cleanly():
    """空 stdin（管道 / IDE）应正常退出，而不是 traceback 后死循环。"""
    result = _run_cli(b"")
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    assert result.returncode == 0, output
    assert "未检测到输入" in output
    assert "Traceback" not in output
    print("✓ stdin EOF 正常退出")


def test_eof_at_main_prompt_breaks_loop(monkeypatch):
    """主循环 stdin 结束时应退出，而不是每轮打印错误刷屏。"""
    import asyncio
    import types

    agent = cli.AligoCLI()
    buffer = io.StringIO()
    agent.console = Console(file=buffer, force_terminal=False, width=100)

    async def fake_init():
        agent.user_id = "u1"
        agent.memory_manager = types.SimpleNamespace(
            end_session=lambda: None
        )

    monkeypatch.setattr(agent, "initialize_system", fake_init)
    monkeypatch.setattr(agent, "print_banner", lambda: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    asyncio.run(agent.run())
    assert "未检测到输入" in buffer.getvalue()
    print("✓ 主循环 EOF 正常退出（不再刷屏）")


@pytest.mark.skipif(
    sys.platform != "win32", reason="GBK 管道只在 Windows 中文 locale 下有区分度"
)
def test_gbk_piped_input_keeps_locale_decoding():
    """原生 cmd 的 GBK 管道输入不能被强行按 UTF-8 解码成乱码。"""
    script = (
        "import sys; sys.path.insert(0, r'%s'); "
        "from utils.console import configure_stdio; configure_stdio(); "
        "print(input())" % project_root
    )
    result = subprocess.run(
        [sys.executable, "-X", "utf8=0", "-c", script],
        input="我要去北京".encode("gbk"),
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert result.stdout.decode("utf-8").strip() == "我要去北京"
    print("✓ GBK 管道输入保持 locale 解码")


def test_utf8_piped_input_not_mojibake():
    """Windows GBK locale 下，UTF-8 管道输入应被正确解码。"""
    script = (
        "import sys; sys.path.insert(0, r'%s'); "
        "from utils.console import configure_stdio; configure_stdio(); "
        "print(input())" % project_root
    )
    result = subprocess.run(
        [sys.executable, "-X", "utf8=0", "-c", script],
        input="我要去北京".encode("utf-8"),
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert result.stdout.decode("utf-8").strip() == "我要去北京"
    print("✓ UTF-8 管道输入正确解码")


def _capture_console() -> tuple[cli.AligoCLI, io.StringIO]:
    agent = cli.AligoCLI()
    buffer = io.StringIO()
    agent.console = Console(file=buffer, force_terminal=False, width=100)
    return agent, buffer


def test_human_response_escapes_markup():
    """回答里的方括号 / 关闭标签不能被 rich 当作标记（会吞字或抛异常）。"""
    agent, buffer = _capture_console()
    shown = agent._generate_human_response(
        [
            {
                "agent_name": "rag_knowledge",
                "status": "success",
                "data": {"answer": "参考 [https://example.com] 与 [/red] 标签"},
            }
        ]
    )
    assert shown is True
    text = buffer.getvalue()
    assert "https://example.com" in text
    assert "[/red]" in text

    # 模型给出的智能体名同样可能带标记：不能在汇总行抛 MarkupError
    agent2, buffer2 = _capture_console()
    agent2._display_agents_called(
        {"results": [{"agent_name": "bad[/red]", "status": "success"}]}
    )
    assert "bad[/red]" in buffer2.getvalue()
    print("✓ 回答 / 智能体名中的方括号与关闭标签按原文显示")


def test_human_response_tolerates_malformed_itinerary():
    """非标准行程结构（字符串 / 字典 daily_plans / 非字典 meals）不能中断整轮。"""
    payloads = [
        {"itinerary": "第一天：北京出发，高铁前往杭州。"},
        {"itinerary": {"daily_plans": {"第1天": [{"time": "09:00", "activity": "出发"}]}}},
        {
            "itinerary": {
                "daily_plans": [
                    {"day": 1, "activities": [{"time": "09:00", "activity": "出发"}], "meals": ["午餐"]}
                ],
                "notes": "带伞",
            }
        },
        {"itinerary": ["北京", "上海"]},
        {"itinerary": "", "answer": "缺少出发地，请补充"},
    ]
    for payload in payloads:
        agent, buffer = _capture_console()
        assert agent._generate_human_response(
            [{"agent_name": "itinerary_planning", "status": "success", "data": payload}]
        )
        assert buffer.getvalue().strip(), payload
    # 空字符串 itinerary 应回退到通用字段，而不是只打印空标题
    agent, buffer = _capture_console()
    agent._generate_human_response(
        [
            {
                "agent_name": "itinerary_planning",
                "status": "success",
                "data": {"itinerary": "", "answer": "缺少出发地，请补充"},
            }
        ]
    )
    assert "缺少出发地" in buffer.getvalue()
    print("✓ 非标准行程结构渲染不崩溃（空字符串回退通用字段）")


def test_read_commands_reload_snapshot():
    """preferences / history / status 前应刷新长期记忆，避免导出旧快照。"""
    agent = cli.AligoCLI()
    reloaded = {"n": 0}

    class _LongTerm:
        def get_preference(self):
            return {"home_location": "北京"}

        def get_trip_history(self, limit=10):
            return []

    class _ShortTerm:
        def get_recent_context(self, n_turns=5):
            return []

    class _Manager:
        long_term = _LongTerm()
        short_term = _ShortTerm()

        def reload_long_term(self):
            reloaded["n"] += 1

        def get_full_context(self):
            return {
                "short_term": {"statistics": {"total_messages": 0}},
                "long_term": {"statistics": {"total_trips": 0}},
            }

    agent.memory_manager = _Manager()
    buffer = io.StringIO()
    agent.console = Console(file=buffer, force_terminal=False, width=100)
    agent.show_preferences()
    agent.show_history()
    agent.show_status()
    assert reloaded["n"] == 3, "三个读命令都应先重载长期记忆"
    print("✓ preferences / history / status 刷新长期记忆快照")


def test_summary_tolerates_legacy_nested_preference_and_trip_fields():
    """历史数据里的嵌套偏好 / 非字符串行程字段不能让摘要抛 TypeError。"""
    import asyncio

    agent = cli.AligoCLI()

    class _LongTerm:
        def get_preference(self):
            return {"hotel_brands": [["汉庭"], "如家"]}

        def get_trip_history(self, limit=None):
            return [
                {
                    "origin": 123,
                    "destination": "上海",
                    "start_date": "2026-12-01",
                    "purpose": "出差",
                }
            ]

    class _Manager:
        long_term = _LongTerm()

        async def get_long_term_summary_async(self, max_messages=50):
            return ""

    agent.memory_manager = _Manager()
    text = asyncio.run(agent._get_long_term_summary("从北京去上海"))
    assert "汉庭" in text
    assert "上海" in text
    print("✓ 嵌套偏好 / 非字符串行程字段不崩溃")


def test_process_query_releases_probe_on_early_failure(monkeypatch):
    """LLM 调用前失败（如记忆写盘错误）：半开试探名额必须归还。"""
    from utils import circuit_breaker as cb_module
    from utils.circuit_breaker import CircuitBreaker
    from utils.errors import MemoryPersistenceError

    agent, _ = _make_cli_stub()
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout_sec=60, half_open_successes=1
    )
    breaker.record_failure()
    real_monotonic = cb_module.time.monotonic
    monkeypatch.setattr(cb_module.time, "monotonic", lambda: real_monotonic() + 61)
    agent.circuit_breaker = breaker

    async def boom(user_input=""):
        raise MemoryPersistenceError("disk full")

    agent._get_long_term_summary = boom
    try:
        asyncio.run(agent.process_query("你好"))
    except MemoryPersistenceError:
        pass
    else:
        raise AssertionError("应抛出 MemoryPersistenceError")
    assert breaker.allow_call() is True, "提前失败后应归还半开名额"
    print("✓ CLI 提前失败时归还半开名额")


class _TextMessage:
    def __init__(self, content):
        self.content = content


def _make_cli_stub():
    """构造可跑 process_query 的轻量 CLI 桩（假记忆 / 假意图 / 假编排）。"""
    agent = cli.AligoCLI()
    agent.console = Console(file=io.StringIO(), force_terminal=False, width=100)
    events = {"reload": 0, "messages": []}

    class _Memory:
        def __init__(self):
            self.short_term = self

        def reload_long_term(self):
            events["reload"] += 1

        def add_message(self, role, content, metadata=None):
            events["messages"].append(role)

        def get_recent_context(self, n_turns=5):
            return []

    class _Intention:
        async def reply(self, context):
            return _TextMessage(json.dumps({"agent_schedule": []}))

    class _Orchestrator:
        async def reply(self, intention, on_event=None):
            return _TextMessage(json.dumps({"results": []}))

    agent.memory_manager = _Memory()
    agent.intention_agent = _Intention()
    agent.orchestrator = _Orchestrator()

    async def _summary(user_input=""):
        return ""

    agent._get_long_term_summary = _summary
    agent._display_agents_called = lambda result_data: None
    agent._display_results = lambda result_data: None
    return agent, events


def test_process_query_reloads_long_term_each_turn():
    """CLI 每轮对话前重载长期记忆（与 Web 端一致），避免旧快照覆盖外部写入。"""
    agent, events = _make_cli_stub()
    asyncio.run(agent.process_query("你好"))
    assert events["reload"] == 1, "每轮对话前必须重载长期记忆"
    assert events["messages"] == ["user", "assistant"]
    print("✓ CLI 每轮对话前重载长期记忆")


def test_process_query_memory_failure_not_counted_by_breaker():
    """记忆写盘失败不计入熔断器（与 ConversationRuntime 一致）。"""
    from utils.circuit_breaker import CircuitBreaker
    from utils.errors import MemoryPersistenceError

    agent, _ = _make_cli_stub()
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout_sec=60, half_open_successes=1
    )
    agent.circuit_breaker = breaker

    class _BrokenOrchestrator:
        async def reply(self, intention, on_event=None):
            raise MemoryPersistenceError("disk full")

    agent.orchestrator = _BrokenOrchestrator()
    try:
        asyncio.run(agent.process_query("你好"))
    except MemoryPersistenceError:
        pass
    else:
        raise AssertionError("应抛出 MemoryPersistenceError")
    assert breaker.get_status()["failure_count"] == 0, "写盘失败不应计入熔断"
    print("✓ CLI 记忆写盘失败不计入熔断器")


if __name__ == "__main__":
    _mp = pytest.MonkeyPatch()
    try:
        test_eof_at_user_prompt_exits_cleanly()
        test_eof_at_main_prompt_breaks_loop(_mp)
        test_gbk_piped_input_keeps_locale_decoding()
        test_utf8_piped_input_not_mojibake()
        test_human_response_escapes_markup()
        test_human_response_tolerates_malformed_itinerary()
        test_read_commands_reload_snapshot()
        test_summary_tolerates_legacy_nested_preference_and_trip_fields()
        test_process_query_reloads_long_term_each_turn()
        test_process_query_memory_failure_not_counted_by_breaker()
        test_process_query_releases_probe_on_early_failure(_mp)
    finally:
        _mp.undo()
    print("\n全部通过")
