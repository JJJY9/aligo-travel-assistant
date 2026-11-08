"""
知识库内容管理测试（无需 API Key、不访问网络）

覆盖：
1. list_documents 解析 / 排序 / 过滤 / 分页；
2. add_document 参数校验与结果映射；
3. delete_document 存在性判断。

运行：python tests/test_knowledge_admin.py
"""
import json
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

    def query(self, collection_name, filter="", output_fields=None, limit=10, offset=0):
        if filter.startswith("id =="):
            doc_id = int(filter.split("==")[1])
            return [row for row in self.rows if int(row["id"]) == doc_id]
        if filter.startswith("id >="):
            import re

            match = re.match(r"id >= (\d+)(?: and id < (\d+))?", filter)
            low = int(match.group(1)) if match else 0
            high = int(match.group(2)) if match and match.group(2) else None
            matched = [
                row
                for row in self.rows
                if int(row["id"]) >= low and (high is None or int(row["id"]) < high)
            ]
            return matched[offset : offset + limit]
        # 与真实 Milvus 一致：查询受 limit / offset 限制（分页回归才能被测出）
        return list(self.rows)[offset : offset + limit]

    def query_iterator(self, collection_name, batch_size=1000, limit=-1, filter="", output_fields=None):
        low, high = 0, None
        if filter and filter.startswith("id >="):
            import re

            match = re.match(r"id >= (\d+)(?: and id < (\d+))?", filter)
            if match:
                low = int(match.group(1))
                high = int(match.group(2)) if match.group(2) else None
        rows = [
            row
            for row in self.rows
            if int(row["id"]) >= low and (high is None or int(row["id"]) < high)
        ]

        class _Iterator:
            def __init__(self):
                self.pos = 0

            def next(self):
                batch = rows[self.pos : self.pos + batch_size]
                self.pos += batch_size
                return batch

            def close(self):
                pass

        return _Iterator()

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


def test_list_documents_pages_full_scan(monkeypatch):
    """列表翻页走全量扫描 + 内存分页，不受单次 query 上限影响。"""
    rows = [
        {"id": index, "content": f"c{index}", "metadata": "{}"}
        for index in range(1, 26)
    ]
    fake = _FakeMilvusClient(rows)
    monkeypatch.setattr(knowledge_admin, "_client", lambda: fake)
    monkeypatch.setattr(knowledge_admin, "_IMPORT_CHUNK", 7)  # 迭代器多批次
    page = knowledge_admin.list_documents(limit=5, offset=20)
    assert page["total"] == 25
    assert [item["id"] for item in page["items"]] == [5, 4, 3, 2, 1]
    print("✓ 列表翻页（全量扫描 / 多批次）")


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

    assert knowledge_admin.delete_document(5) == 1
    assert fake.deleted == [5]
    assert knowledge_admin.delete_document(99) == 0
    print("✓ 删除知识片段（存在性判断）")


class _BrokenMilvusClient(_FakeMilvusClient):
    def query(self, *args, **kwargs):
        raise RuntimeError("collection is in state 'released'")

    def query_iterator(self, *args, **kwargs):
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
    import tempfile
    from pathlib import Path

    class _SearchAgent:
        def __init__(self):
            self.calls = []

        def search_knowledge(self, query, top_k=5, strict=False):
            self.calls.append((query, top_k, strict))
            return [{"content": "命中"}]

    db = Path(tempfile.mkdtemp()) / "milvus_lite.db"
    db.write_text("placeholder", encoding="utf-8")
    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: db)

    agent = _SearchAgent()
    # 检索路径使用只读 Agent（create_if_missing=False），不在集合缺失时
    # 静默创建空集合
    monkeypatch.setattr(knowledge_admin, "_get_read_agent", lambda: agent)
    result = knowledge_admin.search_documents("住宿标准", top_k=3)
    assert result == [{"content": "命中"}]
    # strict=True：后端故障要抛出（映射 503），不能伪装成“没有相关内容”
    assert agent.calls == [("住宿标准", 3, True)]
    print("✓ 语义检索委托 RAG Agent（严格模式）")


def test_search_documents_requires_initialized_db(monkeypatch):
    """未初始化的知识库不能被一次检索悄悄建成空库。"""
    import tempfile
    from pathlib import Path

    called = {"agent": 0}

    def fake_agent():
        called["agent"] += 1
        raise AssertionError("未初始化时不应构造 RAG Agent")

    monkeypatch.setattr(
        knowledge_admin,
        "knowledge_db_path",
        lambda: Path(tempfile.mkdtemp()) / "missing.db",
    )
    monkeypatch.setattr(knowledge_admin, "_get_agent", fake_agent)
    with pytest.raises(FileNotFoundError, match="未初始化"):
        knowledge_admin.search_documents("住宿标准")
    assert called["agent"] == 0
    print("✓ 语义检索拒绝未初始化知识库（不会新建空库）")


def test_rag_agent_strict_search_raises_when_uninitialized(monkeypatch):
    """未初始化的知识库在严格模式下抛出（Web 接口据此返回 503）。"""
    from agents.rag_knowledge_agent import RAGKnowledgeAgent

    agent = object.__new__(RAGKnowledgeAgent)
    agent.initialized = False
    assert agent.search_knowledge("住宿标准") == []
    with pytest.raises(RuntimeError, match="未初始化"):
        agent.search_knowledge("住宿标准", strict=True)

    # 空查询不能返回任意 top-k 片段；严格模式抛错（管理接口按 503 处理）
    agent.initialized = True
    assert agent.search_knowledge("   ") == []
    with pytest.raises(ValueError, match="为空"):
        agent.search_knowledge(None, strict=True)
    print("✓ 未初始化知识库的严格检索抛出异常")


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


def test_export_not_truncated_by_default_page(monkeypatch):
    """导出必须取全量（全量扫描），不能退回列表默认分页（20 条）。"""
    rows = [
        {"id": index, "content": f"c{index}", "metadata": "{}"}
        for index in range(1, 26)
    ]
    fake = _FakeMilvusClient(rows)
    monkeypatch.setattr(knowledge_admin, "_client", lambda: fake)
    documents = knowledge_admin.export_documents()
    assert len(documents) == 25, "导出被默认分页截断"
    print("✓ 导出不受列表默认分页截断")


def test_import_documents_sanitizes(monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))
    monkeypatch.setattr(knowledge_admin, "_existing_contents", lambda: set())
    agent = _FakeAgent({"status": "success", "ids": [1], "total_count": 1})
    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: agent)

    long_content = "".join(f"第{i}段。" for i in range(500))
    result = knowledge_admin.import_documents(
        [
            {"content": "  a  ", "metadata": {"category": "x"}},
            {"content": ""},
            "not-a-dict",
            {"content": None},
            {"content": False},
            {"content": 0},
            {"content": long_content, "metadata": {}},
        ]
    )
    assert result == {
        "imported": 1,
        "skipped": 6,
        "skipped_invalid": 6,
        "skipped_duplicate": 0,
    }, "超长单条内容按无效项跳过（不再静默截断）"
    sent = agent.calls[0]
    assert sent[0]["content"] == "a"

    # 元数据清洗与分片标记：用不超过上限的长内容单独验证
    agent2 = _FakeAgent({"status": "success", "ids": [1], "total_count": 1})
    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: agent2)
    medium = "内容" * 300
    knowledge_admin.import_documents(
        [{"content": medium, "metadata": {f"k{i}": "v" * 300 for i in range(15)}}]
    )
    sent2 = agent2.calls[0]
    assert all(len(item["content"]) <= 400 for item in sent2)
    assert "".join(item["content"] for item in sent2) == medium
    meta = sent2[0]["metadata"]
    user_keys = {key for key in meta if key not in {"part", "parts", "group"}}
    assert len(user_keys) == 10, "用户元数据仍按 10 键清洗"
    assert {"part", "parts", "group"} <= set(meta), "分片标记必须写入"
    assert max(len(meta[key]) for key in user_keys) == 200
    print("✓ 导入知识片段（空项 / 非字符串跳过，超长按无效项，长内容拆分）")


