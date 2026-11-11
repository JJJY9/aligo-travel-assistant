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


def test_repl_error_markup_is_escaped(monkeypatch):
    """命令异常文本含 [/red] / link 标记时按原文显示，不能二次抛错终止 REPL。"""
    import types

    agent = cli.AligoCLI()
    buffer = io.StringIO()
    agent.console = Console(file=buffer, force_terminal=False, width=200)

    async def fake_init():
        agent.user_id = "u1"
        agent.memory_manager = types.SimpleNamespace(end_session=lambda: None)

    async def boom(text):
        raise RuntimeError(
            "[/red] boom [link=https://evil.example]点我[/link]"
        )

    monkeypatch.setattr(agent, "initialize_system", fake_init)
    monkeypatch.setattr(agent, "print_banner", lambda: None)
    monkeypatch.setattr(agent, "process_query", boom)
    monkeypatch.setattr(sys, "stdin", io.StringIO("触发错误\nexit\n"))
    asyncio.run(agent.run())
    text = buffer.getvalue()
    assert "[/red] boom" in text, "关闭标签必须按原文显示"
    assert "[link=" in text
    assert "\x1b]8;" not in text, "不能把异常文本渲染成终端超链接"
    print("✓ 主循环兜底错误按原文显示（不再二次 MarkupError）")


def test_history_table_strips_terminal_controls():
    """存储的行程字段含 OSC 序列时不能原样送达终端（剪贴板/伪链接注入）。"""
    import types

    agent = cli.AligoCLI()
    buffer = io.StringIO()
    agent.console = Console(file=buffer, force_terminal=False, width=200)
    poisoned = "上海\x1b]52;c;ZWNobyBwd25lZA==\x1b\\"
    trip = {
        "trip_id": "t1",
        "origin": "北京",
        "destination": poisoned,
        "start_date": "2026-11-11",
        "purpose": "出差",
    }
    agent.memory_manager = types.SimpleNamespace(
        reload_long_term=lambda: None,
        long_term=types.SimpleNamespace(
            get_trip_history=lambda n: [trip],
        ),
    )
    agent.show_history()
    text = buffer.getvalue()
    assert "上海" in text
    assert "\x1b]" not in text, "OSC 序列必须被剥离"
    print("✓ 历史行程表格剥离终端控制序列")


def test_sanitized_console_strips_controls_from_print():
    """所有经 Console.print 的 str 都剥离终端控制序列（模型输出 / 上游错误）。"""
    agent = cli.AligoCLI()
    buffer = io.StringIO()
    agent.console = cli._SanitizedConsole(file=buffer, force_terminal=False)
    agent.console.print("前缀\x1b]8;;https://evil.example\x1b\\点我\x1b]8;;\x1b\\后缀")
    text = buffer.getvalue()
    assert "前缀" in text and "后缀" in text
    assert "\x1b]" not in text
    print("✓ Console 输出统一剥离终端控制序列")


def test_pending_cancel_cleared_when_loop_ends_by_eof(monkeypatch):
    """同步命令期间按 Ctrl-C 后 stdin EOF：主任务不能带着挂起取消收尾。"""
    import signal as signal_module
    import types

    agent = cli.AligoCLI()
    buffer = io.StringIO()
    agent.console = Console(file=buffer, force_terminal=False, width=100)

    async def fake_init():
        agent.user_id = "u1"
        agent.memory_manager = types.SimpleNamespace(end_session=lambda: None)

    def fake_history():
        # 同步命令不让出事件循环：取消只挂在主任务上，随后 stdin 直接 EOF
        signal_module.raise_signal(signal_module.SIGINT)

    monkeypatch.setattr(agent, "initialize_system", fake_init)
    monkeypatch.setattr(agent, "print_banner", lambda: None)
    monkeypatch.setattr(agent, "show_history", fake_history)
    monkeypatch.setattr(sys, "stdin", io.StringIO("history\n"))
    asyncio.run(agent.run())  # 不得抛 CancelledError / traceback
    assert "未检测到输入" in buffer.getvalue()
    print("✓ 循环结束时挂起的用户取消被消化")


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
    events = {"reload": 0, "messages": [], "staged": [], "persisted": [], "rolled_back": []}

    class _Memory:
        def __init__(self):
            self.short_term = self

        def reload_long_term(self):
            events["reload"] += 1

        def persist_message(self, role, content):
            events["persisted"].append(role)

        def persist_turn(self, user_content, assistant_content):
            # 整轮问答一次提交：用户 + 助手两条都在同一次调用里落库
            self.persist_message("user", user_content)
            self.persist_message("assistant", assistant_content)
            self.stage_message("assistant", assistant_content)

        def stage_message(self, role, content, metadata=None):
            events["staged"].append(content)

        def rollback_staged_message(self, content):
            events["rolled_back"].append(content)
            if events["staged"] and events["staged"][-1] == content:
                events["staged"].pop()
            return True

        def add_message(self, role, content, metadata=None):
            self.persist_message(role, content)
            self.stage_message(role, content, metadata)

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
    assert events["persisted"] == ["user", "assistant"]
    assert events["staged"][0] == "你好", "编排前暂存本轮提问"
    assert len(events["staged"]) == 2, "用户只能暂存一次（不能重复），助手回复正常进短期"
    print("✓ CLI 每轮对话前重载长期记忆")


