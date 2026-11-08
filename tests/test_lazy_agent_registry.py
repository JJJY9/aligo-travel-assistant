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
