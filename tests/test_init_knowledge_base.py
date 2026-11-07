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


class _RebuildAgent:
    """成功路径的假 Agent：集合不存在，写入与自检成功。"""

    def __init__(self, **kwargs):
        self.initialized = True
        self.milvus_client = _FakeMilvus()
        self.collection_name = "business_travel_knowledge"
        self.embedding_dim = 8
        self.closed = False

    def add_documents(self, documents):
        return {
            "status": "success",
            "added_count": len(documents),
            "total_count": len(documents),
        }

    def get_stats(self):
        return {"status": "success", "collection_name": self.collection_name}

    def search_knowledge(self, query, top_k=None, strict=False):
        return [{"metadata": {"title": "t"}, "distance": 0.9}]

    def close(self):
        self.closed = True


def test_empty_documents_fail_before_agent(monkeypatch):
    """文档目录为空：在构造 Agent 之前退出 1，不留下空集合。"""
    module = _load_init_module()
    built = {"n": 0}

    class FakeAgent:
        def __init__(self, **kwargs):
            built["n"] += 1
            self.initialized = True

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "skill"
        (root / "data" / "documents").mkdir(parents=True)
        (root / "script").mkdir()
        module.current_dir = root / "script"
        monkeypatch.setattr(module, "create_chat_model", lambda: None)
        monkeypatch.setattr(module, "RAGKnowledgeAgent", FakeAgent)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = module.main()
        assert code == 1 and built["n"] == 0
        assert "已中止" in out.getvalue() or "未加载到任何文档" in out.getvalue()
    print("✓ 空文档目录在构造 Agent 前退出")


def test_load_failure_aborts_before_agent(monkeypatch):
    """单个文档加载失败：中止且不触碰知识库。"""
    module = _load_init_module()
    built = {"n": 0}

    class FakeAgent:
        def __init__(self, **kwargs):
            built["n"] += 1
            self.initialized = True

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "skill"
        documents = root / "data" / "documents"
        documents.mkdir(parents=True)
        (root / "script").mkdir()
        (documents / "01_good.txt").write_text("正常内容", encoding="utf-8")
        (documents / "02_bad.txt").write_bytes("住宿标准".encode("gbk"))
        module.current_dir = root / "script"
        monkeypatch.setattr(module, "create_chat_model", lambda: None)
        monkeypatch.setattr(module, "RAGKnowledgeAgent", FakeAgent)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = module.main()
        assert code == 1 and built["n"] == 0, "加载失败不应构造 Agent"
        assert "已中止" in out.getvalue()
        assert "02_bad.txt" in out.getvalue()
    print("✓ 文档加载失败在触碰知识库前中止")


def test_self_check_failure_returns_1(monkeypatch):
    """自检检索全部未命中：退出码 1，不能报「初始化完成」。"""
    module = _load_init_module()

    class FakeAgent(_RebuildAgent):
        def search_knowledge(self, query, top_k=None, strict=False):
            return []

    with tempfile.TemporaryDirectory() as tmp:
        _prepare_dirs(module, tmp)
        monkeypatch.setattr(module, "create_chat_model", lambda: None)
        monkeypatch.setattr(module, "RAGKnowledgeAgent", FakeAgent)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = module.main()
        assert code == 1
        assert "自检" in out.getvalue()
    print("✓ 自检未命中返回 1")


def test_dimension_mismatch_rebuilds(monkeypatch):
    """旧集合维度不一致：先删除旧集合再用新维度重建，而不是直接退出。"""
    module = _load_init_module()
    calls = {"n": 0}
    dropped = []

    class FakeAgent(_RebuildAgent):
        def __init__(self, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError(
                    "知识库向量维度 1024 与嵌入模型 512 不一致，请重新运行 "
                    "python init_knowledge_base.py 重建知识库"
                )
            super().__init__(**kwargs)

    class _Client:
        def drop_collection(self, name):
            dropped.append(name)

    with tempfile.TemporaryDirectory() as tmp:
        _prepare_dirs(module, tmp)
        monkeypatch.setattr(module, "create_chat_model", lambda: None)
        monkeypatch.setattr(module, "RAGKnowledgeAgent", FakeAgent)
        import utils.milvus_client as milvus_module

        monkeypatch.setattr(
            milvus_module, "get_milvus_client", lambda path: _Client()
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = module.main()
        assert code == 0, out.getvalue()
        assert dropped == [module.COLLECTION_NAME]
        assert calls["n"] == 2
    print("✓ 维度不一致时删旧集合重建")


def test_multi_chunk_documents_carry_group_metadata():
    """多分片文档带 group / part / parts：整组删除与检索拼装依赖这些标记。"""
    module = _load_init_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "skill"
        documents = root / "data" / "documents"
        documents.mkdir(parents=True)
        module.current_dir = root / "script"
        long_text = "\n\n".join("第一段内容。" * 30 for _ in range(5))
        (documents / "01_long.txt").write_text(long_text, encoding="utf-8")
        (documents / "02_short.txt").write_text("短内容", encoding="utf-8")
        docs, failures = module.load_documents_from_directory(str(documents))
        assert not failures
        grouped = [d for d in docs if d["metadata"].get("group")]
        assert grouped, "多分片文档应带 group 元数据"
        assert all(
            d["metadata"].get("parts") == len(grouped) for d in grouped
        )
        assert sorted(int(d["metadata"]["part"]) for d in grouped) == list(
            range(1, len(grouped) + 1)
        )
        single = [d for d in docs if d["metadata"]["parent_doc"] == "02_short.txt"]
        assert single and "group" not in single[0]["metadata"]
    print("✓ 多分片文档带 group 元数据")

if __name__ == "__main__":
    _mp = pytest.MonkeyPatch()
    try:
        test_main_returns_1_when_agent_not_initialized(_mp)
        test_main_returns_1_when_add_documents_fails(_mp)
        test_empty_documents_fail_before_agent(_mp)
        test_load_failure_aborts_before_agent(_mp)
        test_self_check_failure_returns_1(_mp)
        test_dimension_mismatch_rebuilds(_mp)
    finally:
        _mp.undo()
    test_multi_chunk_documents_carry_group_metadata()
    print("\n全部通过")
