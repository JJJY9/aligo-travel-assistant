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


def _run(
    user_id,
    target=None,
    to_stdout=False,
    force=False,
    ics=False,
    query="",
    html=False,
    status="",
):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.run_trips_export_standalone(
            user_id,
            target=target,
            to_stdout=to_stdout,
            force=force,
            ics=ics,
            query=query,
            html=html,
            status=status,
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


def test_export_write_failure_reports(monkeypatch):
    """父路径被文件占用时 trips-export 写入失败明确报错。"""
    import tempfile

    workdir = tempfile.mkdtemp()
    blocker = os.path.join(workdir, "blocker")
    with io.open(blocker, "w", encoding="utf-8") as handle:
        handle.write("x")
    fake = _FakeStore([_trip()])
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *a: fake)
    code, out = _run("u1", target=os.path.join(blocker, "trips.csv"))
    assert code == 1 and "写入失败" in out
    print("✓ trips-export 写入失败友好报错")


def test_export_ics_stdout_writes_utf8_bytes(monkeypatch):
    """--ics --stdout 走真实字节输出路径（sys.stdout.buffer），不丢行尾。"""

    class _BytesStdout:
        def __init__(self):
            self.buffer = io.BytesIO()
            self.flushes = 0

        def flush(self):
            self.flushes += 1

        def fileno(self):
            return 1

    fake = _FakeStore([_trip()])
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *a: fake)
    real_stdout = sys.stdout
    sink = _BytesStdout()
    monkeypatch.setattr(sys, "stdout", sink)
    code = cli.run_trips_export_standalone("u1", to_stdout=True, ics=True)
    data = sink.buffer.getvalue().decode("utf-8")
    flushes = sink.flushes
    monkeypatch.setattr(sys, "stdout", real_stdout)
    assert code == 0
    assert data.startswith("BEGIN:VCALENDAR") and "BEGIN:VEVENT" in data
    assert flushes >= 1
    print("✓ trips-export --ics --stdout 字节输出")


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
    path = os.path.join(workdir, "exports", "aligo_u1_trips.csv")
    assert os.path.exists(path)

    code, out = _run("u1")
    assert code == 1 and "文件已存在" in out
    code, out = _run("u1", force=True)
    assert code == 0
    print("✓ 默认路径 exports/aligo_u1_trips.csv 与覆盖保护")


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


def test_export_ics_count_ignores_field_text(monkeypatch):
    """行程字段含 BEGIN:VEVENT 时导出条数按整行统计，不放大也不虚报跳过。"""
    import tempfile

    workdir = tempfile.mkdtemp()
    fake = _FakeStore([_trip()])
    fake.trips[0]["purpose"] = "会议 BEGIN:VEVENT 演练"
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *a: fake)
    target = os.path.join(workdir, "trips.ics")
    code, out = _run("u1", target=target, ics=True)
    assert code == 0
    assert "已导出 1 条行程" in out, out
    assert "跳过" not in out, "不得虚报跳过"
    print("✓ ICS 导出条数按整行统计")


def test_export_ics(monkeypatch):
    import tempfile

    workdir = tempfile.mkdtemp()
    monkeypatch.chdir(workdir)
    fake = _FakeStore(
        [_trip("上海"), _trip("杭州", "2026-12-09", "2026-12-10"), {"destination": "无日期"}]
    )
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args: fake)

    code, out = _run("u1", ics=True)
    assert code == 0 and "iCalendar" in out
    path = os.path.join(workdir, "exports", "aligo_u1_trips.ics")
    assert os.path.exists(path)
    text = open(path, encoding="utf-8").read()
    assert text.startswith("BEGIN:VCALENDAR")
    assert text.count("BEGIN:VEVENT") == 2, "缺少出发日期的行程应跳过"
    assert "DTSTART;VALUE=DATE:20261205" in text

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.run_trips_export_standalone("u1", to_stdout=True, ics=True)
    assert code == 0 and "BEGIN:VCALENDAR" in buf.getvalue()
    print("✓ trips-export --ics 导出日历（默认路径 / stdout / 跳过无日期行程）")


def test_export_query_filter(monkeypatch):
    import tempfile

    fake = _FakeStore([_trip("上海"), _trip("杭州", "2026-12-09", "2026-12-10")])
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args: fake)
    target = os.path.join(tempfile.mkdtemp(), "sh.csv")
    code, out = _run("u1", target=target, query="上海")
    assert code == 0
    assert "已导出 1 条行程" in out and "筛选「上海」" in out
    with open(target, encoding="utf-8") as handle:
        text = handle.read()
    assert "上海" in text and "杭州" not in text

    code, out = _run("u1", target=target, force=True, query="不存在")
    assert code == 0 and "已导出 0 条行程" in out
    print("✓ trips-export --query 过滤后导出")


