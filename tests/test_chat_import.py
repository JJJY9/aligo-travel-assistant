"""备份恢复的批量消息去重与时间排序（utils.chat_import 及存储层接入）。

覆盖：提问已在目标库时，备份里该轮次的回答（重复或其它版本）一律跳过，
不产生没有对应提问的尾部回答；无会话遗留消息不做轮次配对；缺失时间戳的
消息按相邻消息补齐、两后端顺序一致；恢复的旧历史按时间戳并入全局历史
位置、不挤占「最近 N 条」窗口；幂等恢复也会把旧版本的乱序历史归位。
"""
import json
import os
import sys
import tempfile
from collections import Counter

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from context.long_term_memory import LongTermMemory
from utils.chat_import import (
    filter_import_messages,
    prepare_import_messages,
)
from utils.timestamps import chronological_key
from utils.user_import import (
    import_user_data,
    parse_user_export,
    preview_user_import,
)


def _sections(rows):
    sections, errors = parse_user_export(
        json.dumps({"chat_history": rows}, ensure_ascii=False)
    )
    assert errors == []
    return sections


def test_filter_skips_old_answer_of_deduped_question():
    """提问已存在：备份里同轮次的回答（内容不同也算备选版本）一并跳过。"""
    existing = Counter({("s1", "user", "Q1"): 1})
    kept = filter_import_messages(
        [
            {"role": "user", "content": "Q1", "session_id": "s1"},
            {
                "role": "assistant",
                "content": "A-old",
                "session_id": "s1",
                "timestamp": "2026-01-01T09:00:05",
            },
        ],
        existing,
    )
    assert kept == []
    print("✓ 提问命中去重后该轮回答被跳过")


def test_filter_skips_alternative_answer_from_other_machine():
    """另一台机器上重新生成的回答同样跳过：本地优先，避免尾部孤儿回答。"""
    existing = Counter({("s1", "user", "Q1"): 1, ("s1", "assistant", "A-old"): 1})
    kept = filter_import_messages(
        [
            {
                "role": "user",
                "content": "Q1",
                "session_id": "s1",
                "timestamp": "2026-11-02T09:00:00",
            },
            {
                "role": "assistant",
                "content": "A-new",
                "session_id": "s1",
                "timestamp": "2026-11-02T09:00:05",
            },
        ],
        existing,
    )
    assert kept == []
    print("✓ 备选版本的返回回答被跳过")


def test_filter_skips_answer_when_only_question_exists():
    """库里只有提问时，备份里的回答也不追加（避免回答配到别的提问下）。"""
    existing = Counter({("s1", "user", "Q1"): 1})
    kept = filter_import_messages(
        [
            {"role": "user", "content": "Q1", "session_id": "s1"},
            {
                "role": "assistant",
                "content": "A1",
                "session_id": "s1",
                "timestamp": "2026-01-01T09:00:05",
            },
        ],
        existing,
    )
    assert kept == []
    print("✓ 只有提问时回答不追加")


def test_filter_keeps_following_new_turn():
    """跳过旧回答后，同一备份里的下一轮新提问 / 回答照常保留。"""
    existing = Counter({("s1", "user", "Q1"): 1})
    kept = filter_import_messages(
        [
            {"role": "user", "content": "Q1", "session_id": "s1"},
            {
                "role": "assistant",
                "content": "A-old",
                "session_id": "s1",
                "timestamp": "2026-01-01T09:00:05",
            },
            {"role": "user", "content": "Q2", "session_id": "s1"},
            {"role": "assistant", "content": "A2", "session_id": "s1"},
        ],
        existing,
    )
    assert [message["content"] for message in kept] == ["Q2", "A2"]
    print("✓ 旧回答被跳过后新轮次照常恢复")


def test_filter_keeps_backup_internal_duplicates():
    """多重集语义不变：备份自身内部的重复消息仍全部保留。"""
    kept = filter_import_messages(
        [
            {"role": "user", "content": "Q", "session_id": "s1"},
            {"role": "user", "content": "Q", "session_id": "s1"},
        ],
        Counter(),
    )
    assert len(kept) == 2
    print("✓ 备份内部重复消息保留")


