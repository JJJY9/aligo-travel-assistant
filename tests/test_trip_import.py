"""行程 CSV 导入测试（无需 API Key、不访问网络）。

运行：python tests/test_trip_import.py\n"""
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

    @staticmethod
    def _key(trip):
        start = trip.get("start_date") or ""
        return (
            trip.get("origin"),
            trip.get("destination"),
            start,
            trip.get("end_date") or start,
        )

    def save_trip_history(self, trip):
        self.saved.append(dict(trip))
        key = self._key(trip)
        for existing in self.trips:
            if self._key(existing) == key:
                existing.update(trip)
                return existing.get("trip_id")
        new = {**trip, "trip_id": f"trip_{len(self.trips) + 1}"}
        self.trips.append(new)
        return new["trip_id"]

    def save_trip_histories(self, rows):
        created = updated = 0
        for row in rows:
            hit = any(self._key(existing) == self._key(row) for existing in self.trips)
            self.save_trip_history(row)
            if hit:
                updated += 1
            else:
                created += 1
        return created, updated

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


def test_csv_malformed_quote_reports_error_instead_of_merging():
    """引号错位时非严格模式会把后续行静默合并成一条记录、零错误导入：

    strict=True 必须改为带行号的解析错误，已解析的有效行保留。
    """
    text = (
        "出发地,目的地,出发日期\n"
        "北京,上海,2026-12-01\n"
        'C,"D,2026-01-02\n'
        'X,"Y,2026-01-03"\n'
    )
    rows, errors = parse_trips_csv(text)
    assert [row["destination"] for row in rows] == ["上海"], rows
    assert errors, "引号错位必须报错，不能把两行静默合并成一条行程"
    assert "CSV 解析失败" in errors[0][1], errors
    print("✓ CSV 引号错位报错而非静默合并")


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


def test_runner_dry_run_preview(monkeypatch):
    store = _ImportStore()
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    rows_text = chr(10).join([
        "出发地,目的地,出发日期,返程日期,目的",
        "北京,上海,2026-12-01,2026-12-03,出差",
        "北京,广州,2026-12-10,,培训",
        "",
    ])
    path = _write_csv(rows_text)

    captured = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = captured
    try:
        code = cli.run_trips_import_standalone(
            "u-dry", path, as_json=True, dry_run=True
        )
    finally:
        sys.stdout = real_stdout
    payload = json.loads(captured.getvalue())
    assert code == 0 and payload["dry_run"] is True
    assert payload["created"] == 2 and payload["updated"] == 0
    assert store.trips == [] and store.saved == [], "预览不得写库"

    captured = io.StringIO()
    sys.stdout = captured
    try:
        code = cli.run_trips_import_standalone("u-dry", path, as_json=True)
    finally:
        sys.stdout = real_stdout
    real = json.loads(captured.getvalue())
    assert real["dry_run"] is False
    assert real["created"] == payload["created"]
    assert real["updated"] == payload["updated"]
    assert len(store.trips) == 2

    captured = io.StringIO()
    sys.stdout = captured
    try:
        code = cli.run_trips_import_standalone(
            "u-dry", path, as_json=True, dry_run=True
        )
    finally:
        sys.stdout = real_stdout
    again = json.loads(captured.getvalue())
    assert again["created"] == 0 and again["updated"] == 2
    print("✓ trips-import --dry-run 预览且与真实导入一致")


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


def test_runner_rejects_oversized_file_before_read(monkeypatch):
    """读入前按文件大小拒绝：误选大文件不先占满内存再报错。"""
    store = _ImportStore()
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    monkeypatch.setattr(cli.os.path, "getsize", lambda path: 26 * 1024 * 1024)
    captured = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = captured
    try:
        code = cli.run_trips_import_standalone(
            "u-big", "/tmp/whatever.csv", as_json=True
        )
    finally:
        sys.stdout = real_stdout
    assert code == 1
    payload = json.loads(captured.getvalue())
    assert payload["ok"] is False and "过大" in payload["error"]
    print("✓ 超限文件在读入前拒绝")


