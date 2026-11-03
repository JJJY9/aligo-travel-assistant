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


if __name__ == "__main__":
    test_uses_max_id_not_row_count()
    test_empty_collection_starts_at_one()
    test_query_failure_propagates()
    test_load_failure_still_queries()
    print("\n全部通过")
