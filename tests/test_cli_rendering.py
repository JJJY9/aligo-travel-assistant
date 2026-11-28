"""交互 CLI 渲染与健壮性测试（无需 API Key、不访问网络）。

运行：python tests/test_cli_rendering.py
"""
import asyncio
import io
import json
import os
import sys

import pytest
from rich.console import Console

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

import cli as cli_module


def _console_buffer() -> tuple[Console, io.StringIO]:
    buffer = io.StringIO()
    console = Console(
        file=buffer, force_terminal=False, width=200, color_system=None
    )
    return console, buffer


def _new_cli() -> cli_module.AligoCLI:
    instance = object.__new__(cli_module.AligoCLI)
    console, buffer = _console_buffer()
    instance.console = console
    setattr(instance, "_buffer", buffer)
    # run() 的 SIGINT 状态在 __init__ 里创建；这里绕过 __init__ 造实例，
    # 补上同一结构（取消来源标记）
    instance._interrupt = {"count": 0, "user": False, "at_prompt": False}
    return instance


def test_missing_api_key_exits_cleanly(monkeypatch):
    monkeypatch.setitem(cli_module.LLM_CONFIG, "api_key", "")
    monkeypatch.setattr(cli_module.AligoCLI, "_prompt_user_id", lambda self: "u1")
    monkeypatch.setattr(cli_module, "init_agentscope", lambda: None)
    called = []
    monkeypatch.setattr(
        cli_module, "create_chat_model", lambda **kwargs: called.append(1)
    )
    instance = _new_cli()
    with pytest.raises(SystemExit) as exc:
        asyncio.run(instance.initialize_system())
    assert exc.value.code == 2, "缺 Key 应干净退出（exit 2），不能 traceback"
    assert called == [], "缺 Key 时不得尝试构造模型"
    print("✓ 未配置 API Key 时交互模式干净退出")


def test_model_init_failure_exits_cleanly(monkeypatch):
    monkeypatch.setitem(cli_module.LLM_CONFIG, "api_key", "sk-test")
    monkeypatch.setattr(cli_module.AligoCLI, "_prompt_user_id", lambda self: "u1")
    monkeypatch.setattr(cli_module, "init_agentscope", lambda: None)

    def boom(**kwargs):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(cli_module, "create_chat_model", boom)
    instance = _new_cli()
    with pytest.raises(SystemExit) as exc:
        asyncio.run(instance.initialize_system())
    assert exc.value.code == 1
    assert "模型初始化失败" in instance._buffer.getvalue()
    print("✓ 模型初始化失败给出提示并干净退出")


def test_preference_rendering_robust_and_consistent():
    instance = _new_cli()
    results = [
        {
            "agent_name": "preference",
            "status": "success",
            "data": {
                "preferences": [
                    {"type": "hotel_brands", "value": ["汉庭", "如家"], "action": "Append"},
                    "不是对象条目",
                    {"type": "budget_level", "value": 0, "action": "replace"},
                ]
            },
        }
    ]
    handled = instance._generate_human_response(results)
    output = instance._buffer.getvalue()
    assert handled is True
    assert "追加" in output, "Append 变体必须按追加入库口径显示"
    assert "汉庭、如家" in output, "列表值按「、」拼接而不是 Python repr"
    assert "0" in output, "预算 0 是合法值"
    assert "Traceback" not in output
    print("✓ 偏好回显：类型守卫 / Append 变体 / 列表拼接 / 0 值")


def test_preference_rendering_skips_unmanageable_types():
    """含 "/" / 超过 64 字符的 type 不回显「已更新」（与落库口径一致）。"""
    instance = _new_cli()
    long_key = "k" * 65
    handled = instance._generate_human_response(
        [
            {
                "agent_name": "preference",
                "status": "success",
                "data": {
                    "preferences": [
                        {"type": "a/b", "value": "x"},
                        {"type": long_key, "value": "y"},
                        {"type": "airlines", "value": "东航"},
                    ]
                },
            }
        ]
    )
    output = instance._buffer.getvalue()
    assert handled is True
    assert "已更新您的偏好设置" in output
    assert "东航" in output
    assert "a/b" not in output, "含 / 的偏好键不能被宣称已保存"
    assert long_key not in output, "超过 64 字符的偏好键不能被宣称已保存"

    solo = _new_cli()
    solo._generate_human_response(
        [
            {
                "agent_name": "preference",
                "status": "success",
                "data": {"preferences": [{"type": "a/b", "value": "x"}]},
            }
        ]
    )
    assert "已更新" not in solo._buffer.getvalue()
    print("✓ 偏好回显过滤含 / 与超长 type")


