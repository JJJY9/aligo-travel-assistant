"""
知识库内容管理测试（无需 API Key、不访问网络）

覆盖：
1. list_documents 解析 / 排序 / 过滤 / 分页；
2. add_document 参数校验与结果映射；
3. delete_document 存在性判断。

运行：python tests/test_knowledge_admin.py
"""
import os
import sys

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils import knowledge_admin


class _FakeMilvusClient:
    def __init__(self, rows):
        self.rows = list(rows)
        self.deleted = []

    def load_collection(self, name):
        pass

    def query(self, collection_name, filter="", output_fields=None, limit=10):
        if filter.startswith("id =="):
            doc_id = int(filter.split("==")[1])
            return [row for row in self.rows if int(row["id"]) == doc_id]
        return list(self.rows)

    def delete(self, collection_name, ids):
        self.deleted.extend(ids)
        self.rows = [row for row in self.rows if int(row["id"]) not in ids]


class _FakeAgent:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def add_documents(self, documents):
        self.calls.append(documents)
        return self.result


def test_list_documents_parses_sorts_and_filters(monkeypatch):
    rows = [
        {"id": 3, "content": "住宿标准 600 元", "metadata": '{"category": "住宿"}'},
        {"id": 1, "content": "交通标准", "metadata": '{"category": "交通"}'},
        {"id": 2, "content": "餐饮标准", "metadata": "not-json"},
    ]
    fake = _FakeMilvusClient(rows)
    monkeypatch.setattr(knowledge_admin, "_client", lambda: fake)

    data = knowledge_admin.list_documents(limit=2, offset=0)
    assert data["total"] == 3
    assert [item["id"] for item in data["items"]] == [3, 2], "按 id 倒序，最新片段在前"
    assert data["items"][1]["metadata"] == {}, "非法 metadata 应兜底为空字典"

    page2 = knowledge_admin.list_documents(limit=2, offset=2)
    assert [item["id"] for item in page2["items"]] == [1]

    filtered = knowledge_admin.list_documents(query="住宿")
    assert filtered["total"] == 1
    assert filtered["items"][0]["id"] == 3
    print("✓ 知识片段列表（解析 / 排序 / 过滤 / 分页）")


def test_add_document_maps_result(monkeypatch):
    agent = _FakeAgent({"status": "success", "ids": [42], "total_count": 43})
    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: agent)

    result = knowledge_admin.add_document("测试内容", {"category": "测试"})
    assert result == {"status": "success", "id": 42, "total_count": 43}
    assert agent.calls == [[{"content": "测试内容", "metadata": {"category": "测试"}}]]

    with pytest.raises(ValueError):
        knowledge_admin.add_document("   ")
    print("✓ 新增知识片段（参数校验 / 返回值映射）")


def test_add_document_error_raises(monkeypatch):
    agent = _FakeAgent({"status": "error", "message": "写入失败"})
    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: agent)
    with pytest.raises(RuntimeError, match="写入失败"):
        knowledge_admin.add_document("内容")
    print("✓ 新增失败时抛出错误")


def test_delete_document_checks_existence(monkeypatch):
    fake = _FakeMilvusClient([{"id": 5, "content": "x", "metadata": "{}"}])
    monkeypatch.setattr(knowledge_admin, "_client", lambda: fake)

    assert knowledge_admin.delete_document(5) is True
    assert fake.deleted == [5]
    assert knowledge_admin.delete_document(99) is False
    print("✓ 删除知识片段（存在性判断）")


if __name__ == "__main__":
    for case in (
        test_list_documents_parses_sorts_and_filters,
        test_add_document_maps_result,
        test_add_document_error_raises,
        test_delete_document_checks_existence,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    print("\n全部通过")
