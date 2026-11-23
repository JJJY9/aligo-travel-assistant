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
    test_lru_eviction()
    test_key_normalization_and_clear()
    print("\n全部通过")
