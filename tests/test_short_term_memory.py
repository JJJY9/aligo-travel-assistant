"""
短期记忆（进程内实现）测试（无需 Redis / API Key）

运行：python tests/test_short_term_memory.py
"""
import json
import os
import sys

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from context.short_term_memory import ShortTermMemory


@pytest.fixture(autouse=True)
def _isolated_short_term_state():
    """隔离进程级状态：注册表与降级冷却不能跨用例泄漏。"""
    from context import short_term_memory as module

    with module._PENDING_JOURNALS_LOCK:
        module._PENDING_JOURNALS.clear()
    module._redis_degraded_until = 0.0
    yield
    with module._PENDING_JOURNALS_LOCK:
        module._PENDING_JOURNALS.clear()
    module._redis_degraded_until = 0.0


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


def test_truncate_expect_without_user_message_returns_zero():
    """末尾只有助手消息（无用户提问可校验）时 expect 截断返回 0。"""
    memory = ShortTermMemory()
    memory.add_message("assistant", "只有回答")
    assert memory.truncate_last_turn("问") == 0
    assert len(memory.messages) == 1
    print("✓ 无用户提问时 expect 截断返回 0")


def test_get_context_string_formats_and_skips_bad_entries(monkeypatch):
    """上下文文本：空历史提示、角色格式化；Redis 路径跳过形状非法的条目。"""
    memory = ShortTermMemory()
    assert memory.get_context_string() == "无历史对话"
    memory.add_message("user", "你好")
    memory.add_message("assistant", "您好")
    assert memory.get_context_string() == "用户: 你好\n助手: 您好"

    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    redis_memory = module.RedisShortTermMemory(
        client, "s-ctx", max_turns=5, user_id="u1"
    )
    assert redis_memory.get_context_string() == "无历史对话"
    redis_memory.add_message("user", "你好")
    redis_memory.add_message("assistant", "您好")
    assert redis_memory.get_context_string() == "用户: 你好\n助手: 您好"
    client.data.append("null")
    client.data.append(json.dumps({"role": 1, "content": "x"}))
    client.data.append(json.dumps({"role": "user", "content": None}))
    assert redis_memory.get_context_string() == "用户: 你好\n助手: 您好"
    print("✓ 上下文文本格式化与非法条目跳过")


def test_reset_redis_degraded_clears_cooldown(monkeypatch):
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 12345.0)
    module.reset_redis_degraded()
    assert module._redis_degraded_until == 0.0
    print("✓ reset_redis_degraded 清空降级冷却")


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


def test_none_client_recovers_via_factory(monkeypatch):
    """构造时拿不到客户端（工厂冷却）：恢复后自动重新接入共享 Redis。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = object()
    state = {"calls": 0}

    def factory():
        state["calls"] += 1
        return client if state["calls"] >= 2 else None

    memory = module.RedisShortTermMemory(
        None, "s-factory", user_id="u-factory", client_factory=factory
    )
    result, used = memory._call_redis(lambda: "ok", lambda: "fb")
    assert (result, used) == ("fb", False), "工厂未恢复：走影子缓存"
    assert module.redis_short_term_degraded() is False, (
        "没有客户端不应置位进程级降级标志（会拖累其它健康会话）"
    )
    result, used = memory._call_redis(lambda: "ok", lambda: "fb")
    assert (result, used) == ("ok", True)
    assert memory.client is client
    print("✓ client=None 的实例在工厂恢复后重新接入")


def test_broken_client_re_resolved_after_failure(monkeypatch):
    """有工厂时操作失败丢弃坏客户端：冷却结束经由工厂拿到健康客户端。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    healthy = object()
    memory = module.RedisShortTermMemory(
        _BrokenRedis(),
        "s-heal",
        user_id="u-heal",
        client_factory=lambda: healthy,
    )

    def broken_op():
        raise ConnectionError("redis down")

    memory._call_redis(broken_op, lambda: "fb")
    assert memory.client is None, "失败后应丢弃坏客户端"
    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    result, used = memory._call_redis(lambda: "ok", lambda: "fb")
    assert (result, used) == ("ok", True)
    assert memory.client is healthy
    print("✓ 故障后经工厂重新解析客户端")


class _FlakyRedis:
    """可切换故障的假 Redis（实现短期记忆用到的最小命令集）。"""

    def __init__(self):
        self.fail = False
        self.fail_after_apply = False
        self.data: list[str] = []
        self.applied: dict[str, set[str]] = {}
        self.markers: dict[str, str] = {}
        self.marker_ttls: list[str] = []
        self.eval_calls = 0

    def _check(self):
        if self.fail:
            raise ConnectionError("redis down")

    def lrange(self, key, start, end):
        # Redis 的 end 是闭区间，-1 表示最后一条（Python 切片 end 是开区间）
        self._check()
        data = list(self.data)
        return data[start:] if end == -1 else data[start:end + 1]

    def delete(self, *keys):
        self._check()
        for key in keys:
            if key.endswith(":applied"):
                self.applied.pop(key, None)
            elif ":op:" in key:
                self.markers.pop(key, None)
            else:
                self.data.clear()
        return len(keys)

    def srem(self, key, *members):
        self._check()
        bucket = self.applied.get(key)
        if bucket is None:
            return 0
        removed = 0
        for member in members:
            if member in bucket:
                bucket.remove(member)
                removed += 1
        if not bucket:
            self.applied.pop(key, None)
        return removed

    def eval(self, script, numkeys, *keys_and_args):
        """按 redis-py 的 eval(script, numkeys, *keys_and_args) 解析。"""
        self._check()
        self.eval_calls += 1
        keys = list(keys_and_args[:numkeys])
        args = list(keys_and_args[numkeys:]) or [""]
        if "ipairs(stale)" in script:
            # 条件清空：只删除 cutoff 之前写入的消息
            cut = args[0] if args else ""
            if cut == "":
                count = len(self.data)
                self.data.clear()
                return count
            stale = 0
            kept = []
            for raw in self.data:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    msg = None
                timestamp = msg.get("timestamp") if isinstance(msg, dict) else None
                if isinstance(timestamp, str) and timestamp <= cut:
                    stale += 1
                else:
                    kept.append(raw)
            self.data = kept
            return stale
        if "SADD" in script:
            # 幂等追加：按 op_id 去重后 RPUSH，窗口裁剪与 TTL 略去
            applied = self.applied.setdefault(keys[1], set())
            window = int(args[0])
            pushed = 0
            for index in range(2, len(args) - 1, 2):
                op_id, payload = args[index], args[index + 1]
                if op_id in applied:
                    continue
                applied.add(op_id)
                self.data.append(payload)
                pushed += 1
            if pushed:
                self.data = self.data[-window:]
            return pushed
        if "LREM" in script:
            removed = 0
            for payload in args:
                kept = []
                for item in self.data:
                    if item == payload:
                        removed += 1
                    else:
                        kept.append(item)
                self.data = kept
            return removed
        # 截断脚本：角色判断、内容校验、一次性标记与 cutoff 与 Lua 对应
        marker = keys[1] if len(keys) > 1 else None
        check = args[0] if args else "0"
        expected = args[1] if len(args) > 1 else ""
        marker_ttl = args[2] if len(args) > 2 else ""
        cutoff = args[3] if len(args) > 3 else ""
        if marker is not None:
            if marker in self.markers:
                return -1
            self.markers[marker] = marker_ttl
            self.marker_ttls.append(marker_ttl)
        if not self.data:
            return 0
        count = 1
        user_raw = None
        try:
            last = json.loads(self.data[-1])
        except json.JSONDecodeError:
            last = None
        if isinstance(last, dict) and last.get("role") == "assistant" and len(self.data) >= 2:
            try:
                prev = json.loads(self.data[-2])
            except json.JSONDecodeError:
                prev = None
            if isinstance(prev, dict) and prev.get("role") == "user":
                count = 2
                user_raw = self.data[-2]
        elif isinstance(last, dict) and last.get("role") == "user":
            user_raw = self.data[-1]
        # cutoff 对有无内容校验的删除都生效（与真实脚本一致）
        if cutoff and user_raw is not None:
            try:
                cut_user = json.loads(user_raw)
            except json.JSONDecodeError:
                cut_user = None
            if isinstance(cut_user, dict):
                timestamp = cut_user.get("timestamp")
                if isinstance(timestamp, str) and timestamp > cutoff:
                    return 0
        if check == "1":
            if user_raw is None:
                return 0
            try:
                user = json.loads(user_raw)
            except json.JSONDecodeError:
                return 0
            if not isinstance(user, dict) or user.get("role") != "user":
                return 0
            content = user.get("content")
            if not isinstance(content, str):
                return 0
            if content.strip() != expected:
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


