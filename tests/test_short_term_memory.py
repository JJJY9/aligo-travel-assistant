"""
短期记忆（进程内实现）测试（无需 Redis / API Key）

运行：python tests/test_short_term_memory.py
"""
import json
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


class _FlakyRedis:
    """可切换故障的假 Redis（实现短期记忆用到的最小命令集）。"""

    def __init__(self):
        self.fail = False
        self.data: list[str] = []

    def _check(self):
        if self.fail:
            raise ConnectionError("redis down")

    def pipeline(self):
        client = self

        class _Pipe:
            def rpush(self, key, *values):
                client._check()
                client.data.extend(values)

            def ltrim(self, key, start, end):
                client._check()
                if end == -1 and start < 0:
                    client.data = client.data[-abs(start):]

            def expire(self, key, ttl):
                client._check()

            def execute(self):
                client._check()

        return _Pipe()

    def lrange(self, key, start, end):
        # Redis 的 end 是闭区间，-1 表示最后一条（Python 切片 end 是开区间）
        self._check()
        data = list(self.data)
        return data[start:] if end == -1 else data[start:end + 1]

    def delete(self, key):
        self._check()
        self.data.clear()
        return 1

    def eval(self, script, numkeys, key, check, expected):
        self._check()
        if not self.data:
            return 0
        count = 2 if len(self.data) >= 2 else 1
        if check == "1" and expected:
            user_raw = self.data[-2] if count == 2 else self.data[-1]
            if str(json.loads(user_raw).get("content", "")).strip() != expected:
                return 0
        del self.data[-count:]
        return count

    def llen(self, key):
        self._check()
        return len(self.data)

    def lindex(self, key, index):
        self._check()
        return self.data[index]


def test_shadow_cache_mirrors_truncate(monkeypatch):
    """Redis 截断成功时影子缓存同步删除，故障回退不会读回已删除的一轮。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    memory = module.RedisShortTermMemory(client, "s1", max_turns=5, user_id="u1")
    for role, content in [
        ("user", "Q1"),
        ("assistant", "A1"),
        ("user", "Q2"),
        ("assistant", "A2"),
    ]:
        memory.add_message(role, content)
    assert memory.truncate_last_turn("Q2") == 2

    client.fail = True
    messages = memory.get_recent_context()
    assert [item["content"] for item in messages] == ["Q1", "A1"]
    print("✓ 影子缓存镜像截断，故障时不复活已删除轮次")


def test_shadow_cache_mirrors_clear(monkeypatch):
    """清空对话后 Redis 故障回退，影子缓存不能把已清空的对话读回来。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    memory = module.RedisShortTermMemory(client, "s1", max_turns=5, user_id="u1")
    memory.add_message("user", "Q1")
    memory.add_message("assistant", "A1")
    memory.clear()

    client.fail = True
    assert memory.get_recent_context() == []
    print("✓ 影子缓存镜像清空")


def test_outage_writes_replayed_after_recovery(monkeypatch):
    """故障期间新增的消息在 Redis 恢复后补写，上下文不丢轮次。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    memory = module.RedisShortTermMemory(client, "s1", max_turns=5, user_id="u1")
    memory.add_message("user", "Q1")
    memory.add_message("assistant", "A1")

    client.fail = True
    memory.add_message("user", "Q2")
    memory.add_message("assistant", "A2")
    assert [item["content"] for item in memory.get_recent_context()] == [
        "Q1",
        "A1",
        "Q2",
        "A2",
    ]

    client.fail = False
    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    messages = memory.get_recent_context()
    assert [item["content"] for item in messages] == ["Q1", "A1", "Q2", "A2"]
    assert module.redis_short_term_degraded() is False
    print("✓ 故障期消息在恢复后补写")


class _ApplyThenFailRedis(_FlakyRedis):
    """写入已生效但响应丢失：execute 在数据写入后抛连接错误。"""

    def __init__(self):
        super().__init__()
        self.fail_after_apply = False

    def pipeline(self):
        client = self

        class _Pipe:
            def rpush(self, key, *values):
                client._check()
                client.data.extend(values)

            def ltrim(self, key, start, end):
                client._check()
                if end == -1 and start < 0:
                    client.data = client.data[-abs(start):]

            def expire(self, key, ttl):
                client._check()

            def execute(self):
                client._check()
                if client.fail_after_apply:
                    raise ConnectionError("response lost after commit")

        return _Pipe()


def test_outage_backlog_is_bounded(monkeypatch):
    """长时间故障时待补写消息只保留最近窗口，进程内存不会无限增长。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    memory = module.RedisShortTermMemory(client, "s1", max_turns=2, user_id="u1")

    client.fail = True
    for index in range(10):
        memory.add_message("user" if index % 2 == 0 else "assistant", f"m{index}")
    assert len(memory._pending) <= 4, "待补写队列应与滑动窗口一致"

    client.fail = False
    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    messages = memory.get_recent_context()
    assert [item["content"] for item in messages] == ["m6", "m7", "m8", "m9"]
    assert len(client.data) == 4
    print("✓ 故障期待补写队列有界（只保留最近窗口）")


def test_replay_dedupes_committed_payload(monkeypatch):
    """写入已在 Redis 生效但响应丢失时，恢复补写不能重复追加同一条消息。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _ApplyThenFailRedis()
    memory = module.RedisShortTermMemory(client, "s1", max_turns=5, user_id="u1")
    memory.add_message("user", "Q1")
    memory.add_message("assistant", "A1")

    client.fail_after_apply = True
    memory.add_message("user", "Q2")
    assert memory._pending, "响应丢失时消息应进入待补写队列"
    assert len(client.data) == 3, "写入本身已在服务端生效"

    client.fail_after_apply = False
    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    messages = memory.get_recent_context()
    assert [item["content"] for item in messages] == ["Q1", "A1", "Q2"], "补写不能重复"
    assert memory._pending == []
    print("✓ 补写按已提交内容去重（至少一次语义不重复）")


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
    _mp = __import__("pytest").MonkeyPatch()
    try:
        test_shadow_cache_mirrors_truncate(_mp)
    finally:
        _mp.undo()
    _mp = __import__("pytest").MonkeyPatch()
    try:
        test_shadow_cache_mirrors_clear(_mp)
    finally:
        _mp.undo()
    _mp = __import__("pytest").MonkeyPatch()
    try:
        test_outage_writes_replayed_after_recovery(_mp)
    finally:
        _mp.undo()
    _mp = __import__("pytest").MonkeyPatch()
    try:
        test_outage_backlog_is_bounded(_mp)
    finally:
        _mp.undo()
    _mp = __import__("pytest").MonkeyPatch()
    try:
        test_replay_dedupes_committed_payload(_mp)
    finally:
        _mp.undo()
    print("\n全部通过")
