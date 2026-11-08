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

def test_dimension_probe_and_collection_errors(monkeypatch):
    """维度探测失败 / 集合不存在 / 统计失败都按可用性语义返回，不抛异常。"""
    import utils.knowledge_stats as stats_module

    class _NoDim:
        def has_collection(self, name):
            return True

        def get_collection_stats(self, name):
            return {"row_count": 2}

        def describe_collection(self, name):
            raise RuntimeError("old client")

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "milvus_lite.db"
        db.write_bytes(b"x")

        monkeypatch.setattr(
            stats_module, "get_milvus_client", lambda path: _NoDim()
        )
        status = knowledge_status(db)
        assert status["available"] and status["documents"] == 2
        assert "dimension" not in status, "维度读不到时状态照常返回"

        class _NoCollection(_NoDim):
            def has_collection(self, name):
                return False

        monkeypatch.setattr(
            stats_module, "get_milvus_client", lambda path: _NoCollection()
        )
        status = knowledge_status(db)
        assert status["available"] is False
        assert status["reason"] == "集合不存在"

        class _Boom(_NoDim):
            def get_collection_stats(self, name):
                raise RuntimeError("boom")

        monkeypatch.setattr(
            stats_module, "get_milvus_client", lambda path: _Boom()
        )
        status = knowledge_status(db)
        assert status["available"] is False
        assert "boom" in status["reason"]
    print("✓ 维度探测 / 集合缺失 / 统计失败的状态语义")


def test_write_lock_import_failure_falls_back(monkeypatch):
    """维护模块不可用时状态查询退回无锁路径（不抛 ImportError）。"""
    import sys

    import utils.knowledge_stats as stats_module

    class _Client:
        def has_collection(self, name):
            return True

        def get_collection_stats(self, name):
            return {"row_count": 1}

        def describe_collection(self, name):
            return {"fields": []}

    monkeypatch.setattr(stats_module, "get_milvus_client", lambda path: _Client())
    monkeypatch.setitem(sys.modules, "utils.knowledge_admin", None)
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "milvus_lite.db"
        db.write_bytes(b"x")
        status = knowledge_status(db)
    assert status["available"] is True and status["documents"] == 1
    print("✓ 维护模块不可用时退回无锁状态查询")


def test_loaded_dimension_probe_failure(monkeypatch):
    """已加载嵌入模型维度探测失败时按未知处理（不影响状态主体）。"""
    import utils.embedding_model as embedding_module
    import utils.knowledge_stats as stats_module

    def boom():
        raise RuntimeError("model probe failed")

    monkeypatch.setattr(embedding_module, "peek_embedding_dim", boom)
    assert stats_module._loaded_embedding_dim() is None
    print("✓ 嵌入维度探测失败按未知处理")


if __name__ == "__main__":
    test_missing_database()
    for case in (
        test_dimension_mismatch_best_effort,
        test_dimension_probe_and_collection_errors,
        test_write_lock_import_failure_falls_back,
        test_loaded_dimension_probe_failure,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    status = knowledge_status()
    if status["available"]:
        print(f"✓ 本地知识库：{status['documents']} 片段")
    else:
        print(f"（本地知识库不可用：{status.get('reason')}）")
    print("\n全部通过")
