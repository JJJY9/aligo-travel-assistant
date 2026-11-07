"""行程 CSV 导入测试（无需 API Key、不访问网络）。

运行：python tests/test_trip_import.py
"""
import io
import json
import os
import sys
import tempfile

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

import pytest

import cli
from context import storage
from utils.trip_export import render_trips_ics
from utils.trip_import import parse_trips_csv, parse_trips_ics


class _ImportStore:
    """内存行程存储：复刻「同路线 + 同起止日期更新目的」的去重语义。"""

    def __init__(self):
        self.trips: list[dict] = []
        self.saved: list[dict] = []
        self.closed = False

    def get_trip_history(self, limit=None):
        return [dict(trip) for trip in self.trips]

    def save_trip_history(self, trip):
        self.saved.append(dict(trip))
        key = (
            trip.get("origin"),
            trip.get("destination"),
            trip.get("start_date"),
            trip.get("end_date"),
        )
        for existing in self.trips:
            existing_key = (
                existing.get("origin"),
                existing.get("destination"),
                existing.get("start_date"),
                existing.get("end_date"),
            )
            if existing_key == key:
                existing.update(trip)
                return existing.get("trip_id")
        new = {**trip, "trip_id": f"trip_{len(self.trips) + 1}"}
        self.trips.append(new)
        return new["trip_id"]

    def close(self):
        self.closed = True


def _write_csv(text: str) -> str:
    path = os.path.join(tempfile.mkdtemp(prefix="chailv_import_"), "trips.csv")
    io.open(path, "w", encoding="utf-8-sig").write(text)
    return path


def _write_ics(text: str) -> str:
    path = os.path.join(tempfile.mkdtemp(prefix="chailv_import_"), "trips.ics")
    io.open(path, "w", encoding="utf-8-sig", newline="").write(text)
    return path


def test_parse_chinese_and_english_headers():
    rows, errors = parse_trips_csv(
        "出发地,目的地,出发日期,返程日期,目的\n北京,上海,2026-12-01,2026-12-03,出差\n"
    )
    assert not errors
    assert rows == [
        {
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-12-01",
            "end_date": "2026-12-03",
            "purpose": "出差",
        }
    ]
    english, errors_en = parse_trips_csv(
        "origin,destination,start_date,end_date,purpose\n"
        "北京,杭州,2026-12-10,,培训\n"
    )
    assert not errors_en
    assert english[0]["destination"] == "杭州" and english[0]["purpose"] == "培训"
    print("✓ 中英文字段名解析")


def test_parse_skips_invalid_rows():
    text = (
        "出发地,目的地,出发日期,返程日期,目的\n"
        "北京,上海,2026-12-01,2026-12-03,出差\n"
        ",,2026-12-10,,\n"
        "北京,广州,2026-13-01,,\n"
        "北京,深圳,2026-12-05,2026-12-04,\n"
    )
    rows, errors = parse_trips_csv(text)
    assert len(rows) == 1
    reasons = dict(errors)
    assert reasons[3] == "目的地为空"
    assert "出发日期格式" in reasons[4]
    assert "结束日期不能早于出发日期" in reasons[5]
    print("✓ 非法行收集错误而不中断")


def test_parse_missing_columns_and_empty():
    rows, errors = parse_trips_csv("出发地,目的地\n北京,上海\n")
    assert rows == [] and "缺少必需列" in errors[0][1]
    rows, errors = parse_trips_csv("")
    assert rows == [] and errors[0][1] == "文件为空"
    print("✓ 缺列 / 空文件报错")


def test_runner_imports_and_dedupes(monkeypatch):
    store = _ImportStore()
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    path = _write_csv(
        "出发地,目的地,出发日期,返程日期,目的\n"
        "北京,上海,2026-12-01,2026-12-03,出差\n"
        "北京,广州,2026-12-10,,培训\n"
    )
    code = cli.run_trips_import_standalone("u-import", path)
    assert code == 0
    assert len(store.trips) == 2
    assert store.closed
    # 重复导入：全部命中去重，只更新目的（统计字段必须真实反映）
    captured = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = captured
    try:
        code = cli.run_trips_import_standalone("u-import", path, as_json=True)
    finally:
        sys.stdout = real_stdout
    assert code == 0
    payload = json.loads(captured.getvalue())
    assert payload["created"] == 0 and payload["updated"] == 2, payload
    assert len(store.trips) == 2, "重复导入不能新增行程"
    # 字段保真：返程日期不能被导入路径丢掉
    assert store.saved[-2]["end_date"] == "2026-12-03"
    assert store.saved[-1]["end_date"] == ""
    print("✓ 导入与重复导入去重")


