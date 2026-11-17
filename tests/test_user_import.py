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


def test_backup_limit_problem_checks_counts_and_size(monkeypatch):
    """导出上限检查：条数与体积超出导入侧上限时给出具体原因。"""
    from utils import user_import as module

    assert module.backup_limit_problem({"chat_history": []}) is None
    monkeypatch.setattr(module, "_MAX_TRIPS", 1)
    assert "行程" in module.backup_limit_problem({"trip_history": [1, 2]})
    monkeypatch.setattr(module, "_MAX_MESSAGES", 1)
    assert "消息" in module.backup_limit_problem({"chat_history": [1, 2]})
    monkeypatch.setattr(module, "_MAX_PREFERENCES", 1)
    assert "偏好" in module.backup_limit_problem({"preferences": {"a": 1, "b": 2}})
    monkeypatch.setattr(module, "_MAX_KNOWLEDGE", 1)
    assert "知识片段" in module.backup_limit_problem({"knowledge": [1, 2]})
    monkeypatch.setattr(module, "MAX_BACKUP_BYTES", 10)
    assert "MB" in module.backup_limit_problem({"preferences": {"a": "x" * 100}})
    print("✓ backup_limit_problem 条数与体积上限")



def test_backup_limit_problem_matches_export_indent_and_surrogates(monkeypatch):
    """体积按导出实际写出的 indent=2 估算；孤立代理项不再让检查崩溃。"""
    import json as json_module

    from utils import user_import as module

    messages = [
        {"role": "user", "content": "x" * 50, "session_id": "s"} for _ in range(50)
    ]
    backup = {"chat_history": messages}
    compact = len(
        json_module.dumps(backup, ensure_ascii=False).encode("utf-8")
    )
    indented = len(
        json_module.dumps(backup, ensure_ascii=False, indent=2).encode("utf-8")
    )
    assert indented > compact
    monkeypatch.setattr(module, "MAX_BACKUP_BYTES", compact + 1)
    assert module.backup_limit_problem(backup) is not None, "缩进后体积超限必须判出"

    # 孤立代理项（旧数据）：先转义再估算，不能抛 UnicodeEncodeError
    surrogate = "bad " + chr(0xD800)
    assert (
        module.backup_limit_problem(
            {"chat_history": [{"role": "assistant", "content": surrogate}]}
        )
        is None
    )
    print("✓ backup_limit_problem 与导出体积口径一致且耐受孤立代理项")

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
    # 未补零的日期归一后保真保留（备份恢复优先于格式严格）；倒置区间
    # 同样按原样保留（整条丢弃会让备份静默丢行程），错误列表给出提示
    assert [t["start_date"] for t in sections["trips"]] == [
        "2026-01-01",
        "2026-12-05",
        "2026-12-01",
    ]
    inverted = [
        t for t in sections["trips"] if t["start_date"] == "2026-12-05"
    ]
    assert inverted and inverted[0]["end_date"] == "2026-12-01"
    assert len(sections["messages"]) == 1
    assert any("目的地为空" in e for e in errors)
    assert any("结束日期早于出发日期" in e and "保留" in e for e in errors)
    assert sum("角色或内容非法" in e for e in errors) == 2
    print("✓ 非法偏好 / 行程 / 消息逐条处理（脏日期保真、倒置保留）")


def test_import_keeps_inverted_date_trip():
    """倒置日期（结束早于出发）的行程随备份恢复，不再整条丢弃。"""
    payload = {
        "trip_history": [
            {
                "destination": "上海",
                "start_date": "2026-12-05",
                "end_date": "2026-12-01",
            }
        ]
    }
    sections, errors = parse_user_export(json.dumps(payload, ensure_ascii=False))
    assert any("按原样保留" in error for error in errors), errors
    store = _real_store("u-inverted")
    try:
        counts = import_user_data(store, sections)
        assert counts["trips_created"] == 1, counts
        trips = store.get_trip_history(limit=None)
        assert trips and trips[0]["end_date"] == "2026-12-01", trips
    finally:
        store._close() if hasattr(store, "_close") else None
    print("✓ 倒置日期行程随备份恢复")


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


def test_session_id_matches_web_word_semantics():
    """导入的会话 ID 校验与 Web 路径参数（pydantic-core 的 Rust \\w）逐字符一致。"""
    from typing import Annotated

    from pydantic import Field, TypeAdapter, ValidationError
    from utils.identifiers import is_valid_web_id

    adapter = TypeAdapter(Annotated[str, Field(pattern=r"^[\w-]{1,64}$")])
    samples = [
        "s1",
        "用户-1",
        "αβγ",
        "١٢٣",
        "a_b",
        "-lead",
        "a b",
        "a\nb",
        "é",
        "e" + chr(0x301),
        "a" + chr(0x200D) + "b",
        "a" + chr(0x200C) + "b",
        "a" + chr(0xB2),
        "a" * 64,
        "a" * 65,
        "",
        "..",
        "/x",
    ]
    for sample in samples:
        try:
            adapter.validate_python(sample)
            web_ok = True
        except ValidationError:
            web_ok = False
        assert is_valid_web_id(sample) == web_ok, repr(sample)
    print("✓ 导入的会话 ID 校验与 Web 路径一致")


def test_import_session_id_roundtrip_with_web_charset():
    """Web 可创建的零宽连接符会话能恢复；上标数字这类 Web 422 的 ID 被拒绝。"""
    join_id = "a" + chr(0x200D) + "b"
    sections, errors = parse_user_export(
        json.dumps(
            {
                "chat_history": [
                    {"role": "user", "content": "连接符会话", "session_id": join_id}
                ]
            }
        )
    )
    assert errors == [], errors
    assert [m["content"] for m in sections["messages"]] == ["连接符会话"]

    superscript = "a" + chr(0xB2)
    sections, errors = parse_user_export(
        json.dumps(
            {
                "chat_history": [
                    {"role": "user", "content": "上标会话", "session_id": superscript}
                ]
            }
        )
    )
    assert not sections.get("messages"), sections
    assert any("会话 ID 非法" in error for error in errors), errors
    print("✓ 会话 ID 往返字符集与 Web 一致")


def test_import_preserves_message_order():
    """恢复保持备份里的消息顺序：无时间戳旧消息不再被排到回答之后。"""
    payload = {
        "chat_history": [
            {"role": "user", "content": "无时间戳提问", "session_id": "s1"},
            {
                "role": "assistant",
                "content": "有时间戳回答",
                "session_id": "s1",
                "timestamp": "2020-06-01T10:00:00",
            },
            {"role": "user", "content": "较晚", "session_id": "s2", "timestamp": "2026-11-02T10:00:00"},
            {"role": "user", "content": "较早", "session_id": "s2", "timestamp": "2026-11-02T08:00:00"},
        ]
    }
    sections, _ = parse_user_export(json.dumps(payload, ensure_ascii=False))
    assert [m["content"] for m in sections["messages"]] == [
        "无时间戳提问",
        "有时间戳回答",
        "较晚",
        "较早",
    ], "全局排序会打乱会话内提问 / 回答顺序"
    store = _real_store("u-order")
    import_user_data(store, sections)
    s1 = store.get_chat_history(session_id="s1")
    assert [m["role"] for m in s1] == ["user", "assistant"], s1
    assert [m["content"] for m in s1] == ["无时间戳提问", "有时间戳回答"]
    print("✓ 恢复保持备份消息顺序")


def test_import_preserves_trip_id_and_timestamp():
    """备份里的 trip_id / timestamp 原样保留，新行程编号从水位继续。"""
    payload = {
        "trip_history": [
            {
                "trip_id": "trip_37",
                "origin": "北京",
                "destination": "上海",
                "start_date": "2026-12-01",
                "end_date": "2026-12-02",
                "purpose": "出差",
                "timestamp": "2020-06-01T10:00:00",
            }
        ]
    }
    sections, errors = parse_user_export(json.dumps(payload, ensure_ascii=False))
    assert errors == [], errors
    trip = sections["trips"][0]
    assert trip["trip_id"] == "trip_37"
    assert trip["timestamp"].startswith("2020-06-01"), trip["timestamp"]
    store = _real_store("u-trip-id")
    import_user_data(store, sections)
    restored = store.get_trip_history(limit=None)[0]
    assert restored["trip_id"] == "trip_37", restored
    assert str(restored["timestamp"]).startswith("2020-06-01"), restored
    next_id = store.save_trip_history(
        {
            "origin": "北京",
            "destination": "杭州",
            "start_date": "2026-12-03",
            "purpose": "出差",
        }
    )
    assert next_id == "trip_38", "编号水位要按还原的旧编号抬高"
    print("✓ 恢复保留 trip_id / timestamp 且编号水位继续")


def test_import_drop_sessions_clears_trip_link():
    """drop_sessions 显式清除的会话关联在恢复时写回（不复活死链接）。"""
    from utils.user_import import drop_sessions

    store = _real_store("u-drop-link")
    store.save_trip_history(
        {
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-12-01",
            "end_date": "2026-12-02",
            "purpose": "出差",
            "session_id": "s-gone",
        }
    )
    payload = {
        "trip_history": [
            {
                "origin": "北京",
                "destination": "上海",
                "start_date": "2026-12-01",
                "end_date": "2026-12-02",
                "purpose": "出差",
                "session_id": "s-gone",
            }
        ]
    }
    sections, _ = parse_user_export(json.dumps(payload, ensure_ascii=False))
    drop_sessions(sections, {"s-gone"})
    import_user_data(store, sections)
    restored = store.get_trip_history(limit=None)
    assert len(restored) == 1, restored
    assert restored[0].get("session_id") is None, restored
    print("✓ drop_sessions 的会话关联清除在恢复时生效")


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
    monkeypatch.setattr(cli, "_open_cli_store", lambda user_id, **kwargs: store)
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

        def boom(documents, dry_run=False, *, return_pieces=False):
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

    def fake_import(documents, dry_run=False, *, return_pieces=False):
        calls.append(documents)
        return {"imported": 1, "skipped": 0, "skipped_invalid": 0, "skipped_duplicate": 0}

    monkeypatch.setattr(admin_module, "import_documents", fake_import)
    counts = import_user_data(store, sections)
    assert counts["knowledge_imported"] == 1 and counts["knowledge_skipped"] == 0
    assert calls and calls[0][0]["content"] == "知识一"

    def missing_db(documents, dry_run=False, *, return_pieces=False):
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

    def missing(documents, dry_run=False, *, return_pieces=False):
        raise FileNotFoundError("知识库未初始化")

    monkeypatch.setattr(admin_module, "import_documents", missing)
    code, out = _run_import(monkeypatch, "u-kb-cli", path, store, as_json=True)
    payload = json.loads(out)
    assert code == 1 and payload["ok"] is False
    assert "knowledge_error" in payload
    assert store.get_preference().get("home_location") == "北京", "用户数据仍应恢复"

    calls = []

    def fake_import(documents, dry_run=False, *, return_pieces=False):
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


def test_import_enforces_preference_type_limit():
    """恢复后偏好类型并集超过上限时拒绝（否则导出会永久被拒）。"""
    store = _real_store("u-pref-limit")
    try:
        for index in range(200):
            store.save_preference(f"p{index}", "v")
        sections, _errors = parse_user_export(
            json.dumps({"preferences": {f"q{i}": "v" for i in range(200)}})
        )
        with pytest.raises(ValueError) as info:
            import_user_data(store, sections)
        assert "偏好类型" in str(info.value), info.value
        assert len(store.get_preference()) == 200, "拒绝时不能已写入部分新类型"
        # 覆盖同名类型不增加总量：并集仍为 200，允许恢复
        sections2, _errors2 = parse_user_export(
            json.dumps({"preferences": {f"p{i}": "new" for i in range(200)}})
        )
        import_user_data(store, sections2)
        assert len(store.get_preference()) == 200
    finally:
        store._close() if hasattr(store, "_close") else None
    print("✓ 偏好类型并集上限在恢复入口生效")


