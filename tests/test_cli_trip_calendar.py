"""`python cli.py trip-calendar` 测试（无需 API Key、不访问网络）。

运行：python tests/test_cli_trip_calendar.py
"""
import contextlib
import io
import os
import sys
import tempfile

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

import cli


class _FakeStore:
    def __init__(self, trips):
        self.trips = list(trips)
        self.closed = False

    def get_trip_history(self, limit=None):
        return list(self.trips)

    def close(self):
        self.closed = True


def _trip(trip_id="trip_1", **overrides):
    trip = {
        "trip_id": trip_id,
        "origin": "北京",
        "destination": "上海",
        "start_date": "2026-12-05",
        "end_date": "2026-12-07",
        "purpose": "出差",
    }
    trip.update(overrides)
    return trip


def _run(
    monkeypatch,
    user_id,
    trip_id,
    trips,
    target=None,
    to_stdout=False,
    force=False,
):
    store = _FakeStore(trips)
    monkeypatch.setattr(cli, "_open_cli_store", lambda uid: store)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.run_trip_calendar_standalone(
            user_id, trip_id, target=target, to_stdout=to_stdout, force=force
        )
    return code, buf.getvalue(), store


def test_export_single_trip_ics(monkeypatch):
    target = os.path.join(tempfile.mkdtemp(), "trip.ics")
    code, out, store = _run(
        monkeypatch,
        "u1",
        "trip_1",
        [_trip(), _trip("trip_2", destination="杭州")],
        target=target,
    )
    assert code == 0, out
    assert store.closed, "导出后必须关闭存储连接"
    assert "已导出行程 trip_1 的日历" in out
    text = open(target, encoding="utf-8").read()
    assert text.count("BEGIN:VEVENT") == 1
    assert "上海" in text and "杭州" not in text, "只导出指定行程"
    assert "BEGIN:VCALENDAR" in text and "VALARM" in text
    print("✓ trip-calendar 导出单条行程 ICS")


def test_missing_and_ambiguous_trip_rejected(monkeypatch):
    target = os.path.join(tempfile.mkdtemp(), "x.ics")
    code, out, _ = _run(monkeypatch, "u1", "trip_9", [_trip()], target=target)
    assert code == 1 and "不存在" in out

    code, out, _ = _run(
        monkeypatch,
        "u1",
        "trip_1",
        [_trip(), _trip(destination="杭州")],
        target=target,
    )
    assert code == 1 and "trips-dedupe" in out, "歧义 trip_id 不能猜行"
    assert not os.path.exists(target)
    print("✓ trip-calendar 不存在 / 歧义 trip_id 拒绝导出")


def test_trip_without_start_date_rejected(monkeypatch):
    target = os.path.join(tempfile.mkdtemp(), "x.ics")
    code, out, _ = _run(
        monkeypatch, "u1", "trip_1", [_trip(start_date="")], target=target
    )
    assert code == 1 and "缺少出发日期" in out
    assert not os.path.exists(target)
    print("✓ trip-calendar 缺少出发日期时拒绝导出")


def test_stdout_and_overwrite_protection(monkeypatch):
    code, out, _ = _run(monkeypatch, "u1", "trip_1", [_trip()], to_stdout=True)
    assert code == 0
    assert "BEGIN:VCALENDAR" in out and "BEGIN:VEVENT" in out

    target = os.path.join(tempfile.mkdtemp(), "trip.ics")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("old")
    code, out, _ = _run(monkeypatch, "u1", "trip_1", [_trip()], target=target)
    assert code == 1 and "文件已存在" in out
    with open(target, encoding="utf-8") as handle:
        assert handle.read() == "old", "默认不覆盖已有文件"

    code, out, _ = _run(
        monkeypatch, "u1", "trip_1", [_trip()], target=target, force=True
    )
    assert code == 0
    with open(target, encoding="utf-8") as handle:
        assert "BEGIN:VCALENDAR" in handle.read()
    print("✓ trip-calendar --stdout 与覆盖保护")


