"""
可靠性工具测试（无需 API Key、不访问网络）

覆盖：可重试错误判定、重试不误伤永久错误、健康检查空响应、熔断器半开单飞、
轮次元数据非列表容错、访问日志控制字符清理。

运行：python tests/test_reliability_utils.py
"""
import asyncio
import json
import logging
import os
import sys
import threading
import types

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils import circuit_breaker as cb_module
from utils.access_log import JsonLogFormatter, format_access_log
from utils.circuit_breaker import CircuitBreaker
from utils.errors import MemoryPersistenceError
from utils.llm_resilience import is_retriable_error, retry_with_backoff, run_health_check
from utils.turn_meta import build_turn_meta


def test_retriable_error_classification():
    class APIConnectionError(Exception):
        pass

    class ServerError(Exception):
        status_code = 503

    class BadRequestError(Exception):
        status_code = 400

    assert is_retriable_error(APIConnectionError("Connection error."))
    assert is_retriable_error(ServerError("upstream"))
    assert is_retriable_error(ValueError("Error code: 503 - upstream unavailable"))
    assert is_retriable_error(ValueError("429 Too Many Requests"))
    assert is_retriable_error(MemoryPersistenceError("disk full")) is False
    assert is_retriable_error(BadRequestError("bad request")) is False
    # 报错位置 / token 数里的 500 不能当成 HTTP 500
    assert (
        is_retriable_error(
            ValueError("Expecting ':' delimiter: line 1 column 500 (char 499)")
        )
        is False
    )
    assert is_retriable_error(ValueError("Error code: 400 - bad request")) is False
    print("✓ 可重试错误判定（SDK 连接错误 / 状态码 / 数字误伤）")


def test_retry_does_not_retry_parse_error_with_500():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        raise ValueError("column 500 (char 499)")

    with pytest.raises(ValueError):
        asyncio.run(retry_with_backoff(factory, max_retries=3, base_delay_sec=0.01))
    assert calls["n"] == 1, "永久错误不应重试"
    print("✓ 解析错误（含 500 字样）不触发重试")


def test_health_check_rejects_empty_response(monkeypatch):
    class EmptyReply:
        content = []

    class GoodReply:
        content = [{"type": "text", "text": "1"}]

    state = {"reply": EmptyReply()}

    class FakeModel:
        async def __call__(self, messages):
            return state["reply"]

    fake_module = types.ModuleType("config_agentscope")
    fake_module.create_chat_model = lambda **kwargs: FakeModel()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "config_agentscope", fake_module)

    ok, message = asyncio.run(run_health_check("http://example", "key", "model"))
    assert ok is False and "空响应" in message

    state["reply"] = GoodReply()
    ok, message = asyncio.run(run_health_check("http://example", "key", "model"))
    assert ok is True and message == "ok"
    print("✓ 健康检查拒绝 200 + 空响应（错误密钥 / 网关错误体）")


def test_half_open_single_probe_and_recovery(monkeypatch):
    times = {"now": 100.0}
    monkeypatch.setattr(cb_module.time, "monotonic", lambda: times["now"])
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout_sec=10, half_open_successes=2
    )
    breaker.record_failure()
    assert breaker.state.value == "open"
    assert breaker.allow_call() is False

    times["now"] = 111.0
    assert breaker.allow_call() is True, "恢复超时后进入半开并放行一个试探"
    assert breaker.allow_call() is False, "半开期间不再放行更多请求"
    times["now"] = 115.0
    breaker.record_success()
    assert breaker.allow_call() is True
    breaker.record_success()
    assert breaker.state.value == "closed"
    print("✓ 熔断器半开单飞探测并恢复")