def test_runner_reports_partial_import(monkeypatch):
    """批量写入中途失败：报告已提交条数（重跑按同路线同日期去重，安全）。"""
    from utils.errors import PartialImportError

    store = _ImportStore()

    def failing_save(rows):
        raise PartialImportError("批量保存在第 2 条后失败：db down", 1, 1)

    store.save_trip_histories = failing_save
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    path = _write_csv(
        "出发地,目的地,出发日期,返程日期,目的\n"
        "北京,上海,2026-12-01,2026-12-02,出差\n"
        "北京,杭州,2026-12-03,2026-12-04,出差\n"
    )
    captured = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = captured
    try:
        code = cli.run_trips_import_standalone("u-partial", path, as_json=True)
    finally:
        sys.stdout = real_stdout
    payload = json.loads(captured.getvalue())
    assert code == 1
    assert payload["ok"] is False
    assert "已提交 1 条新增、1 条更新" in payload["error"]
    print("✓ 部分导入失败报告已提交条数")


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


def test_ics_roundtrip_escapes_machine_delimiters():
    """出发地 / 目的地含箭头或全角逗号时，DESCRIPTION 机器段仍可原样回导。"""
    trip = {
        "trip_id": "t1",
        "origin": "北京 → 天津",
        "destination": "上海，浦东",
        "start_date": "2026-12-01",
        "end_date": "2026-12-02",
        "purpose": "出差",
    }
    text = render_trips_ics([trip], "u1")
    rows, errors = parse_trips_ics(text)
    assert errors == [], errors
    assert len(rows) == 1
    assert rows[0]["origin"] == "北京 → 天津", rows[0]
    assert rows[0]["destination"] == "上海，浦东", rows[0]
    assert rows[0]["start_date"] == "2026-12-01"
    print("✓ ICS 往返：字段中的箭头 / 全角逗号不再被拆错")


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


def test_ics_invalid_utc_offset_does_not_abort():
    """偏移超出 ±23:59（timezone() 会 ValueError）按浮动时间处理，不中断导入。"""
    text = (
        "BEGIN:VCALENDAR\n"
        "BEGIN:VEVENT\n"
        "DTSTART:20261110T090000+9999\n"
        "SUMMARY:坏偏移事件\n"
        "END:VEVENT\n"
        "BEGIN:VEVENT\n"
        "DTSTART;VALUE=DATE:20261112\n"
        "SUMMARY:杭州 → 成都（会议）\n"
        "END:VEVENT\n"
        "END:VCALENDAR\n"
    )
    rows, errors = parse_trips_ics(text)
    assert errors == [], errors
    assert [row["start_date"] for row in rows] == ["2026-11-10", "2026-11-12"]
    print("✓ ICS 非法 UTC 偏移不再中断整份导入")


def test_ics_tzid_parameter_matches_utc_representation():
    """TZID 参数必须参与换算：Outlook 写法与同一时刻的 UTC 写法同一天。"""
    template = (
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\n{line}\n"
        "SUMMARY:北京 → 上海（出差）\n"
        "END:VEVENT\nEND:VCALENDAR\n"
    )
    utc_rows, utc_errors = parse_trips_ics(
        template.format(line="DTSTART:20261111T043000Z")
    )
    tz_rows, tz_errors = parse_trips_ics(
        template.format(line="DTSTART;TZID=America/New_York:20261110T233000")
    )
    assert utc_errors == [] and tz_errors == []
    assert tz_rows[0]["start_date"] == utc_rows[0]["start_date"], (
        "同一时刻的 TZID 写法与 UTC 写法必须换算到同一天"
    )

    # 未知 TZID 按浮动时间处理，不中断解析
    bad_rows, bad_errors = parse_trips_ics(
        template.format(line="DTSTART;TZID=Not/AZone:20261110T233000")
    )
    assert bad_errors == []
    assert bad_rows[0]["start_date"] == "2026-11-10"
    print("✓ ICS TZID 参数参与时区换算")


def test_csv_oversized_field_returns_error_and_keeps_rows():
    """单字段超过 csv 上限：按解析错误返回，已解析行保留而不是 traceback。"""
    import csv as csv_module

    huge = "x" * (csv_module.field_size_limit() + 10)
    text = (
        "出发地,目的地,出发日期\n"
        "北京,上海,2026-01-01\n"
        '北京,"' + huge + '",2026-01-02\n'
    )
    rows, errors = parse_trips_csv(text)
    assert [row["destination"] for row in rows] == ["上海"], "已解析行必须保留"
    assert any("CSV 解析失败" in message for _, message in errors), errors
    print("✓ 超长 CSV 字段按解析错误返回（不再整条命令 traceback）")