def test_import_cross_group_share_keeps_unique_pieces(monkeypatch):
    """共享片段属于别的组时，独特分片以独立片段入库而不是整篇静默丢弃。"""
    from pathlib import Path

    shared = "一、共同条款：" + "甲" * 290
    unique = "二、独有段落：" + "乙" * 290
    full = shared + "\n\n" + unique
    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))
    monkeypatch.setattr(knowledge_admin, "_existing_contents", lambda: {shared})
    # 库内已有同内容 part1，属于另一篇文档（组 GA，parts=2）
    monkeypatch.setattr(
        knowledge_admin, "_existing_group_layout", lambda: {shared: ("GA", 1, 2)}
    )
    agent = _FakeAgent({"status": "success", "ids": [1], "total_count": 1})
    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: agent)

    result = knowledge_admin.import_documents([{"content": full, "metadata": {}}])

    assert result["imported"] == 1, "独特分片不能静默丢失"
    sent = [doc for batch in agent.calls for doc in batch]
    assert len(sent) == 1, sent
    assert "二、独有段落" in sent[0]["content"]
    assert "group" not in sent[0]["metadata"], "独立片段不能带组标记"
    assert "part" not in sent[0]["metadata"]
    print("✓ 跨组共享片段时独特分片以独立片段入库")


def test_import_batch_shared_paragraph_keeps_unique_pieces(monkeypatch):
    """同一批两篇文档共享一段：第二篇的独特分片不能被假布局冲突丢弃。"""
    from pathlib import Path

    shared = "一、共同条款：" + "甲" * 290
    doc1 = shared + "\n\n" + "二、第一篇：" + "乙" * 290
    doc2 = shared + "\n\n" + "三、第二篇：" + "丙" * 290
    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))
    monkeypatch.setattr(knowledge_admin, "_existing_contents", lambda: set())
    # 空库：初始没有同组旧分片（第二篇命中的共享片段来自本批第一篇）
    monkeypatch.setattr(knowledge_admin, "_existing_group_layout", lambda: {})
    agent = _FakeAgent(
        {"status": "success", "ids": list(range(3)), "total_count": 3}
    )
    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: agent)

    result = knowledge_admin.import_documents(
        [{"content": doc1, "metadata": {}}, {"content": doc2, "metadata": {}}]
    )

    assert result["imported"] == 3, result
    assert result["skipped_duplicate"] == 1, result
    sent = [doc for batch in agent.calls for doc in batch]
    joined = "\n".join(doc["content"] for doc in sent)
    assert "三、第二篇" in joined, "第二篇的独特分片必须入库"
    standalone = next(doc for doc in sent if "三、第二篇" in doc["content"])
    assert "group" not in standalone["metadata"], "共享片段的组无法成立：独特分片按独立片段入库"
    print("✓ 同批共享段落时第二篇的独特分片入库")


def test_import_completes_group_with_duplicate_pieces(monkeypatch):
    """组内两片内容相同、库内只存在一片：补齐缺失分片而不是整组跳过。"""
    from pathlib import Path

    para = "一、共同条款：" + "甲" * 280
    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))
    monkeypatch.setattr(knowledge_admin, "_existing_contents", lambda: {para})
    # 库内已有同组的 part1（同文分片的第二片未被写入）
    monkeypatch.setattr(
        knowledge_admin, "_existing_group_layout", lambda: {para: ("G", 1, 2)}
    )
    agent = _FakeAgent({"status": "success", "ids": [2], "total_count": 2})
    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: agent)

    result = knowledge_admin.import_documents(
        [
            {"content": para, "metadata": {"group": "G", "part": "1", "parts": "2"}},
            {"content": para, "metadata": {"group": "G", "part": "2", "parts": "2"}},
        ]
    )

    assert result["imported"] == 1, "缺失的同文分片必须补齐"
    sent = agent.calls[0]
    assert sent[0]["metadata"].get("part") == "2"
    assert sent[0]["metadata"].get("group") == "G"
    print("✓ 组内同文分片缺失时补齐（不再整组跳过）")


def test_add_document_splits_long_content(monkeypatch):
    """长内容按语义边界拆成 ≤400 字片段，返回分片数。"""
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))

    class _Agent:
        def __init__(self):
            self.documents = None

        def add_documents(self, documents):
            self.documents = documents
            return {
                "status": "success",
                "ids": list(range(len(documents))),
                "total_count": len(documents),
            }

    agent = _Agent()
    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: agent)
    content = "第一段内容。" * 200
    result = knowledge_admin.add_document(content, {"category": "测试"})
    assert result["parts"] == len(agent.documents)
    assert len(agent.documents) > 1
    assert all(len(item["content"]) <= 400 for item in agent.documents)
    assert all(item["metadata"]["category"] == "测试" for item in agent.documents)
    assert [item["metadata"]["part"] for item in agent.documents] == [
        str(index + 1) for index in range(len(agent.documents))
    ]
    print("✓ 长内容自动拆分（新增）")


def test_update_document_splits_long_content(monkeypatch):
    """长内容更新时首片覆盖原 id，其余分片以新记录追加。"""
    from pathlib import Path

    rows = [{"id": 7, "content": "旧内容", "metadata": "{}"}]
    fake_client = _FakeMilvusClient(rows)
    monkeypatch.setattr(knowledge_admin, "_client", lambda: fake_client)
    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))

    class _Agent:
        def __init__(self):
            self.updated = None
            self.added = None

        def update_document(self, doc_id, content, metadata):
            self.updated = (doc_id, content, metadata)
            return {"status": "success", "id": doc_id}

        def add_documents(self, documents):
            self.added = documents
            return {"status": "success", "ids": [8, 9], "total_count": 3}

    agent = _Agent()
    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: agent)
    result = knowledge_admin.update_document(7, "长" * 1000, {"category": "测试"})
    assert result["id"] == 7 and result["parts"] > 1
    assert agent.updated[0] == 7
    assert len(agent.updated[1]) <= 400
    assert agent.added, "其余分片应作为新记录追加"
    assert all(len(item["content"]) <= 400 for item in agent.added)
    print("✓ 长内容自动拆分（更新）")


def test_chunk_documents_overrides_stale_part_metadata(monkeypatch):
    """编辑旧分片时传入的旧 part/parts 必须被实际分片编号覆盖。"""
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))

    class _Agent:
        def __init__(self):
            self.documents = None

        def add_documents(self, documents):
            self.documents = documents
            return {
                "status": "success",
                "ids": list(range(len(documents))),
                "total_count": len(documents),
            }

    agent = _Agent()
    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: agent)
    knowledge_admin.add_document(
        "第一段内容。" * 200, {"category": "测试", "part": "2", "parts": "9"}
    )
    assert [item["metadata"]["part"] for item in agent.documents] == [
        str(index + 1) for index in range(len(agent.documents))
    ]
    groups = {item["metadata"].get("group") for item in agent.documents}
    assert len(groups) == 1 and "" not in groups, "一次拆分应共享同一个 group"
    assert all(
        item["metadata"]["parts"] == str(len(agent.documents))
        for item in agent.documents
    )
    print("✓ 分片标记覆盖旧 part/parts")


def test_update_document_rejects_multipart_edit(monkeypatch):
    """多分片记录缺少组标识：单条编辑会留下无法定位的旧分片，应明确拒绝。"""
    from pathlib import Path

    rows = [{"id": 7, "content": "第一段", "metadata": '{"part": "1", "parts": "3"}'}]
    fake_client = _FakeMilvusClient(rows)
    monkeypatch.setattr(knowledge_admin, "_client", lambda: fake_client)
    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))

    class _Agent:
        def update_document(self, *args, **kwargs):
            raise AssertionError("多分片记录不应进入更新流程")

    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: _Agent())
    with pytest.raises(ValueError, match="多分片"):
        knowledge_admin.update_document(7, "新内容")
    assert not fake_client.deleted, "拒绝编辑时不应删除任何分片"
    print("✓ 多分片知识片段拒绝单条编辑")


def test_update_document_replaces_grouped_multipart(monkeypatch):
    """带 group 的多分片记录按整组替换：更新目标分片后删除其余旧分片。"""
    from pathlib import Path

    def _meta(part):
        return json.dumps({"part": str(part), "parts": "3", "group": "g-old", "category": "测试"})

    rows = [
        {"id": 1, "content": "第一段", "metadata": _meta(1)},
        {"id": 2, "content": "第二段", "metadata": _meta(2)},
        {"id": 3, "content": "第三段", "metadata": _meta(3)},
        {"id": 9, "content": "无关片段", "metadata": "{}"},
    ]
    fake_client = _FakeMilvusClient(rows)
    monkeypatch.setattr(knowledge_admin, "_client", lambda: fake_client)
    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))

    class _Agent:
        def __init__(self):
            self.updated = None
            self.added = None

        def update_document(self, doc_id, content, metadata):
            self.updated = (doc_id, content, metadata)
            return {"status": "success", "id": doc_id}

        def add_documents(self, documents):
            self.added = documents
            return {"status": "success", "ids": [10, 11], "total_count": 2}

    agent = _Agent()
    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: agent)
    result = knowledge_admin.update_document(2, "长" * 1000, {"category": "测试"})
    assert agent.updated[0] == 2
    assert result["parts"] > 1
    assert result["removed_ids"] == [1, 3], "整组替换后应删除其余旧分片"
    assert fake_client.deleted == [1, 3]
    new_group = agent.updated[2].get("group")
    assert new_group and new_group != "g-old", "替换后的分片应使用新的组标识"
    assert agent.updated[2]["part"] == "1"
    assert all(item["metadata"].get("group") == new_group for item in agent.added)
    print("✓ 带组标识的多分片记录整组替换")


