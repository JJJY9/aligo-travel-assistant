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
    # 目标库固定到临时技能目录：默认解析与运行时一致（真实仓库 / 用户数据
    # 目录），测试必须隔离，不能写进真实知识库
    module._resolve_knowledge_base_path = (
        lambda skill_root: Path(skill_root) / "data" / "rag_knowledge"
    )
    module._resolve_knowledge_source_dir = lambda knowledge_base_path: (
        knowledge_base_path
    )


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


def test_main_succeeds_without_chat_model(monkeypatch):
    """模型创建失败（无 API Key / 离线）时导入与自检照常成功。"""
    module = _load_init_module()

    def _missing_credentials():
        raise RuntimeError("Missing credentials")

    with tempfile.TemporaryDirectory() as tmp:
        _prepare_dirs(module, tmp)
        monkeypatch.setattr(module, "create_chat_model", _missing_credentials)
        monkeypatch.setattr(module, "RAGKnowledgeAgent", _RebuildAgent)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = module.main()
        assert code == 0, out.getvalue()
        assert "模型不可用" in out.getvalue()
        assert "知识库初始化完成" in out.getvalue()
    print("✓ 无模型也能完成导入与自检")


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
        import utils.knowledge_admin as admin_module

        # 维度重建前会先导出库内片段：单测环境没有真实知识库，导出空集
        monkeypatch.setattr(admin_module, "export_documents", lambda directory=None: [])
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

def test_preserved_semantics_by_source(monkeypatch):
    """保留片段按来源判定：受管旧分片丢弃、用户组整组保留、残缺组丢弃。"""
    module = _load_init_module()
    rows = [
        {
            "content": "旧版内置内容",
            "metadata": {"source": "商旅知识库文档", "file_path": "01.txt"},
        },
        {"content": "面板加的片段", "metadata": {"category": "住宿"}},
        {"content": "面板加的片段 ", "metadata": {"category": "住宿"}},
        {"content": "重复段", "metadata": {"group": "u1", "part": 1, "parts": 2}},
        {"content": "重复段", "metadata": {"group": "u1", "part": 2, "parts": 2}},
        {"content": "残片1", "metadata": {"group": "u2", "part": 1, "parts": 3}},
        {"content": "残片2", "metadata": {"group": "u2", "part": 2, "parts": 3}},
    ]
    import utils.knowledge_admin as admin_module

    monkeypatch.setattr(admin_module, "export_documents", lambda directory=None: rows)
    preserved, warnings = module._collect_preserved_documents(Path("/tmp/source-kb"))
    contents = [piece["content"] for piece in preserved]
    assert "旧版内置内容" not in contents, "受管旧分片必须丢弃（源文件已改动）"
    assert contents.count("面板加的片段") == 1, "单条按 strip 内容去重"
    assert contents.count("重复段") == 2, "用户组内相同内容不得合并"
    assert all("残片" not in content for content in contents)
    assert warnings and "残缺" in warnings[0]
    print("✓ 保留片段按来源判定（受管旧分片丢弃 / 组完整保留）")


def test_collect_preserved_uses_explicit_source_dir(monkeypatch):
    """来源目录必须显式传给导出：默认解析会在新库建好后翻转过去。"""
    module = _load_init_module()
    import utils.knowledge_admin as admin_module

    seen: dict = {}

    def fake_export(directory=None):
        seen["dir"] = directory
        return []

    monkeypatch.setattr(admin_module, "export_documents", fake_export)
    source = Path("/tmp/legacy-kb")
    preserved, warnings = module._collect_preserved_documents(source)
    assert preserved == [] and warnings == []
    assert seen["dir"] == source
    print("✓ 保留片段从显式来源目录导出")


def test_collect_preserved_drops_huge_parts_declaration(monkeypatch):
    """parts 声明超大（旧库脏数据）不能被 set(range) 展开，整组丢弃。"""
    module = _load_init_module()
    import utils.knowledge_admin as admin_module

    rows = [
        {
            "content": "脏分片",
            "metadata": {"group": "bad", "part": 1, "parts": 10**12},
        },
        {"content": "面板片段", "metadata": {"category": "住宿"}},
    ]
    monkeypatch.setattr(admin_module, "export_documents", lambda directory=None: rows)
    preserved, warnings = module._collect_preserved_documents(Path("/tmp/source"))
    contents = [piece["content"] for piece in preserved]
    assert "脏分片" not in contents
    assert "面板片段" in contents
    assert warnings and "残缺" in warnings[0]
    print("✓ 超大 parts 声明整组丢弃，不展开 range")