def test_truncate_last_turn_skips_turns_written_after_cutoff(monkeypatch):
    """另一实例在删除发起之后写入的同文本新轮次不能被在线截断误删。"""
    from datetime import datetime, timedelta

    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    memory = module.RedisShortTermMemory(client, "s1", max_turns=5, user_id="u1")
    memory.add_message("user", "Q")
    memory.add_message("assistant", "A")
    # 模拟另一实例在本实例发起删除之后写入的新一轮（时间戳晚于 cutoff）
    future = (datetime.now() + timedelta(minutes=5)).isoformat()
    client.data.append(
        json.dumps({"role": "user", "content": "Q", "timestamp": future},
                   ensure_ascii=False)
    )
    client.data.append(
        json.dumps({"role": "assistant", "content": "A2", "timestamp": future},
                   ensure_ascii=False)
    )
    assert memory.truncate_last_turn("Q") == 0, "cutoff 之后写入的新轮次不能被删"
    assert len(client.data) == 4, "新写入的一轮必须原样保留"
    print("✓ 在线截断带 cutoff，不误删发起之后写入的同文本新轮次")


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


class _EvalApplyThenFailRedis(_FlakyRedis):
    """截断脚本已执行但响应丢失：eval 生效后抛连接错误（只丢一次）。"""

    def eval(self, script, numkeys, *keys_and_args):
        result = super().eval(script, numkeys, *keys_and_args)
        if self.fail_after_apply:
            self.fail_after_apply = False
            raise ConnectionError("response lost after script")
        return result


def _message(role: str, content: str, timestamp: str) -> str:
    return json.dumps(
        {"role": role, "content": content, "timestamp": timestamp, "metadata": {}},
        ensure_ascii=False,
    )


def test_outage_backlog_is_bounded(monkeypatch):
    """长时间故障时操作日志有界，进程内存不会无限增长。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    memory = module.RedisShortTermMemory(client, "s1", max_turns=2, user_id="u1")

    client.fail = True
    for index in range(200):
        memory.add_message("user" if index % 2 == 0 else "assistant", f"m{index}")
    assert len(memory._pending_ops) <= max(64, 2 * 8), "操作日志应有上界"

    client.fail = False
    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    messages = memory.get_recent_context()
    assert [item["content"] for item in messages] == ["m196", "m197", "m198", "m199"]
    assert len(client.data) == 4
    print("✓ 故障期操作日志有界（重放后仍只保留最近窗口）")


def test_replay_dedupes_committed_payload(monkeypatch):
    """写入已在 Redis 生效但响应丢失时，恢复补写不能重复追加同一条消息。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _EvalApplyThenFailRedis()
    memory = module.RedisShortTermMemory(client, "s1", max_turns=5, user_id="u1")
    memory.add_message("user", "Q1")
    memory.add_message("assistant", "A1")

    client.fail_after_apply = True
    memory.add_message("user", "Q2")
    assert memory._pending_ops, "响应丢失时消息应进入操作日志"
    assert len(client.data) == 3, "写入本身已在服务端生效"

    client.fail_after_apply = False
    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    messages = memory.get_recent_context()
    assert [item["content"] for item in messages] == ["Q1", "A1", "Q2"], "补写不能重复"
    assert memory._pending_ops == []
    print("✓ 补写按已提交内容去重（至少一次语义不重复）")


def test_outage_truncate_replays_on_recovery(monkeypatch):
    """故障期间删除的轮次不能在恢复后从 Redis 复活（按完整消息项精确删除）。"""
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

    client.fail = True
    assert memory.truncate_last_turn("Q2") == 2
    assert [item["content"] for item in memory.get_recent_context()] == ["Q1", "A1"]

    client.fail = False
    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    messages = memory.get_recent_context()
    assert [item["content"] for item in messages] == ["Q1", "A1"], (
        "恢复后已删除的轮次不能从 Redis 复活"
    )
    assert len(client.data) == 2
    print("✓ 故障期截断在恢复后精确删除")


def test_outage_add_then_truncate_replays_in_order(monkeypatch):
    """故障期先新增再截断：重放顺序保证新增项先补写、随后被精确删除。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    memory = module.RedisShortTermMemory(client, "s1", max_turns=5, user_id="u1")
    memory.add_message("user", "Q1")
    memory.add_message("assistant", "A1")

    client.fail = True
    memory.add_message("user", "Q2")
    memory.add_message("assistant", "A2")
    assert memory.truncate_last_turn("Q2") == 2

    client.fail = False
    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    messages = memory.get_recent_context()
    assert [item["content"] for item in messages] == ["Q1", "A1"]
    assert len(client.data) == 2
    assert memory._pending_ops == []
    print("✓ 故障期新增 + 截断按顺序重放")


def test_outage_truncate_keeps_other_instance_messages(monkeypatch):
    """故障期截断的重放只能删除本对象删掉的消息，不能整体重写其它实例的轮次。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    alice = module.RedisShortTermMemory(client, "s1", max_turns=5, user_id="u1")
    bob = module.RedisShortTermMemory(client, "s1", max_turns=5, user_id="u1")
    alice.add_message("user", "Q1")
    alice.add_message("assistant", "A1")
    # 另一实例（另一个 worker）写入新的一轮：alice 的影子缓存里没有它
    bob.add_message("user", "Q2")
    bob.add_message("assistant", "A2")

    client.fail = True
    assert alice.truncate_last_turn("Q1") == 2

    client.fail = False
    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    contents = [item["content"] for item in alice.get_recent_context()]
    assert contents == ["Q2", "A2"], "恢复重放不能丢掉其它实例写入的轮次"
    print("✓ 故障期截断只精确删除本对象删掉的消息")