def test_update_grouped_add_failure_keeps_old_content(monkeypatch):
    """整组替换先写新分片：新增失败时目标分片与旧分片都不能被改动。"""
    from pathlib import Path

    def _meta(part):
        return json.dumps({"part": str(part), "parts": "3", "group": "g-old"})

    rows = [
        {"id": 1, "content": "第一段", "metadata": _meta(1)},
        {"id": 2, "content": "第二段", "metadata": _meta(2)},
        {"id": 3, "content": "第三段", "metadata": _meta(3)},
    ]
    fake_client = _FakeMilvusClient(rows)
    monkeypatch.setattr(knowledge_admin, "_client", lambda: fake_client)
    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))

    class _Agent:
        def __init__(self):
            self.updated = None
            self.added = None

        def update_document(self, *args, **kwargs):
            self.updated = (args, kwargs)
            raise AssertionError("新增失败时不应覆盖目标分片")

        def add_documents(self, documents):
            self.added = documents
            return {"status": "error", "message": "embedding down"}

    agent = _Agent()
    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: agent)
    with pytest.raises(RuntimeError, match="原内容未修改"):
        knowledge_admin.update_document(2, "长" * 1000, {"category": "测试"})
    assert agent.updated is None
    assert fake_client.deleted == [], "失败时不能删除任何旧分片"
    print("✓ 整组替换新增失败时保留旧内容")


def test_update_grouped_update_failure_rolls_back_extras(monkeypatch):
    """新分片写入成功但覆盖目标失败：补偿删除本次插入的分片后报错。"""
    from pathlib import Path

    rows = [{"id": 7, "content": "旧内容", "metadata": "{}"}]
    fake_client = _FakeMilvusClient(rows)
    monkeypatch.setattr(knowledge_admin, "_client", lambda: fake_client)
    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))

    class _Agent:
        def add_documents(self, documents):
            return {"status": "success", "ids": [8, 9], "total_count": 2}

        def update_document(self, doc_id, content, metadata):
            return {"status": "error", "message": "upsert failed"}

    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: _Agent())
    with pytest.raises(RuntimeError, match="upsert failed"):
        knowledge_admin.update_document(7, "长" * 1000, {"category": "测试"})
    assert fake_client.deleted == [8, 9], "应补偿删除本次插入的分片"
    print("✓ 覆盖目标失败时补偿删除新分片")


def test_chunk_documents_marks_even_with_full_metadata(monkeypatch):
    """用户元数据占满 10 键时分片记录仍必须带 part/parts/group。"""
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))

    class _Agent:
        def __init__(self):
            self.documents = None

        def add_documents(self, documents):
            self.documents = documents
            return {
                "status": "success",
                "ids": list(range(len(documents))),
                "total_count": len(documents),
            }

    agent = _Agent()
    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: agent)
    metadata = {f"k{index}": f"v{index}" for index in range(8)}
    knowledge_admin.add_document("第一段内容。" * 200, metadata)
    assert len(agent.documents) > 1
    for item in agent.documents:
        assert item["metadata"]["parts"] == str(len(agent.documents))
        assert item["metadata"]["group"]
    print("✓ 元数据较多时分片标记仍然写入")


def test_existing_contents_scans_all_rows(monkeypatch):
    """去重快照走全量扫描（iterator 分批），不依赖单页 16384 条上限。"""
    rows = [
        {"id": index, "content": f"c{index}", "metadata": "{}"}
        for index in range(1, 26)
    ]
    fake = _FakeMilvusClient(rows)
    monkeypatch.setattr(knowledge_admin, "_client", lambda: fake)
    monkeypatch.setattr(knowledge_admin, "_IMPORT_CHUNK", 4)
    assert knowledge_admin._existing_contents() == {
        row["content"] for row in rows
    }
    documents = knowledge_admin.export_documents()
    assert [item["content"] for item in documents] == [
        f"c{index}" for index in range(1, 26)
    ]
    print("✓ 去重快照与导出的全量扫描")


def test_import_regroups_exported_chunks(monkeypatch):
    """导出备份中同一 group 的分片应按 part 拼回整篇再导入，而不是各自成条。"""
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))
    monkeypatch.setattr(knowledge_admin, "_existing_contents", lambda: set())

    class _Agent:
        def __init__(self):
            self.calls = []

        def add_documents(self, documents):
            self.calls.append(documents)
            return {
                "status": "success",
                "ids": list(range(len(documents))),
                "total_count": len(documents),
            }

    agent = _Agent()
    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: agent)
    text = "".join(f"第{i}段内容，长度适中，用于触发多分片拆分与重组验证。" for i in range(1, 61))
    exported = [
        {
            "content": text[1000:],
            "metadata": {"group": "g1", "part": "2", "parts": "2", "category": "测试"},
        },
        {
            "content": text[:1000],
            "metadata": {"group": "g1", "part": "1", "parts": "2", "category": "测试"},
        },
        {"content": "普通片段", "metadata": {}},
    ]
    knowledge_admin.import_documents(exported)
    added = agent.calls[0]
    grouped = [doc for doc in added if doc["metadata"].get("group")]
    assert len(grouped) > 1, "整篇内容应重新拆分并带组标识"
    assert len({doc["metadata"]["group"] for doc in grouped}) == 1
    rejoined = "".join(doc["content"] for doc in grouped)
    assert rejoined.replace("\n", "") == (text[:1000] + text[1000:]).replace("\n", "")
    assert any(doc["content"] == "普通片段" for doc in added)
    print("✓ 导入按 group 重新合并导出分片")


def test_regroup_requires_multiset_coverage(monkeypatch):
    """组内两片同文、库里只有一份：不能算整组已存在（多重集语义）。"""
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))
    monkeypatch.setattr(knowledge_admin, "_existing_contents", lambda: {"同文片段"})
    # 库里那一份是上次导入中途失败留下的 part 1（同组、同布局）：允许补齐 part 2
    monkeypatch.setattr(
        knowledge_admin,
        "_existing_group_layout",
        lambda: {"同文片段": ("g-multi", 1, 2)},
    )
    documents = [
        {
            "content": "同文片段",
            "metadata": {"group": "g-multi", "part": "1", "parts": "2"},
        },
        {
            "content": "同文片段",
            "metadata": {"group": "g-multi", "part": "2", "parts": "2"},
        },
    ]
    result = knowledge_admin.import_documents(documents, dry_run=True)
    assert result["would_import"] == 1, result
    assert result["skipped_duplicate"] == 1, result
    print("✓ 整组跳过按多重集判定（同文缺片仍补齐）")


def test_import_skips_fully_existing_group(monkeypatch):
    """整组已完整存在于库中时整组跳过，重复导入备份不产生新记录。"""
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))
    part1, part2 = "第一片内容", "第二片内容"
    monkeypatch.setattr(knowledge_admin, "_existing_contents", lambda: {part1, part2})

    class _Agent:
        def __init__(self):
            self.calls = []

        def add_documents(self, documents):
            self.calls.append(documents)
            return {"status": "success", "ids": [], "total_count": 0}

    agent = _Agent()
    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: agent)
    result = knowledge_admin.import_documents(
        [
            {"content": part1, "metadata": {"group": "g", "part": "1", "parts": "2"}},
            {"content": part2, "metadata": {"group": "g", "part": "2", "parts": "2"}},
        ]
    )
    assert result == {
        "imported": 0,
        "skipped": 2,
        "skipped_invalid": 0,
        "skipped_duplicate": 2,
    }
    assert agent.calls == []
    print("✓ 已存在的整组分片导入时整组跳过")


def test_import_restores_large_group_without_truncation(monkeypatch):
    """备份还原的超长整组文档不能被 2000 字导入上限截断。"""
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))
    monkeypatch.setattr(knowledge_admin, "_existing_contents", lambda: set())

    class _Agent:
        def __init__(self):
            self.calls = []

        def add_documents(self, documents):
            self.calls.append(documents)
            return {
                "status": "success",
                "ids": list(range(len(documents))),
                "total_count": len(documents),
            }

    agent = _Agent()
    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: agent)
    text = "".join(f"第{i}段内容，用于验证备份恢复不截断。" for i in range(1, 300))
    parts = [text[index : index + 400] for index in range(0, len(text), 400)]
    documents = [
        {
            "content": part,
            "metadata": {
                "group": "g2",
                "part": str(index + 1),
                "parts": str(len(parts)),
            },
        }
        for index, part in enumerate(parts)
    ]
    knowledge_admin.import_documents(documents)
    rejoined = "".join(doc["content"] for doc in agent.calls[0]).replace("\n", "")
    assert rejoined == text, "整篇内容应完整恢复，不能被导入上限截断"
    print("✓ 超长整组备份恢复不截断")


