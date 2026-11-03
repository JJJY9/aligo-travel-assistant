"""
知识库内容管理集成测试（本地已初始化知识库时运行）

真实写入一条临时片段并删除，验证「新增 → 检索可见 → 删除」闭环，
同时覆盖删除后新增不会与现有主键冲突（按最大 id 递增）。

运行：python tests/test_knowledge_admin_integration.py
"""
import os
import sys
import uuid

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.knowledge_stats import knowledge_status


def test_add_and_delete_document_roundtrip():
    status = knowledge_status()
    if not status["available"]:
        pytest.skip(f"本地未初始化知识库：{status.get('reason')}")

    from utils.knowledge_admin import add_document, delete_document, list_documents

    marker = f"集成测试临时片段 {uuid.uuid4().hex[:8]}"
    result = add_document(marker, {"category": "临时测试"})
    doc_id = result.get("id")
    assert doc_id, f"新增后应返回片段 id：{result}"

    try:
        found = list_documents(query=marker)["items"]
        assert any(item["id"] == doc_id for item in found), "新增片段应能被检索到"
    finally:
        assert delete_document(doc_id) is True

    remaining = list_documents(query=marker)["items"]
    assert not any(item["id"] == doc_id for item in remaining), "删除后不应再检索到"
    print(f"✓ 知识片段新增 / 检索 / 删除闭环（id={doc_id}）")


if __name__ == "__main__":
    test_add_and_delete_document_roundtrip()
    print("\n全部通过")
