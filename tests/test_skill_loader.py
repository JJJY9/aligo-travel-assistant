"""SkillLoader 测试：frontmatter 解析与容错（无需 API Key、不访问网络）。

运行：python tests/test_skill_loader.py
"""
import os
import sys
import tempfile

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.skill_loader import SkillLoader


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _make_skills_dir() -> str:
    root = tempfile.mkdtemp(prefix="chailv_skills_")
    _write(
        os.path.join(root, "good", "SKILL.md"),
        "---\nname: good-skill\ndescription: 正常技能\n---\n正文",
    )
    _write(os.path.join(root, "no-md", "README.md"), "没有 SKILL.md")
    _write(os.path.join(root, "bad-yaml", "SKILL.md"), "---\nname: [未闭合\n---\n")
    _write(os.path.join(root, "not-mapping", "SKILL.md"), "---\n就是一段字符串\n---\n")
    _write(
        os.path.join(root, "list-name", "SKILL.md"),
        "---\nname:\n  - a\n  - b\ndescription: 名字是列表\n---\n",
    )
    _write(os.path.join(root, "no-desc", "SKILL.md"), "---\nname: no-desc\n---\n")
    _write(os.path.join(root, "plain.txt"), "不是目录")
    return root


def test_load_skills_tolerates_malformed_entries():
    """损坏的 frontmatter 跳过而不是让整个技能目录加载失败。"""
    loader = SkillLoader(_make_skills_dir())
    skills = loader.load_skills()
    assert set(skills) == {"good-skill", "list-name", "no-desc"}
    assert skills["good-skill"]["description"] == "正常技能"
    # name 不是字符串时退回目录名做键，不能因不可哈希而崩溃
    assert skills["list-name"]["name"] == ["a", "b"]
    print("✓ 技能加载容忍损坏的 frontmatter")


def test_get_skill_prompt_sorted_and_mapped():
    """提示词按名称排序；映射生效；缺失描述不报错。"""
    loader = SkillLoader(_make_skills_dir())
    prompt = loader.get_skill_prompt({"good-skill": "itinerary_planning"})
    lines = [line for line in prompt.splitlines() if line.strip()]
    assert lines[0].startswith("1. itinerary_planning - 正常技能"), lines
    assert any("no-desc -" in line for line in lines), lines
    print("✓ 技能提示词排序与映射")


def test_missing_skills_dir_returns_empty():
    loader = SkillLoader(os.path.join(tempfile.mkdtemp(), "missing"))
    assert loader.load_skills() == {}
    print("✓ 技能目录缺失时返回空")


if __name__ == "__main__":
    test_load_skills_tolerates_malformed_entries()
    test_get_skill_prompt_sorted_and_mapped()
    test_missing_skills_dir_returns_empty()
    print("\n全部通过")
