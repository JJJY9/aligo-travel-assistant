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
    test_import_roundtrip_and_idempotent()
    test_import_duplicate_message_counts()
    for case in (
        test_runner_json_output,
        test_runner_dry_run,
        test_runner_errors,
        test_runner_rejects_oversized_backup,
        test_runner_dispatch,
        test_parse_knowledge_section_and_import,
        test_runner_knowledge_error_and_skip,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    print("\n全部通过")