def test_import_keeps_existing_purpose_when_backup_lacks_it():
    """备份行程缺目的时不补默认「出差」：不能改写用户编辑过的目的。"""
    store = _real_store("u-purpose")
    try:
        store.save_trip_history(
            {
                "origin": "北京",
                "destination": "上海",
                "start_date": "2026-12-01",
                "end_date": "2026-12-03",
                "purpose": "拜访客户",
            }
        )
        sections, _errors = parse_user_export(
            json.dumps(
                {
                    "trip_history": [
                        {
                            "origin": "北京",
                            "destination": "上海",
                            "start_date": "2026-12-01",
                            "end_date": "2026-12-03",
                        }
                    ]
                }
            )
        )
        counts = import_user_data(store, sections)
        assert counts["trips_updated"] == 1, counts
        trips = store.get_trip_history(limit=None)
        assert trips[0]["purpose"] == "拜访客户", trips
    finally:
        store._close() if hasattr(store, "_close") else None
    print("✓ 恢复不覆盖既有行程目的")


def test_sections_limit_problem_reflects_export_format(monkeypatch):
    """恢复段按导出格式（indent=2）复核体积，紧凑备份不能绕过。"""
    from utils import user_import as module

    messages = [
        {"role": "user", "content": "内容", "session_id": "s1"} for _ in range(50)
    ]
    sections, _errors = parse_user_export(
        json.dumps({"chat_history": messages}, ensure_ascii=False)
    )
    monkeypatch.setattr(module, "MAX_BACKUP_BYTES", 10**9)
    assert module.sections_limit_problem(sections) is None
    compact_len = len(
        json.dumps(
            {"chat_history": sections["messages"]}, ensure_ascii=False
        ).encode("utf-8")
    )
    # 阈值落在紧凑体积与导出体积（indent=2 明显更大）之间时，紧凑备份也必须被拒
    monkeypatch.setattr(module, "MAX_BACKUP_BYTES", compact_len + 10)
    problem = module.sections_limit_problem(sections)
    assert problem and "MB" in problem, (compact_len, problem)
    print("✓ 恢复前按导出格式复核体积")


def test_parse_many_oversized_knowledge_groups_completes_quickly():
    """大量超大 parts 声明的分组必须快速丢弃（不能构造巨型 range 集合）。"""
    import time

    entries = [
        {
            "content": "x",
            "metadata": {"group": f"g{index}", "parts": 999999, "part": 1},
        }
        for index in range(2000)
    ]
    started = time.monotonic()
    sections, errors = parse_user_export(json.dumps({"knowledge": entries}))
    elapsed = time.monotonic() - started
    assert not sections.get("knowledge"), sections.get("knowledge")
    assert any("分片数声明异常" in error for error in errors), errors[:3]
    assert elapsed < 5, f"解析 2000 个超组耗时 {elapsed:.2f}s"
    print("✓ 超大知识分片组快速丢弃（无 range 构造）")


def test_parse_pinned_list_dedupes_in_linear_time():
    """接受 / 置顶列表去重走集合：大列表不能被放大成 O(n²)。"""
    messages = [
        {"role": "user", "content": "hi", "session_id": "s1"},
        {"role": "user", "content": "hello", "session_id": "s2"},
    ]
    pinned = ["s1"] * 20000 + ["s2"] * 20000
    sections, _errors = parse_user_export(
        json.dumps({"chat_history": messages, "pinned_sessions": pinned})
    )
    assert sections["pinned"] == ["s1", "s2"], sections["pinned"][:5]
    print("✓ 置顶 / 归档列表线性去重")


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

    def fake_import(documents, dry_run=False, *, return_pieces=False):
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


def _nested(depth: int, leaf: object) -> object:
    value = leaf
    for _ in range(depth):
        value = [value]
    return value


def test_parse_rejects_content_deeper_than_export_saves():
    """超过导出截断深度（sanitize_json 的 50）的非空内容在导入时拒绝。

    否则导入「成功」的数据在下一次导出被静默截成 null，备份链丢数据；
    已经导出过的备份在该深度只剩 null，必须仍可导入（导出 → 恢复闭环）。
    """
    from utils.json_safe import MAX_JSON_DEPTH

    # assistant JSON：60 层带内容 → 拒绝
    deep = json.dumps(_nested(MAX_JSON_DEPTH + 10, "x"))
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
    assert any("超过导出保存深度" in error for error in errors), errors

    # 导出产物形状（MAX_JSON_DEPTH 层容器 + 末端 null）→ 仍可导入
    exported = json.dumps(_nested(MAX_JSON_DEPTH, None))
    sections, errors = parse_user_export(
        json.dumps(
            {
                "chat_history": [
                    {"role": "assistant", "content": exported, "session_id": "s1"}
                ]
            },
            ensure_ascii=False,
        )
    )
    assert len(sections["messages"]) == 1, errors
    assert not any("嵌套" in error for error in errors), errors

    # 偏好值：绝对深度 = 自身深度 + 2（{"preferences": {name: value}}）
    payload = {
        "preferences": {
            "hotel_brands": _nested(MAX_JSON_DEPTH, "汉庭"),
            "ok_list": _nested(10, "如家"),
        }
    }
    sections, errors = parse_user_export(json.dumps(payload))
    assert "hotel_brands" not in sections.get("preferences", {}), sections
    assert sections["preferences"].get("ok_list") == _nested(10, "如家")
    assert any("超过导出保存深度" in error for error in errors), errors
    print("✓ 深于导出截断点的内容在导入时拒绝，已导出的 null 结尾备份仍可恢复")


def test_drop_sessions_only_clears_matching_trip_links():
    """被丢弃会话的行程关联只清真正指向它的记录，不误清存活会话的链接。"""
    from utils.user_import import drop_sessions

    backup_trip = {
        "origin": "北京",
        "destination": "上海",
        "start_date": "2026-12-01",
        "end_date": "2026-12-03",
        "purpose": "会议",
        "session_id": "s-dead",
    }

    # 目标库同一行程关联着另一条存活会话：恢复不能把活链接清掉
    store = LongTermMemory("u-trip-live", tempfile.mkdtemp())
    store.save_trip_history(
        {
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-12-01",
            "end_date": "2026-12-03",
            "purpose": "会议",
            "session_id": "s-live",
        }
    )
    payload = {
        "trip_history": [backup_trip],
        "chat_history": [{"role": "user", "content": "hi", "session_id": "s-dead"}],
    }
    sections, errors = parse_user_export(json.dumps(payload, ensure_ascii=False))
    drop_sessions(sections, {"s-dead"})
    counts = import_user_data(store, sections)
    assert counts["trips_updated"] == 1, counts
    trips = store.get_trip_history(limit=None)
    assert trips[0]["session_id"] == "s-live", "存活会话的既有链接不能被清掉"
    assert all("_dropped_session_id" not in trip for trip in trips), trips

    # 目标库同一行程链接的正是被丢弃的会话：清除死链
    store = LongTermMemory("u-trip-dead", tempfile.mkdtemp())
    store.save_trip_history(dict(backup_trip))
    payload = {"trip_history": [dict(backup_trip)]}
    sections, errors = parse_user_export(json.dumps(payload, ensure_ascii=False))
    drop_sessions(sections, {"s-dead"})
    import_user_data(store, sections)
    trips = store.get_trip_history(limit=None)
    assert trips[0].get("session_id") is None, "死链应被清除"
    assert "_dropped_session_id" not in trips[0]

    # 新建行程（目标库没有同路线记录）：不产生指向已删除会话的链接
    store = LongTermMemory("u-trip-new", tempfile.mkdtemp())
    sections, errors = parse_user_export(json.dumps(payload, ensure_ascii=False))
    drop_sessions(sections, {"s-dead"})
    counts = import_user_data(store, sections)
    assert counts["trips_created"] == 1, counts
    trips = store.get_trip_history(limit=None)
    assert "session_id" not in trips[0], trips
    assert "_dropped_session_id" not in trips[0]
    print("✓ 丢弃会话只清真正命中的行程关联（活链接保留）")


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


def test_parse_limits_trip_id_length_and_preference_keys():
    """超大 trip_id 丢弃编号、偏好键与 Web 同口径、上限不被非法键绕过。"""
    sections, errors = parse_user_export(
        json.dumps(
            {
                "trip_history": [
                    {
                        "trip_id": "trip_" + "9" * 19,
                        "destination": "上海",
                        "start_date": "2026-12-01",
                    },
                    {
                        "trip_id": "trip_123456789012345678",
                        "destination": "北京",
                        "start_date": "2026-12-02",
                    },
                ]
            }
        )
    )
    assert sections["trips"][0].get("trip_id") is None, (
        "19 位编号会毒化 BIGINT 编号水位，必须按新编号恢复"
    )
    assert sections["trips"][1]["trip_id"] == "trip_123456789012345678"
    assert any("trip_id" in error for error in errors), errors

    sections, errors = parse_user_export(
        json.dumps({"preferences": {"x" + chr(10) + "y": 1, " home": "上海"}})
    )
    assert "x" + chr(10) + "y" not in sections["preferences"]
    assert sections["preferences"] == {" home": "上海"}, (
        "键名不能 strip：改名会让相邻键碰撞互相覆盖"
    )
    assert any("键名非法" in error for error in errors)

    # 5000 个非法键：错误列表必须被 200 条上限截断（旧实现全部处理）
    many_bad = {f"bad/{index}": 1 for index in range(5000)}
    sections, errors = parse_user_export(json.dumps({"preferences": many_bad}))
    assert sections.get("preferences", {}) == {}
    assert len(errors) <= 205, len(errors)
    print("✓ trip_id / 偏好键 / 上限校验收口")


def test_import_rejects_when_existing_totals_exceed_limits():
    """存量 + 本备份越过导出上限时恢复必须中止（否则永久无法导出）。"""
    sections = {
        "preferences": {},
        "trips": [],
        "messages": [{"session_id": "s", "role": "user", "content": "hi"}],
    }

    class _FullStore:
        def get_statistics(self):
            return {"total_messages": 200000, "total_trips": 0}

        def save_preference(self, key, value):  # pragma: no cover - 不应被调用
            raise AssertionError("超限时不能开始写入")

    try:
        import_user_data(_FullStore(), sections)
    except ValueError as exc:
        assert "上限" in str(exc), exc
    else:
        raise AssertionError("越过导出上限的恢复必须被拒绝")

    class _TripFullStore:
        def get_statistics(self):
            return {"total_messages": 0, "total_trips": 5000}

    trip_sections = {
        "preferences": {},
        "trips": [{"destination": "上海", "start_date": "2026-12-01"}],
        "messages": [],
    }
    try:
        import_user_data(_TripFullStore(), trip_sections)
    except ValueError as exc:
        assert "上限" in str(exc), exc
    else:
        raise AssertionError("行程超限的恢复必须被拒绝")
    print("✓ 存量超限时恢复中止")


def test_preview_normalizes_legacy_integer_session_id():
    """dry-run 与真实导入共用归一化去重键（旧整数 session_id 不多报）。"""
    from utils.user_import import preview_user_import

    class _LegacyStore:
        def get_trip_history(self, limit=None):
            return []

        def get_chat_history(self, limit=None):
            return [{"session_id": 123, "role": "user", "content": "Q"}]

    sections = {
        "preferences": {},
        "trips": [],
        "messages": [{"session_id": "123", "role": "user", "content": "Q"}],
    }
    preview = preview_user_import(_LegacyStore(), sections)
    assert preview["messages_added"] == 0, preview
    assert preview["messages_skipped"] == 1, preview
    print("✓ 预览与导入的会话去重键归一化一致")


