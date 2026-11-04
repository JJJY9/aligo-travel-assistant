"""
打包清单测试（不实际构建，秒级）

确保 MANIFEST.in 不会把运行时数据（本地 Milvus 知识库 / 模型 / 记忆）打进 sdist。
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)


def test_manifest_prunes_runtime_data():
    text = open(os.path.join(project_root, "MANIFEST.in"), encoding="utf-8").read()
    assert "prune .claude/skills/ask-question/data/rag_knowledge" in text, (
        "本地知识库（含业务文档内容）绝不能进入 sdist"
    )
    assert "prune data/models" in text
    assert "prune data/memory" in text
    assert "prune exports" in text
    print("✓ MANIFEST.in 排除知识库 / 模型 / 记忆 / 导出")


if __name__ == "__main__":
    test_manifest_prunes_runtime_data()
    print("\n全部通过")
