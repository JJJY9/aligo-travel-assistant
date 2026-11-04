"""
知识库内容管理集成测试（本地已初始化知识库时运行）

真实写入临时片段并删除，验证「新增 → 语义检索可见 → 删除」闭环，
并覆盖删除后新增从最大 id 继续递增（不复用主键、不覆盖已有知识）。

运行：python tests/test_knowledge_admin_integration.py
"""
import importlib.util
import os
import sys
import uuid

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.knowledge_stats import knowledge_status


def _skip_reason() -> str | None:
    """返回跳过原因；None 表示当前环境可以运行真实写库测试。"""
    if importlib.util.find_spec("sentence_transformers") is None:
        return "未安装 sentence-transformers（新增片段需要向量化）"
    status = knowledge_status()
    if status["available"]:
        return None
    reason = str(status.get("reason", "未知原因"))
    if "Fail connecting" in reason or "Open local milvus failed" in reason:
        # Milvus Lite 在同一进程反复开关客户端后偶发连接失败，属测试环境问题
        return f"Milvus Lite 暂不可用：{reason}"
    return f"本地未初始化知识库：{reason}"


def test_add_search_and_delete_roundtrip():
    reason = _skip_reason()
    if reason:
        pytest.skip(reason)

    from utils.knowledge_admin import (
        add_document,
        delete_document,
        list_documents,
        search_documents,
    )

    first_marker = f"集成测试临时片段 A {uuid.uuid4().hex[:8]}"
    second_marker = f"集成测试临时片段 B {uuid.uuid4().hex[:8]}"

    first = add_document(first_marker, {"category": "临时测试"})
    first_id = first.get("id")
    assert first_id, f"新增后应返回片段 id：{first}"

    try:
        found = list_documents(query=first_marker)["items"]
        assert any(item["id"] == first_id for item in found), "新增片段应能在列表中查到"

        # 语义检索链路（RAG 实际使用的路径）也应能命中新片段
        hits = search_documents(first_marker, top_k=3)
        assert any(first_marker in str(hit.get("content", "")) for hit in hits), (
            f"新增片段应能被语义检索命中，实际：{hits}"
        )
    finally:
        assert delete_document(first_id) is True

    remaining = list_documents(query=first_marker)["items"]
    assert not any(item["id"] == first_id for item in remaining), "删除后不应再检索到"

    # 删除后再新增：新 id 必须大于现有最大 id，且不能覆盖已有片段的内容
    before_items = list_documents(limit=100000)["items"]
    before_max = max((item["id"] for item in before_items), default=0)
    before_max_content = next(
        (item["content"] for item in before_items if item["id"] == before_max), ""
    )

    second = add_document(second_marker, {"category": "临时测试"})
    second_id = second.get("id")
    try:
        assert second_id and second_id > before_max, (
            f"新增 id 应大于现有最大 id（before={before_max}, second={second_id}）"
        )
        after_items = list_documents(limit=100000)["items"]
        assert any(item["id"] == before_max and item["content"] == before_max_content
                   for item in after_items), "原有最大 id 片段的内容不应被覆盖"
    finally:
        assert delete_document(second_id) is True

    print(f"✓ 知识片段新增 / 语义检索 / 删除闭环（id={first_id} → {second_id}）")


def test_update_document_roundtrip():
    """更新片段：内容与 metadata 覆盖、重新嵌入后可检索，删除后不残留。"""
    reason = _skip_reason()
    if reason:
        pytest.skip(reason)

    from utils.knowledge_admin import (
        add_document,
        delete_document,
        list_documents,
        search_documents,
        update_document,
    )

    old_marker = f"集成测试原始内容 {uuid.uuid4().hex[:8]}"
    new_marker = f"集成测试更新内容 {uuid.uuid4().hex[:8]}"

    created = add_document(old_marker, {"category": "旧分类"})
    doc_id = created.get("id")
    assert doc_id, f"新增后应返回片段 id：{created}"

    try:
        result = update_document(doc_id, new_marker, {"category": "临时测试"})
        assert result == {"status": "success", "id": doc_id}

        items = list_documents(query=new_marker)["items"]
        updated = next((item for item in items if item["id"] == doc_id), None)
        assert updated is not None, "更新后应能按新内容查到"
        assert updated["content"] == new_marker, "内容应被覆盖"
        assert updated["metadata"].get("category") == "临时测试"

        assert not list_documents(query=old_marker)["items"], "旧内容不应残留"

        hits = search_documents(new_marker, top_k=3)
        assert any(new_marker in str(hit.get("content", "")) for hit in hits), (
            f"更新后的片段应能被语义检索命中，实际：{hits}"
        )
    finally:
        assert delete_document(doc_id) is True

    print(f"✓ 知识片段更新闭环（重新嵌入 + 覆盖旧内容，id={doc_id}）")


def test_export_import_roundtrip_is_idempotent():
    """导出 → 原样导入：已有片段全部按内容跳过，数量不变。"""
    reason = _skip_reason()
    if reason:
        pytest.skip(reason)

    from utils.knowledge_admin import (
        export_documents,
        import_documents,
        list_documents,
    )

    documents = export_documents()
    assert documents, "本地知识库应有片段"
    before = list_documents(limit=1)["total"]
    result = import_documents(documents)
    assert result == {"imported": 0, "skipped": len(documents)}, (
        "重复导入同一备份应全部跳过"
    )
    assert list_documents(limit=1)["total"] == before
    print(f"✓ 导出→导入幂等（{len(documents)} 条全部跳过）")


if __name__ == "__main__":
    skip_reason = _skip_reason()
    if skip_reason:
        print(f"跳过：{skip_reason}")
    else:
        test_add_search_and_delete_roundtrip()
        test_update_document_roundtrip()
        test_export_import_roundtrip_is_idempotent()
        print("\n全部通过")
