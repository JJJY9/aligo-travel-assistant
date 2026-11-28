"""最近对话滚动摘要测试（MemoryManager，无需真实模型）。

覆盖：消息不足不调用、指纹缓存、窗口变化重算、200 字截断、失败冷却。

运行：python tests/test_memory_recent_summary.py
"""
import asyncio
import logging
import os
import shutil
import sys
import tempfile

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from context.memory_manager import MemoryManager


class _CountingModel:
    def __init__(self, response="最近去了杭州，预算 1000 元，还差返程时间"):
        self.response = response
        self.calls = 0
        self.prompts = []

    async def __call__(self, messages, **kwargs):
        self.calls += 1
        self.prompts.append(messages[-1]["content"])
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _manager(model):
    storage = tempfile.mkdtemp()
    manager = MemoryManager(
        user_id="recent_summary_user",
        session_id="recent_summary_session",
        storage_path=storage,
        llm_model=model,
    )
    return manager, storage


def _add_turn(manager, user_text, assistant_text):
    manager.short_term.add_message("user", user_text)
    manager.short_term.add_message("assistant", assistant_text)


def test_insufficient_messages_skip_llm():
    """最近对话不足 4 条时不调用模型。"""
    model = _CountingModel()
    manager, storage = _manager(model)
    try:
        _add_turn(manager, "你好", "你好呀")
        assert asyncio.run(manager.get_recent_summary_async()) == ""
        assert model.calls == 0
        print("✓ 消息不足时不调用摘要模型")
    finally:
        shutil.rmtree(storage, ignore_errors=True)


def test_summary_cached_until_window_changes():
    """窗口内容不变时复用摘要；新增消息后重新生成。"""
    model = _CountingModel()
    manager, storage = _manager(model)
    try:
        _add_turn(manager, "我下周去杭州", "好的，预算多少？")
        _add_turn(manager, "预算 1000 元", "已记录")
        first = asyncio.run(manager.get_recent_summary_async())
        assert first == model.response
        assert model.calls == 1

        second = asyncio.run(manager.get_recent_summary_async())
        assert second == first
        assert model.calls == 1, "窗口未变化时不能重复调用模型"

        _add_turn(manager, "还要去灵隐寺", "已加入必去项")
        third = asyncio.run(manager.get_recent_summary_async())
        assert third == first
        assert model.calls == 2, "窗口变化后应重新生成"
        print("✓ 滚动摘要指纹缓存与窗口变化重算")
    finally:
        shutil.rmtree(storage, ignore_errors=True)


def test_summary_truncated_to_200_chars():
    """模型输出超过 200 字时截断。"""
    model = _CountingModel(response="摘" * 300)
    manager, storage = _manager(model)
    try:
        _add_turn(manager, "一", "二")
        _add_turn(manager, "三", "四")
        summary = asyncio.run(manager.get_recent_summary_async())
        assert len(summary) == 200
        assert summary == "摘" * 200
        print("✓ 摘要截断到 200 字")
    finally:
        shutil.rmtree(storage, ignore_errors=True)


def test_summary_failure_cooldown():
    """模型失败后进入冷却：下一次直接返回空且不再调用。"""
    model = _CountingModel(response=RuntimeError("boom"))
    manager, storage = _manager(model)
    try:
        _add_turn(manager, "一", "二")
        _add_turn(manager, "三", "四")
        assert asyncio.run(manager.get_recent_summary_async()) == ""
        assert model.calls == 1
        assert asyncio.run(manager.get_recent_summary_async()) == ""
        assert model.calls == 1, "冷却期内不能重复调用模型"
        print("✓ 摘要失败冷却")
    finally:
        shutil.rmtree(storage, ignore_errors=True)


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def test_summary_failure_logs_are_redacted():
    """摘要失败的日志（含 traceback）不能带上网关回显的 API Key。"""
    key = "sk-SECRET1234567890abcdef"
    error = RuntimeError(
        "Error code: 401 - Incorrect API key provided: " + key
    )
    model = _CountingModel(response=error)
    manager, storage = _manager(model)
    handler = _ListHandler()
    logger = logging.getLogger("context.memory_manager")
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        _add_turn(manager, "一", "二")
        _add_turn(manager, "三", "四")
        assert asyncio.run(manager.get_recent_summary_async()) == ""

        # 长期摘要：其它会话窗口非空才会走到模型调用
        manager._summary_inputs = lambda: (  # noqa: SLF001 - 注入窗口
            [
                {
                    "session_id": "other",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "role": "user",
                    "content": "hi",
                }
            ],
            [],
        )
        assert asyncio.run(manager.get_long_term_summary_async()) == ""

        joined = "\n".join(handler.messages)
        assert "生成最近对话摘要失败" in joined
        assert "Failed to generate long-term summary" in joined
        assert key not in joined, joined
        assert "***" in joined, joined
        print("✓ 摘要失败日志已脱敏")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        shutil.rmtree(storage, ignore_errors=True)