def test_import_repeat_backup_at_limit_is_idempotent(monkeypatch):
    """接近上限时重复导入自己的备份成功；--dry-run 与真实执行同一结论。"""
    from utils import user_import as module

    payload = {
        "trip_history": [
            {"origin": "北京", "destination": "上海", "start_date": "2026-03-01"}
        ],
        "chat_history": [{"role": "user", "content": "你好", "session_id": "s1"}],
    }
    sections, errors = module.parse_user_export(
        json.dumps(payload, ensure_ascii=False)
    )
    assert errors == [], errors
    # 备份里多一条新行程：先在正常上限下解析（解析侧也会按上限截断），
    # 再收紧恢复上限验证「新增越限」路径
    extra = json.loads(json.dumps(payload))
    extra["trip_history"].append(
        {"origin": "北京", "destination": "杭州", "start_date": "2026-03-02"}
    )
    sections2, _ = module.parse_user_export(
        json.dumps(extra, ensure_ascii=False)
    )
    assert len(sections2["trips"]) == 2

    store = _real_store("u-limit-idem")
    module.import_user_data(store, sections)
    stats = store.get_statistics()
    assert stats["total_trips"] == 1 and stats["total_messages"] == 1

    monkeypatch.setattr(module, "_MAX_TRIPS", 1)
    monkeypatch.setattr(module, "_MAX_MESSAGES", 1)
    counts = module.import_user_data(store, sections)
    assert counts["trips_created"] == 0 and counts["trips_updated"] == 1
    assert counts["messages_added"] == 0 and counts["messages_skipped"] == 1
    preview = module.preview_user_import(store, sections)
    assert preview["trips_created"] == 0 and preview["messages_added"] == 0

    with pytest.raises(ValueError, match="上限"):
        module.preview_user_import(store, sections2)
    with pytest.raises(ValueError, match="上限"):
        module.import_user_data(store, sections2)
    print("✓ 上限内幂等重放与 dry-run 一致")

def test_merged_trip_rows_empty_keys_backend_aware():
    """purpose / session_id 空键仅 PG 形状补写（JSON 后端不加）。"""
    from utils import user_import as module

    incoming = [
        {"origin": "上海", "destination": "成都", "start_date": "2026-05-01"}
    ]
    json_rows = module._merged_trip_rows([], incoming)
    assert "purpose" not in json_rows[0], json_rows[0]
    pg_rows = module._merged_trip_rows(
        [], incoming, include_empty_keys=True
    )
    assert pg_rows[0].get("purpose", "missing") is None
    print("✓ 合并预演的空键按后端形状补写")


def test_merged_backup_limit_previews_dedupe(monkeypatch):
    """重导自己的备份：合并上限检查按导入去重语义预演，接近上限不再误拒。"""
    from utils import user_import as module

    store = _real_store("u-merge-dedupe")
    sections, errors = parse_user_export(
        json.dumps(
            {
                "trip_history": [
                    {
                        "origin": "北京",
                        "destination": "上海",
                        "start_date": "2026-03-01",
                    },
                    {
                        "origin": "上海",
                        "destination": "杭州",
                        "start_date": "2026-04-01",
                    },
                ],
                "chat_history": [
                    {"role": "user", "content": "你好", "session_id": "s1"},
                    {"role": "assistant", "content": "你好！", "session_id": "s1"},
                ],
            },
            ensure_ascii=False,
        )
    )
    assert errors == [], errors
    module.import_user_data(store, sections)

    # 重导同一备份：重复行按去重语义跳过，合并后的条数等于现有条数
    monkeypatch.setattr(module, "_MAX_MESSAGES", 2)
    monkeypatch.setattr(module, "_MAX_TRIPS", 2)
    assert module.merged_backup_limit_problem(store, sections) is None, (
        "重导自己的备份不应被合并上限误拒"
    )

    # 备份里有真正新增的行：越过上限仍必须拒绝（去重预演不能放行新增）
    extra = dict(sections)
    extra["messages"] = list(sections["messages"]) + [
        {"session_id": "s1", "role": "user", "content": "新问题"}
    ]
    problem = module.merged_backup_limit_problem(store, extra)
    assert problem and "上限" in problem, problem

    # 体积轴：与现有行程同身份的行会被导入更新（不新增条数），更新后的
    # 行更大——合并体积必须按更新后的行估算，整行跳过会低估、放行一个
    # 让账号恢复后永久无法导出的恢复
    big_sections = dict(sections)
    big_rows = []
    for trip in sections["trips"]:
        row = dict(trip)
        row["purpose"] = "超长目的" * 100_000
        big_rows.append(row)
    big_sections["trips"] = big_rows
    monkeypatch.setattr(module, "MAX_BACKUP_BYTES", 400_000)
    problem = module.merged_backup_limit_problem(store, big_sections)
    assert problem and "超过导入上限" in problem, problem
    print("✓ 合并上限检查按导入去重语义预演（含更新行体积）")


def test_merged_trip_rows_mirror_storage_semantics():
    """合并预演与 save_trip_histories 一致：同身份只更新第一条既有行；
    同批重复新行先建后更新（前行其余字段保留）；缺目的地/日期的行逐条新增。"""
    from utils import user_import as module

    existing = [
        {
            "trip_id": "trip_1",
            "origin": "上海",
            "destination": "杭州",
            "start_date": "2026-03-01",
        },
        {
            # 同身份重复的历史行（旧数据）：存储层只更新 index 里的第一条
            "trip_id": "trip_2",
            "origin": "上海",
            "destination": "杭州",
            "start_date": "2026-03-01",
        },
        {
            "trip_id": "trip_3",
            "origin": "北京",
            "destination": "无日期",
        },
    ]
    trips = [
        {
            "origin": "上海",
            "destination": "杭州",
            "start_date": "2026-03-01",
            "purpose": "第一次更新",
        },
        {
            "origin": "上海",
            "destination": "杭州",
            "start_date": "2026-03-01",
            "purpose": "第二次更新",
        },
        {"origin": "北京", "destination": "无日期"},
        {"origin": "北京", "destination": "无日期"},
    ]
    merged = module._merged_trip_rows(existing, trips)
    assert len(merged) == 5, merged  # 3 条既有 + 2 条无身份新行
    assert merged[0]["purpose"] == "第二次更新", merged[0]
    assert "purpose" not in merged[1], "同身份只应更新第一条既有行"
    assert "purpose" not in existing[0], "预演不能原地修改既有行"

    # 同批重复的新行先建后更新：后行的 purpose 覆盖前行，前行其余字段保留
    fresh = module._merged_trip_rows(
        [],
        [
            {
                "origin": "上海",
                "destination": "成都",
                "start_date": "2026-05-01",
                "purpose": "长目的",
                "note": "保留字段",
            },
            {
                "origin": "上海",
                "destination": "成都",
                "start_date": "2026-05-01",
                "purpose": "短",
            },
        ],
    )
    assert len(fresh) == 1, fresh
    assert fresh[0]["purpose"] == "短"
    assert fresh[0]["note"] == "保留字段"
    # 存储层补写的字段同样进入预演形状；drop_sessions 内部标记不会落库
    marked = module._merged_trip_rows(
        [],
        [
            {
                "origin": "上海",
                "destination": "南京",
                "start_date": "2026-06-01",
                module._DROPPED_SESSION_KEY: "s-gone",
            }
        ],
    )
    assert len(marked) == 1
    assert "trip_id" in marked[0] and "timestamp" in marked[0], marked[0]
    assert module._DROPPED_SESSION_KEY not in marked[0], marked[0]

    # 与真实写入层对照：预演形状必须与 save_trip_histories 的落库结果一致
    # （身份匹配字段、purpose / session_id 覆盖、新行补写字段）
    from context.long_term_memory import LongTermMemory as _LTM

    storage_dir = tempfile.mkdtemp(prefix="chailv_merge_parity_")
    real_store = _LTM("u-merge-parity", storage_dir)
    real_store.save_trip_histories(
        [
            {
                "origin": "上海",
                "destination": "杭州",
                "start_date": "2026-03-01",
                "purpose": "旧目的",
                "session_id": "s-old",
            }
        ]
    )
    incoming = [
        {
            "origin": "上海",
            "destination": "杭州",
            "start_date": "2026-03-01",
            "purpose": "新目的",
            "session_id": "s-new",
        },
        {"origin": "北京", "destination": "无日期"},
        {"origin": "北京", "destination": "无日期"},
    ]
    preview = module._merged_trip_rows(
        real_store.get_trip_history(limit=None), incoming
    )
    real_store.save_trip_histories(incoming)
    stored = real_store.get_trip_history(limit=None)
    assert len(preview) == len(stored), (preview, stored)
    by_key = {
        (
            row.get("destination"),
            row.get("start_date"),
            row.get("purpose"),
        )
        for row in stored
    }
    preview_keys = {
        (
            row.get("destination"),
            row.get("start_date"),
            row.get("purpose"),
        )
        for row in preview
    }
    assert preview_keys == by_key, (preview_keys, by_key)
    print("✓ 合并预演与存储层的行程索引 / 更新语义一致（含补写字段）")


def test_import_without_user_data_keeps_quarantine_guidance():
    """隔离账号 + 备份不含任何用户数据：不重建主文件（重建会抹掉
    .corrupt-* / .quarantine 的恢复指引，让用户误以为已恢复），返回
    recovery_incomplete；一旦真正写回用户数据，才重建并解除隔离。"""
    from pathlib import Path as _Path

    from context.long_term_memory import LongTermMemory

    storage_dir = tempfile.mkdtemp(prefix="chailv_uimport_recover_")
    backup = (
        _Path(storage_dir)
        / "u-recover-empty.json.corrupt-20260101000000000000-1"
    )
    backup.write_text("{}", encoding="utf-8")
    store = LongTermMemory(
        "u-recover-empty", storage_dir, allow_quarantined=True
    )
    assert store.get_recovered_corrupt_backup()
    counts = import_user_data(
        store, {"preferences": {}, "trips": [], "messages": []}
    )
    assert counts.get("recovery_incomplete") is True, counts
    assert not (_Path(storage_dir) / "u-recover-empty.json").exists(), (
        "无用户数据的恢复不能重建主文件、抹掉隔离指引"
    )
    fresh = LongTermMemory("u-recover-empty", storage_dir)
    assert fresh.get_recovered_corrupt_backup(), "隔离证据必须仍然可见"
    assert backup.exists(), "隔离备份必须仍在磁盘上"

    # 真正写回用户数据：照常重建主文件并解除隔离
    counts = import_user_data(
        store, {"preferences": {"home_location": "北京"}}
    )
    assert "recovery_incomplete" not in counts
    assert (_Path(storage_dir) / "u-recover-empty.json").exists()
    recovered = LongTermMemory("u-recover-empty", storage_dir)
    assert recovered.get_recovered_corrupt_backup() is None
    print("✓ 无用户数据的隔离恢复保留指引，有数据才重建")