def test_failure_after_recovery_timeout_reopens(monkeypatch):
    times = {"now": 100.0}
    monkeypatch.setattr(cb_module.time, "monotonic", lambda: times["now"])
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout_sec=10, half_open_successes=2
    )
    breaker.record_failure()
    assert breaker.allow_call() is False

    times["now"] = 111.0
    # 超时后、下一次 allow_call 之前到达的失败：把状态重新打回 OPEN 并刷新计时
    breaker.record_failure()
    assert breaker.state.value == "open"

    times["now"] = 119.0
    assert breaker.state.value == "open", "刷新后的 10 秒内不应再次半开"
    print("✓ 半开窗口内的迟到失败重新打开熔断并刷新计时")


def test_build_turn_meta_tolerates_non_list_shapes():
    meta = build_turn_meta(
        {"agent_schedule": 1, "intents": {"type": "x"}},
        {"results": "oops"},
    )
    assert meta["schedule"] == []
    assert meta["agents"] == []
    assert meta["intents"] == []
    print("✓ 轮次元数据对非列表 schedule / intents / results 容错")


def test_access_log_strips_control_characters():
    line = format_access_log(
        "GET", "/api/x\n2026-01-01 00:00:00 INFO fake", 200, 1.2, "rid", fmt="text"
    )
    assert "\n" not in line
    assert "2026-01-01" in line  # 内容保留但被压成同一行
    escaped = format_access_log("GET", "/x\x1b[31m", 200, 1.0, "rid", fmt="text")
    assert "\x1b" not in escaped
    # C1 控制符（含 NEL）与 U+2028/U+2029 行分隔符同样不能透出
    poisoned = format_access_log(
        "GET", "/a\x85b\u2028c\u2029d", 200, 1.0, "rid", fmt="text"
    )
    assert "\x85" not in poisoned
    assert "\u2028" not in poisoned and "\u2029" not in poisoned
    json_line = format_access_log(
        "GET", "/a\x85b\u2028c", 200, 1.0, "rid", fmt="json"
    )
    assert "\u2028" not in json_line
    assert len(json_line.splitlines()) == 1, "JSON 日志不能被行分隔符拆断"
    print("✓ 访问日志清理 CR/LF 与转义字符")


def test_access_log_truncates_user_controlled_fields():
    """超长路径 / 方法不能按请求放大写日志（404 路径不受限流）。"""
    long_path = "/api/" + "a" * 60_000
    line = format_access_log("GET", long_path, 404, 1.0, "rid", fmt="json")
    parsed = json.loads(line)
    assert len(parsed["path"]) == 200, "路径必须截断到 200 字符"
    assert len(line) < 1000

    text = format_access_log("GET", long_path, 404, 1.0, "rid", fmt="text")
    assert len(text) < 300, "文本模式下同样不能透出超长路径"

    parsed_method = json.loads(
        format_access_log("M" * 1000, "/x", 200, 1.0, "rid", fmt="json")
    )
    assert len(parsed_method["method"]) == 16, "方法名与指标同一上限"
    print("✓ 访问日志截断超长路径 / 方法")


def test_json_formatter_sanitizes_any_message():
    """JsonLogFormatter 的普通消息同样不能把单条 JSON 记录拆成多行。"""
    poison = "北京" + chr(0x2028) + "X" + chr(0x85) + "Y"
    record = logging.LogRecord(
        "weather",
        logging.WARNING,
        __file__,
        1,
        "天气查询失败（%s）：%s",
        (poison, "上游不可达"),
        None,
    )
    line = JsonLogFormatter().format(record)
    assert chr(0x2028) not in line and chr(0x85) not in line
    assert len(line.splitlines()) == 1, "单条记录必须保持一行合法 JSON"
    parsed = json.loads(line)
    assert "北京" in parsed["message"] and "上游不可达" in parsed["message"]
    print("✓ JSON 日志格式化器清洗任意日志消息")


