"""
Milvus 客户端工具测试（无需 pymilvus / 网络）

覆盖 next_entity_id：加载集合后再查询、按最大 id 递增（而非行数）、
查询失败向上抛出（避免主键冲突导致静默覆盖）。

运行：python tests/test_milvus_client.py
"""
import os
import sys

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.milvus_client import next_entity_id


class _FakeClient:
    def __init__(self, rows, fail_query=False, fail_load=False):
        self.rows = rows
        self.fail_query = fail_query
        self.fail_load = fail_load
        self.load_calls = []
        self.query_calls = []

    def load_collection(self, name):
        self.load_calls.append(name)
        if self.fail_load:
            raise RuntimeError("load failed")

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        if self.fail_query:
            raise RuntimeError("collection is in state 'released'")
        return list(self.rows)


def test_uses_max_id_not_row_count():
    # 删除过片段后行数（3）小于最大 id（141）；按行数递增会与已有主键冲突
    client = _FakeClient([{"id": 3}, {"id": 141}, {"id": 7}])
    assert next_entity_id(client, "knowledge") == 142
    assert client.load_calls == ["knowledge"], "查询前必须先 load_collection"
    print("✓ 按最大 id 递增（不受删除造成的空洞影响）")


def test_empty_collection_starts_at_one():
    client = _FakeClient([])
    assert next_entity_id(client, "knowledge") == 1
    print("✓ 空集合从 1 开始")


def test_query_failure_propagates():
    client = _FakeClient([], fail_query=True)
    with pytest.raises(RuntimeError, match="released"):
        next_entity_id(client, "knowledge")
    print("✓ 查询失败向上抛出，不用行数兜底")


def test_load_failure_still_queries():
    # 旧版本没有 load_collection：失败后仍应继续查询
    client = _FakeClient([{"id": 9}], fail_load=True)
    assert next_entity_id(client, "knowledge") == 10
    print("✓ load_collection 不可用时仍继续查询")


def test_client_creation_retries_transient_failure(monkeypatch):
    """另一进程刚释放文件锁时首次打开可能失败，应重试一次。"""
    from utils import milvus_client as mc

    calls = {"n": 0}

    class _Fake:
        def close(self):
            pass

    def fake_create(db_path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("another process holds the lock")
        return _Fake()

    monkeypatch.setattr(mc, "_create_client", fake_create)
    monkeypatch.setattr(mc.time, "sleep", lambda _seconds: None)

    path = "retry-once-test.db"
    try:
        client = mc.get_milvus_client(path)
        assert calls["n"] == 2
        assert mc.peek_milvus_client(path) is client
    finally:
        mc.reset_milvus_client(path)
    print("✓ 客户端首次打开失败时重试一次")


def test_client_creation_retry_failure_propagates(monkeypatch):
    from utils import milvus_client as mc

    def always_fail(db_path):
        raise RuntimeError("still locked")

    monkeypatch.setattr(mc, "_create_client", always_fail)
    monkeypatch.setattr(mc.time, "sleep", lambda _seconds: None)

    path = "retry-fail-test.db"
    try:
        with pytest.raises(RuntimeError, match="still locked"):
            mc.get_milvus_client(path)
        assert mc.peek_milvus_client(path) is None
    finally:
        mc.reset_milvus_client(path)
    print("✓ 重试仍失败时向上抛出，不缓存坏客户端")


if __name__ == "__main__":
    for plain_case in (
        test_uses_max_id_not_row_count,
        test_empty_collection_starts_at_one,
        test_query_failure_propagates,
        test_load_failure_still_queries,
    ):
        plain_case()
    for patched_case in (
        test_client_creation_retries_transient_failure,
        test_client_creation_retry_failure_propagates,
    ):
        mp = pytest.MonkeyPatch()
        try:
            patched_case(mp)
        finally:
            mp.undo()
    print("\n全部通过")
