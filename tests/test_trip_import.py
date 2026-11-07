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

import cli
from context import storage
from utils.trip_import import parse_trips_csv


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
    # 重复导入：全部命中去重，只更新目的
    code = cli.run_trips_import_standalone("u-import", path, as_json=True)
    assert code == 0
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


if __name__ == "__main__":
    import pytest

    test_parse_chinese_and_english_headers()
    test_parse_skips_invalid_rows()
    test_parse_missing_columns_and_empty()
    for case in (
        test_runner_imports_and_dedupes,
        test_runner_json_output_and_skipped,
        test_runner_errors,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    print("\n全部通过")