def test_runner_json_output_and_skipped(monkeypatch):
    store = _ImportStore()
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    path = _write_csv(
        "出发地,目的地,出发日期,返程日期,目的\n"
        "北京,上海,2026-12-01,,\n"
        "坏行,,2026-01-01,,\n"
    )
    captured = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = captured
    try:
        code = cli.run_trips_import_standalone("u-json", path, as_json=True)
    finally:
        sys.stdout = real_stdout
    assert code == 0
    payload = json.loads(captured.getvalue())
    assert payload["ok"] is True
    assert payload["rows"] == 1 and payload["created"] == 1
    assert payload["skipped"][0]["line"] == 3
    print("✓ --json 输出与跳过清单")


def test_runner_errors(monkeypatch):
    store = _ImportStore()
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    assert cli.run_trips_import_standalone("bad id!", "/tmp/x.csv") == 1
    assert cli.run_trips_import_standalone("u1", "/nonexistent/file.csv") == 1
    empty = _write_csv("出发地,目的地,出发日期,返程日期,目的\n")
    assert cli.run_trips_import_standalone("u1", empty) == 1
    print("✓ 非法用户 / 文件不存在 / 无有效行均失败退出")


def test_runner_rejects_gbk_csv(monkeypatch):
    """GBK 编码的 CSV：友好报错（--json 也有结构化输出）。"""
    store = _ImportStore()
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    path = os.path.join(tempfile.mkdtemp(prefix="chailv_import_"), "gbk.csv")
    with io.open(path, "wb") as handle:
        handle.write("出发地,目的地,出发日期\n北京,上海,2026-12-01\n".encode("gbk"))
    captured = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = captured
    try:
        code = cli.run_trips_import_standalone("u-gbk", path, as_json=True)
    finally:
        sys.stdout = real_stdout
    assert code == 1
    payload = json.loads(captured.getvalue())
    assert payload["ok"] is False and "UTF-8" in payload["error"]
    print("✓ GBK CSV 友好报错")


def test_ics_roundtrip_matches_export():
    trips = [
        {
            "trip_id": "t1",
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-11-10",
            "end_date": "2026-11-12",
            "purpose": "客户会议",
        },
        {
            "trip_id": "t2",
            "origin": "杭州",
            "destination": "成都",
            "start_date": "2026-12-01",
            "end_date": "2026-12-01",
            "purpose": "会议,对接",  # ASCII 逗号：导出转义、导入还原
        },
    ]
    rows, errors = parse_trips_ics(render_trips_ics(trips, "u1"))
    assert errors == []
    assert rows == [
        {
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-11-10",
            "end_date": "2026-11-12",
            "purpose": "客户会议",
        },
        {
            "origin": "杭州",
            "destination": "成都",
            "start_date": "2026-12-01",
            "end_date": "2026-12-01",
            "purpose": "会议,对接",
        },
    ], rows
    print("✓ ICS 导出→导入往返保真（多日 / 单日 / 转义逗号）")


def test_ics_unfold_and_lf_only():
    purpose = "超长目的说明" * 12  # > 75 字节，SUMMARY / DESCRIPTION 必然折行
    trips = [
        {
            "trip_id": "t1",
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-11-10",
            "end_date": "2026-11-10",
            "purpose": purpose,
        }
    ]
    text = render_trips_ics(trips, "u1")
    assert "\r\n " in text, "导出端应折行"
    # LF-only 的日历文件（部分工具就是这样写出的）同样可解析
    rows, errors = parse_trips_ics(text.replace("\r\n", "\n"))
    assert errors == []
    assert rows[0]["purpose"] == purpose, "折行拼接后文本必须完整"
    assert rows[0]["destination"] == "上海"
    print("✓ ICS 折行展开与 LF-only 文件")