def test_parse_export_args_ics_flag(monkeypatch):
    assert cli._parse_export_args(["--ics"], allow_ics=True) == (
        None,
        False,
        False,
        True,
        "",
        "",
    )
    assert cli._parse_export_args(["out.ics", "--ics"], allow_ics=True) == (
        "out.ics",
        False,
        False,
        True,
        "",
        "",
    )
    assert cli._parse_export_args(["--ics"]) is None, "sessions-export 不接受 --ics"
    assert cli._parse_export_args(
        ["--ics", "--query", "上海"], allow_ics=True
    ) == (None, False, False, True, "上海", "")
    assert cli._parse_export_args(
        ["--query=会议"], allow_ics=True
    ) == (None, False, False, False, "会议", "")
    assert cli._parse_export_args(["--query", "--ics"], allow_ics=True) is None
    assert cli._parse_export_args(["--query"]) is None
    assert cli._parse_export_args(["--query", "x"]) is None, "sessions-export 不接受 --query"
    print("✓ --ics / --query 只在 trips-export 中接受")


def test_main_dispatch_ics(monkeypatch):
    import tempfile

    workdir = tempfile.mkdtemp()
    monkeypatch.chdir(workdir)
    fake = _FakeStore([_trip()])
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args: fake)
    monkeypatch.setattr(
        sys, "argv", ["cli.py", "trips-export", "u1", "--ics", "--force"]
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert os.path.exists(os.path.join(workdir, "exports", "aligo_u1_trips.ics"))
    print("✓ dispatch 层 trips-export --ics 正常执行")


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
    assert os.path.exists(os.path.join(workdir, "exports", "aligo_u1_trips.csv"))
    print("✓ dispatch 层 trips-export 正常执行")


def test_export_html(monkeypatch):
    """--html 导出台账：可打印页面、内容全部转义。"""
    import tempfile

    workdir = tempfile.mkdtemp()
    fake = _FakeStore([_trip(destination="<上海>")])
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *a: fake)
    target = os.path.join(workdir, "trips.html")
    code, out = _run("u1", target=target, html=True)
    assert code == 0 and "（HTML）" in out
    content = io.open(target, encoding="utf-8").read()
    assert content.startswith("<!doctype html>")
    assert "&lt;上海&gt;" in content and "<上海>" not in content
    print("✓ trips-export --html 台账导出")


def test_main_dispatch_html_and_conflict(monkeypatch):
    """dispatch 层 --html 正常执行；与 --ics 互斥返回用法错误。"""
    import tempfile

    workdir = tempfile.mkdtemp()
    monkeypatch.chdir(workdir)
    fake = _FakeStore([_trip()])
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *a: fake)
    monkeypatch.setattr(
        sys, "argv", ["cli.py", "trips-export", "u1", "--html", "--force"]
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert os.path.exists(os.path.join(workdir, "exports", "aligo_u1_trips.html"))
    monkeypatch.setattr(
        sys, "argv", ["cli.py", "trips-export", "u1", "--html", "--ics"]
    )
    with contextlib.redirect_stdout(io.StringIO()), pytest.raises(
        SystemExit
    ) as conflict:
        cli.main()
    assert conflict.value.code == 2
    print("✓ dispatch 层 --html 正常 / 与 --ics 互斥")


def test_main_dispatch_query(monkeypatch):
    import tempfile

    workdir = tempfile.mkdtemp()
    monkeypatch.chdir(workdir)
    fake = _FakeStore([_trip("上海"), _trip("杭州", "2026-12-09", "2026-12-10")])
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args: fake)
    monkeypatch.setattr(
        sys, "argv", ["cli.py", "trips-export", "u1", "--query", "上海"]
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    path = os.path.join(workdir, "exports", "aligo_u1_trips.csv")
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    assert "上海" in text and "杭州" not in text
    print("✓ dispatch 层 trips-export --query 过滤导出")


def test_main_dispatch_status(monkeypatch):
    """main() 按 6 元组解包 --status 并传给 standalone（过滤 + 状态提示）。"""
    import datetime as dt
    import tempfile

    today = dt.date.today()

    def _d(days):
        return (today + dt.timedelta(days=days)).isoformat()

    workdir = tempfile.mkdtemp()
    monkeypatch.chdir(workdir)
    fake = _FakeStore(
        [
            _trip("上海", _d(-10), _d(-9)),  # 已结束
            _trip("杭州", _d(-1), _d(1)),  # 进行中
            _trip("广州", _d(10), _d(11)),  # 即将出行
        ]
    )
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args: fake)
    target = os.path.join(workdir, "trips.csv")
    monkeypatch.setattr(
        sys,
        "argv",
        ["cli.py", "trips-export", "u1", target, "--status", "past", "--force"],
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    out = buf.getvalue()
    assert "已导出 1 条行程" in out
    assert "状态「已结束」" in out
    with open(target, encoding="utf-8") as handle:
        text = handle.read()
    assert "上海" in text and "杭州" not in text and "广州" not in text
    print("✓ dispatch 层 trips-export --status 过滤导出")


def test_export_status_filter(monkeypatch):
    """trips-export --status 与面板 / trips 列表同一口径过滤。"""
    import datetime as dt
    import tempfile

    today = dt.date.today()

    def _d(days):
        return (today + dt.timedelta(days=days)).isoformat()

    fake = _FakeStore(
        [
            _trip("上海", _d(-10), _d(-9)),  # 已结束
            _trip("杭州", _d(-1), _d(1)),  # 进行中
            _trip("广州", _d(10), _d(11)),  # 即将出行
        ]
    )
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args: fake)
    target = os.path.join(tempfile.mkdtemp(), "trips.csv")
    code, out = _run("u1", target=target, status="upcoming")
    assert code == 0
    assert "已导出 1 条行程" in out and "状态「即将出行」" in out
    with open(target, encoding="utf-8") as handle:
        text = handle.read()
    assert "广州" in text and "上海" not in text and "杭州" not in text
    print("✓ trips-export 按状态过滤")


