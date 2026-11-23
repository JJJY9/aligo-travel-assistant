"""最近对话滚动摘要测试（MemoryManager，无需真实模型）。

覆盖：消息不足不调用、指纹缓存、窗口变化重算、200 字截断、失败冷却。

运行：python tests/test_memory_recent_summary.py
"""
import asyncio
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


if __name__ == "__main__":
    import _stdio

    _stdio.ensure_utf8()
    test_insufficient_messages_skip_llm()
    test_summary_cached_until_window_changes()
    test_summary_truncated_to_200_chars()
    test_summary_failure_cooldown()
    print("\n全部通过")
