"""user-import（user-export JSON 的逆操作）测试（无需 API Key、不访问网络）。

运行：python tests/test_user_import.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

import cli
from context.long_term_memory import LongTermMemory
from utils.user_import import import_user_data, parse_user_export


def _export_payload():
    return {
        "user_id": "u-src",
        "exported_at": "2026-11-01T10:00:00",
        "preferences": {"home_location": "北京", "hotel_brands": ["汉庭", "如家"]},
        "trip_history": [
            {
                "trip_id": "trip_1",
                "origin": "北京",
                "destination": "上海",
                "start_date": "2026-12-01",
                "end_date": "2026-12-03",
                "purpose": "客户会议",
                "session_id": "s1",
            },
            {
                "trip_id": "trip_2",
                "origin": "杭州",
                "destination": "成都",
                "start_date": "2026-12-10",
                "end_date": "",
                "purpose": "培训",
                "session_id": None,
            },
        ],
        "chat_history": [
            {
                "role": "user",
                "content": "帮我规划北京到上海的行程",
                "timestamp": "2026-11-01T09:00:00",
                "session_id": "s1",
            },
            {
                "role": "assistant",
                "content": '{"results": [], "reply": "好的"}',
                "timestamp": "2026-11-01T09:00:05",
                "session_id": "s1",
            },
        ],
        "session_titles": {"s1": "上海出差", "s-missing": "悬空标题"},
        "pinned_sessions": ["s1", "s-missing"],
        "archived_sessions": ["s-missing"],
        "statistics": {"total_messages": 2},
    }


def _write_json(payload) -> str:
    path = os.path.join(tempfile.mkdtemp(prefix="chailv_uimport_"), "backup.json")
    io.open(path, "w", encoding="utf-8").write(json.dumps(payload, ensure_ascii=False))
    return path


def _real_store(user_id="u-dst") -> LongTermMemory:
    return LongTermMemory(user_id, tempfile.mkdtemp(prefix="chailv_uimport_store_"))


def test_parse_export_sections():
    sections, errors = parse_user_export(json.dumps(_export_payload(), ensure_ascii=False))
    assert errors == []
    assert sections["preferences"] == {"home_location": "北京", "hotel_brands": ["汉庭", "如家"]}
    assert len(sections["trips"]) == 2
    assert sections["trips"][0]["purpose"] == "客户会议"
    assert sections["trips"][1]["end_date"] == ""
    assert len(sections["messages"]) == 2
    # 悬空的会话元数据被静默忽略，不阻断恢复
    assert sections["titles"] == {"s1": "上海出差"}
    assert sections["pinned"] == ["s1"]
    assert sections["archived"] == []
    print("✓ 解析备份：偏好 / 行程 / 消息 / 会话元数据")


def test_parse_export_rejects_invalid():
    sections, errors = parse_user_export("not json")
    assert sections == {} and "JSON 解析失败" in errors[0]

    sections, errors = parse_user_export("[]")
    assert sections == {} and "不是 JSON 对象" in errors[0]

    sections, errors = parse_user_export("{}")
    assert sections == {} and "没有可恢复的数据" in errors[-1]
    print("✓ 非 JSON / 非对象 / 空备份报错")


def test_parse_export_skips_invalid_rows():
    payload = {
        "preferences": {"": "x", "home_location": "北京"},
        "trip_history": [
            {"destination": "", "start_date": "2026-12-01"},
            {"destination": "上海", "start_date": "2026-1-1"},
            {"destination": "上海", "start_date": "2026-12-05", "end_date": "2026-12-01"},
            {"destination": "上海", "start_date": "2026-12-01"},
        ],
        "chat_history": [
            {"role": "system", "content": "x", "session_id": "s1"},
            {"role": "user", "content": 42, "session_id": "s1"},
            {"role": "user", "content": "合法消息", "session_id": "s1"},
        ],
    }
    sections, errors = parse_user_export(json.dumps(payload, ensure_ascii=False))
    assert sections["preferences"] == {"home_location": "北京"}
    # 未补零的日期归一后保真保留（备份恢复优先于格式严格）；倒置区间整条跳过
    assert [t["start_date"] for t in sections["trips"]] == ["2026-01-01", "2026-12-01"]
    assert len(sections["messages"]) == 1
    assert any("目的地为空" in e for e in errors)
    assert any("结束日期不能早于出发日期" in e for e in errors)
    assert sum("角色或内容非法" in e for e in errors) == 2
    print("✓ 非法偏好 / 行程 / 消息逐条跳过（脏日期保真）")


def test_import_roundtrip_and_idempotent():
    sections, errors = parse_user_export(json.dumps(_export_payload(), ensure_ascii=False))
    store = _real_store()
    try:
        counts = import_user_data(store, sections)
        assert counts["preferences"] == 2
        assert counts["trips_created"] == 2 and counts["trips_updated"] == 0
        assert counts["messages_added"] == 2 and counts["messages_skipped"] == 0
        assert counts["titles"] == 1 and counts["pinned"] == 1 and counts["archived"] == 0

        assert store.get_preference()["home_location"] == "北京"
        trips = store.get_trip_history(limit=None)
        assert len(trips) == 2 and trips[0]["destination"] == "上海"
        messages = store.get_chat_history(limit=None)
        assert len(messages) == 2
        # 时间戳保真：归一成带时区的 ISO，墙钟时间与备份一致
        from datetime import datetime

        normalized = datetime.fromisoformat(messages[0]["timestamp"])
        assert normalized.tzinfo is not None
        assert normalized.replace(tzinfo=None).isoformat() == "2026-11-01T09:00:00"
        assert messages[0]["session_id"] == "s1"
        assert store.get_session_titles() == {"s1": "上海出差"}
        assert store.get_pinned_sessions() == ["s1"]
        assert store.get_archived_sessions() == []

        # 重复导入：行程全部命中更新、消息全部按内容去重，不产生重复数据
        counts2 = import_user_data(store, sections)
        assert counts2["trips_created"] == 0 and counts2["trips_updated"] == 2
        assert counts2["messages_added"] == 0 and counts2["messages_skipped"] == 2
        assert len(store.get_trip_history(limit=None)) == 2
        assert len(store.get_chat_history(limit=None)) == 2
    finally:
        store._close() if hasattr(store, "_close") else None
    print("✓ 恢复往返保真且重复导入幂等")


def test_parse_validates_session_ids_and_usage():
    payload = {
        "preferences": ["不是对象"],  # 整段类型错误：记错误、按空处理
        "chat_history": [
            {"role": "user", "content": "合法", "session_id": "s1"},
            {"role": "user", "content": "坏会话", "session_id": "s 1\n注入"},
            {"role": "assistant", "content": '{"reply": "x", "_meta": {"usage": {"calls": "很多"}}}', "session_id": "s1"},
            {"role": "assistant", "content": '{"reply": "y", "_meta": "bad"}', "session_id": "s1"},
            {
                "role": "assistant",
                "content": '{"reply": "z", "_meta": {"usage": {"calls": 1, "input_tokens": NaN}}}',
                "session_id": "s1",
            },
            {
                "role": "assistant",
                "content": '{"reply": "w", "_meta": {"usage": {"output_tokens": Infinity}}}',
                "session_id": "s1",
            },
            {
                "role": "assistant",
                "content": '{"reply": "v", "_meta": {"agents": 5}}',
                "session_id": "s1",
            },
            {"role": "assistant", "content": "纯文本回答", "session_id": "s1"},
        ],
    }
    sections, errors = parse_user_export(json.dumps(payload, ensure_ascii=False))
    assert sections["preferences"] == {}
    assert any("preferences 段不是对象" in e for e in errors)
    assert [m["content"] for m in sections["messages"]] == ["合法", "纯文本回答"]
    joined = "；".join(errors)
    assert "会话 ID 非法" in joined
    assert "消息用量字段不是数字" in joined
    assert "消息用量字段不是有限数" in joined, errors
    assert "消息智能体列表不是数组" in joined, errors
    assert "消息元数据不是对象" in joined
    print("✓ 会话 ID / 用量类型校验，偏好类型错误显式报告")


def test_import_sorts_messages_by_timestamp():
    payload = {
        "chat_history": [
            {"role": "user", "content": "较晚", "session_id": "s1", "timestamp": "2026-11-02T10:00:00"},
            {"role": "user", "content": "较早", "session_id": "s1", "timestamp": "2026-11-02T08:00:00"},
        ]
    }
    sections, _ = parse_user_export(json.dumps(payload, ensure_ascii=False))
    assert [m["content"] for m in sections["messages"]] == ["较早", "较晚"]
    store = _real_store("u-order")
    import_user_data(store, sections)
    assert [m["content"] for m in store.get_chat_history(limit=None)] == ["较早", "较晚"]
    print("✓ 恢复按时间升序写入")


def test_import_accepts_z_suffix_timestamps():
    """备份里的 ...Z（合法 UTC 写法，Python 3.10 的 fromisoformat 不支持）不能丢时间。"""
    payload = {
        "chat_history": [
            {
                "role": "user",
                "content": "UTC 时间",
                "session_id": "s-z",
                "timestamp": "2026-11-02T08:00:00Z",
            },
            {
                "role": "user",
                "content": "偏移时间",
                "session_id": "s-z",
                "timestamp": "2026-11-02T09:00:00+00:00",
            },
        ]
    }
    sections, errors = parse_user_export(json.dumps(payload, ensure_ascii=False))
    assert errors == []
    timestamps = [m["timestamp"] for m in sections["messages"]]
    assert timestamps == [
        "2026-11-02T08:00:00+00:00",
        "2026-11-02T09:00:00+00:00",
    ], timestamps
    assert [m["content"] for m in sections["messages"]] == ["UTC 时间", "偏移时间"]
    print("✓ 导入接受 Z 后缀时间戳并按真实时刻排序")


def test_runner_skip_knowledge_only_backup_fails(monkeypatch):
    """只含 knowledge 段的备份在 --skip-knowledge 后没有可恢复数据：退出 1。"""
    payload = {"knowledge": [{"content": "面板片段"}]}
    path = _write_json(payload)
    store = _real_store("u-skip-kb")
    code, out = _run_import(
        monkeypatch, "u-skip-kb", path, store, skip_knowledge=True
    )
    assert code == 1
    assert "没有可恢复的数据" in out and "只含知识片段" in out
    print("✓ --skip-knowledge 只含知识段的备份不再误报成功")


def test_runner_text_summary_and_knowledge_error(monkeypatch):
    """非 --json 摘要含各计数；知识片段失败时打印 ❌ 并退出 1（用户数据已恢复）。"""
    from utils import user_import as import_module

    counts = {
        "preferences": 2,
        "trips_created": 1,
        "trips_updated": 0,
        "messages_added": 3,
        "messages_skipped": 1,
        "titles": 1,
        "pinned": 0,
        "archived": 0,
        "knowledge_imported": 4,
        "knowledge_skipped": 0,
    }
    monkeypatch.setattr(
        import_module, "import_user_data", lambda store, sections: dict(counts)
    )
    path = _write_json(_export_payload())
    store = _real_store("u-text-summary")
    code, out = _run_import(monkeypatch, "u-text-summary", path, store)
    assert code == 0
    assert "已恢复：偏好 2 项" in out
    assert "知识片段新增 4 条（跳过 0 条）" in out

    counts_err = dict(counts, knowledge_error="知识库未初始化")
    monkeypatch.setattr(
        import_module, "import_user_data", lambda store, sections: dict(counts_err)
    )
    code, out = _run_import(monkeypatch, "u-text-summary", path, store)
    assert code == 1
    assert "❌ 知识库未初始化" in out and "用户数据已恢复" in out
    print("✓ user-import 文本摘要与知识片段失败提示")


def test_runner_rejects_gbk_backup_and_directory(monkeypatch):
    """非 UTF-8 备份与目录路径分别命中解码 / IO 错误分支。"""
    store = _real_store("u-badfile")
    gbk_path = os.path.join(
        tempfile.mkdtemp(prefix="chailv_import_"), "gbk.json"
    )
    with io.open(gbk_path, "wb") as handle:
        handle.write("{\"a\": \"中文\"}".encode("gbk"))
    code, out = _run_import(monkeypatch, "u-badfile", gbk_path, store)
    assert code == 1 and "UTF-8" in out

    code, out = _run_import(monkeypatch, "u-badfile", tempfile.mkdtemp(), store)
    assert code == 1 and "读取备份失败" in out
    print("✓ user-import GBK 备份与目录路径友好报错")


def test_preview_does_not_write_and_matches_import():
    from utils.user_import import preview_user_import

    sections, errors = parse_user_export(json.dumps(_export_payload(), ensure_ascii=False))
    assert errors == []
    store = _real_store("u-preview")
    preview = preview_user_import(store, sections)
    assert preview["preferences"] == 2
    assert preview["trips_created"] == 2 and preview["trips_updated"] == 0
    assert preview["messages_added"] == 2 and preview["messages_skipped"] == 0
    assert preview["titles"] == 1 and preview["pinned"] == 1
    # 预览不写库
    assert store.get_preference() == {}
    assert store.get_trip_history(limit=None) == []
    assert store.get_chat_history(limit=None) == []

    counts = import_user_data(store, sections)
    for key in (
        "preferences",
        "trips_created",
        "trips_updated",
        "messages_added",
        "messages_skipped",
        "titles",
        "pinned",
        "archived",
    ):
        assert counts[key] == preview[key], key
    again = preview_user_import(store, sections)
    assert again["trips_created"] == 0 and again["trips_updated"] == 2
    assert again["messages_added"] == 0 and again["messages_skipped"] == 2
    print("✓ 预览只读且计数与真实恢复一致")


def test_exporter_json_is_importable():
    """user-export 实际写出的 JSON 必须能被 user-import 解析（防 schema 漂移）。"""
    from utils.exporter import export_files

    payload = _export_payload()
    with tempfile.TemporaryDirectory(prefix="chailv_uimport_export_") as tmp:
        paths = export_files(payload, tmp)
        json_path = next(p for p in paths if str(p).endswith(".json"))
        sections, errors = parse_user_export(json_path.read_text(encoding="utf-8"))
    assert errors == [], errors
    assert sections["preferences"] == {"home_location": "北京", "hotel_brands": ["汉庭", "如家"]}
    assert len(sections["trips"]) == 2 and len(sections["messages"]) == 2
    assert sections["titles"] == {"s1": "上海出差"}
    assert sections["pinned"] == ["s1"]
    print("✓ user-export 实际输出可直接被 user-import 解析")


def test_import_duplicate_message_counts():
    """同一会话里重复的相同提问：按出现次数补齐，不多也不少。"""
    payload = {
        "chat_history": [
            {"role": "user", "content": "同一句话", "timestamp": "2026-11-01T09:00:00", "session_id": "s1"},
            {"role": "user", "content": "同一句话", "timestamp": "2026-11-01T09:01:00", "session_id": "s1"},
        ]
    }
    sections, errors = parse_user_export(json.dumps(payload, ensure_ascii=False))
    store = _real_store("u-dup")
    counts = import_user_data(store, sections)
    assert counts["messages_added"] == 2
    counts2 = import_user_data(store, sections)
    assert counts2["messages_added"] == 0 and counts2["messages_skipped"] == 2
    assert len(store.get_chat_history(limit=None)) == 2
    print("✓ 重复消息多重集去重")


def _run_import(
    monkeypatch,
    user_id,
    path,
    store,
    as_json=False,
    dry_run=False,
    skip_knowledge=False,
):
    monkeypatch.setattr(cli, "_open_cli_store", lambda user_id: store)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.run_user_import_standalone(
            user_id,
            path,
            as_json=as_json,
            dry_run=dry_run,
            skip_knowledge=skip_knowledge,
        )
    return code, buf.getvalue()


def test_runner_json_output(monkeypatch):
    path = _write_json(_export_payload())
    store = _real_store("u-cli")
    code, out = _run_import(monkeypatch, "u-cli", path, store, as_json=True)
    payload = json.loads(out)
    assert code == 0 and payload["ok"] is True
    assert payload["preferences"] == 2
    assert payload["trips_created"] == 2
    assert payload["messages_added"] == 2
    assert payload["skipped"] == []
    print("✓ user-import --json 输出")


def test_runner_dry_run(monkeypatch):
    path = _write_json(_export_payload())
    store = _real_store("u-dry")
    code, out = _run_import(
        monkeypatch, "u-dry", path, store, as_json=True, dry_run=True
    )
    payload = json.loads(out)
    assert code == 0 and payload["dry_run"] is True
    assert payload["preferences"] == 2 and payload["messages_added"] == 2
    assert payload["trips_created"] == 2
    assert store.get_chat_history(limit=None) == []
    assert store.get_trip_history(limit=None) == []
    print("✓ user-import --dry-run 预览且不写库")


def test_runner_errors(monkeypatch):
    store = _real_store("u-err")
    code, out = _run_import(monkeypatch, "bad id!", "/tmp/x.json", store)
    assert code == 1 and "用户 ID" in out
    code, out = _run_import(monkeypatch, "u1", "/nonexistent/file.json", store)
    assert code == 1 and "文件不存在" in out

    bad = os.path.join(tempfile.mkdtemp(prefix="chailv_uimport_"), "bad.json")
    io.open(bad, "w", encoding="utf-8").write("{}")
    code, out = _run_import(monkeypatch, "u1", bad, store, as_json=True)
    payload = json.loads(out)
    assert code == 1 and payload["ok"] is False
    assert "没有可恢复的数据" in payload["error"]
    print("✓ 非法用户 / 文件不存在 / 空备份失败")


def test_runner_rejects_oversized_backup(monkeypatch):
    from utils import user_import as user_import_module

    path = _write_json(_export_payload())
    store = _real_store("u-big")
    monkeypatch.setattr(user_import_module, "MAX_BACKUP_BYTES", 1)
    code, out = _run_import(monkeypatch, "u-big", path, store, as_json=True)
    payload = json.loads(out)
    assert code == 1 and "50 MB" in payload["error"]
    print("✓ 超大备份文件被拒绝")


def test_runner_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli,
        "run_user_import_standalone",
        lambda user_id, path, as_json=False, dry_run=False, skip_knowledge=False: (
            calls.append((user_id, path, as_json, dry_run, skip_knowledge)) or 0
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["cli.py", "user-import", "u1", "backup.json", "--json", "--dry-run"],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert calls[-1] == ("u1", "backup.json", True, True, False)

    monkeypatch.setattr(sys, "argv", ["cli.py", "user-import", "u1"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    print("✓ user-import dispatch 与用法错误")


def test_bulk_storage_helpers():
    """批量接口：行程去重计数、会话元数据只对存在的会话生效。"""
    store = _real_store("u-bulk")
    created, updated = store.save_trip_histories(
        [
            {"origin": "北京", "destination": "上海", "start_date": "2026-12-01"},
            {"origin": "北京", "destination": "上海", "start_date": "2026-12-01", "purpose": "会议"},
            {"origin": "杭州", "destination": "成都", "start_date": "2026-12-10"},
        ]
    )
    assert (created, updated) == (2, 1), (created, updated)
    trips = store.get_trip_history(limit=None)
    assert len(trips) == 2
    assert trips[0]["purpose"] == "会议", "批量内的重复行程应更新既有记录"
    again = store.save_trip_histories(trips[:1])
    assert again == (0, 1)

    store.add_chat_message("user", "hi", "sess-bulk")
    assert store.set_session_titles({"sess-bulk": "批量标题", "ghost": "悬空"}) == 1
    assert store.set_sessions_pinned(["sess-bulk", "ghost"]) == 1
    assert store.set_sessions_archived(["sess-bulk", "ghost"]) == 1
    assert store.get_session_titles() == {"sess-bulk": "批量标题"}
    assert store.get_pinned_sessions() == ["sess-bulk"]
    assert store.get_archived_sessions() == ["sess-bulk"]
    print("✓ 批量存储接口（行程去重 / 元数据仅存在会话）")


def test_parse_tolerates_bad_timestamps_and_rows():
    payload = {
        "preferences": {"k" * 65: "太长键", "ok": "值"},
        "trip_history": [
            "不是对象",
            {"destination": "上海", "start_date": "2026-12-01", "end_date": "2026-12-32"},
        ],
        "chat_history": [
            "不是对象",
            {"role": "user", "content": "时间戳非法", "session_id": "s1", "timestamp": "not-a-date"},
            {"role": "assistant", "content": "数字时间", "session_id": "s1", "timestamp": 123},
            {"role": "user", "content": "空时间", "session_id": "s1", "timestamp": ""},
        ],
        "session_titles": {"s1": "  ", "missing": "悬空"},
    }
    sections, errors = parse_user_export(json.dumps(payload, ensure_ascii=False))
    assert sections["preferences"] == {"ok": "值"}
    # 无法解析的返程日期原样保留（保真），不整条丢弃
    assert [t["end_date"] for t in sections["trips"]] == ["2026-12-32"]
    assert [m["timestamp"] for m in sections["messages"]] == [None, None, None]
    assert sections["titles"] == {}
    joined = "；".join(errors)
    assert "键名非法" in joined
    assert "不是对象" in joined
    print("✓ 非法时间戳 / 行类型 / 超长键名逐条容忍")


def test_parse_limits_preferences():
    payload = {"preferences": {f"k{i}": i for i in range(205)}}
    sections, errors = parse_user_export(json.dumps(payload))
    assert len(sections["preferences"]) == 200
    assert any("偏好超过 200 个" in e for e in errors)
    print("✓ 偏好数量上限")



def test_parse_trip_and_knowledge_edge_rows():
    """行程空出发日期 / 非法会话 ID、知识片段 NUL 与坏 parts 元数据逐条处理。"""
    from utils.user_import import drop_sessions

    payload = {
        "trip_history": [
            {"destination": "上海", "start_date": "", "end_date": ""},
            {"destination": "杭州", "start_date": "2026-01-01", "session_id": "bad id!"},
        ],
        "knowledge": [
            {
                "content": "含NUL" + chr(0) + "内容",
                "metadata": {"group": "g1", "part": "1", "parts": "2"},
            },
            {
                "content": "元数据坏",
                "metadata": {
                    "group": "g2",
                    "part": "1",
                    "parts": "2",
                    "category": "坏" + chr(0) + "值",
                },
            },
            {"content": "坏parts", "metadata": {"group": "g3", "part": "1", "parts": "abc"}},
            {"content": "坏part号", "metadata": {"group": "g4", "part": "x", "parts": "2"}},
        ],
    }
    sections, errors = parse_user_export(json.dumps(payload, ensure_ascii=False))
    assert [trip["destination"] for trip in sections["trips"]] == ["杭州"]
    assert any("出发日期为空" in error for error in errors), errors
    assert any("会话 ID 非法" in error for error in errors), errors
    # 内容与元数据各一条 NUL：都按「包含 NUL 字符」逐条跳过
    assert sum("包含 NUL 字符" in error for error in errors) == 2, errors
    # part 非数字按 0 处理 → 组校验按分片编号问题报出该组
    assert any("知识分片组" in error for error in errors), errors

    # 空集合直接返回（Web / CLI 墓碑剔除路径）
    sections_before = {"messages": [{"content": "x"}]}
    drop_sessions(sections_before, set())
    assert sections_before["messages"] == [{"content": "x"}]
    print("✓ 行程空日期 / 坏会话 ID、知识片段 NUL 与坏 parts 元数据")


def test_import_restores_archived_and_reports_generic_knowledge_error(monkeypatch):
    """归档会话随备份恢复；知识库导入的通用异常计入 knowledge_error 而非中断。"""
    from utils import knowledge_admin as admin_module

    payload = {
        "archived_sessions": ["s-arch"],
        "chat_history": [
            {"role": "user", "content": "旧消息", "session_id": "s-arch"}
        ],
        "knowledge": [{"content": "知识", "metadata": {}}],
    }
    sections, errors = parse_user_export(json.dumps(payload, ensure_ascii=False))
    assert errors == [], errors
    store = _real_store("u-arch")
    try:

        def boom(documents):
            raise RuntimeError("milvus down")

        monkeypatch.setattr(admin_module, "import_documents", boom)
        counts = import_user_data(store, sections)
        assert counts.get("archived") == 1, counts
        assert "知识片段恢复失败" in counts.get("knowledge_error", ""), counts
        assert "s-arch" in store.get_archived_sessions()
    finally:
        store._close() if hasattr(store, "_close") else None
    print("✓ 归档恢复与知识库通用错误上报")


def test_parse_limits_trips_messages_and_knowledge(monkeypatch):
    """行程 / 消息 / 知识片段超出上限时截断并报错（含坏 parts 声明）。"""
    from utils import user_import as module

    monkeypatch.setattr(module, "_MAX_TRIPS", 1)
    monkeypatch.setattr(module, "_MAX_MESSAGES", 1)
    monkeypatch.setattr(module, "_MAX_KNOWLEDGE", 1)
    payload = {
        "trip_history": [
            {"destination": "甲", "start_date": "2026-01-01"},
            {"destination": "乙", "start_date": "2026-01-02"},
        ],
        "chat_history": [
            {"role": "user", "content": "一", "session_id": "s1"},
            {"role": "user", "content": "二", "session_id": "s1"},
        ],
        "knowledge": [
            {
                "content": "组片",
                "metadata": {"group": "g-cap", "part": "1", "parts": "abc"},
            },
            {"content": "多出来了", "metadata": {}},
        ],
    }
    sections, errors = parse_user_export(json.dumps(payload, ensure_ascii=False))
    assert any("行程超过" in error for error in errors), errors
    assert any("聊天消息超过" in error for error in errors), errors
    assert any("知识片段超过" in error for error in errors), errors
    assert [trip["destination"] for trip in sections["trips"]] == ["甲"]
    assert len(sections["messages"]) == 1
    assert [item["content"] for item in sections["knowledge"]] == ["组片"]
    print("✓ 行程 / 消息 / 知识片段超限截断（含坏 parts 声明）")


def test_parse_knowledge_section_and_import(monkeypatch):
    """备份的 knowledge 段：解析校验、预览计数、导入计数与错误上报。"""
    from utils import knowledge_admin as admin_module

    payload = {
        "knowledge": [
            {"content": "知识一", "metadata": {"category": "住宿"}},
            {"content": "", "metadata": {}},
            "不是对象",
        ]
    }
    sections, errors = parse_user_export(json.dumps(payload, ensure_ascii=False))
    assert [item["content"] for item in sections["knowledge"]] == ["知识一"]
    assert sum("知识片段" in error for error in errors) == 2
    preview = None
    store = _real_store("u-knowledge")
    from utils.user_import import preview_user_import

    preview = preview_user_import(store, sections)
    assert preview["knowledge"] == 1

    calls = []

    def fake_import(documents):
        calls.append(documents)
        return {"imported": 1, "skipped": 0, "skipped_invalid": 0, "skipped_duplicate": 0}

    monkeypatch.setattr(admin_module, "import_documents", fake_import)
    counts = import_user_data(store, sections)
    assert counts["knowledge_imported"] == 1 and counts["knowledge_skipped"] == 0
    assert calls and calls[0][0]["content"] == "知识一"

    def missing_db(documents):
        raise FileNotFoundError("知识库未初始化")

    monkeypatch.setattr(admin_module, "import_documents", missing_db)
    counts = import_user_data(store, sections)
    assert "knowledge_error" in counts and "未初始化" in counts["knowledge_error"]
    print("✓ knowledge 段解析 / 预览 / 导入与错误上报")


def test_parse_knowledge_section_wrong_type():
    sections, errors = parse_user_export(json.dumps({"knowledge": {"a": 1}}))
    assert sections == {}
    assert any("knowledge 段不是数组" in error for error in errors)
    print("✓ knowledge 段类型错误显式报告")

def test_runner_knowledge_error_and_skip(monkeypatch):
    """知识片段恢复失败：用户数据已恢复但退出码 1；--skip-knowledge 跳过。"""
    from utils import knowledge_admin as admin_module

    path = _write_json(
        {"knowledge": [{"content": "知识一"}], "preferences": {"home_location": "北京"}}
    )
    store = _real_store("u-kb-cli")

    def missing(documents):
        raise FileNotFoundError("知识库未初始化")

    monkeypatch.setattr(admin_module, "import_documents", missing)
    code, out = _run_import(monkeypatch, "u-kb-cli", path, store, as_json=True)
    payload = json.loads(out)
    assert code == 1 and payload["ok"] is False
    assert "knowledge_error" in payload
    assert store.get_preference().get("home_location") == "北京", "用户数据仍应恢复"

    calls = []

    def fake_import(documents):
        calls.append(documents)
        return {"imported": 1, "skipped": 0, "skipped_invalid": 0, "skipped_duplicate": 0}

    monkeypatch.setattr(admin_module, "import_documents", fake_import)
    code, out = _run_import(
        monkeypatch,
        "u-kb-cli-skip",
        path,
        _real_store("u-kb-cli-skip"),
        as_json=True,
        skip_knowledge=True,
    )
    payload = json.loads(out)
    assert code == 0 and payload["ok"] is True
    assert payload["skip_knowledge"] is True
    assert payload["knowledge_imported"] == 0
    assert calls == [], "--skip-knowledge 不得触碰知识库"
    print("✓ 知识片段失败上报 / --skip-knowledge 跳过")

def test_parse_rejects_nul_and_lone_surrogates():
    """NUL / 孤立代理项在 PostgreSQL / JSON 后端不可恢复，导入时拒绝。"""
    payload = {
        "chat_history": [
            {"role": "user", "content": "a\x00b", "session_id": "s1"},
            {"role": "user", "content": "bad\ud800", "session_id": "s1"},
            {"role": "user", "content": "正常提问", "session_id": "s1"},
        ],
        "trip_history": [
            {"destination": "上海\x00", "start_date": "2026-01-01"},
        ],
        "session_titles": {"s1": "标题\ud800"},
    }
    sections, errors = parse_user_export(json.dumps(payload, ensure_ascii=True))
    assert [message["content"] for message in sections["messages"]] == ["正常提问"]
    assert sections["trips"] == []
    assert sections["titles"] == {}
    assert any("NUL" in error for error in errors), errors
    assert any("孤立代理项" in error for error in errors), errors
    print("✓ NUL / 孤立代理项在导入时被拒绝")


def test_parse_rejects_non_finite_meta_durations():
    """_meta.elapsed / agents[].duration 非有限数会让状态接口永久 500。"""
    payload = {
        "chat_history": [
            {
                "role": "assistant",
                "content": json.dumps(
                    {"status": "completed", "results": [], "_meta": {"elapsed": float("inf")}}
                ),
                "session_id": "s1",
            },
            {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "status": "completed",
                        "results": [],
                        "_meta": {"agents": [{"name": "x", "duration": float("nan")}]},
                    }
                ),
                "session_id": "s1",
            },
            {
                "role": "assistant",
                "content": json.dumps(
                    {"status": "completed", "results": [], "_meta": {"elapsed": 1.5}}
                ),
                "session_id": "s1",
            },
        ]
    }
    sections, errors = parse_user_export(json.dumps(payload, ensure_ascii=True))
    assert len(sections["messages"]) == 1, "只有合法耗时的消息可恢复"
    assert any("消息耗时不是有限数" in error for error in errors), errors
    assert any("智能体耗时不是有限数" in error for error in errors), errors
    print("✓ 非有限耗时在导入时被拒绝")


def test_parse_rejects_bad_meta_shapes():
    """_meta 内的类型错误、深嵌套与非有限数列表都被拒绝，旧文本消息保留。"""

    def content(meta):
        return json.dumps({"status": "completed", "results": [], "_meta": meta})

    deep = []
    cursor = deep
    for _ in range(40):
        nested = []
        cursor.append(nested)
        cursor = nested

    payload = {
        "chat_history": [
            {
                "role": "assistant",
                "content": content({"agents": [{"duration": "5s"}]}),
                "session_id": "s1",
            },
            {
                "role": "assistant",
                "content": content({"schedule": 5}),
                "session_id": "s1",
            },
            {
                "role": "assistant",
                "content": content({"elapsed": "3s"}),
                "session_id": "s1",
            },
            {
                "role": "assistant",
                "content": content({"usage": 5}),
                "session_id": "s1",
            },
            {
                "role": "assistant",
                "content": content({"anything": deep}),
                "session_id": "s1",
            },
            {
                "role": "assistant",
                "content": content({"list": [float("nan")]}),
                "session_id": "s1",
            },
            # 以 { 开头但不是合法 JSON 的普通文本：按旧格式原样保留
            {
                "role": "assistant",
                "content": "{这不是 JSON",
                "session_id": "s1",
            },
        ]
    }
    sections, errors = parse_user_export(json.dumps(payload))
    assert len(sections["messages"]) == 1, errors
    assert sections["messages"][0]["content"].startswith("{这不是"), errors
    assert any("智能体耗时不是数字" in error for error in errors), errors
    assert any("消息执行路线不是数组" in error for error in errors), errors
    assert any("消息耗时不是数字" in error for error in errors), errors
    assert any("消息用量不是对象" in error for error in errors), errors
    assert any("消息元数据嵌套过深" in error for error in errors), errors
    assert any("消息元数据含非有限数" in error for error in errors), errors
    print("✓ _meta 形状错误 / 深嵌套 / 非有限数列表被拒绝")


def test_parse_drops_whole_knowledge_group_with_invalid_part():
    """组内有无效分片时整组丢弃：只删坏分片会让知识恢复因缺片整体失败。"""
    payload = {
        "knowledge": [
            {
                "content": "第一片",
                "metadata": {"group": "gX", "part": "1", "parts": "2"},
            },
            {
                "content": "",
                "metadata": {"group": "gX", "part": "2", "parts": "2"},
            },
            {"content": "完整文档", "metadata": {}},
        ]
    }
    sections, errors = parse_user_export(json.dumps(payload, ensure_ascii=False))
    assert [item["content"] for item in sections["knowledge"]] == ["完整文档"]
    assert any("同组含无效分片" in error for error in errors), errors
    print("✓ knowledge 组内坏分片整组跳过（不拖垮其它文档）")


def test_parse_rejects_deep_nested_assistant_content():
    """`[` 开头的深嵌套 JSON 会在展示 / 摘要路径抛 RecursionError，导入时拒绝。"""
    deep = "[" * 5000 + "]" * 5000
    sections, errors = parse_user_export(
        json.dumps(
            {
                "chat_history": [
                    {"role": "assistant", "content": deep, "session_id": "s1"},
                    {"role": "user", "content": "正常提问", "session_id": "s1"},
                ]
            },
            ensure_ascii=False,
        )
    )
    assert [message["role"] for message in sections["messages"]] == ["user"]
    assert any("嵌套过深" in error for error in errors), errors
    print("✓ 深嵌套 assistant 内容在导入时被拒绝")


def test_parse_accepts_leading_dash_session_ids():
    """Web API 允许的 "-abc" 会话 ID 在导入时不能丢（否则自己的备份恢复不了）。"""
    payload = {
        "chat_history": [
            {"role": "user", "content": "hi", "session_id": "-abc"},
            {"role": "assistant", "content": "在", "session_id": "-abc"},
        ],
        "session_titles": {"-abc": "深链接会话"},
        "pinned_sessions": ["-abc"],
    }
    sections, errors = parse_user_export(json.dumps(payload, ensure_ascii=False))
    assert [m["session_id"] for m in sections["messages"]] == ["-abc", "-abc"]
    assert sections["titles"] == {"-abc": "深链接会话"}
    assert sections["pinned"] == ["-abc"]
    assert errors == []
    print("✓ 前导 - 的会话 ID 导入保留（与 Web API 同口径）")


def test_parse_rejects_bad_preference_text():
    """偏好键 / 值（含列表内字符串）里的 NUL / 孤立代理项必须拒绝。"""
    payload = {
        "preferences": {
            "home_location": "a\x00b",
            "hotel_brands": ["汉庭", "bad\ud800"],
            "budget_level": "经济",
        }
    }
    sections, errors = parse_user_export(json.dumps(payload, ensure_ascii=True))
    assert sections["preferences"] == {"budget_level": "经济"}
    assert any("NUL" in error for error in errors), errors
    assert any("孤立代理项" in error for error in errors), errors
    print("✓ 偏好里的 NUL / 孤立代理项在导入时拒绝")


def test_parse_rejects_non_object_agent_entries():
    """_meta.agents / schedule 里的非对象条目会让面板渲染 TypeError，导入拒绝。"""
    payload = {
        "chat_history": [
            {
                "role": "assistant",
                "content": json.dumps({"results": [], "_meta": {"agents": [None]}}),
                "session_id": "s1",
            },
            {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "results": [],
                        "_meta": {"agents": [{"name": "x"}], "schedule": [1]},
                    }
                ),
                "session_id": "s1",
            },
            {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "results": [],
                        "_meta": {
                            "agents": [{"name": "x"}],
                            "schedule": [{"agent_name": "x"}],
                        },
                    }
                ),
                "session_id": "s1",
            },
        ]
    }
    sections, errors = parse_user_export(json.dumps(payload, ensure_ascii=True))
    assert len(sections["messages"]) == 1
    assert any("消息智能体列表包含非法条目" in error for error in errors), errors
    assert any("消息执行路线包含非法条目" in error for error in errors), errors
    print("✓ agents / schedule 非对象条目在导入时拒绝")


def test_parse_user_export_tolerates_recursion_error(monkeypatch):
    """结构性深层嵌套的 JSON 解析抛 RecursionError 时按错误返回，不打 traceback。"""
    from utils import user_import as user_import_module

    def boom(text):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(user_import_module.json, "loads", boom)
    sections, errors = user_import_module.parse_user_export("{}")
    assert sections == {}
    assert any("嵌套过深" in error for error in errors), errors
    print("✓ 深嵌套备份解析报结构化错误（RecursionError 被捕获）")


def test_runner_recovers_knowledge_only_backup(monkeypatch):
    """只含 knowledge 段的备份（未加 --skip-knowledge）必须恢复知识片段。"""
    from utils import knowledge_admin as admin_module

    path = _write_json({"knowledge": [{"content": "知识一"}]})
    store = _real_store("u-kb-only")
    calls = []

    def fake_import(documents):
        calls.append(documents)
        return {"imported": 1, "skipped": 0, "skipped_invalid": 0, "skipped_duplicate": 0}

    monkeypatch.setattr(admin_module, "import_documents", fake_import)
    code, out = _run_import(monkeypatch, "u-kb-only", path, store, as_json=True)
    payload = json.loads(out)
    assert code == 0, payload
    assert payload["knowledge_imported"] == 1
    assert calls and calls[0][0]["content"] == "知识一"
    print("✓ 只含知识片段的备份可正常恢复")

def test_parse_truncation_drops_split_knowledge_group(monkeypatch):
    """50000 条截断点落在多分片组中间时整组丢弃（不触发组完整性拒绝）。"""
    from utils import user_import as user_import_module

    monkeypatch.setattr(user_import_module, "_MAX_KNOWLEDGE", 3)
    payload = {
        "knowledge": [
            {"content": "甲", "metadata": {"group": "gX", "part": "1", "parts": "3"}},
            {"content": "乙", "metadata": {"group": "gX", "part": "2", "parts": "3"}},
            {"content": "完整文档", "metadata": {}},
            {"content": "丙", "metadata": {"group": "gX", "part": "3", "parts": "3"}},
        ]
    }
    sections, errors = user_import_module.parse_user_export(
        json.dumps(payload, ensure_ascii=False)
    )
    assert [item["content"] for item in sections["knowledge"]] == ["完整文档"]
    assert any("截断" in error for error in errors), errors
    print("✓ 截断点落在分片组中间时整组丢弃")


def test_import_matches_unpadded_stored_trip_dates():
    """库内未补零日期（2026-1-5）与导入补零后的行程按同一键去重。"""
    store = _real_store("u-unpadded")
    store.data["trip_history"].append(
        {
            "trip_id": "trip_1",
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-1-5",
            "end_date": "",
            "purpose": "出差",
        }
    )
    store.data["statistics"]["total_trips"] = 1
    store._save()
    payload = {
        "trip_history": [
            {"origin": "北京", "destination": "上海", "start_date": "2026-1-5", "purpose": "出差"}
        ]
    }
    sections, errors = parse_user_export(json.dumps(payload, ensure_ascii=False))
    assert errors == []
    counts = import_user_data(store, sections)
    assert counts["trips_created"] == 0 and counts["trips_updated"] == 1, counts
    assert len(store.get_trip_history(limit=None)) == 1
    print("✓ 未补零日期的既有行程不再被重复新增")


def test_parse_rejects_deeply_nested_preference_value():
    """近递归上限的嵌套偏好值按非法值拒绝，不能以 RecursionError 崩溃 CLI。

    深度取 200：Python 3.10 的 json 编码器在约 1000 层就会自身抛
    RecursionError（用例本身无法构造 payload）；200 层同样超过合法偏好
    应支持的层级，足以覆盖「递归解析先于 json 护栏溢出」的路径。
    """
    value: object = "x"
    for _ in range(200):
        value = [value]
    payload = {"preferences": {"hotel_brands": value}}
    sections, errors = parse_user_export(json.dumps(payload))
    assert "hotel_brands" not in sections.get("preferences", {}), sections
    assert any("嵌套过深" in error for error in errors), errors
    print("✓ 深嵌套偏好值按非法值拒绝")


def test_runner_skips_reserved_and_deleted_sessions(monkeypatch):
    """CLI user-import 与 Web 同一口径：保留字 / 墓碑期会话不能被恢复写回。"""
    from utils import tombstones

    payload = {
        "chat_history": [
            {
                "role": "user",
                "content": "保留字会话",
                "timestamp": "2026-11-01T09:00:00",
                "session_id": "export",
            },
            {
                "role": "user",
                "content": "刚删除的会话",
                "timestamp": "2026-11-01T09:01:00",
                "session_id": "s-del",
            },
            {
                "role": "user",
                "content": "正常会话",
                "timestamp": "2026-11-01T09:02:00",
                "session_id": "s-ok",
            },
        ],
        "session_titles": {"export": "坏标题", "s-del": "删掉", "s-ok": "好标题"},
        "pinned_sessions": ["export", "s-del"],
        "trip_history": [
            {
                "origin": "北京",
                "destination": "上海",
                "start_date": "2027-01-01",
                "session_id": "export",
            }
        ],
    }
    path = _write_json(payload)
    store = _real_store("u-cli-skip")
    monkeypatch.setattr(
        tombstones,
        "is_session_deleted",
        lambda user_id, session_id: session_id == "s-del",
    )
    code, out = _run_import(monkeypatch, "u-cli-skip", path, store, as_json=True)
    result = json.loads(out)
    assert code == 0, out
    assert any("保留字" in item for item in result["skipped"]), result
    assert any("最近被删除" in item for item in result["skipped"]), result
    ids = {msg.get("session_id") for msg in store.get_chat_history(limit=None)}
    assert ids == {"s-ok"}, ids
    trips = store.get_trip_history(limit=None)
    assert trips and trips[0].get("session_id") in (None, ""), trips
    print("✓ user-import 跳过保留字 / 墓碑期会话")


def test_runner_skips_trip_only_reserved_and_deleted_sessions(monkeypatch):
    """备份只有行程引用保留字 / 墓碑期会话（无消息）时同样清空关联。"""
    from utils import tombstones

    payload = {
        "trip_history": [
            {
                "origin": "北京",
                "destination": "上海",
                "start_date": "2027-02-01",
                "session_id": "export",
            },
            {
                "origin": "北京",
                "destination": "广州",
                "start_date": "2027-02-05",
                "session_id": "s-del",
            },
            {
                "origin": "北京",
                "destination": "深圳",
                "start_date": "2027-02-09",
                "session_id": "s-keep",
            },
        ]
    }
    path = _write_json(payload)
    store = _real_store("u-cli-triponly")
    monkeypatch.setattr(
        tombstones,
        "is_session_deleted",
        lambda user_id, session_id: session_id == "s-del",
    )
    code, out = _run_import(monkeypatch, "u-cli-triponly", path, store, as_json=True)
    result = json.loads(out)
    assert code == 0, out
    assert any("保留字" in item for item in result["skipped"]), result
    assert any("最近被删除" in item for item in result["skipped"]), result
    trips = {trip["destination"]: trip for trip in store.get_trip_history(limit=None)}
    assert trips["上海"].get("session_id") in (None, ""), trips["上海"]
    assert trips["广州"].get("session_id") in (None, ""), trips["广州"]
    assert trips["深圳"].get("session_id") == "s-keep"
    print("✓ user-import 过滤仅被行程引用的保留字 / 墓碑期会话")


def test_runner_rolls_back_session_deleted_during_write(monkeypatch):
    """CLI 恢复写库期间会话被其它实例删除：写完复查共享墓碑并回滚。"""
    from utils import tombstones
    from utils import user_import as user_import_module

    payload = {
        "chat_history": [
            {"role": "user", "content": "恢复期间被删", "session_id": "s-late"}
        ]
    }
    path = _write_json(payload)
    store = _real_store("u-cli-late")
    flags = {"late": False}
    original_import = user_import_module.import_user_data

    def import_then_delete(store_obj, sections):
        counts = original_import(store_obj, sections)
        flags["late"] = True
        return counts

    monkeypatch.setattr(user_import_module, "import_user_data", import_then_delete)
    monkeypatch.setattr(
        tombstones,
        "is_session_deleted",
        lambda user_id, session_id: session_id == "s-late" and flags["late"],
    )
    code, out = _run_import(monkeypatch, "u-cli-late", path, store, as_json=True)
    result = json.loads(out)
    assert code == 0, out
    assert result["messages_added"] == 0, result
    assert any("写入期间被删除" in item for item in result["skipped"]), result
    assert store.get_chat_history(session_id="s-late") == []
    print("✓ CLI 恢复回滚写入期间被删除的会话")


def test_parse_rejects_huge_integer_literal_content():
    """含超 4300 位整数字面量的助手消息在导入时拒绝（否则汇总/查看永久 500）。"""
    payload = {
        "chat_history": [
            {
                "role": "assistant",
                "content": '{"n": ' + "1" * 5000 + "}",
                "timestamp": "2026-11-01T09:00:00",
                "session_id": "s1",
            }
        ]
    }
    sections, errors = parse_user_export(json.dumps(payload))
    assert sections == {} or sections.get("messages") == [], sections
    assert any("超长数字字面量" in error for error in errors), errors
    print("✓ 超长整数字面量的助手消息被拒绝")


def test_parse_survives_extreme_timestamp():
    """极端年份时间戳（.NET DateTime.MinValue）不再让整份备份解析崩溃。"""
    payload = {
        "chat_history": [
            {
                "role": "user",
                "content": "hi",
                "timestamp": "0001-01-01T00:00:00",
                "session_id": "s1",
            }
        ]
    }
    sections, errors = parse_user_export(json.dumps(payload))
    assert errors == [], errors
    assert len(sections["messages"]) == 1
    assert sections["messages"][0]["timestamp"] is None, "越界年份按缺失处理"
    print("✓ 极端年份时间戳的备份可正常解析")
def test_parse_rejects_non_finite_preference_values():
    """偏好值里的 NaN / Infinity 在导入时拒绝（PG JSONB 确定性失败）。"""
    text = '{"preferences": {"x": NaN, "y": [1.0, Infinity], "ok": 1}}'
    sections, errors = parse_user_export(text)
    prefs = sections.get("preferences", {})
    assert "x" not in prefs and "y" not in prefs, prefs
    assert prefs.get("ok") == 1, prefs
    assert sum("非有限数" in error for error in errors) == 2, errors
    print("✓ 非有限数偏好值被拒绝")


def test_parse_rejects_non_finite_unknown_meta_keys():
    """已知字段之外的 _meta 数值（usage.time 等）非有限数同样拒绝。"""
    content = json.dumps({"results": [], "_meta": {"usage": {"time": float("nan")}}})
    payload = {
        "chat_history": [
            {
                "role": "assistant",
                "content": content,
                "timestamp": "2026-11-01T09:00:00",
                "session_id": "s1",
            }
        ]
    }
    sections, errors = parse_user_export(json.dumps(payload))
    assert sections == {} or sections.get("messages") == [], sections
    assert any("非有限数" in error for error in errors), errors
    print("✓ _meta 任意键的非有限数被拒绝")


def test_parse_rejects_slash_preference_keys():
    """含 "/" 的偏好键落库后面板 / CLI 都无法编辑或删除，导入时拒绝。"""
    payload = {"preferences": {"a/b": "x", "ok": 1}}
    sections, errors = parse_user_export(json.dumps(payload))
    prefs = sections.get("preferences", {})
    assert "a/b" not in prefs, prefs
    assert prefs.get("ok") == 1, prefs
    assert any("键名非法" in error for error in errors), errors
    print("✓ 含斜杠的偏好键被拒绝")


def test_parse_rejects_bad_trip_date_text():
    """行程日期里的 NUL / 孤立代理项同样跳过该行（PG 整份恢复不再 503）。"""
    payload = {
        "trip_history": [
            {"destination": "上海", "start_date": "2026-01-01\x00"},
            {
                "destination": "广州",
                "start_date": "2026-01-02",
                "end_date": "2026-01-01\ud800",
            },
            {"destination": "北京", "start_date": "2026-01-03"},
        ]
    }
    sections, errors = parse_user_export(json.dumps(payload))
    assert [trip["destination"] for trip in sections["trips"]] == ["北京"]
    assert sum("NUL" in error for error in errors) == 1, errors
    assert sum("孤立代理项" in error for error in errors) == 1, errors
    print("✓ 行程日期文本门禁（NUL / 孤立代理项）")


def test_extreme_timestamps_fall_back_to_restore_time():
    """极端年份（含时区偏移越界）按缺失处理，不落库成 PG 不可读的时间。"""
    payload = {
        "chat_history": [
            {
                "role": "user",
                "content": "a",
                "timestamp": "0001-01-01T00:00:00+08:00",
                "session_id": "s1",
            },
            {
                "role": "user",
                "content": "b",
                "timestamp": "9999-12-31T23:59:59-08:00",
                "session_id": "s1",
            },
            {
                "role": "user",
                "content": "ok",
                "timestamp": "2026-01-01T00:00:00",
                "session_id": "s1",
            },
        ]
    }
    sections, errors = parse_user_export(json.dumps(payload))
    assert errors == [], errors
    timestamps = [message["timestamp"] for message in sections["messages"]]
    assert sum(value is None for value in timestamps) == 2, timestamps
    assert any(value is not None for value in timestamps)


def test_parse_drops_broken_knowledge_groups_and_bad_metadata():
    """知识元数据含坏文本 / 分片编号重复或缺失的组整组丢弃，其余照常恢复。"""
    payload = {
        "knowledge": [
            {"content": "正常文档", "metadata": {}},
            {"content": "坏元数据", "metadata": {"title": "x\x00"}},
            {
                "content": "组一号",
                "metadata": {"group": "g", "part": "1", "parts": "2"},
            },
            {
                "content": "组一号",
                "metadata": {"group": "g", "part": "1", "parts": "2"},
            },
            {
                "content": "缺号",
                "metadata": {"group": "g2", "part": "2", "parts": "2"},
            },
            {"content": "独立片段", "metadata": {}},
        ]
    }
    sections, errors = parse_user_export(json.dumps(payload))
    contents = [entry["content"] for entry in sections["knowledge"]]
    assert "坏元数据" not in contents
    assert "组一号" not in contents and "缺号" not in contents
    assert "正常文档" in contents and "独立片段" in contents
    assert any("NUL" in error for error in errors), errors
    assert any("分片编号重复或缺失" in error for error in errors), errors
    print("✓ 知识元数据文本门禁与残缺组整组丢弃")


def test_parse_drops_knowledge_group_with_absurd_parts():
    """parts 声明无上限时 set(range(1, parts+1)) 会被 1e9 撑爆内存：

    百字节备份即可触发 OOM，必须在构造 range 之前按异常组整组丢弃。
    """
    payload = {
        "knowledge": [
            {
                "content": "异常分片",
                "metadata": {
                    "group": "gBoom",
                    "part": "1",
                    "parts": "1000000000",
                },
            },
            {"content": "正常文档", "metadata": {}},
        ]
    }
    sections, errors = parse_user_export(json.dumps(payload, ensure_ascii=False))
    assert [item["content"] for item in sections["knowledge"]] == ["正常文档"]
    assert any("分片数声明异常" in error for error in errors), errors
    print("✓ 异常 parts 声明整组丢弃（不构造巨型 range）")


def test_parse_rejects_escaped_surrogate_in_assistant_json():
    """转义形态的孤立代理项 / NUL 绕过原文门禁：

    assistant 内容里的 ``\\ud800`` 在原文中是 ASCII，解析后才是孤立代理项，
    渲染 / 导出 / 总结每次编码都失败（会话永久 500），必须在导入时拒绝。
    """
    bad_surrogate = (
        '{"results": [{"reply": "\\ud800"}], "_meta": {"agents": []}}'
    )
    bad_nul = (
        '{"results": [{"reply": "a\\u0000b"}], "_meta": {"agents": []}}'
    )
    good = '{"results": [{"reply": "正常回答"}], "_meta": {"agents": []}}'
    payload = {
        "chat_history": [
            {"role": "assistant", "content": bad_surrogate, "session_id": "s1"},
            {"role": "assistant", "content": bad_nul, "session_id": "s1"},
            {"role": "assistant", "content": good, "session_id": "s1"},
        ]
    }
    sections, errors = parse_user_export(json.dumps(payload, ensure_ascii=False))
    assert [m["content"] for m in sections["messages"]] == [good]
    assert any("孤立代理项" in error for error in errors), errors
    assert any("NUL" in error for error in errors), errors
    print("✓ 转义代理项 / NUL 的 assistant 内容在导入时被拒绝")


if __name__ == "__main__":
    test_parse_export_sections()
    test_parse_export_rejects_invalid()
    test_parse_export_skips_invalid_rows()
    test_exporter_json_is_importable()
    test_parse_validates_session_ids_and_usage()
    test_import_sorts_messages_by_timestamp()
    test_import_accepts_z_suffix_timestamps()
    test_preview_does_not_write_and_matches_import()
    test_parse_tolerates_bad_timestamps_and_rows()
    test_parse_limits_preferences()
    test_bulk_storage_helpers()
    test_parse_knowledge_section_wrong_type()
    test_parse_drops_whole_knowledge_group_with_invalid_part()
    test_parse_rejects_deep_nested_assistant_content()
    test_parse_rejects_nul_and_lone_surrogates()
    test_parse_rejects_non_finite_meta_durations()
    test_parse_accepts_leading_dash_session_ids()
    test_parse_rejects_bad_preference_text()
    test_parse_rejects_non_object_agent_entries()
    test_import_matches_unpadded_stored_trip_dates()
    test_import_roundtrip_and_idempotent()
    test_import_duplicate_message_counts()
    test_parse_rejects_deeply_nested_preference_value()
    test_parse_rejects_huge_integer_literal_content()
    test_parse_survives_extreme_timestamp()
    test_parse_rejects_non_finite_preference_values()
    test_parse_rejects_non_finite_unknown_meta_keys()
    test_parse_rejects_slash_preference_keys()
    test_parse_rejects_bad_trip_date_text()
    test_extreme_timestamps_fall_back_to_restore_time()
    test_parse_drops_broken_knowledge_groups_and_bad_metadata()
    test_parse_drops_knowledge_group_with_absurd_parts()
    test_parse_rejects_escaped_surrogate_in_assistant_json()
    test_parse_trip_and_knowledge_edge_rows()
    for case in (
        test_runner_json_output,
        test_runner_dry_run,
        test_runner_errors,
        test_runner_rejects_oversized_backup,
        test_runner_dispatch,
        test_parse_knowledge_section_and_import,
        test_parse_limits_trips_messages_and_knowledge,
        test_import_restores_archived_and_reports_generic_knowledge_error,
        test_runner_knowledge_error_and_skip,
        test_runner_recovers_knowledge_only_backup,
        test_parse_user_export_tolerates_recursion_error,
        test_parse_truncation_drops_split_knowledge_group,
        test_runner_skips_reserved_and_deleted_sessions,
        test_runner_skips_trip_only_reserved_and_deleted_sessions,
        test_runner_rolls_back_session_deleted_during_write,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    print("\n全部通过")