def test_trip_identity_normalizes_unpadded_dates():
    """去重键对 YYYY-M-D 补零：历史未补零行程与补零后的导入视为同一行程。"""
    from utils.trip_import import trip_identity

    unpadded = trip_identity(
        {"origin": "北京", "destination": "上海", "start_date": "2026-1-5"}
    )
    padded = trip_identity(
        {
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-01-05",
            "end_date": "2026-1-5",
        }
    )
    assert unpadded == padded
    # 非法数字形态（越界月日）保持原样，不做“修正”
    assert trip_identity({"start_date": "2026-13-40"})[2] == "2026-13-40"
    print("✓ 去重键归一未补零日期")


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
            # 外部事件没有目的：保留 None，去重更新时不覆盖已有目的，
            # 新建行程由导入助手补默认「出差」
            "purpose": None,
        }
    ], rows
    print("✓ ICS 外部事件 SUMMARY 回退")


def test_ics_valarm_before_properties_keeps_event_description():
    """VALARM 在事件属性之前时，闹钟文本不能顶替事件级 DESCRIPTION。"""
    text = (
        "BEGIN:VCALENDAR\nVERSION:2.0\n"
        "BEGIN:VEVENT\n"
        "BEGIN:VALARM\nACTION:DISPLAY\nDESCRIPTION:出行提醒\nEND:VALARM\n"
        "DTSTART;VALUE=DATE:20261205\n"
        "DTEND;VALUE=DATE:20261208\n"
        "SUMMARY:Team offsite\n"
        "DESCRIPTION:Aligo 行程：北京 → 上海，2026-12-05 ~ 2026-12-07，出差\n"
        "END:VEVENT\nEND:VCALENDAR\n"
    )
    rows, errors = parse_trips_ics(text)
    assert errors == [], errors
    assert len(rows) == 1, rows
    assert rows[0]["origin"] == "北京", rows[0]
    assert rows[0]["destination"] == "上海", "事件级描述不能被闹钟文本顶替"
    assert rows[0]["purpose"] == "出差", rows[0]
    print("✓ VALARM 在属性前时事件级描述不被闹钟文本顶替")


def test_ics_structure_errors_do_not_duplicate_events():
    """多余 END / 嵌套 BEGIN / 缺少 END 不能静默重复或丢失事件。"""
    base = (
        "BEGIN:VEVENT\nDTSTART;VALUE=DATE:20261110\n"
        "SUMMARY:北京 → 上海（出差）\nEND:VEVENT\n"
    )
    rows, errors = parse_trips_ics(base + "END:VEVENT\n")
    assert len(rows) == 1, "多余的 END 不能拿上一份属性再生成一条"
    assert any("多余的 END:VEVENT" in reason for _, reason in errors), errors

    nested = (
        "BEGIN:VEVENT\nDTSTART;VALUE=DATE:20261110\n"
        "BEGIN:VEVENT\nDTSTART;VALUE=DATE:20261111\n"
        "SUMMARY:北京 → 上海（出差）\nEND:VEVENT\nEND:VEVENT\n"
    )
    rows, errors = parse_trips_ics(nested)
    assert len(rows) == 1 and rows[0]["start_date"] == "2026-11-11", rows
    assert any("事件嵌套" in reason for _, reason in errors), errors

    rows, errors = parse_trips_ics(
        "BEGIN:VEVENT\nDTSTART;VALUE=DATE:20261110\n"
    )
    assert rows == []
    assert any("缺少 END:VEVENT" in reason for _, reason in errors), errors
    print("✓ ICS 结构错误不重复 / 不丢事件")


def test_ics_quoted_tzid_parameter_is_unquoted():
    """带引号的 TZID（TZID="UTC"）按引号内值查询时区，不按未知时区浮动。"""
    text = (
        "BEGIN:VEVENT\n"
        'DTSTART;TZID="UTC":20261205T120000\n'
        "SUMMARY:北京 → 上海（出差）\n"
        "END:VEVENT\n"
    )
    rows, errors = parse_trips_ics(text)
    assert errors == [], errors
    assert rows[0]["start_date"] == "2026-12-05", rows
    print("✓ 带引号 TZID 参数被正确解析")


def test_ics_roundtrip_unparsable_end_uses_machine_description():
    """返程日期无法解析：DESCRIPTION 用 DTEND 的实际单日日期，回导走机器段。"""
    trip = {
        "trip_id": "t-bad-end",
        "origin": "",
        "destination": "上海",
        "start_date": "2026-12-05",
        "end_date": "待定",
        "purpose": "出差",
    }
    text = render_trips_ics([trip], "u1")
    rows, errors = parse_trips_ics(text)
    assert errors == [], errors
    assert len(rows) == 1, rows
    # 走 DESCRIPTION 机器段：空出发地回导后仍为空（SUMMARY 回退会变成「出发地」）
    assert rows[0]["origin"] == "", rows[0]
    assert rows[0]["destination"] == "上海", rows[0]
    assert rows[0]["start_date"] == "2026-12-05"
    assert rows[0]["end_date"] == "2026-12-05", "返程按 DTEND 的单日口径回导"
    assert rows[0]["purpose"] == "出差"
    print("✓ 无法解析的返程日期往返后身份一致（不退化为 SUMMARY 回退）")