def test_group_member_ids_scans_all_batches(monkeypatch):
    """分片分散在多个迭代批次时都要扫描到（不依赖单页 16384 条限制）。"""
    rows = [
        {
            "id": index,
            "content": f"c{index}",
            "metadata": json.dumps(
                {"group": "g" if index in (3, 15, 25) else "other"}
            ),
        }
        for index in range(1, 31)
    ]
    fake = _FakeMilvusClient(rows)
    monkeypatch.setattr(knowledge_admin, "_IMPORT_CHUNK", 10)
    assert sorted(knowledge_admin._group_member_ids(fake, "g")) == [3, 15, 25]
    assert knowledge_admin._group_member_ids(fake, "missing") == []
    print("✓ 组内分片跨多个迭代批次扫描")


def test_import_restore_over_limit_skips_document(monkeypatch):
    """备份还原超过上限时按无效项跳过（不静默截断、也不阻断整份备份）。"""
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))
    monkeypatch.setattr(knowledge_admin, "_existing_contents", lambda: set())

    class _Agent:
        def add_documents(self, documents):
            raise AssertionError("超限备份不应进入写入")

    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: _Agent())
    monkeypatch.setattr(knowledge_admin, "DOC_MAX_CHARS", 500)
    part_text = "长" * 300
    documents = [
        {"content": part_text, "metadata": {"group": "g", "part": "1", "parts": "2"}},
        {"content": part_text, "metadata": {"group": "g", "part": "2", "parts": "2"}},
    ]
    result = knowledge_admin.import_documents(documents)
    assert result["imported"] == 0
    assert result["skipped_invalid"] == 1
    print("✓ 超限备份按无效项跳过（不静默截断、不阻断其余文档）")


def test_add_document_enforces_doc_size_limit(monkeypatch):
    """kb-add / Web 新增与导入共用文档大小上限，超长内容明确报错。"""
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "DOC_MAX_CHARS", 50)

    class _Agent:
        def add_documents(self, documents):
            raise AssertionError("超长内容不应进入写入")

    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: _Agent())
    with pytest.raises(ValueError, match="超过上限"):
        knowledge_admin.add_document("长" * 51)

    agent = _FakeAgent({"status": "success", "ids": [1], "total_count": 1})
    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: agent)
    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))
    result = knowledge_admin.add_document("字" * 50)
    assert result["id"] == 1, "恰好等于上限仍应可写入"
    print("✓ 新增知识片段强制文档大小上限")


def test_update_document_enforces_joined_size_limit(monkeypatch):
    """kb-update / 整份编辑超过拼接上限时明确报错（不进写入流程）。"""
    with pytest.raises(ValueError, match="超过上限"):
        knowledge_admin.update_document(1, "长" * (knowledge_admin.JOINED_MAX_CHARS + 1))
    print("✓ 更新知识片段强制整份拼接上限")


def test_import_restore_many_short_parts_not_rejected(monkeypatch):
    """旧数据的极短分片：拼接长度超过 JOINED_MAX_CHARS 也要能还原。

    还原的语义上限是文档大小（分片长度之和），拼接长度只受物理上限约束；
    每片 1 字时 20000 字文档拼接近 40000 字，不能因此拒绝合法备份。
    """
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))
    monkeypatch.setattr(knowledge_admin, "_existing_contents", lambda: set())

    class _Agent:
        def __init__(self):
            self.calls = []

        def add_documents(self, documents):
            self.calls.append(documents)
            return {
                "status": "success",
                "ids": list(range(len(documents))),
                "total_count": len(documents),
            }

    agent = _Agent()
    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: agent)
    pieces = [f"{index:04d}" for index in range(4021)]
    joined_len = sum(len(piece) for piece in pieces) + len(pieces) - 1
    assert joined_len > knowledge_admin.JOINED_MAX_CHARS
    assert sum(len(piece) for piece in pieces) <= knowledge_admin.DOC_MAX_CHARS
    documents = [
        {
            "content": piece,
            "metadata": {"group": "g-tiny", "part": str(index + 1), "parts": str(len(pieces))},
        }
        for index, piece in enumerate(pieces)
    ]
    result = knowledge_admin.import_documents(documents)
    assert result["imported"] >= 1
    restored = "".join(doc["content"] for doc in agent.calls[0]).replace("\n", "")
    assert restored == "".join(pieces), "极短分片的整篇内容应完整还原"
    print("✓ 极短分片备份拼接超限仍可还原（语义上限为文档大小）")


def test_import_restore_strips_padding(monkeypatch):
    """分片首尾空白在还原时被丢弃：不进入正文、不虚增文档大小。"""
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))
    monkeypatch.setattr(knowledge_admin, "_existing_contents", lambda: set())
    monkeypatch.setattr(knowledge_admin, "DOC_MAX_CHARS", 100)

    calls = []

    class _Agent:
        def add_documents(self, documents):
            calls.append(documents)
            return {
                "status": "success",
                "ids": list(range(len(documents))),
                "total_count": len(documents),
            }

    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: _Agent())
    # 每片 strip 后 1 字、原始 31 字：空白不进入正文，按实体内容计 30 字
    pieces = ["x" + " " * 30 for _ in range(30)]
    documents = [
        {
            "content": piece,
            "metadata": {"group": "g-pad", "part": str(index + 1), "parts": str(len(pieces))},
        }
        for index, piece in enumerate(pieces)
    ]
    result = knowledge_admin.import_documents(documents)
    assert result["imported"] >= 1
    stored_total = sum(len(doc["content"]) for batch in calls for doc in batch)
    assert stored_total == 30, "空白不应写入正文（既不虚增大小也不能借空白绕过上限）"
    print("✓ 还原时空白被丢弃（文档大小按实体内容计）")


def test_import_restore_oversize_document_skipped_not_aborting(monkeypatch):
    """超限的还原文档计入 invalid 并跳过，不再让整份备份导入失败。"""
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))
    monkeypatch.setattr(knowledge_admin, "_existing_contents", lambda: set())
    monkeypatch.setattr(knowledge_admin, "DOC_MAX_CHARS", 100)

    class _Agent:
        def add_documents(self, documents):
            raise AssertionError("超限文档不应进入写入")

    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: _Agent())
    pieces = ["x"] * 101  # 实体内容 101 字 > 上限 100
    documents = [
        {
            "content": piece,
            "metadata": {"group": "g-many", "part": str(index + 1), "parts": "101"},
        }
        for index, piece in enumerate(pieces)
    ]
    result = knowledge_admin.import_documents(documents)
    assert result["imported"] == 0
    assert result["skipped_invalid"] == 1, result
    print("✓ 超限还原文档跳过并计数（不中断整份备份）")


def test_list_and_export_hold_write_lock(monkeypatch):
    """列表 / 导出与写入互斥，避免读到整组替换的中间状态。"""

    class _TrackingLock:
        def __init__(self):
            self.entries = 0
            self.exits = 0

        def __enter__(self):
            self.entries += 1
            return self

        def __exit__(self, *exc_info):
            self.exits += 1
            return False

    lock = _TrackingLock()
    monkeypatch.setattr(knowledge_admin, "_WRITE_LOCK", lock)
    fake = _FakeMilvusClient([{"id": 1, "content": "内容", "metadata": "{}"}])
    monkeypatch.setattr(knowledge_admin, "_client", lambda: fake)
    knowledge_admin.list_documents()
    knowledge_admin.export_documents()
    assert lock.entries == 2 and lock.exits == 2
    print("✓ 列表 / 导出持有写锁")


def test_update_group_leftover_reported_when_delete_fails(monkeypatch):
    """整组替换逐条删旧分片：某片失败时其余照常删除，失败项单独报告。"""
    from pathlib import Path

    def _meta(part):
        return json.dumps({"part": str(part), "parts": "3", "group": "g-old"})

    rows = [
        {"id": 1, "content": "第一段", "metadata": _meta(1)},
        {"id": 2, "content": "第二段", "metadata": _meta(2)},
        {"id": 3, "content": "第三段", "metadata": _meta(3)},
    ]

    class _FlakyClient(_FakeMilvusClient):
        def delete(self, collection_name, ids):
            if 3 in ids:
                raise RuntimeError("delete boom")
            super().delete(collection_name, ids)

    fake_client = _FlakyClient(rows)
    monkeypatch.setattr(knowledge_admin, "_client", lambda: fake_client)
    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))

    class _Agent:
        def update_document(self, doc_id, content, metadata):
            return {"status": "success", "id": doc_id}

        def add_documents(self, documents):
            return {"status": "success", "ids": [10], "total_count": 1}

    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: _Agent())
    result = knowledge_admin.update_document(2, "长" * 1000)
    assert result["removed_ids"] == [1], "其余旧分片应照常删除"
    assert result["leftover_ids"] == [3], "删除失败的旧分片要单独报告"
    assert fake_client.deleted == [1]
    print("✓ 整组替换删除失败的旧分片单独报告")


