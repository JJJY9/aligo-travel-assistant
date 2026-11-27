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


def test_dispatchable_only_excludes_doc_only_skills():
    """只列出含 script/agent.py 的可调度技能：纯文档技能不会被宣传给模型。"""
    root = tempfile.mkdtemp(prefix="chailv_skills_dispatch_")
    _write(
        os.path.join(root, "with-agent", "SKILL.md"),
        "---\nname: with-agent\ndescription: 可执行技能\n---\n",
    )
    _write(os.path.join(root, "with-agent", "script", "agent.py"), "")
    _write(
        os.path.join(root, "docs-only", "SKILL.md"),
        "---\nname: docs-only\ndescription: 纯文档技能\n---\n",
    )
    loader = SkillLoader(root, dispatchable_only=True)
    skills = loader.load_skills()
    assert set(skills) == {"with-agent"}, skills
    # 默认（不限制）仍列出纯文档技能，供技能内容注入等其它用途使用
    assert set(SkillLoader(root).load_skills()) == {"with-agent", "docs-only"}
    print("✓ 只列出可调度技能（纯文档技能被排除）")


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


def test_frontmatter_description_with_dashes():
    """描述值里出现 --- 时仍按整段 YAML 解析，残余 frontmatter 不能混进正文。"""
    root = tempfile.mkdtemp(prefix="chailv_skills_")
    _write(
        os.path.join(root, "dash", "SKILL.md"),
        "---\nname: dash-skill\ndescription: 含 --- 描述的技能\n---\n正文A\n---\n正文B",
    )
    loader = SkillLoader(root)
    skills = loader.load_skills()
    assert "dash-skill" in skills
    assert skills["dash-skill"]["description"] == "含 --- 描述的技能"
    assert loader.get_skill_content("dash") == "正文A\n---\n正文B"
    print("✓ 描述含 --- 时 frontmatter 正确解析")


def test_indented_fence_inside_block_scalar_not_terminator():
    """YAML 块标量内部缩进的 --- 不是结束标记：描述完整、正文不混入残余 frontmatter。"""
    root = tempfile.mkdtemp(prefix="chailv_skills_")
    _write(
        os.path.join(root, "block", "SKILL.md"),
        "---\nname: block-skill\ndescription: |\n  第一行\n  ---\n  第三行\n---\n正文",
    )
    loader = SkillLoader(root)
    skills = loader.load_skills()
    assert "block-skill" in skills, "技能不能被跳过"
    description = skills["block-skill"]["description"]
    assert "第一行" in description and "第三行" in description, description
    assert loader.get_skill_content("block") == "正文"
    print("✓ 块标量内缩进的 --- 不截断 frontmatter")


def test_bom_skill_md_is_loaded_not_skipped():
    """带 UTF-8 BOM 的 SKILL.md 不能被静默跳过，正文不能带 BOM。"""
    root = tempfile.mkdtemp(prefix="chailv_skills_")
    os.makedirs(os.path.join(root, "bom"))
    with open(
        os.path.join(root, "bom", "SKILL.md"), "w", encoding="utf-8-sig"
    ) as handle:
        handle.write("---\nname: bom-skill\ndescription: BOM 技能\n---\nBOM 正文")
    loader = SkillLoader(root)
    skills = loader.load_skills()
    assert "bom-skill" in skills
    content = loader.get_skill_content("bom")
    assert content == "BOM 正文"
    assert not content.startswith("﻿")
    print("✓ 带 BOM 的技能正常加载且正文无 BOM")


def test_unreadable_skills_dir_returns_empty(monkeypatch):
    """目录不可读（PermissionError）时返回空而不是崩溃。"""
    loader = SkillLoader(tempfile.mkdtemp(prefix="chailv_skills_"))

    def denied(path):
        raise PermissionError("denied")

    monkeypatch.setattr(os, "listdir", denied)
    assert loader.load_skills() == {}
    assert loader.get_skill_content("anything") is None
    print("✓ 技能目录不可读时返回空")


def test_frontmatter_bounds_edge_cases():
    """只有开头标记 / 结束标记在文件末尾（无换行）的边界。"""
    from utils.skill_loader import _frontmatter_bounds, _strip_frontmatter

    assert _frontmatter_bounds("---") is None, "只有开头标记没有换行"
    assert _strip_frontmatter("---\nname: x\n---") == "", "结束标记后没有换行"
    print("✓ frontmatter 边界（无换行 / 结尾标记在 EOF）")


