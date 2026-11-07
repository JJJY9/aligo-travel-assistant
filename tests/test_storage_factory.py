"""
存储工厂与回退测试（无需外部服务）

- 未配置 REDIS_URL / DATABASE_URL 时使用进程内 / JSON 实现；
- 配置了但连接失败时自动回退，并进入失败冷却（不阻塞后续会话）。

真实 Redis / PostgreSQL 的集成测试见 tests/test_storage_integration.py。

运行：python tests/test_storage_factory.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from context import storage
from context.long_term_memory import LongTermMemory
from utils.errors import MemoryPersistenceError
from context.short_term_memory import RedisShortTermMemory, ShortTermMemory


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
    from context import short_term_memory as short_term_module
    from context.short_term_memory import discard_pending_ops

    _set("redis_url", "redis://127.0.0.1:1/0")
    short_term_module._redis_degraded_until = 0.0
    # 配置了 Redis 但连接失败：仍返回 Redis 实现（调用走影子缓存 + 操作
    # 日志，恢复后由后续实例重放），不能回退纯内存——否则故障期的删除 /
    # 清空会彻底丢失
    memory = storage.create_short_term_memory("s-bad")
    assert isinstance(memory, RedisShortTermMemory)
    assert storage._failed_recently("redis:redis://127.0.0.1:1/0")
    memory.add_message("user", "故障期消息")
    assert [item["content"] for item in memory.get_recent_context()] == ["故障期消息"]
    assert [kind for kind, _ in memory._pending_ops] == ["add"]
    discard_pending_ops(memory.key)
    assert isinstance(storage.create_short_term_memory("s-bad2"), RedisShortTermMemory)
    short_term_module._redis_degraded_until = 0.0
    print("✓ Redis 不可用时仍使用 Redis 实现并走故障路径")


def test_describe_backends_reports_long_term_degraded(monkeypatch):
    """配置了 PostgreSQL 但已回退 JSON 时，后端描述带 long_term_degraded 标记。"""
    _set("redis_url", "")
    _set("postgres_dsn", "postgresql://user:pass@db.example/aligo")
    monkeypatch.setattr(
        storage,
        "_failed_recently",
        lambda key: key.startswith("postgres:"),
    )
    backends = storage.describe_backends()
    assert backends["long_term"] == "json"
    assert backends["long_term_configured"] is True
    assert backends["long_term_degraded"] is True

    monkeypatch.setattr(storage, "_failed_recently", lambda key: False)
    assert storage.describe_backends()["long_term_degraded"] is False
    print("✓ describe_backends 标记长期记忆回退")


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
    memory.add_chat_message("user", "hi", "s2")
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
    assert str(os.getpid()) in backups[0].name, "备份名带进程号，避免并发损坏互相覆盖"
    print("✓ 损坏的记忆文件备份后重建")


def test_corrupt_backup_does_not_move_concurrently_written_file(monkeypatch):
    """解析失败后、备份前若其它线程写入了新文件，不能把有效数据当损坏文件移走。"""
    import json as json_module
    from pathlib import Path

    storage_dir = tempfile.mkdtemp()
    path = Path(storage_dir) / "u-race.json"
    path.write_text("{bad json", encoding="utf-8")
    real_load = json_module.load
    calls = {"count": 0}

    def flaky_load(handle, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            # 模拟另一线程在首次解析失败后完成了新的原子写入（带可区分的偏好）
            path.write_text(
                json_module.dumps(
                    {
                        "chat_history": [],
                        "preferences": [
                            {"type": "home_location", "value": "杭州"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            raise ValueError("Expecting property name enclosed in double quotes")
        return real_load(handle, *args, **kwargs)

    monkeypatch.setattr(json_module, "load", flaky_load)
    memory = LongTermMemory("u-race", storage_dir)
    # 必须真正采用并发写入的内容，而不是丢弃后返回空数据
    assert memory.get_preference("home_location") == "杭州"
    assert path.exists(), "并发写入的有效文件不能被移走"
    assert not list(Path(storage_dir).glob("u-race.json.corrupt-*"))
    print("✓ 损坏备份不会误移并发写入的新文件")


def test_valid_json_non_object_memory_file_is_backed_up():
    """合法 JSON 但不是对象时按损坏处理；对象内字段异常读取时修复。"""
    from pathlib import Path

    for content in ("[]", "null"):
        storage_dir = tempfile.mkdtemp()
        path = Path(storage_dir) / "u-shape.json"
        path.write_text(content, encoding="utf-8")
        memory = LongTermMemory("u-shape", storage_dir)
        assert memory.get_chat_history() == [], content
        backups = list(Path(storage_dir).glob("u-shape.json.corrupt-*"))
        assert backups, f"结构异常的文件应备份：{content}"
        assert backups[0].read_text(encoding="utf-8") == content

    storage_dir = tempfile.mkdtemp()
    path = Path(storage_dir) / "u-shape.json"
    path.write_text('{"statistics": null}', encoding="utf-8")
    memory = LongTermMemory("u-shape", storage_dir)
    assert memory.get_chat_history() == []
    assert not list(Path(storage_dir).glob("u-shape.json.corrupt-*")), (
        "对象内的 null 字段可修复，不应备份成损坏文件"
    )
    print("✓ 非对象记忆文件备份重建；字段异常读取时修复")


def test_memory_persistence_error_is_not_retriable():
    """写盘失败不能被当作可重试的网络错误（否则会重跑整轮对话、误开熔断器）。"""
    from utils.llm_resilience import is_retriable_error

    assert is_retriable_error(MemoryPersistenceError("磁盘已满")) is False
    assert is_retriable_error(OSError("network unreachable")) is True
    print("✓ 写盘失败不触发对话重试")


def test_redact_url_masks_password_containing_at():
    """密码里含未转义的 @ 时不能只隐藏第一个 @ 之前的部分。"""
    redacted = storage._redact_url("redis://default:p@ssw0rd@redis.internal:6379/0")
    assert "p@ssw0rd" not in redacted
    assert redacted == "redis://***@redis.internal:6379/0"
    assert (
        storage._redact_url("redis://redis.internal:6379/0")
        == "redis://redis.internal:6379/0"
    )
    print("✓ Redis URL 日志隐藏完整密码（含 @）")


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
        with pytest.raises(MemoryPersistenceError):
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
    assert info["short_term_degraded"] is True, "配置 Redis 但取不到客户端也要报降级"

    monkeypatch.setattr(storage, "_get_redis_client", lambda url: object())
    monkeypatch.setattr(storage, "redis_short_term_degraded", lambda: True)
    info = storage.describe_backends()
    assert info["short_term"] == "redis"
    assert info["short_term_degraded"] is True, "运行中故障回退要在健康信息里可见"
    print("✓ describe_backends 报告配置 / 回退状态")


def test_json_write_failures_raise():
    """所有 JSON 写操作在 _save 失败时都必须抛错（不能静默成功）。"""
    memory = LongTermMemory("u-persist-fail", tempfile.mkdtemp())
    memory._save = lambda: False
    actions = [
        ("偏好", lambda: memory.save_preference("home_location", "北京")),
        ("消息", lambda: memory.add_chat_message("user", "hi", "s1")),
        (
            "行程",
            lambda: memory.save_trip_history(
                {
                    "origin": "北京",
                    "destination": "上海",
                    "start_date": "2026-12-05",
                }
            ),
        ),
        ("删除偏好", lambda: memory.delete_preference("home_location")),
        ("清空历史", memory.clear_history),
    ]
    for label, action in actions:
        with pytest.raises(MemoryPersistenceError):
            action()
    print("✓ JSON 偏好 / 消息 / 行程 / 清空写盘失败均抛错")


def test_export_data_includes_session_metadata():
    from context.memory_manager import MemoryManager

    manager = MemoryManager("u-export", "s1", storage_path=tempfile.mkdtemp())
    manager.long_term.add_chat_message("user", "hi", "s1")
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
        self.prompts = []

    async def __call__(self, messages, **kwargs):
        self.calls += 1
        self.prompts.append(messages)
        return {
            "content": [{"type": "text", "text": "用户偏好汉庭，常驻北京"}],
            "usage": {"input_tokens": 8, "output_tokens": 4, "time": 0.05},
        }


def test_pg_delete_trip_deletes_single_row():
    """PostgreSQL 删除行程按主键：历史库重复 trip_id 时不能整批删除。"""
    import contextlib

    from context.postgres_long_term_memory import PostgresLongTermMemory

    store = object.__new__(PostgresLongTermMemory)
    store.user_id = "u1"
    # advisory lock 需要真实连接；单元测试只关注按主键删除的 SQL
    store._advisory_lock = lambda name: contextlib.nullcontext()
    calls: dict[str, list[tuple[str, object]]] = {"fetch": [], "exec": []}

    def fake_fetch(sql, params):
        calls["fetch"].append((sql, params))
        return (42,)

    def fake_execute(sql, params):
        calls["exec"].append((sql, params))

    setattr(store, "_fetchone", fake_fetch)
    setattr(store, "_execute", fake_execute)
    assert store.delete_trip("trip_1") is True
    select_sql, _ = calls["fetch"][0]
    assert "ORDER BY id LIMIT 1" in select_sql
    delete_sql, delete_params = calls["exec"][0]
    assert "WHERE user_id = %s AND id = %s" in delete_sql
    assert delete_params == ("u1", 42)

    setattr(store, "_fetchone", lambda sql, params: None)
    assert store.delete_trip("missing") is False
    assert len(calls["exec"]) == 1, "不存在时不应执行删除"
    print("✓ PostgreSQL 删除行程按主键（重复 trip_id 只删一行）")


def test_null_list_fields_are_repaired():
    """preferences / chat_history / trip_history 为 null 时按空集合修复。"""
    import json as json_module
    from pathlib import Path

    for field in ("preferences", "chat_history", "trip_history"):
        storage_dir = tempfile.mkdtemp()
        path = Path(storage_dir) / "u-null.json"
        path.write_text(json_module.dumps({field: None}), encoding="utf-8")
        memory = LongTermMemory("u-null", storage_dir)
        assert memory.get_preference() == {}
        assert memory.get_chat_history() == []
        assert memory.get_trip_history(limit=None) == []
        memory.save_preference("home_location", "北京")
        assert memory.get_preference("home_location") == "北京"
        assert not list(Path(storage_dir).glob("u-null.json.corrupt-*")), field
    print("✓ null 字段读取时修复为空集合")


def test_migrate_fills_statistics_defaults():
    """statistics 缺子字段时补默认值，保存行程不再 KeyError。"""
    storage_dir = tempfile.mkdtemp()
    path = Path(storage_dir) / "u-stats.json"
    path.write_text(json.dumps({"statistics": {}}), encoding="utf-8")
    memory = LongTermMemory("u-stats", storage_dir)
    trip_id = memory.save_trip_history(
        {
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-12-01",
            "purpose": "出差",
        }
    )
    assert trip_id == "trip_1"
    stats = memory.get_statistics()
    assert stats["total_trips"] == 1
    assert stats["frequent_destinations"] == {"上海": 1}
    print("✓ 缺失 statistics 子字段时补默认值")


def test_migrate_repairs_bad_statistics_values():
    """计数器为 null / 字符串时必须修复，否则每次写入都抛 TypeError。"""
    storage_dir = tempfile.mkdtemp()
    path = Path(storage_dir) / "u-bad-stats.json"
    path.write_text(
        json.dumps(
            {
                "statistics": {
                    "total_messages": None,
                    "total_trips": "12",
                    "last_trip_number": None,
                }
            }
        ),
        encoding="utf-8",
    )
    memory = LongTermMemory("u-bad-stats", storage_dir)
    memory.add_chat_message("user", "hi", "s1")
    assert memory.get_statistics()["total_messages"] == 1
    memory.increment_query_count()
    assert memory.get_statistics()["total_queries"] == 1
    trip_id = memory.save_trip_history(
        {
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-12-02",
            "purpose": "出差",
        }
    )
    assert trip_id == "trip_1", "last_trip_number 为 null 时也应从 1 开始编号"
    print("✓ statistics 计数器为 null / 字符串时自动修复")


def test_migrate_drops_non_object_entries():
    """chat_history / trip_history 里的非对象元素在迁移时丢弃。"""
    storage_dir = tempfile.mkdtemp()
    path = Path(storage_dir) / "u-bad-entry.json"
    path.write_text(
        json.dumps(
            {
                "chat_history": [
                    None,
                    "oops",
                    {"role": "user", "content": "hi", "session_id": "s1"},
                ],
                "trip_history": [42, ["x"]],
            }
        ),
        encoding="utf-8",
    )
    memory = LongTermMemory("u-bad-entry", storage_dir)
    assert len(memory.get_chat_history()) == 1
    assert memory.get_trip_history(limit=None) == []
    memory.add_chat_message("user", "again", "s1")
    assert len(memory.get_chat_history()) == 2
    print("✓ 非对象历史记录在迁移时被丢弃")


def test_pg_reconnects_after_connection_error():
    """连接被服务端终止后自动重连并重试一次。"""
    from context.postgres_long_term_memory import PostgresLongTermMemory

    class _Boom(Exception):
        pass

    class _BrokenConn:
        def cursor(self):
            raise _Boom("server closed the connection")

    class _GoodCursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=None):
            self.sql = sql

        def fetchall(self):
            return [(1,)]

    class _GoodConn:
        def cursor(self):
            return _GoodCursor()

    store = object.__new__(PostgresLongTermMemory)
    store.user_id = "u1"
    store.dsn = "postgresql://example/db"
    store._conn = _BrokenConn()
    store._retry_errors = (_Boom,)
    reconnects = []

    def fake_reconnect():
        reconnects.append(True)
        store._conn = _GoodConn()

    store._reconnect = fake_reconnect
    assert store._fetchall("SELECT 1") == [(1,)]
    assert reconnects == [True], "连接级错误后应重连一次并重试"
    print("✓ PostgreSQL 连接失效后重连重试")


def _restore_local() -> None:
    """恢复默认本地后端配置（本文件的配置改动不能泄漏到后续测试）。"""
    _set("redis_url", "")
    _set("postgres_dsn", "")


def test_describe_reports_actual_backend_after_cooldown(monkeypatch):
    """冷却过期后、下一次创建之前：描述仍反映实际在用的回退后端。"""
    _set("redis_url", "")
    _set("postgres_dsn", "postgresql://user:pass@127.0.0.1:1/db")
    try:

        def broken_pg(user_id, dsn):
            raise ConnectionError("refused")

        monkeypatch.setattr(storage, "PostgresLongTermMemory", broken_pg)
        memory = storage.create_long_term_memory("u-actual", tempfile.mkdtemp())
        assert isinstance(memory, LongTermMemory)
        # 模拟冷却过期：仅凭时间戳会误报 postgres，但实际仍是 JSON
        monkeypatch.setattr(storage, "_failed_recently", lambda key: False)
        info = storage.describe_backends()
        assert info["long_term"] == "json"
        assert info["long_term_degraded"] is True
    finally:
        _restore_local()
    print("✓ 后端描述使用实际创建结果而不是时间戳推断")


def test_strict_long_term_raises_when_unreachable():
    """strict 创建在 PG 不可用时抛 MemoryPersistenceError（不回退 JSON）。"""
    _set("redis_url", "")
    _set("postgres_dsn", "postgresql://user:pass@127.0.0.1:1/db")
    try:
        storage.create_long_term_memory("u-strict", tempfile.mkdtemp(), strict=True)
    except MemoryPersistenceError as exc:
        assert "PostgreSQL" in str(exc)
    else:
        raise AssertionError("strict 创建应抛 MemoryPersistenceError")
    finally:
        _restore_local()
    print("✓ strict 创建在 PG 不可用时直接失败")


def test_strict_long_term_raises_during_cooldown():
    """冷却期内 strict 创建直接失败；非 strict 仍回退 JSON 保持可用。"""
    dsn = "postgresql://user:pass@db.example/aligo"
    _set("redis_url", "")
    _set("postgres_dsn", dsn)
    try:
        storage._mark_failed(f"postgres:{dsn}")
        try:
            storage.create_long_term_memory(
                "u-strict2", tempfile.mkdtemp(), strict=True
            )
        except MemoryPersistenceError as exc:
            assert "冷却" in str(exc)
        else:
            raise AssertionError("冷却期 strict 创建应失败")
        memory = storage.create_long_term_memory("u-strict2", tempfile.mkdtemp())
        assert isinstance(memory, LongTermMemory)
    finally:
        _restore_local()
    print("✓ 冷却期 strict 创建失败、非 strict 回退 JSON")


def test_redact_url_masks_query_password():
    """redis-py 接受查询串里的 password 参数，日志脱敏必须覆盖。"""
    redacted = storage._redact_url(
        "redis://redis.internal:6379/0?password=s3cret&ssl=true"
    )
    assert "s3cret" not in redacted
    assert "password=***" in redacted and "ssl=true" in redacted
    assert storage._redact_url("redis://h:6379/0?Password=abc").endswith(
        "Password=***"
    )
    print("✓ Redis URL 查询串密码脱敏")


def test_reset_failure_cache_resets_short_term_degraded():
    """reset_failure_cache 同时清除短期记忆的降级冷却（测试 / 热更新用）。"""
    from context import short_term_memory as module

    module._redis_degraded_until = module.time.monotonic() + 60
    assert module.redis_short_term_degraded() is True
    storage.reset_failure_cache()
    assert module.redis_short_term_degraded() is False
    print("✓ reset_failure_cache 清除短期记忆降级")


def test_pg_failed_retry_wrapped_as_memory_error():
    """重连后仍失败：包装为 MemoryPersistenceError（按存储不可用降级）。"""
    from context.postgres_long_term_memory import PostgresLongTermMemory
    from utils.errors import MemoryPersistenceError

    class _Boom(Exception):
        pass

    class _BrokenConn:
        def cursor(self):
            raise _Boom("server closed the connection")

    store = object.__new__(PostgresLongTermMemory)
    store.user_id = "u1"
    store.dsn = "postgresql://example/db"
    store._conn = _BrokenConn()
    store._retry_errors = (_Boom,)

    def broken_reconnect():
        raise _Boom("connection refused")

    store._reconnect = broken_reconnect
    try:
        store._fetchall("SELECT 1")
    except MemoryPersistenceError as exc:
        assert "PostgreSQL" in str(exc)
    else:
        raise AssertionError("重试后仍失败应包装为 MemoryPersistenceError")
    print("✓ PostgreSQL 重试失败包装为存储不可用")


def test_falsey_preferences_included_in_context():
    """0 / False 是合法偏好，必须进入 Agent 上下文；空串视为未设置。"""
    from context.memory_manager import MemoryManager

    manager = MemoryManager("u-falsey", "s1", storage_path=tempfile.mkdtemp())
    manager.long_term.save_preference("budget_level", 0)
    manager.long_term.save_preference("window_seat", False)
    manager.long_term.save_preference("home_location", "")
    text = manager.get_context_for_agent()
    assert "budget_level: 0" in text
    assert "window_seat: False" in text
    assert "home_location" not in text
    print("✓ 0 / False 偏好进入上下文，空串不算")


def test_path_locks_do_not_grow_unbounded():
    """路径锁为弱引用：实例释放后自动移除，不随用户数无限增长。"""
    import gc

    from context import long_term_memory as lt

    storage_dir = tempfile.mkdtemp()
    for index in range(40):
        memory = LongTermMemory(f"u-weak-{index}", storage_dir)
        del memory
    gc.collect()
    assert len(lt._PATH_LOCKS) < 40, "锁表应随实例释放收缩"
    print("✓ 路径锁为弱引用，不随用户数无限增长")


def test_load_holds_path_lock():
    """读取全程持锁：Windows 上打开的文件句柄会让并发 os.replace 失败。"""
    import json as json_module

    from context import long_term_memory as lt

    storage_dir = tempfile.mkdtemp()
    path = Path(storage_dir) / "u-lock.json"
    path.write_text(json_module.dumps({"chat_history": []}), encoding="utf-8")
    lock = lt._lock_for_path(str(path))
    real_load = json_module.load
    checked = {"n": 0}

    def load_inside_lock(handle, *args, **kwargs):
        checked["n"] += 1
        assert getattr(lock, "_is_owned")(), "读取应在按路径锁内进行"
        return real_load(handle, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(json_module, "load", load_inside_lock)
        LongTermMemory("u-lock", storage_dir)
    assert checked["n"] >= 1
    print("✓ 记忆文件读取全程持锁")


def test_summary_not_starved_by_long_current_session():
    """当前会话很长时，跨会话总结仍应包含其它会话的历史。"""
    import asyncio

    from context.memory_manager import MemoryManager

    model = _CountingModel()
    manager = MemoryManager(
        "u-starve", "s-current", storage_path=tempfile.mkdtemp(), llm_model=model
    )
    manager.long_term.add_chat_message("user", "较早的其它会话内容", "s-other")
    for index in range(40):
        manager.long_term.add_chat_message("user", f"当前会话第 {index} 条", "s-current")
    summary = asyncio.run(manager.get_long_term_summary_async(max_messages=30))
    assert summary, "长会话不应让跨会话总结为空"
    assert model.calls == 1
    prompt_text = str(model.prompts[0])
    assert "较早的其它会话内容" in prompt_text, "总结提示词必须包含其它会话的历史"
    print("✓ 长当前会话不会饿死跨会话总结（提示词含其它会话）")


def test_append_preference_merges_atomically():
    """append_preference 在锁内重新加载后合并，避免覆盖并发写入。"""
    storage_dir = tempfile.mkdtemp()
    memory = LongTermMemory("u-append", storage_dir)
    assert memory.append_preference("hotel_brands", "汉庭") == ["汉庭"]
    assert memory.append_preference("hotel_brands", "如家") == ["汉庭", "如家"]
    assert memory.append_preference("hotel_brands", "如家") == ["汉庭", "如家"]
    memory.save_preference("seat_preference", "靠窗")
    assert memory.append_preference("seat_preference", "过道") == ["靠窗", "过道"]

    # 并发场景：先由记忆面板写入新值，再追加；追加基于磁盘最新值合并
    memory.save_preference("hotel_brands", ["万豪"])
    assert memory.append_preference("hotel_brands", "希尔顿") == ["万豪", "希尔顿"]

    # 模型给出列表形式的 append 值：逐项合并，不能存成嵌套列表
    memory.save_preference("airlines", ["东航"])
    assert memory.append_preference("airlines", ["国航", "东航"]) == ["东航", "国航"]
    assert memory.get_preference("airlines") == ["东航", "国航"]

    # 0 / False 是合法偏好：追加时不能被真值判断丢掉
    memory.save_preference("budget_min", 0)
    assert memory.append_preference("budget_min", 500) == [0, 500]
    memory.save_preference("window_seat", False)
    assert memory.append_preference("window_seat", True) == [False, True]

    reopened = LongTermMemory("u-append", storage_dir)
    assert reopened.get_preference("hotel_brands") == ["万豪", "希尔顿"]
    assert reopened.get_preference("airlines") == ["东航", "国航"]
    assert reopened.get_preference("budget_min") == [0, 500]
    print("✓ append_preference 原子合并（字符串→列表 / 列表逐项 / 去重 / 并发值 / 假值偏好保留）")


def test_summary_invalidated_by_trip_edit():
    """编辑行程（不更新时间戳）也要让跨会话总结缓存失效。"""
    import asyncio

    from context.memory_manager import MemoryManager

    model = _CountingModel()
    manager = MemoryManager(
        "u-trip-cache", "s-trip", storage_path=tempfile.mkdtemp(), llm_model=model
    )
    trip_id = manager.long_term.save_trip_history(
        {
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-12-01",
            "purpose": "出差",
        }
    )
    asyncio.run(manager.get_long_term_summary_async())
    assert model.calls == 1
    assert manager.long_term.update_trip(
        trip_id, {"start_date": "2026-12-10", "purpose": "会议"}
    )
    asyncio.run(manager.get_long_term_summary_async())
    assert model.calls == 2, "编辑行程后总结应重新生成"
    print("✓ 行程编辑使跨会话总结缓存失效")


def test_embedding_model_shared(monkeypatch):
    """同一嵌入模型路径在进程内只加载一次。"""
    import sys
    import types

    from utils import embedding_model as embedding_module

    embedding_module.reset_embedding_models()
    created: list[str] = []

    class FakeSentenceTransformer:
        def __init__(self, path):
            created.append(path)
            self.path = path

    fake_module = types.ModuleType("sentence_transformers")
    fake_module.SentenceTransformer = FakeSentenceTransformer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)
    try:
        first = embedding_module.get_embedding_model("model-x")
        second = embedding_module.get_embedding_model("model-x")
        assert first is second
        assert created == ["model-x"], "同一路径只加载一次"
        other = embedding_module.get_embedding_model("model-y")
        assert other is not first
        assert created == ["model-x", "model-y"]
    finally:
        embedding_module.reset_embedding_models()
    print("✓ 嵌入模型进程内共享（按路径缓存）")


def test_peek_embedding_dim_does_not_block_loading(monkeypatch):
    """首次加载（可达数十秒）期间状态探测立即返回 None，不能跟着挂起。"""
    import sys
    import threading
    import time
    import types

    from utils import embedding_model as embedding_module

    embedding_module.reset_embedding_models()
    started = threading.Event()
    release = threading.Event()

    class SlowSentenceTransformer:
        def __init__(self, path):
            started.set()
            release.wait(timeout=10)

        def get_sentence_embedding_dimension(self):
            return 512

    fake_module = types.ModuleType("sentence_transformers")
    fake_module.SentenceTransformer = SlowSentenceTransformer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)
    try:
        loader = threading.Thread(
            target=lambda: embedding_module.get_embedding_model("slow-model"),
            daemon=True,
        )
        loader.start()
        assert started.wait(timeout=5), "加载线程未开始"
        begin = time.monotonic()
        assert embedding_module.peek_embedding_dim() is None
        assert time.monotonic() - begin < 1.0, "探测不能等锁（会让状态接口挂起）"
        release.set()
        loader.join(timeout=5)
        assert embedding_module.peek_embedding_dim() == 512
    finally:
        release.set()
        embedding_module.reset_embedding_models()
    print("✓ 首次加载期间 peek_embedding_dim 不阻塞")


def test_next_trip_id_not_reused_after_deleting_latest():
    from context.long_term_memory import LongTermMemory

    memory = LongTermMemory("u-trip-id", tempfile.mkdtemp())
    base = {
        "origin": "北京",
        "destination": "上海",
        "start_date": "2026-12-01",
        "purpose": "出差",
    }
    assert memory.save_trip_history(dict(base)) == "trip_1"
    assert memory.save_trip_history({**base, "destination": "杭州"}) == "trip_2"
    assert memory.delete_trip("trip_2") is True
    third = memory.save_trip_history({**base, "destination": "广州"})
    assert third == "trip_3", "删除最新行程后新 id 不能复用"
    print("✓ 删除最新行程后 trip_id 不复用")


def test_delete_last_turn_cleans_session_metadata():
    """删除最后一轮删空会话时，标题 / 置顶 / 归档标记一并清理。"""
    from context.long_term_memory import LongTermMemory

    memory = LongTermMemory("u-last-turn-meta", tempfile.mkdtemp())
    memory.add_chat_message("user", "Q", "s-meta")
    memory.add_chat_message("assistant", "A", "s-meta")
    assert memory.set_session_title("s-meta", "标题")
    assert memory.set_session_pinned("s-meta", True)
    assert memory.set_session_archived("s-meta", True)
    assert memory.delete_last_turn("s-meta", expect="Q") == 2
    assert "s-meta" not in memory.get_session_titles()
    assert "s-meta" not in memory.get_pinned_sessions()
    assert "s-meta" not in memory.get_archived_sessions()
    print("✓ 删空会话清理标题 / 置顶 / 归档")


def test_migration_backs_up_and_resets_bad_destination_counts():
    """frequent_destinations 计数为字符串时归零并保留城市键，且先备份原始文件。"""
    from context.long_term_memory import LongTermMemory

    root = tempfile.mkdtemp()
    path = Path(root) / "u-dest-counts.json"
    path.write_text(
        json.dumps(
            {
                "chat_history": [],
                "trip_history": [],
                "preferences": [],
                "statistics": {
                    "total_trips": "many",
                    "frequent_destinations": {"杭州": "3", "上海": 2},
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    memory = LongTermMemory("u-dest-counts", root)
    destinations = memory.get_frequent_destinations()
    counts = dict(destinations)
    assert counts.get("杭州") == 0 and counts.get("上海") == 2, counts
    backup = Path(f"{path}.migrate-backup")
    assert backup.exists(), "破坏性迁移前必须备份原始文件"
    assert "many" in backup.read_text(encoding="utf-8")
    print("✓ 目的地计数修复且迁移前备份")


def test_reload_failure_raises_instead_of_stale_snapshot():
    """磁盘文件在运行中损坏：写入前的重载失败要报错，不能静默沿用旧快照。"""
    from utils.errors import MemoryPersistenceError

    from context.long_term_memory import LongTermMemory

    root = tempfile.mkdtemp()
    memory = LongTermMemory("u-reload-bad", root)
    path = Path(root) / "u-reload-bad.json"
    path.write_text("not-json", encoding="utf-8")
    try:
        memory.add_chat_message("user", "Q", "s1")
    except MemoryPersistenceError:
        pass
    else:
        raise AssertionError("重载失败必须抛 MemoryPersistenceError")
    print("✓ 重载失败不再静默沿用旧快照")


def test_one_day_trip_dedupes_end_date_variants():
    from context.long_term_memory import LongTermMemory

    memory = LongTermMemory("u-day-trip", tempfile.mkdtemp())
    first = memory.save_trip_history(
        {
            "origin": "北京",
            "destination": "天津",
            "start_date": "2026-12-01",
            "purpose": "出差",
        }
    )
    second = memory.save_trip_history(
        {
            "origin": "北京",
            "destination": "天津",
            "start_date": "2026-12-01",
            "end_date": "2026-12-01",
            "purpose": "出差",
        }
    )
    assert first == second, "无返程日期与返程=出发日应视为同一行程"
    assert len(memory.get_trip_history(limit=None)) == 1
    print("✓ 单日行程的两种返程写法视为同一条")


class _FakeLongTerm:
    """总结相关的最小长期记忆桩（可替换 chat / trip 历史）。"""

    snapshot_based = False

    def __init__(self, history=None, trips=None):
        self.history = list(history or [])
        self.trips = list(trips or [])

    def get_chat_history(self, session_id=None, limit=None):
        if session_id is not None:
            return [m for m in self.history if m.get("session_id") == session_id]
        if limit:
            return self.history[-limit:]
        return list(self.history)

    def get_trip_history(self, limit=None):
        return self.trips[:limit] if limit else list(self.trips)

    def get_preference(self):
        return {}


def test_summary_fingerprint_detects_history_replacement():
    """条数与最后时间戳不变、窗口内消息被替换时，总结缓存必须失效。"""
    import asyncio

    from context.memory_manager import MemoryManager

    model = _CountingModel()
    manager = MemoryManager(
        "u-fp", "s-cur", storage_path=tempfile.mkdtemp(), llm_model=model
    )
    manager.long_term = _FakeLongTerm(
        [
            {"role": "user", "content": "A", "timestamp": "t1", "session_id": "s1"},
            {"role": "assistant", "content": "B", "timestamp": "t2", "session_id": "s1"},
        ]
    )
    assert asyncio.run(manager.get_long_term_summary_async())
    assert model.calls == 1
    manager.long_term.history = [
        {"role": "user", "content": "C", "timestamp": "t1", "session_id": "s2"},
        {"role": "assistant", "content": "D", "timestamp": "t2", "session_id": "s2"},
    ]
    asyncio.run(manager.get_long_term_summary_async())
    assert model.calls == 2, "窗口内容变化必须使总结缓存失效"
    print("✓ 总结指纹感知窗口内容替换")


def test_summary_prompt_is_budgeted_and_formats_nulls():
    """超长历史按预算截断；行程字段为 null 时不能输出字面量 None。"""
    import asyncio

    from context.memory_manager import MemoryManager

    model = _CountingModel()
    manager = MemoryManager(
        "u-budget", "s-cur", storage_path=tempfile.mkdtemp(), llm_model=model
    )
    manager.long_term = _FakeLongTerm(
        [
            {
                "role": "assistant",
                "content": "x" * 5000,
                "timestamp": f"t{i}",
                "session_id": "s1",
            }
            for i in range(5)
        ],
        trips=[
            {
                "trip_id": "trip_1",
                "origin": None,
                "destination": "上海",
                "start_date": None,
                "end_date": None,
                "purpose": None,
            }
        ],
    )
    asyncio.run(manager.get_long_term_summary_async(max_messages=30))
    prompt = str(model.prompts[0])
    assert "None" not in prompt
    assert len(prompt) < 30000
    assert "…" in prompt
    print("✓ 总结 prompt 有长度预算且不输出 None")


def test_summary_failure_cooldown():
    """总结失败后 60 秒内不重复调用模型，冷却结束再重试。"""
    import asyncio

    from context.memory_manager import MemoryManager

    class _BoomModel:
        def __init__(self):
            self.calls = 0

        async def __call__(self, messages):
            self.calls += 1
            raise RuntimeError("model down")

    model = _BoomModel()
    manager = MemoryManager(
        "u-fail", "s-cur", storage_path=tempfile.mkdtemp(), llm_model=model
    )
    manager.long_term = _FakeLongTerm(
        [{"role": "user", "content": "A", "timestamp": "t1", "session_id": "s1"}]
    )
    assert asyncio.run(manager.get_long_term_summary_async()) == ""
    assert asyncio.run(manager.get_long_term_summary_async()) == ""
    assert model.calls == 1, "失败后冷却期内不应重复调用"
    manager._summary_failure_until = 0.0
    asyncio.run(manager.get_long_term_summary_async())
    assert model.calls == 2
    print("✓ 总结失败进入冷却，冷却后重试")


def test_empty_summary_enters_cooldown():
    """模型返回 200 但内容为空：与失败同样冷却，不能每轮重复调用。"""
    import asyncio

    from context.memory_manager import MemoryManager

    class _EmptyModel:
        def __init__(self):
            self.calls = 0

        async def __call__(self, messages):
            self.calls += 1

            async def _empty_chunks():
                if False:  # pragma: no cover - 仅声明为异步生成器
                    yield ""

            return _empty_chunks()

    model = _EmptyModel()
    manager = MemoryManager(
        "u-empty", "s-cur", storage_path=tempfile.mkdtemp(), llm_model=model
    )
    manager.long_term = _FakeLongTerm(
        [{"role": "user", "content": "A", "timestamp": "t1", "session_id": "s1"}]
    )
    assert asyncio.run(manager.get_long_term_summary_async()) == ""
    assert asyncio.run(manager.get_long_term_summary_async()) == ""
    assert model.calls == 1, "空响应后冷却期内不应重复调用"
    manager._summary_failure_until = 0.0
    asyncio.run(manager.get_long_term_summary_async())
    assert model.calls == 2
    print("✓ 空总结响应进入冷却")


def test_add_message_writes_long_term_first():
    """长期记忆写入失败时，短期记忆不能留下孤立提问。"""
    from context.memory_manager import MemoryManager

    class _BoomLongTerm:
        snapshot_based = False

        def add_chat_message(self, *args, **kwargs):
            raise RuntimeError("disk full")

    class _ShortTerm:
        def __init__(self):
            self.messages = []

        def add_message(self, role, content, metadata=None):
            self.messages.append(content)

    manager = MemoryManager("u-order", "s1", storage_path=tempfile.mkdtemp())
    manager.long_term = _BoomLongTerm()
    manager.short_term = _ShortTerm()
    try:
        manager.add_message("user", "hi")
    except RuntimeError:
        pass
    else:
        raise AssertionError("长期记忆写入失败应向上抛出")
    assert manager.short_term.messages == [], "长期记忆失败时短期记忆不能留下孤立提问"
    print("✓ add_message 先写长期记忆")


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

    manager.long_term.save_trip_history(
        {
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-12-01",
            "purpose": "出差",
        }
    )
    asyncio.run(manager.get_long_term_summary_async())
    assert model.calls == 3, "新增行程后应重新总结"

    manager.long_term.update_trip("trip_1", {"destination": "杭州"})
    asyncio.run(manager.get_long_term_summary_async())
    assert model.calls == 4, "修改行程目的地后应重新总结"
    print("✓ 长期记忆总结按指纹缓存")


def test_summary_cache_shared_across_max_messages():
    """CLI(50) 与对话 / 技能(30) 的总结调用必须共用缓存，不能重复调用 LLM。"""
    import asyncio

    from context.memory_manager import MemoryManager

    model = _CountingModel()
    manager = MemoryManager(
        "u-cache-window", "s-window", storage_path=tempfile.mkdtemp(), llm_model=model
    )
    manager.long_term.add_chat_message("user", "我之前去了上海", "s-other")
    first = asyncio.run(manager.get_long_term_summary_async(max_messages=30))
    second = asyncio.run(manager.get_long_term_summary_async(max_messages=50))
    assert first and first == second
    assert model.calls == 1, "不同 max_messages 不应让总结缓存互相失效"
    print("✓ 不同 max_messages 共用总结缓存")


def test_recovered_postgres_logs_fallback_warning(monkeypatch):
    """PG 从故障恢复、会话从 JSON 切回 PG 时记录「故障期数据未合并」警告。"""
    from config import STORAGE_CONFIG
    from context import memory_manager as mm_module

    manager = mm_module.MemoryManager(
        "u-switch", "s-switch", storage_path=tempfile.mkdtemp()
    )
    assert isinstance(manager.long_term, LongTermMemory)

    class _PostgresLike:
        snapshot_based = False

        def close(self):
            pass

    monkeypatch.setitem(STORAGE_CONFIG, "postgres_dsn", "postgresql://localhost/x")
    monkeypatch.setattr(
        mm_module, "create_long_term_memory", lambda user_id, path: _PostgresLike()
    )
    warnings: list[str] = []
    monkeypatch.setattr(
        mm_module.logger,
        "warning",
        lambda msg, *args: warnings.append(msg % args if args else msg),
    )
    manager._rebuild_long_term()
    assert isinstance(manager.long_term, _PostgresLike)
    assert any("不会自动合并" in text for text in warnings)
    print("✓ PG 恢复切换后端时记录回退数据警告")


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


def test_pg_retry_does_not_replay_non_idempotent_write():
    """连接级错误后，非幂等写入（不去重的批量插入）不得自动重放。"""
    import psycopg

    from context.postgres_long_term_memory import PostgresLongTermMemory
    from utils.errors import MemoryPersistenceError

    memory = object.__new__(PostgresLongTermMemory)
    memory._retry_errors = (psycopg.OperationalError, psycopg.InterfaceError)
    calls = []

    def op():
        calls.append(1)
        raise psycopg.OperationalError("lost after commit")

    try:
        memory._run_with_retry(op, replay_safe=False)
    except MemoryPersistenceError:
        pass
    else:
        raise AssertionError("非幂等写入应直接报持久化失败")
    assert calls == [1], "非幂等写入不能被重放"

    reconnects = []
    memory._reconnect = lambda: reconnects.append(1)
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise psycopg.OperationalError("temporary")
        return "ok"

    assert memory._run_with_retry(flaky) == "ok"
    assert reconnects == [1] and len(attempts) == 2, "幂等操作仍应重连重试一次"
    print("✓ PG 非幂等写入不重放、幂等操作重试")


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
    test_next_trip_id_not_reused_after_deleting_latest()
    test_one_day_trip_dedupes_end_date_variants()
    test_session_titles()
    test_session_pinned()
    test_add_message_preserves_metadata()
    test_clear_history_clears_metadata()
    test_corrupt_memory_file_is_backed_up()
    test_corrupt_backup_does_not_move_concurrently_written_file(pytest.MonkeyPatch())
    test_valid_json_non_object_memory_file_is_backed_up()
    test_null_list_fields_are_repaired()
    test_migrate_fills_statistics_defaults()
    test_migrate_repairs_bad_statistics_values()
    test_migrate_drops_non_object_entries()
    test_pg_reconnects_after_connection_error()
    test_pg_failed_retry_wrapped_as_memory_error()
    test_falsey_preferences_included_in_context()
    test_path_locks_do_not_grow_unbounded()
    test_load_holds_path_lock()
    test_pg_delete_trip_deletes_single_row()
    test_summary_not_starved_by_long_current_session()
    test_memory_persistence_error_is_not_retriable()
    test_redact_url_masks_password_containing_at()
    test_redact_url_masks_query_password()
    test_strict_long_term_raises_when_unreachable()
    test_strict_long_term_raises_during_cooldown()
    test_reset_failure_cache_resets_short_term_degraded()
    _mp = pytest.MonkeyPatch()
    try:
        test_describe_reports_actual_backend_after_cooldown(_mp)
    finally:
        _mp.undo()
    test_session_metadata_save_failure_raises()
    test_json_write_failures_raise()
    _mp = pytest.MonkeyPatch()
    try:
        test_describe_backends_configured_flags(_mp)
    finally:
        _mp.undo()
    test_export_data_includes_session_metadata()
    test_append_preference_merges_atomically()
    test_summary_invalidated_by_trip_edit()
    test_embedding_model_shared(pytest.MonkeyPatch())
    test_peek_embedding_dim_does_not_block_loading(pytest.MonkeyPatch())
    test_summary_cache_avoids_repeated_calls()
    test_summary_cache_shared_across_max_messages()
    test_summary_fingerprint_detects_history_replacement()
    test_summary_prompt_is_budgeted_and_formats_nulls()
    test_summary_failure_cooldown()
    test_empty_summary_enters_cooldown()
    test_add_message_writes_long_term_first()
    test_reload_long_term_respects_backend()
    test_recovered_postgres_logs_fallback_warning(pytest.MonkeyPatch())
    test_pg_retry_does_not_replay_non_idempotent_write()
    print("\n全部通过")