def test_outage_transient_truncate_queues_conditional_delete(monkeypatch):
    """临时对象影子缓存为空时，故障期截断也要记录，恢复后条件删除仍生效。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    owner = module.RedisShortTermMemory(client, "s1", max_turns=5, user_id="u1")
    owner.add_message("user", "Q1")
    owner.add_message("assistant", "A1")
    # 新建同键对象模拟临时实例（重启 / 会话缓存未命中）：影子缓存为空
    transient = module.RedisShortTermMemory(client, "s1", max_turns=5, user_id="u1")

    client.fail = True
    assert transient.truncate_last_turn("Q1") == 0, "影子缓存为空，内存里删不掉"

    client.fail = False
    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    assert transient.get_recent_context() == [], "恢复后条件删除应清掉 Redis 里的最后一轮"
    print("✓ 临时对象故障期截断在恢复后条件删除")


def test_transient_object_truncate_replays_after_discard(monkeypatch):
    """临时对象用完即弃（生产路径）：排队的删除由后续实例重放，旧轮不会复活。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    owner = module.RedisShortTermMemory(client, "s-discard", max_turns=5, user_id="u1")
    owner.add_message("user", "Q1")
    owner.add_message("assistant", "A1")

    transient = module.RedisShortTermMemory(
        client, "s-discard", max_turns=5, user_id="u1"
    )
    client.fail = True
    assert transient.truncate_last_turn("Q1") == 0
    # webapp 对未命中会话缓存的会话：create_short_term_memory(...) 调用后即丢弃。
    # 操作日志按会话键共享，后续任何实例都能重放
    del transient

    client.fail = False
    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    reader = module.RedisShortTermMemory(client, "s-discard", max_turns=5, user_id="u1")
    assert reader.get_recent_context() == [], "丢弃临时对象后，排队的删除仍要生效"
    print("✓ 临时对象丢弃后删除操作由后续实例重放")


def test_truncate_replay_marker_prevents_double_delete(monkeypatch):
    """截断已执行但响应丢失时，重试凭一次性标记跳过，不会删掉上一轮同文本对话。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _EvalApplyThenFailRedis()
    client.data = [
        _message("user", "同一个问题", "2000-01-01T10:00:00"),
        _message("assistant", "回答一", "2000-01-01T10:00:01"),
        _message("user", "同一个问题", "2000-01-01T10:01:00"),
        _message("assistant", "回答二", "2000-01-01T10:01:01"),
    ]
    memory = module.RedisShortTermMemory(client, "s-marker", max_turns=5, user_id="u1")
    client.fail = True
    assert memory.truncate_last_turn("同一个问题") == 0  # 影子为空，记入共享日志

    client.fail = False
    client.fail_after_apply = True
    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    reader = module.RedisShortTermMemory(client, "s-marker", max_turns=5, user_id="u1")
    reader.get_recent_context()  # 首次重放：脚本已生效，响应丢失
    assert [json.loads(item)["content"] for item in client.data] == [
        "同一个问题",
        "回答一",
    ]
    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    reader.get_recent_context()  # 冷却后重试：标记命中，不再删第二轮
    assert [json.loads(item)["content"] for item in client.data] == [
        "同一个问题",
        "回答一",
    ]
    assert reader._pending_ops == []
    print("✓ 截断重放凭一次性标记避免重复删除")


def test_truncate_replay_cutoff_skips_newer_same_text_turn(monkeypatch):
    """故障期间其它实例写入的同文本新轮次晚于 cutoff，重放不能误删。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    client.data = [
        _message("user", "同一个问题", "2026-11-06T10:00:00"),
        _message("assistant", "回答一", "2026-11-06T10:00:01"),
    ]
    memory = module.RedisShortTermMemory(client, "s-cutoff", max_turns=5, user_id="u1")
    client.fail = True
    assert memory.truncate_last_turn("同一个问题") == 0  # cutoff = 当前时刻

    # 故障期间另一实例写入同文本的新一轮：时间戳晚于 cutoff
    client.data.extend(
        [
            _message("user", "同一个问题", "2099-01-01T00:00:00"),
            _message("assistant", "回答二", "2099-01-01T00:00:01"),
        ]
    )
    client.fail = False
    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    contents = [item["content"] for item in memory.get_recent_context()]
    assert contents == ["同一个问题", "回答一", "同一个问题", "回答二"], (
        "cutoff 之后写入的同文本轮次不能被重放误删"
    )
    assert client.markers == {}, "重放（未匹配）后标记也要清理"
    print("✓ 截断重放用 cutoff 排除故障期新写入的同文本轮次")


def test_truncate_replay_mismatch_logs_warning(monkeypatch, caplog):
    """条件截断重放未匹配时不能静默丢弃，要留下日志便于排查。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    client.data = [_message("user", "旧问题", "2026-11-06T10:00:00")]
    memory = module.RedisShortTermMemory(client, "s-warn", max_turns=5, user_id="u1")
    client.fail = True
    assert memory.truncate_last_turn("旧问题") == 0
    # 恢复前列表末尾已换成别的提问（时间早于 cutoff）：内容校验不成立
    client.data = [_message("user", "新问题", "2000-01-01T00:00:00")]
    client.fail = False
    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    assert [item["content"] for item in memory.get_recent_context()] == ["新问题"]
    assert "截断重放未匹配" in caplog.text
    print("✓ 截断重放未匹配时记录日志")


def test_delete_ops_cap_does_not_drop_new_adds(monkeypatch):
    """删除类操作占满上限时，新的新增消息仍要能入队（不能把消息静默丢掉）。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    memory = module.RedisShortTermMemory(client, "s-cap", max_turns=2, user_id="u1")
    try:
        client.fail = True
        for _ in range(300):
            memory.truncate_last_turn("同一提问")
        memory.add_message("user", "关键消息")
        ops = memory._pending_ops
        assert sum(1 for kind, _ in ops if kind == "add") == 1, (
            "新增消息不能被删除类操作挤掉"
        )
        assert sum(1 for kind, _ in ops if kind == "truncate") == 64, (
            "删除类操作应有独立上限（默认 max(64, max_turns*8)）"
        )
    finally:
        with module._PENDING_JOURNALS_LOCK:
            module._PENDING_JOURNALS.pop(memory.key, None)
    print("✓ 删除类操作占满上限时新增消息仍能入队")