def test_recent_summary_fingerprint_covers_full_content():
    """同长度尾部变化也必须重算：指纹不能只看前 500 字符。"""
    model = _CountingModel()
    manager, storage = _manager(model)
    try:
        _add_turn(manager, "一", "二")
        _add_turn(manager, "三", "前" * 500 + "甲" * 300)
        first = asyncio.run(manager.get_recent_summary_async())
        assert first == model.response
        assert model.calls == 1

        # 保留前缀 500 字符、替换尾部为同长度内容
        messages = manager.short_term.messages
        content = messages[-1]["content"]
        messages[-1]["content"] = content[:500] + "乙" * (len(content) - 500)
        second = asyncio.run(manager.get_recent_summary_async())
        assert second == model.response
        assert model.calls == 2, "正文尾部变化不能命中旧摘要缓存"
        assert "乙" in model.prompts[-1]
        print("✓ 最近摘要指纹覆盖完整正文")
    finally:
        shutil.rmtree(storage, ignore_errors=True)


def test_recent_summary_accumulates_previous_window():
    """窗口滑出后的重算把上一版摘要并入 prompt，早期约定不随窗口消失。"""
    model = _CountingModel(response="预算 1000")
    manager, storage = _manager(model)
    try:
        for index in range(5):
            _add_turn(manager, f"第 {index} 轮", "好的")
        first = asyncio.run(manager.get_recent_summary_async())
        assert first == "预算 1000"
        assert model.calls == 1

        for index in range(5, 12):
            _add_turn(manager, f"第 {index} 轮", "好的")
        second = asyncio.run(manager.get_recent_summary_async())
        assert second == "预算 1000"
        assert model.calls == 2
        prompt = model.prompts[-1]
        assert "【上一版摘要" in prompt
        assert "预算 1000" in prompt
        print("✓ 最近摘要跨窗口累积")
    finally:
        shutil.rmtree(storage, ignore_errors=True)


def test_long_term_summary_flags_unavailable_on_store_failure():
    """摘要读取失败要区别于「真的没有历史」（last_summary_unavailable）。"""
    from utils.errors import MemoryPersistenceError

    model = _CountingModel()
    manager, storage = _manager(model)
    try:
        original = manager.long_term.get_chat_history

        def boom(*args, **kwargs):
            raise MemoryPersistenceError("db down")

        manager.long_term.get_chat_history = boom  # type: ignore[method-assign]
        assert asyncio.run(manager.get_long_term_summary_async()) == ""
        assert manager.last_summary_unavailable is True
        assert model.calls == 0

        manager._summary_failure_until = 0.0
        manager.long_term.get_chat_history = original  # type: ignore[method-assign]
        assert asyncio.run(manager.get_long_term_summary_async()) == ""
        assert manager.last_summary_unavailable is False, "真无历史不能标不可用"
        print("✓ 摘要失败标记不可用，真无历史不标记")
    finally:
        shutil.rmtree(storage, ignore_errors=True)


def test_invalidate_all_summary_caches_clears_cooldown_cache():
    """删除会话后旧摘要（失败冷却期直接回放）必须能被全局失效。"""
    model = _CountingModel()
    manager, storage = _manager(model)
    try:
        manager._summary_cache = ((123,), "已删除会话的旧摘要")
        manager._recent_summary_cache = (((),), "旧摘要")
        import time as _time

        manager._summary_failure_until = _time.monotonic() + 60
        MemoryManager.invalidate_all_summary_caches()
        assert manager._summary_cache is None
        assert manager._recent_summary_cache is None
        assert asyncio.run(manager.get_long_term_summary_async()) == ""
        assert model.calls == 0, "冷却已清除，但空历史应直接返回空"
        print("✓ 摘要缓存全局失效")
    finally:
        shutil.rmtree(storage, ignore_errors=True)


def test_summary_trip_window_uses_dates_not_insertion_order():
    """恢复大量旧行程后，总结窗口仍取出发日期最新的 20 条。"""
    model = _CountingModel()
    manager, storage = _manager(model)
    try:
        recent = [
            {
                "origin": "上海",
                "destination": f"城市{index}",
                "start_date": f"2026-06-{(index % 28) + 1:02d}",
                "purpose": "出差",
            }
            for index in range(30)
        ]
        old = [
            {
                "origin": "上海",
                "destination": f"旧地{index}",
                "start_date": f"2019-{(index % 12) + 1:02d}-{(index % 28) + 1:02d}",
                "purpose": "旅游",
            }
            for index in range(500)
        ]
        manager.long_term.save_trip_histories(recent + old)
        _, trips = manager._summary_inputs()
        assert len(trips) == 20
        assert all(str(trip.get("start_date", "")).startswith("2026-") for trip in trips), [
            trip.get("start_date") for trip in trips
        ]
        print("✓ 总结行程窗口按出发日期取最近")
    finally:
        shutil.rmtree(storage, ignore_errors=True)


if __name__ == "__main__":
    import _stdio

    _stdio.ensure_utf8()
    test_insufficient_messages_skip_llm()
    test_summary_cached_until_window_changes()
    test_summary_truncated_to_200_chars()
    test_summary_failure_cooldown()
    test_summary_failure_logs_are_redacted()
    test_recent_summary_fingerprint_covers_full_content()
    test_recent_summary_accumulates_previous_window()
    test_long_term_summary_flags_unavailable_on_store_failure()
    test_invalidate_all_summary_caches_clears_cooldown_cache()
    test_summary_trip_window_uses_dates_not_insertion_order()
    print("\n全部通过")
