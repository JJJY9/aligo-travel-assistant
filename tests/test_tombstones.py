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

    def set(self, key, value, nx=False, ex=None):
        if self.fail:
            raise ConnectionError("redis down")
        self.set_calls.append((key, value, nx, ex))
        return True

    def exists(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        self.exists_calls.append(key)
        return 1 if self.exists_result else 0


def _use_client(monkeypatch, client):
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


if __name__ == "__main__":
    test_deleted_key_matches_web_contract()
    for case in (
        test_mark_writes_nx_with_ttl,
        test_is_session_deleted_reads_shared_key,
        test_missing_redis_means_not_deleted,
        test_redis_errors_are_swallowed,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    print("\n全部通过")