def test_replay_batches_adds_in_one_pipeline(monkeypatch):
    """恢复重放把连续新增合并为一次 pipeline，不随积压条数放大往返次数。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    memory = module.RedisShortTermMemory(client, "s-batch", max_turns=10, user_id="u1")
    client.fail = True
    for index in range(10):
        memory.add_message("user" if index % 2 == 0 else "assistant", f"m{index}")
    client.fail = False
    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    before = client.eval_calls
    messages = memory.get_recent_context()
    assert client.eval_calls - before == 1, "连续新增应合并为一次脚本调用"
    assert [item["content"] for item in messages] == [f"m{i}" for i in range(10)]
    print("✓ 连续新增合并为一次 pipeline")


def test_replay_skips_committed_add_pushed_out_by_newer_messages(monkeypatch):
    """响应丢失的消息若已写入又被新消息挤出窗口，凭 applied 集合去重不再追加。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _EvalApplyThenFailRedis()
    memory = module.RedisShortTermMemory(client, "s-stale", max_turns=2, user_id="u1")
    memory.add_message("user", "Q1")
    memory.add_message("assistant", "A1")

    client.fail_after_apply = True
    memory.add_message("user", "Q2")  # 已写入 Redis，但响应丢失
    assert len(memory._pending_ops) == 1

    # 模拟另一实例在其后写入 4 条更新的消息：Q2 被挤出 4 条滑动窗口
    client.data = [
        _message("user", "B1", "2099-01-01T00:00:00"),
        _message("assistant", "B1a", "2099-01-01T00:00:01"),
        _message("user", "B2", "2099-01-01T00:00:02"),
        _message("assistant", "B2a", "2099-01-01T00:00:03"),
    ]
    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    contents = [item["content"] for item in memory.get_recent_context()]
    assert contents == ["B1", "B1a", "B2", "B2a"], (
        "已被更新的内容挤出窗口的旧消息不能以最新身份复活"
    )
    assert memory._pending_ops == []
    print("✓ 重放跳过已被更新消息挤出窗口的过期新增")


def test_replay_keeps_uncommitted_message_despite_newer_messages(monkeypatch):
    """从未写入 Redis 的故障期消息必须补写，不能被过期判断静默丢弃。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    memory = module.RedisShortTermMemory(client, "s-keep", max_turns=2, user_id="u1")
    memory.add_message("user", "Q1")
    memory.add_message("assistant", "A1")

    client.fail = True
    memory.add_message("user", "Q2")  # 从未写入 Redis
    # 故障期间另一实例写入更新的消息
    client.data.extend(
        [
            _message("user", "B1", "2099-01-01T00:00:00"),
            _message("assistant", "B1a", "2099-01-01T00:00:01"),
        ]
    )
    client.fail = False
    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    contents = [item["content"] for item in memory.get_recent_context()]
    assert "Q2" in contents, "未写入的故障期消息不能被静默丢弃"
    assert memory._pending_ops == []
    print("✓ 未写入的故障期消息在恢复后补写")


def test_online_truncate_response_lost_does_not_double_delete(monkeypatch):
    """在线截断已执行但响应丢失：排队的同 op_id 重放凭标记跳过，不能删两轮。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _EvalApplyThenFailRedis()
    client.data = [
        _message("user", "同一个问题", "2000-01-01T10:00:00"),
        _message("assistant", "回答一", "2000-01-01T10:00:01"),
        _message("user", "同一个问题", "2000-01-01T10:01:00"),
        _message("assistant", "回答二", "2000-01-01T10:01:01"),
    ]
    memory = module.RedisShortTermMemory(client, "s-online", max_turns=5, user_id="u1")
    client.fail_after_apply = True
    assert memory.truncate_last_turn("同一个问题") == 0  # 在线脚本生效，响应丢失
    assert [json.loads(item)["content"] for item in client.data] == [
        "同一个问题",
        "回答一",
    ]

    client.fail_after_apply = False
    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    reader = module.RedisShortTermMemory(client, "s-online", max_turns=5, user_id="u1")
    contents = [item["content"] for item in reader.get_recent_context()]
    assert contents == ["同一个问题", "回答一"], "响应丢失后重放不能多删一轮"
    assert reader._pending_ops == []
    assert client.markers == {}, "标记命中（-1）后也要清理"
    print("✓ 在线截断响应丢失后重放不多删")


def test_clear_then_add_replays_in_order(monkeypatch):
    """故障期先清空再新增：重放顺序为删除后写入，旧对话不会复活。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    memory = module.RedisShortTermMemory(client, "s-order", max_turns=5, user_id="u1")
    memory.add_message("user", "旧消息")
    memory.add_message("assistant", "旧回答")

    client.fail = True
    memory.add_message("user", "清空前的消息")
    memory.clear()  # 丢弃更早的排队操作
    memory.add_message("user", "清空后的消息")
    ops = memory._pending_ops
    assert [kind for kind, _ in ops] == ["clear", "add"], ops
    assert isinstance(ops[0][1], str) and ops[0][1], "clear 必须携带发起时刻"

    client.fail = False
    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    contents = [item["content"] for item in memory.get_recent_context()]
    assert contents == ["清空后的消息"]
    print("✓ 故障期清空 + 新增按调用顺序重放")


def test_discard_pending_ops_clears_shared_journal(monkeypatch):
    """会话删除时丢弃该键排队的操作，复用的 session_id 不会重放旧会话内容。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    memory = module.RedisShortTermMemory(client, "s-discard2", max_turns=5, user_id="u1")
    client.fail = True
    memory.add_message("user", "待删除会话的私密内容")
    assert len(memory._pending_ops) == 1

    assert module.discard_pending_ops(memory.key) == 1
    assert memory._pending_ops == []
    print("✓ discard_pending_ops 清空会话排队操作")


def test_applied_set_does_not_grow_with_normal_adds(monkeypatch):
    """成功写入后 op_id 从 applied 集合移除，集合大小不随正常消息量增长。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _EvalApplyThenFailRedis()
    memory = module.RedisShortTermMemory(client, "s-applied", max_turns=5, user_id="u1")
    for index in range(20):
        memory.add_message("user" if index % 2 == 0 else "assistant", f"m{index}")
    bucket = client.applied.get(module.applied_key(memory.key), set())
    assert bucket == set(), "成功写入的 op_id 应立即清理"

    client.fail_after_apply = True
    memory.add_message("user", "响应丢失")
    bucket = client.applied.get(module.applied_key(memory.key), set())
    assert len(bucket) == 1, "响应丢失的 op_id 需要保留用于去重"
    print("✓ applied 集合只保留不确定结果的消息")


def test_truncate_marker_cleaned_after_success(monkeypatch):
    """成功截断后一次性标记被删除；响应丢失时标记保留供重放使用。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    memory = module.RedisShortTermMemory(client, "s-marker2", max_turns=5, user_id="u1")
    memory.add_message("user", "Q1")
    memory.add_message("assistant", "A1")
    assert memory.truncate_last_turn("Q1") == 2
    assert client.markers == {}, "成功截断后标记应被清理"

    memory.add_message("user", "Q2")
    memory.add_message("assistant", "A2")
    client = _EvalApplyThenFailRedis()
    memory = module.RedisShortTermMemory(client, "s-marker3", max_turns=5, user_id="u1")
    memory.add_message("user", "Q1")
    memory.add_message("assistant", "A1")
    client.fail_after_apply = True
    assert memory.truncate_last_turn("Q1") == 2  # 回退到影子缓存删除
    assert len(client.markers) == 1, "响应丢失时标记保留（只在拿到响应后清理）"
    print("✓ 截断标记成功即清理、失败时保留")