def test_process_query_sanitizes_surrogates_before_persist():
    """模型输出含孤立代理项：显示 / 轮次元数据 / 落库前统一转义，不再丢整轮。"""
    agent, _events = _make_cli_stub()
    lonely = chr(0xD800)

    class _SurrogateIntention:
        async def reply(self, context):
            return _TextMessage(
                json.dumps(
                    {
                        "reasoning": "推理" + lonely,
                        "intents": ["greeting"],
                        "agent_schedule": [],
                    }
                )
            )

    class _SurrogateOrchestrator:
        async def reply(self, intention, on_event=None):
            return _TextMessage(
                json.dumps({"answer": "回答" + lonely, "results": []})
            )

    agent.intention_agent = _SurrogateIntention()
    agent.orchestrator = _SurrogateOrchestrator()

    persisted = []
    original_persist = agent.memory_manager.persist_turn

    def strict_persist(user_content, assistant_content):
        # 未转义的孤立代理项在这里抛 UnicodeEncodeError（真实 JSON 后端同款）
        assistant_content.encode("utf-8")
        persisted.append(assistant_content)
        original_persist(user_content, assistant_content)

    agent.memory_manager.persist_turn = strict_persist
    asyncio.run(agent.process_query("你好"))
    assert persisted, "整轮必须成功落库"
    assert lonely not in persisted[0], "落库文本必须已转义"
    print("✓ CLI 模型输出代理项在落库前转义")


def test_failed_turn_usage_carried_to_next_success():
    """失败整轮的 token 计入下一轮展示与落库元数据（不永久少记）。"""
    import types

    from config_agentscope import UsageTracker
    from utils.turn_meta import extract_turn_meta

    agent, events = _make_cli_stub()
    tracker = UsageTracker()
    agent.model = types.SimpleNamespace(usage=tracker)
    buffer = io.StringIO()
    agent.console = Console(file=buffer, force_terminal=False, width=120)

    class _Boom:
        async def reply(self, intention, on_event=None):
            tracker.calls += 1
            tracker.input_tokens += 100
            tracker.output_tokens += 10
            raise RuntimeError("boom")

    agent.orchestrator = _Boom()
    with pytest.raises(RuntimeError):
        asyncio.run(agent.process_query("第一轮"))

    class _Ok:
        async def reply(self, intention, on_event=None):
            tracker.calls += 1
            tracker.input_tokens += 50
            tracker.output_tokens += 5
            return _TextMessage(json.dumps({"results": []}))

    agent.orchestrator = _Ok()
    asyncio.run(agent.process_query("第二轮"))
    output = buffer.getvalue()
    assert "tokens 165" in output, output
    assistant = events["staged"][-1]
    meta = extract_turn_meta(assistant)
    assert meta and meta["usage"]["total_tokens"] == 165, meta
    print("✓ CLI 失败回合用量并入下一轮展示与落库")


def test_process_query_rolls_back_when_persist_vetoed():
    """墓碑否决整轮（persist_turn 返回 False）：回滚暂存提问并明确提示。"""
    agent, events = _make_cli_stub()
    buffer = io.StringIO()
    agent.console = Console(file=buffer, force_terminal=False, width=120)

    def veto(user_content, assistant_content):
        return False

    agent.memory_manager.persist_turn = veto
    asyncio.run(agent.process_query("你好"))
    assert events["rolled_back"] == ["你好"], (
        "墓碑否决后暂存提问必须回滚，不能留在短期上下文"
    )
    assert events["persisted"] == [], "否决的整轮不能计入已落库"
    output = buffer.getvalue()
    assert "已被删除" in output and "未写入长期记忆" in output, output
    print("✓ CLI 会话被删除时整轮回滚并提示")