def test_preview_read_failure_raises_memory_error():
    """--dry-run 读取既有数据失败按持久化失败上报（503 脱敏），不能伪装成
    用户可修正的 ValueError（/api/import 会回 400 并回显底层错误文本）。"""
    from utils import user_import as module
    from utils.errors import MemoryPersistenceError

    class _BrokenStore:
        def get_trip_history(self, limit=None):
            raise OSError("disk")

    with pytest.raises(MemoryPersistenceError) as info:
        module.preview_user_import(
            _BrokenStore(),
            {"preferences": {}, "trips": [], "messages": []},
        )
    assert "读取既有行程失败" in str(info.value), str(info.value)

    class _BrokenChatStore:
        def get_trip_history(self, limit=None):
            return []

        def get_chat_history(self, limit=None):
            raise OSError("disk")

    with pytest.raises(MemoryPersistenceError) as info:
        module.preview_user_import(
            _BrokenChatStore(),
            {
                "preferences": {},
                "trips": [],
                "messages": [
                    {"role": "user", "content": "hi", "session_id": "s1"}
                ],
            },
        )
    assert "读取既有消息失败" in str(info.value), str(info.value)

    class _QuarantinedStore:
        def get_trip_history(self, limit=None):
            raise MemoryPersistenceError("记忆主文件缺失且存在损坏隔离备份")

    with pytest.raises(MemoryPersistenceError) as info:
        module.preview_user_import(
            _QuarantinedStore(),
            {"preferences": {}, "trips": [], "messages": []},
        )
    assert str(info.value).startswith("记忆主文件缺失且存在损坏隔离备份")
    print("✓ 合并预演读失败按持久化失败上报")


def test_merged_payload_top_level_placeholders_equal_length(monkeypatch):
    """合并体积复核的顶层占位与真实导出信封等长（短占位会低估体积）。"""
    from utils import user_import as module

    captured = {}

    def fake_limit(payload, **kwargs):
        captured["payload"] = payload
        return None

    monkeypatch.setattr(module, "backup_limit_problem", fake_limit)

    class _Store:
        snapshot_based = False

        def export_snapshot(self):
            return {
                "chat_history": [],
                "trip_history": [],
                "preferences": {},
                "session_titles": {},
                "pinned_sessions": [],
                "archived_sessions": [],
                "statistics": {},
            }

    assert module.merged_backup_limit_problem(_Store(), {}) is None
    payload = captured["payload"]
    assert len(payload["user_id"]) == 64, payload["user_id"]
    assert (
        len(payload["exported_at"]) == 26
        and payload["exported_at"].endswith("000000")
    ), payload["exported_at"]
    assert payload["session_id"] == ""
    # 真实导出恒含该键（空 dict 也序列化）
    assert payload["statistics"]["frequent_destinations"] == {}
    print("✓ 合并预演顶层占位等长")


def test_merged_trip_rows_wired_to_backend_shape(monkeypatch):
    """include_empty_keys 接线按后端：PG 行含空键与带时区时间戳，JSON 行
    没有；行级 trip_id 占位按落库后的最坏长度。"""
    from utils import user_import as module

    captured = {}

    def fake_limit(payload, **kwargs):
        captured["payload"] = payload
        return None

    monkeypatch.setattr(module, "backup_limit_problem", fake_limit)

    def make_store(snapshot_based):
        class _Store:
            pass

        store = _Store()
        store.snapshot_based = snapshot_based
        store.export_snapshot = lambda: {
            "chat_history": [],
            "trip_history": [],
            "preferences": {},
            "session_titles": {},
            "pinned_sessions": [],
            "archived_sessions": [],
            "statistics": {},
        }
        return store

    incoming = {
        "trips": [
            {"origin": "上海", "destination": "成都", "start_date": "2026-05-01"}
        ]
    }
    # JSON 后端 snapshot_based=True：导出行没有 purpose / session_id 键
    module.merged_backup_limit_problem(make_store(True), incoming)
    json_row = captured["payload"]["trip_history"][0]
    assert "purpose" not in json_row and "session_id" not in json_row, json_row
    # 编号仲裁：无既有水位时首行分配 trip_1（不再用 9*19 假占位）
    assert json_row["trip_id"] == "trip_1", json_row["trip_id"]
    assert len(json_row["timestamp"]) == 26, json_row["timestamp"]

    # PostgreSQL 后端 snapshot_based=False：空键也序列化，时间戳带时区
    module.merged_backup_limit_problem(make_store(False), incoming)
    pg_row = captured["payload"]["trip_history"][0]
    assert pg_row.get("purpose", "missing") is None, pg_row
    assert pg_row.get("session_id", "missing") is None, pg_row
    assert len(pg_row["timestamp"]) == 32, pg_row["timestamp"]
    print("✓ 合并预演行形状按后端接线")


def test_merged_statistics_trip_number_raised(monkeypatch):
    """预演 statistics.last_trip_number 随备份有效编号抬升（占位不计入）。"""
    from utils import user_import as module

    captured = {}

    def fake_limit(payload, **kwargs):
        captured["payload"] = payload
        return None

    monkeypatch.setattr(module, "backup_limit_problem", fake_limit)

    class _Store:
        snapshot_based = True

        def export_snapshot(self):
            return {
                "chat_history": [],
                "trip_history": [],
                "preferences": {},
                "session_titles": {},
                "pinned_sessions": [],
                "archived_sessions": [],
                "statistics": {"last_trip_number": 5},
            }

    module.merged_backup_limit_problem(
        _Store(),
        {
            "trips": [
                {
                    "origin": "上海",
                    "destination": "成都",
                    "start_date": "2026-05-01",
                    "trip_id": "trip_42",
                },
                {
                    "origin": "北京",
                    "destination": "西安",
                    "start_date": "2026-06-01",
                },
            ]
        },
    )
    stats = captured["payload"]["statistics"]
    # 保守上界：最大有效编号 42 + 新建行数（每行至少推进一次水位）
    assert stats["last_trip_number"] >= 43, stats["last_trip_number"]
    print("✓ 合并预演抬升 trip_id 水位（占位不参与）")


def test_merged_message_rows_timestamp_backend_shape(monkeypatch):
    """缺时间戳消息的合并占位按后端导出形状：PG 32 字符、JSON 26。"""
    from utils import user_import as module

    captured = {}

    def fake_limit(payload, **kwargs):
        captured["payload"] = payload
        return None

    monkeypatch.setattr(module, "backup_limit_problem", fake_limit)

    def make_store(snapshot_based):
        class _Store:
            pass

        store = _Store()
        store.snapshot_based = snapshot_based
        store.export_snapshot = lambda: {
            "chat_history": [],
            "trip_history": [],
            "preferences": {},
            "session_titles": {},
            "pinned_sessions": [],
            "archived_sessions": [],
            "statistics": {},
        }
        return store

    incoming = {
        "messages": [
            {
                "role": "user",
                "content": "hi",
                "session_id": "s1",
                "timestamp": None,
            }
        ]
    }
    module.merged_backup_limit_problem(make_store(False), incoming)
    pg_row = captured["payload"]["chat_history"][0]
    assert len(pg_row["timestamp"]) == 32, pg_row["timestamp"]

    module.merged_backup_limit_problem(make_store(True), incoming)
    json_row = captured["payload"]["chat_history"][0]
    assert len(json_row["timestamp"]) == 26, json_row["timestamp"]
    print("✓ 合并预演消息时间戳按后端形状占位")


def test_sections_limit_counts_missing_timestamps_as_export_shape(monkeypatch):
    """备份自身体积预检：缺时间戳行按导出端带时区时间戳计（不是 null）。"""
    from utils import user_import as module

    captured = {}

    def fake_limit(payload, **kwargs):
        captured["payload"] = payload
        return None

    monkeypatch.setattr(module, "backup_limit_problem", fake_limit)
    module.sections_limit_problem(
        {"messages": [{"role": "user", "content": "x", "timestamp": None}]}
    )
    row = captured["payload"]["chat_history"][0]
    assert isinstance(row["timestamp"], str), row
    # JSON 后端默认 26 字符（带时区 32 会把可恢复备份每条多算 6 字节）
    assert len(row["timestamp"]) == 26, row["timestamp"]
    module.sections_limit_problem(
        {"messages": [{"role": "user", "content": "x", "timestamp": None}]},
        pg_shape=True,
    )
    pg_row = captured["payload"]["chat_history"][0]
    assert len(pg_row["timestamp"]) == 32, pg_row["timestamp"]
    print("✓ 备份自身体积预检按导出时间戳形状计")


def test_merged_update_rows_get_timestamp(monkeypatch):
    """同身份更新行落库时无条件刷新时间戳：预演必须同样补占位，
    否则导出形状（含时间戳键）比预演多出每行约 40 字节。"""
    from utils import user_import as module

    captured = {}

    def fake_limit(payload, **kwargs):
        captured["payload"] = payload
        return None

    monkeypatch.setattr(module, "backup_limit_problem", fake_limit)

    def run(snapshot_based):
        class _Store:
            pass

        store = _Store()
        store.snapshot_based = snapshot_based
        store.export_snapshot = lambda: {
            "chat_history": [],
            "trip_history": [
                {
                    "origin": "上海",
                    "destination": "成都",
                    "start_date": "2026-05-01",
                }
            ],
            "preferences": {},
            "session_titles": {},
            "pinned_sessions": [],
            "archived_sessions": [],
            "statistics": {},
        }
        module.merged_backup_limit_problem(
            store,
            {
                "trips": [
                    {
                        "origin": "上海",
                        "destination": "成都",
                        "start_date": "2026-05-01",
                    }
                ]
            },
        )
        return captured["payload"]["trip_history"][0]

    # JSON 后端（snapshot_based=True）：naive 26 字符
    json_row = run(True)
    assert isinstance(json_row.get("timestamp"), str), json_row
    assert len(json_row["timestamp"]) == 26, json_row["timestamp"]
    # PostgreSQL 后端：带时区偏移 32 字符
    pg_row = run(False)
    assert isinstance(pg_row.get("timestamp"), str), pg_row
    assert len(pg_row["timestamp"]) == 32, pg_row["timestamp"]
    print("✓ 合并预演更新行补时间戳占位")


def test_merged_fallback_uses_backend_shape(monkeypatch):
    """快照读失败回退到备份自检时，缺时间戳占位仍按后端形状
    （JSON 26 / PG 32）。"""
    from utils import user_import as module

    captured = {}

    def fake_limit(payload, **kwargs):
        captured["payload"] = payload
        return None

    monkeypatch.setattr(module, "backup_limit_problem", fake_limit)

    def run(snapshot_based):
        class _Store:
            pass

        store = _Store()
        store.snapshot_based = snapshot_based

        def boom():
            raise RuntimeError("snapshot down")

        store.export_snapshot = boom
        module.merged_backup_limit_problem(
            store,
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "x",
                        "session_id": "s1",
                        "timestamp": None,
                    }
                ]
            },
        )
        return captured["payload"]["chat_history"][0]["timestamp"]

    assert len(run(True)) == 26
    assert len(run(False)) == 32
    print("✓ 合并回退分支的时间戳占位按后端形状")


def test_import_limit_read_failure_is_memory_error():
    """接近上限时读取既有数据失败：按持久化失败（503）上报，不能
    伪造「0 条重复」并回 400 引导用户清理历史。"""
    from utils import user_import as module
    from utils.errors import MemoryPersistenceError

    class _StatsStore:
        def get_statistics(self):
            return {"total_messages": 199_999}

        def get_chat_history(self, limit=None):
            raise MemoryPersistenceError(
                "PostgreSQL 操作失败：connection reset"
            )

    sections = {
        "messages": [
            {"role": "user", "content": "a", "session_id": "s1"},
            {"role": "assistant", "content": "b", "session_id": "s1"},
        ]
    }
    for call in (module.preview_user_import, module.import_user_data):
        with pytest.raises(MemoryPersistenceError):
            call(_StatsStore(), sections)

    class _TripStore:
        def get_statistics(self):
            return {"total_trips": 4_999}

        def get_trip_history(self, limit=None):
            raise OSError("lock timeout")

    trips_backup = {
        "trips": [
            {
                "origin": "上海",
                "destination": "成都",
                "start_date": "2026-05-01",
            },
            {
                "origin": "北京",
                "destination": "西安",
                "start_date": "2026-05-02",
            },
        ]
    }
    # 4_999 + 2 > 上限：预演与真实导入两条路径都必须按持久化失败上报
    for call in (module.preview_user_import, module.import_user_data):
        with pytest.raises(MemoryPersistenceError) as info:
            call(_TripStore(), trips_backup)
        assert "读取既有行程失败" in str(info.value), str(info.value)

    class _TripQuarantineStore:
        def get_statistics(self):
            return {"total_trips": 4_999}

        def get_trip_history(self, limit=None):
            raise MemoryPersistenceError("lock timeout")

    # 存储类异常原样透传（保留隔离等前缀供 503 文案分层）
    with pytest.raises(MemoryPersistenceError) as info:
        module.import_user_data(_TripQuarantineStore(), trips_backup)
    assert str(info.value) == "lock timeout"
    print("✓ 上限预检的读取失败按持久化失败上报")