def test_queue_clear_replays_on_next_access(monkeypatch):
    """会话删除时 Redis 清理失败会登记整键删除，下次访问先重放它。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    memory = module.RedisShortTermMemory(client, "s-qclear", max_turns=5, user_id="u1")
    memory.add_message("user", "旧对话")
    module.queue_clear(memory.key)
    reader = module.RedisShortTermMemory(client, "s-qclear", max_turns=5, user_id="u1")
    assert reader.get_recent_context() == []
    print("✓ queue_clear 在下次访问时重放删除")


def test_clear_short_term_session_discards_and_clears_applied(monkeypatch):
    """删除会话丢弃排队操作并清理 applied 集合（复用 session_id 不重放旧内容）。"""
    from context import short_term_memory as module
    from context import storage

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    monkeypatch.setitem(storage.STORAGE_CONFIG, "redis_url", "redis://fake")
    monkeypatch.setattr(storage, "_get_redis_client", lambda url: client)
    key = module.session_key("s-storage", "u-storage")
    client.applied[module.applied_key(key)] = {"x"}
    memory = module.RedisShortTermMemory(client, "s-storage", user_id="u-storage")
    client.fail = True
    memory.add_message("user", "待删除会话的内容")
    assert memory._pending_ops

    client.fail = False
    storage.clear_short_term_session("s-storage", "u-storage")
    assert memory._pending_ops == []
    assert module.applied_key(key) not in client.applied
    print("✓ 会话删除清理排队操作与 applied 集合")


def test_clear_short_term_session_failure_queues_clear(monkeypatch):
    """会话删除时 Redis 不可用：登记整键删除，恢复后旧对话不会复活。"""
    from context import short_term_memory as module
    from context import storage

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    client.data = [_message("user", "旧对话", "2000-01-01T00:00:00")]
    monkeypatch.setitem(storage.STORAGE_CONFIG, "redis_url", "redis://fake")
    monkeypatch.setattr(storage, "_get_redis_client", lambda url: client)
    client.fail = True
    storage.clear_short_term_session("s-fail", "u-fail")
    client.fail = False
    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    reader = module.RedisShortTermMemory(client, "s-fail", user_id="u-fail")
    assert reader.get_recent_context() == []
    print("✓ 会话删除失败时登记整键删除")


def test_eviction_prefers_add_only_journals(monkeypatch, caplog):
    """注册表满额时优先淘汰只含新增的日志，含删除操作的日志留到最后。"""
    from context import short_term_memory as module

    saved = dict(module._PENDING_JOURNALS)
    try:
        with module._PENDING_JOURNALS_LOCK:
            module._PENDING_JOURNALS.clear()
        with module._pending_journal("del-keep") as journal:
            journal.ops.append(("clear", None))
        for index in range(module._PENDING_JOURNALS_MAX - 1):
            with module._pending_journal(f"adds-{index}") as journal:
                journal.ops.append(("add", ("payload", "id")))
        with module._pending_journal("newcomer"):
            pass
        assert "del-keep" in module._PENDING_JOURNALS, "含删除的日志应最后淘汰"
        assert len(module._PENDING_JOURNALS) <= module._PENDING_JOURNALS_MAX
        assert "丢弃最旧会话" in caplog.text, "淘汰待重放消息必须记录警告"
    finally:
        with module._PENDING_JOURNALS_LOCK:
            module._PENDING_JOURNALS.clear()
            module._PENDING_JOURNALS.update(saved)
    print("✓ 淘汰优先选择只含新增的日志")


def test_clear_op_survives_journal_trim(monkeypatch):
    """日志裁剪永不丢弃 clear：删除类操作保留，新增/截断各自限额。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    memory = module.RedisShortTermMemory(client, "s-cleartrim", max_turns=2, user_id="u1")
    client.fail = True
    memory.add_message("user", "清空前的消息")
    memory.clear()
    for _ in range(300):
        memory.truncate_last_turn("同一提问")
    kinds = [kind for kind, _ in memory._pending_ops]
    assert kinds[0] == "clear", "clear 必须在日志首位且不被裁剪"
    assert kinds.count("clear") == 1
    assert kinds.count("truncate") == 64
    with module._PENDING_JOURNALS_LOCK:
        module._PENDING_JOURNALS.pop(memory.key, None)
    print("✓ clear 操作不参与日志裁剪")


def test_clear_replay_keeps_messages_written_after_cutoff(monkeypatch):
    """迟到的清空只删 cutoff 之前的消息，墓碑过期后复用的新会话消息保留。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    client.data = [
        _message("user", "已删除会话的旧内容", "2000-01-01T00:00:00"),
        _message("assistant", "旧回答", "2000-01-01T00:00:01"),
        _message("user", "复用后的新会话", "2099-01-01T00:00:00"),
    ]
    memory = module.RedisShortTermMemory(client, "s-cutclear", max_turns=5, user_id="u1")
    module.queue_clear(memory.key)

    contents = [item["content"] for item in memory.get_recent_context()]
    assert contents == ["复用后的新会话"], "新会话消息不能被迟到的清空删掉"
    assert memory._pending_ops == []
    print("✓ 迟到的清空保留新会话消息、删除旧内容")


def test_replay_cleans_applied_ids_and_markers(monkeypatch):
    """重放成功后回收 applied 记录与截断标记，键空间不随故障次数增长。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    memory = module.RedisShortTermMemory(client, "s-reclaim", max_turns=5, user_id="u1")
    client.fail = True
    memory.add_message("user", "Q1")
    memory.add_message("assistant", "A1")
    module.RedisShortTermMemory(client, "s-reclaim", max_turns=5, user_id="u1").truncate_last_turn("Q1")

    client.fail = False
    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    memory.get_recent_context()
    assert client.applied.get(module.applied_key(memory.key), set()) == set(), (
        "重放成功的 op_id 应被回收"
    )
    assert client.markers == {}, "重放成功的截断标记应被删除"
    print("✓ 重放成功后回收 applied 与标记")


def test_clear_short_term_session_without_client_queues_clear(monkeypatch):
    """Redis 客户端不可用（冷却期）时删除会话也要登记整键删除。"""
    from context import short_term_memory as module
    from context import storage

    client = _FlakyRedis()
    monkeypatch.setitem(storage.STORAGE_CONFIG, "redis_url", "redis://fake")
    monkeypatch.setattr(storage, "_get_redis_client", lambda url: None)
    storage.clear_short_term_session("s-noclient", "u-noclient")
    key = module.session_key("s-noclient", "u-noclient")
    probe = module.RedisShortTermMemory(client, "s-noclient", user_id="u-noclient")
    assert [kind for kind, _ in probe._pending_ops] == ["clear"]
    with module._PENDING_JOURNALS_LOCK:
        module._PENDING_JOURNALS.pop(key, None)
        module._PENDING_JOURNALS.pop(module.session_key("s-noclient"), None)
    assert module.session_key("s-noclient") not in module._PENDING_JOURNALS, (
        "旧格式键没有重放者，不应登记"
    )
    print("✓ 客户端不可用时删除会话登记整键删除")