def test_filter_ignores_turn_pairing_for_sessionless_messages():
    """无 session_id 的遗留消息共用同一个键：不做轮次配对，只按单条去重。"""
    existing = Counter({(None, "user", "Q-A"): 1})
    kept = filter_import_messages(
        [
            {"role": "user", "content": "Q-B", "session_id": None},
            {"role": "user", "content": "Q-A", "session_id": None},
            {"role": "assistant", "content": "A-B", "session_id": None},
            {"role": "assistant", "content": "A-A", "session_id": None},
        ],
        existing,
    )
    assert [message["content"] for message in kept] == ["Q-B", "A-B", "A-A"]
    print("✓ 无会话消息不做轮次配对")


def test_prepare_import_messages_inherits_neighbor_timestamps():
    prepared = prepare_import_messages(
        [
            {"role": "user", "content": "无时间戳提问", "session_id": "s1"},
            {
                "role": "assistant",
                "content": "有时间戳回答",
                "session_id": "s1",
                "timestamp": "2020-06-01T10:00:00",
            },
            {"role": "user", "content": "无锚点", "session_id": "s2"},
        ]
    )
    assert chronological_key(prepared[0]["timestamp"]) == chronological_key(
        "2020-06-01T10:00:00"
    ), prepared[0]
    # 没有同会话相邻消息时按恢复时间
    assert chronological_key(prepared[2]["timestamp"]) >= chronological_key(
        "2026-01-01T00:00:00"
    )
    print("✓ 缺失时间戳按相邻消息补齐")


def test_prepare_import_messages_question_follows_its_answer():
    """乱序备份里缺失时间戳的提问贴合自己的回答（取后一条）。"""
    prepared = prepare_import_messages(
        [
            {
                "role": "user",
                "content": "Q-Nov",
                "session_id": "s1",
                "timestamp": "2026-11-01T09:00:00",
            },
            {
                "role": "assistant",
                "content": "A-Nov",
                "session_id": "s1",
                "timestamp": "2026-11-01T09:00:05",
            },
            {"role": "user", "content": "Q-Jan", "session_id": "s1"},
            {
                "role": "assistant",
                "content": "A-Jan",
                "session_id": "s1",
                "timestamp": "2026-01-01T09:00:05",
            },
        ]
    )
    assert chronological_key(prepared[2]["timestamp"]) == chronological_key(
        "2026-01-01T09:00:05"
    ), prepared[2]
    print("✓ 缺失时间戳的提问贴合自己的回答")


def test_prepare_import_messages_answer_follows_its_question():
    """乱序备份里缺失时间戳的回答贴合自己的提问（取前一条），不排到提问前。"""
    prepared = prepare_import_messages(
        [
            {
                "role": "user",
                "content": "Q-Nov",
                "session_id": "s1",
                "timestamp": "2026-11-01T09:00:00",
            },
            {"role": "assistant", "content": "A-Nov", "session_id": "s1"},
            {
                "role": "user",
                "content": "Q-Jan",
                "session_id": "s1",
                "timestamp": "2026-01-01T09:00:00",
            },
            {
                "role": "assistant",
                "content": "A-Jan",
                "session_id": "s1",
                "timestamp": "2026-01-01T09:00:05",
            },
        ]
    )
    assert chronological_key(prepared[1]["timestamp"]) == chronological_key(
        "2026-11-01T09:00:00"
    ), prepared[1]
    print("✓ 缺失时间戳的回答贴合自己的提问")


def _seed_edited_session(store):
    """现有会话：用户导出备份后在 2026-11 编辑重发过同一问题。"""
    store.add_chat_messages(
        [
            {
                "role": "user",
                "content": "帮我规划行程",
                "session_id": "s1",
                "timestamp": "2026-11-10T09:00:00+08:00",
            },
            {
                "role": "assistant",
                "content": "新回答",
                "session_id": "s1",
                "timestamp": "2026-11-10T09:00:05+08:00",
            },
        ],
        dedupe=True,
    )