def test_sections_limit_uses_neighbor_timestamp_anchor(monkeypatch):
    """缺时间戳消息的导出形状预检复用写入层模型：同会话有锚点时
    取邻居的 UTC 键（32 字符），不能按无锚点占位低估。"""
    from utils import user_import as module

    captured = {}

    def fake_limit(payload, **kwargs):
        captured["payload"] = payload
        return None

    monkeypatch.setattr(module, "backup_limit_problem", fake_limit)
    module.sections_limit_problem(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "a",
                    "session_id": "s1",
                    "timestamp": "2026-05-01T10:00:00.123456+08:00",
                },
                {
                    "role": "assistant",
                    "content": "b",
                    "session_id": "s1",
                    "timestamp": None,
                },
            ]
        }
    )
    rows = captured["payload"]["chat_history"]
    # JSON 形状预检按真实导出宽度估算：解析得到的带偏移时间戳截到 26 字符
    assert rows[0]["timestamp"] == "2026-05-01T10:00:00.123456", rows[0]
    anchored = rows[1]["timestamp"]
    assert isinstance(anchored, str) and len(anchored) == 26, rows[1]
    # 锚点语义保留：缺时间戳行取邻居的 UTC 键（10:00+08:00 → 02:00Z）
    assert anchored.startswith("2026-05-01T02:00:00"), anchored
    print("✓ 导出形状预检复用邻居时间戳锚点（JSON 宽度）")


def test_merged_trip_ids_mirror_storage_renumbering(monkeypatch):
    """预演复刻存储层编号仲裁：冲突的备份编号被改写成 trip_<水位>
    （18 位水位后为 24 字符），未被占用的编号沿用并抬水位。"""
    from utils import user_import as module

    captured = {}

    def fake_limit(payload, **kwargs):
        captured["payload"] = payload
        return None

    monkeypatch.setattr(module, "backup_limit_problem", fake_limit)

    class _Store:
        snapshot_based = True

        def export_snapshot(self):
            return {
                "chat_history": [],
                "trip_history": [
                    {
                        "origin": "上海",
                        "destination": "成都",
                        "start_date": "2026-05-01",
                        "trip_id": "trip_1",
                    },
                    {
                        "origin": "北京",
                        "destination": "西安",
                        "start_date": "2026-06-01",
                        "trip_id": "trip_2",
                    },
                ],
                "preferences": {},
                "session_titles": {},
                "pinned_sessions": [],
                "archived_sessions": [],
                "statistics": {"last_trip_number": 999999999999999999},
            }

    module.merged_backup_limit_problem(
        _Store(),
        {
            "trips": [
                {
                    # 同身份更新：编号保持不变
                    "origin": "上海",
                    "destination": "成都",
                    "start_date": "2026-05-01",
                },
                {
                    # 新身份但备份编号与既有行冲突：存储层会改号
                    "origin": "上海",
                    "destination": "杭州",
                    "start_date": "2026-07-01",
                    "trip_id": "trip_1",
                },
                {
                    # 未占用的备份编号：沿用并抬水位
                    "origin": "上海",
                    "destination": "南京",
                    "start_date": "2026-08-01",
                    "trip_id": "trip_5",
                },
            ]
        },
    )
    rows = captured["payload"]["trip_history"]
    assert rows[0]["trip_id"] == "trip_1"
    conflict_row = rows[2]
    assert conflict_row["trip_id"] == "trip_1000000000000000000", (
        conflict_row["trip_id"]
    )
    assert len(conflict_row["trip_id"]) == 24
    assert rows[3]["trip_id"] == "trip_5"
    stats = captured["payload"]["statistics"]
    assert stats["last_trip_number"] >= 10**18, stats["last_trip_number"]
    print("✓ 合并预演复刻编号仲裁（冲突改号/沿用抬水位）")


def test_merged_preview_uses_pg_counter_watermark(monkeypatch):
    """PG 账号的编号水位来自 aligo_trip_counters（快照 statistics 不带
    last_trip_number）：读得到按真实值起号，读不到按 BIGINT 上限
    （trip_ + 19 位 = 24 字符）占位；JSON 无统计从 trip_1 起号。"""
    from utils import user_import as module

    captured = {}

    def fake_limit(payload, **kwargs):
        captured["payload"] = payload
        return None

    monkeypatch.setattr(module, "backup_limit_problem", fake_limit)

    snapshot = {
        "chat_history": [],
        "trip_history": [],
        "preferences": {},
        "session_titles": {},
        "pinned_sessions": [],
        "archived_sessions": [],
        "statistics": {},
    }
    incoming = {
        "trips": [
            {"origin": "上海", "destination": "成都", "start_date": "2026-05-01"}
        ]
    }

    class _PgWithCounter:
        snapshot_based = False

        def export_snapshot(self):
            return dict(snapshot)

        def get_trip_number_watermark(self):
            return 7

    module.merged_backup_limit_problem(_PgWithCounter(), incoming)
    row = captured["payload"]["trip_history"][0]
    # 计数器小值必须真实采用（与未知回退的 24 字符占位可区分）
    assert row["trip_id"] == "trip_8", row["trip_id"]

    class _PgNoCounterApi:
        snapshot_based = False

        def export_snapshot(self):
            return dict(snapshot)

    module.merged_backup_limit_problem(_PgNoCounterApi(), incoming)
    row = captured["payload"]["trip_history"][0]
    assert row["trip_id"] == "trip_1000000000000000000", (
        "PG 水位未知时必须按 BIGINT 上限占位，不能从 trip_1 起算"
    )
    assert len(row["trip_id"]) == 24

    class _PgBrokenCounter:
        snapshot_based = False

        def export_snapshot(self):
            return dict(snapshot)

        def get_trip_number_watermark(self):
            raise RuntimeError("counter query failed")

    module.merged_backup_limit_problem(_PgBrokenCounter(), incoming)
    row = captured["payload"]["trip_history"][0]
    assert len(row["trip_id"]) == 24, (
        "读水位失败同样必须按最坏占位，不能静默从 trip_1 起算"
    )

    class _JsonStore:
        snapshot_based = True

        def export_snapshot(self):
            return dict(snapshot)

    module.merged_backup_limit_problem(_JsonStore(), incoming)
    assert captured["payload"]["trip_history"][0]["trip_id"] == "trip_1"
    print("✓ 合并预演按 PG 计数器水位 / 最坏占位起号")


def test_merged_preview_ids_match_real_storage(monkeypatch):
    """预演编号必须与真实 save_trip_histories 逐行一致（含重复折叠、
    冲突改号与沿用编号）。"""
    import tempfile
    from pathlib import Path as pathlib_path

    from config import STORAGE_CONFIG
    from context.long_term_memory import LongTermMemory
    from utils import user_import as module

    monkeypatch.setitem(STORAGE_CONFIG, "postgres_dsn", "")
    storage_dir = tempfile.mkdtemp()
    path = pathlib_path(storage_dir) / "u-ids.json"
    seed = {
        "user_id": "u-ids",
        "preferences": [],
        "chat_history": [],
        "trip_history": [],
        "session_titles": {},
        "pinned_sessions": [],
        "archived_sessions": [],
        "statistics": {
            "last_trip_number": 10**18 - 1,
            "total_trips": 0,
            "total_messages": 0,
            "frequent_destinations": {},
        },
    }
    path.write_text(json.dumps(seed), encoding="utf-8")
    store = LongTermMemory("u-ids", storage_dir)

    captured = {}

    def fake_limit(payload, **kwargs):
        captured["payload"] = payload
        return None

    monkeypatch.setattr(module, "backup_limit_problem", fake_limit)

    trips = [
        {"origin": "上海", "destination": "成都", "start_date": "2026-05-01"},
        {
            "origin": "上海",
            "destination": "成都",
            "start_date": "2026-05-01",
            "purpose": "会",
        },
        {
            "origin": "上海",
            "destination": "杭州",
            "start_date": "2026-07-01",
            "trip_id": "trip_1",
        },
        {
            "origin": "上海",
            "destination": "南京",
            "start_date": "2026-08-01",
            "trip_id": "trip_5",
        },
    ]
    module.merged_backup_limit_problem(
        store, {"trips": [dict(item) for item in trips]}
    )
    preview_ids = [
        row["trip_id"] for row in captured["payload"]["trip_history"]
    ]

    store.save_trip_histories([dict(item) for item in trips])
    stored_ids = [row["trip_id"] for row in store.get_trip_history(limit=None)]
    assert preview_ids == stored_ids, (preview_ids, stored_ids)
    print("✓ 合并预演编号与真实落库逐行一致")


def test_sections_limit_uses_merged_export_shape(monkeypatch):
    """备份自身体积预检与合并复核同一导出形状：同身份行程折叠、
    会话元数据与重算统计计入、PG 空键按后端补写。"""
    from utils import user_import as module

    captured = {}

    def fake_limit(payload, **kwargs):
        captured["payload"] = payload
        return None

    monkeypatch.setattr(module, "backup_limit_problem", fake_limit)

    sections = {
        "trips": [
            {"origin": "上海", "destination": "成都", "start_date": "2026-05-01"},
            {
                "origin": "上海",
                "destination": "成都",
                "start_date": "2026-05-01",
                "purpose": "会",
            },
        ],
        "messages": [],
        "titles": {"s1": "标题"},
        "pinned": ["s1"],
        "archived": [],
    }
    module.sections_limit_problem(sections)
    payload = captured["payload"]
    assert len(payload["trip_history"]) == 1, "同身份重复行必须折叠"
    assert payload["trip_history"][0]["purpose"] == "会"
    assert payload["session_titles"] == {"s1": "标题"}
    assert payload["pinned_sessions"] == ["s1"]
    assert payload["statistics"]["total_trips"] == 1
    assert payload["statistics"]["total_queries"] == 0
    assert payload["statistics"]["last_trip_number"] >= 1
    assert "frequent_destinations" in payload["statistics"]

    # 空键后端形状：单条无 purpose 行程，JSON 无键、PG 补 None

    single = {
        "trips": [
            {"origin": "北京", "destination": "西安", "start_date": "2026-06-01"}
        ]
    }
    module.sections_limit_problem(single)
    json_row = captured["payload"]["trip_history"][0]
    assert "purpose" not in json_row and "session_id" not in json_row
    module.sections_limit_problem(single, pg_shape=True)
    pg_payload = captured["payload"]
    pg_row = pg_payload["trip_history"][0]
    assert pg_row.get("purpose", "missing") is None
    assert pg_row.get("session_id", "missing") is None
    # 独立预检没有 store、拿不到 PG 计数器：起号从 0（不按最坏占位
    # 误拒），权威合并复核随后用真实水位兜底
    assert pg_row["trip_id"] == "trip_1", pg_row["trip_id"]
    assert "last_trip_number" not in pg_payload["statistics"], (
        "PG 的 get_statistics 没有该字段，预检保持后端真实形状"
    )

    # JSON 空行程：统计恒含 last_trip_number（0）
    module.sections_limit_problem({"trips": []})
    empty_stats = captured["payload"]["statistics"]
    assert empty_stats["last_trip_number"] == 0
    assert empty_stats["total_queries"] == 0
    print("✓ 备份自身体积预检与合并复核同形状")


