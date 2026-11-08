"""维护命令 --json 失败输出契约（无网络 / 无真实存储）。

sessions / trips / preferences / stats 的失败路径必须与成功输出保持同一
契约：``--json`` 模式下输出 ``{"ok": false, "error": ...}``，纯文本错误会
让脚本的 JSON 解析直接失败。运行：python tests/test_cli_json_failures.py
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
from context import storage


def _run(call):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = call()
    return code, out.getvalue()


def test_json_failures_are_structured(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("cannot open")

    monkeypatch.setattr(storage, "create_long_term_memory", boom)
    cases = [
        lambda: cli.run_stats_standalone("u1", as_json=True),
        lambda: cli.run_sessions_list_standalone("u1", as_json=True),
        lambda: cli.run_trips_list_standalone("u1", as_json=True),
        lambda: cli.run_preferences_list_standalone("u1", as_json=True),
    ]
    for case in cases:
        code, out = _run(case)
        payload = json.loads(out)
        assert code == 1, out
        assert payload["ok"] is False, payload
        assert "打开长期记忆失败" in payload["error"], payload

    # 非 --json 模式保持原有纯文本提示
    code, text = _run(lambda: cli.run_stats_standalone("u1"))
    assert code == 1 and "打开长期记忆失败" in text
    assert not text.strip().startswith("{")
    print("✓ 四个维护命令 --json 失败输出结构化")


def test_json_invalid_user_is_structured(monkeypatch):
    """参数错误的 --json 输出同样结构化（不触达存储）。"""
    assert monkeypatch is not None
    cases = [
        lambda: cli.run_stats_standalone("-bad", as_json=True),
        lambda: cli.run_sessions_list_standalone("-bad", as_json=True),
        lambda: cli.run_trips_list_standalone("-bad", as_json=True),
        lambda: cli.run_preferences_list_standalone("-bad", as_json=True),
    ]
    for case in cases:
        code, out = _run(case)
        payload = json.loads(out)
        assert code == 1 and payload["ok"] is False, payload
        assert "用户 ID" in payload["error"], payload
    print("✓ 参数错误的 --json 输出同样结构化")


if __name__ == "__main__":
    for case in (
        test_json_failures_are_structured,
        test_json_invalid_user_is_structured,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    print("\n全部通过")