def test_preference_rendering_skips_explanatory_and_blank():
    """说明键（note/status）与空值不会落库，回显不能宣称「已更新」。"""
    instance = _new_cli()
    handled = instance._generate_human_response(
        [
            {
                "agent_name": "preference",
                "status": "success",
                "data": {
                    "preferences": [
                        {"type": "note", "value": "用户喜欢靠窗"},
                        {"type": "status", "value": "已记录"},
                        {"type": "seat_preference", "value": "靠窗"},
                        {"type": "meal_preference", "value": None},
                    ]
                },
            }
        ]
    )
    output = instance._buffer.getvalue()
    assert handled is True
    assert "座位偏好" in output and "靠窗" in output
    assert "用户喜欢靠窗" not in output, "说明性条目不能回显为偏好"
    assert "meal_preference" not in output, "空值偏好不能被宣称已保存"

    solo = _new_cli()
    solo._generate_human_response(
        [
            {
                "agent_name": "preference",
                "status": "success",
                "data": {"preferences": [{"type": "note", "value": "仅说明"}]},
            }
        ]
    )
    assert "已更新您的偏好设置" not in solo._buffer.getvalue()
    print("✓ 偏好回显跳过说明键与空值")


def test_preference_rendering_skips_oversized_value():
    """超限偏好值不会落库：回显不能先宣称「已更新」。"""
    instance = _new_cli()
    instance._generate_human_response(
        [
            {
                "agent_name": "preference",
                "status": "success",
                "data": {
                    "preferences": [
                        {"type": "home_location", "value": "x" * 10001},
                    ]
                },
            }
        ]
    )
    assert "已更新" not in instance._buffer.getvalue()
    print("✓ 偏好回显过滤超限的值")


def test_preference_legacy_dict_and_nested_shapes():
    instance = _new_cli()
    results = [
        {
            "agent_name": "preference",
            "status": "success",
            "data": {"preferences": {"home_location": "北京"}},
        },
        {
            "agent_name": "event_collection",
            "status": "success",
            "data": {"data": "不是对象", "destination": ["上海", "杭州"]},
        },
    ]
    instance._generate_human_response(results)
    output = instance._buffer.getvalue()
    assert "常驻地" in output and "北京" in output, "旧版 {type: value} 字典要能显示"
    assert "上海、杭州" in output, "列表目的地按「、」拼接"
    print("✓ 旧版偏好字典 / data 非对象 / 列表字段")


def test_agent_error_message_fallback():
    instance = _new_cli()
    instance._generate_human_response(
        [
            {
                "agent_name": "information_query",
                "status": "error",
                "data": {"message": "上游限流"},
            }
        ]
    )
    output = instance._buffer.getvalue()
    assert "上游限流" in output and "未知错误" not in output
    print("✓ 失败智能体优先显示 message 字段")


def test_agent_error_uses_aggregated_message():
    """错误原因只在聚合条目的 message 上时也要显示，而不是「未知错误」。"""
    instance = _new_cli()
    instance._generate_human_response(
        [
            {
                "agent_name": "query-info",
                "status": "error",
                "data": {
                    "query_success": False,
                    "results": {"error": "搜索失败: boom"},
                },
                "message": "搜索失败: boom",
            }
        ]
    )
    output = instance._buffer.getvalue()
    assert "搜索失败: boom" in output and "未知错误" not in output
    print("✓ 失败智能体使用聚合条目的 message")


def test_status_renders_assistant_plaintext():
    class _ShortTerm:
        def get_recent_context(self, n_turns=5):
            return [
                {"role": "user", "content": "住宿标准？", "timestamp": ""},
                {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "results": [
                                {
                                    "agent_name": "rag_knowledge",
                                    "status": "success",
                                    "data": {"answer": "一线城市 600 元"},
                                }
                            ]
                        },
                        ensure_ascii=False,
                    ),
                    "timestamp": "",
                },
            ]

    class _Memory:
        short_term = _ShortTerm()

        def reload_long_term(self):
            pass

        def get_full_context(self):
            return {
                "short_term": {"statistics": {"total_messages": 2}},
                "long_term": {"statistics": {"total_trips": 0}},
            }

        @property
        def long_term(self):
            raise AssertionError("status 不应读取长期记忆")

    instance = _new_cli()
    instance.memory_manager = _Memory()
    instance.user_id = "u1"
    instance.session_id = "s1"
    instance._agent_cache = {}
    instance.model = None
    instance.show_status()
    output = instance._buffer.getvalue()
    assert "一线城市 600 元" in output, "助手消息应渲染正文而不是编排 JSON"
    assert "results" not in output
    print("✓ status 最近对话渲染助手正文")