def _fake_knowledge_planner(pieces):
    """规划替身：合并预检与真实导入共用 import_documents 的计划接口。"""

    def fake_import(documents, dry_run=False, *, return_pieces=False):
        if not dry_run:
            return {
                "imported": 0,
                "skipped": 0,
                "skipped_invalid": 0,
                "skipped_duplicate": 0,
                "skipped_conflict": 0,
            }
        payload = {
            "imported": 0,
            "skipped": 0,
            "skipped_invalid": 0,
            "skipped_duplicate": 0,
            "skipped_conflict": 0,
            "would_import": len(pieces),
        }
        if return_pieces:
            payload["pieces"] = [dict(piece) for piece in pieces]
        return payload

    return fake_import


def test_merged_preview_includes_global_knowledge(monkeypatch):
    """合并体积复核的知识段 = 现有全局库 + 导入层规划出的「真正会写入」
    分片（去重 / 分组 / 切分同源，重导自己的备份不会被双计误拒）。"""
    import utils.knowledge_admin as kb
    from utils import user_import as module

    captured = {}

    def fake_limit(payload, **kwargs):
        captured["payload"] = payload
        return None

    monkeypatch.setattr(module, "backup_limit_problem", fake_limit)
    monkeypatch.setattr(
        module,
        "_existing_knowledge_rows",
        lambda: [{"content": "旧知识", "metadata": {}}],
    )
    pieces = [
        {
            "content": "拆分后的新知识一",
            "metadata": {"part": "1", "parts": "2", "group": "g1"},
        },
        {
            "content": "拆分后的新知识二",
            "metadata": {"part": "2", "parts": "2", "group": "g1"},
        },
    ]
    monkeypatch.setattr(
        kb, "import_documents", _fake_knowledge_planner(pieces)
    )

    class _Store:
        snapshot_based = True

        def export_snapshot(self):
            return {
                "chat_history": [],
                "trip_history": [],
                "preferences": {},
                "session_titles": {},
                "pinned_sessions": [],
                "archived_sessions": [],
                "statistics": {},
            }

    module.merged_backup_limit_problem(
        _Store(), {"knowledge": [{"content": "原始长文"}]}
    )
    knowledge = captured["payload"]["knowledge"]
    assert knowledge[0]["content"] == "旧知识"
    # 规划出的分片原样计入（含派生元数据），不再自造体积估算
    assert [row["content"] for row in knowledge[1:]] == [
        piece["content"] for piece in pieces
    ]
    assert knowledge[1]["metadata"]["group"] == "g1"

    # 备份不带知识段：现有库也必须计入（带知识库导出会带上它们）
    module.merged_backup_limit_problem(_Store(), {})
    assert captured["payload"]["knowledge"] == [
        {"content": "旧知识", "metadata": {}}
    ]

    # 现有库读失败：不阻断用户数据恢复（知识轴跳过、不抛错）
    from utils.errors import MemoryPersistenceError as _MPE

    def boom(*args, **kwargs):
        raise _MPE("kb busy")

    monkeypatch.setattr(module, "_existing_knowledge_rows", boom)
    assert (
        module.merged_backup_limit_problem(
            _Store(), {"knowledge": [{"content": "新"}]}
        )
        is None
    )
    print("✓ 合并预检知识段与导入规划同源（含恒计入/读失败降级）")


def test_merged_knowledge_duplicate_backup_not_rejected(monkeypatch):
    """幂等重导自己的一体化备份：规划结果为空分片时知识段只剩现有库，
    不会因「现有 + 备份」双计超过上限。"""
    import utils.knowledge_admin as kb
    from utils import user_import as module

    captured = {}

    def fake_limit(payload, **kwargs):
        captured["payload"] = payload
        return None

    monkeypatch.setattr(module, "backup_limit_problem", fake_limit)
    existing = [
        {"content": f"分片{i}", "metadata": {"part": "1", "parts": "1"}}
        for i in range(30_000)
    ]
    monkeypatch.setattr(
        module, "_existing_knowledge_rows", lambda: list(existing)
    )
    # 导入层判重后没有任何新片段
    monkeypatch.setattr(kb, "import_documents", _fake_knowledge_planner([]))

    class _Store:
        snapshot_based = True

        def export_snapshot(self):
            return {
                "chat_history": [],
                "trip_history": [],
                "preferences": {},
                "session_titles": {},
                "pinned_sessions": [],
                "archived_sessions": [],
                "statistics": {},
            }

    backup_rows = [{"content": f"分片{i}"} for i in range(30_000)]
    assert (
        module.merged_backup_limit_problem(
            _Store(), {"knowledge": backup_rows}
        )
        is None
    )
    assert len(captured["payload"]["knowledge"]) == 30_000
    print("✓ 幂等重导知识备份不被双计误拒")


def test_merged_knowledge_over_limit_library_does_not_block(monkeypatch):
    """部署级知识库自身已超导入上限：任何恢复都无法把它推得更糟，
    不能据此拒绝用户数据（含 --skip-knowledge 路径）。"""
    from utils import user_import as module

    over_limit = [
        {"content": f"片{i}", "metadata": {}} for i in range(50_001)
    ]
    monkeypatch.setattr(
        module, "_existing_knowledge_rows", lambda: list(over_limit)
    )
    # 用真实计数上限判定基线：库自身超过 50000 片
    import utils.knowledge_admin as kb

    monkeypatch.setattr(
        kb, "import_documents", _fake_knowledge_planner([])
    )

    class _Store:
        snapshot_based = True

        def export_snapshot(self):
            return {
                "chat_history": [],
                "trip_history": [],
                "preferences": {},
                "session_titles": {},
                "pinned_sessions": [],
                "archived_sessions": [],
                "statistics": {},
            }

    # 走真实的 backup_limit_problem（基线判定依赖它）
    problem = module.merged_backup_limit_problem(
        _Store(), {"preferences": {"home_location": "北京"}}
    )
    assert problem is None, problem
    problem = module.merged_backup_limit_problem(
        _Store(), {"knowledge": [{"content": "新"}]}
    )
    assert problem is None, problem
    print("✓ 已超限的全局知识库不阻断用户数据恢复")


def test_merged_knowledge_increment_guard_preserved(monkeypatch):
    """合规知识库 + 本次新增分片越过上限：仍按增量拒绝（保护的正是
    「恢复后无法再导出」这一状态）。"""
    from utils import user_import as module
    import utils.knowledge_admin as kb

    existing = [
        {"content": f"片{i}", "metadata": {}} for i in range(49_999)
    ]
    monkeypatch.setattr(
        module, "_existing_knowledge_rows", lambda: list(existing)
    )
    pieces = [
        {"content": "新片一", "metadata": {}},
        {"content": "新片二", "metadata": {}},
    ]
    monkeypatch.setattr(
        kb, "import_documents", _fake_knowledge_planner(pieces)
    )

    class _Store:
        snapshot_based = True

        def export_snapshot(self):
            return {
                "chat_history": [],
                "trip_history": [],
                "preferences": {},
                "session_titles": {},
                "pinned_sessions": [],
                "archived_sessions": [],
                "statistics": {},
            }

    problem = module.merged_backup_limit_problem(
        _Store(), {"knowledge": [{"content": "原始"}]}
    )
    assert problem and "知识片段" in problem, problem
    print("✓ 合规知识库的增量越限仍被拒绝")


def test_merged_knowledge_baseline_uses_full_export_shape(monkeypatch):
    """知识轴的基线是「导入前完整导出形状」（存量 + 现有库）：库单独合规
    但两者合计已超限时，任何恢复都无法更糟，不得据此拒绝用户数据；
    用户数据轴仍照常复核。"""
    from utils import user_import as module
    import utils.knowledge_admin as kb

    monkeypatch.setattr(module, "MAX_BACKUP_BYTES", 8000)
    kb_rows = [
        {"content": "k" * 300, "metadata": {"part": "1", "parts": "1"}}
        for _ in range(6)
    ]
    monkeypatch.setattr(
        module, "_existing_knowledge_rows", lambda: list(kb_rows)
    )
    monkeypatch.setattr(kb, "import_documents", _fake_knowledge_planner([]))
    messages = [
        {
            "role": "user",
            "content": "m" * 300,
            "session_id": "s1",
            "timestamp": "2026-01-01T00:00:00.000000",
        }
        for _ in range(14)
    ]

    class _Store:
        snapshot_based = True

        def export_snapshot(self):
            return {
                "chat_history": list(messages),
                "trip_history": [],
                "preferences": {},
                "session_titles": {},
                "pinned_sessions": [],
                "archived_sessions": [],
                "statistics": {},
            }

    # 库单独合规、合计超限
    assert module.backup_limit_problem({"knowledge": list(kb_rows)}) is None
    assert (
        module.merged_backup_limit_problem(
            _Store(), {"preferences": {"home_location": "北京"}}
        )
        is None
    )
    # 幂等重导自身备份（消息全部命中去重、零写入）同样放行
    idempotent = {"messages": [dict(message) for message in messages]}
    assert module.merged_backup_limit_problem(_Store(), idempotent) is None
    print("✓ 知识轴基线按导入前完整导出形状判定")


def test_merged_knowledge_size_axis_baseline(monkeypatch):
    """基线的尺寸轴：知识库单独超过（按导出格式）体积上限时跳过知识轴。"""
    from utils import user_import as module
    import utils.knowledge_admin as kb

    monkeypatch.setattr(module, "MAX_BACKUP_BYTES", 2000)
    kb_rows = [
        {"content": "k" * 700, "metadata": {}} for _ in range(5)
    ]
    monkeypatch.setattr(
        module, "_existing_knowledge_rows", lambda: list(kb_rows)
    )
    monkeypatch.setattr(kb, "import_documents", _fake_knowledge_planner([]))

    class _Store:
        snapshot_based = True

        def export_snapshot(self):
            return {
                "chat_history": [],
                "trip_history": [],
                "preferences": {},
                "session_titles": {},
                "pinned_sessions": [],
                "archived_sessions": [],
                "statistics": {},
            }

    assert module.backup_limit_problem({"knowledge": list(kb_rows)}) is not None
    assert (
        module.merged_backup_limit_problem(
            _Store(), {"preferences": {"home_location": "北京"}}
        )
        is None
    )
    print("✓ 知识轴尺寸基线超限时跳过")


