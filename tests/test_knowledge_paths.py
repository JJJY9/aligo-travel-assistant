"""knowledge_paths 默认目录解析测试（无需 API Key、不访问网络）。

运行：python tests/test_knowledge_paths.py
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils import knowledge_paths


def test_default_knowledge_dir_prefers_skill_then_legacy(monkeypatch, tmp_path):
    """Skill 数据目录优先；都不存在时回退 Skill 目录；legacy 有库时取 legacy。"""
    skill = tmp_path / "skill-rag"
    legacy = tmp_path / "legacy-rag"
    monkeypatch.setattr(knowledge_paths, "SKILL_KNOWLEDGE_DIR", skill)
    monkeypatch.setattr(knowledge_paths, "LEGACY_KNOWLEDGE_DIR", legacy)
    knowledge_paths.reset_default_knowledge_dir()

    # 两处都没有库文件：回退 Skill 数据目录（doctor 会提示先初始化）
    assert knowledge_paths.default_knowledge_dir() == skill

    # 只有项目根的兼容路径有库：返回 legacy
    legacy.mkdir()
    (legacy / "milvus_lite.db").mkdir()
    assert knowledge_paths.default_knowledge_dir() == legacy

    # 两处都有：Skill 数据目录优先（需先丢弃上一阶段的快照）
    skill.mkdir()
    (skill / "milvus_lite.db").mkdir()
    knowledge_paths.reset_default_knowledge_dir()
    assert knowledge_paths.default_knowledge_dir() == skill
    print("✓ 默认知识库目录解析（Skill 优先 / legacy 回退）")
    knowledge_paths.reset_default_knowledge_dir()


def test_default_dir_is_pinned_for_process(monkeypatch, tmp_path):
    """首次解析后固定在进程内：运行期新建库不会让面板与已缓存智能体分裂。"""
    skill = tmp_path / "skill-pin"
    legacy = tmp_path / "legacy-pin"
    legacy.mkdir()
    (legacy / "milvus_lite.db").mkdir()
    monkeypatch.setattr(knowledge_paths, "SKILL_KNOWLEDGE_DIR", skill)
    monkeypatch.setattr(knowledge_paths, "LEGACY_KNOWLEDGE_DIR", legacy)
    knowledge_paths.reset_default_knowledge_dir()
    try:
        assert knowledge_paths.default_knowledge_dir() == legacy
        # 服务运行期间新建 skill 库：同一进程仍使用启动时解析的库
        skill.mkdir()
        (skill / "milvus_lite.db").mkdir()
        assert knowledge_paths.default_knowledge_dir() == legacy
        # 显式重置后才切换（等价于重启进程）
        knowledge_paths.reset_default_knowledge_dir()
        assert knowledge_paths.default_knowledge_dir() == skill
        print("✓ 默认知识库目录进程内固定与显式重置")
    finally:
        knowledge_paths.reset_default_knowledge_dir()


def test_knowledge_db_path_explicit_directory(tmp_path):
    """显式目录（str / Path）直接拼接 milvus_lite.db，不触发默认解析。"""
    assert knowledge_paths.knowledge_db_path(str(tmp_path)) == (
        tmp_path / "milvus_lite.db"
    )
    assert knowledge_paths.knowledge_db_path(tmp_path) == (
        tmp_path / "milvus_lite.db"
    )
    print("✓ 显式目录的库路径拼接")


if __name__ == "__main__":
    print("monkeypatch 用例需 pytest 运行")
