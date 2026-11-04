"""
`python cli.py trips-export` 测试（无需 API Key、不访问网络）

运行：python tests/test_cli_trips_export.py
"""
import contextlib
import io
import os
import sys

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

import cli
from context import storage


class _FakeStore:
    def __init__(self, trips, fail=False):
        self.trips = list(trips)
        self.fail = fail
        self.closed = False

    def get_trip_history(self, limit=None):
        if self.fail:
            raise RuntimeError("db down")
        return list(self.trips)

    def close(self):
        self.closed = True


def _trip(destination="上海", start="2026-12-05", end="2026-12-07"):
    return {
        "origin": "北京",
        "destination": destination,
        "start_date": start,
        "end_date": end,
        "purpose": "出差",
    }


def _run(user_id, target=None, to_stdout=False, force=False):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.run_trips_export_standalone(
            user_id, target=target, to_stdout=to_stdout, force=force
        )
    return code, buf.getvalue()


def test_export_to_file(monkeypatch):
    import tempfile

    fake = _FakeStore([_trip("上海"), _trip("杭州", "2026-12-09", "2026-12-10")])
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args: fake)
    target = os.path.join(tempfile.mkdtemp(), "trips.csv")
    code, out = _run("u1", target=target)
    assert code == 0 and "已导出 2 条行程" in out
    with open(target, encoding="utf-8") as handle:
        text = handle.read()
    assert text.startswith("\ufeff")
    assert "北京,上海,2026-12-05,2026-12-07,出差" in text
    assert fake.closed
    print("✓ trips-export 写入 CSV 文件")


def test_export_stdout(monkeypatch):
    fake = _FakeStore([_trip()])
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args: fake)
    code, out = _run("u1", to_stdout=True)
    assert code == 0
    assert "出发地,目的地,出发日期,返程日期,目的" in out
    assert "北京,上海" in out
    print("✓ trips-export --stdout")


def test_default_path_and_overwrite(monkeypatch):
    import tempfile

    workdir = tempfile.mkdtemp()
    monkeypatch.chdir(workdir)
    fake = _FakeStore([_trip()])
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args: fake)

    code, out = _run("u1")
    assert code == 0
    path = os.path.join(workdir, "exports", "aligo_trips.csv")
    assert os.path.exists(path)

    code, out = _run("u1")
    assert code == 1 and "文件已存在" in out
    code, out = _run("u1", force=True)
    assert code == 0
    print("✓ 默认路径 exports/aligo_trips.csv 与覆盖保护")


def test_empty_trips(monkeypatch):
    import tempfile

    fake = _FakeStore([])
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args: fake)
    target = os.path.join(tempfile.mkdtemp(), "empty.csv")
    code, out = _run("u1", target=target)
    assert code == 0 and "已导出 0 条行程" in out
    with open(target, encoding="utf-8") as handle:
        assert len(handle.read().strip().splitlines()) == 1
    print("✓ 无行程时仅导出表头")


def test_invalid_and_failures(monkeypatch):
    called = {"n": 0}

    def fake_factory(*args):
        called["n"] += 1
        return _FakeStore([])

    monkeypatch.setattr(storage, "create_long_term_memory", fake_factory)
    code, out = _run("--bad")
    assert code == 1 and "用户 ID" in out
    assert called["n"] == 0

    fake = _FakeStore([], fail=True)
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args: fake)
    code, out = _run("u1")
    assert code == 1 and "读取行程失败" in out
    assert fake.closed
    print("✓ 非法 ID / 读取失败")


def test_store_open_failure(monkeypatch):
    def boom(*args):
        raise RuntimeError("cannot open")

    monkeypatch.setattr(storage, "create_long_term_memory", boom)
    code, out = _run("u1")
    assert code == 1 and "打开长期记忆失败" in out
    print("✓ 存储打开失败返回 1")


def test_pg_fallback_refused(monkeypatch):
    from config import STORAGE_CONFIG
    from context import long_term_memory as ltm

    class _JsonStore(_FakeStore):
        pass

    monkeypatch.setattr(ltm, "LongTermMemory", _JsonStore)
    monkeypatch.setitem(STORAGE_CONFIG, "postgres_dsn", "postgresql://localhost/x")
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args: _JsonStore([])
    )
    code, out = _run("u1")
    assert code == 1 and "回退本地 JSON" in out
    print("✓ PostgreSQL 回退时拒绝执行")


def test_main_dispatch(monkeypatch):
    import tempfile

    workdir = tempfile.mkdtemp()
    monkeypatch.chdir(workdir)
    fake = _FakeStore([_trip()])
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args: fake)
    monkeypatch.setattr(
        sys, "argv", ["cli.py", "trips-export", "u1", "--force"]
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert os.path.exists(os.path.join(workdir, "exports", "aligo_trips.csv"))
    print("✓ dispatch 层 trips-export 正常执行")


if __name__ == "__main__":
    for case in (
        test_export_to_file,
        test_export_stdout,
        test_default_path_and_overwrite,
        test_empty_trips,
        test_invalid_and_failures,
        test_store_open_failure,
        test_pg_fallback_refused,
        test_main_dispatch,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    print("\n全部通过")