def _earlier_backup_sections():
    """更早导出的备份：同一提问的旧回答 + 一轮更早的历史。"""
    return _sections(
        [
            {
                "role": "user",
                "content": "帮我规划行程",
                "session_id": "s1",
                "timestamp": "2026-01-01T09:00:00",
            },
            {
                "role": "assistant",
                "content": "旧回答",
                "session_id": "s1",
                "timestamp": "2026-01-01T09:00:05",
            },
            {
                "role": "user",
                "content": "更早的提问",
                "session_id": "s1",
                "timestamp": "2025-12-01T09:00:00",
            },
            {
                "role": "assistant",
                "content": "更早的回答",
                "session_id": "s1",
                "timestamp": "2025-12-01T09:00:05",
            },
        ]
    )


def test_import_skips_orphan_answer_and_merges_chronologically():
    store = LongTermMemory(
        "u-merge", tempfile.mkdtemp(prefix="chailv_import_order_")
    )
    try:
        _seed_edited_session(store)
        counts = import_user_data(store, _earlier_backup_sections())
        assert counts["messages_added"] == 2, counts
        assert counts["messages_skipped"] == 2, counts
        history = store.get_chat_history(limit=None, session_id="s1")
        assert [message["content"] for message in history] == [
            "更早的提问",
            "更早的回答",
            "帮我规划行程",
            "新回答",
        ], history
        print("✓ 旧回答被跳过，旧历史按时间归位")
    finally:
        store._close() if hasattr(store, "_close") else None


def test_import_skips_alternative_answer_local_wins():
    """同一提问的备选回答不追加（本地优先）：不会出现尾部孤儿回答。"""
    store = LongTermMemory(
        "u-newer", tempfile.mkdtemp(prefix="chailv_import_newer_")
    )
    try:
        store.add_chat_messages(
            [
                {
                    "role": "user",
                    "content": "同一个问题",
                    "session_id": "s1",
                    "timestamp": "2026-01-01T09:00:00+08:00",
                },
                {
                    "role": "assistant",
                    "content": "旧回答",
                    "session_id": "s1",
                    "timestamp": "2026-01-01T09:00:05+08:00",
                },
            ],
            dedupe=True,
        )
        sections = _sections(
            [
                {
                    "role": "user",
                    "content": "同一个问题",
                    "session_id": "s1",
                    "timestamp": "2026-11-02T09:00:00",
                },
                {
                    "role": "assistant",
                    "content": "新回答",
                    "session_id": "s1",
                    "timestamp": "2026-11-02T09:00:05",
                },
            ]
        )
        counts = import_user_data(store, sections)
        assert counts["messages_added"] == 0, counts
        assert counts["messages_skipped"] == 2, counts
        history = store.get_chat_history(limit=None, session_id="s1")
        assert [message["content"] for message in history] == [
            "同一个问题",
            "旧回答",
        ], history
        print("✓ 同一提问的备选回答按本地优先跳过")
    finally:
        store._close() if hasattr(store, "_close") else None


def test_preview_matches_import_counts():
    store = LongTermMemory(
        "u-preview", tempfile.mkdtemp(prefix="chailv_import_preview_")
    )
    try:
        _seed_edited_session(store)
        sections = _earlier_backup_sections()
        preview = preview_user_import(store, sections)
        counts = import_user_data(store, sections)
        for key in ("messages_added", "messages_skipped"):
            assert preview[key] == counts[key], (preview, counts)
        assert counts["messages_added"] == 2 and counts["messages_skipped"] == 2
        print("✓ 预览与真实恢复的消息计数一致")
    finally:
        store._close() if hasattr(store, "_close") else None


def test_empty_history_import_sorts_messages_chronologically():
    """空库恢复：全局按时间排序（备份自身可能不是时间升序）。"""
    store = LongTermMemory(
        "u-empty", tempfile.mkdtemp(prefix="chailv_import_empty_")
    )
    try:
        counts = import_user_data(store, _earlier_backup_sections())
        assert counts["messages_added"] == 4, counts
        history = store.get_chat_history(limit=None, session_id="s1")
        assert [message["content"] for message in history] == [
            "更早的提问",
            "更早的回答",
            "帮我规划行程",
            "旧回答",
        ], history
        print("✓ 空库恢复按时间排序")
    finally:
        store._close() if hasattr(store, "_close") else None