def test_json_formatter_includes_exc_info():
    """带异常栈的日志记录输出 exc_info 字段（单行 JSON）。"""
    try:
        1 / 0
    except ZeroDivisionError:
        exc_info = sys.exc_info()
    record = logging.LogRecord(
        "unit",
        logging.ERROR,
        __file__,
        1,
        "处理失败：%s",
        ("boom",),
        exc_info,
    )
    line = JsonLogFormatter().format(record)
    assert len(line.splitlines()) == 1
    parsed = json.loads(line)
    assert "ZeroDivisionError" in parsed["exc_info"]
    assert "Traceback" in parsed["exc_info"]
    print("✓ JSON 日志包含异常栈且保持单行")


def test_release_probe_returns_half_open_slot(monkeypatch):
    """归还半开名额：试探被取消后其它请求可立即重试；已记录后不再放行。"""
    times = {"now": 100.0}
    monkeypatch.setattr(cb_module.time, "monotonic", lambda: times["now"])
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout_sec=10, half_open_successes=2
    )
    breaker.record_failure()
    times["now"] = 111.0
    assert breaker.allow_call() is True
    assert breaker.allow_call() is False
    breaker.release_probe()
    assert breaker.allow_call() is True, "归还名额后应立即允许新的试探"

    breaker.record_failure()
    assert breaker.allow_call() is False
    breaker.release_probe()
    assert breaker.allow_call() is False, "已记录失败后 release_probe 不应放行"
    print("✓ 半开试探名额可归还且不会绕过失败")


def test_conversation_ask_releases_probe_on_cancel(monkeypatch):
    """半开试探请求被取消（用户停止 / 断网）：名额必须归还，否则其它用户被拒。"""
    from agents.conversation import ConversationRuntime

    times = {"now": 100.0}
    monkeypatch.setattr(cb_module.time, "monotonic", lambda: times["now"])
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout_sec=60, half_open_successes=1
    )
    breaker.record_failure()
    times["now"] = 161.0

    runtime = object.__new__(ConversationRuntime)
    runtime.user_id = "u-probe"
    runtime.session_id = "s-probe"
    runtime.model = None
    runtime.circuit_breaker = breaker
    runtime._probe_recorded = False

    class _Memory:
        def __init__(self):
            self.long_term = self
            self.short_term = self

        def reload_long_term(self):
            pass

        def get_preference(self):
            return {}

        def get_recent_context(self, n_turns=5):
            return []

        async def get_long_term_summary_async(self, max_messages=30):
            return ""

        def add_message(self, role, content, metadata=None):
            pass

    class _Intention:
        async def reply(self, context):
            raise asyncio.CancelledError

    runtime.memory = _Memory()
    runtime.intention_agent = _Intention()
    runtime.orchestrator = object()

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await runtime.ask("你好")

    asyncio.run(scenario())
    assert breaker.allow_call() is True, "取消后应归还半开试探名额"
    print("✓ 半开试探在被取消时归还名额")


def test_probe_owner_only(monkeypatch):
    """半开名额归持有线程：其它线程的归还 / 迟到成功都不能消费或清除。"""
    times = {"now": 100.0}
    monkeypatch.setattr(cb_module.time, "monotonic", lambda: times["now"])
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout_sec=10, half_open_successes=1
    )
    breaker.record_failure()
    times["now"] = 111.0
    assert breaker.allow_call() is True, "主线程占用试探名额"

    done = {}

    def other_thread():
        breaker.release_probe()
        breaker.record_success()
        done["ran"] = True

    thread = threading.Thread(target=other_thread)
    thread.start()
    thread.join()
    assert done["ran"]
    assert breaker.state.value == "half_open", "迟到成功不能关闭熔断器"
    assert breaker.allow_call() is False, "名额仍归持有线程"

    breaker.record_success()  # 持有者成功：half_open_successes=1 → CLOSED
    assert breaker.state.value == "closed"
    print("✓ 半开名额只归持有线程")