def test_export_csv_warns_about_unimportable_rows(monkeypatch):
    """CSV 含导入端会整行拒绝的脏日期：导出时明确告警，不静默丢行程。"""
    import tempfile

    fake = _FakeStore(
        [
            _trip("上海", "2026-12-05", "2026-12-07"),
            _trip("杭州", "2026-12-05", "2026/12/20"),
        ]
    )
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *a: fake)
    target = os.path.join(tempfile.mkdtemp(), "trips.csv")
    code, out = _run("u1", target=target)
    assert code == 0
    assert "回导时有 1 行会被跳过" in out, out
    print("✓ trips-export CSV 回导告警")


def test_export_ics_stdout_reports_skipped(monkeypatch):
    """--ics --stdout 跳过无出发日期行程时向 stderr 提示，而不是静默空日历。"""

    class _BytesStdout:
        def __init__(self):
            self.buffer = io.BytesIO()

        def flush(self):
            pass

        def fileno(self):
            return 1

    fake = _FakeStore([_trip(), {"destination": "无日期"}])
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *a: fake)
    sink = _BytesStdout()
    err = io.StringIO()
    real_stdout = sys.stdout
    monkeypatch.setattr(sys, "stdout", sink)
    try:
        with contextlib.redirect_stderr(err):
            code = cli.run_trips_export_standalone("u1", to_stdout=True, ics=True)
    finally:
        monkeypatch.setattr(sys, "stdout", real_stdout)
    data = sink.buffer.getvalue().decode("utf-8")
    assert code == 0
    assert data.count("BEGIN:VEVENT") == 1
    assert "跳过 1 条缺少出发日期" in err.getvalue()
    print("✓ trips-export --ics --stdout 跳过提示走 stderr")


def test_export_stdout_strips_terminal_controls(monkeypatch):
    """--stdout 导出剥离终端控制序列（ESC / OSC 不送达终端）。"""
    fake = _FakeStore([_trip("上海\x1b]52;c;QUJD\x07")])
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *a: fake)
    code, out = _run("u1", to_stdout=True)
    assert code == 0
    assert "\x1b" not in out and "\x07" not in out
    assert "上海" in out
    print("✓ trips-export --stdout 剥离控制序列")


def test_export_status_arg_parsing():
    """--status 只对 trips-export 生效，非法状态按用法错误拒绝。"""
    assert cli._parse_export_args(["--status", "past"], allow_ics=True) == (
        None,
        False,
        False,
        False,
        "",
        "past",
    )
    assert cli._parse_export_args(["--status=已结束"], allow_ics=True) is not None
    assert cli._parse_export_args(["--status", "bogus"], allow_ics=True) is None
    assert cli._parse_export_args(["--status", "past"]) is None, (
        "其它导出命令不接受 --status"
    )
    print("✓ --status 参数解析")


def test_export_csv_over_import_row_limit(monkeypatch):
    """行程超过单次导入上限（5000）时 CSV 导出仍成功，而不是 traceback。"""
    import tempfile
    from pathlib import Path

    trips = [
        _trip(destination=f"城市{i}", start="2026-12-05") for i in range(5001)
    ]
    store = _FakeStore(trips)
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    target = str(Path(tempfile.mkdtemp()) / "trips.csv")
    code, out = _run("u1", target=target)
    assert code == 0, out
    path = Path(target)
    assert path.exists()
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    assert len(lines) == 5002, len(lines)  # 表头 + 5001 行
    print("✓ 行程超过导入上限时 CSV 导出仍成功")

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
        test_export_ics,
        test_export_query_filter,
        test_parse_export_args_ics_flag,
        test_main_dispatch_ics,
        test_main_dispatch_query,
        test_main_dispatch_status,
        test_export_html,
        test_main_dispatch_html_and_conflict,
        test_export_status_filter,
        test_export_csv_warns_about_unimportable_rows,
        test_export_ics_stdout_reports_skipped,
        test_export_stdout_strips_terminal_controls,
        test_export_csv_over_import_row_limit,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    test_export_status_arg_parsing()
    print("\n全部通过")
