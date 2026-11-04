"""
存储工厂与回退测试（无需外部服务）

- 未配置 REDIS_URL / DATABASE_URL 时使用进程内 / JSON 实现；
- 配置了但连接失败时自动回退，并进入失败冷却（不阻塞后续会话）。

真实 Redis / PostgreSQL 的集成测试见 tests/test_storage_integration.py。

运行：python tests/test_storage_factory.py
"""
import os
import sys
import tempfile

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from context import storage
from context.long_term_memory import LongTermMemory
from context.short_term_memory import ShortTermMemory


def _set(url_key: str, value: str) -> None:
    if url_key == "redis_url":
        storage.STORAGE_CONFIG["redis_url"] = value
    elif url_key == "postgres_dsn":
        storage.STORAGE_CONFIG["postgres_dsn"] = value
    else:
        raise ValueError(f"unknown storage key: {url_key}")
    storage.reset_failure_cache()


def test_defaults_use_local_backends():
    _set("redis_url", "")
    _set("postgres_dsn", "")
    assert isinstance(storage.create_short_term_memory("s-default"), ShortTermMemory)
    memory = storage.create_long_term_memory("u-default", tempfile.mkdtemp())
    assert isinstance(memory, LongTermMemory)
    print("✓ 未配置时使用本地实现")


def test_bad_redis_falls_back():
    _set("redis_url", "redis://127.0.0.1:1/0")
    assert isinstance(storage.create_short_term_memory("s-bad"), ShortTermMemory)
    # 失败冷却期内直接回退（不再尝试连接）
    assert storage._failed_recently("redis:redis://127.0.0.1:1/0")
    assert isinstance(storage.create_short_term_memory("s-bad2"), ShortTermMemory)
    print("✓ Redis 不可用时回退并进入冷却")


def test_bad_postgres_falls_back():
    _set("redis_url", "")
    _set("postgres_dsn", "postgresql://user:pass@127.0.0.1:1/db")
    memory = storage.create_long_term_memory("u-bad", tempfile.mkdtemp())
    assert isinstance(memory, LongTermMemory)
    assert storage._failed_recently("postgres:postgresql://user:pass@127.0.0.1:1/db")
    print("✓ PostgreSQL 不可用时回退 JSON 并进入冷却")


def test_memory_manager_uses_factory():
    _set("redis_url", "")
    _set("postgres_dsn", "")
    from context.memory_manager import MemoryManager

    manager = MemoryManager("u-mm", "s-mm", storage_path=tempfile.mkdtemp())
    assert isinstance(manager.short_term, ShortTermMemory)
    assert isinstance(manager.long_term, LongTermMemory)
    manager.add_message("user", "你好")
    assert manager.short_term.get_statistics()["total_messages"] == 1
    assert len(manager.long_term.get_chat_history()) == 1
    print("✓ MemoryManager 走存储工厂")


def test_long_term_delete_session():
    from context.long_term_memory import LongTermMemory

    memory = LongTermMemory("u-del", tempfile.mkdtemp())
    memory.add_chat_message("user", "你好", "s1")
    memory.add_chat_message("assistant", "您好", "s1")
    memory.add_chat_message("user", "其他", "s2")
    assert memory.delete_chat_session("s1") == 2
    assert [item["content"] for item in memory.get_chat_history()] == ["其他"]
    assert memory.get_statistics()["total_messages"] == 1
    assert memory.delete_chat_session("s1") == 0
    print("✓ 长期记忆按会话删除")


def test_long_term_delete_preference():
    from context.long_term_memory import LongTermMemory

    memory = LongTermMemory("u-pref", tempfile.mkdtemp())
    memory.save_preference("home_location", "北京")
    memory.save_preference("hotel_brands", ["汉庭"])
    assert memory.delete_preference("home_location") is True
    assert memory.get_preference() == {"hotel_brands": ["汉庭"]}
    assert memory.delete_preference("home_location") is False
    print("✓ 长期记忆删除偏好")