def test_late_success_cannot_close_breaker(monkeypatch):
    """熔断打开前放行的慢调用迟到返回时，不能凑够半开成功计数关闭熔断器。"""
    times = {"now": 100.0}
    monkeypatch.setattr(cb_module.time, "monotonic", lambda: times["now"])
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout_sec=10, half_open_successes=2
    )
    breaker.record_failure()
    times["now"] = 111.0
    assert breaker.allow_call() is True

    def late_successes():
        breaker.record_success()
        breaker.record_success()

    thread = threading.Thread(target=late_successes)
    thread.start()
    thread.join()
    assert breaker.state.value == "half_open", "没有任何真实试探完成时不能关闭"
    assert breaker.allow_call() is False
    print("✓ 迟到成功不能关闭熔断器")


def test_late_failure_cannot_reopen_half_open(monkeypatch):
    """熔断打开前放行的慢请求迟到失败时，不能重开半开或清掉当前试探名额。"""
    times = {"now": 100.0}
    monkeypatch.setattr(cb_module.time, "monotonic", lambda: times["now"])
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout_sec=10, half_open_successes=1
    )
    breaker.record_failure()
    times["now"] = 111.0
    assert breaker.allow_call() is True, "本线程占用试探名额"

    done = {}

    def late_failure():
        breaker.record_failure()
        done["ran"] = True

    thread = threading.Thread(target=late_failure)
    thread.start()
    thread.join()
    assert done["ran"]
    assert breaker.state.value == "half_open", "迟到失败不能把半开重开为 open"
    assert breaker.allow_call() is False, "名额仍归持有线程"
    breaker.record_success()
    assert breaker.state.value == "closed", "持试探者的成功仍能正常关闭熔断"
    print("✓ 迟到失败不能重开半开熔断")


def test_retry_jitter_respects_max_delay(monkeypatch):
    """抖动在截断之前应用：实际退避不超过 max_delay_sec。"""
    import utils.llm_resilience as resilience_module

    delays: list[float] = []

    async def fake_sleep(sec):
        delays.append(sec)

    monkeypatch.setattr(resilience_module.asyncio, "sleep", fake_sleep)

    async def always_fail():
        raise ConnectionError("boom")

    with pytest.raises(ConnectionError):
        asyncio.run(
            resilience_module.retry_with_backoff(
                always_fail,
                max_retries=5,
                base_delay_sec=2.0,
                max_delay_sec=2.0,
                jitter=True,
            )
        )
    assert delays, "应发生重试退避"
    assert all(delay <= 2.0 + 1e-9 for delay in delays), delays
    print("✓ 抖动不会突破最大退避上限")


def test_memory_persistence_error_does_not_trip_breaker():
    """记忆写盘失败不能计入熔断器（磁盘故障与 LLM 健康无关）。"""
    from agents.conversation import ConversationRuntime
    from utils.circuit_breaker import CircuitBreaker

    runtime = object.__new__(ConversationRuntime)
    runtime.circuit_breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout_sec=60, half_open_successes=1
    )

    async def disk_error():
        raise MemoryPersistenceError("disk full")

    with pytest.raises(MemoryPersistenceError):
        asyncio.run(
            runtime._call_with_resilience(
                disk_error, max_retries=0, base_delay=0.0, max_delay=0.0
            )
        )
    status = runtime.circuit_breaker.get_status()
    assert status["failure_count"] == 0, "写盘失败不应计入熔断"
    assert runtime.circuit_breaker.state.value == "closed"

    async def llm_error():
        raise RuntimeError("llm down")

    with pytest.raises(RuntimeError):
        asyncio.run(
            runtime._call_with_resilience(
                llm_error, max_retries=0, base_delay=0.0, max_delay=0.0
            )
        )
    assert runtime.circuit_breaker.get_status()["failure_count"] == 1
    print("✓ 记忆写盘失败不计入熔断器")


def test_response_text_dict_and_scalar_shapes():
    from utils.llm_resilience import _response_text

    assert _response_text({"text": "T"}) == "T"
    assert _response_text({"content": "C"}) == "C"
    assert _response_text([{"text": "甲"}, {"text": "乙"}]) == "甲乙"
    assert _response_text(123) == "123"
    assert _response_text(None) == ""
    print("✓ _response_text 的 dict / 列表 / 标量形状")