def test_status_table_single_lines_multiline_message():
    """status 最近对话表格：多行消息必须单行化，不能拆行错位后续列。"""

    class _ShortTerm:
        def get_recent_context(self, n_turns=5):
            return [
                {"role": "user", "content": "第一行\n第二行", "timestamp": ""},
                {"role": "assistant", "content": "回答\n也换行", "timestamp": ""},
            ]

    class _Memory:
        short_term = _ShortTerm()

        def reload_long_term(self):
            pass

        def get_full_context(self):
            return {
                "short_term": {"statistics": {"total_messages": 2}},
                "long_term": {"statistics": {"total_trips": 0}},
            }

        @property
        def long_term(self):
            raise AssertionError("status 不应读取长期记忆")

    instance = _new_cli()
    instance.memory_manager = _Memory()
    instance.user_id = "u1"
    instance.session_id = "s1"
    instance._agent_cache = {}
    instance.model = None
    instance.show_status()
    output = instance._buffer.getvalue()
    assert "第一行 第二行" in output, output
    assert "回答 也换行" in output, output
    # 角色列每行只出现一次：多行内容没有把表格拆成续行
    assert output.count("👤 用户") == 1
    assert output.count("🤖 助手") == 1
    print("✓ status 多行消息单行化")


def test_preferences_table_keeps_zero_and_lists():
    class _LongTerm:
        def get_preference(self):
            return {"budget_level": 0, "seat_preference": False, "hotel_brands": ["汉庭"]}

    class _Memory:
        long_term = _LongTerm()

        def reload_long_term(self):
            pass

    instance = _new_cli()
    instance.memory_manager = _Memory()
    instance.show_preferences()
    output = instance._buffer.getvalue()
    assert "budget_level" in output and "0" in output, "0 是合法偏好值"
    assert "seat_preference" in output, "False 是合法偏好值"
    assert "汉庭" in output and "['汉庭']" not in output, "列表值按「、」拼接"
    print("✓ preferences 命令保留 0/False 并拼接列表")


def test_print_stdout_swallows_pipe_errors_only(monkeypatch):
    real_print = print
    errors = []

    def fake_print(*args, **kwargs):
        error = errors.pop(0)
        raise error

    monkeypatch.setattr("builtins.print", fake_print)
    errors.append(OSError(22, "invalid argument"))
    # 管道错误分支会把真实 fd1 重定向到 devnull（生产语义：下游已断开，
    # 静默收尾）：测试必须保存并恢复 fd1，否则进程后续输出被永久吞掉
    saved_fd = os.dup(1)
    try:
        cli_module._print_stdout("x")  # Errno 22（Windows 管道关闭）应被吞掉
    finally:
        os.dup2(saved_fd, 1)
        os.close(saved_fd)

    errors.append(OSError(5, "io error"))
    with pytest.raises(OSError):
        cli_module._print_stdout("x")  # 真实写失败必须抛出
    monkeypatch.setattr("builtins.print", real_print)
    print("✓ --stdout 仅吞管道类 OSError")