def test_trip_history_delete_and_ids():
    from context.long_term_memory import LongTermMemory

    memory = LongTermMemory("u-trip", tempfile.mkdtemp())
    for day, destination in ((9, "上海"), (10, "上海"), (11, "杭州")):
        memory.save_trip_history(
            {
                "origin": "北京",
                "destination": destination,
                "start_date": f"2026-11-{day:02d}",
                "end_date": f"2026-11-{day:02d}",
                "purpose": "出差",
            }
        )
    trips = memory.get_trip_history(limit=None)
    assert [trip["trip_id"] for trip in trips] == ["trip_1", "trip_2", "trip_3"]

    assert memory.delete_trip("trip_2") is True
    assert memory.delete_trip("trip_2") is False
    stats = memory.get_statistics()
    assert stats["total_trips"] == 2
    assert stats["frequent_destinations"] == {"上海": 1, "杭州": 1}

    # 删除中间片段后新增：id 必须大于现有最大 id（trip_3），不能与已有记录冲突
    memory.save_trip_history(
        {
            "origin": "北京",
            "destination": "广州",
            "start_date": "2026-12-01",
            "end_date": "2026-12-02",
            "purpose": "出差",
        }
    )
    ids = [trip["trip_id"] for trip in memory.get_trip_history(limit=None)]
    assert ids == ["trip_1", "trip_3", "trip_4"], "删除后新增按现有最大 id 递增"

    assert memory.delete_trip("trip_3") is True
    assert memory.get_statistics()["frequent_destinations"] == {"上海": 1, "广州": 1}
    print("✓ 行程删除同步统计，新增 id 不与已有记录冲突")


def test_session_titles():
    from context.long_term_memory import LongTermMemory

    memory = LongTermMemory("u-title", tempfile.mkdtemp())
    memory.add_chat_message("user", "hi", "s1")
    memory.set_session_title("s1", " 杭州出差 ")
    assert memory.get_session_titles() == {"s1": "杭州出差"}

    memory.set_session_title("s1", "")
    assert memory.get_session_titles() == {}

    memory.set_session_title("s1", "标题")
    memory.delete_chat_session("s1")
    assert memory.get_session_titles() == {}
    print("✓ 会话标题设置 / 清除 / 随会话删除")


def test_session_pinned():
    from context.long_term_memory import LongTermMemory

    memory = LongTermMemory("u-pin", tempfile.mkdtemp())
    memory.add_chat_message("user", "hi", "s1")
    memory.set_session_pinned("s1", True)
    memory.set_session_pinned("s2", True)
    assert memory.get_pinned_sessions() == ["s1", "s2"]

    memory.set_session_pinned("s1", False)
    assert memory.get_pinned_sessions() == ["s2"]

    memory.set_session_pinned("s2", True)  # 幂等
    assert memory.get_pinned_sessions() == ["s2"]

    memory.delete_chat_session("s2")
    assert memory.get_pinned_sessions() == []
    print("✓ 会话置顶 / 取消 / 随会话删除")


def test_add_message_preserves_metadata():
    from context.long_term_memory import LongTermMemory
    from context.memory_manager import MemoryManager

    storage_path = tempfile.mkdtemp()
    manager = MemoryManager("u-race", "s1", storage_path=storage_path)
    manager.long_term.add_chat_message("user", "hi", "s1")

    # 模拟对话进行期间，记忆面板另开存储写入标题 / 偏好
    other = LongTermMemory("u-race", storage_path)
    other.set_session_title("s1", "新标题")
    other.save_preference("home_location", "北京")

    manager.add_message("assistant", "yo")

    again = LongTermMemory("u-race", storage_path)
    assert again.get_session_titles()["s1"] == "新标题"
    assert again.get_preference("home_location") == "北京"
    assert [item["content"] for item in again.get_chat_history()] == ["hi", "yo"]
    print("✓ 对话写入前重新加载，保留中途元数据")


def test_clear_history_clears_metadata():
    from context.long_term_memory import LongTermMemory

    memory = LongTermMemory("u-clear", tempfile.mkdtemp())
    memory.add_chat_message("user", "hi", "s1")
    memory.set_session_title("s1", "标题")
    memory.set_session_pinned("s1", True)
    memory.set_session_archived("s1", True)

    memory.clear_history()
    assert memory.get_chat_history() == []
    assert memory.get_session_titles() == {}
    assert memory.get_pinned_sessions() == []
    assert memory.get_archived_sessions() == []
    print("✓ clear_history 同时清理会话元数据（标题 / 置顶 / 归档）")


def test_corrupt_memory_file_is_backed_up():
    from pathlib import Path

    storage_dir = tempfile.mkdtemp()
    path = Path(storage_dir) / "u-corrupt.json"
    path.write_text("{bad json", encoding="utf-8")
    memory = LongTermMemory("u-corrupt", storage_dir)
    assert memory.get_chat_history() == []
    backups = list(Path(storage_dir).glob("u-corrupt.json.corrupt-*"))
    assert backups, "损坏文件应保留备份"
    assert backups[0].read_text(encoding="utf-8") == "{bad json"
    print("✓ 损坏的记忆文件备份后重建")