def test_ics_empty_origin_roundtrip():
    """空出发地按原值写入 DESCRIPTION：往返后不能变成「出发地」占位行程。"""
    trips = [
        {
            "trip_id": "t1",
            "origin": "",
            "destination": "上海",
            "start_date": "2026-12-01",
            "end_date": "2026-12-01",
            "purpose": "出差",
        }
    ]
    text = render_trips_ics(trips, "u1")
    assert "Aligo 行程： → 上海" in text, "DESCRIPTION 保留空出发地"
    assert "出发地 → 上海" in text, "SUMMARY 仍是给人看的占位文案"
    rows, errors = parse_trips_ics(text)
    assert errors == []
    assert rows[0]["origin"] == "", rows[0]
    assert rows[0]["destination"] == "上海"
    print("✓ ICS 空出发地往返保真（不产生「出发地」占位）")


def test_ics_datetime_offset_converts_local_date():
    """Z / ±HHMM 的 DATE-TIME 先换算本机时区再取日期，不能直接截前 8 位。"""
    from datetime import datetime, timedelta, timezone

    text = (
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
        "DTSTART:20261110T233000-0500\n"
        "DTEND:20261111T013000-0500\n"
        "SUMMARY:越洋航班\n"
        "END:VEVENT\nEND:VCALENDAR\n"
    )
    rows, errors = parse_trips_ics(text)
    assert errors == []
    start_local = datetime(2026, 11, 10, 23, 30, tzinfo=timezone(-timedelta(hours=5)))
    start_local = start_local.astimezone()
    end_local = datetime(2026, 11, 11, 1, 30, tzinfo=timezone(-timedelta(hours=5)))
    end_local = end_local.astimezone()
    assert rows[0]["start_date"] == start_local.strftime("%Y-%m-%d"), rows[0]
    expected_end = end_local.date()
    if expected_end < start_local.date():
        expected_end = start_local.date()
    assert rows[0]["end_date"] == expected_end.strftime("%Y-%m-%d"), rows[0]

    zulu = (
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
        "DTSTART:20261110T233000Z\n"
        "SUMMARY:UTC 事件\n"
        "END:VEVENT\nEND:VCALENDAR\n"
    )
    rows, errors = parse_trips_ics(zulu)
    assert errors == []
    expected = datetime(2026, 11, 10, 23, 30, tzinfo=timezone.utc).astimezone()
    assert rows[0]["start_date"] == expected.strftime("%Y-%m-%d")
    print("✓ ICS 带时区 DATE-TIME 按本机日期导入")


def test_ics_timed_end_inclusive_except_midnight():
    """DATE-TIME 的 DTEND 是含尾时刻：只有恰好 00:00 才按排他日期减一天。"""
    def event(start: str, end: str) -> str:
        return (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
            f"DTSTART:{start}\nDTEND:{end}\n"
            "SUMMARY:客户会议\n"
            "END:VEVENT\nEND:VCALENDAR\n"
        )

    rows, _ = parse_trips_ics(event("20261110T090000", "20261112T180000"))
    assert rows[0]["end_date"] == "2026-11-12", "跨日定时事件不能被减一天"

    rows, _ = parse_trips_ics(event("20261110T230000", "20261111T060000"))
    assert rows[0]["end_date"] == "2026-11-11", "红眼航班按落地日结束"

    rows, _ = parse_trips_ics(event("20261110T090000", "20261110T180000"))
    assert rows[0]["end_date"] == "2026-11-10", "同日定时事件当天结束"

    rows, _ = parse_trips_ics(event("20261110T090000", "20261111T000000"))
    assert rows[0]["end_date"] == "2026-11-10", "恰为 00:00 仍按排他日期处理"
    print("✓ ICS DATE-TIME 结束时刻含尾，仅 00:00 排他")


