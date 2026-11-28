"""
打包清单测试（不实际构建，秒级）

确保 MANIFEST.in 不会把运行时数据（本地 Milvus 知识库 / 模型 / 记忆）打进 sdist，
同时收全脚本式测试依赖的非 .py 夹具。
"""
import os
import re
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


def test_manifest_includes_js_harness():
    text = open(os.path.join(project_root, "MANIFEST.in"), encoding="utf-8").read()
    # tests/test_app_js_behaviors.py 用 node 执行该夹具：sdist 漏打包会让
    # 解包后跑 pytest 在装有 node 的机器上确定性失败（无 node 时才 skip）
    assert re.search(
        r"(?m)^\s*(?:recursive-include\s+tests\b[^\n]*\*\.js|include\s+tests/app_js_harness\.js)\s*$",
        text,
    ), "MANIFEST.in 必须收录 tests/app_js_harness.js"
    print("✓ MANIFEST.in 收录 node 测试夹具")


if __name__ == "__main__":
    import _stdio
    import _tmp_registry

    _stdio.ensure_utf8()
    _tmp_registry.install()
    test_manifest_prunes_runtime_data()
    test_manifest_includes_js_harness()
    print("\n全部通过")
