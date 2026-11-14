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


def test_dispatch_usage_errors_honor_json():
    """dispatch 层参数错误与 run_* 内部错误同一 --json 契约。"""
    cases = [
        ["cli.py", "sessions", "u1", "--json", "--bogus"],
        ["cli.py", "preferences", "u1", "--json", "extra"],
        ["cli.py", "pref-set", "u1", "home", "--json"],
        ["cli.py", "session-show", "u1", "--json"],
        ["cli.py", "kb-import", "b.json", "extra", "--json"],
        ["cli.py", "kb-dedupe", "--bogus", "--json"],
    ]
    for argv in cases:
        mp = pytest.MonkeyPatch()
        try:
            mp.setattr(sys, "argv", argv)
            out = io.StringIO()
            with contextlib.redirect_stdout(out), pytest.raises(SystemExit) as exc:
                cli.main()
        finally:
            mp.undo()
        assert exc.value.code == 2, argv
        payload = json.loads(out.getvalue())
        assert payload["ok"] is False and "用法" in payload["error"], (
            argv,
            out.getvalue(),
        )
    print("✓ dispatch 参数错误遵守 --json 契约")


def test_corrupt_snapshot_fails_read_commands(monkeypatch):
    """损坏隔离后 trips / preferences / sessions / trips-export 不能报「无数据」。"""
    import tempfile
    from pathlib import Path

    class _CorruptStore:
        def get_recovered_corrupt_backup(self):
            return "u1.json.corrupt-20261113-1200"

        def _unexpected(self, *args, **kwargs):
            raise AssertionError("损坏状态必须先检查再读取")

        get_chat_history = _unexpected
        get_trip_history = _unexpected
        get_preference = _unexpected
        get_session_titles = _unexpected
        get_pinned_sessions = _unexpected
        get_archived_sessions = _unexpected

        def close(self):
            pass

    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: _CorruptStore()
    )
    cases = [
        lambda: cli.run_sessions_list_standalone("u1", as_json=True),
        lambda: cli.run_trips_list_standalone("u1", as_json=True),
        lambda: cli.run_preferences_list_standalone("u1", as_json=True),
    ]
    for case in cases:
        code, out = _run(case)
        payload = json.loads(out)
        assert code == 1 and payload["ok"] is False, out
        assert "损坏" in payload["error"], payload

    target = Path(tempfile.mkdtemp()) / "trips.csv"
    code, out = _run(
        lambda: cli.run_trips_export_standalone("u1", target=str(target))
    )
    assert code == 1 and "损坏" in out, out
    assert not target.exists(), "损坏状态不能写出空导出文件"
    print("✓ 损坏隔离后读命令失败而不是回报空数据")


def test_corrupt_snapshot_blocks_write_commands(monkeypatch):
    """损坏隔离期间写命令同样拒绝：不能在空快照上重建主文件。"""
    import tempfile
    from pathlib import Path

    backup = "data/memory/u1.json.corrupt-20261113-1200"

    class _CorruptStore:
        def get_recovered_corrupt_backup(self):
            return backup

        def _unexpected(self, *args, **kwargs):
            raise AssertionError("损坏状态必须先拒绝，不能触碰存储")

        get_trip_history = _unexpected
        save_trip_history = _unexpected
        get_preference = _unexpected
        save_preference = _unexpected
        delete_preference = _unexpected

        def close(self):
            pass

    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *a, **k: _CorruptStore()
    )
    backup_file = Path(tempfile.mkdtemp()) / "backup.json"
    backup_file.write_text(
        json.dumps({"preferences": {"home_location": "上海"}}), encoding="utf-8"
    )
    cases = (
        lambda: cli.run_trip_add_standalone(
            "u1", "北京", "上海", "2026-12-01", as_json=True
        ),
        lambda: cli.run_preference_set_standalone(
            "u1", "hotel_brands", "汉庭", as_json=True
        ),
        # user-import 是隔离账号唯一的产品内恢复路径：显式豁免隔离守卫，
        # 不走 _fail("损坏")，这里只断言前两类写命令被拒绝
    )
    for case in cases:
        code, out = _run(case)
        payload = json.loads(out)
        assert code == 1 and payload["ok"] is False, out
        assert "损坏" in payload["error"], payload
        assert backup in payload["error"], payload
    print("✓ 损坏隔离后写命令拒绝执行（不重建空快照）")


def test_open_cli_store_forwards_allow_quarantined(monkeypatch):
    """恢复入口（user-import）把 allow_quarantined=True 传给存储工厂，
    普通维护命令默认 False——隔离状态下只有恢复能重建主文件。"""
    recorded = []

    class _Store:
        def close(self):
            pass

    def fake_factory(user_id, path, strict=False, allow_quarantined=False):
        recorded.append(allow_quarantined)
        return _Store()

    monkeypatch.setattr(storage, "create_long_term_memory", fake_factory)
    cli._open_cli_store("u1")
    assert recorded[-1] is False
    cli._open_cli_store("u1", allow_quarantined=True)
    assert recorded[-1] is True
    print("✓ CLI 存储打开按入口转发 allow_quarantined")


if __name__ == "__main__":
    import inspect

    ran = 0
    for name, func in sorted(globals().items()):
        if not name.startswith("test_") or not callable(func):
            continue
        params = inspect.signature(func).parameters
        if any(param not in ("monkeypatch",) for param in params):
            continue
        fixture = None
        kwargs: dict = {}
        if "monkeypatch" in params:
            fixture = pytest.MonkeyPatch()
            kwargs["monkeypatch"] = fixture
        try:
            func(**kwargs)
            ran += 1
        finally:
            if fixture is not None:
                fixture.undo()
    print(f"\n全部通过（{ran} 个用例）")