def test_csv_formula_guard_roundtrip():
    """公式注入防护前导引号在导入端还原，含已有引号的值也可逆。"""
    from utils.trip_export import render_trips_csv

    trips = [
        {
            "origin": "-北京",
            "destination": "@上海",
            "start_date": "2026-12-01",
            "end_date": "",
            "purpose": "=1+1",
        },
        {
            "origin": "'@已有引号",
            "destination": "+86 广州",
            "start_date": "2026-12-02",
            "end_date": "",
            "purpose": "\t制表符开头",
        },
        {
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-12-03",
            "end_date": "",
            "purpose": "普通行程",
        },
    ]
    rows, errors = parse_trips_csv(render_trips_csv(trips))
    assert errors == []
    for original, restored in zip(trips, rows):
        for field in ("origin", "destination", "start_date", "end_date", "purpose"):
            assert restored[field] == original[field], (field, restored, original)
    print("✓ CSV 公式防护前缀往返还原（含已有引号）")


def test_csv_error_line_numbers_physical():
    """引号包裹的多行字段之后，报错行号必须是物理行号。"""
    text = (
        "出发地,目的地,出发日期,返程日期,目的\n"
        '北京,上海,2026-12-01,,"第一行\n第二行"\n'
        "北京,广州,2026-13-01,,\n"
    )
    rows, errors = parse_trips_csv(text)
    assert len(rows) == 1
    assert errors == [(4, "出发日期格式应为 YYYY-MM-DD：2026-13-01")], errors
    print("✓ CSV 报错行号为物理行号")


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
        lambda user_id, path, as_json=False, as_ics=False, dry_run=False: (
            calls.append((user_id, path, as_json, as_ics, dry_run)) or 0
        ),
    )
    monkeypatch.setattr(
        sys, "argv", ["cli.py", "trips-import", "u1", "x.ics", "--ics", "--json"]
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert calls[-1] == ("u1", "x.ics", True, True, False)

    monkeypatch.setattr(
        sys, "argv", ["cli.py", "trips-import", "u1", "x.ics", "--dry-run"]
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert calls[-1] == ("u1", "x.ics", False, False, True)

    monkeypatch.setattr(sys, "argv", ["cli.py", "trips-import", "u1", "--ics"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    print("✓ trips-import dispatch --ics / --dry-run")


def test_ics_extreme_year_datetime_skipped():
    """可解析但换算越界的 DATE-TIME（0001-01-01+1400）逐事件跳过不崩溃。"""
    from utils.trip_import import parse_trips_ics

    ics = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "BEGIN:VEVENT",
            "UID:extreme-1",
            "SUMMARY:北京 → 上海（出差）",
            "DTSTART:00010101T000000+1400",
            "DTEND:00010102T000000+1400",
            "END:VEVENT",
            "END:VCALENDAR",
        ]
    )
    rows, errors = parse_trips_ics(ics)
    assert rows == [], rows
    assert errors, "越界时间要按非法事件报告而不是静默通过"
    print("✓ 极端年份的 ICS 时间按非法事件跳过")


def test_ics_extreme_year_tzid_datetime_skipped():
    """TZID 换算越界（0001 年）逐事件跳过，同一 ICS 其余事件照常导入。"""
    from utils.trip_import import parse_trips_ics

    ics = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "BEGIN:VEVENT",
            "UID:extreme-tzid",
            "SUMMARY:北京 → 上海（出差）",
            "DTSTART;TZID=Asia/Shanghai:00010101T000000",
            "DTEND;TZID=Asia/Shanghai:00010102T000000",
            "END:VEVENT",
            "BEGIN:VEVENT",
            "UID:valid-after",
            "SUMMARY:北京 → 广州（出差）",
            "DTSTART:20261110T090000",
            "DTEND:20261111T180000",
            "END:VEVENT",
            "END:VCALENDAR",
        ]
    )
    rows, errors = parse_trips_ics(ics)
    assert errors == [(1, "缺少或非法的 DTSTART")], errors
    assert len(rows) == 1, rows
    assert rows[0]["start_date"] == "2026-11-10"
    print("✓ TZID 极端年份的 ICS 事件按非法事件跳过、其余照常导入")