def test_read_errors_return_none(monkeypatch):
    """SKILL.md / 正文读取失败返回 None，不让整个加载崩溃。"""
    root = tempfile.mkdtemp(prefix="chailv_skills_")
    loader = SkillLoader(os.path.join(root, "skills"))
    assert loader._parse_skill_md(os.path.join(root, "missing", "SKILL.md")) is None

    skill_dir = os.path.join(root, "skills", "broken")
    target = os.path.join(skill_dir, "SKILL.md")
    _write(target, "---\nname: broken\n---\n正文")

    real_open = open

    def denied(path, *args, **kwargs):
        if str(path) == target:
            raise PermissionError("denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", denied)
    assert loader.get_skill_content("broken") is None
    print("✓ SKILL.md / 正文读取失败返回 None")


def test_leading_four_dash_line_is_not_frontmatter_opener():
    """首行必须是精确的 ---：'----' 分隔线不能把后续正文解析成 frontmatter。"""
    from utils.skill_loader import _frontmatter_bounds

    root = tempfile.mkdtemp(prefix="chailv_skills_")
    _write(
        os.path.join(root, "weird", "SKILL.md"),
        "----\nname: fake\ndescription: fake desc\n---\n真正正文",
    )
    with open(os.path.join(root, "weird", "SKILL.md"), encoding="utf-8") as handle:
        text = handle.read()
    assert _frontmatter_bounds(text) is None, "首行不是精确 --- 不能当围栏"
    loader = SkillLoader(root)
    assert loader.load_skills() == {}, "伪技能不能被登记"
    content = loader.get_skill_content("weird")
    assert content is not None, content
    assert "----" in content and "name: fake" in content, "正文不能被截断"

    # CRLF 正例：首行 ---\r 经 rstrip 后仍是合法围栏
    _write(
        os.path.join(root, "crlf", "SKILL.md"),
        "---\r\nname: ok\r\ndescription: ok\r\n---\r\n正文\r\n",
    )
    assert "ok" in SkillLoader(root).load_skills()
    print("✓ 首行围栏必须精确 ---（CRLF 仍兼容）")


def test_duplicate_frontmatter_name_is_consistent():
    """同名技能的「宣传」与「注入」必须解析到同一份正文。"""
    root = tempfile.mkdtemp(prefix="chailv_skills_")
    _write(
        os.path.join(root, "dir-a", "SKILL.md"),
        "---\nname: dup\ndescription: 描述A\n---\n正文A",
    )
    _write(
        os.path.join(root, "dir-b", "SKILL.md"),
        "---\nname: dup\ndescription: 描述B\n---\n正文B",
    )
    loader = SkillLoader(root)
    skills = loader.load_skills()
    # 排序后保留首个：目录 dir-a
    assert skills["dup"]["description"] == "描述A", skills
    prompt = loader.get_skill_prompt()
    assert "描述A" in prompt and "描述B" not in prompt, prompt
    assert loader.get_skill_content("dup") == "正文A", "注入必须与宣传同源"
    print("✓ 重名技能宣传与注入同源（保留排序首个）")


def test_skill_name_is_stripped():
    """frontmatter name 带首尾空白时归一，意图侧与注册表解析一致。"""
    root = tempfile.mkdtemp(prefix="chailv_skills_")
    _write(
        os.path.join(root, "custom", "SKILL.md"),
        '---\nname: "  my custom  "\ndescription: 带空白\n---\n正文C',
    )
    loader = SkillLoader(root)
    skills = loader.load_skills()
    assert "my custom" in skills, skills
    assert "  my custom  " not in skills
    assert loader.get_skill_content("my custom") == "正文C"
    print("✓ frontmatter name 去首尾空白")


def test_get_skill_prompt_budget():
    """description 单条与总量都有上限，且技能名不能被整条省略。"""
    root = tempfile.mkdtemp(prefix="chailv_skills_")
    _write(
        os.path.join(root, "long", "SKILL.md"),
        "---\nname: long\ndescription: |\n  " + "很长的描述" * 600 + "\n---\n",
    )
    _write(os.path.join(root, "long", "script", "agent.py"), "")
    for index in range(8):
        _write(
            os.path.join(root, f"skill-{index}", "SKILL.md"),
            f"---\nname: skill-{index}\ndescription: " + "描述" * 300 + "\n---\n",
        )
        _write(os.path.join(root, f"skill-{index}", "script", "agent.py"), "")
    loader = SkillLoader(root, dispatchable_only=True)
    prompt = loader.get_skill_prompt()
    long_line = next(line for line in prompt.splitlines() if line.startswith("1. long -"))
    assert long_line.endswith("…"), long_line
    assert len(long_line) <= 500 + 16, len(long_line)
    # 总量超预算后只省略描述，所有技能名仍然在清单里
    for index in range(8):
        assert f"skill-{index}" in prompt, prompt
    assert "（描述已省略）" in prompt, prompt
    assert len(prompt) <= 4000 + 9 * 40, len(prompt)
    print("✓ 技能 description 单条 / 总量预算")


