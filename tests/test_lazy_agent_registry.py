"""
懒加载智能体注册器测试（无需 API Key、不访问网络）

覆盖：技能发现、遗留名称映射、缓存、缺失目录、导入失败、缺少 AgentBase 子类。

运行：python tests/test_lazy_agent_registry.py
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from agents import lazy_agent_registry
from agents.lazy_agent_registry import LazyAgentRegistry

_AGENT_SOURCE = '''
from agentscope.agent import AgentBase
from agentscope.message import Msg


class DemoAgent(AgentBase):
    def __init__(self, name="DemoAgent", model=None, **kwargs):
        super().__init__()
        self.name = name
        self.model = model

    async def reply(self, x=None):
        return Msg(name=self.name, content="ok", role="assistant")
'''

_BROKEN_SOURCE = "raise RuntimeError('boom at import')\n"
_NO_AGENT_SOURCE = "VALUE = 1\n"


def _write_skill(root: Path, name: str, source: str) -> None:
    script = root / name / "script"
    script.mkdir(parents=True)
    (script / "agent.py").write_text(source, encoding="utf-8")


def _registry(monkeypatch, skills: dict[str, str]) -> LazyAgentRegistry:
    root = Path(tempfile.mkdtemp())
    for name, source in skills.items():
        _write_skill(root, name, source)
    monkeypatch.setattr(lazy_agent_registry, "resolve_skills_root", lambda: root)
    return LazyAgentRegistry(model="fake-model", cache={})


_UNINITIALIZED_SOURCE = '''
from agentscope.agent import AgentBase
from agentscope.message import Msg


class BrokenInitAgent(AgentBase):
    def __init__(self, name="BrokenInitAgent", model=None, **kwargs):
        super().__init__()
        self.name = name
        self.initialized = False

    async def reply(self, x=None):
        return Msg(name=self.name, content="", role="assistant")
'''


def test_uninitialized_agents_not_cached(monkeypatch):
    """初始化失败的实例不缓存：补跑初始化后下一次调用重新加载。"""
    registry = _registry(monkeypatch, {"ask-question": _UNINITIALIZED_SOURCE})
    first = registry["rag_knowledge"]
    assert first.initialized is False
    second = registry["rag_knowledge"]
    assert second is not first, "initialized=False 的实例不能进入缓存"
    print("✓ 初始化失败的智能体不缓存")


def test_discovers_and_caches_agents(monkeypatch):
    registry = _registry(monkeypatch, {"query-info": _AGENT_SOURCE})
    assert "query-info" in registry
    assert "information_query" in registry, "旧版名称应映射到技能"

    first = registry["information_query"]
    assert first.name == "information_query"
    assert registry["information_query"] is first, "同名请求应命中缓存"
    print("✓ 技能发现 / 遗留名称映射 / 缓存")


def test_unknown_agent(monkeypatch):
    registry = _registry(monkeypatch, {})
    with pytest.raises(KeyError, match="not found"):
        registry["nope"]
    assert registry.get("nope") is None
    assert "nope" not in registry
    print("✓ 未注册智能体 KeyError / get 默认值")


def test_missing_skills_root(monkeypatch, tmp_path):
    missing = tmp_path / "missing"
    monkeypatch.setattr(lazy_agent_registry, "resolve_skills_root", lambda: missing)
    registry = LazyAgentRegistry(model="fake-model", cache={})
    assert registry.keys() == []
    print("✓ 技能目录缺失时注册器为空")


def test_import_failure_propagates(monkeypatch):
    registry = _registry(monkeypatch, {"broken": _BROKEN_SOURCE})
    with pytest.raises(RuntimeError, match="boom at import"):
        registry["broken"]
    print("✓ 技能导入失败向上抛出")


def test_missing_agent_class(monkeypatch):
    registry = _registry(monkeypatch, {"empty": _NO_AGENT_SOURCE})
    with pytest.raises(ValueError, match="No AgentBase"):
        registry["empty"]
    print("✓ 缺少 AgentBase 子类报错")


def test_keys_include_legacy_names(monkeypatch):
    registry = _registry(
        monkeypatch,
        {"query-info": _AGENT_SOURCE, "plan-trip": _AGENT_SOURCE},
    )
    keys = set(registry.keys())
    assert {"query-info", "information_query", "plan-trip", "itinerary_planning"} <= keys
    assert "rag_knowledge" not in keys, "未安装的技能不应出现在 keys"
    print("✓ keys 包含已安装技能的遗留名称")


def test_names_resolve_case_insensitively(monkeypatch):
    """模型偶尔输出 Plan-Trip / EVENT-COLLECTION：不能当成未注册。"""
    registry = _registry(
        monkeypatch,
        {"plan-trip": _AGENT_SOURCE, "event-collection": _AGENT_SOURCE},
    )
    assert registry._resolve_agent_name("Plan-Trip") == "plan-trip"
    assert registry._resolve_agent_name("EVENT-COLLECTION") == "event-collection"
    assert registry._resolve_agent_name("Itinerary_Planning") == "plan-trip", (
        "遗留标准名同样大小写不敏感"
    )
    assert "Plan-Trip" in registry
    agent = registry["PLAN-TRIP"]
    assert agent is not None
    print("✓ 智能体名称大小写不敏感解析")


def test_frontmatter_name_alias_resolves(monkeypatch):
    """意图 prompt 展示 frontmatter name：注册器必须能解析它（name≠目录名）。"""
    root = Path(tempfile.mkdtemp())
    _write_skill(root, "foo", _AGENT_SOURCE)
    (root / "foo" / "SKILL.md").write_text(
        "---\nname: Foo Travel Helper\ndescription: 自定义技能\n---\n正文",
        encoding="utf-8",
    )
    monkeypatch.setattr(lazy_agent_registry, "resolve_skills_root", lambda: root)
    registry = LazyAgentRegistry(model="fake-model", cache={})
    assert "foo" in registry
    assert "Foo Travel Helper" in registry, "frontmatter name 也要能解析"
    agent = registry["Foo Travel Helper"]
    assert getattr(agent, "name", "") == "Foo Travel Helper"


def test_setitem_and_delitem_support(monkeypatch):
    """注册 / 注销自定义智能体：与 dict 注册表同一语义，不再抛 TypeError。"""
    registry = _registry(monkeypatch, {"demo": _AGENT_SOURCE})
    marker = object()
    registry["custom"] = marker
    assert registry["custom"] is marker and "custom" in registry
    del registry["custom"]
    assert "custom" not in registry
    # 已发现但未加载的技能：注销不抛错（发现表保留）
    del registry["demo"]
    assert "demo" in registry


def test_alias_frontmatter_failure_is_ignored(monkeypatch):
    """SKILL.md 解析失败只是没有别名，不影响技能发现。"""
    from utils.skill_loader import SkillLoader

    root = Path(tempfile.mkdtemp())
    _write_skill(root, "foo", _AGENT_SOURCE)
    (root / "foo" / "SKILL.md").write_text("---\nname: X\n---\n", encoding="utf-8")

    def boom(self, path):
        raise RuntimeError("bad frontmatter")

    monkeypatch.setattr(SkillLoader, "_parse_skill_md", boom)
    monkeypatch.setattr(lazy_agent_registry, "resolve_skills_root", lambda: root)
    registry = LazyAgentRegistry(model="fake-model", cache={})
    assert "foo" in registry, "frontmatter 解析失败不能影响技能发现"
    assert registry._name_aliases == {}
    print("✓ frontmatter 解析失败不影响技能发现")


def test_alias_resolution_case_insensitive(monkeypatch):
    """frontmatter 别名同样大小写不敏感。"""
    root = Path(tempfile.mkdtemp())
    _write_skill(root, "foo", _AGENT_SOURCE)
    (root / "foo" / "SKILL.md").write_text(
        "---\nname: Foo Travel Helper\n---\n正文", encoding="utf-8"
    )
    monkeypatch.setattr(lazy_agent_registry, "resolve_skills_root", lambda: root)
    registry = LazyAgentRegistry(model="fake-model", cache={})
    assert registry._resolve_agent_name("FOO TRAVEL HELPER") == "foo"
    print("✓ frontmatter 别名大小写不敏感")


def test_spec_loader_missing_raises(monkeypatch):
    """spec_from_file_location 返回 None 时报明确错误。"""
    registry = _registry(monkeypatch, {"demo": _AGENT_SOURCE})
    monkeypatch.setattr(
        lazy_agent_registry.importlib.util,
        "spec_from_file_location",
        lambda *args, **kwargs: None,
    )
    with pytest.raises(ImportError, match="Cannot load spec"):
        registry["demo"]
    print("✓ spec 无法创建时报 ImportError")


def test_init_receives_memory_manager(monkeypatch):
    """构造参数带 memory_manager 的智能体获得注入（不带的保持原构造）。"""
    source = '''
from agentscope.agent import AgentBase
from agentscope.message import Msg


class MemoAgent(AgentBase):
    def __init__(self, name="MemoAgent", model=None, memory_manager=None, **kwargs):
        super().__init__()
        self.name = name
        self.model = model
        self.memory_manager = memory_manager

    async def reply(self, x=None):
        return Msg(name=self.name, content="ok", role="assistant")
'''
    root = Path(tempfile.mkdtemp())
    _write_skill(root, "memo", source)
    monkeypatch.setattr(lazy_agent_registry, "resolve_skills_root", lambda: root)
    marker = object()
    registry = LazyAgentRegistry(model="fake-model", cache={}, memory_manager=marker)
    agent = registry["memo"]
    assert agent.memory_manager is marker
    print("✓ 需要 memory_manager 的智能体获得注入")


def test_project_root_is_added_to_sys_path(monkeypatch):
    """从非项目根目录启动（如 wheel 安装）时，加载技能前先补 sys.path。"""
    registry = _registry(monkeypatch, {"demo": _AGENT_SOURCE})
    root = str(Path(__file__).resolve().parent.parent)
    kept = [
        entry
        for entry in sys.path
        if os.path.normcase(os.path.abspath(entry or "."))
        != os.path.normcase(os.path.abspath(root))
    ]
    monkeypatch.setattr(sys, "path", kept)
    agent = registry["demo"]
    assert agent is not None
    assert any(
        os.path.normcase(os.path.abspath(entry)) == os.path.normcase(os.path.abspath(root))
        for entry in sys.path
    ), "加载技能前必须把项目根目录补进 sys.path"
    print("✓ 加载技能前补项目根目录到 sys.path")


def test_console_output_failure_does_not_break_loading(monkeypatch):
    """控制台编码错误（GBK 重定向遇 emoji）不能让技能加载失败。"""
    registry = _registry(monkeypatch, {"demo": _AGENT_SOURCE})

    class _BrokenConsole:
        def print(self, *args, **kwargs):
            raise UnicodeEncodeError(
                "gbk", chr(0x1F504), 0, 1, "illegal multibyte sequence"
            )

    registry.console = _BrokenConsole()
    agent = registry["demo"]
    assert agent is not None
    print("✓ 控制台输出失败不影响技能加载")


def test_loaded_agent_accessors(monkeypatch):
    """values / items / get_loaded_agents 与缓存一致。"""
    registry = _registry(monkeypatch, {"demo": _AGENT_SOURCE})
    agent = registry["demo"]
    assert list(registry.values()) == [agent]
    assert dict(registry.items()) == {"demo": agent}
    assert registry.get_loaded_agents() == ["demo"]
    print("✓ values / items / get_loaded_agents")


def test_module_executed_once_across_registries(monkeypatch):
    """同一技能脚本进程内只执行一次：模块级状态不按会话复制。"""
    root = Path(tempfile.mkdtemp())
    marker = root / "exec_count.txt"
    source = (
        "from pathlib import Path\n"
        f"p = Path({str(marker)!r})\n"
        "p.write_text(str(int(p.read_text(encoding='utf-8')) + 1) "
        "if p.exists() else '1', encoding='utf-8')\n"
    ) + _AGENT_SOURCE
    _write_skill(root, "demo-skill", source)
    monkeypatch.setattr(lazy_agent_registry, "resolve_skills_root", lambda: root)
    first = LazyAgentRegistry(model="fake-model", cache={})
    second = LazyAgentRegistry(model="fake-model", cache={})
    first_agent = first["demo-skill"]
    second_agent = second["demo-skill"]
    assert marker.read_text(encoding="utf-8") == "1", "技能模块被重复执行"
    assert first_agent is not second_agent, "每个注册器仍应各自实例化"
    print("✓ 技能模块跨注册器只执行一次")


def test_agent_class_selection_ignores_imported_subclass(monkeypatch):
    """只实例化技能模块自己定义的 AgentBase 子类，不选它 import 的类。"""
    import types

    from agentscope.agent import AgentBase

    helper = types.ModuleType("fake_helper_module")

    class ImportedAgent(AgentBase):
        async def reply(self, x=None):
            return None

    helper.ImportedAgent = ImportedAgent
    monkeypatch.setitem(sys.modules, "fake_helper_module", helper)
    source = "from fake_helper_module import ImportedAgent\n" + _AGENT_SOURCE
    registry = _registry(monkeypatch, {"demo": source})
    agent = registry["demo"]
    assert type(agent).__name__ == "DemoAgent", type(agent).__name__
    print("✓ 忽略 import 进来的 AgentBase 子类")


def test_cache_is_keyed_by_skill_name(monkeypatch):
    """标准名与目录名命中同一缓存实例（注册/删除不再绕过或残留）。"""
    registry = _registry(monkeypatch, {"plan-trip": _AGENT_SOURCE})
    standard = registry["itinerary_planning"]
    directory = registry["plan-trip"]
    assert standard is directory
    assert set(registry.cache) == {"plan-trip"}
    del registry["plan-trip"]
    assert registry.cache == {}
    print("✓ 缓存按技能目录名统一")


if __name__ == "__main__":
    for case in (
        test_uninitialized_agents_not_cached,
        test_discovers_and_caches_agents,
        test_unknown_agent,
        test_import_failure_propagates,
        test_missing_agent_class,
        test_keys_include_legacy_names,
        test_names_resolve_case_insensitively,
        test_frontmatter_name_alias_resolves,
        test_setitem_and_delitem_support,
        test_alias_frontmatter_failure_is_ignored,
        test_alias_resolution_case_insensitive,
        test_spec_loader_missing_raises,
        test_init_receives_memory_manager,
        test_project_root_is_added_to_sys_path,
        test_loaded_agent_accessors,
        test_console_output_failure_does_not_break_loading,
        test_module_executed_once_across_registries,
        test_agent_class_selection_ignores_imported_subclass,
        test_cache_is_keyed_by_skill_name,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    mp = pytest.MonkeyPatch()
    tmp = Path(tempfile.mkdtemp())
    try:
        test_missing_skills_root(mp, tmp)
    finally:
        mp.undo()
    print("\n全部通过")
