"""请求幂等缓存测试（纯 Python）。

覆盖：深拷贝隔离、TTL 过期、LRU 淘汰、键归一化、清空。

运行：python tests/test_idempotency.py
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils import idempotency
from utils.idempotency import IdempotencyCache, make_key


def test_put_get_returns_deep_copy():
    """读出的是深拷贝：修改返回值不会污染缓存。"""
    cache = IdempotencyCache()
    key = make_key("u1", "s1", "req-1")
    payload = {"answer": "ok", "nested": {"n": 1}}
    cache.put(key, payload)
    first = cache.get(key)
    assert first == payload
    assert first is not payload
    first["nested"]["n"] = 99
    assert cache.get(key)["nested"]["n"] == 1
    assert cache.get(("u1", "s1", "missing")) is None
    print("✓ 幂等缓存深拷贝隔离")


def test_ttl_expiry(monkeypatch):
    """超过 TTL 的条目视为不存在。"""
    cache = IdempotencyCache(ttl_sec=10)
    key = make_key("u", "s", "k")
    cache.put(key, {"answer": "ok"})
    assert cache.get(key) == {"answer": "ok"}

    monkeypatch.setattr(idempotency.time, "monotonic", lambda: 1_000.0)
    cache2 = IdempotencyCache(ttl_sec=10)
    cache2.put(key, {"answer": "ok"})
    monkeypatch.setattr(idempotency.time, "monotonic", lambda: 1_011.0)
    assert cache2.get(key) is None
    print("✓ 幂等缓存 TTL 过期")


def test_lru_eviction():
    """超过最大条目数时淘汰最久未使用的键。"""
    cache = IdempotencyCache(max_entries=2)
    keys = [make_key("u", "s", f"k{i}") for i in range(3)]
    cache.put(keys[0], {"v": 0})
    cache.put(keys[1], {"v": 1})
    cache.get(keys[0])  # 触碰 k0，k1 成为最久未使用
    cache.put(keys[2], {"v": 2})
    assert cache.get(keys[0]) == {"v": 0}
    assert cache.get(keys[1]) is None
    assert cache.get(keys[2]) == {"v": 2}
    print("✓ 幂等缓存 LRU 淘汰")


def test_eviction_is_per_user_fair():
    """超限淘汰只动条目最多的用户：另一用户的条目不能被挤出。

    断连回合的 __incomplete__ 标记被他人流量挤出后，重试会重复执行、
    重复落库。
    """
    cache = IdempotencyCache(max_entries=3)
    bob_key = make_key("bob", "s", "mark")
    cache.put(bob_key, {"__incomplete__": True})
    for index in range(4):
        cache.put(make_key("alice", "s", f"k{index}"), {"v": index})
    assert cache.get(bob_key) == {"__incomplete__": True}
    assert len(cache) == 3

    # 条目数不同时淘汰条目多的一方；同用户内部仍是标准 LRU
    cache2 = IdempotencyCache(max_entries=2)
    bob1 = make_key("bob", "s", "1")
    bob2 = make_key("bob", "s", "2")
    alice = make_key("alice", "s", "1")
    cache2.put(bob1, {"v": 1})
    cache2.put(alice, {"v": 2})
    cache2.put(bob2, {"v": 3})  # bob 2 条 > alice 1 条：淘汰 bob1
    assert cache2.get(bob2) == {"v": 3}
    assert cache2.get(alice) == {"v": 2}
    assert cache2.get(bob1) is None
    print("✓ 幂等缓存按用户公平淘汰")


def test_key_normalization_and_clear():
    """键去空白、限长；clear 后为空。"""
    key = make_key(" u1 ", " s1 ", " x " * 200)
    assert key[0] == "u1" and key[1] == "s1" and len(key[2]) == 200
    cache = IdempotencyCache()
    cache.put(key, {"answer": "ok"})
    assert len(cache) == 1
    cache.clear()
    assert len(cache) == 0
    print("✓ 幂等键归一化与清空")


def test_key_binds_endpoint_and_message():
    """同一幂等键 + 不同消息 / 端点必须得到不同缓存键。"""
    base = make_key("u", "s", "k", endpoint="chat", message="你好")
    assert base == make_key("u", "s", "k", endpoint="chat", message="你好")
    assert base != make_key("u", "s", "k", endpoint="chat", message="在吗")
    assert base != make_key(
        "u", "s", "k", endpoint="chat_stream", message="你好"
    )
    print("✓ 幂等键绑定消息与端点")


def test_discard_session_clears_all_forms():
    """会话删除后清掉全部相关条目：含空 session_id 键（载荷带会话 id）。"""
    cache = IdempotencyCache()
    key_s1 = make_key("u1", "s1", "a")
    key_empty = make_key("u1", "", "b")
    key_s2 = make_key("u1", "s2", "c")
    key_other_user = make_key("u2", "s1", "d")
    cache.put(key_s1, {"session_id": "s1"})
    cache.put(key_empty, {"session_id": "s1"})
    cache.put(key_s2, {"session_id": "s2"})
    cache.put(key_other_user, {"session_id": "s1"})
    cache.discard_session("u1", "s1")
    assert cache.get(key_s1) is None
    assert cache.get(key_empty) is None, "空 session_id 键按载荷会话 id 清理"
    assert cache.get(key_s2) == {"session_id": "s2"}
    assert cache.get(key_other_user) == {"session_id": "s1"}
    cache.discard_session("u1", "  ")
    assert len(cache) == 2, "空白会话 id 不清任何条目"
    print("✓ 幂等缓存按会话失效（含首条消息的空键）")


def test_eviction_purges_expired_before_fair(monkeypatch):
    """超限时先清过期条目：活跃用户仍在 TTL 内的新鲜条目不能被挤出。"""
    cache = IdempotencyCache(max_entries=4, ttl_sec=600)
    stale_bob = make_key("bob", "s", "mark")
    stale_carol = make_key("carol", "s", "mark")
    monkeypatch.setattr(idempotency.time, "monotonic", lambda: 1_000.0)
    cache.put(stale_bob, {"__incomplete__": True})
    cache.put(stale_carol, {"__incomplete__": True})
    monkeypatch.setattr(idempotency.time, "monotonic", lambda: 1_601.0)
    fresh = [make_key("alice", "s", f"k{index}") for index in range(4)]
    for index, key in enumerate(fresh):
        cache.put(key, {"v": index})
    assert len(cache) == 4, "超限后仍应裁到上限"
    for index, key in enumerate(fresh):
        assert cache.get(key) == {"v": index}, (
            "活跃用户的新鲜条目被过期条目挤掉了"
        )
    assert cache.get(stale_bob) is None
    assert cache.get(stale_carol) is None
    print("✓ 幂等缓存先清过期再按用户公平淘汰")


def test_eviction_tie_targets_most_entries_owner():
    """条目数并列时的实际淘汰口径（尽力而为，文档已按此修正）。

    多用户各占一条、新用户写入时 counts 全为 1：victim 是全局最旧条目
    的归属，新写入会淘汰另一用户仍在 TTL 内的条目——行为用用例钉死，
    避免实现与文档承诺再次漂移。
    """
    cache = IdempotencyCache(max_entries=3, ttl_sec=600)
    keys = [make_key(user, "s", "k") for user in ("alice", "bob", "carol")]
    for key in keys:
        cache.put(key, {"ok": True})
    cache.put(make_key("dave", "s", "k"), {"ok": True})
    assert len(cache) == 3
    assert cache.get(keys[0]) is None, "并列时淘汰全局最旧条目的归属"
    assert cache.get(make_key("dave", "s", "k")) is not None
    print("✓ 幂等缓存条目并列时按全局最旧淘汰（尽力而为）")


if __name__ == "__main__":
    import _stdio

    _stdio.ensure_utf8()
    import pytest

    mp = pytest.MonkeyPatch()
    try:
        test_put_get_returns_deep_copy()
        test_ttl_expiry(mp)
    finally:
        mp.undo()
    mp = pytest.MonkeyPatch()
    try:
        test_eviction_purges_expired_before_fair(mp)
    finally:
        mp.undo()
    test_lru_eviction()
    test_eviction_is_per_user_fair()
    test_key_normalization_and_clear()
    test_key_binds_endpoint_and_message()
    test_discard_session_clears_all_forms()
    test_eviction_tie_targets_most_entries_owner()
    print("\n全部通过")