def test_get_skill_content_is_capped():
    """超长技能正文截断并注明，避免每轮调用撑爆模型上下文。"""
    from utils.skill_loader import MAX_SKILL_CONTENT_CHARS

    root = tempfile.mkdtemp(prefix="chailv_skills_")
    _write(
        os.path.join(root, "big", "SKILL.md"),
        "---\nname: big\n---\n" + "长正文" * (MAX_SKILL_CONTENT_CHARS // 3 + 100),
    )
    loader = SkillLoader(root)
    content = loader.get_skill_content("big")
    assert content is not None
    assert content.endswith("（技能正文过长已截断）"), content[-20:]
    assert len(content) <= MAX_SKILL_CONTENT_CHARS + 20, len(content)
    print("✓ 技能正文注入上限")


def test_repo_skill_docs_use_valid_json_and_model_factory():
    """真实 SKILL.md：示例必须是合法 JSON（不能残留 f-string 转义的 {{ }}），
    且不再示范直接构造 OpenAIChatModel（顶层参数会被静默忽略）。"""
    import json

    loader = SkillLoader()
    for skill in ("plan-trip", "preference"):
        body = loader.get_skill_content(skill) or ""
        _, _, rest = body.partition("【输出格式】(严格JSON)")
        assert rest, skill
        assert "{{" not in rest, skill
        # 段首即 JSON 示例：按 raw_decode 解析前缀（后续「重要规则」里还有
        # 内联示例，不能取到段末的 } 再整体解析）
        parsed, _ = json.JSONDecoder().raw_decode(rest[rest.index("{"):])
        assert isinstance(parsed, dict) and parsed, skill
    query_info = loader.get_skill_content("query-info") or ""
    assert "OpenAIChatModel(" not in query_info
    assert "create_chat_model" in query_info
    print("✓ 仓库 SKILL.md 示例为合法 JSON / 统一模型工厂")


def test_load_skills_is_idempotent():
    """重复扫描不应把自身上次登记的名字当成重名冲突（结果保持一致）。"""
    loader = SkillLoader(_make_skills_dir())
    first = dict(loader.load_skills())
    second = loader.load_skills()
    assert second == first, second
    # 再取正文 / 提示词也不受二次扫描影响
    assert loader.get_skill_content("good") == "正文"
    assert "1. good-skill - 正常技能" in loader.get_skill_prompt()
    print("✓ 技能扫描幂等")


_DIRECT_RUN_SKIP: dict[str, str] = {
}

if __name__ == "__main__":
    import _stdio
    import _tmp_registry

    _stdio.ensure_utf8()
    _tmp_registry.install()
    test_load_skills_tolerates_malformed_entries()
    test_get_skill_prompt_sorted_and_mapped()
    test_missing_skills_dir_returns_empty()
    test_get_skill_content_by_dir_and_metadata_name()
    test_get_skill_content_without_frontmatter()
    test_get_skill_content_missing_dir_returns_none()
    test_frontmatter_description_with_dashes()
    test_indented_fence_inside_block_scalar_not_terminator()
    test_bom_skill_md_is_loaded_not_skipped()
    _mp = __import__("pytest").MonkeyPatch()
    try:
        test_unreadable_skills_dir_returns_empty(_mp)
    finally:
        _mp.undo()
    test_frontmatter_bounds_edge_cases()
    _mp = __import__("pytest").MonkeyPatch()
    try:
        test_read_errors_return_none(_mp)
    finally:
        _mp.undo()
    test_dispatchable_only_excludes_doc_only_skills()
    test_leading_four_dash_line_is_not_frontmatter_opener()
    test_duplicate_frontmatter_name_is_consistent()
    test_skill_name_is_stripped()
    test_get_skill_prompt_budget()
    test_get_skill_content_is_capped()
    test_repo_skill_docs_use_valid_json_and_model_factory()
    test_load_skills_is_idempotent()
    print("\n全部通过")
