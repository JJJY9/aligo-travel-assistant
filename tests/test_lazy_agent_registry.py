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


if __name__ == "__main__":
    for case in (
        test_discovers_and_caches_agents,
        test_unknown_agent,
        test_import_failure_propagates,
        test_missing_agent_class,
        test_keys_include_legacy_names,
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
