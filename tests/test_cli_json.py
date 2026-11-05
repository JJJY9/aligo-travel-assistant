"""
`python cli.py health --json` / `kb --json` 测试（无需 API Key、不访问网络）

运行：python tests/test_cli_json.py
"""
import contextlib
import io
import json
import os
import sys

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

import cli


def _capture(func, *args) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = func(*args)
    return code, buf.getvalue()


def test_health_json_ok(monkeypatch):
    async def fake_health(**kwargs):
        return True, "deepseek-chat 可用"

    monkeypatch.setattr(cli, "init_agentscope", lambda: print("✓ init noise"))
    monkeypatch.setattr(cli, "check_llm_health", fake_health)
    code, out = _capture(cli.run_health_check_standalone, True)
    payload = json.loads(out)
    assert code == 0
    assert payload == {"ok": True, "message": "deepseek-chat 可用"}
    assert "noise" not in out, "JSON 模式应屏蔽初始化日志"
    print("✓ health --json 成功")


def test_health_json_failure(monkeypatch):
    async def fake_health(**kwargs):
        return False, "timeout"

    monkeypatch.setattr(cli, "init_agentscope", lambda: None)
    monkeypatch.setattr(cli, "check_llm_health", fake_health)
    code, out = _capture(cli.run_health_check_standalone, True)
    payload = json.loads(out)
    assert code == 1
    assert payload["ok"] is False
    assert "timeout" in payload["message"]
    print("✓ health --json 失败退出码 1")


def test_kb_status_json(monkeypatch):
    from utils import knowledge_stats

    monkeypatch.setattr(
        knowledge_stats,
        "knowledge_status",
        lambda: {
            "available": True,
            "collection": "aligo_knowledge",
            "documents": 3,
            "path": "data/x.db",
        },
    )
    code, out = _capture(cli.run_knowledge_status_standalone, True)
    payload = json.loads(out)
    assert code == 0
    assert payload["documents"] == 3
    print("✓ kb --json 可用")

    monkeypatch.setattr(
        knowledge_stats,
        "knowledge_status",
        lambda: {"available": False, "reason": "知识库未初始化", "path": "data/x.db"},
    )
    code, out = _capture(cli.run_knowledge_status_standalone, True)
    payload = json.loads(out)
    assert code == 1
    assert payload["available"] is False
    print("✓ kb --json 不可用退出码 1")


if __name__ == "__main__":
    for case in (
        test_health_json_ok,
        test_health_json_failure,
        test_kb_status_json,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    print("\n全部通过")