def test_read_hydrates_shadow_from_redis(monkeypatch):
    """读路径用 Redis 的真实内容校正影子缓存：重启 / LRU 淘汰后的新实例
    在 Redis 再次故障时不会丢掉既有对话。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    client.data = [
        _message("user", "历史问题", "2000-01-01T00:00:00"),
        _message("assistant", "历史回答", "2000-01-01T00:00:01"),
    ]
    memory = module.RedisShortTermMemory(client, "s-hydrate", max_turns=5, user_id="u1")
    assert [item["content"] for item in memory.get_recent_context()] == [
        "历史问题",
        "历史回答",
    ]
    assert [item["content"] for item in memory._fallback.messages] == [
        "历史问题",
        "历史回答",
    ]
    client.fail = True
    assert [item["content"] for item in memory.get_recent_context()] == [
        "历史问题",
        "历史回答",
    ]
    print("✓ 读路径校正影子缓存")


def test_read_skips_unparsable_entries(monkeypatch):
    """列表中的坏元素不能把整次读取升级为 Redis 故障（进程级冷却）。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    client.data = [
        "not-json",
        _message("user", "正常消息", "2000-01-01T00:00:00"),
    ]
    memory = module.RedisShortTermMemory(client, "s-badjson", max_turns=5, user_id="u1")
    assert [item["content"] for item in memory.get_recent_context()] == ["正常消息"]
    assert module.redis_short_term_degraded() is False
    print("✓ 读取跳过无法解析的条目")


def test_read_skips_malformed_message_objects(monkeypatch):
    """可解析但缺 role/content 的条目同样跳过（否则下游 KeyError 每轮 500）。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    client.data = [
        '{"foo": 1}',
        '{"role": "user"}',
        _message("user", "正常消息", "2000-01-01T00:00:00"),
    ]
    memory = module.RedisShortTermMemory(client, "s-badshape", max_turns=5, user_id="u1")
    assert [item["content"] for item in memory.get_recent_context()] == ["正常消息"]
    assert module.redis_short_term_degraded() is False
    print("✓ 读取跳过形状非法的条目")


def test_statistics_tolerates_non_object_entries(monkeypatch):
    """可解析但非对象的条目不能让 get_statistics 误判 Redis 故障（进程级冷却）。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    client.data = ["null", _message("user", "正常", "2000-01-01T00:00:00")]

    def lindex(key, index):
        client._check()
        return client.data[index]

    client.lindex = lindex  # type: ignore[method-assign]
    memory = module.RedisShortTermMemory(client, "s-stat", max_turns=5, user_id="u1")
    stats = memory.get_statistics()
    assert stats["total_messages"] == 2
    assert stats["oldest_message_time"] is None
    assert stats["newest_message_time"] == "2000-01-01T00:00:00"
    assert module.redis_short_term_degraded() is False
    print("✓ 统计口径容忍非对象条目且不触发降级")


def test_read_corrects_shadow_cache_with_external_writes(monkeypatch):
    """读路径用 Redis 全窗口校正影子缓存：外部实例新写入的消息要进入影子。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    client.data = [_message("user", "A", "2000-01-01T00:00:00")]
    memory = module.RedisShortTermMemory(client, "s-correct", max_turns=10, user_id="u1")
    assert [item["content"] for item in memory.get_recent_context()] == ["A"]
    client.data.append(_message("assistant", "B", "2000-01-01T00:00:01"))
    assert [item["content"] for item in memory.get_recent_context()] == ["A", "B"]
    assert [item["content"] for item in memory._fallback.get_recent_context()] == [
        "A",
        "B",
    ]
    print("✓ 读路径校正影子缓存（外部写入可见）")


def test_pending_journal_eviction_tiers(monkeypatch):
    """注册表淘汰优先级：空日志 → 只含新增 → 任意（含删除类）最后牺牲。"""
    import threading

    from context import short_term_memory as module

    class _Journal:
        def __init__(self, ops):
            self.lock = threading.Lock()
            self.ops = list(ops)

    saved = dict(module._PENDING_JOURNALS)
    try:
        module._PENDING_JOURNALS.clear()
        module._PENDING_JOURNALS["empty"] = _Journal([])
        assert module._evict_journal_locked() is None
        assert "empty" not in module._PENDING_JOURNALS

        module._PENDING_JOURNALS["adds"] = _Journal([("add", ("x", "op1"))])
        assert module._evict_journal_locked() == ("adds", 1, "消息")

        module._PENDING_JOURNALS["dels"] = _Journal(
            [("truncate", ("q", "op2", "cut"))]
        )
        assert module._evict_journal_locked() == ("dels", 1, "操作")

        # 被占用的日志跳过，优先淘汰可拿锁的
        locked = _Journal([("add", ("y", "op3"))])
        module._PENDING_JOURNALS["locked"] = locked
        module._PENDING_JOURNALS["free"] = _Journal([("add", ("z", "op4"))])
        locked.lock.acquire()
        try:
            assert module._evict_journal_locked() == ("free", 1, "消息")
            assert "locked" in module._PENDING_JOURNALS
        finally:
            locked.lock.release()
    finally:
        module._PENDING_JOURNALS.clear()
        module._PENDING_JOURNALS.update(saved)
    print("✓ 延迟操作日志淘汰优先级")


def test_outage_truncate_without_expect_replays(monkeypatch):
    """未提供 expect 的最后一轮删除在故障期也要登记，恢复后补删。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    client.data = [
        _message("user", "Q1", "2000-01-01T00:00:00"),
        _message("assistant", "A1", "2000-01-01T00:00:01"),
    ]
    memory = module.RedisShortTermMemory(client, "s-noexpect", max_turns=5, user_id="u1")
    client.fail = True
    assert memory.truncate_last_turn() == 0
    assert [kind for kind, _ in memory._pending_ops] == ["truncate"]

    client.fail = False
    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    assert memory.get_recent_context() == []
    print("✓ 无 expect 的故障期删除在恢复后补删")


def test_unconditional_truncate_replay_respects_cutoff(monkeypatch):
    """无条件删除的重放同样受 cutoff 保护：不误删故障期新写入的轮次。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    client.data = [
        _message("user", "旧问题", "2000-01-01T00:00:00"),
        _message("assistant", "旧回答", "2000-01-01T00:00:01"),
    ]
    memory = module.RedisShortTermMemory(client, "s-nocut", max_turns=5, user_id="u1")
    client.fail = True
    assert memory.truncate_last_turn() == 0  # cutoff = 当前时刻

    client.data = [
        _message("user", "新问题", "2099-01-01T00:00:00"),
        _message("assistant", "新回答", "2099-01-01T00:00:01"),
    ]
    client.fail = False
    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    contents = [item["content"] for item in memory.get_recent_context()]
    assert contents == ["新问题", "新回答"]
    print("✓ 无条件删除重放受 cutoff 保护")


def test_poison_op_dropped_after_repeated_failures(monkeypatch):
    """确定性失败的操作连续三次后丢弃，后续操作仍能重放（日志不会卡死）。"""
    from context import short_term_memory as module

    class _PoisonError(Exception):
        """确定性错误（对应 redis ResponseError / WRONGTYPE 之类）。

        不能用 ConnectionError：连接类故障不代表操作有毒，必须保留重放。
        """

    class _PoisonRedis(_FlakyRedis):
        def eval(self, script, numkeys, *keys_and_args):
            if any("POISON" in str(arg) for arg in keys_and_args):
                raise _PoisonError("deterministic failure")
            return super().eval(script, numkeys, *keys_and_args)

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _PoisonRedis()
    memory = module.RedisShortTermMemory(client, "s-poison", max_turns=5, user_id="u1")
    client.fail = True
    memory.add_message("user", "POISON")
    memory.add_message("assistant", "正常回复")
    assert [kind for kind, _ in memory._pending_ops] == ["add", "add"]

    client.fail = False
    for _ in range(3):
        monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
        memory.get_recent_context()
    contents = [item["content"] for item in memory.get_recent_context()]
    assert contents == ["正常回复"], "毒操作应被丢弃，正常操作继续重放"
    assert memory._pending_ops == []
    print("✓ 连续失败的毒操作最终被丢弃")


def test_replay_failure_still_registers_current_add(monkeypatch):
    """重放中途失败时本次新增也要登记，恢复后按顺序补写。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    memory = module.RedisShortTermMemory(client, "s-replayfail", max_turns=5, user_id="u1")
    client.fail = True
    memory.add_message("user", "M1")
    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    memory.add_message("user", "M2")
    assert [kind for kind, _ in memory._pending_ops] == ["add", "add"]

    client.fail = False
    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    contents = [item["content"] for item in memory.get_recent_context()]
    assert contents == ["M1", "M2"]
    print("✓ 重放失败时本次操作仍登记")