def test_collect_preserved_missing_db_is_empty(monkeypatch):
    """来源库尚未初始化（无 milvus_lite.db）：没有可保留内容，不算失败。"""
    module = _load_init_module()
    import utils.knowledge_admin as admin_module

    def missing(directory=None):
        raise FileNotFoundError("知识库未初始化")

    monkeypatch.setattr(admin_module, "export_documents", missing)
    preserved, warnings = module._collect_preserved_documents(Path("/tmp/missing"))
    assert preserved == [] and warnings == []
    print("✓ 来源库不存在时按空集处理")


def test_embedding_dim_unreadable_does_not_drop_collection(monkeypatch):
    """「无法读取嵌入模型维度」不是维度不一致：不得删集合再重试。"""
    module = _load_init_module()
    dropped = []

    class FakeAgent:
        def __init__(self, **kwargs):
            raise RuntimeError("无法读取嵌入模型维度")

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
        assert code == 1
        assert dropped == [], "模型维度探测失败不得删除知识库集合"
        assert "知识库保持原样" in out.getvalue()
    print("✓ 嵌入模型维度不可读时中止且不删库")


def test_export_failure_aborts_before_drop(monkeypatch):
    """重建前导出失败必须中止：不得先删集合（面板/CLI 知识会丢失）。"""
    module = _load_init_module()
    calls = {"n": 0}
    dropped = []

    class FakeAgent(_RebuildAgent):
        def __init__(self, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("知识库向量维度 1024 与嵌入模型 512 不一致")
            super().__init__(**kwargs)

    class _Client:
        def drop_collection(self, name):
            dropped.append(name)

    with tempfile.TemporaryDirectory() as tmp:
        _prepare_dirs(module, tmp)
        monkeypatch.setattr(module, "create_chat_model", lambda: None)
        monkeypatch.setattr(module, "RAGKnowledgeAgent", FakeAgent)
        import utils.knowledge_admin as admin_module
        import utils.milvus_client as milvus_module

        def boom(directory=None):
            raise RuntimeError("db locked")

        monkeypatch.setattr(admin_module, "export_documents", boom)
        monkeypatch.setattr(
            milvus_module, "get_milvus_client", lambda path: _Client()
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = module.main()
        assert code == 1
        assert dropped == [], "导出失败时不得删除集合"
        assert calls["n"] == 1
        assert not (Path(tmp) / "skill" / "data" / "rag_knowledge").exists(), (
            "中止后不能留下新建的空 skill 库：下次运行会把它当来源，"
            "旧部署 rag_knowledge/ 的知识永远不再迁移"
        )
    print("✓ 导出失败先于删库中止")


def test_add_in_batches_retries_and_reports(monkeypatch):
    """分批补写：单批失败重试一次；恒失败报出批号与已补数量。"""
    module = _load_init_module()
    monkeypatch.setattr(module, "_PRESERVED_BATCH", 2)

    class Flaky:
        def __init__(self):
            self.calls = 0

        def add_documents(self, batch):
            self.calls += 1
            if self.calls == 1:
                return {"status": "error", "message": "embed down"}
            return {"status": "success"}

    flaky = Flaky()
    rows = [{"content": f"c{index}"} for index in range(5)]
    module._add_in_batches(flaky, rows, "补写")
    assert flaky.calls == 4, "首批失败一次、重试一次，共 3 批"

    class Broken:
        def add_documents(self, batch):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="第 1 批"):
        module._add_in_batches(Broken(), rows, "补写")
    print("✓ 分批补写重试与失败进度")

def test_batch_by_group_keeps_groups_together():
    """批大小达到上限时只在组边界切批：多分片文档整组进同一批。"""
    module = _load_init_module()
    singles = [{"content": f"s{i}", "metadata": {}} for i in range(200)]
    group = [
        {"content": f"g{i}", "metadata": {"group": "G", "part": i + 1, "parts": 3}}
        for i in range(3)
    ]
    batches = module._batch_by_group(singles + group, 200)
    assert [len(batch) for batch in batches] == [200, 3], batches
    assert all(
        batch["metadata"].get("group") == "G" for batch in batches[1]
    ), "组不能跨批"
    print("✓ 分批按组边界切分")


def test_preserved_backup_helpers_roundtrip(monkeypatch):
    """备份写入/并入/损坏容错：保留片段落盘后可原样读回并去重。"""
    module = _load_init_module()
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "data" / "rag_knowledge"
        base.mkdir(parents=True)
        docs = [
            {"content": "面板片段", "metadata": {"category": "住宿"}},
            {
                "content": "分片",
                "metadata": {"group": "G", "part": 1, "parts": 1},
            },
        ]
        path = module._write_preserved_backup(base, docs)
        assert path is not None and path.exists()
        merged, notes = module._merge_preserved_backup(
            base, [{"content": "面板片段 ", "metadata": {}}]
        )
        assert notes and "已并入" in notes[0]
        contents = [doc["content"].strip() for doc in merged]
        assert contents.count("面板片段") == 1, "单条按 strip 内容去重"
        assert "分片" in contents

        path.write_text("not-json", encoding="utf-8")
        merged, notes = module._merge_preserved_backup(base, docs)
        assert notes and "不可读" in notes[0]
        assert merged == docs
    print("✓ 保留片段备份写读与容错")


def test_preserved_backup_survives_failed_restore(monkeypatch):
    """写入失败时保留片段已落盘；重跑自动并入补写，成功后清理备份。"""
    module = _load_init_module()
    import utils.knowledge_admin as admin_module

    monkeypatch.setattr(
        admin_module,
        "export_documents",
        lambda directory=None: [
            {"content": "面板加的片段", "metadata": {"category": "住宿"}}
        ],
    )

    class _ExistingMilvus(_FakeMilvus):
        def __init__(self):
            self.dropped = []

        def has_collection(self, name):
            return True

        def drop_collection(self, name):
            self.dropped.append(name)

        def create_collection(self, **kwargs):
            pass

    class _FailingRestoreAgent(_RebuildAgent):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.milvus_client = _ExistingMilvus()

        def add_documents(self, documents):
            if any(
                str(doc.get("content") or "").startswith("面板加的片段")
                for doc in documents
            ):
                return {"status": "error", "message": "Milvus 故障"}
            return super().add_documents(documents)

    class _SuccessRestoreAgent(_RebuildAgent):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.milvus_client = _ExistingMilvus()

    with tempfile.TemporaryDirectory() as tmp:
        _prepare_dirs(module, tmp)
        monkeypatch.setattr(module, "create_chat_model", lambda: None)
        backup = module._preserved_backup_path(
            module.current_dir.parent / "data" / "rag_knowledge"
        )
        monkeypatch.setattr(module, "RAGKnowledgeAgent", _FailingRestoreAgent)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = module.main()
        assert code == 1, out.getvalue()
        assert backup.exists(), "补写失败后保留片段必须留盘"
        assert str(backup) in out.getvalue()

        monkeypatch.setattr(module, "RAGKnowledgeAgent", _SuccessRestoreAgent)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = module.main()
        assert code == 0, out.getvalue()
        assert "已并入上次" in out.getvalue()
        assert not backup.exists(), "补写成功后应清理备份"
    print("✓ 保留片段备份：失败留盘、重跑补齐、成功后清理")


def test_target_follows_runtime_when_skills_dir_overridden(monkeypatch, tmp_path):
    """ALIGO_SKILLS_DIR 覆盖时目标库跟随运行时解析（脚本目录应用看不到）。"""
    module = _load_init_module()
    runtime = tmp_path / "runtime-rag"
    monkeypatch.setenv("ALIGO_SKILLS_DIR", str(tmp_path / "external-skills"))
    import utils.knowledge_paths as knowledge_paths
    import utils.paths as paths

    monkeypatch.setattr(knowledge_paths, "default_knowledge_dir", lambda: runtime)
    monkeypatch.setattr(paths, "is_source_checkout", lambda: True)
    skill_root = tmp_path / "repo-skill"
    target = module._resolve_knowledge_base_path(skill_root)
    assert target == runtime, target
    # 来源库同样跟随运行时（legacy 迁移来源与目标一致）
    assert module._resolve_knowledge_source_dir(target) == runtime

    # 未覆盖技能目录的纯源码 checkout：保持脚本本地目录（旧迁移语义）
    monkeypatch.delenv("ALIGO_SKILLS_DIR", raising=False)
    local = skill_root / "data" / "rag_knowledge"
    assert module._resolve_knowledge_base_path(skill_root) == local
    print("✓ 初始化目标库跟随运行时解析（技能目录覆盖场景）")


if __name__ == "__main__":
    _mp = pytest.MonkeyPatch()
    try:
        test_main_returns_1_when_agent_not_initialized(_mp)
        test_main_returns_1_when_add_documents_fails(_mp)
        test_main_succeeds_without_chat_model(_mp)
        test_empty_documents_fail_before_agent(_mp)
        test_load_failure_aborts_before_agent(_mp)
        test_self_check_failure_returns_1(_mp)
        test_dimension_mismatch_rebuilds(_mp)
        test_preserved_semantics_by_source(_mp)
        test_export_failure_aborts_before_drop(_mp)
        test_preserved_backup_helpers_roundtrip(_mp)
        test_preserved_backup_survives_failed_restore(_mp)
    finally:
        _mp.undo()
    test_multi_chunk_documents_carry_group_metadata()
    test_batch_by_group_keeps_groups_together()
    test_add_in_batches_retries_and_reports(pytest.MonkeyPatch())
    print("\n全部通过")