def test_unparsable_end_notice(monkeypatch):
    """返程日期无法解析：日历按单日导出，但输出提示（不再静默截断）。"""
    target = os.path.join(tempfile.mkdtemp(), "trip.ics")
    code, out, _ = _run(
        monkeypatch,
        "u1",
        "trip_1",
        [_trip(end_date="2026/12/20")],
        target=target,
    )
    assert code == 0
    assert "返程日期无法解析" in out
    print("✓ trip-calendar 返程无法解析时提示")


def test_invalid_ids_and_store_failure(monkeypatch):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert cli.run_trip_calendar_standalone("bad id", "trip_1") == 1
        assert cli.run_trip_calendar_standalone("u1", "-bad") == 1

        def boom(uid):
            raise RuntimeError("db down")

        monkeypatch.setattr(cli, "_open_cli_store", boom)
        assert cli.run_trip_calendar_standalone("u1", "trip_1") == 1
    assert "打开长期记忆失败" in buf.getvalue()
    print("✓ trip-calendar 非法 ID / 存储打开失败")


def test_main_dispatch_trip_calendar(monkeypatch):
    workdir = tempfile.mkdtemp()
    monkeypatch.chdir(workdir)
    store = _FakeStore([_trip()])
    monkeypatch.setattr(cli, "_open_cli_store", lambda uid: store)
    monkeypatch.setattr(
        sys, "argv", ["cli.py", "trip-calendar", "u1", "trip_1"]
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0, buf.getvalue()
    path = os.path.join(workdir, "exports", "aligo_u1_trip_1.ics")
    assert os.path.exists(path)
    with open(path, encoding="utf-8") as handle:
        assert "BEGIN:VEVENT" in handle.read()
    print("✓ main() 分发 trip-calendar（默认导出路径）")


def test_default_path_includes_safe_user(monkeypatch):
    """默认文件名带（清洗后的）用户 ID：不同用户的同名 trip_id 不互相覆盖。"""
    workdir = tempfile.mkdtemp()
    monkeypatch.chdir(workdir)
    store = _FakeStore([_trip()])
    monkeypatch.setattr(cli, "_open_cli_store", lambda uid: store)
    for user in ("alice", "bob", "中文用户"):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.run_trip_calendar_standalone(user, "trip_1")
        assert code == 0, (user, buf.getvalue())
    exports = os.path.join(workdir, "exports")
    names = sorted(name for name in os.listdir(exports) if name.endswith(".ics"))
    assert len(names) == 3, names
    assert "aligo_alice_trip_1.ics" in names
    assert "aligo_bob_trip_1.ics" in names
    assert any(name.startswith("aligo_") and name.endswith("_trip_1.ics") for name in names)
    assert not any("中文用户" in name for name in names), "文件名必须是 ASCII 安全字符"
    print("✓ trip-calendar 默认文件名按用户隔离")


def test_main_dispatch_rejects_html_and_bad_args(monkeypatch):
    for argv in (
        ["cli.py", "trip-calendar", "u1", "trip_1", "--html"],
        ["cli.py", "trip-calendar", "u1", "trip_1", "--stdout", "x.ics"],
        ["cli.py", "trip-calendar", "u1"],
        ["cli.py", "trip-calendar", "-u1", "trip_1"],
    ):
        monkeypatch.setattr(sys, "argv", argv)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2, argv
        assert "用法" in buf.getvalue()
    print("✓ trip-calendar 非法参数返回用法错误")


if __name__ == "__main__":
    mp = pytest.MonkeyPatch()
    try:
        test_export_single_trip_ics(mp)
        test_missing_and_ambiguous_trip_rejected(mp)
        test_trip_without_start_date_rejected(mp)
        test_unparsable_end_notice(mp)
        test_stdout_and_overwrite_protection(mp)
        test_invalid_ids_and_store_failure(mp)
        test_main_dispatch_trip_calendar(mp)
        test_default_path_includes_safe_user(mp)
        test_main_dispatch_rejects_html_and_bad_args(mp)
    finally:
        mp.undo()
    print("\n全部通过")