def test_ics_collects_invalid_events():
    text = (
        "BEGIN:VCALENDAR\nVERSION:2.0\n"
        "BEGIN:VEVENT\n"
        "DTSTART;VALUE=DATE:20261110\n"
        "SUMMARY:北京 → 上海（出差）\n"
        "END:VEVENT\n"
        "BEGIN:VEVENT\n"
        "SUMMARY:没有日期的会议\n"
        "END:VEVENT\n"
        "END:VCALENDAR\n"
    )
    rows, errors = parse_trips_ics(text)
    assert len(rows) == 1 and rows[0]["destination"] == "上海"
    assert errors[0][0] == 2 and "DTSTART" in errors[0][1]

    rows, errors = parse_trips_ics("BEGIN:VCALENDAR\nEND:VCALENDAR\n")
    assert rows == [] and "未找到 VEVENT" in errors[0][1]
    print("✓ ICS 非法事件收集错误而不中断")


def test_ics_external_summary_fallback():
    """外部日历的普通事件：无「出发地 → 目的地」结构时以 SUMMARY 为目的地。"""
    text = (
        "BEGIN:VCALENDAR\nVERSION:2.0\n"
        "BEGIN:VEVENT\n"
        "DTSTART;VALUE=DATE:20261110\n"
        "DTEND;VALUE=DATE:20261111\n"
        "SUMMARY:外部日历事件\n"
        "END:VEVENT\nEND:VCALENDAR\n"
    )
    rows, errors = parse_trips_ics(text)
    assert errors == []
    assert rows == [
        {
            "origin": "",
            "destination": "外部日历事件",
            "start_date": "2026-11-10",
            "end_date": "2026-11-10",  # 排他 DTEND=次日 → 单日
            "purpose": "出差",
        }
    ], rows
    print("✓ ICS 外部事件 SUMMARY 回退")


def test_runner_imports_ics_and_dedupes(monkeypatch):
    store = _ImportStore()
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    trips = [
        {
            "trip_id": "t1",
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-12-01",
            "end_date": "2026-12-03",
            "purpose": "出差",
        }
    ]
    path = _write_ics(render_trips_ics(trips, "u-ics"))

    captured = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = captured
    try:
        code = cli.run_trips_import_standalone(
            "u-ics", path, as_json=True, as_ics=True
        )
    finally:
        sys.stdout = real_stdout
    payload = json.loads(captured.getvalue())
    assert code == 0 and payload["format"] == "ics"
    assert payload["created"] == 1 and payload["skipped"] == []

    # 不传 --ics 时按内容（BEGIN:VCALENDAR）自动识别；重复导入只更新
    captured = io.StringIO()
    sys.stdout = captured
    try:
        code = cli.run_trips_import_standalone("u-ics", path, as_json=True)
    finally:
        sys.stdout = real_stdout
    payload = json.loads(captured.getvalue())
    assert code == 0 and payload["format"] == "ics"
    assert payload["created"] == 0 and payload["updated"] == 1
    assert len(store.trips) == 1 and store.saved[-1]["end_date"] == "2026-12-03"
    print("✓ ICS 导入 + 自动识别 + 去重")


def test_runner_dispatch_ics(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli,
        "run_trips_import_standalone",
        lambda user_id, path, as_json=False, as_ics=False: (
            calls.append((user_id, path, as_json, as_ics)) or 0
        ),
    )
    monkeypatch.setattr(
        sys, "argv", ["cli.py", "trips-import", "u1", "x.ics", "--ics", "--json"]
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert calls[-1] == ("u1", "x.ics", True, True)

    monkeypatch.setattr(sys, "argv", ["cli.py", "trips-import", "u1", "--ics"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    print("✓ trips-import dispatch --ics")


if __name__ == "__main__":
    test_parse_chinese_and_english_headers()
    test_parse_skips_invalid_rows()
    test_parse_missing_columns_and_empty()
    test_ics_roundtrip_matches_export()
    test_ics_unfold_and_lf_only()
    test_ics_collects_invalid_events()
    test_ics_external_summary_fallback()
    for case in (
        test_runner_imports_and_dedupes,
        test_runner_json_output_and_skipped,
        test_runner_errors,
        test_runner_rejects_gbk_csv,
        test_runner_imports_ics_and_dedupes,
        test_runner_dispatch_ics,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    print("\n全部通过")
