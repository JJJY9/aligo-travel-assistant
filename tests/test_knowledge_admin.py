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

    by_category = knowledge_admin.list_documents(category="住宿")
    assert by_category["total"] == 1
    assert by_category["items"][0]["id"] == 3
    assert knowledge_admin.list_documents(category="交通")["items"][0]["id"] == 1
    assert knowledge_admin.list_documents(category="不存在")["total"] == 0
    print("✓ 知识片段列表（解析 / 排序 / 过滤 / 分类 / 分页）")


def test_list_categories_counts(monkeypatch):
    rows = [
        {"id": 4, "content": "住宿标准 A", "metadata": '{"category": "住宿"}'},
        {"id": 3, "content": "住宿标准 B", "metadata": '{"category": "住宿"}'},
        {"id": 2, "content": "交通标准", "metadata": '{"category": "交通"}'},
        {"id": 1, "content": "无分类", "metadata": "{}"},
    ]
    fake = _FakeMilvusClient(rows)
    monkeypatch.setattr(knowledge_admin, "_client", lambda: fake)

    categories = knowledge_admin.list_categories()
    assert categories == [
        {"category": "住宿", "count": 2},
        {"category": "交通", "count": 1},
    ], "按数量倒序，未分类不出现"
    print("✓ 知识分类统计（按数量倒序 / 忽略未分类）")


def test_add_document_maps_result(monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))
    agent = _FakeAgent({"status": "success", "ids": [42], "total_count": 43})
    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: agent)

    result = knowledge_admin.add_document("测试内容", {"category": "测试"})
    assert result == {"status": "success", "id": 42, "total_count": 43}
    assert agent.calls == [[{"content": "测试内容", "metadata": {"category": "测试"}}]]

    with pytest.raises(ValueError):
        knowledge_admin.add_document("   ")
    print("✓ 新增知识片段（参数校验 / 返回值映射）")


def test_add_document_error_raises(monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))
    agent = _FakeAgent({"status": "error", "message": "写入失败"})
    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: agent)
    with pytest.raises(RuntimeError, match="写入失败"):
        knowledge_admin.add_document("内容")
    print("✓ 新增失败时抛出错误")


def test_update_document_rewrites_content(monkeypatch):
    from pathlib import Path

    rows = [{"id": 7, "content": "旧内容", "metadata": '{"category": "住宿"}'}]
    fake_client = _FakeMilvusClient(rows)
    monkeypatch.setattr(knowledge_admin, "_client", lambda: fake_client)
    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))

    class _UpdateAgent:
        def __init__(self):
            self.calls = []

        def update_document(self, doc_id, content, metadata):
            self.calls.append((doc_id, content, metadata))
            return {"status": "success", "id": doc_id}

    agent = _UpdateAgent()
    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: agent)

    result = knowledge_admin.update_document(7, "  新内容  ", {"category": "交通"})
    assert result == {"status": "success", "id": 7}
    assert agent.calls == [(7, "新内容", {"category": "交通"})]

    with pytest.raises(LookupError):
        knowledge_admin.update_document(99, "不存在")
    with pytest.raises(ValueError):
        knowledge_admin.update_document(7, "   ")
    print("✓ 知识片段更新（存在性 / 内容清洗 / 缺省 id）")


def test_update_document_error_propagates(monkeypatch):
    from pathlib import Path

    fake_client = _FakeMilvusClient([{"id": 7, "content": "旧内容", "metadata": "{}"}])
    monkeypatch.setattr(knowledge_admin, "_client", lambda: fake_client)
    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))

    class _FailAgent:
        def update_document(self, doc_id, content, metadata):
            return {"status": "error", "message": "embedding failed"}

    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: _FailAgent())
    with pytest.raises(RuntimeError, match="embedding failed"):
        knowledge_admin.update_document(7, "新内容")
    print("✓ 知识片段更新失败向上抛出（不静默成功）")


def test_delete_document_checks_existence(monkeypatch):
    fake = _FakeMilvusClient([{"id": 5, "content": "x", "metadata": "{}"}])
    monkeypatch.setattr(knowledge_admin, "_client", lambda: fake)

    assert knowledge_admin.delete_document(5) is True
    assert fake.deleted == [5]
    assert knowledge_admin.delete_document(99) is False
    print("✓ 删除知识片段（存在性判断）")


class _BrokenMilvusClient(_FakeMilvusClient):
    def query(self, *args, **kwargs):
        raise RuntimeError("collection is in state 'released'")


def test_query_errors_propagate(monkeypatch):
    """查询失败不能伪装成空库 / 片段不存在，否则接口会返回错误的 200 / 404。"""
    fake = _BrokenMilvusClient([])
    monkeypatch.setattr(knowledge_admin, "_client", lambda: fake)

    with pytest.raises(RuntimeError, match="released"):
        knowledge_admin.list_documents()
    with pytest.raises(RuntimeError, match="released"):
        knowledge_admin.delete_document(1)
    print("✓ 查询失败向上抛出（不伪装成空库 / 不存在）")