def test_queue_head_out_of_order_backup_still_merges_by_time():
    """备份内同会话顺序非时间升序时，旧消息仍插到正确的历史位置。"""
    store = LongTermMemory(
        "u-disorder", tempfile.mkdtemp(prefix="chailv_import_disorder_")
    )
    try:
        store.add_chat_messages(
            [
                {
                    "role": "user",
                    "content": "Q-Jun",
                    "session_id": "s1",
                    "timestamp": "2026-06-01T09:00:00+08:00",
                },
                {
                    "role": "assistant",
                    "content": "A-Jun",
                    "session_id": "s1",
                    "timestamp": "2026-06-01T09:00:05+08:00",
                },
            ],
            dedupe=True,
        )
        sections = _sections(
            [
                {
                    "role": "user",
                    "content": "Q-Nov",
                    "session_id": "s1",
                    "timestamp": "2026-11-01T09:00:00",
                },
                {
                    "role": "assistant",
                    "content": "A-Nov",
                    "session_id": "s1",
                    "timestamp": "2026-11-01T09:00:05",
                },
                {
                    "role": "user",
                    "content": "Q-Jan",
                    "session_id": "s1",
                    "timestamp": "2026-01-01T09:00:00",
                },
                {
                    "role": "assistant",
                    "content": "A-Jan",
                    "session_id": "s1",
                    "timestamp": "2026-01-01T09:00:05",
                },
            ]
        )
        import_user_data(store, sections)
        history = store.get_chat_history(limit=None, session_id="s1")
        assert [message["content"] for message in history] == [
            "Q-Jan",
            "A-Jan",
            "Q-Jun",
            "A-Jun",
            "Q-Nov",
            "A-Nov",
        ], history
        print("✓ 备份内乱序仍按时间归位")
    finally:
        store._close() if hasattr(store, "_close") else None


def test_new_session_messages_do_not_fill_recent_window():
    """恢复更早的新会话不能挤占跨会话「最近 N 条」窗口（与 PG 一致）。"""
    store = LongTermMemory(
        "u-window", tempfile.mkdtemp(prefix="chailv_import_window_")
    )
    try:
        store.add_chat_messages(
            [
                {
                    "role": "user",
                    "content": f"s2-{index}",
                    "session_id": "s2",
                    "timestamp": f"2026-10-01T09:{index:02d}:00+08:00",
                }
                for index in range(10)
            ],
            dedupe=True,
        )
        sections = _sections(
            [
                {
                    "role": "user",
                    "content": f"s1-{index}",
                    "session_id": "s1",
                    "timestamp": f"2025-01-01T09:{index:02d}:00",
                }
                for index in range(10)
            ]
        )
        import_user_data(store, sections)
        window = store.get_chat_history(limit=5, exclude_session_id="s9")
        assert [message["content"] for message in window] == [
            f"s2-{index}" for index in range(5, 10)
        ], window
        print("✓ 旧会话不挤占跨会话最近窗口")
    finally:
        store._close() if hasattr(store, "_close") else None


def test_import_heals_polluted_history_order():
    """旧版本追加在末尾的乱序历史在下次恢复时整体归位。"""
    store = LongTermMemory(
        "u-heal", tempfile.mkdtemp(prefix="chailv_import_heal_")
    )
    try:
        # 模拟旧版本导入的污染：更早的回答被追加在末尾
        store.add_chat_messages(
            [
                {
                    "role": "user",
                    "content": "Q-Nov",
                    "session_id": "s1",
                    "timestamp": "2026-11-01T09:00:00+08:00",
                },
                {
                    "role": "assistant",
                    "content": "A-new",
                    "session_id": "s1",
                    "timestamp": "2026-11-01T09:00:05+08:00",
                },
                {
                    "role": "assistant",
                    "content": "A-old",
                    "session_id": "s1",
                    "timestamp": "2026-01-01T09:00:05+08:00",
                },
            ],
            dedupe=False,
        )
        sections = _sections(
            [
                {
                    "role": "user",
                    "content": "2025 的消息",
                    "session_id": "s1",
                    "timestamp": "2025-06-01T09:00:00",
                }
            ]
        )
        import_user_data(store, sections)
        history = store.get_chat_history(limit=None, session_id="s1")
        assert [message["content"] for message in history] == [
            "2025 的消息",
            "A-old",
            "Q-Nov",
            "A-new",
        ], history
        print("✓ 存量乱序历史在恢复时归位")
    finally:
        store._close() if hasattr(store, "_close") else None


