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


def test_get_skill_content_by_dir_and_metadata_name():
    """get_skill_content：目录名直配、metadata name 回退、缺失返回 None。"""
    loader = SkillLoader(_make_skills_dir())
    assert loader.get_skill_content("good") == "正文"

    root = _make_skills_dir()
    _write(
        os.path.join(root, "folder-b", "SKILL.md"),
        "---\nname: custom-name\ndescription: 目录名与 name 不同\n---\n自定义正文",
    )
    loader2 = SkillLoader(root)
    assert loader2.get_skill_content("custom-name") == "自定义正文"
    assert loader.get_skill_content("missing-skill") is None
    print("✓ 技能正文读取（目录名 / metadata name 回退 / 缺失）")


def test_get_skill_content_without_frontmatter():
    """无 frontmatter / frontmatter 未闭合时原样返回正文。"""
    root = tempfile.mkdtemp(prefix="chailv_skills_")
    _write(os.path.join(root, "plain", "SKILL.md"), "没有 frontmatter 的正文")
    _write(os.path.join(root, "unclosed", "SKILL.md"), "---\n没有结束标记")
    loader = SkillLoader(root)
    assert loader.get_skill_content("plain") == "没有 frontmatter 的正文"
    assert loader.get_skill_content("unclosed") == "---\n没有结束标记"
    print("✓ 无 frontmatter / 未闭合 frontmatter 的正文读取")


def test_get_skill_content_missing_dir_returns_none():
    """技能目录不存在时按名回退不能抛 FileNotFoundError。"""
    loader = SkillLoader(os.path.join(tempfile.mkdtemp(), "missing"))
    assert loader.get_skill_content("anything") is None
    print("✓ 技能目录缺失时按名查找返回 None")


if __name__ == "__main__":
    test_load_skills_tolerates_malformed_entries()
    test_get_skill_prompt_sorted_and_mapped()
    test_missing_skills_dir_returns_empty()
    test_get_skill_content_by_dir_and_metadata_name()
    test_get_skill_content_without_frontmatter()
    test_get_skill_content_missing_dir_returns_none()
    print("\n全部通过")