def test_process_query_stages_user_message_before_orchestration():
    """编排期间短期记忆必须已含本轮提问（_prepare_context 读取用户本轮原话）。"""
    agent, events = _make_cli_stub()
    seen = {}

    class _InspectingOrchestrator:
        async def reply(self, intention, on_event=None):
            seen["staged"] = list(events["staged"])
            return _TextMessage(json.dumps({"results": []}))

    agent.orchestrator = _InspectingOrchestrator()
    asyncio.run(agent.process_query("我搬家到上海了"))
    assert seen["staged"] == ["我搬家到上海了"], seen


def test_process_query_failure_leaves_no_long_term_user_turn():
    """编排失败时长期记忆不落孤儿用户轮，短期记忆的暂存提问同步回滚。"""

    class _FailingOrchestrator:
        async def reply(self, intention, on_event=None):
            raise RuntimeError("gateway down")

    agent, events = _make_cli_stub()
    agent.orchestrator = _FailingOrchestrator()
    with pytest.raises(RuntimeError):
        asyncio.run(agent.process_query("你好"))
    assert events["persisted"] == [], "失败时不得写长期记忆"
    assert events["staged"] == [], "失败后暂存提问必须从短期记忆回滚"
    assert events["rolled_back"] == ["你好"]
    print("✓ 编排失败不落孤儿用户轮，短期暂存提问回滚")


def test_process_query_counts_generic_orchestration_failure():
    """编排阶段的一般异常计入熔断失败并向上抛出。"""
    from utils.circuit_breaker import CircuitBreaker

    agent, _ = _make_cli_stub()
    breaker = CircuitBreaker(
        failure_threshold=5, recovery_timeout_sec=60, half_open_successes=1
    )
    agent.circuit_breaker = breaker

    class _BoomOrchestrator:
        async def reply(self, intention, on_event=None):
            raise RuntimeError("boom")

    agent.orchestrator = _BoomOrchestrator()
    with pytest.raises(RuntimeError):
        asyncio.run(agent.process_query("你好"))
    assert breaker.get_status()["failure_count"] == 1
    print("✓ 编排一般异常计入熔断")


def test_process_query_open_breaker_returns_quickly():
    """熔断已打开：入口直接提示并返回，不再调用意图 / 编排。"""
    from utils.circuit_breaker import CircuitBreaker

    agent, events = _make_cli_stub()
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout_sec=60, half_open_successes=1
    )
    breaker.record_failure()
    agent.circuit_breaker = breaker
    calls = []
    agent.intention_agent.reply = lambda context: calls.append("intent")
    asyncio.run(agent.process_query("你好"))
    assert calls == []
    assert "服务暂时不可用" in agent.console.file.getvalue()
    print("✓ 熔断打开时入口快速返回")


def test_process_query_filters_malformed_recent_context():
    """短期记忆中的非法条目（非 dict / 非法角色 / 缺 content）不进入意图上下文。"""
    agent, _ = _make_cli_stub()
    captured = {}

    class _CaptureIntention:
        async def reply(self, context):
            captured["context"] = list(context)
            return _TextMessage(json.dumps({"agent_schedule": []}))

    agent.intention_agent = _CaptureIntention()
    agent.memory_manager.get_recent_context = lambda n_turns=5: [
        "bad",
        {"role": "user", "content": "有效消息"},
        {"role": "tool", "content": "x"},
        {"role": "assistant"},
    ]
    asyncio.run(agent.process_query("本轮提问"))
    contents = [msg.content for msg in captured["context"]]
    assert "有效消息" in contents and "本轮提问" in contents
    assert all("有效消息" in c or "本轮提问" in c for c in contents)
    print("✓ 非法短期条目被过滤后组装意图上下文")


def test_process_query_unparsable_intention_returns():
    """意图结果不是 JSON：提示无法理解并直接返回，不进入编排。"""
    agent, events = _make_cli_stub()
    orchestrated = []

    class _BadIntention:
        async def reply(self, context):
            return _TextMessage("这不是 JSON")

    class _CountingOrchestrator:
        async def reply(self, intention, on_event=None):
            orchestrated.append(1)
            return _TextMessage(json.dumps({"results": []}))

    agent.intention_agent = _BadIntention()
    agent.orchestrator = _CountingOrchestrator()
    asyncio.run(agent.process_query("你好"))
    assert orchestrated == []
    assert "无法理解您的需求" in agent.console.file.getvalue()
    print("✓ 意图解析失败友好提示且不编排")


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