def test_connection_failure_during_replay_keeps_queue(monkeypatch):
    """连接类故障不消耗毒操作次数：长时间断连后故障期消息仍能重放。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    memory = module.RedisShortTermMemory(client, "s-transient", max_turns=5, user_id="u1")
    client.fail = True
    memory.add_message("user", "故障期提问")
    memory.add_message("assistant", "故障期回答")
    assert [kind for kind, _ in memory._pending_ops] == ["add", "add"]

    # 故障持续超过三轮冷却重试：每次重放都因连接失败，操作必须保留
    for _ in range(6):
        monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
        memory.get_recent_context()
    assert [kind for kind, _ in memory._pending_ops] == ["add", "add"], (
        "连接类故障不能计入连续失败次数，消息必须等恢复后重放"
    )
    assert module.redis_short_term_degraded() is True

    client.fail = False
    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    contents = [item["content"] for item in memory.get_recent_context()]
    assert contents == ["故障期提问", "故障期回答"], "恢复后故障期消息不能永久缺失"
    print("✓ 长时间连接故障后故障期消息仍被重放")


def test_truncate_marker_outlives_message_ttl(monkeypatch):
    """截断一次性标记的 TTL 必须远长于消息 TTL（操作日志在进程内不过期）。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    client.data = [
        _message("user", "问题", "2000-01-01T00:00:00"),
        _message("assistant", "回答", "2000-01-01T00:00:01"),
    ]
    memory = module.RedisShortTermMemory(
        client, "s-marker-ttl", max_turns=5, ttl_sec=60, user_id="u1"
    )
    assert memory.truncate_last_turn("问题") == 2
    ttl = [int(value) for value in client.marker_ttls if value]
    assert ttl and ttl[0] >= 24 * 3600, (
        f"标记 TTL={ttl} 不能短于故障重放的等待窗口（消息 TTL={memory.ttl_sec}s）"
    )
    print("✓ 截断标记 TTL 覆盖故障重放窗口")


def test_named_connection_error_is_transient():
    """redis-py 的 ConnectionError / TimeoutError 不是内置异常子类，按类名归类。"""
    from context import short_term_memory as module

    class _PoisonError(Exception):
        pass

    named_conn = type("ConnectionError", (Exception,), {})("redis down")
    named_timeout = type("TimeoutError", (Exception,), {})("read timeout")
    assert module._is_transient_redis_error(named_conn) is True
    assert module._is_transient_redis_error(named_timeout) is True
    assert module._is_transient_redis_error(_PoisonError("bad payload")) is False

    try:
        import redis
    except ImportError:  # pragma: no cover - requirements 固定安装 redis
        pass
    else:
        # 真实 redis-py 异常走同一名字分支（继承 RedisError 而非内置异常）
        assert module._is_transient_redis_error(redis.exceptions.ConnectionError("down"))
        assert module._is_transient_redis_error(redis.exceptions.TimeoutError("slow"))
    print("✓ redis-py 同名连接 / 超时异常按暂时故障处理")


def test_named_connection_error_during_replay_keeps_queue(monkeypatch):
    """重放遇到 redis-py 式连接异常：不计毒、队列保留，恢复后按序生效。"""
    from context import short_term_memory as module

    class _NamedDownRedis(_FlakyRedis):
        def _check(self):
            if self.fail:
                raise type("ConnectionError", (Exception,), {})("redis down")

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _NamedDownRedis()
    memory = module.RedisShortTermMemory(
        client, "s-named-transient", max_turns=5, user_id="u1"
    )
    client.fail = True
    memory.add_message("user", "Q1")
    memory.add_message("assistant", "A1")
    # 影子缓存里删掉这一轮：排队按具体消息删除，恢复后重放不得复活
    assert memory.truncate_last_turn("Q1") == 2
    snapshot = memory._pending_ops
    assert [kind for kind, _ in snapshot] == ["add", "add", "remove"]

    # 连续 6 轮重试都因同名连接异常失败：超过毒操作阈值也不能丢队列
    for _ in range(6):
        monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
        memory.get_recent_context()
        assert memory._pending_ops == snapshot, "暂时故障不能丢弃排队操作"
        with module._PENDING_JOURNALS_LOCK:
            journal = module._PENDING_JOURNALS[memory.key]
        assert journal.stuck == ("", 0), "暂时故障不能计入毒操作"

    client.fail = False
    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    assert memory.get_recent_context() == [], "重放顺序：先补写故障期消息，再删除已截断轮次"
    assert memory._pending_ops == []
    assert client.data == []
    print("✓ 重放中的 redis-py 连接异常保留队列且恢复后按序生效")


def test_truncate_replay_marker_uses_long_ttl(monkeypatch):
    """重放路径传给截断脚本的标记 TTL 与在线路径一致（30 天，非消息 TTL）。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    memory = module.RedisShortTermMemory(
        client, "s-marker-ttl-replay", max_turns=5, ttl_sec=60, user_id="u1"
    )
    client.fail = True
    assert memory.truncate_last_turn("Q1") == 0
    assert [kind for kind, _ in memory._pending_ops] == ["truncate"]

    client.fail = False
    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    memory.get_recent_context()
    assert client.marker_ttls == [str(module._OP_MARKER_TTL_SEC)]
    assert int(client.marker_ttls[0]) >= 24 * 3600, "标记必须覆盖进程内日志的整个等待窗口"
    assert client.marker_ttls[0] != "60", "不能用消息 TTL（本实例 60 秒）"
    assert client.markers == {}, "重放（含未匹配）后标记要清理"
    print("✓ 截断重放路径的标记 TTL 与在线路径一致")


def test_read_skips_deeply_nested_entries(monkeypatch):
    """深嵌套 JSON 条目的 RecursionError 按坏条目跳过，不能当成 Redis 故障。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    client.data = [
        "[" * 3000 + "]" * 3000,
        _message("user", "正常消息", "2026-01-01T00:00:00"),
    ]
    memory = module.RedisShortTermMemory(client, "s-deep", max_turns=5, user_id="u1")
    contents = [item["content"] for item in memory.get_recent_context()]
    assert contents == ["正常消息"], "深嵌套坏条目不能拖垮整次读取"
    assert module.redis_short_term_degraded() is False, (
        "数据问题不能被当成 Redis 不可用触发进程级降级"
    )
    stats = memory.get_statistics()
    assert stats["total_messages"] == 2, "统计信息同样要容忍深嵌套条目"
    print("✓ 深嵌套短期记忆条目被跳过")