def test_merged_user_data_baseline_allows_oversized_account(monkeypatch):
    """存量用户数据自身（不含知识库）已超导出上限的账号（正常聊天积累
    到 50MB 以上）：任何恢复都无法让它更糟，体积轴不得把该账号的所有
    导入都 413（连幂等重导自身备份都不行）；条数轴仍照常拒绝。"""
    from utils import user_import as module

    messages = [
        {
            "role": "user",
            "content": "m" * 3000,
            "session_id": "s1",
            "timestamp": "2026-01-01T00:00:00.000000",
        }
        for _ in range(10)
    ]

    class _Store:
        snapshot_based = True

        def export_snapshot(self):
            return {
                "chat_history": [dict(item) for item in messages],
                "trip_history": [],
                "preferences": {},
                "session_titles": {},
                "pinned_sessions": [],
                "archived_sessions": [],
                "statistics": {},
            }

    monkeypatch.setattr(module, "MAX_BACKUP_BYTES", 8000)
    assert (
        module.backup_limit_problem(
            module._export_shape_baseline(_Store().export_snapshot(), [])
        )
        is not None
    )
    # 小备份不再被体积轴拒绝（否则该账号换机 / 恢复完全不可用）
    assert (
        module.merged_backup_limit_problem(
            _Store(), {"preferences": {"home_location": "北京"}}
        )
        is None
    )
    # 幂等重导自身备份（消息全部命中去重、零写入）同样放行
    assert (
        module.merged_backup_limit_problem(
            _Store(), {"messages": [dict(item) for item in messages]}
        )
        is None
    )
    # 条数轴照常生效：把账号推过消息条数上限的备份仍然拒绝
    monkeypatch.setattr(module, "_MAX_MESSAGES", 12)
    assert (
        module.merged_backup_limit_problem(
            _Store(), {"preferences": {"home_location": "北京"}}
        )
        is None
    )
    too_many = {
        "messages": [dict(item) for item in messages]
        + [
            {"role": "user", "content": "新", "session_id": "s2"}
            for _ in range(5)
        ]
    }
    problem = module.merged_backup_limit_problem(_Store(), too_many)
    assert problem and "上限" in problem, problem
    # 存量自身已超条数上限（硬上限上线前的越限账号）：条数轴同样豁免，
    # 任何导入都不能被存量条数拒绝；_enforce_import_limits 同口径
    monkeypatch.setattr(module, "_MAX_MESSAGES", 8)

    class _CapStore:
        def get_statistics(self):
            return {"total_messages": 10}

    assert (
        module.merged_backup_limit_problem(
            _Store(), {"preferences": {"home_location": "北京"}}
        )
        is None
    )
    # 已越限账号导入新增消息也不再按「推过上限」拒绝（与基线语义一致）
    module._enforce_import_limits(
        _CapStore(),
        {"messages": [{"role": "user", "content": "新", "session_id": "s2"}]},
    )
    print("✓ 存量已超限账号的体积轴放行、条数轴保留")


def test_sections_limit_baseline_exempts_over_limit_axis(monkeypatch):
    """备份自身体积预检同样按导入前基线豁免：存量已超限的账号恢复
    自身体积超限的备份不能被预检 413（合并复核的放行必须可达）；
    不同轴互不豁免。"""
    from utils import user_import as module

    monkeypatch.setattr(module, "MAX_BACKUP_BYTES", 2000)
    message = {
        "role": "user",
        "content": "m" * 3000,
        "session_id": "s1",
        "timestamp": "2026-01-01T00:00:00.000000",
    }
    sections = {"messages": [dict(message)]}
    existing = {
        "chat_history": [],
        "trip_history": [
            {
                "origin": "A",
                "destination": "B",
                "start_date": "2026-01-01",
                "purpose": "p" * 3000,
            }
        ],
        "preferences": {},
        "session_titles": {},
        "pinned_sessions": [],
        "archived_sessions": [],
        "statistics": {},
    }
    # 无基线：原严格口径（体积超限）
    assert module.sections_limit_problem(sections) is not None
    # 存量体积已超限：体积轴豁免；存量未超限时保持原判定
    assert module.sections_limit_problem(sections, existing=existing) is None
    assert (
        module.sections_limit_problem(sections, existing={"chat_history": []})
        is not None
    )
    # 不同轴互不豁免：存量只在体积轴超限，不能放行条数轴超限的备份
    monkeypatch.setattr(module, "_MAX_MESSAGES", 0)
    problem = module.sections_limit_problem(sections, existing=existing)
    assert problem and "消息" in problem, problem
    print("✓ 备份体积预检按导入前基线豁免")


def test_cli_import_baseline_allows_oversized_backup(monkeypatch):
    """CLI 入口：存量已超导出上限的账号导入「按导出形状超限」的备份
    不再被预检拒绝（预检需要打开 store 取基线，与合并复核同结论）。"""
    from utils import user_import as module

    # 原始文件（紧凑 JSON）低于上限、按导出形状（indent=2）超限：
    # 覆盖真实场景里「紧凑备份预检通过、导出形状复核超限」的差异
    monkeypatch.setattr(module, "MAX_BACKUP_BYTES", 8000)
    storage_dir = tempfile.mkdtemp(prefix="chailv_uimport_bigcli_")
    store = LongTermMemory("u-bigcli", storage_dir)
    for index in range(60):
        store.add_chat_message("user", "m" * 40, f"old-{index}")
    path = _write_json(
        {
            "chat_history": [
                {
                    "role": "user",
                    "content": "m" * 40,
                    "session_id": f"new-{index}",
                }
                for index in range(60)
            ]
        }
    )
    code, out = _run_import(monkeypatch, "u-bigcli", path, store)
    assert code == 0, out
    assert len(store.get_chat_history()) == 120
    print("✓ 存量超限账号的 CLI 导入按基线放行")


def test_import_user_data_over_limit_account_real_write(monkeypatch):
    """预检与写层同口径：存量已越限账号导入含新增行的备份真实执行成功
    （不再出现「预览成功、真实写入 400」的部分失败），重复导入幂等。"""
    import utils.trip_import as trip_module
    from utils import user_import as module

    storage_dir = tempfile.mkdtemp(prefix="chailv_uimport_over_")
    store = LongTermMemory("u-over-write", storage_dir)
    for index in range(3):
        store.save_trip_history(
            {"destination": f"城市{index}", "start_date": "2026-01-01"}
        )
    for index in range(3):
        store.add_chat_message("user", "old", f"s{index}")
    # 种入越限存量之后再收紧上限（上限上线前的历史账号）
    monkeypatch.setattr(trip_module, "MAX_TOTAL_TRIPS", 2)
    monkeypatch.setattr(module, "_MAX_MESSAGES", 2)
    sections = {
        "messages": [{"role": "user", "content": "new", "session_id": "s9"}],
        "trips": [{"destination": "新城市", "start_date": "2026-02-01"}],
    }
    preview = module.preview_user_import(store, sections)
    assert preview["messages_added"] == 1, preview
    counts = module.import_user_data(store, sections)
    assert counts["messages_added"] == 1, counts
    assert counts["trips_created"] == 1, counts
    # 幂等重导（本批新增为 0）：写层同样放行
    counts = module.import_user_data(store, sections)
    assert counts["messages_added"] == 0, counts
    assert counts["trips_created"] == 0, counts
    print("✓ 存量超限账号的真实导入与幂等重导放行")


def test_sections_limit_defers_knowledge_axis_to_merged(monkeypatch):
    """有存量快照时备份自身体积预检不把知识段计入：知识轴（含其体积）
    由随后的合并复核按「用户数据 + 现有知识库」完整基线判定，否则
    部署级超限知识库的重导会被预检 413 而 merged 本会放行。"""
    from utils import user_import as module

    monkeypatch.setattr(module, "MAX_BACKUP_BYTES", 3000)
    sections = {"knowledge": [{"content": "k" * 4000, "metadata": {}}]}
    existing = {
        "chat_history": [],
        "trip_history": [],
        "preferences": {},
        "session_titles": {},
        "pinned_sessions": [],
        "archived_sessions": [],
        "statistics": {},
    }
    # 无基线（快照读不到、后续没有权威复核）：保持严格口径
    assert module.sections_limit_problem(sections) is not None
    # 有基线：知识段不计入预检体积（交给 merged 的完整基线判定）
    assert (
        module.sections_limit_problem(sections, existing=existing) is None
    )
    print("✓ 备份预检把知识轴交给合并复核")


def test_cli_import_reports_partial_commit(monkeypatch):
    """CLI user-import 遇 PartialImportError 如实报告已提交条数并可安全
    重跑（与 trips-import / Web 同口径），不再只说「写入失败」。"""
    from utils import user_import as module
    from utils.errors import PartialImportError

    def boom(store, sections):
        raise PartialImportError("批量保存在第 1 条后失败：db down", 1, 0)

    monkeypatch.setattr(module, "import_user_data", boom)
    storage_dir = tempfile.mkdtemp(prefix="chailv_uimport_partial_")
    store = LongTermMemory("u-partial", storage_dir)
    path = _write_json({"preferences": {"home_location": "北京"}})
    code, out = _run_import(monkeypatch, "u-partial", path, store)
    assert code == 1, out
    assert "已提交 1 条新增、0 条更新" in out, out
    assert "可安全重跑" in out, out
    code, out = _run_import(monkeypatch, "u-partial", path, store, as_json=True)
    payload = json.loads(out)
    assert code == 1 and payload["ok"] is False, payload
    assert "已提交 1 条新增" in payload["error"], payload
    print("✓ 主恢复入口报告部分提交")


def test_recovery_incomplete_reported_with_knowledge_error(monkeypatch):
    """知识片段恢复失败不能吞掉「用户数据未恢复」：隔离账号 + 备份只有
    保留字会话消息 + 一条知识片段，导入结果同时带 knowledge_error 与
    recovery_incomplete，CLI 文案不得声称「用户数据已恢复」。"""
    import utils.knowledge_admin as kb
    from pathlib import Path as pathlib_path

    def boom(documents, *args, **kwargs):
        raise RuntimeError("kb down")

    monkeypatch.setattr(kb, "import_documents", boom)
    storage_dir = tempfile.mkdtemp(prefix="chailv_uimport_combo_")
    leftover = (
        pathlib_path(storage_dir)
        / "u-combo.json.corrupt-20260101000000000000-9"
    )
    leftover.write_text("{}", encoding="utf-8")
    store = LongTermMemory("u-combo", storage_dir, allow_quarantined=True)
    assert store.get_recovered_corrupt_backup()
    path = _write_json(
        {
            "chat_history": [
                {"role": "user", "content": "x", "session_id": "export"}
            ],
            "knowledge": [{"content": "知识片段"}],
        }
    )
    code, out = _run_import(monkeypatch, "u-combo", path, store, as_json=True)
    payload = json.loads(out)
    assert code == 1, (code, out)
    assert payload.get("recovery_incomplete") is True, payload
    assert payload.get("knowledge_error"), payload
    # 预览与真实执行同结论：--dry-run 同样报告恢复未完成
    code, out = _run_import(
        monkeypatch, "u-combo", path, store, as_json=True, dry_run=True
    )
    payload = json.loads(out)
    assert code == 1, (code, out)
    assert payload.get("recovery_incomplete") is True, payload
    code, out = _run_import(monkeypatch, "u-combo", path, store)
    assert code == 1, (code, out)
    assert "⚠ 用户数据未恢复" in out, out
    assert "用户数据已恢复" not in out, out
    print("✓ 知识失败与空恢复同时报告")


def test_user_import_warns_when_quarantined_and_no_user_data(monkeypatch):
    """隔离账号 + 备份无可写回用户数据：CLI ok=false、退出码 1 并明确
    提示账号仍处隔离态（不能报告为恢复成功）。"""
    from pathlib import Path as pathlib_path

    from context.long_term_memory import LongTermMemory

    storage_dir = tempfile.mkdtemp(prefix="chailv_uimport_incomplete_")
    leftover = (
        pathlib_path(storage_dir)
        / "u-inc.json.corrupt-20260101000000000000-9"
    )
    leftover.write_text("{}", encoding="utf-8")
    store = LongTermMemory("u-inc", storage_dir, allow_quarantined=True)
    assert store.get_recovered_corrupt_backup()
    path = _write_json(
        {
            "chat_history": [
                {"role": "user", "content": "x", "session_id": "export"}
            ]
        }
    )
    code, out = _run_import(monkeypatch, "u-inc", path, store)
    assert code == 1, (code, out)
    assert "用户数据未恢复" in out, out
    code, out = _run_import(monkeypatch, "u-inc", path, store, as_json=True)
    payload = json.loads(out)
    assert code == 1 and payload["ok"] is False, payload
    assert payload.get("recovery_incomplete") is True, payload
    print("✓ 隔离账号空恢复 CLI 报告未完成")


