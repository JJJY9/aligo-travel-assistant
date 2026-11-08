"""共享删除墓碑（utils/tombstones.py）单元测试（无 Redis，桩客户端）。

墓碑协议被 Web 与 CLI 共用：`mark_session_deleted` 写入、`is_session_deleted`
读取（Web /api/import 的墓碑过滤与 CLI user-import 都依赖读接口）。这里覆盖
键格式、TTL、NX 语义与 Redis 不可用 / 异常时的保守回退（按未删除处理）。

运行：python tests/test_tombstones.py
"""
import os
import sys

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from context import storage
from utils import tombstones


class _FakeRedis:
    def __init__(self, exists=False, fail=False):
        self.exists_result = exists
        self.fail = fail
        self.set_calls = []
        self.exists_calls = []
        self.values: dict[str, str] = {}

    def set(self, key, value, nx=False, ex=None):
        self.set_calls.append((key, value, nx, ex))
        if self.fail:
            raise ConnectionError("redis down")
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    def exists(self, key):
        self.exists_calls.append(key)
        if self.fail:
            raise ConnectionError("redis down")
        return 1 if self.exists_result or key in self.values else 0

    def get(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        return self.values.get(key)

    def delete(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        self.values.pop(key, None)
        return 1


def _use_client(monkeypatch, client):
    tombstones.reset_failure_cache()
    monkeypatch.setattr(storage, "get_shared_redis_client", lambda: client)


def test_deleted_key_matches_web_contract():
    """键格式与 Web 端 aligo:deleted:<user>:<session> 完全一致。"""
    assert (
        tombstones.deleted_key("u1", "s1") == "aligo:deleted:u1:s1"
    ), "Web 实例与 CLI 必须读写同一个键"
    print("✓ 墓碑键格式与 Web 契约一致")


def test_mark_writes_nx_with_ttl(monkeypatch):
    """写入用 SET NX + TTL：不覆盖其它实例先写的墓碑，且会自动过期。"""
    client = _FakeRedis()
    _use_client(monkeypatch, client)
    assert tombstones.mark_session_deleted("u1", "s1", ttl_sec=300) is True
    assert client.set_calls == [
        ("aligo:deleted:u1:s1", "cli", True, 300)
    ], client.set_calls
    # TTL 小于 1 秒会被夹到 1（EX 0 非法）
    tombstones.mark_session_deleted("u1", "s1", ttl_sec=0, source="web")
    assert client.set_calls[-1][2] is True and client.set_calls[-1][3] == 1
    print("✓ 墓碑写入 NX + TTL")


def test_is_session_deleted_reads_shared_key(monkeypatch):
    client = _FakeRedis(exists=True)
    _use_client(monkeypatch, client)
    assert tombstones.is_session_deleted("u1", "s1") is True
    assert client.exists_calls == ["aligo:deleted:u1:s1"]
    client.exists_result = False
    assert tombstones.is_session_deleted("u1", "s1") is False
    print("✓ 共享墓碑读取")


def test_missing_redis_means_not_deleted(monkeypatch):
    """未配置 Redis：进程之间没有共享通道，按未删除处理（不抛异常）。"""
    _use_client(monkeypatch, None)
    assert tombstones.is_session_deleted("u1", "s1") is False
    assert tombstones.mark_session_deleted("u1", "s1") is False
    print("✓ Redis 未配置时保守返回未删除")


def test_redis_errors_are_swallowed(monkeypatch):
    """读取 / 写入失败不能中断调用方（删除本身或恢复流程照常继续）。"""
    client = _FakeRedis(fail=True)
    _use_client(monkeypatch, client)
    assert tombstones.is_session_deleted("u1", "s1") is False
    assert tombstones.mark_session_deleted("u1", "s1") is False

    def boom():
        raise RuntimeError("client factory down")

    monkeypatch.setattr(storage, "get_shared_redis_client", boom)
    assert tombstones.is_session_deleted("u1", "s1") is False
    assert tombstones.mark_session_deleted("u1", "s1") is False
    print("✓ Redis 故障时保守回退且不抛异常")


def test_failure_cooldown_skips_redis(monkeypatch):
    """操作失败后进入冷却：逐会话的墓碑检查不再各等满 socket 超时。"""
    client = _FakeRedis(fail=True)
    _use_client(monkeypatch, client)
    assert tombstones.is_session_deleted("u1", "s1") is False
    calls_after_failure = len(client.exists_calls) + len(client.set_calls)
    assert calls_after_failure == 1, "失败调用本身要记录一次尝试"
    # 冷却期内不再触达 Redis（黑洞时每会话各一次 1.5s 超时会拖垮恢复）
    assert tombstones.is_session_deleted("u2", "s2") is False
    assert tombstones.mark_session_deleted("u2", "s2") is False
    assert (
        len(client.exists_calls) + len(client.set_calls) == calls_after_failure
    ), "冷却期内不应再调用 Redis"
    # 清空冷却后恢复尝试
    tombstones.reset_failure_cache()
    client.fail = False
    assert tombstones.is_session_deleted("u2", "s2") is False
    assert len(client.exists_calls) == calls_after_failure + 1
    print("✓ 墓碑失败冷却与恢复")


def test_unmark_only_removes_own_source(monkeypatch):
    """撤销墓碑只删本进程写的值：其它实例（Web token）的墓碑保留。"""
    client = _FakeRedis()
    _use_client(monkeypatch, client)
    client.values["aligo:deleted:u1:s1"] = "web-token"
    assert tombstones.unmark_session_deleted("u1", "s1", source="cli") is False
    assert client.values, "他人墓碑不能被撤销"
    client.values["aligo:deleted:u1:s1"] = "cli"
    assert tombstones.unmark_session_deleted("u1", "s1", source="cli") is True
    assert not client.values
    print("✓ 撤销墓碑只删本进程写入的值")


if __name__ == "__main__":
    test_deleted_key_matches_web_contract()
    for case in (
        test_mark_writes_nx_with_ttl,
        test_is_session_deleted_reads_shared_key,
        test_missing_redis_means_not_deleted,
        test_redis_errors_are_swallowed,
        test_failure_cooldown_skips_redis,
        test_unmark_only_removes_own_source,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    print("\n全部通过")