def test_import_trips_from_text_csv_and_ics():
    """CSV / ICS 文本导入：解析、去重模拟（dry-run）与格式自动识别。"""
    from utils.trip_import import import_trips_from_text

    csv_text = (
        "出发地,目的地,出发日期,返程日期,目的\n"
        "北京,上海,2026-12-01,2026-12-03,出差\n"
    )
    store = _ImportStore()
    result = import_trips_from_text(store, csv_text)
    assert result["format"] == "csv"
    assert (result["rows"], result["created"], result["updated"]) == (1, 1, 0)
    assert store.saved and store.saved[0]["destination"] == "上海"

    store = _ImportStore()
    store.trips.append(
        {
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-12-01",
            "end_date": "2026-12-03",
            "purpose": "出差",
        }
    )
    dry = import_trips_from_text(store, csv_text, dry_run=True)
    assert (dry["rows"], dry["created"], dry["updated"]) == (1, 0, 1)
    assert store.saved == [], "dry-run 不能写库"

    ics = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "BEGIN:VEVENT",
            "UID:web-import-1",
            "SUMMARY:北京 → 广州（出差）",
            "DTSTART:20261210T090000",
            "DTEND:20261211T180000",
            "END:VEVENT",
            "END:VCALENDAR",
        ]
    )
    store = _ImportStore()
    ics_result = import_trips_from_text(store, ics)
    assert ics_result["format"] == "ics"
    assert ics_result["rows"] == 1
    assert store.saved and store.saved[0]["destination"] == "广州"

    empty = import_trips_from_text(_ImportStore(), "不是行程")
    assert empty["rows"] == 0 and empty["skipped"]
    print("✓ 行程文本导入（CSV / dry-run 去重 / ICS 自动识别 / 无可导入项）")


def test_import_rejects_oversized_bad_text_and_caps_skipped():
    """行数上限 / NUL 文本整份拒绝 / 解析错误列表有界。"""
    import utils.trip_import as trip_import

    header = "origin,destination,start_date,end_date,purpose\n"
    big = header + "".join(
        f"北京,D{i},2026-01-01,,出差\n" for i in range(5001)
    )
    with pytest.raises(ValueError, match="单次最多导入 5000 条"):
        trip_import.import_trips_from_text(_ImportStore(), big)

    # CSV 的 NUL 行为随 Python 版本不同（3.10 的 csv 直接报错整行），
    # 用 iCalendar 验证同一字段校验分支
    with pytest.raises(ValueError, match="NUL"):
        trip_import.import_trips_from_text(
            _ImportStore(),
            "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\n"
            "DTSTART;VALUE=DATE:20261201\nDTEND;VALUE=DATE:20261202\n"
            "SUMMARY:北\x00京\nEND:VEVENT\nEND:VCALENDAR\n",
        )

    bad_rows = header + "".join("坏行\n" for _ in range(200))
    result = trip_import.import_trips_from_text(_ImportStore(), bad_rows)
    assert result["rows"] == 0
    assert len(result["skipped"]) == trip_import._MAX_SKIPPED == 50
    assert result["skipped_total"] == 200
    print("✓ 行程导入行数上限 / NUL 拒绝 / 错误列表有界")


def test_import_preserves_existing_purpose():
    """缺省目的不覆盖已有行程的目的；新行程才补默认「出差」。"""
    import tempfile

    from context.long_term_memory import LongTermMemory
    from utils.trip_import import import_trips_from_text

    store = LongTermMemory("u-import-purpose", tempfile.mkdtemp())
    assert store.save_trip_history(
        {
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-12-01",
            "end_date": "2026-12-03",
            "purpose": "客户会议",
        }
    )
    result = import_trips_from_text(
        store,
        "origin,destination,start_date,end_date\n"
        "北京,上海,2026-12-01,2026-12-03\n",
    )
    assert result["updated"] == 1
    existing = next(
        trip
        for trip in store.get_trip_history(limit=None)
        if trip["destination"] == "上海"
    )
    assert existing["purpose"] == "客户会议", "缺省目的不能覆盖用户改过的目的"

    result2 = import_trips_from_text(
        store,
        "origin,destination,start_date,end_date\n"
        "北京,杭州,2026-12-10,2026-12-11\n",
    )
    assert result2["created"] == 1
    created = next(
        trip
        for trip in store.get_trip_history(limit=None)
        if trip["destination"] == "杭州"
    )
    assert created["purpose"] == "出差", "新行程缺省目的补「出差」"
    print("✓ 行程导入缺省目的保留已有 / 新行程补默认")


