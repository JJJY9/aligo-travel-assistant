"""
知识库状态测试（无需 API Key；本地已初始化知识库时验证片段数）

运行：python tests/test_knowledge_stats.py
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.knowledge_stats import DEFAULT_COLLECTION, knowledge_status


def test_missing_database():
    with tempfile.TemporaryDirectory() as tmp:
        status = knowledge_status(Path(tmp) / "missing.db")
    assert status["available"] is False
    assert status["reason"] == "知识库未初始化"
    assert status["collection"] == DEFAULT_COLLECTION
    assert status["path"].endswith("missing.db")
    print("✓ 知识库缺失时返回不可用")


def test_default_knowledge_base_if_present():
    status = knowledge_status()
    if not status["available"]:
        pytest.skip(f"本地未初始化知识库：{status.get('reason')}")
    assert status["documents"] > 0
    print(f"✓ 本地知识库：{status['documents']} 片段")


def test_dimension_mismatch_best_effort(monkeypatch):
    """状态附带集合维度；与已加载嵌入模型不一致时标记 mismatch。"""
    import utils.knowledge_stats as stats_module

    class _Client:
        def has_collection(self, name):
            return True

        def get_collection_stats(self, name):
            return {"row_count": 3}

        def describe_collection(self, name):
            return {
                "fields": [
                    {"name": "id", "params": {}},
                    {"name": "embedding", "params": {"dim": 1024}},
                ]
            }

    monkeypatch.setattr(stats_module, "get_milvus_client", lambda path: _Client())
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "milvus_lite.db"
        db.write_bytes(b"x")

        monkeypatch.setattr(stats_module, "_loaded_embedding_dim", lambda: 512)
        status = knowledge_status(db)
        assert status["available"] and status["documents"] == 3
        assert status["dimension"] == 1024
        assert status["dimension_mismatch"] is True, "换嵌入模型后状态必须标记不一致"

        monkeypatch.setattr(stats_module, "_loaded_embedding_dim", lambda: 1024)
        status = knowledge_status(db)
        assert status["dimension_mismatch"] is False

        monkeypatch.setattr(stats_module, "_loaded_embedding_dim", lambda: None)
        status = knowledge_status(db)
        assert "dimension_mismatch" not in status, "模型未加载时不做判断"
    print("✓ 状态维度与嵌入模型 mismatch（best-effort）")

if __name__ == "__main__":
    test_missing_database()
    test_dimension_mismatch_best_effort(pytest.MonkeyPatch())
    status = knowledge_status()
    if status["available"]:
        print(f"✓ 本地知识库：{status['documents']} 片段")
    else:
        print(f"（本地知识库不可用：{status.get('reason')}）")
    print("\n全部通过")