def test_retry_error_edges_and_response_shapes():
    """retry 的保留分支：CircuitOpenError 不重试、负重试次数、status_code 属性。"""
    from utils.circuit_breaker import CircuitOpenError

    calls = {"n": 0}

    async def open_breaker():
        calls["n"] += 1
        raise CircuitOpenError("open")

    with pytest.raises(CircuitOpenError):
        asyncio.run(
            retry_with_backoff(
                open_breaker, max_retries=3, base_delay_sec=0.0
            )
        )
    assert calls["n"] == 1, "熔断开启时不应重试"

    async def never_called():
        raise AssertionError("不应执行")

    with pytest.raises(RuntimeError, match="retry loop ended"):
        asyncio.run(
            retry_with_backoff(never_called, max_retries=-1, base_delay_sec=0.0)
        )

    class WithResponse(Exception):
        def __init__(self):
            self.response = types.SimpleNamespace(status_code=503)

    assert is_retriable_error(WithResponse()) is True

    from utils.llm_resilience import _response_text

    assert _response_text("纯文本") == "纯文本"
    assert _response_text([types.SimpleNamespace(text="甲")]) == "甲"
    assert _response_text({"other": 1}) == ""
    print("✓ retry 保留分支与 _response_text 形状")


def test_health_check_error_paths(monkeypatch):
    # AgentScope 未安装：import 失败分支
    monkeypatch.setitem(sys.modules, "config_agentscope", None)
    ok, message = asyncio.run(run_health_check("http://x", "k", "m"))
    assert ok is False and "AgentScope not installed" in message

    # 模型调用抛异常：返回异常文本（不抛出）
    def boom(**kwargs):
        raise ConnectionError("gateway down")

    fake_module = types.ModuleType("config_agentscope")
    fake_module.create_chat_model = boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "config_agentscope", fake_module)
    ok, message = asyncio.run(run_health_check("http://x", "k", "m"))
    assert ok is False and "gateway down" in message
    print("✓ 健康检查的 ImportError 与异常路径")


def test_health_check_enforces_overall_timeout(monkeypatch):
    """timeout_sec 是整体预算：慢模型被截断，且 SDK 内建重试关闭。"""
    import time as _time

    captured = {}

    class _SlowModel:
        async def __call__(self, messages):
            await asyncio.sleep(5)
            return "ok"

    def fake_create(**kwargs):
        captured["kwargs"] = kwargs
        return _SlowModel()

    fake_module = types.ModuleType("config_agentscope")
    fake_module.create_chat_model = fake_create  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "config_agentscope", fake_module)
    started = _time.monotonic()
    ok, message = asyncio.run(run_health_check("http://x", "k", "m", timeout_sec=0.2))
    elapsed = _time.monotonic() - started
    assert ok is False and "超时" in message
    assert elapsed < 2.0, elapsed
    assert captured["kwargs"].get("max_retries") == 0, "健康检查必须关闭 SDK 重试"
    print("✓ 健康检查整体超时与 SDK 重试关闭")


def test_half_open_probe_lease_covers_slow_probe(monkeypatch):
    """慢试探仍在跑时不能放行第二个（旧的 60s 租约会被覆盖归属）。"""
    times = {"now": 100.0}
    monkeypatch.setattr(cb_module.time, "monotonic", lambda: times["now"])
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout_sec=10, half_open_successes=1
    )
    breaker.record_failure()
    times["now"] = 111.0
    assert breaker.allow_call() is True
    # 超过 recovery_timeout（10s）但仍在试探租约内：不得覆盖归属
    times["now"] = 150.0
    assert breaker.allow_call() is False, "慢试探未结束前不能放行第二个"
    # 试探完成（失败）才重新打开；结果来自原持有者
    breaker.record_failure()
    assert breaker.state.value == "open"

    # 超过租约视为卡死回收：允许新的试探
    breaker2 = CircuitBreaker(
        failure_threshold=1, recovery_timeout_sec=10, half_open_successes=1
    )
    breaker2.record_failure()
    times["now"] = 200.0
    assert breaker2.allow_call() is True
    breaker2._probe_lease_sec = 5.0
    times["now"] = 206.0
    assert breaker2.allow_call() is True, "超租约后才允许回收试探名额"
    print("✓ 半开试探租约覆盖慢调用（60s 不再提前覆盖归属）")