def test_session_metadata_save_failure_raises():
    """JSON 写入失败时会话元数据操作必须抛错，不能静默返回成功。"""
    memory = LongTermMemory("u-save-fail", tempfile.mkdtemp())
    memory.add_chat_message("user", "hi", "s1")
    memory._save = lambda: False
    for setter in (
        lambda: memory.set_session_archived("s1", True),
        lambda: memory.set_session_pinned("s1", True),
        lambda: memory.set_session_title("s1", "标题"),
    ):
        with pytest.raises(OSError):
            setter()
    print("✓ 会话元数据写入失败时抛错（不再静默成功）")


def test_describe_backends_configured_flags(monkeypatch):
    from config import STORAGE_CONFIG

    monkeypatch.setitem(STORAGE_CONFIG, "redis_url", "")
    monkeypatch.setitem(STORAGE_CONFIG, "postgres_dsn", "")
    info = storage.describe_backends()
    assert info["short_term"] == "memory" and info["long_term"] == "json"
    assert info["short_term_configured"] is False
    assert info["long_term_configured"] is False

    monkeypatch.setitem(STORAGE_CONFIG, "redis_url", "redis://127.0.0.1:1/0")
    storage.reset_failure_cache()
    try:
        info = storage.describe_backends()
    finally:
        storage.reset_failure_cache()
    assert info["short_term_configured"] is True
    assert info["short_term"] == "memory", "连接失败时回退进程内"
    print("✓ describe_backends 报告配置 / 回退状态")


def test_export_data_includes_session_metadata():
    from context.memory_manager import MemoryManager

    manager = MemoryManager("u-export", "s1", storage_path=tempfile.mkdtemp())
    manager.long_term.set_session_title("s1", "T")
    manager.long_term.set_session_pinned("s1", True)
    manager.long_term.set_session_archived("s1", True)

    data = manager.export_data()
    assert data["session_titles"]["s1"] == "T"
    assert "s1" in data["pinned_sessions"]
    assert "s1" in data["archived_sessions"]
    print("✓ 导出数据包含标题 / 置顶 / 归档")


class _CountingModel:
    """记录调用次数的假模型（用于验证总结缓存）。"""

    def __init__(self):
        self.calls = 0

    async def __call__(self, messages, **kwargs):
        self.calls += 1
        return {
            "content": [{"type": "text", "text": "用户偏好汉庭，常驻北京"}],
            "usage": {"input_tokens": 8, "output_tokens": 4, "time": 0.05},
        }


def test_summary_cache_avoids_repeated_calls():
    import asyncio

    from context.memory_manager import MemoryManager

    model = _CountingModel()
    manager = MemoryManager(
        "u-cache", "s-cache", storage_path=tempfile.mkdtemp(), llm_model=model
    )
    manager.long_term.add_chat_message("user", "我之前去了上海", "s-other")

    first = asyncio.run(manager.get_long_term_summary_async())
    second = asyncio.run(manager.get_long_term_summary_async())
    assert first and first == second
    assert model.calls == 1, "历史未变化时应复用总结"

    manager.long_term.add_chat_message("user", "我又去了杭州", "s-other")
    asyncio.run(manager.get_long_term_summary_async())
    assert model.calls == 2, "其它会话新增消息后应重新总结"
    print("✓ 长期记忆总结按指纹缓存")


def test_reload_long_term_respects_backend():
    from context.memory_manager import MemoryManager

    manager = MemoryManager("u-reload", "s-reload", storage_path=tempfile.mkdtemp())
    original = manager.long_term
    manager.reload_long_term()
    assert manager.long_term is not original, "JSON 快照后端应重新加载"

    class _LiveStore:
        snapshot_based = False

    live = _LiveStore()
    manager.long_term = live  # type: ignore[assignment]
    manager.reload_long_term()
    assert manager.long_term is live, "无快照后端不应重建（避免每轮新建连接）"
    print("✓ 长期记忆重载按后端类型决定")


def test_stale_snapshot_does_not_resurrect_trip():
    """回合开始时的旧快照写回时，不能把别处刚删除的行程/统计恢复。"""
    from context.long_term_memory import LongTermMemory

    path = tempfile.mkdtemp()
    stale = LongTermMemory("u-stale", path)
    for destination in ("上海", "杭州"):
        stale.save_trip_history(
            {
                "origin": "北京",
                "destination": destination,
                "start_date": "2026-11-09",
                "end_date": "2026-11-11",
                "purpose": "出差",
            }
        )
    other = LongTermMemory("u-stale", path)
    assert other.delete_trip("trip_1") is True

    # stale 仍持有删除前的快照；模拟对话回合结束时保存新行程
    stale.save_trip_history(
        {
            "origin": "北京",
            "destination": "广州",
            "start_date": "2026-12-01",
            "end_date": "2026-12-02",
            "purpose": "出差",
        }
    )

    fresh = LongTermMemory("u-stale", path)
    trips = fresh.get_trip_history(limit=None)
    assert [trip["trip_id"] for trip in trips] == ["trip_2", "trip_3"]
    assert all(trip["destination"] != "上海" for trip in trips), "已删除的行程不应复活"
    assert fresh.get_statistics()["total_trips"] == 2
    print("✓ 旧快照写回不会复活已删除的行程")


