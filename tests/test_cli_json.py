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


def test_health_missing_runtime_dependencies(monkeypatch):
    """运行时依赖导入失败时给出提示而不是对 None 调用。"""
    monkeypatch.setattr(cli, "init_agentscope", None)
    code, out = _capture(cli.run_health_check_standalone, True)
    payload = json.loads(out)
    assert code == 1
    assert payload["ok"] is False and "缺少运行时依赖" in payload["message"]
    code, out = _capture(cli.run_health_check_standalone, False)
    assert code == 1 and "FAIL" in out
    print("✓ health 缺少运行时依赖时友好失败")

def test_trips_import_flags_are_case_insensitive(monkeypatch, capsys):
    """trips-import 的 --JSON / --DRY-RUN / --ICS 大小写不敏感。"""
    original_stdout = sys.stdout
    try:
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "cli.py",
                "trips-import",
                "u1",
                "no-such-file-xyz.csv",
                "--JSON",
            ],
        )
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1, "文件不存在属于运行期错误（exit 1）"
        sys.stdout = original_stdout
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["ok"] is False and "不存在" in payload["error"], payload
    finally:
        sys.stdout = original_stdout
    print("✓ trips-import 标志大小写不敏感")


def test_dispatch_usage_errors_respect_json(monkeypatch, capsys):
    """dispatch 级参数错误：带 --json 时 stdout 仍是合法 JSON。"""
    original_stdout = sys.stdout
    try:
        monkeypatch.setattr(
            sys, "argv", ["cli.py", "stats", "bad!", "--json"]
        )
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2
        sys.stdout = original_stdout
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["ok"] is False, payload
        assert "用法" in payload["error"], payload

        monkeypatch.setattr(sys, "argv", ["cli.py", "stats", "bad!"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2
        sys.stdout = original_stdout
        out = capsys.readouterr().out
        assert "用法" in out and not out.lstrip().startswith("{")

        monkeypatch.setattr(
            sys, "argv", ["cli.py", "user-import", "u1", "--json"]
        )
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2
        sys.stdout = original_stdout
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["ok"] is False, payload
        assert "user-import" in payload["error"], payload

        monkeypatch.setattr(sys, "argv", ["cli.py", "user-import", "u1"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2
        out = capsys.readouterr().out
        assert "user-import" in out

        # 其它一次性命令的用法错误同样遵守 --json 契约
        for argv, marker in [
            (["cli.py", "trip-add", "u1", "--destination", "上海", "--json"], "trip-add"),
            (["cli.py", "trip-update", "u1", "--json"], "trip-update"),
            (["cli.py", "trip-delete", "u1", "--json"], "trip-delete"),
            (["cli.py", "trips", "--json"], "trips"),
            (["cli.py", "trips-dedupe", "--json"], "trips-dedupe"),
            (["cli.py", "trips-import", "u1", "--json"], "trips-import"),
            (["cli.py", "health", "--json", "extra"], "health"),
            (["cli.py", "doctor", "--json", "--bogus"], "doctor"),
            (["cli.py", "eval", "--min-hit5", "nope", "--json"], "eval"),
        ]:
            monkeypatch.setattr(sys, "argv", argv)
            with pytest.raises(SystemExit) as exc:
                cli.main()
            assert exc.value.code == 2, argv
            sys.stdout = original_stdout
            out = capsys.readouterr().out
            payload = json.loads(out)
            assert payload["ok"] is False, (argv, payload)
            assert marker in payload["error"], (argv, payload)
    finally:
        sys.stdout = original_stdout
    print("✓ dispatch 用法错误遵守 --json 契约（全命令）")


def test_cli_id_validation_matches_web():
    """CLI 的 ID 校验与 Web（Rust \\w）同一字符集，前导 - 仍拒绝。"""
    connector = chr(0x203F)  # ‿ (Pc)：Web 接受、Python re \\w 不接受
    assert cli._valid_web_id("a" + connector + "b"), "连接符标点必须与 Web 一致"
    assert not cli._valid_web_id("²abc"), "上标数字 Web 拒绝，CLI 不能放行"
    assert not cli._valid_web_id("-lead"), "前导 - 会被 CLI 当选项"
    assert cli._valid_web_id("张三-1")
    print("✓ CLI 用户 / 会话 ID 校验与 Web 对齐")


if __name__ == "__main__":
    for case in (
        test_health_json_ok,
        test_health_json_failure,
        test_kb_status_json,
        test_health_missing_runtime_dependencies,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    test_cli_id_validation_matches_web()
    print("\n全部通过")