def test_add_document_requires_initialized_db(monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(
        knowledge_admin, "knowledge_db_path", lambda: Path("missing-dir/milvus_lite.db")
    )
    with pytest.raises(FileNotFoundError, match="知识库未初始化"):
        knowledge_admin.add_document("内容")
    print("✓ 知识库未初始化时新增给出明确错误")


def test_search_documents_delegates_to_agent(monkeypatch):
    class _SearchAgent:
        def __init__(self):
            self.calls = []

        def search_knowledge(self, query, top_k=5):
            self.calls.append((query, top_k))
            return [{"content": "命中"}]

    agent = _SearchAgent()
    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: agent)
    result = knowledge_admin.search_documents("住宿标准", top_k=3)
    assert result == [{"content": "命中"}]
    assert agent.calls == [("住宿标准", 3)]
    print("✓ 语义检索委托 RAG Agent")


def test_export_documents_sorted_and_stripped(monkeypatch):
    fake = _FakeMilvusClient(
        [
            {"id": 3, "content": "c3", "metadata": "{}"},
            {"id": 1, "content": "c1", "metadata": '{"category": "a"}'},
            {"id": 2, "content": "c2", "metadata": ""},
        ]
    )
    monkeypatch.setattr(knowledge_admin, "_client", lambda: fake)
    documents = knowledge_admin.export_documents()
    assert [item["content"] for item in documents] == ["c1", "c2", "c3"]
    assert all("id" not in item for item in documents), "导出不含数据库 id，导入时重新分配"
    assert documents[0]["metadata"] == {"category": "a"}
    print("✓ 导出知识片段（按 id 升序 / 不含 id）")


def test_import_documents_sanitizes(monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))
    monkeypatch.setattr(
        knowledge_admin, "list_documents", lambda limit, offset=0: {"total": 0, "items": []}
    )
    agent = _FakeAgent({"status": "success", "ids": [1, 2], "total_count": 2})
    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: agent)

    result = knowledge_admin.import_documents(
        [
            {"content": "  a  ", "metadata": {"category": "x"}},
            {"content": ""},
            "not-a-dict",
            {
                "content": "b" * 2100,
                "metadata": {f"k{i}": "v" * 300 for i in range(15)},
            },
        ]
    )
    assert result == {"imported": 2, "skipped": 2}
    sent = agent.calls[0]
    assert sent[0]["content"] == "a"
    assert sent[1]["content"] == "b" * 2000
    assert len(sent[1]["metadata"]) == 10
    assert max(len(value) for value in sent[1]["metadata"].values()) == 200
    print("✓ 导入知识片段（跳过空项 / 截断超长字段）")


def test_import_skips_existing_and_duplicate_content(monkeypatch):
    """已有内容与同批次重复内容都跳过，重复导入同一备份不会产生重复片段。"""
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))
    monkeypatch.setattr(
        knowledge_admin,
        "list_documents",
        lambda limit, offset=0: {
            "total": 1,
            "items": [{"id": 1, "content": "已有内容", "metadata": {}}],
        },
    )
    agent = _FakeAgent({"status": "success", "ids": [2], "total_count": 2})
    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: agent)

    result = knowledge_admin.import_documents(
        [{"content": "已有内容"}, {"content": "新内容"}, {"content": "新内容"}]
    )
    assert result == {"imported": 1, "skipped": 2}
    assert [item["content"] for item in agent.calls[0]] == ["新内容"]
    print("✓ 导入按内容去重（已有 / 同批次重复）")


def test_import_empty_and_missing_db(monkeypatch):
    from pathlib import Path

    result = knowledge_admin.import_documents([{"content": "  "}, None])
    assert result == {"imported": 0, "skipped": 2}

    monkeypatch.setattr(
        knowledge_admin, "knowledge_db_path", lambda: Path("missing/milvus_lite.db")
    )
    with pytest.raises(FileNotFoundError, match="知识库未初始化"):
        knowledge_admin.import_documents([{"content": "x"}])
    print("✓ 空导入返回 0；知识库缺失时报错")


def test_import_chunks_large_documents(monkeypatch):
    """大文件按块嵌入 / 插入，控制峰值内存。"""
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))
    monkeypatch.setattr(
        knowledge_admin, "list_documents", lambda limit, offset=0: {"total": 0, "items": []}
    )

    class _BatchAgent:
        def __init__(self):
            self.calls = []

        def add_documents(self, documents):
            self.calls.append([item["content"] for item in documents])
            return {
                "status": "success",
                "ids": list(range(len(documents))),
                "total_count": len(documents),
            }

    agent = _BatchAgent()
    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: agent)

    documents = [{"content": f"item-{i}"} for i in range(1200)]
    result = knowledge_admin.import_documents(documents)
    assert result == {"imported": 1200, "skipped": 0}
    assert [len(batch) for batch in agent.calls] == [500, 500, 200]
    print("✓ 大文件导入按 500 条分块嵌入 / 插入")


if __name__ == "__main__":
    for case in (
        test_list_documents_parses_sorts_and_filters,
        test_list_categories_counts,
        test_add_document_maps_result,
        test_add_document_error_raises,
        test_update_document_rewrites_content,
        test_update_document_error_propagates,
        test_delete_document_checks_existence,
        test_query_errors_propagate,
        test_add_document_requires_initialized_db,
        test_search_documents_delegates_to_agent,
        test_export_documents_sorted_and_stripped,
        test_import_documents_sanitizes,
        test_import_skips_existing_and_duplicate_content,
        test_import_chunks_large_documents,
        test_import_empty_and_missing_db,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    print("\n全部通过")