def _sigint_cli(monkeypatch, ask, process):
    """构造接管 SIGINT 的 CLI 桩：假初始化 / 假输入 / 假查询。"""
    import types

    agent = cli.AligoCLI()
    buffer = io.StringIO()
    agent.console = Console(file=buffer, force_terminal=False, width=100)

    async def fake_init():
        agent.user_id = "u1"
        agent.memory_manager = types.SimpleNamespace(end_session=lambda: None)

    monkeypatch.setattr(agent, "initialize_system", fake_init)
    monkeypatch.setattr(agent, "print_banner", lambda: None)
    monkeypatch.setattr(cli.Prompt, "ask", ask)
    monkeypatch.setattr(agent, "process_query", process)
    return agent, buffer


def test_prompt_sigint_does_not_cancel_next_command(monkeypatch):
    """提示符处按 Ctrl-C 只打断当前输入；随后输入的命令必须正常执行。"""
    import signal as signal_module
    import time as time_module

    calls = {"ask": 0, "processed": []}

    async def fake_process(user_input):
        await asyncio.sleep(0)  # 第一个 await：悬挂的取消会在这里抛出
        calls["processed"].append(user_input)

    def fake_ask(prompt, **kwargs):
        calls["ask"] += 1
        if calls["ask"] == 1:
            # 提示符（同步 input）期间按 Ctrl-C：处理器打断 input()
            signal_module.raise_signal(signal_module.SIGINT)
            time_module.sleep(0.05)  # 信号检查在这里派发处理器
            return "被打断的输入不应生效"
        if calls["ask"] == 2:
            return "随便问点什么"
        raise EOFError

    agent, buffer = _sigint_cli(monkeypatch, fake_ask, fake_process)
    asyncio.run(agent.run())
    assert calls["ask"] >= 2, "Ctrl-C 后应回到提示符而不是退出"
    assert calls["processed"] == ["随便问点什么"], "下一条命令不能被静默取消"
    assert "使用 'exit' 退出" in buffer.getvalue()
    print("✓ 提示符 Ctrl-C 不再取消下一条命令")


def test_user_sigint_during_query_returns_to_prompt(monkeypatch):
    """查询执行中按 Ctrl-C：取消当前查询并回到提示符，而不是退出 REPL。"""
    import signal as signal_module

    calls = {"ask": 0}

    async def fake_process(user_input):
        loop = asyncio.get_running_loop()
        loop.call_soon(signal_module.raise_signal, signal_module.SIGINT)
        await asyncio.sleep(10)  # 等待被取消

    def fake_ask(prompt, **kwargs):
        calls["ask"] += 1
        if calls["ask"] == 1:
            return "问题"
        raise EOFError

    agent, buffer = _sigint_cli(monkeypatch, fake_ask, fake_process)
    asyncio.run(agent.run())
    assert calls["ask"] == 2, "取消后应回到提示符继续读输入"
    assert "使用 'exit' 退出" in buffer.getvalue()
    print("✓ 查询中 Ctrl-C 回到提示符")


def test_external_cancel_propagates_out_of_repl(monkeypatch):
    """非 SIGINT 发起的取消（asyncio.run 关闭阶段）不能被 REPL 吞掉。"""

    async def fake_process(user_input):
        await asyncio.sleep(10)

    def fake_ask(prompt, **kwargs):
        return "问题"

    agent, _ = _sigint_cli(monkeypatch, fake_ask, fake_process)

    async def scenario():
        task = asyncio.create_task(agent.run())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    print("✓ 关闭阶段的主任务取消继续向上抛（不再卡住退出）")


def test_sigint_at_user_id_prompt_exits_cleanly(monkeypatch):
    """用户ID 提示符处按 Ctrl-C：立即干净退出，不把取消挂到主任务上。"""
    import signal as signal_module
    import time as time_module

    def fake_ask(prompt, **kwargs):
        signal_module.raise_signal(signal_module.SIGINT)
        time_module.sleep(0.05)  # 信号检查在这里派发处理器
        return "never_used"  # 处理器应已打断 input，不会走到这里

    agent = cli.AligoCLI()
    buffer = io.StringIO()
    agent.console = Console(file=buffer, force_terminal=False, width=100)
    monkeypatch.setattr(cli.Prompt, "ask", fake_ask)
    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(agent.run())
    assert excinfo.value.code == 130
    assert "已取消，退出" in buffer.getvalue()
    assert agent._interrupt["at_prompt"] is False
    print("✓ 用户ID 提示符 Ctrl-C 干净退出")


