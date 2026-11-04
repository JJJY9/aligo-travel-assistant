"""
`init_knowledge_base.py` 退出码测试（无需 API Key、不访问真实知识库）

覆盖：Agent 初始化失败 / 添加文档失败时 main() 返回 1，供 cron / CI 检测。

运行：python tests/test_init_knowledge_base.py
"""
import contextlib
import importlib.util
import io
import os
import sys
import tempfile
from pathlib import Path

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)


def _load_init_module():
    path = (
        Path(project_root)
        / ".claude"
        / "skills"
        / "ask-question"
        / "script"
        / "init_knowledge_base.py"
    )
    spec = importlib.util.spec_from_file_location("_test_init_knowledge_base", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeMilvus:
    def has_collection(self, name):
        return False


def _prepare_dirs(module, tmp):
    root = Path(tmp) / "skill"
    script = root / "script"
    documents = root / "data" / "documents"
    documents.mkdir(parents=True)
    script.mkdir()
    (documents / "a.txt").write_text("差旅住宿标准：一线城市不超过 500 元。", encoding="utf-8")
    module.current_dir = script


def test_main_returns_1_when_agent_not_initialized(monkeypatch):
    module = _load_init_module()

    class FakeAgent:
        def __init__(self, **kwargs):
            self.initialized = False

    with tempfile.TemporaryDirectory() as tmp:
        _prepare_dirs(module, tmp)
        monkeypatch.setattr(module, "create_chat_model", lambda: None)
        monkeypatch.setattr(module, "RAGKnowledgeAgent", FakeAgent)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = module.main()
        assert code == 1
        assert "初始化失败" in out.getvalue()
    print("✓ 初始化失败返回 1")


def test_main_returns_1_when_add_documents_fails(monkeypatch):
    module = _load_init_module()

    class FakeAgent:
        def __init__(self, **kwargs):
            self.initialized = True
            self.milvus_client = _FakeMilvus()
            self.collection_name = "business_travel_knowledge"
            self.embedding_dim = 8

        def add_documents(self, documents):
            return {"status": "error", "message": "Milvus 故障"}

    with tempfile.TemporaryDirectory() as tmp:
        _prepare_dirs(module, tmp)
        monkeypatch.setattr(module, "create_chat_model", lambda: None)
        monkeypatch.setattr(module, "RAGKnowledgeAgent", FakeAgent)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = module.main()
        assert code == 1
        assert "添加文档失败" in out.getvalue()
    print("✓ 添加文档失败返回 1")


if __name__ == "__main__":
    _mp = pytest.MonkeyPatch()
    try:
        test_main_returns_1_when_agent_not_initialized(_mp)
        test_main_returns_1_when_add_documents_fails(_mp)
    finally:
        _mp.undo()
    print("\n全部通过")