def test_idempotent_import_heals_polluted_history():
    """重复导入全部命中（无新增）也会把存量乱序历史归位并落盘。"""
    root = tempfile.mkdtemp(prefix="chailv_import_heal_idem_")
    store = LongTermMemory("u-heal-idem", root)
    try:
        store.add_chat_messages(
            [
                {
                    "role": "user",
                    "content": "Q-Nov",
                    "session_id": "s1",
                    "timestamp": "2026-11-01T09:00:00+08:00",
                },
                {
                    "role": "assistant",
                    "content": "A-new",
                    "session_id": "s1",
                    "timestamp": "2026-11-01T09:00:05+08:00",
                },
                {
                    "role": "assistant",
                    "content": "A-old",
                    "session_id": "s1",
                    "timestamp": "2026-01-01T09:00:05+08:00",
                },
            ],
            dedupe=False,
        )
        # 备份里的两条都已在库中：幂等恢复，messages_added=0
        sections = _sections(
            [
                {
                    "role": "user",
                    "content": "Q-Nov",
                    "session_id": "s1",
                    "timestamp": "2026-11-01T09:00:00",
                },
                {
                    "role": "assistant",
                    "content": "A-new",
                    "session_id": "s1",
                    "timestamp": "2026-11-01T09:00:05",
                },
            ]
        )
        counts = import_user_data(store, sections)
        assert counts["messages_added"] == 0, counts
    finally:
        store._close() if hasattr(store, "_close") else None
    # 重新从磁盘加载：归位结果必须已落盘
    reloaded = LongTermMemory("u-heal-idem", root)
    try:
        history = reloaded.get_chat_history(limit=None, session_id="s1")
        assert [message["content"] for message in history] == [
            "A-old",
            "Q-Nov",
            "A-new",
        ], history
        print("✓ 幂等恢复也会归位并落盘")
    finally:
        reloaded._close() if hasattr(reloaded, "_close") else None


def test_idempotent_import_without_reorder_does_not_rewrite(monkeypatch):
    """顺序未变化的幂等恢复不重写整份记忆文件。"""
    store = LongTermMemory(
        "u-no-rewrite", tempfile.mkdtemp(prefix="chailv_import_no_rewrite_")
    )
    try:
        store.add_chat_messages(
            [
                {
                    "role": "user",
                    "content": "Q1",
                    "session_id": "s1",
                    "timestamp": "2026-01-01T09:00:00+08:00",
                },
                {
                    "role": "assistant",
                    "content": "A1",
                    "session_id": "s1",
                    "timestamp": "2026-01-01T09:00:05+08:00",
                },
            ],
            dedupe=True,
        )
        calls: list[int] = []
        monkeypatch.setattr(store, "_persist", lambda: calls.append(1))
        sections = _sections(
            [
                {
                    "role": "user",
                    "content": "Q1",
                    "session_id": "s1",
                    "timestamp": "2026-01-01T09:00:00",
                },
                {
                    "role": "assistant",
                    "content": "A1",
                    "session_id": "s1",
                    "timestamp": "2026-01-01T09:00:05",
                },
            ]
        )
        counts = import_user_data(store, sections)
        assert counts["messages_added"] == 0, counts
        assert calls == [], "顺序未变化时不应重写文件"
        print("✓ 无变化的幂等恢复不重写文件")
    finally:
        store._close() if hasattr(store, "_close") else None