def test_ics_regex_scan_is_bounded():
    """恶意超长 DESCRIPTION / SUMMARY 只扫描前缀，正则不会二次方回溯。"""
    import time

    from utils.trip_import import parse_trips_ics

    evil = " → " * 40000
    text = (
        "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\n"
        "DTSTART;VALUE=DATE:20261110\nDTEND;VALUE=DATE:20261111\n"
        f"DESCRIPTION:Aligo 行程：{evil}\n"
        f"SUMMARY:{evil}\n"
        "END:VEVENT\nEND:VCALENDAR\n"
    )
    started = time.perf_counter()
    rows, errors = parse_trips_ics(text)
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"超长 SUMMARY/DESCRIPTION 解析耗时 {elapsed:.2f}s"
    # 超长字段按行跳过（与 Web / CLI 的 100 字上限一致），解析本身仍有界
    assert rows == []
    assert any("超过 100 字" in reason for _, reason in errors), errors
    print("✓ ICS 超长 SUMMARY / DESCRIPTION 解析有界且按行拒绝")


def test_import_force_ics_and_purpose_precedence():
    """--ics 强制按 iCalendar 解析；同文件内空白目的不覆盖前一行给出的目的。"""
    import tempfile

    from context.long_term_memory import LongTermMemory
    from utils.trip_import import import_trips_from_text

    csv_text = (
        "origin,destination,start_date,end_date\n"
        "北京,上海,2026-12-01,2026-12-03\n"
    )
    forced = import_trips_from_text(
        _ImportStore(), csv_text, dry_run=True, force_ics=True
    )
    assert forced["format"] == "ics"
    assert forced["rows"] == 0, "--ics 必须按 iCalendar 解析（CSV 文本没有 VEVENT）"

    store = LongTermMemory("u-import-purpose-order", tempfile.mkdtemp())
    text = (
        "origin,destination,start_date,end_date,purpose\n"
        "北京,上海,2026-12-01,2026-12-03,客户会议\n"
        "北京,上海,2026-12-01,2026-12-03,\n"
    )
    result = import_trips_from_text(store, text)
    assert (result["created"], result["updated"]) == (1, 1)
    trip = store.get_trip_history(limit=None)[0]
    assert trip["purpose"] == "客户会议", (
        "后一行空白目的不能覆盖本批前一行显式给出的目的"
    )
    print("✓ 导入 --ics 强制格式 / 批内目的优先级")


def test_import_rejects_oversized_raw_text(monkeypatch):
    """解析前先拒绝超过原始文本上限的文件（CLI 无请求体上限）。"""
    import utils.trip_import as trip_import

    monkeypatch.setattr(trip_import, "_MAX_IMPORT_CHARS", 100)
    with pytest.raises(ValueError, match="导入文件过大"):
        trip_import.import_trips_from_text(_ImportStore(), "x" * 101)
    print("✓ 导入原始文本上限")


def test_parse_rejects_overlong_fields():
    """字段超过 100 字按行跳过（与 Web / CLI 写入口径一致）。"""
    from utils.trip_import import parse_trips_csv

    long_dest = "目" * 101
    rows, errors = parse_trips_csv(
        "出发地,目的地,出发日期\n北京,%s,2026-01-05\n上海,%s,2026-01-06\n"
        % (long_dest, "目" * 100)
    )
    assert [row["destination"] for row in rows] == ["目" * 100]
    assert any("字段 destination 超过 100 字" in reason for _, reason in errors)
    print("✓ 超长字段按行跳过（100 字边界保留）")


def test_parse_row_cap_fails_fast():
    """超过 5000 行的文件在解析阶段就报错（不先物化全部行 / 错误）。"""
    from utils.trip_import import parse_trips_csv

    body = "出发地,目的地,出发日期\n" + "".join(
        f"北京,城市{i},2026-01-05\n" for i in range(5001)
    )
    with pytest.raises(ValueError, match="单次最多导入 5000 条"):
        parse_trips_csv(body)
    print("✓ 超 5000 行在解析阶段快速失败")


def test_parse_errors_materialization_is_bounded():
    """大量坏行的错误列表有界：只物化前 1000 条，total 精确计数。"""
    from utils.trip_import import parse_trips_csv

    body = "出发地,目的地,出发日期\n" + "北京,,2026-01-05\n" * 2000
    rows, errors = parse_trips_csv(body)
    assert rows == []
    assert len(errors) == 1000
    assert errors.total == 2000
    print("✓ 解析错误列表有界（total 精确）")
