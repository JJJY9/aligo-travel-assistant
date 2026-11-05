"""
短期记忆（进程内实现）测试（无需 Redis / API Key）

运行：python tests/test_short_term_memory.py
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from context.short_term_memory import ShortTermMemory


def _seeded() -> ShortTermMemory:
    memory = ShortTermMemory()
    for role, content in [
        ("user", "问题 1"),
        ("assistant", "回答 1"),
        ("user", "问题 2"),
        ("assistant", "回答 2"),
    ]:
        memory.add_message(role, content)
    return memory


def test_truncate_pair():
    memory = _seeded()
    assert memory.truncate_last_turn() == 2
    assert [item["content"] for item in memory.messages] == ["问题 1", "回答 1"]
    print("✓ 删除最后一轮（成对）")


def test_truncate_expect_mismatch_keeps_history():
    memory = _seeded()
    assert memory.truncate_last_turn("其它问题") == 0
    assert len(memory.messages) == 4, "内容不一致时不应删除任何消息"
    print("✓ expect 不一致时保留历史")


def test_truncate_trailing_user_only():
    memory = ShortTermMemory()
    memory.add_message("user", "只有提问")
    assert memory.truncate_last_turn("只有提问") == 1
    assert memory.messages == []
    print("✓ 只剩提问时只删除提问")


def test_truncate_empty():
    assert ShortTermMemory().truncate_last_turn() == 0
    print("✓ 空历史返回 0")


def test_max_turns_evicts_oldest():
    memory = ShortTermMemory(max_turns=2)
    for index in range(3):
        memory.add_message("user", f"问题 {index}")
        memory.add_message("assistant", f"回答 {index}")
    assert len(memory.messages) == 4
    assert memory.messages[0]["content"] == "问题 1"
    print("✓ max_turns 滑动窗口淘汰最旧消息")


def test_recent_context_and_string():
    memory = _seeded()
    recent = memory.get_recent_context(1)
    assert [item["content"] for item in recent] == ["问题 2", "回答 2"]
    text = memory.get_context_string(1)
    assert "用户: 问题 2" in text and "助手: 回答 2" in text
    assert "问题 1" not in text
    print("✓ get_recent_context / get_context_string 按轮截取")


def test_statistics_and_clear():
    memory = _seeded()
    stats = memory.get_statistics()
    assert stats["total_messages"] == 4
    assert stats["max_turns"] == 10
    assert stats["oldest_message_time"] and stats["newest_message_time"]
    memory.clear()
    assert memory.messages == []
    assert memory.get_statistics()["total_messages"] == 0
    assert memory.get_context_string() == "无历史对话"
    print("✓ 统计信息与 clear")


class _BrokenRedis:
    """所有操作都抛连接错误的 Redis 桩。"""

    def pipeline(self):
        raise ConnectionError("redis down")

    def lrange(self, *args, **kwargs):
        raise ConnectionError("redis down")

    def delete(self, *args, **kwargs):
        raise ConnectionError("redis down")

    def eval(self, *args, **kwargs):
        raise ConnectionError("redis down")

    def llen(self, *args, **kwargs):
        raise ConnectionError("redis down")

    def lindex(self, *args, **kwargs):
        raise ConnectionError("redis down")


def test_redis_failure_falls_back_to_memory(monkeypatch):
    """Redis 运行中故障时短期记忆回退进程内缓存，对话不应 500。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    memory = module.RedisShortTermMemory(
        _BrokenRedis(), "s1", max_turns=2, user_id="u1"
    )
    memory.add_message("user", "问题")
    memory.add_message("assistant", "回答")
    assert [m["content"] for m in memory.get_recent_context()] == ["问题", "回答"]
    assert memory.get_statistics()["total_messages"] == 2
    assert memory.truncate_last_turn("问题") == 2
    assert memory.get_recent_context() == []
    assert module.redis_short_term_degraded() is True
    print("✓ Redis 短期记忆故障回退进程内缓存")


def test_redis_recovery_resets_degraded(monkeypatch):
    """冷却结束后一次成功的 Redis 调用应清除降级标记。"""
    import time

    from context import short_term_memory as module

    memory = module.RedisShortTermMemory(_BrokenRedis(), "s1")
    # 冷却结束（标记为过去）后走 Redis 成功路径，降级标记保持清除
    monkeypatch.setattr(module, "_redis_degraded_until", time.monotonic() - 1)
    assert memory._redis(lambda: "ok", lambda: "fallback") == "ok"
    assert module.redis_short_term_degraded() is False
    print("✓ Redis 恢复后清除降级标记")


if __name__ == "__main__":
    test_truncate_pair()
    test_truncate_expect_mismatch_keeps_history()
    test_truncate_trailing_user_only()
    test_truncate_empty()
    test_max_turns_evicts_oldest()
    test_recent_context_and_string()
    test_statistics_and_clear()
    _mp = __import__("pytest").MonkeyPatch()
    try:
        test_redis_failure_falls_back_to_memory(_mp)
    finally:
        _mp.undo()
    _mp = __import__("pytest").MonkeyPatch()
    try:
        test_redis_recovery_resets_degraded(_mp)
    finally:
        _mp.undo()
    print("\n全部通过")