def test_sigint_during_initialization_does_not_poison_first_command(monkeypatch):
    """初始化期间按 Ctrl-C：不进入 REPL，第一条命令不被悬挂的取消吞掉。"""
    import signal as signal_module

    agent = cli.AligoCLI()
    buffer = io.StringIO()
    agent.console = Console(file=buffer, force_terminal=False, width=100)
    monkeypatch.setattr(agent, "print_banner", lambda: None)

    async def fake_init():
        # 同步初始化期间按 Ctrl-C：处理器只能取消主任务，取消在下一个
        # await 才送达——run() 必须在进入 REPL 前消化掉
        signal_module.raise_signal(signal_module.SIGINT)

    monkeypatch.setattr(agent, "initialize_system", fake_init)

    def fail_ask(prompt, **kwargs):
        raise AssertionError("初始化被取消后不应进入 REPL")

    monkeypatch.setattr(cli.Prompt, "ask", fail_ask)
    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(agent.run())
    assert excinfo.value.code == 130, "取消启动不能以 0 汇报成功"
    assert "已取消，退出" in buffer.getvalue()
    print("✓ 初始化期间 Ctrl-C 不再毒化第一条命令")


def test_preference_display_tolerates_unhashable_type():
    """偏好 type 为对象 / 数组时跳过（与落库路径一致），整轮不能崩。"""
    agent, buffer = _capture_console()
    shown = agent._generate_human_response(
        [
            {
                "agent_name": "preference",
                "status": "success",
                "data": {
                    "preferences": [
                        {"type": {"name": "hotel_brands"}, "value": "汉庭"},
                        {"type": ["airlines"], "value": "东航"},
                        {
                            "type": "hotel_brands",
                            "value": "如家",
                            "action": "append",
                        },
                    ]
                },
            }
        ]
    )
    assert shown is True
    text = buffer.getvalue()
    assert "如家" in text
    assert "汉庭" not in text, "落库会跳过的条目也不应回显"
    print("✓ 偏好回显跳过非字符串 type（不再 TypeError）")


def test_prompt_oserror_exits_instead_of_hot_loop(monkeypatch):
    """stdin 读取抛 OSError（句柄失效 / pty 断开）时退出，不能刷屏热循环。"""
    import types

    agent = cli.AligoCLI()
    buffer = io.StringIO()
    agent.console = Console(file=buffer, force_terminal=False, width=100)

    async def fake_init():
        agent.user_id = "u1"
        agent.memory_manager = types.SimpleNamespace(end_session=lambda: None)

    calls = {"n": 0}

    def boom(prompt, **kwargs):
        calls["n"] += 1
        raise OSError("Input/output error")

    monkeypatch.setattr(cli.Prompt, "ask", boom)
    monkeypatch.setattr(agent, "initialize_system", fake_init)
    monkeypatch.setattr(agent, "print_banner", lambda: None)
    asyncio.run(agent.run())
    assert calls["n"] == 1, "读失败必须退出而不是无退避热循环"
    assert "无法读取输入" in buffer.getvalue()
    print("✓ stdin OSError 干净退出（不热循环）")


def test_closed_stdout_pipe_does_not_traceback():
    """`| head` 提前关闭 stdout：交互模式也不打 traceback / 非零退出。"""
    proc = subprocess.Popen(
        [sys.executable, "cli.py"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=project_root,
    )
    try:
        assert proc.stdout is not None
        proc.stdout.read(1)  # 等首屏输出后再关闭读端
        proc.stdout.close()
        if proc.stdin:
            proc.stdin.close()
        stderr = proc.stderr.read() if proc.stderr else b""
        code = proc.wait(timeout=60)
    finally:
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass
    text = stderr.decode("utf-8", errors="replace")
    assert "Traceback" not in text, text
    assert "Exception ignored" not in text, text
    # 无 API Key 的 CI 环境初始化失败退出码为 1；本机配置了 Key 时为 0
    assert code in (0, 1), (code, text)
    print("✓ stdout 管道提前关闭不再 traceback")


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
        test_process_query_stages_user_message_before_orchestration()
        test_process_query_rolls_back_when_persist_vetoed()
        test_failed_turn_usage_carried_to_next_success()
        test_process_query_sanitizes_surrogates_before_persist()
        test_process_query_failure_leaves_no_long_term_user_turn()
        test_process_query_memory_failure_not_counted_by_breaker()
        test_process_query_releases_probe_on_early_failure(_mp)
        test_prompt_sigint_does_not_cancel_next_command(_mp)
        test_user_sigint_during_query_returns_to_prompt(_mp)
        test_external_cancel_propagates_out_of_repl(_mp)
        test_sigint_at_user_id_prompt_exits_cleanly(_mp)
        test_sigint_during_initialization_does_not_poison_first_command(_mp)
        test_preference_display_tolerates_unhashable_type()
        test_closed_stdout_pipe_does_not_traceback()
    finally:
        _mp.undo()
    print("\n全部通过")