def test_ics_roundtrip_dirty_dates_closed():
    """end<start / 9999-12-31 的脏日期导出再导入仍按同一身份（不新增重复）。"""
    from utils.trip_export import render_trips_ics
    from utils.trip_import import parse_trips_ics, trip_identity

    cases = [
        {
            "trip_id": "t1",
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-12-05",
            "end_date": "2026-12-01",
            "purpose": "出差",
        },
        {
            "trip_id": "t2",
            "origin": "北京",
            "destination": "广州",
            "start_date": "9999-12-31",
            "end_date": "9999-12-31",
            "purpose": "出差",
        },
    ]
    rows, errors = parse_trips_ics(render_trips_ics(cases, "u1"))
    assert errors == [], errors
    by_destination = {row["destination"]: row for row in rows}
    for case in cases:
        row = by_destination[case["destination"]]
        assert trip_identity(row) == trip_identity(case), case
    print("✓ ICS 脏日期往返身份闭合")


def test_ics_unfold_long_line_is_linear():
    """超长折行展开必须线性完成（旧实现逐段拼接字符串是 O(n²)，5M 要 20+ 秒）。"""
    import time

    from utils.trip_import import _unfold_ics

    payload = "A" * 3_000_000
    folded = "\r\n ".join(
        payload[index : index + 75] for index in range(0, len(payload), 75)
    )
    text = "BEGIN:VEVENT\r\nX:" + folded + "\r\nEND:VEVENT"
    started = time.monotonic()
    lines = _unfold_ics(text)
    elapsed = time.monotonic() - started
    assert lines[0] == "BEGIN:VEVENT"
    assert lines[1] == "X:" + payload
    assert elapsed < 5.0, f"展开 3M 字符折行耗时 {elapsed:.1f}s（疑似恢复 O(n²)）"
    print("✓ 超长 ICS 折行线性展开")


def test_ics_roundtrip_linebreak_identity_closed():
    """字段内 CR / CRLF 导出往返后身份不变（不再重复新增）。"""
    from utils.trip_export import render_trips_ics
    from utils.trip_import import parse_trips_ics, trip_identity

    trip = {
        "trip_id": "t3",
        "origin": "北京",
        "destination": "a\rb\r\nc",
        "start_date": "2026-12-05",
        "end_date": "2026-12-06",
    }
    rows, errors = parse_trips_ics(render_trips_ics([trip], "u1"))
    assert errors == [], errors
    assert trip_identity(rows[0]) == trip_identity(trip)
    print("✓ ICS 换行归一后身份闭合")


if __name__ == "__main__":
    test_parse_chinese_and_english_headers()
    test_parse_skips_invalid_rows()
    test_parse_missing_columns_and_empty()
    test_csv_malformed_quote_reports_error_instead_of_merging()
    test_ics_roundtrip_matches_export()
    test_ics_roundtrip_escapes_machine_delimiters()
    test_ics_unfold_and_lf_only()
    test_ics_unfold_long_line_is_linear()
    test_ics_collects_invalid_events()
    test_ics_external_summary_fallback()
    test_ics_valarm_before_properties_keeps_event_description()
    test_ics_structure_errors_do_not_duplicate_events()
    test_ics_quoted_tzid_parameter_is_unquoted()
    test_ics_roundtrip_unparsable_end_uses_machine_description()
    test_ics_empty_origin_roundtrip()
    test_ics_datetime_offset_converts_local_date()
    test_trip_identity_normalizes_unpadded_dates()
    test_ics_invalid_utc_offset_does_not_abort()
    test_ics_tzid_parameter_matches_utc_representation()
    test_csv_oversized_field_returns_error_and_keeps_rows()
    test_ics_timed_end_inclusive_except_midnight()
    test_ics_extreme_year_tzid_datetime_skipped()
    test_import_trips_from_text_csv_and_ics()
    test_import_rejects_oversized_bad_text_and_caps_skipped()
    test_import_preserves_existing_purpose()
    test_import_force_ics_and_purpose_precedence()
    test_ics_regex_scan_is_bounded()
    test_csv_formula_guard_roundtrip()
    test_csv_error_line_numbers_physical()
    for case in (
        test_runner_imports_and_dedupes,
        test_runner_dry_run_preview,
        test_runner_json_output_and_skipped,
        test_runner_errors,
        test_runner_reports_partial_import,
        test_runner_rejects_oversized_file_before_read,
        test_runner_rejects_gbk_csv,
        test_runner_imports_ics_and_dedupes,
        test_runner_dispatch_ics,
        test_import_rejects_oversized_raw_text,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    test_parse_rejects_overlong_fields()
    test_parse_row_cap_fails_fast()
    test_parse_errors_materialization_is_bounded()
    test_ics_roundtrip_dirty_dates_closed()
    test_ics_roundtrip_linebreak_identity_closed()
    print("\n全部通过")