def test_pending_journal_registry_is_bounded(monkeypatch):
    """进程级日志注册表有总量上限，不会随会话数无界增长。"""
    from context import short_term_memory as module

    saved = dict(module._PENDING_JOURNALS)
    try:
        with module._PENDING_JOURNALS_LOCK:
            module._PENDING_JOURNALS.clear()
        for index in range(module._PENDING_JOURNALS_MAX):
            with module._pending_journal(f"cap-{index}"):
                pass
        with module._pending_journal("cap-new"):
            pass
        assert len(module._PENDING_JOURNALS) <= module._PENDING_JOURNALS_MAX
        assert "cap-new" in module._PENDING_JOURNALS
    finally:
        with module._PENDING_JOURNALS_LOCK:
            module._PENDING_JOURNALS.clear()
            module._PENDING_JOURNALS.update(saved)
    print("✓ 故障期日志注册表有上限")


def test_replay_batch_failure_retries_without_duplicates(monkeypatch):
    """重放批次已生效但响应丢失：重试按已提交内容去重，不重复追加。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _EvalApplyThenFailRedis()
    memory = module.RedisShortTermMemory(client, "s-retry", max_turns=5, user_id="u1")
    client.fail = True
    memory.add_message("user", "Q2")
    memory.add_message("assistant", "A2")

    client.fail = False
    client.fail_after_apply = True
    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    memory.get_recent_context()  # 重放已写入，响应丢失
    assert len(client.data) == 2
    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    messages = memory.get_recent_context()
    assert [item["content"] for item in messages] == ["Q2", "A2"]
    assert len(client.data) == 2, "重试不能重复追加"
    assert memory._pending_ops == []
    print("✓ 重放批次响应丢失后重试不重复")


def test_outage_clear_rewrites_on_recovery(monkeypatch):
    """故障期间清空的会话不能在恢复后从 Redis 复活（重放一次删除）。"""
    from context import short_term_memory as module

    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    client = _FlakyRedis()
    memory = module.RedisShortTermMemory(client, "s1", max_turns=5, user_id="u1")
    memory.add_message("user", "Q1")
    memory.add_message("assistant", "A1")

    client.fail = True
    memory.clear()
    assert client.data, "故障期内 Redis 里的旧内容无法删除"

    client.fail = False
    monkeypatch.setattr(module, "_redis_degraded_until", 0.0)
    assert memory.get_recent_context() == []
    assert client.data == []
    print("✓ 故障期清空在恢复后整体重写")


def test_rollback_staged_message_only_removes_matching_tail():
    """暂存提问回滚只删未回答的同文本末条，带回答的轮次不受影响。"""
    import tempfile

    from context.memory_manager import MemoryManager

    manager = MemoryManager(
        "u-rollback", "s-rollback", storage_path=tempfile.mkdtemp()
    )
    manager.stage_message("user", "问题")
    assert manager.rollback_staged_message("问题") is True
    assert manager.short_term.get_recent_context() == []

    manager.stage_message("user", "问题")
    manager.stage_message("assistant", "回答")
    assert manager.rollback_staged_message("问题") is False, "已回答的轮次不能删"
    assert [m["content"] for m in manager.short_term.get_recent_context()] == [
        "问题",
        "回答",
    ]
    assert manager.rollback_staged_message("别的提问") is False
    print("✓ 暂存提问回滚只删匹配的未回答末条")


if __name__ == "__main__":
    test_truncate_pair()
    test_truncate_expect_mismatch_keeps_history()
    test_truncate_trailing_user_only()
    test_truncate_empty()
    test_truncate_expect_without_user_message_returns_zero()
    _mp = __import__("pytest").MonkeyPatch()
    try:
        test_get_context_string_formats_and_skips_bad_entries(_mp)
    finally:
        _mp.undo()
    test_max_turns_evicts_oldest()
    test_recent_context_and_string()
    test_statistics_and_clear()
    test_named_connection_error_is_transient()
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
        test_truncate_last_turn_skips_turns_written_after_cutoff(_mp)
    finally:
        _mp.undo()
    _mp = __import__("pytest").MonkeyPatch()
    try:
        test_shadow_cache_mirrors_clear(_mp)
    finally:
        _mp.undo()
    _mp = __import__("pytest").MonkeyPatch()
    try:
        test_reset_redis_degraded_clears_cooldown(_mp)
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
    _mp = __import__("pytest").MonkeyPatch()
    try:
        test_outage_truncate_replays_on_recovery(_mp)
    finally:
        _mp.undo()
    _mp = __import__("pytest").MonkeyPatch()
    try:
        test_outage_add_then_truncate_replays_in_order(_mp)
    finally:
        _mp.undo()
    _mp = __import__("pytest").MonkeyPatch()
    try:
        test_outage_truncate_keeps_other_instance_messages(_mp)
    finally:
        _mp.undo()
    _mp = __import__("pytest").MonkeyPatch()
    try:
        test_outage_transient_truncate_queues_conditional_delete(_mp)
    finally:
        _mp.undo()
    _mp = __import__("pytest").MonkeyPatch()
    try:
        test_transient_object_truncate_replays_after_discard(_mp)
    finally:
        _mp.undo()
    _mp = __import__("pytest").MonkeyPatch()
    try:
        test_truncate_replay_marker_prevents_double_delete(_mp)
    finally:
        _mp.undo()
    _mp = __import__("pytest").MonkeyPatch()
    try:
        test_truncate_replay_cutoff_skips_newer_same_text_turn(_mp)
    finally:
        _mp.undo()
    _mp = __import__("pytest").MonkeyPatch()
    try:
        test_delete_ops_cap_does_not_drop_new_adds(_mp)
    finally:
        _mp.undo()
    _mp = __import__("pytest").MonkeyPatch()
    try:
        test_replay_batches_adds_in_one_pipeline(_mp)
    finally:
        _mp.undo()
    _mp = __import__("pytest").MonkeyPatch()
    try:
        test_replay_batch_failure_retries_without_duplicates(_mp)
    finally:
        _mp.undo()
    _mp = __import__("pytest").MonkeyPatch()
    try:
        test_outage_clear_rewrites_on_recovery(_mp)
    finally:
        _mp.undo()
    for case in (
        test_none_client_recovers_via_factory,
        test_broken_client_re_resolved_after_failure,
        test_connection_failure_during_replay_keeps_queue,
        test_truncate_marker_outlives_message_ttl,
        test_named_connection_error_during_replay_keeps_queue,
        test_truncate_replay_marker_uses_long_ttl,
        test_read_skips_deeply_nested_entries,
    ):
        _case_mp = __import__("pytest").MonkeyPatch()
        try:
            case(_case_mp)
        finally:
            _case_mp.undo()
    test_rollback_staged_message_only_removes_matching_tail()
    print("\n全部通过")
