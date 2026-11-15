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


def test_user_import_call_site_passes_allow_quarantined(monkeypatch, tmp_path):
    """user-import 真实调用点以 allow_quarantined=True 打开存储。"""
    backup_path = tmp_path / "backup.json"
    backup_path.write_text(
        json.dumps({"preferences": {"home_location": "北京"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    recorded = []

    class _Store:
        def export_snapshot(self):
            return {
                "preferences": {},
                "trip_history": [],
                "chat_history": [],
                "session_titles": {},
                "pinned_sessions": [],
                "archived_sessions": [],
                "statistics": {},
            }

        def get_statistics(self):
            return {"total_messages": 0, "total_trips": 0}

        def get_preference(self):
            return {}

        def get_trip_history(self, limit=None):
            return []

        def get_chat_history(self, limit=None):
            return []

        def close(self):
            pass

    def fake_open(user_id, allow_quarantined=False):
        recorded.append(allow_quarantined)
        return _Store()

    monkeypatch.setattr(cli, "_open_cli_store", fake_open)
    code = cli.run_user_import_standalone(
        "u1", str(backup_path), dry_run=True
    )
    assert code == 0, code
    assert recorded == [True], "恢复必须豁免隔离打开（allow_quarantined=True）"


def test_recovered_corrupt_backup_ignores_deleted_accounts(tmp_path):
    """账号数据已删除（.deleted 墓碑）时遗留隔离备份是孤儿：不再拦截。"""
    db_path = tmp_path / "u-gone.json"
    (tmp_path / "u-gone.json.corrupt-20260101000000000000-1").write_text(
        "{}", encoding="utf-8"
    )
    (tmp_path / "u-gone.json.deleted").write_text("", encoding="utf-8")

    db_path_value = str(db_path)

    class _Store:
        db_path = db_path_value

    assert cli._recovered_corrupt_backup(_Store()) is None


def test_open_cli_store_blocks_pending_quarantine(monkeypatch):
    """pending（隔离改名未完成）状态下普通命令拒绝、恢复入口放行。"""
    from config import STORAGE_CONFIG

    monkeypatch.setitem(STORAGE_CONFIG, "postgres_dsn", "")
    recorded = []

    class _PendingStore:
        def get_fresh_quarantine_backup(self):
            return None

        def has_pending_corrupt_quarantine(self):
            return True

        def close(self):
            recorded.append("closed")

    monkeypatch.setattr(
        storage,
        "create_long_term_memory",
        lambda *args, **kwargs: _PendingStore(),
    )
    with pytest.raises(cli._QuarantinedMemoryError) as info:
        cli._open_cli_store("u1")
    assert "尚未完成" in str(info.value), str(info.value)
    assert "已隔离备份到" not in str(info.value)
    assert recorded == ["closed"]
    # 恢复入口豁免（不再查询 pending）
    assert cli._open_cli_store("u1", allow_quarantined=True) is not None


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
    # 直跑模式自动发现全部用例：monkeypatch / tmp_path 自动提供，
    # 可选 caplog（默认 None，用例自带无 caplog 分支）按 None 传入；
    # 其余需 pytest 专用夹具的用例计入跳过数并明确报告
    import asyncio
    import inspect
    import sys

    # Windows 控制台 / 重定向管道默认 GBK：✓ 等字符会抛 UnicodeEncodeError
    # 把清单截断在第一个用例；先切 UTF-8（不支持时保持原状）
    try:
        for _stream in (sys.stdout, sys.stderr):
            _reconfigure = getattr(_stream, "reconfigure", None)
            if callable(_reconfigure):
                _reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        pass
    import pathlib
    import tempfile

    ran = 0
    skipped = 0
    for name, func in sorted(globals().items()):
        if not name.startswith("test_") or not callable(func):
            continue
        params = inspect.signature(func).parameters
        fixture = None
        kwargs: dict = {}
        unsupported = False
        for param, spec in params.items():
            if param == "monkeypatch":
                fixture = pytest.MonkeyPatch()
                kwargs[param] = fixture
            elif param == "tmp_path":
                kwargs[param] = pathlib.Path(tempfile.mkdtemp())
            elif param == "caplog" and spec.default is None:
                kwargs[param] = None
            else:
                unsupported = True
                break
        if unsupported:
            if fixture is not None:
                fixture.undo()
            skipped += 1
            continue
        storage_snapshot: dict | None = None
        try:
            from config import STORAGE_CONFIG

            storage_snapshot = dict(STORAGE_CONFIG)
        except Exception:  # noqa: BLE001 - 配置不可用时不快照
            storage_snapshot = None
        try:
            # async def 用例同样要真正执行：直接调用只创建 coroutine，
            # 断言从不运行，直跑清单会给出假绿
            if inspect.iscoroutinefunction(func):
                asyncio.run(func(**kwargs))
            else:
                func(**kwargs)
            ran += 1
        except pytest.skip.Exception:
            skipped += 1
        finally:
            if storage_snapshot is not None:
                from typing import cast

                from config import STORAGE_CONFIG

                _cfg = cast(dict, STORAGE_CONFIG)
                _cfg.clear()
                _cfg.update(storage_snapshot)
            if fixture is not None:
                fixture.undo()
    total = sum(
        1
        for name, func in globals().items()
        if name.startswith("test_") and callable(func)
    )
    print(
        f"\n全部通过（{ran}/{total} 个用例；"
        f"{skipped} 个需 pytest 专用夹具或按条件跳过）"
    )