def test_import_partial_group_repairs_missing_parts(monkeypatch):
    """部分分片已存在时只补缺失分片：残缺组被补齐而不是永久残留。"""
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))

    calls = []

    class _Agent:
        def add_documents(self, documents):
            calls.append(documents)
            return {
                "status": "success",
                "ids": list(range(len(documents))),
                "total_count": len(documents),
            }

    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: _Agent())
    first = "甲" * 300
    second = "乙" * 300
    documents = [
        {"content": first, "metadata": {"group": "g-part", "part": "1", "parts": "2"}},
        {"content": second, "metadata": {"group": "g-part", "part": "2", "parts": "2"}},
    ]
    # 第一片已在库中（上次导入中途失败）：只补第二片，组号与编号保持
    monkeypatch.setattr(
        knowledge_admin, "_existing_contents", lambda: {"甲" * 300}
    )
    # 补齐前校验库内旧分片布局：同一篇文档中途失败时 part/parts 一致
    monkeypatch.setattr(
        knowledge_admin,
        "_existing_group_layout",
        lambda: {"甲" * 300: ("g-part", 1, 2)},
    )
    result = knowledge_admin.import_documents(documents)
    assert result["imported"] == 1
    assert result["skipped_duplicate"] == 1
    written = [doc for batch in calls for doc in batch]
    assert [doc["content"] for doc in written] == [second]
    assert written[0]["metadata"]["group"] == "g-part"
    assert written[0]["metadata"]["part"] == "2"
    assert written[0]["metadata"]["parts"] == "2", "补齐后组声明与实际成员一致"
    print("✓ 残缺组只补缺失分片（不产生重复文档）")


def test_import_skips_restore_group_with_layout_conflict(monkeypatch):
    """编辑后重新导入（分片布局变化）不能补写半组：整组跳过并统计。"""
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))
    old_first = "甲" * 300
    old_second = "乙" * 300
    new_pieces = ["甲" * 300, "乙" * 300, "丙" * 300, "丁" * 300]
    monkeypatch.setattr(
        knowledge_admin, "_existing_contents", lambda: {old_first, old_second}
    )
    # 库内旧文档 3 片；备份是编辑后的新文本 4 片，前两片内容相同
    monkeypatch.setattr(
        knowledge_admin,
        "_existing_group_layout",
        lambda: {
            old_first: ("g-edit", 1, 3),
            old_second: ("g-edit", 2, 3),
        },
    )
    documents = [
        {
            "content": piece,
            "metadata": {"group": "g-edit", "part": str(i + 1), "parts": "4"},
        }
        for i, piece in enumerate(new_pieces)
    ]
    result = knowledge_admin.import_documents(documents, dry_run=True)
    assert result["would_import"] == 0, result
    assert result["skipped_duplicate"] == 4, result  # 2 片已存在 + 2 片随组跳过
    print("✓ 布局冲突的还原组整组跳过")


def test_import_tolerates_non_finite_parts_metadata(monkeypatch):
    """备份 metadata 的 parts/part 为非有限数（Infinity）时不能 OverflowError。"""
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))
    monkeypatch.setattr(knowledge_admin, "_existing_contents", lambda: set())
    result = knowledge_admin.import_documents(
        [
            {
                "content": "甲" * 100,
                "metadata": {
                    "group": "g-inf",
                    "parts": float("inf"),
                    "part": float("inf"),
                },
            }
        ],
        dry_run=True,
    )
    assert result["would_import"] == 1, result
    print("✓ 非有限数分片声明按单条处理（不再 OverflowError）")


def test_import_missing_part_conflict_skips_group(monkeypatch):
    """同组旧分片内容被改过时，缺失分片的 part 号仍被占用：整组跳过不产生重复 part。"""
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))
    first = "甲" * 300
    modified = "乙改" * 200
    backup_first = "甲" * 300
    backup_second = "乙" * 300
    monkeypatch.setattr(
        knowledge_admin, "_existing_contents", lambda: {first, modified}
    )
    monkeypatch.setattr(
        knowledge_admin,
        "_existing_group_layout",
        lambda: {first: ("g-dup", 1, 2), modified: ("g-dup", 2, 2)},
    )
    documents = [
        {
            "content": backup_first,
            "metadata": {"group": "g-dup", "part": "1", "parts": "2"},
        },
        {
            "content": backup_second,
            "metadata": {"group": "g-dup", "part": "2", "parts": "2"},
        },
    ]
    result = knowledge_admin.import_documents(documents, dry_run=True)
    assert result["would_import"] == 0, result
    assert result["skipped_duplicate"] == 2, result
    print("✓ 缺失分片 part 号被占用时整组跳过")


def test_import_invalid_group_member_skips_group_not_batch(monkeypatch):
    """组内有一个无效分片：整组跳过，其它合法文档照常导入（备份不整体失败）。"""
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))
    monkeypatch.setattr(knowledge_admin, "_existing_contents", lambda: set())
    documents = [
        {"content": "普通文档", "metadata": {}},
        {"content": "组片1", "metadata": {"group": "gx", "part": "1", "parts": "2"}},
        {"content": "", "metadata": {"group": "gx", "part": "2", "parts": "2"}},
    ]
    result = knowledge_admin.import_documents(documents, dry_run=True)
    assert result["would_import"] == 1, result
    assert result["skipped_invalid"] == 2, result
    print("✓ 组内无效分片整组跳过、不拖垮整份备份")


def test_import_rejects_absurd_parts_declaration(monkeypatch):
    """parts 声明过大（1e12）在构造缺失集合前就被拒绝，不能撑爆内存。"""
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))
    with pytest.raises(ValueError, match="parts 声明异常"):
        knowledge_admin.import_documents(
            [
                {
                    "content": "x",
                    "metadata": {"group": "g", "part": "1", "parts": str(10**12)},
                }
            ],
            dry_run=True,
        )
    print("✓ 异常 parts 声明被拒绝")


def test_import_dedupes_chunks_with_boundary_newline(monkeypatch):
    """分片带切分残留换行时，重复导入仍按 strip 后内容判重（幂等）。"""
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))

    class _Agent:
        def add_documents(self, documents):
            raise AssertionError("全部已存在时不应写入")

    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: _Agent())
    content = "甲" * 399 + "\n" + "乙" * 300
    # 上一轮导入写入的分片：第一片带切分残留的尾部换行
    monkeypatch.setattr(
        knowledge_admin,
        "_existing_contents",
        lambda: {"甲" * 399, "乙" * 300},
    )
    result = knowledge_admin.import_documents([{"content": content}])
    assert result["imported"] == 0
    assert result["skipped_duplicate"] == 2
    print("✓ 分片残留换行不影响重复导入判重")


def test_delete_grouped_fragment_requires_whole_group(monkeypatch):
    """多分片记录拒绝单条删除（避免残留 parts 与实际不符的残缺组）。"""
    import json

    def _meta(part):
        return json.dumps({"group": "g-del2", "part": str(part), "parts": "2"})

    rows = [
        {"id": 1, "content": "第一段", "metadata": _meta(1)},
        {"id": 2, "content": "第二段", "metadata": _meta(2)},
    ]
    fake = _FakeMilvusClient(rows)
    monkeypatch.setattr(knowledge_admin, "_client", lambda: fake)
    with pytest.raises(ValueError, match="整组删除"):
        knowledge_admin.delete_document(1)
    assert fake.deleted == []
    assert knowledge_admin.delete_document(1, whole_group=True) == 2
    print("✓ 多分片记录拒绝单条删除，整组删除正常")


