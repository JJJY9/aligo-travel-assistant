"""
可靠性工具测试（无需 API Key、不访问网络）

覆盖：可重试错误判定、重试不误伤永久错误、健康检查空响应、熔断器半开单飞、
轮次元数据非列表容错、访问日志控制字符清理。

运行：python tests/test_reliability_utils.py
"""
import asyncio
import os
import sys
import types

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils import circuit_breaker as cb_module
from utils.access_log import format_access_log
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
    print("✓ 访问日志清理 CR/LF 与转义字符")


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


if __name__ == "__main__":
    _mp = pytest.MonkeyPatch()
    try:
        test_retriable_error_classification()
        test_retry_does_not_retry_parse_error_with_500()
        test_health_check_rejects_empty_response(_mp)
        test_half_open_single_probe_and_recovery(_mp)
        test_failure_after_recovery_timeout_reopens(_mp)
        test_build_turn_meta_tolerates_non_list_shapes()
        test_access_log_strips_control_characters()
        test_memory_persistence_error_does_not_trip_breaker()
    finally:
        _mp.undo()
    print("\n全部通过")
