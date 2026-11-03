"""
`python cli.py eval` 退出码语义测试（无需 API Key、不访问网络）

覆盖：
1. 知识库未初始化 → 跳过（退出码 0）；
2. 读取失败 / 后端故障 → 失败（退出码 1），不能当“跳过”；
3. RAG Agent 初始化失败 → 失败（退出码 1）；
4. hit@5 达标 / 未达标 / 全部未命中的输出与退出码。

运行：python tests/test_cli_eval.py
"""
import contextlib
import io
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

import cli
from agents import rag_knowledge_agent
from utils import knowledge_stats, rag_eval


def _status(available, reason=None, collection="business_travel_knowledge"):
    data = {"available": available, "collection": collection}
    if reason:
        data["reason"] = reason
    return data


def _run(monkeypatch):
    """执行评测并捕获 stdout（pytest 与脚本直跑均可）。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.run_rag_eval_standalone()
    return code, buf.getvalue()


def test_skip_when_uninitialized(monkeypatch):
    monkeypatch.setattr(
        knowledge_stats, "knowledge_status", lambda: _status(False, "知识库未初始化")
    )
    code, out = _run(monkeypatch)
    assert code == 0
    assert "跳过评测" in out
    print("✓ 未初始化 → 跳过（退出码 0）")


def test_failure_on_infra_error(monkeypatch):
    monkeypatch.setattr(
        knowledge_stats,
        "knowledge_status",
        lambda: _status(False, "Fail connecting to server on 127.0.0.1:1"),
    )
    code, out = _run(monkeypatch)
    assert code == 1
    assert "评测失败" in out
    print("✓ 后端故障 → 失败（退出码 1）")


def test_failure_when_agent_init_raises(monkeypatch):
    monkeypatch.setattr(knowledge_stats, "knowledge_status", lambda: _status(True))

    class _BoomAgent:
        def __init__(self, **kwargs):
            raise RuntimeError("嵌入模型缺失")

    monkeypatch.setattr(rag_knowledge_agent, "RAGKnowledgeAgent", _BoomAgent)
    code, out = _run(monkeypatch)
    assert code == 1
    assert "无法初始化 RAG Agent" in out
    print("✓ RAG Agent 初始化失败 → 失败（退出码 1）")


class _FakeAgent:
    initialized = True

    def search_knowledge(self, query, top_k=5):
        return [{"content": "命中"}]


def _fake_result(hit5):
    return {
        "total": 2,
        "hit1": hit5,
        "hit3": hit5,
        "hit5": hit5,
        "mrr": hit5,
        "rows": [
            {"query": "问题一", "rank": 1 if hit5 else None},
            {"query": "问题二", "rank": None},
        ],
    }


def test_pass_and_fail_thresholds(monkeypatch):
    monkeypatch.setattr(knowledge_stats, "knowledge_status", lambda: _status(True))
    monkeypatch.setattr(rag_knowledge_agent, "RAGKnowledgeAgent", lambda **kw: _FakeAgent())

    monkeypatch.setattr(rag_eval, "evaluate_retrieval", lambda search: _fake_result(1.0))
    code, out = _run(monkeypatch)
    assert code == 0
    assert "hit@5 100%" in out

    monkeypatch.setattr(rag_eval, "evaluate_retrieval", lambda search: _fake_result(0.5))
    code, out = _run(monkeypatch)
    assert code == 1
    assert "低于阈值" in out
    print("✓ 达标 / 未达标退出码正确")


def test_hint_when_all_miss(monkeypatch):
    monkeypatch.setattr(knowledge_stats, "knowledge_status", lambda: _status(True))
    monkeypatch.setattr(rag_knowledge_agent, "RAGKnowledgeAgent", lambda **kw: _FakeAgent())
    monkeypatch.setattr(rag_eval, "evaluate_retrieval", lambda search: _fake_result(0.0))
    code, out = _run(monkeypatch)
    assert code == 1
    assert "索引未加载" in out, "全部未命中时应提示可能是索引故障，而不是切分策略"
    print("✓ 全部未命中时提示索引故障")


if __name__ == "__main__":
    import pytest

    for case in (
        test_skip_when_uninitialized,
        test_failure_on_infra_error,
        test_failure_when_agent_init_raises,
        test_pass_and_fail_thresholds,
        test_hint_when_all_miss,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    print("\n全部通过")