def test_get_document_group_returns_joined_content(monkeypatch):
    """整组回填：按 part 顺序拼接整篇文档；单条返回自身；不存在返回 None。"""
    rows = [
        {
            "id": 1,
            "content": "第一段",
            "metadata": json.dumps(
                {"part": "1", "parts": "3", "group": "g", "category": "测试"}
            ),
        },
        {"id": 2, "content": "第二段", "metadata": json.dumps({"part": "2", "parts": "3", "group": "g"})},
        {"id": 3, "content": "第三段", "metadata": json.dumps({"part": "3", "parts": "3", "group": "g"})},
        {"id": 4, "content": "普通片段", "metadata": "{}"},
    ]
    fake = _FakeMilvusClient(rows)
    monkeypatch.setattr(knowledge_admin, "_client", lambda: fake)

    grouped = knowledge_admin.get_document_group(2)
    assert grouped == {
        "id": 2,
        "group": "g",
        "parts": 3,
        "size": 9,
        # 空串拼接与导入还原同口径：分片是原文的连续片段，换行分隔会把
        # 分隔符带进编辑框并让长文档超提交上限
        "content": "第一段第二段第三段",
    }
    single = knowledge_admin.get_document_group(4)
    assert single == {
        "id": 4,
        "group": "",
        "parts": 1,
        "size": 4,
        "content": "普通片段",
    }
    assert knowledge_admin.get_document_group(99) is None
    print("✓ 整组文档回填（按 part 拼接 / 单条 / 不存在）")


def test_document_group_at_size_limit_roundtrip(monkeypatch):
    """恰好 20000 字的整组文档：回填长度不超上限、备份可完整还原。"""
    from pathlib import Path

    # 独特句子组成的 20000 字文档（避免重复内容被同批去重）
    base = "".join(f"第{i:05d}段验证内容。" for i in range(3000))
    text = base[:20000]
    parts = [text[index : index + 400] for index in range(0, len(text), 400)]
    rows = [
        {
            "id": index + 1,
            "content": part,
            "metadata": json.dumps(
                {
                    "part": str(index + 1),
                    "parts": str(len(parts)),
                    "group": "g-limit",
                }
            ),
        }
        for index, part in enumerate(parts)
    ]
    fake = _FakeMilvusClient(rows)
    monkeypatch.setattr(knowledge_admin, "_client", lambda: fake)
    grouped = knowledge_admin.get_document_group(1)
    assert grouped["parts"] == len(parts)
    assert grouped["size"] == 20000, "文档大小按分片长度之和"
    assert len(grouped["content"]) == 20000, "空串拼接与还原口径一致，不带分隔符"

    # 备份还原：整篇仍是 20000 字，不应触发还原上限错误
    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))
    monkeypatch.setattr(knowledge_admin, "_existing_contents", lambda: set())

    class _Agent:
        def __init__(self):
            self.calls = []

        def add_documents(self, documents):
            self.calls.append(documents)
            return {
                "status": "success",
                "ids": list(range(len(documents))),
                "total_count": len(documents),
            }

    agent = _Agent()
    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: agent)
    exported = [
        {"content": row["content"], "metadata": json.loads(row["metadata"])}
        for row in rows
    ]
    result = knowledge_admin.import_documents(exported)
    assert result["imported"] >= 1
    assert agent.calls, "20000 字整组备份应能还原"
    stored_docs = [doc for batch in agent.calls for doc in batch]
    restored = "".join(doc["content"] for doc in stored_docs).replace("\n", "")
    assert restored == text, "还原后的整篇内容必须完整（不截断、不丢内容）"
    assert sum(len(doc["content"]) for doc in stored_docs) == 20000, (
        "还原后的文档大小不得膨胀（导出→导入必须闭合）"
    )

    # 第二轮：把还原结果再导出→导入到空库，仍应完整且总长不变
    agent2 = _Agent()
    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: agent2)
    monkeypatch.setattr(knowledge_admin, "_existing_contents", lambda: set())
    exported2 = [
        {"content": doc["content"], "metadata": doc["metadata"]} for doc in stored_docs
    ]
    result2 = knowledge_admin.import_documents(exported2)
    assert result2["imported"] >= 1 and agent2.calls
    second_total = sum(len(doc["content"]) for batch in agent2.calls for doc in batch)
    assert second_total == 20000, "二次导出→导入后文档大小仍须保持 20000"
    print("✓ 上限大小整组文档可回填、可还原（多轮导出→导入不膨胀）")


def test_find_duplicate_contents(monkeypatch):
    """重复内容扫描：只统计独立片段，分片记录内部相同段落不算重复。"""
    rows = [
        {"id": 1, "content": "重复内容", "metadata": "{}"},
        {"id": 2, "content": "唯一内容", "metadata": "{}"},
        {"id": 3, "content": " 重复内容 ", "metadata": "{}"},
        {"id": 4, "content": "", "metadata": "{}"},
        {
            "id": 5,
            "content": "重复内容",
            "metadata": json.dumps({"group": "g", "part": "1", "parts": "2"}),
        },
        # 旧版多分片记录（只有 parts 没有 group）：同样不能按单条删除
        {
            "id": 6,
            "content": "旧分片内容",
            "metadata": json.dumps({"part": "1", "parts": "2"}),
        },
        {
            "id": 7,
            "content": "旧分片内容",
            "metadata": json.dumps({"part": "2", "parts": "2"}),
        },
    ]
    fake = _FakeMilvusClient(rows)
    monkeypatch.setattr(knowledge_admin, "_client", lambda: fake)
    # 5 / 6 / 7 号属于多分片记录：整组内容含相同段落是正常的，不参与重复统计
    assert knowledge_admin.find_duplicate_contents() == [("重复内容", [1, 3])]
    print("✓ 重复内容扫描（去空白后相同 / 排除多分片记录与旧版 parts>1）")


def test_import_dry_run_skips_writes(monkeypatch):
    """dry-run 只统计将导入 / 跳过的数量，不加载嵌入模型、不写入。"""
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))
    monkeypatch.setattr(knowledge_admin, "_existing_contents", lambda: set())

    class _Agent:
        def add_documents(self, documents):
            raise AssertionError("dry-run 不应写入")

    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: _Agent())
    result = knowledge_admin.import_documents([{"content": "新内容"}], dry_run=True)
    assert result == {
        "imported": 0,
        "skipped": 0,
        "skipped_invalid": 0,
        "skipped_duplicate": 0,
        "would_import": 1,
    }

    # 已有内容仍计入跳过
    monkeypatch.setattr(
        knowledge_admin, "_existing_contents", lambda: {"已有内容"}
    )
    result2 = knowledge_admin.import_documents(
        [{"content": "已有内容"}, {"content": "新内容"}], dry_run=True
    )
    assert result2 == {
        "imported": 0,
        "skipped": 1,
        "skipped_invalid": 0,
        "skipped_duplicate": 1,
        "would_import": 1,
    }

    # 全部已存在：预览返回稳定结构（would_import = 0）
    result3 = knowledge_admin.import_documents(
        [{"content": "已有内容"}], dry_run=True
    )
    assert result3 == {
        "imported": 0,
        "skipped": 1,
        "skipped_invalid": 0,
        "skipped_duplicate": 1,
        "would_import": 0,
    }
    print("✓ 导入 dry-run 只统计不写入")


def test_import_dry_run_reports_skip_reasons(monkeypatch):
    """预览分别统计跳过原因：已存在（重复）与无效内容。"""
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))
    monkeypatch.setattr(knowledge_admin, "_existing_contents", lambda: {"已有内容"})
    invalid_only = knowledge_admin.import_documents(
        [None, {"content": "  "}], dry_run=True
    )
    assert invalid_only == {
        "imported": 0,
        "skipped": 2,
        "skipped_invalid": 2,
        "skipped_duplicate": 0,
        "would_import": 0,
    }

    result = knowledge_admin.import_documents(
        [
            {"content": "已有内容"},
            {"content": "  "},
            None,
            {"content": "新内容"},
        ],
        dry_run=True,
    )
    assert result["would_import"] == 1
    assert result["skipped"] == 3
    assert result["skipped_duplicate"] == 1
    assert result["skipped_invalid"] == 2
    print("✓ 导入预览区分已存在 / 无效的跳过原因")


def test_import_rejects_incomplete_group(monkeypatch):
    """缺少分片的组必须报错，不能按残片重组（会造成文档被拆段导入）。"""
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))
    monkeypatch.setattr(knowledge_admin, "_existing_contents", lambda: set())

    def _no_agent():
        raise AssertionError("不完整备份不应进入写入")

    monkeypatch.setattr(knowledge_admin, "_get_agent", _no_agent)
    documents = [
        {"content": "第一片", "metadata": {"group": "g", "part": "1", "parts": "3"}},
        {"content": "第二片", "metadata": {"group": "g", "part": "2", "parts": "3"}},
    ]
    with pytest.raises(ValueError, match="多分片文档不完整"):
        knowledge_admin.import_documents(documents)
    print("✓ 不完整的分片组导入报错")


