"""
`python cli.py trips-dedupe` 测试（无需 API Key、不访问网络）

运行：python tests/test_cli_trips_dedupe.py
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
    def __init__(self, trips, fail_get=False):
        self.trips = list(trips)
        self.deleted = []
        self.fail_get = fail_get
        self.closed = False

    def get_trip_history(self, limit=None):
        if self.fail_get:
            raise RuntimeError("db down")
        return list(self.trips)

    def delete_trip(self, trip_id):
        if not any(trip["trip_id"] == trip_id for trip in self.trips):
            return False
        self.trips = [trip for trip in self.trips if trip["trip_id"] != trip_id]
        self.deleted.append(trip_id)
        return True

    def close(self):
        self.closed = True


def _trip(trip_id, origin="北京", destination="上海", start="2026-11-09", end="2026-11-11"):
    return {
        "trip_id": trip_id,
        "origin": origin,
        "destination": destination,
        "start_date": start,
        "end_date": end,
    }


def _run(user_id, dry_run=False):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.run_trips_dedupe_standalone(user_id, dry_run=dry_run)
    return code, buf.getvalue()


def test_dedupes_and_keeps_first(monkeypatch):
    fake = _FakeStore(
        [_trip("trip_1"), _trip("trip_2"), _trip("trip_3", destination="杭州")]
    )
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda user_id, path: fake
    )
    code, out = _run("u1")
    assert code == 0
    assert fake.deleted == ["trip_2"], "同路线同日期只保留最早一条"
    assert "删除 1 条重复行程" in out
    assert fake.closed
    print("✓ 删除完全重复的行程（保留最早）")


def test_undated_trips_not_deduped(monkeypatch):
    fake = _FakeStore(
        [_trip("trip_1", start=None, end=None), _trip("trip_2", start=None, end=None)]
    )
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda user_id, path: fake
    )
    code, out = _run("u1")
    assert code == 0
    assert fake.deleted == []
    assert "没有重复行程（共 2 条）" in out
    print("✓ 缺少出发日期的行程不参与去重")


def test_no_trips(monkeypatch):
    fake = _FakeStore([])
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda user_id, path: fake
    )
    code, out = _run("u1")
    assert code == 0 and "共 0 条" in out
    print("✓ 无行程时正常返回")


def test_invalid_user_id(monkeypatch):
    called = {"n": 0}

    def fake_factory(user_id, path):
        called["n"] += 1
        return _FakeStore([])

    monkeypatch.setattr(storage, "create_long_term_memory", fake_factory)
    code, out = _run("../etc/passwd")
    assert code == 1
    assert "用户 ID" in out
    assert called["n"] == 0, "非法 ID 不应打开存储"
    print("✓ 非法用户 ID 被拒绝")


def test_read_failure(monkeypatch):
    fake = _FakeStore([], fail_get=True)
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda user_id, path: fake
    )
    code, out = _run("u1")
    assert code == 1 and "读取行程失败" in out
    assert fake.closed
    print("✓ 读取失败返回 1")


def test_store_open_failure(monkeypatch):
    def boom(user_id, path):
        raise RuntimeError("cannot open")

    monkeypatch.setattr(storage, "create_long_term_memory", boom)
    code, out = _run("u1")
    assert code == 1 and "打开长期记忆失败" in out
    print("✓ 存储打开失败返回 1")


def test_skips_group_when_keeper_deleted_concurrently(monkeypatch):
    """删除前复核：保留者已被并发删除时，跳过该组，避免删掉仅剩副本。"""
    fake = _FakeStore([_trip("trip_1"), _trip("trip_2")])
    original = fake.get_trip_history
    calls = {"n": 0}

    def second_read_without_keeper(limit=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return original(limit)
        return [trip for trip in fake.trips if trip["trip_id"] != "trip_1"]

    fake.get_trip_history = second_read_without_keeper
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda user_id, path: fake
    )
    code, out = _run("u1")
    assert code == 0
    assert fake.deleted == [], "保留者已不存在时不应删除仅剩的副本"
    assert "没有需要删除的重复行程" in out
    print("✓ 保留者被并发删除时跳过该组")


def test_json_backend_reloads_before_keeper_recheck(monkeypatch):
    """JSON 后端删除前必须重载磁盘：保留者被 Web 端并发删除时不能误删仅剩副本。"""
    import json
    import tempfile
    from pathlib import Path

    from context.long_term_memory import LongTermMemory

    storage_dir = tempfile.mkdtemp()
    path = Path(storage_dir) / "u-real.json"
    trips = [
        {
            "trip_id": "trip_1",
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-12-01",
            "purpose": "出差",
        },
        {
            "trip_id": "trip_2",
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-12-01",
            "purpose": "出差",
        },
    ]
    path.write_text(json.dumps({"trip_history": trips}), encoding="utf-8")
    # CLI 进程启动时加载的快照包含 trip_1 / trip_2
    store = LongTermMemory("u-real", storage_dir)
    # 模拟 Web 端在 CLI 运行期间删除了保留者 trip_1
    path.write_text(json.dumps({"trip_history": [trips[1]]}), encoding="utf-8")

    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda user_id, path: store
    )
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_trips_dedupe_standalone("u-real")
    assert code == 0
    assert "没有需要删除的重复行程" in out.getvalue()
    remaining = json.loads(path.read_text(encoding="utf-8"))["trip_history"]
    assert [item["trip_id"] for item in remaining] == ["trip_2"], "不能删掉仅剩的副本"
    print("✓ JSON 后端删除前重载磁盘（保留者并发删除时不误删）")


def test_delete_failure_returns_1(monkeypatch):
    class _FailDeleteStore(_FakeStore):
        def delete_trip(self, trip_id):
            raise RuntimeError("disk error")

    fake = _FailDeleteStore([_trip("trip_1"), _trip("trip_2")])
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda user_id, path: fake
    )
    code, out = _run("u1")
    assert code == 1 and "删除过程中失败" in out
    print("✓ 删除过程中失败返回 1")


def test_pg_configured_but_json_fallback(monkeypatch):
    """PostgreSQL 配置错误时工厂会回退 JSON，维护命令必须拒绝执行。"""
    from config import STORAGE_CONFIG
    from context import long_term_memory as ltm

    class _JsonStore:
        def close(self):
            pass

    monkeypatch.setattr(ltm, "LongTermMemory", _JsonStore)
    monkeypatch.setitem(STORAGE_CONFIG, "postgres_dsn", "postgresql://localhost/x")
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda user_id, path: _JsonStore()
    )
    code, out = _run("u1")
    assert code == 1
    assert "回退本地 JSON" in out
    print("✓ PostgreSQL 回退时拒绝执行去重")


def test_dry_run_does_not_delete(monkeypatch):
    fake = _FakeStore(
        [_trip("trip_1"), _trip("trip_2"), _trip("trip_3", destination="杭州")]
    )
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda user_id, path: fake
    )
    code, out = _run("u1", dry_run=True)
    assert code == 0
    assert fake.deleted == [], "dry-run 不应删除任何记录"
    assert "发现 1 条重复行程" in out
    assert "将删除 trip_2" in out
    assert "去掉 --dry-run" in out
    print("✓ --dry-run 只列出重复记录，不删除")


def test_main_rejects_unknown_flags(monkeypatch):
    monkeypatch.setattr(
        sys, "argv", ["cli.py", "trips-dedupe", "u1", "--dryrun"]
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert "用法" in buf.getvalue()
    print("✓ 未知参数返回用法错误")


def test_ambiguous_trip_id_is_not_deleted(monkeypatch):
    """同一 trip_id 出现在多条不同行程里时，不自动删除并返回 1。"""
    fake = _FakeStore(
        [
            _trip("trip_1"),
            _trip("trip_2"),
            _trip("trip_2", destination="杭州"),
        ]
    )
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda user_id, path: fake
    )
    code, out = _run("u1")
    assert code == 1
    assert "无法安全自动删除" in out
    assert "trip_2" in out
    assert fake.deleted == [], "歧义 id 不能删除"
    print("✓ 歧义 trip_id 不自动删除并返回 1")


def test_one_day_trips_use_start_as_end(monkeypatch):
    """无返程日期与返程=出发日按同一行程去重。"""
    fake = _FakeStore(
        [
            _trip("trip_1", start="2026-12-01", end=None),
            _trip("trip_2", start="2026-12-01", end="2026-12-01"),
        ]
    )
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda user_id, path: fake
    )
    code, out = _run("u1")
    assert code == 0
    assert fake.deleted == ["trip_2"]
    print("✓ 单日行程的两种返程写法按重复处理")


def test_json_output_delete_and_dry_run(monkeypatch):
    """--json：预览列出将删除条目，执行后报告实际删除数与条目详情。"""
    import json as _json

    fake = _FakeStore(
        [_trip("trip_1"), _trip("trip_2"), _trip("trip_3", destination="杭州")]
    )
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda user_id, path: fake
    )
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_trips_dedupe_standalone("u1", dry_run=True, as_json=True)
    payload = _json.loads(out.getvalue())
    assert code == 0
    assert payload["ok"] is True and payload["dry_run"] is True
    assert payload["would_remove"] == 1
    assert payload["duplicates"][0]["trip_id"] == "trip_2"
    assert payload["duplicates"][0]["origin"] == "北京"
    assert payload["duplicates"][0]["destination"] == "上海"
    assert fake.deleted == []

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_trips_dedupe_standalone("u1", as_json=True)
    payload = _json.loads(out.getvalue())
    assert code == 0
    assert payload["dry_run"] is False and payload["removed"] == 1
    assert payload["duplicates"][0]["trip_id"] == "trip_2"
    assert payload["ambiguous_ids"] == []
    assert fake.deleted == ["trip_2"]


def test_json_output_no_duplicates(monkeypatch):
    import json as _json

    fake = _FakeStore([])
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda user_id, path: fake
    )
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_trips_dedupe_standalone("u1", as_json=True)
    payload = _json.loads(out.getvalue())
    assert code == 0
    assert payload == {
        "ok": True,
        "user_id": "u1",
        "dry_run": False,
        "total_trips": 0,
        "duplicates": [],
        "ambiguous_ids": [],
        "removed": 0,
    }


def test_json_output_errors_and_ambiguous(monkeypatch):
    import json as _json

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_trips_dedupe_standalone("../bad", as_json=True)
    payload = _json.loads(out.getvalue())
    assert code == 1 and payload["ok"] is False and "用户 ID" in payload["error"]

    fake = _FakeStore(
        [_trip("trip_1"), _trip("trip_2"), _trip("trip_2", destination="杭州")]
    )
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda user_id, path: fake
    )
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_trips_dedupe_standalone("u1", dry_run=True, as_json=True)
    payload = _json.loads(out.getvalue())
    assert payload["ambiguous_ids"] == ["trip_2"]
    assert payload["would_remove"] == 0
    assert fake.deleted == []

    def boom(user_id, path):
        raise RuntimeError("cannot open")

    monkeypatch.setattr(storage, "create_long_term_memory", boom)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_trips_dedupe_standalone("u1", as_json=True)
    payload = _json.loads(out.getvalue())
    assert code == 1 and "打开长期记忆失败" in payload["error"]
    print("✓ trips-dedupe --json 错误 / 歧义 id")


def test_dispatch_json_flag(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli,
        "run_trips_dedupe_standalone",
        lambda user_id, dry_run=False, as_json=False: (
            calls.append((user_id, dry_run, as_json)) or 0
        ),
    )
    for argv, expected in (
        (["cli.py", "trips-dedupe", "u1"], ("u1", False, False)),
        (
            ["cli.py", "trips-dedupe", "u1", "--json", "--dry-run"],
            ("u1", True, True),
        ),
        (
            ["cli.py", "trips-dedupe", "u1", "--dry-run", "--JSON"],
            ("u1", True, True),
        ),
    ):
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0, argv
        assert calls[-1] == expected, argv
    for argv in (
        ["cli.py", "trips-dedupe"],
        ["cli.py", "trips-dedupe", "--json"],
        ["cli.py", "trips-dedupe", "u1", "--bad"],
    ):
        monkeypatch.setattr(sys, "argv", argv)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2, argv
        assert "--json" in buf.getvalue()
    print("✓ trips-dedupe dispatch --json / --dry-run 参数")


def test_same_trip_id_duplicates_keep_earliest_row(monkeypatch):
    """旧版重复 trip_id 的两条行程：删除最后插入的一条、保留最早记录。"""

    class _DupStore(_FakeStore):
        def delete_duplicate_trip(self, trip_id):
            for index in range(len(self.trips) - 1, -1, -1):
                if self.trips[index]["trip_id"] == trip_id:
                    self.trips.pop(index)
                    self.deleted.append(trip_id)
                    return True
            return False

    earliest = {**_trip("trip_1"), "purpose": "最早"}
    later = {**_trip("trip_1"), "purpose": "后来"}
    fake = _DupStore([earliest, later])
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda user_id, path: fake
    )
    code, out = _run("u-dup")
    assert code == 0
    assert fake.deleted == ["trip_1"]
    assert len(fake.trips) == 1
    assert fake.trips[0] == earliest, "必须保留最早记录，不能删掉它留下后改的重复行"
    assert "删除 1 条重复行程" in out
    print("✓ 同 trip_id 重复保留最早行（删除最后插入的一条）")


def test_delete_duplicate_trip_json_backend_keeps_earliest(tmp_path):
    """JSON 后端 delete_duplicate_trip：删除最后一条同 id 行、统计同步。"""
    import json

    from context.long_term_memory import LongTermMemory

    path = tmp_path / "u-dup-real.json"
    path.write_text(
        json.dumps(
            {
                "trip_history": [
                    {
                        "trip_id": "trip_9",
                        "origin": "北京",
                        "destination": "上海",
                        "start_date": "2026-12-01",
                        "purpose": "最早",
                    },
                    {
                        "trip_id": "trip_9",
                        "origin": "北京",
                        "destination": "上海",
                        "start_date": "2026-12-01",
                        "purpose": "后来",
                    },
                ],
                "statistics": {"total_trips": 2, "frequent_destinations": {"上海": 2}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store = LongTermMemory("u-dup-real", str(tmp_path))
    assert store.delete_duplicate_trip("trip_9") is True
    remaining = store.get_trip_history(limit=None)
    assert [item["purpose"] for item in remaining] == ["最早"]
    stats = store.get_statistics()
    assert stats["total_trips"] == 1
    assert stats["frequent_destinations"] == {"上海": 1}
    print("✓ JSON delete_duplicate_trip 删除最后一条并同步统计")


if __name__ == "__main__":
    for case in (
        test_dedupes_and_keeps_first,
        test_undated_trips_not_deduped,
        test_no_trips,
        test_invalid_user_id,
        test_read_failure,
        test_store_open_failure,
        test_skips_group_when_keeper_deleted_concurrently,
        test_json_backend_reloads_before_keeper_recheck,
        test_delete_failure_returns_1,
        test_pg_configured_but_json_fallback,
        test_dry_run_does_not_delete,
        test_main_rejects_unknown_flags,
        test_ambiguous_trip_id_is_not_deleted,
        test_one_day_trips_use_start_as_end,
        test_json_output_delete_and_dry_run,
        test_json_output_no_duplicates,
        test_json_output_errors_and_ambiguous,
        test_dispatch_json_flag,
        test_same_trip_id_duplicates_keep_earliest_row,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    print("\n全部通过")