def test_backup_size_accounts_for_lf_vs_crlf(monkeypatch):
    """Windows 上 Web 响应体是 LF：体积轴按通道选择行尾口径，
    否则接近 50 MB 的合法 Web 备份每行多算 1 字节被误判超限。"""
    from utils import user_import as module

    backup = {"preferences": {"home_location": "北京", "note": "x" * 200}}
    lf = module._backup_payload_size(backup, crlf=False)
    crlf = module._backup_payload_size(backup, crlf=True)
    assert crlf > lf, (lf, crlf)
    monkeypatch.setattr(module, "MAX_BACKUP_BYTES", (lf + crlf) // 2)
    assert module.backup_limit_problem(backup, crlf=False) is None
    assert module.backup_limit_problem(backup, crlf=True) is not None
    print("✓ 体积轴按 LF/CRLF 通道口径区分")


def test_merged_reuses_provided_snapshot(monkeypatch):
    """调用方传入导出快照后合并复核不得再次全量读取（两次读取既费时
    又可能读到不一致的快照）。"""
    from utils import user_import as module

    calls = {"n": 0}

    class _Store:
        snapshot_based = True

        def export_snapshot(self):
            calls["n"] += 1
            return {
                "chat_history": [],
                "trip_history": [],
                "preferences": {},
                "session_titles": {},
                "pinned_sessions": [],
                "archived_sessions": [],
                "statistics": {},
            }

    snapshot = _Store().export_snapshot()
    calls["n"] = 0
    assert (
        module.merged_backup_limit_problem(
            _Store(), {"preferences": {"home_location": "北京"}},
            existing=snapshot,
        )
        is None
    )
    assert calls["n"] == 0, "传入快照后不得再次读取"
    module.merged_backup_limit_problem(
        _Store(), {"preferences": {"home_location": "北京"}}
    )
    assert calls["n"] == 1
    print("✓ 合并复核复用调用方快照")


def test_merged_fallback_counts_full_timestamp_width(monkeypatch):
    """快照读不到时该预检即最终判定：JSON 时间戳按落库后的真实宽度
    32 计（不再截成 26 低估，避免放行恢复后永久 413 的备份）。"""
    from utils import user_import as module

    captured = {}

    def fake_limit(payload, **kwargs):
        captured["payload"] = payload
        return None

    monkeypatch.setattr(module, "backup_limit_problem", fake_limit)

    class _Broken:
        snapshot_based = True

        def export_snapshot(self):
            raise RuntimeError("disk busy")

    module.merged_backup_limit_problem(
        _Broken(),
        {
            "messages": [
                {
                    "role": "user",
                    "content": "x",
                    "session_id": "s1",
                    "timestamp": "2026-05-01T10:00:00.123456+08:00",
                }
            ]
        },
    )
    rows = captured["payload"]["chat_history"]
    assert len(rows[0]["timestamp"]) == 32, rows[0]
    print("✓ 回退口径按真实时间戳宽度计")


def test_merged_payload_includes_snapshot_extra_fields(monkeypatch):
    """合并体积复核以现有快照为底：statistics 等额外字段同样计入
    （真实 /api/export 会带上它们，缺失会让预演偏乐观）。"""
    from utils import user_import as module

    class _Store:
        def export_snapshot(self):
            return {
                "chat_history": [],
                "trip_history": [],
                "preferences": {},
                "custom_pointer": "y" * 2000,
                "session_titles": {"s1": "t"},
                "pinned_sessions": [],
                "archived_sessions": [],
                "statistics": {"total_queries": 1},
                "user_id": "u1",
                "exported_at": "2026-01-01T00:00:00",
            }

    monkeypatch.setattr(module, "MAX_BACKUP_BYTES", 4000)
    problem = module.merged_backup_limit_problem(
        _Store(),
        {
            # 写入的偏好本身不超限：只有把现有快照的额外字段计入才超限
            "preferences": {"home_location": "p" * 2000},
            "trips": [],
            "messages": [],
        },
    )
    assert problem and "超过导入上限" in problem, problem
    print("✓ 合并复核计入现有快照的额外字段")


def test_merged_preview_keeps_existing_frequent_destinations(monkeypatch):
    """存量 statistics 与 trip_history 不一致时取并集（保守，不低估体积）。"""
    from utils import user_import as module

    class _Store:
        def export_snapshot(self):
            return {
                "chat_history": [],
                "trip_history": [],
                "preferences": {},
                "session_titles": {},
                "pinned_sessions": [],
                "archived_sessions": [],
                "statistics": {
                    "frequent_destinations": {"旧城市" * 600: 3},
                },
            }

    monkeypatch.setattr(module, "MAX_BACKUP_BYTES", 6000)
    problem = module.merged_backup_limit_problem(
        _Store(),
        {
            "preferences": {"home_location": "p" * 1500},
            "trips": [],
            "messages": [],
        },
    )
    # 存量统计（旧城市×180 字 × 3 的键文本）保留 → 合并超限；
    # 丢掉存量 frequent_destinations 则合并体积在上限内
    assert problem and "超过导入上限" in problem, problem
    print("✓ 合并预演保留存量 frequent_destinations")


def test_merged_payload_recomputes_frequent_destinations(monkeypatch):
    """导入会为每个唯一目的地复制完整文本进 statistics：合并体积按导入后
    重算（沿用导入前快照会系统性低估、放行导出永久 413 的恢复）。"""
    from utils import user_import as module

    class _Store:
        def export_snapshot(self):
            return {
                "chat_history": [],
                "trip_history": [],
                "preferences": {},
                "session_titles": {},
                "pinned_sessions": [],
                "archived_sessions": [],
                "statistics": {},
            }

    big_dest = "目的地" * 80
    trips = [
        {
            "origin": "上海",
            "destination": f"{big_dest}{i}",
            "start_date": "2027-01-01",
        }
        for i in range(50)
    ]
    monkeypatch.setattr(module, "MAX_BACKUP_BYTES", 50_000)
    problem = module.merged_backup_limit_problem(
        _Store(), {"preferences": {}, "trips": trips, "messages": []}
    )
    assert problem and "超过导入上限" in problem, problem
    print("✓ 合并体积按导入后重算 frequent_destinations")


def test_preview_counts_identityless_trips_as_created():
    """--dry-run 与真实导入同一去重口径：缺目的地 / 出发日期的行程行
    逐条新增（裸 trip_identity 会把它们折叠，预览与真跑结论相反）。"""
    from utils.user_import import preview_user_import

    class _Store:
        def get_trip_history(self, limit=None):
            return []

        def get_statistics(self):
            return {"total_messages": 0, "total_trips": 0}

        def get_chat_history(self, limit=None):
            return []

        def get_preference(self):
            return {}

    sections = {
        "preferences": {},
        "trips": [
            {"origin": "北京", "destination": "无日期"},
            {"origin": "北京", "destination": "无日期"},
        ],
        "messages": [],
    }
    preview = preview_user_import(_Store(), sections)
    assert preview["trips_created"] == 2, preview
    print("✓ 预览把无身份行程按逐条新增计数")


def test_import_accepts_device_like_session_ids():
    """会话 ID 不拼文件名：nul / con 不再被判非法并丢弃消息。"""
    payload = {
        "chat_history": [
            {"role": "user", "content": "hi", "session_id": "nul"}
        ]
    }
    sections, errors = parse_user_export(
        json.dumps(payload, ensure_ascii=False)
    )
    assert errors == [], errors
    assert sections["messages"][0]["session_id"] == "nul"
    store = _real_store("u-device-session")
    counts = import_user_data(store, sections)
    assert counts["messages_added"] == 1
    print("✓ 备份导入接受设备名会话 ID")

def test_import_knowledge_error_redacts_paths(monkeypatch):
    """知识片段恢复失败回流给用户的文本遮蔽本机路径 / 上游 URL。"""
    from utils import knowledge_admin as knowledge_module

    def boom(documents, dry_run=False, *, return_pieces=False):
        raise RuntimeError(
            "加载失败: C:\\Users\\alice\\.cache\\model 与 "
            "https://huggingface.co/BAAI/bge (Connection refused)"
        )

    monkeypatch.setattr(knowledge_module, "import_documents", boom)
    sections = {
        "preferences": {},
        "trips": [],
        "messages": [],
        "knowledge": [{"content": "片段", "metadata": {}}],
    }
    store = _real_store("u-kb-redact")
    counts = import_user_data(store, sections)
    error = counts["knowledge_error"]
    assert "alice" not in error, error
    assert "huggingface.co" not in error, error
    assert "<路径>" in error and "http(s)://***" in error, error
    assert "Connection refused" in error, "瞬时错误关键词要保留"
    print("✓ 知识片段恢复错误脱敏（路径 / URL）")

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
    import shutil
    import tempfile

    ran = 0
    skipped = 0
    skipped_names: list[str] = []
    for name, func in sorted(globals().items()):
        if not name.startswith("test_") or not callable(func):
            continue
        params = inspect.signature(func).parameters
        fixture = None
        caplog_shim = None
        kwargs: dict = {}
        tmp_dir_paths: list[pathlib.Path] = []
        unsupported = False
        for param, spec in params.items():
            if param == "monkeypatch":
                fixture = pytest.MonkeyPatch()
                kwargs[param] = fixture
            elif param == "tmp_path":
                _tmp_dir = pathlib.Path(tempfile.mkdtemp())
                tmp_dir_paths.append(_tmp_dir)
                kwargs[param] = _tmp_dir
            elif param == "caplog":
                # pytest 夹具只在 pytest 内注入：直跑用最小替身真正执行
                # 日志断言，不能静默跳过（否则脱敏回归在直跑清单上假绿）
                import _caplog_shim

                caplog_shim = _caplog_shim.CaplogShim()
                kwargs[param] = caplog_shim
            else:
                unsupported = True
                break
        if unsupported:
            if caplog_shim is not None:
                caplog_shim.close()
            if fixture is not None:
                fixture.undo()
            for _tmp_dir in tmp_dir_paths:
                shutil.rmtree(_tmp_dir, ignore_errors=True)
            skipped += 1
            skipped_names.append(f"{name}（需 pytest 专用夹具）")
            continue
        kb_fixture: pytest.MonkeyPatch | None = None
        try:
            from utils import user_import as _ui_for_kb

            kb_fixture = pytest.MonkeyPatch()
            kb_fixture.setattr(
                _ui_for_kb, "_existing_knowledge_rows", lambda: []
            )
        except Exception:  # noqa: BLE001 - 模块不可用时不补丁
            if kb_fixture is not None:
                kb_fixture.undo()
            kb_fixture = None
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
        except pytest.skip.Exception as exc:
            skipped += 1
            skipped_names.append(f"{name}（条件跳过：{exc}）")
        finally:
            if caplog_shim is not None:
                caplog_shim.close()
            if storage_snapshot is not None:
                from typing import cast

                from config import STORAGE_CONFIG

                _cfg = cast(dict, STORAGE_CONFIG)
                _cfg.clear()
                _cfg.update(storage_snapshot)
            if fixture is not None:
                fixture.undo()
            if kb_fixture is not None:
                kb_fixture.undo()
            # 直跑模式自建的临时目录要自己清：否则每跑一次清单就在系统
            # 临时目录里留下一批带记忆文件的目录
            for _tmp_dir in tmp_dir_paths:
                shutil.rmtree(_tmp_dir, ignore_errors=True)
    total = sum(
        1
        for name, func in globals().items()
        if name.startswith("test_") and callable(func)
    )
    if skipped_names:
        print("直跑跳过：" + "、".join(skipped_names))
    print(
        f"\n全部通过（{ran}/{total} 个用例；"
        f"{skipped} 个需 pytest 专用夹具或按条件跳过）"
    )