def test_live_probe_task_not_preempted_after_lease(monkeypatch):
    """租约到期但试探任务仍在跑：不得覆盖归属（长回合可能远超租约）。"""
    times = {"now": 100.0}
    monkeypatch.setattr(cb_module.time, "monotonic", lambda: times["now"])
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout_sec=10, half_open_successes=1
    )
    breaker.record_failure()
    times["now"] = 111.0

    async def scenario():
        assert breaker.allow_call() is True  # 本任务（任务 A）持有试探
        # 远超租约：任务 A 仍在跑
        times["now"] = 111.0 + breaker._probe_lease_sec + 1

        async def other():
            return breaker.allow_call()

        allowed = await asyncio.create_task(other())
        assert allowed is False, "持有试探的任务仍存活时不能放行第二个"
        # 原试探的真实失败仍计入并重开熔断
        breaker.record_failure()
        assert breaker.state.value == "open"

    asyncio.run(scenario())
    print("✓ 租约到期但试探任务仍存活时不覆盖归属")


def test_finished_probe_task_reclaimed_within_lease(monkeypatch):
    """试探任务已结束（取消 / 异常退出）时名额立即回收，不必等租约到期。"""
    times = {"now": 100.0}
    monkeypatch.setattr(cb_module.time, "monotonic", lambda: times["now"])
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout_sec=10, half_open_successes=1
    )
    breaker.record_failure()
    times["now"] = 111.0

    async def scenario():
        holder = {}

        async def stale_probe():
            holder["allowed"] = breaker.allow_call()
            await asyncio.sleep(60)

        task = asyncio.create_task(stale_probe())
        await asyncio.sleep(0)
        assert holder.get("allowed") is True
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # 任务已结束、租约仍远未到期：允许新试探回收名额
        assert breaker.allow_call() is True

    asyncio.run(scenario())
    print("✓ 试探任务结束后名额立即回收")


if __name__ == "__main__":
    _mp = pytest.MonkeyPatch()
    try:
        test_retriable_error_classification()
        test_retry_does_not_retry_parse_error_with_500()
        test_health_check_rejects_empty_response(_mp)
        test_half_open_single_probe_and_recovery(_mp)
        test_half_open_probe_lease_covers_slow_probe(_mp)
        test_live_probe_task_not_preempted_after_lease(_mp)
        test_finished_probe_task_reclaimed_within_lease(_mp)
        test_failure_after_recovery_timeout_reopens(_mp)
        test_build_turn_meta_tolerates_non_list_shapes()
        test_access_log_strips_control_characters()
        test_access_log_truncates_user_controlled_fields()
        test_release_probe_returns_half_open_slot(_mp)
        test_probe_owner_only(_mp)
        test_late_success_cannot_close_breaker(_mp)
        test_late_failure_cannot_reopen_half_open(_mp)
        test_retry_jitter_respects_max_delay(_mp)
        test_response_text_dict_and_scalar_shapes()
        test_retry_error_edges_and_response_shapes()
        test_health_check_error_paths(_mp)
        test_health_check_enforces_overall_timeout(_mp)
        test_conversation_ask_releases_probe_on_cancel(_mp)
        test_memory_persistence_error_does_not_trip_breaker()
    finally:
        _mp.undo()
    print("\n全部通过")