def test_trip_save_deduplicates_same_trip():
    """同路线 + 同起止日期的行程重复保存时更新既有记录，不重复计数。"""
    from context.long_term_memory import LongTermMemory

    memory = LongTermMemory("u-dup", tempfile.mkdtemp())
    base = {
        "origin": "北京",
        "destination": "杭州",
        "start_date": "2026-11-09",
        "end_date": "2026-11-11",
        "purpose": "出差",
    }
    memory.save_trip_history(base)
    memory.save_trip_history({**base, "session_id": "sess-dedup"})

    trips = memory.get_trip_history(limit=None)
    assert len(trips) == 1, "同一行程不应重复保存"
    assert trips[0]["session_id"] == "sess-dedup", "重复保存应更新会话关联"
    stats = memory.get_statistics()
    assert stats["total_trips"] == 1
    assert stats["frequent_destinations"] == {"杭州": 1}

    # 日期不同视为新行程
    memory.save_trip_history(
        {**base, "start_date": "2026-12-01", "end_date": "2026-12-02"}
    )
    assert len(memory.get_trip_history(limit=None)) == 2

    # 缺少出发日期时不参与去重
    undated = {"origin": "北京", "destination": "杭州", "purpose": "出差"}
    memory.save_trip_history(undated)
    memory.save_trip_history(undated)
    assert len(memory.get_trip_history(limit=None)) == 4
    print("✓ 行程重复保存去重（同日期更新、不同日期新增）")


def test_trip_update_and_conflict():
    from context.long_term_memory import LongTermMemory

    memory = LongTermMemory("u-update", tempfile.mkdtemp())
    memory.save_trip_history(
        {
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-11-09",
            "end_date": "2026-11-11",
            "purpose": "出差",
        }
    )
    memory.save_trip_history(
        {
            "origin": "北京",
            "destination": "杭州",
            "start_date": "2026-12-01",
            "end_date": "2026-12-03",
            "purpose": "出差",
        }
    )

    assert memory.update_trip("trip_1", {"destination": "南京"}) is True
    trips = memory.get_trip_history(limit=None)
    assert trips[0]["destination"] == "南京"
    assert memory.get_statistics()["frequent_destinations"] == {"南京": 1, "杭州": 1}

    # 清空返程日期
    assert memory.update_trip("trip_1", {"end_date": None}) is True
    assert memory.get_trip_history(limit=None)[0]["end_date"] is None

    # 更新后与另一条行程重复 → 拒绝
    with pytest.raises(ValueError, match="已存在相同行程"):
        memory.update_trip(
            "trip_1",
            {"destination": "杭州", "start_date": "2026-12-01", "end_date": "2026-12-03"},
        )
    assert [trip["destination"] for trip in memory.get_trip_history(limit=None)] == [
        "南京",
        "杭州",
    ], "冲突时不应修改数据"

    assert memory.update_trip("trip_missing", {"purpose": "x"}) is False
    print("✓ 行程更新（字段 / 统计 / 清空日期 / 冲突拒绝）")


if __name__ == "__main__":
    test_defaults_use_local_backends()
    test_bad_redis_falls_back()
    test_bad_postgres_falls_back()
    test_memory_manager_uses_factory()
    test_long_term_delete_session()
    test_long_term_delete_preference()
    test_trip_history_delete_and_ids()
    test_stale_snapshot_does_not_resurrect_trip()
    test_trip_save_deduplicates_same_trip()
    test_trip_update_and_conflict()
    test_session_titles()
    test_session_pinned()
    test_add_message_preserves_metadata()
    test_clear_history_clears_metadata()
    test_corrupt_memory_file_is_backed_up()
    test_session_metadata_save_failure_raises()
    _mp = pytest.MonkeyPatch()
    try:
        test_describe_backends_configured_flags(_mp)
    finally:
        _mp.undo()
    test_export_data_includes_session_metadata()
    test_summary_cache_avoids_repeated_calls()
    test_reload_long_term_respects_backend()
    print("\n全部通过")