def test_read_agent_not_cached_when_uninitialized(monkeypatch):
    """initialized=False 的只读 Agent 不缓存：集合补齐后检索自动恢复。"""
    import agents.rag_knowledge_agent as rag_module

    calls = []

    class _Agent:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            self.initialized = len(calls) > 1

    monkeypatch.setattr(rag_module, "RAGKnowledgeAgent", _Agent)
    monkeypatch.setattr(knowledge_admin, "_read_agent", None)
    first = knowledge_admin._get_read_agent()
    assert first.initialized is False
    second = knowledge_admin._get_read_agent()
    assert second.initialized is True and len(calls) == 2, "未初始化实例不能缓存"
    knowledge_admin.reset_knowledge_agent()
    assert knowledge_admin._read_agent is None
    print("✓ 只读 Agent 未初始化时不缓存、reset 同时清理")


def test_delete_document_old_parts_without_group_rejected(monkeypatch):
    """旧版 parts>1 且无 group 的多分片记录同样拒绝单条删除。"""
    rows = [
        {
            "id": 1,
            "content": "第一片",
            "metadata": json.dumps({"part": "1", "parts": "2"}),
        },
        {
            "id": 2,
            "content": "第二片",
            "metadata": json.dumps({"part": "2", "parts": "2"}),
        },
    ]
    fake = _FakeMilvusClient(rows)
    monkeypatch.setattr(knowledge_admin, "_client", lambda: fake)
    with pytest.raises(ValueError, match="多分片"):
        knowledge_admin.delete_document(1)
    assert fake.deleted == [], "拒绝删除时不能动库"
    print("✓ 旧版无 group 的多分片记录拒绝单条删除")


def test_update_group_to_single_part_replaces_old_members(monkeypatch):
    """整份编辑成单片短内容：目标片更新后旧组其余分片被删除。"""
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))

    class _Agent:
        def __init__(self):
            self.updated = []
            self.batches = []

        def update_document(self, doc_id, content, metadata):
            self.updated.append((doc_id, content, metadata))
            return {"status": "success"}

        def add_documents(self, documents):
            self.batches.append(documents)
            return {"status": "success", "ids": [99]}

    agent = _Agent()
    fake = _FakeMilvusClient(
        [
            {"id": 1, "content": "旧第一片", "metadata": "{}"},
            {"id": 2, "content": "旧第二片", "metadata": "{}"},
        ]
    )
    monkeypatch.setattr(knowledge_admin, "_client", lambda: fake)
    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: agent)
    monkeypatch.setattr(knowledge_admin, "_document_info", lambda client, doc_id: (2, "g-upd"))
    monkeypatch.setattr(
        knowledge_admin, "_group_member_ids", lambda client, group: [1, 2]
    )

    result = knowledge_admin.update_document(1, "新的短内容")
    assert result.get("status") == "success"
    assert agent.updated and agent.updated[0][1] == "新的短内容"
    assert agent.batches == [], "单片内容不应追加新分片"
    assert fake.deleted == [2], "旧组其余分片应被删除"
    print("✓ 整组→单片编辑删除旧成员")


def test_existing_group_layout_real_scan(monkeypatch):
    """_existing_group_layout 的真实扫描实现：内容→(group, part, parts)。"""
    import json as _json

    rows = [
        {
            "id": 1,
            "content": "分片一",
            "metadata": _json.dumps({"group": "g-layout", "part": "1", "parts": "2"}),
        },
        {
            "id": 2,
            "content": "分片二",
            "metadata": _json.dumps({"group": "g-layout", "part": "2", "parts": "2"}),
        },
        {"id": 3, "content": "独立片段", "metadata": "{}"},
    ]
    fake = _FakeMilvusClient(rows)
    monkeypatch.setattr(knowledge_admin, "_client", lambda: fake)
    layout = knowledge_admin._existing_group_layout()
    assert layout["分片一"] == ("g-layout", 1, 2)
    assert layout["分片二"] == ("g-layout", 2, 2)
    assert layout["独立片段"] == ("", 0, 1)
    print("✓ 库内分片布局真实扫描")


def test_delete_document_whole_group(monkeypatch):
    """group=1 时删除整组分片；无组标识的旧记录只删除当前片段。"""
    def _meta(part, group="g-del"):
        return json.dumps({"part": str(part), "parts": "3", "group": group})

    rows = [
        {"id": 1, "content": "第一段", "metadata": _meta(1)},
        {"id": 2, "content": "第二段", "metadata": _meta(2)},
        {"id": 3, "content": "第三段", "metadata": _meta(3)},
        {"id": 4, "content": "旧分片", "metadata": '{"part": "1", "parts": "3"}'},
    ]
    fake_client = _FakeMilvusClient(rows)
    monkeypatch.setattr(knowledge_admin, "_client", lambda: fake_client)

    assert knowledge_admin.delete_document(2, whole_group=True) == 3
    assert sorted(fake_client.deleted) == [1, 2, 3], "应删除同组全部分片"
    assert knowledge_admin.delete_document(4, whole_group=True) == 1
    assert fake_client.deleted == [1, 2, 3, 4], "无 group 的旧记录只删除当前片段"
    assert knowledge_admin.delete_document(99) == 0
    print("✓ 知识分片整组删除")


def test_import_skips_existing_and_duplicate_content(monkeypatch):
    """已有内容与同批次重复内容都跳过，重复导入同一备份不会产生重复片段。"""
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))
    monkeypatch.setattr(
        knowledge_admin, "_existing_contents", lambda: {"已有内容"}
    )
    agent = _FakeAgent({"status": "success", "ids": [2], "total_count": 2})
    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: agent)

    result = knowledge_admin.import_documents(
        [{"content": "已有内容"}, {"content": "新内容"}, {"content": "新内容"}]
    )
    assert result == {
        "imported": 1,
        "skipped": 2,
        "skipped_invalid": 0,
        "skipped_duplicate": 2,
    }
    assert [item["content"] for item in agent.calls[0]] == ["新内容"]
    print("✓ 导入按内容去重（已有 / 同批次重复）")


def test_import_failure_reports_partial_count(monkeypatch):
    """第二批失败时应报告已持久化的数量（用户可判断是否需重试 / 核对）。"""
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))
    monkeypatch.setattr(knowledge_admin, "_existing_contents", lambda: set())

    class _FailSecondBatch:
        def __init__(self):
            self.calls = 0

        def add_documents(self, documents):
            self.calls += 1
            if self.calls == 1:
                return {
                    "status": "success",
                    "ids": list(range(len(documents))),
                    "total_count": len(documents),
                }
            raise RuntimeError("Milvus 故障")

    agent = _FailSecondBatch()
    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: agent)
    with pytest.raises(RuntimeError, match="已导入 500 条"):
        knowledge_admin.import_documents(
            [{"content": f"item-{i}"} for i in range(800)]
        )
    print("✓ 分批导入失败时报告已导入数量")


def test_import_empty_and_missing_db(monkeypatch):
    from pathlib import Path

    result = knowledge_admin.import_documents([{"content": "  "}, None])
    assert result == {
        "imported": 0,
        "skipped": 2,
        "skipped_invalid": 2,
        "skipped_duplicate": 0,
    }

    monkeypatch.setattr(
        knowledge_admin, "knowledge_db_path", lambda: Path("missing/milvus_lite.db")
    )
    with pytest.raises(FileNotFoundError, match="知识库未初始化"):
        knowledge_admin.import_documents([{"content": "x"}])
    print("✓ 空导入返回 0；知识库缺失时报错")


def test_import_raises_on_agent_error_status(monkeypatch):
    """add_documents 返回 status=error（不抛异常）同样按批次失败上报。"""
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))
    monkeypatch.setattr(knowledge_admin, "_existing_contents", lambda: set())

    class _ErrorAgent:
        def add_documents(self, documents):
            return {"status": "error", "message": "milvus down"}

    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: _ErrorAgent())
    with pytest.raises(RuntimeError, match="第 1 批导入失败") as info:
        knowledge_admin.import_documents([{"content": "片段"}])
    assert "milvus down" in str(info.value)
    print("✓ 导入批次的 status=error 上报")


def test_import_batches_split_only_at_group_start(monkeypatch):
    """按组分批：同一 group 的分片永远落在同一批，只在组起始处切批。"""
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))
    monkeypatch.setattr(knowledge_admin, "_existing_contents", lambda: set())
    monkeypatch.setattr(knowledge_admin, "_IMPORT_CHUNK", 4)

    batches = []

    class _Agent:
        def add_documents(self, documents):
            batches.append([doc["content"] for doc in documents])
            return {"status": "success", "ids": [1] * len(documents)}

    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: _Agent())
    documents = [{"content": f"single-{i}"} for i in range(5)]
    documents += [
        {
            "content": f"group-{i}",
            "metadata": {"group": "g-batch", "part": str(i), "parts": "3"},
        }
        for i in (1, 2, 3)
    ]
    documents += [{"content": "tail-1"}, {"content": "tail-2"}]
    knowledge_admin.import_documents(documents)

    group_batches = [
        batch for batch in batches if any(c.startswith("group-") for c in batch)
    ]
    assert len(group_batches) == 1, batches
    ordered = [c for c in group_batches[0] if c.startswith("group-")]
    assert ordered == ["group-1", "group-2", "group-3"], group_batches
    print("✓ 导入按组起始切批（组不跨批）")


