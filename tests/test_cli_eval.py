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
import json
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

import pytest

import cli
from agents import rag_knowledge_agent
from utils import knowledge_stats, rag_eval


def _status(available, reason=None, collection="business_travel_knowledge"):
    data = {"available": available, "collection": collection}
    if reason:
        data["reason"] = reason
    return data


def _run(monkeypatch, as_json=False, min_hit5=0.8):
    """执行评测并捕获 stdout（pytest 与脚本直跑均可）。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.run_rag_eval_standalone(as_json=as_json, min_hit5=min_hit5)
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

    def search_knowledge(self, query, top_k=5, strict=False):
        return [{"content": "命中"}]


class _SearchBoomAgent:
    initialized = True

    def __init__(self, **kwargs):
        pass

    def search_knowledge(self, query, top_k=5, strict=False):
        raise ConnectionError("milvus down")


def test_failure_when_search_backend_raises(monkeypatch):
    """逐题检索故障要报后端错误，不能算成 hit@5=0% 的质量不达标。"""
    monkeypatch.setattr(knowledge_stats, "knowledge_status", lambda: _status(True))
    monkeypatch.setattr(
        rag_knowledge_agent, "RAGKnowledgeAgent", _SearchBoomAgent
    )
    code, out = _run(monkeypatch, as_json=True)
    payload = json.loads(out)
    assert code == 1
    assert payload["ok"] is False
    assert "检索后端不可用" in payload["error"]
    print("✓ 检索故障 → 失败（退出码 1）")


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


def test_min_hit5_flag_changes_threshold(monkeypatch):
    """--min-hit5 可调阈值：同一结果 0.5 达标记 0、默认 0.8 时退出 1。"""
    monkeypatch.setattr(knowledge_stats, "knowledge_status", lambda: _status(True))
    monkeypatch.setattr(
        rag_knowledge_agent, "RAGKnowledgeAgent", lambda **kw: _FakeAgent()
    )
    monkeypatch.setattr(rag_eval, "evaluate_retrieval", lambda fn: _fake_result(0.5))

    code, out = _run(monkeypatch, min_hit5=0.5)
    assert code == 0, out
    code, out = _run(monkeypatch)
    assert code == 1 and "低于阈值 hit@5 >= 80%" in out

    code, out = _run(monkeypatch, as_json=True, min_hit5=0.6)
    payload = json.loads(out)
    assert payload["threshold"] == 0.6 and payload["ok"] is False
    code, out = _run(monkeypatch, as_json=True, min_hit5=0.4)
    assert json.loads(out)["ok"] is True
    print("✓ --min-hit5 调整达标阈值（文本 / JSON）")


def test_parse_eval_args():
    assert cli._parse_eval_args([]) == (False, 0.8)
    assert cli._parse_eval_args(["--json"]) == (True, 0.8)
    assert cli._parse_eval_args(["--min-hit5", "0.5"]) == (False, 0.5)
    assert cli._parse_eval_args(["--min-hit5=1"]) == (False, 1.0)
    assert cli._parse_eval_args(["--json", "--min-hit5=0"]) == (True, 0.0)
    assert cli._parse_eval_args(["--min-hit5"]) is None
    assert cli._parse_eval_args(["--min-hit5", "1.1"]) is None
    assert cli._parse_eval_args(["--min-hit5", "-0.1"]) is None
    assert cli._parse_eval_args(["--min-hit5", "abc"]) is None
    assert cli._parse_eval_args(["--bad"]) is None
    print("✓ eval 参数解析（阈值范围 / 缺值 / 未知参数）")


def test_eval_dispatch_passes_threshold(monkeypatch):
    calls = {}

    def fake_eval(**kwargs):
        calls.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_rag_eval_standalone", fake_eval)
    monkeypatch.setattr(
        sys, "argv", ["cli.py", "eval", "--json", "--min-hit5", "0.5"]
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert calls == {"as_json": True, "min_hit5": 0.5}

    monkeypatch.setattr(sys, "argv", ["cli.py", "eval", "--min-hit5", "2"])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2 and "用法" in buf.getvalue()
    print("✓ dispatch 层 eval 传递阈值 / 越界拒绝")


def test_hint_when_all_miss(monkeypatch):
    monkeypatch.setattr(knowledge_stats, "knowledge_status", lambda: _status(True))
    monkeypatch.setattr(rag_knowledge_agent, "RAGKnowledgeAgent", lambda **kw: _FakeAgent())
    monkeypatch.setattr(rag_eval, "evaluate_retrieval", lambda search: _fake_result(0.0))
    code, out = _run(monkeypatch)
    assert code == 1
    assert "索引未加载" in out, "全部未命中时应提示可能是索引故障，而不是切分策略"
    print("✓ 全部未命中时提示索引故障")


def test_json_output(monkeypatch):
    """eval --json：成功 / 未达标 / 跳过 / 基础设施故障四种状态。"""
    monkeypatch.setattr(knowledge_stats, "knowledge_status", lambda: _status(True))
    monkeypatch.setattr(rag_knowledge_agent, "RAGKnowledgeAgent", lambda **kw: _FakeAgent())
    monkeypatch.setattr(rag_eval, "evaluate_retrieval", lambda search: _fake_result(1.0))

    code, out = _run(monkeypatch, as_json=True)
    payload = json.loads(out)
    assert code == 0
    assert payload["ok"] is True and payload["skipped"] is False
    assert payload["hit5"] == 1.0 and payload["threshold"] == 0.8

    monkeypatch.setattr(rag_eval, "evaluate_retrieval", lambda search: _fake_result(0.5))
    code, out = _run(monkeypatch, as_json=True)
    payload = json.loads(out)
    assert code == 1
    assert payload["ok"] is False and payload["hit5"] == 0.5

    monkeypatch.setattr(
        knowledge_stats, "knowledge_status", lambda: _status(False, "知识库未初始化")
    )
    code, out = _run(monkeypatch, as_json=True)
    payload = json.loads(out)
    assert code == 0
    assert payload["skipped"] is True and payload["ok"] is True

    monkeypatch.setattr(
        knowledge_stats, "knowledge_status", lambda: _status(False, "Fail connecting")
    )
    code, out = _run(monkeypatch, as_json=True)
    payload = json.loads(out)
    assert code == 1
    assert payload["ok"] is False and "Fail" in payload["reason"]
    print("✓ eval --json 四种状态与退出码")


if __name__ == "__main__":
    import pytest

    for case in (
        test_skip_when_uninitialized,
        test_failure_on_infra_error,
        test_failure_when_agent_init_raises,
        test_failure_when_search_backend_raises,
        test_pass_and_fail_thresholds,
        test_min_hit5_flag_changes_threshold,
        test_eval_dispatch_passes_threshold,
        test_hint_when_all_miss,
        test_json_output,
        test_parse_eval_args,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    print("\n全部通过")
