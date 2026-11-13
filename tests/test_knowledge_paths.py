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




def test_wheel_install_prefers_user_data_dir(monkeypatch, tmp_path):
    """wheel 安装：知识库跟随用户数据目录（不写安装前缀的技能目录）。"""
    skill = tmp_path / "site-skills"
    legacy = tmp_path / "legacy"
    data_root = tmp_path / "userdata"
    monkeypatch.setattr(knowledge_paths, "SKILL_KNOWLEDGE_DIR", skill)
    monkeypatch.setattr(knowledge_paths, "LEGACY_KNOWLEDGE_DIR", legacy)
    monkeypatch.setattr(knowledge_paths, "is_source_checkout", lambda: False)
    monkeypatch.setattr(knowledge_paths, "default_data_root", lambda: data_root)
    knowledge_paths.reset_default_knowledge_dir()
    try:
        expected = data_root / "rag_knowledge"
        assert knowledge_paths.default_knowledge_dir() == expected

        # 用户数据目录已有库：仍取它（优先于已存在的 skill 库）
        skill.mkdir()
        (skill / "milvus_lite.db").mkdir()
        expected.mkdir(parents=True)
        (expected / "milvus_lite.db").mkdir()
        knowledge_paths.reset_default_knowledge_dir()
        assert knowledge_paths.default_knowledge_dir() == expected
    finally:
        knowledge_paths.reset_default_knowledge_dir()
    print("✓ wheel 安装知识库跟随用户数据目录")


def test_discard_pending_library_is_ignored(monkeypatch, tmp_path):
    """初始化清理失败留下的空库（待清理标记）不能当作已初始化的来源。"""
    skill = tmp_path / "skill-marked"
    legacy = tmp_path / "legacy-marked"
    legacy.mkdir()
    (legacy / "milvus_lite.db").mkdir()
    skill.mkdir()
    (skill / "milvus_lite.db").mkdir()
    (skill / ".discard-pending").write_text("x", encoding="utf-8")
    monkeypatch.setattr(knowledge_paths, "SKILL_KNOWLEDGE_DIR", skill)
    monkeypatch.setattr(knowledge_paths, "LEGACY_KNOWLEDGE_DIR", legacy)
    knowledge_paths.reset_default_knowledge_dir()
    try:
        assert knowledge_paths.default_knowledge_dir() == legacy
        print("✓ 待清理的空知识库被来源解析忽略")
    finally:
        knowledge_paths.reset_default_knowledge_dir()


if __name__ == "__main__":
    print("monkeypatch 用例需 pytest 运行")