def test_import_chunks_large_documents(monkeypatch):
    """大文件按块嵌入 / 插入，控制峰值内存。"""
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))
    monkeypatch.setattr(knowledge_admin, "_existing_contents", lambda: set())

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
    assert result == {
        "imported": 1200,
        "skipped": 0,
        "skipped_invalid": 0,
        "skipped_duplicate": 0,
    }
    assert [len(batch) for batch in agent.calls] == [500, 500, 200]
    print("✓ 大文件导入按 500 条分块嵌入 / 插入")


def test_import_rejects_oversized_single_content(monkeypatch):
    """单条导入内容超过上限：明确报错而不是静默截断。"""
    from pathlib import Path

    monkeypatch.setattr(knowledge_admin, "knowledge_db_path", lambda: Path(__file__))
    monkeypatch.setattr(knowledge_admin, "_existing_contents", lambda: set())

    class _Agent:
        def add_documents(self, documents):
            return {"status": "success", "ids": [], "total_count": 0}

    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: _Agent())
    oversized = "长" * (knowledge_admin._IMPORT_CONTENT_MAX_CHARS + 10)
    class _Agent:
        def __init__(self):
            self.calls = []

        def add_documents(self, documents):
            self.calls.append(documents)
            return {"status": "success", "ids": [], "total_count": 0}

    agent = _Agent()
    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: agent)
    result = knowledge_admin.import_documents(
        [{"content": oversized, "metadata": {}}]
    )
    assert result["imported"] == 0
    assert result["skipped_invalid"] == 1, "超长内容按无效项跳过"
    assert agent.calls == [], "超长内容不能被截断后写入"
    print("✓ 单条导入超限按无效项跳过（不静默截断）")

def test_next_entity_id_monotonic_after_delete():
    """删除当前最大 id 后不复用（进程内单调），避免旧句柄误伤新记录。"""
    from utils.milvus_client import next_entity_id

    class _Client:
        def __init__(self):
            self.rows = [{"id": 5}]

        def load_collection(self, name):
            pass

        def query(self, collection_name, filter="", output_fields=None, limit=10):
            return list(self.rows)

    client = _Client()
    assert next_entity_id(client, "kb-monotonic", max_scan=10) == 6
    client.rows = []  # 删除最大 id 后集合为空
    assert next_entity_id(client, "kb-monotonic", max_scan=10) == 7, "不得复用已删除的 id"
    print("✓ 实体 id 进程内单调（删除后不复用）")


def test_update_document_rejects_post_chunk_oversize(monkeypatch):
    monkeypatch.setattr(knowledge_admin, "DOC_MAX_CHARS", 100)
    monkeypatch.setattr(knowledge_admin, "JOINED_MAX_CHARS", 210)
    fake = _FakeMilvusClient([{"id": 1, "content": "x", "metadata": "{}"}])
    monkeypatch.setattr(knowledge_admin, "_client", lambda: fake)
    monkeypatch.setattr(
        knowledge_admin,
        "_get_agent",
        lambda: _FakeAgent({"status": "success", "ids": [1]}),
    )
    with pytest.raises(ValueError, match="重新分片后的文档大小超过上限"):
        knowledge_admin.update_document(1, "长" * 150)
    print("✓ 整份编辑校验重新分片后的实际大小")


def test_update_document_scans_old_group_before_writes(monkeypatch):
    """旧组扫描失败时不得写入任何新分片（否则留下无主的新 group）。"""
    calls = []

    class _Agent:
        def add_documents(self, documents):
            calls.append("add")
            return {"status": "success", "ids": [9]}

        def update_document(self, doc_id, content, metadata):
            calls.append("update")
            return {"status": "success"}

    client = _FakeMilvusClient(
        [
            {"id": 1, "content": "甲", "metadata": '{"group": "g1", "part": "1", "parts": "2"}'},
            {"id": 2, "content": "乙", "metadata": '{"group": "g1", "part": "2", "parts": "2"}'},
        ]
    )
    def failing_iterator(*args, **kwargs):
        raise RuntimeError("scan down")

    # 旧组成员是遍历全表的（query_iterator）：模拟扫描失败
    client.query_iterator = failing_iterator
    monkeypatch.setattr(knowledge_admin, "_client", lambda: client)
    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: _Agent())
    with pytest.raises(RuntimeError, match="scan down"):
        knowledge_admin.update_document(1, "长" * 900)
    assert calls == [], "旧组扫描失败时不得写入任何分片"
    print("✓ 旧组扫描在写入之前（失败不留孤儿组）")


def test_update_compensation_failure_reported(monkeypatch):
    """补偿删除失败时把残留分片 id 报给调用方，而不是只写日志。"""

    class _Agent:
        def add_documents(self, documents):
            return {"status": "success", "ids": [9, 10]}

        def update_document(self, doc_id, content, metadata):
            return {"status": "error", "message": "boom"}

    client = _FakeMilvusClient([{"id": 1, "content": "x", "metadata": "{}"}])

    def booming_delete(collection_name, ids):
        raise RuntimeError("delete down")

    client.delete = booming_delete
    monkeypatch.setattr(knowledge_admin, "_client", lambda: client)
    monkeypatch.setattr(knowledge_admin, "_get_agent", lambda: _Agent())
    with pytest.raises(RuntimeError, match=r"残留分片 \[9, 10\]"):
        knowledge_admin.update_document(1, "长" * 900)
    print("✓ 补偿删除失败的残留分片随异常报出")


if __name__ == "__main__":
    test_next_entity_id_monotonic_after_delete()
    for case in (
        test_list_documents_parses_sorts_and_filters,
        test_list_documents_pages_full_scan,
        test_list_categories_counts,
        test_add_document_maps_result,
        test_add_document_error_raises,
        test_add_document_splits_long_content,
        test_update_document_rewrites_content,
        test_update_document_splits_long_content,
        test_chunk_documents_overrides_stale_part_metadata,
        test_update_document_rejects_multipart_edit,
        test_update_document_replaces_grouped_multipart,
        test_update_grouped_add_failure_keeps_old_content,
        test_update_grouped_update_failure_rolls_back_extras,
        test_chunk_documents_marks_even_with_full_metadata,
        test_import_regroups_exported_chunks,
        test_existing_contents_scans_all_rows,
        test_import_skips_fully_existing_group,
        test_import_cross_group_share_keeps_unique_pieces,
        test_import_batch_shared_paragraph_keeps_unique_pieces,
        test_import_completes_group_with_duplicate_pieces,
        test_import_restores_large_group_without_truncation,
        test_group_member_ids_scans_all_batches,
        test_import_restore_over_limit_skips_document,
        test_add_document_enforces_doc_size_limit,
        test_update_document_enforces_joined_size_limit,
        test_import_restore_many_short_parts_not_rejected,
        test_import_restore_strips_padding,
        test_import_restore_oversize_document_skipped_not_aborting,
        test_import_partial_group_repairs_missing_parts,
        test_update_document_rejects_post_chunk_oversize,
        test_update_document_scans_old_group_before_writes,
        test_update_compensation_failure_reported,
        test_import_dedupes_chunks_with_boundary_newline,
        test_delete_grouped_fragment_requires_whole_group,
        test_list_and_export_hold_write_lock,
        test_update_group_leftover_reported_when_delete_fails,
        test_import_rejects_incomplete_group,
        test_import_dry_run_skips_writes,
        test_get_document_group_returns_joined_content,
        test_document_group_at_size_limit_roundtrip,
        test_find_duplicate_contents,
        test_delete_document_whole_group,
        test_update_document_error_propagates,
        test_delete_document_checks_existence,
        test_query_errors_propagate,
        test_add_document_requires_initialized_db,
        test_search_documents_delegates_to_agent,
        test_search_documents_requires_initialized_db,
        test_rag_agent_strict_search_raises_when_uninitialized,
        test_export_documents_sorted_and_stripped,
        test_export_not_truncated_by_default_page,
        test_import_documents_sanitizes,
        test_import_skips_existing_and_duplicate_content,
        test_import_chunks_large_documents,
        test_import_failure_reports_partial_count,
        test_import_empty_and_missing_db,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
        test_import_rejects_oversized_single_content(mp)
    print("\n全部通过")