def test_repl_foreign_cancelled_error_propagates(monkeypatch):
    """非 SIGINT 发起的 CancelledError（如关闭阶段）不能被 REPL 吞掉。

    用户 Ctrl-C 的「回到提示符」路径由 run() 自装的 SIGINT 处理器标记
    （见 test_cli_interactive 的 Ctrl-C 用例）；从 Prompt 逃逸出的外来
    取消必须向上抛——否则 asyncio.run 关闭阶段的 gather 永远等不到主
    任务，进程会卡在提示符上无法退出。
    """
    instance = _new_cli()

    async def fake_initialize(self):
        return None

    def fake_prompt(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(cli_module.AligoCLI, "initialize_system", fake_initialize)
    monkeypatch.setattr(cli_module, "Prompt", type("P", (), {"ask": staticmethod(fake_prompt)}))
    monkeypatch.setattr(instance, "print_banner", lambda: None)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(instance.run())
    print("✓ 外来 CancelledError 从 REPL 向上抛出")


def test_repl_memory_failure_keeps_session(monkeypatch):
    """记忆写盘失败（MemoryPersistenceError 是 RuntimeError 子类）只报错，不退出会话。"""
    from utils.errors import MemoryPersistenceError

    instance = _new_cli()
    calls = {"prompt": 0}

    async def fake_initialize(self):
        return None

    def fake_prompt(*args, **kwargs):
        calls["prompt"] += 1
        if calls["prompt"] == 1:
            return "你好"
        raise EOFError

    async def broken_query(self, text):
        raise MemoryPersistenceError("disk full")

    monkeypatch.setattr(cli_module.AligoCLI, "initialize_system", fake_initialize)
    monkeypatch.setattr(cli_module.AligoCLI, "process_query", broken_query)
    monkeypatch.setattr(
        cli_module, "Prompt", type("P", (), {"ask": staticmethod(fake_prompt)})
    )
    monkeypatch.setattr(instance, "print_banner", lambda: None)
    asyncio.run(instance.run())
    output = instance._buffer.getvalue()
    assert calls["prompt"] == 2, "写盘失败后必须回到提示符（不能被当成 stdin 故障退出）"
    assert "无法读取输入" not in output
    assert "错误: disk full" in output
    assert "未检测到输入" in output
    print("✓ 一轮记忆写盘失败不终止 REPL")


def test_repl_input_unavailable_exits(monkeypatch):
    instance = _new_cli()

    async def fake_initialize(self):
        return None

    def broken_prompt(*args, **kwargs):
        raise RuntimeError("no stdin")

    monkeypatch.setattr(cli_module.AligoCLI, "initialize_system", fake_initialize)
    monkeypatch.setattr(cli_module, "Prompt", type("P", (), {"ask": staticmethod(broken_prompt)}))
    monkeypatch.setattr(instance, "print_banner", lambda: None)
    asyncio.run(instance.run())
    assert "无法读取输入" in instance._buffer.getvalue()
    print("✓ stdin 不可用时干净退出（不刷屏）")


def test_cli_realtime_renders_itinerary_validation_warning():
    """CLI 实时回复与 Web / 历史同口径：行程校验失败必须当场提示。"""
    instance = _new_cli()
    results = [
        {
            "agent_name": "itinerary_planning",
            "status": "success",
            "data": {
                "itinerary": {
                    "title": "杭州三日",
                    "duration": "3天",
                    "daily_plans": [],
                    "notes": [],
                },
                "validation": {
                    "passed": False,
                    "issues": [{"message": "必去项未覆盖：西湖"}],
                },
            },
        }
    ]
    assert instance._generate_human_response(results) is True
    output = instance._buffer.getvalue()
    assert "未通过行程校验" in output, output
    assert "必去项未覆盖：西湖" in output

    passed = _new_cli()
    passed_results = [
        {
            "agent_name": "itinerary_planning",
            "status": "success",
            "data": {
                "itinerary": {"title": "杭州三日", "daily_plans": []},
                "validation": {"passed": True, "issues": []},
            },
        }
    ]
    assert passed._generate_human_response(passed_results) is True
    assert "未通过行程校验" not in passed._buffer.getvalue()
    print("✓ CLI 实时回复渲染行程校验警告")


def test_result_formatter_generic_output_field():
    from utils.result_formatter import result_to_markdown

    text = result_to_markdown(
        {
            "results": [
                {
                    "agent_name": "unknown_agent",
                    "status": "success",
                    "data": {"output": "包装后的正文"},
                }
            ]
        }
    )
    assert "包装后的正文" in text, "非 JSON 智能体输出包装在 output 字段时不能丢正文"
    print("✓ 通用兜底渲染识别 output 字段")


def test_cli_realtime_renders_rag_sources():
    """CLI 实时知识问答与 Web / 历史 / 导出同口径：展示参考来源与参考片段。"""
    instance = _new_cli()
    results = [
        {
            "agent_name": "rag_knowledge",
            "status": "success",
            "data": {
                "answer": "一线城市住宿标准 600 元/晚。",
                "retrieved_documents": [
                    {
                        "id": 3,
                        "content": "一线城市住宿标准：500 元/晚，凭发票报销。",
                        "metadata": {"parent_doc": "01_travel_standards.txt"},
                    },
                    {
                        "id": 4,
                        "content": "面板知识：住宿优先协议酒店。",
                        "metadata": {"category": "住宿"},
                    },
                ],
            },
        }
    ]
    assert instance._generate_human_response(results) is True
    output = instance._buffer.getvalue()
    assert "参考来源" in output, output
    assert "01_travel_standards.txt" in output, output
    assert "参考片段" in output and "凭发票报销" in output, output


def test_cli_rag_sources_escape_rich_markup():
    """来源 / 片段里的第三方内容按字面展示，不触发 rich 标记解析。"""
    instance = _new_cli()
    results = [
        {
            "agent_name": "rag_knowledge",
            "status": "success",
            "data": {
                "answer": "答案",
                "retrieved_documents": [
                    {
                        "id": 1,
                        "content": "片段含 [https://example.com] 与 [red]标记[/red]",
                        "metadata": {"parent_doc": "[/red]"},
                    }
                ],
            },
        }
    ]
    assert instance._generate_human_response(results) is True
    output = instance._buffer.getvalue()
    assert "[/red]" in output, output
    assert "[https://example.com]" in output, output


def test_skill_loader_warns_missing_manifest(tmp_path, capsys):
    from utils.skill_loader import SkillLoader

    empty_dir = tmp_path / "empty-skill"
    empty_dir.mkdir()
    no_fm = tmp_path / "no-frontmatter"
    no_fm.mkdir()
    (no_fm / "SKILL.md").write_text("没有 frontmatter 的内容", encoding="utf-8")
    loader = SkillLoader(str(tmp_path))
    loader.load_skills()
    # 诊断输出必须走 stderr：stdout 要留给 --json 等机器可读输出
    captured = capsys.readouterr()
    assert "missing SKILL.md" in captured.err
    assert captured.out == "", "--json 命令的 stdout 不能被技能加载警告污染"
    assert "no valid" in captured.err
    print("✓ 技能缺 SKILL.md / frontmatter 时有警告")


def test_local_timestamp_text_tolerates_extreme_years():
    """极端年份 / 偏移的展示时间不抛 OverflowError（列表 / 详情不再整条崩溃）。"""
    assert (
        cli_module._local_timestamp_text("0001-01-01T00:00:00+08:00")
        == "0001-01-01 00:00:00"
    )
    assert (
        cli_module._local_timestamp_text("9999-12-31T23:59:59-08:00")
        == "9999-12-31 23:59:59"
    )
    assert (
        cli_module._local_timestamp_text("2026-01-01T00:00:00")
        == "2026-01-01 00:00:00"
    )
    print("✓ 展示时间容忍极端年份")


_DIRECT_RUN_SKIP: dict[str, str] = {
    "test_skill_loader_warns_missing_manifest": "需要 capsys 夹具",
}

if __name__ == "__main__":
    import _stdio
    import _tmp_registry

    _stdio.ensure_utf8()
    _tmp_registry.install()
    mp = pytest.MonkeyPatch()
    try:
        test_missing_api_key_exits_cleanly(mp)
    finally:
        mp.undo()
    mp = pytest.MonkeyPatch()
    try:
        test_model_init_failure_exits_cleanly(mp)
    finally:
        mp.undo()
    mp = pytest.MonkeyPatch()
    try:
        test_print_stdout_swallows_pipe_errors_only(mp)
    finally:
        mp.undo()
    mp = pytest.MonkeyPatch()
    try:
        test_repl_foreign_cancelled_error_propagates(mp)
    finally:
        mp.undo()
    mp = pytest.MonkeyPatch()
    try:
        test_repl_memory_failure_keeps_session(mp)
    finally:
        mp.undo()
    mp = pytest.MonkeyPatch()
    try:
        test_repl_input_unavailable_exits(mp)
    finally:
        mp.undo()
    test_preference_rendering_robust_and_consistent()
    test_preference_rendering_skips_unmanageable_types()
    test_preference_rendering_skips_explanatory_and_blank()
    test_preference_rendering_skips_oversized_value()
    test_preference_legacy_dict_and_nested_shapes()
    test_local_timestamp_text_tolerates_extreme_years()
    test_agent_error_message_fallback()
    test_status_renders_assistant_plaintext()
    test_status_table_single_lines_multiline_message()
    test_preferences_table_keeps_zero_and_lists()
    test_result_formatter_generic_output_field()
    test_cli_realtime_renders_itinerary_validation_warning()
    test_cli_realtime_renders_rag_sources()
    test_cli_rag_sources_escape_rich_markup()
    print("全部通过（capsys 相关用例请用 pytest 运行）")
    test_agent_error_uses_aggregated_message()
    if _DIRECT_RUN_SKIP:
        print(f"直跑未纳入 {len(_DIRECT_RUN_SKIP)} 个用例（需 pytest 专用夹具或真实服务）")
