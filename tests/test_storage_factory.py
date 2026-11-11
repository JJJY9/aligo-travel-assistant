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


def test_short_term_ttl_non_positive_clamped(monkeypatch):
    """REDIS_TTL_SEC=0（「永不过期」的误配置）：EXPIRE 非正数会删键，必须钳制。"""
    from context import storage as storage_module

    monkeypatch.setitem(
        storage_module.STORAGE_CONFIG, "redis_url", "redis://localhost:6379/0"
    )
    monkeypatch.setitem(storage_module.STORAGE_CONFIG, "redis_ttl_sec", 0)
    monkeypatch.setitem(
        storage_module._ACTUAL_BACKEND,
        "short_term",
        storage_module._ACTUAL_BACKEND.get("short_term"),
    )
    monkeypatch.setattr(storage_module, "_get_redis_client", lambda url: object())
    memory = storage_module.create_short_term_memory("s-ttl-zero")
    assert memory.ttl_sec == 1
    print("✓ 非正 REDIS_TTL_SEC 被钳制")


def test_redis_client_factory_single_flight(monkeypatch):
    """并发重建只创建一个客户端：不产生连接风暴 / 多连接池泄漏。"""
    import threading
    import time as time_module

    import redis as redis_module

    from context import storage as storage_module

    storage_module.reset_failure_cache()
    created: list[str] = []

    class _Client:
        def ping(self):
            return True

    def fake_from_url(url, **kwargs):
        created.append(url)
        time_module.sleep(0.05)
        return _Client()

    monkeypatch.setattr(redis_module.Redis, "from_url", fake_from_url)
    results: list[object] = []
    threads = [
        threading.Thread(
            target=lambda: results.append(
                storage_module._get_redis_client("redis://race/0")
            )
        )
        for _ in range(8)
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        assert created == ["redis://race/0"], created
        assert len({id(item) for item in results}) == 1
        assert results[0] is not None
    finally:
        storage_module.reset_failure_cache()
    print("✓ Redis 客户端并发重建单飞")


def test_redis_client_cache_is_url_scoped(monkeypatch):
    """客户端按 URL 缓存；reset 与在途创建互斥，旧客户端不会写回缓存。"""
    import threading
    import time as time_module

    import redis as redis_module

    from context import storage as storage_module

    class _Client:
        def __init__(self, url):
            self.url = url

        def ping(self):
            return True

    storage_module.reset_failure_cache()

    def fake_from_url(url, **kwargs):
        return _Client(url)

    monkeypatch.setattr(redis_module.Redis, "from_url", fake_from_url)
    try:
        a = storage_module._get_redis_client("redis://a/0")
        b = storage_module._get_redis_client("redis://b/0")
        assert a is not b and a.url == "redis://a/0"
        assert storage_module._get_redis_client("redis://a/0") is a
        # reset 后同 URL 得到新实例（旧客户端已被丢弃）
        storage_module.reset_failure_cache()
        assert storage_module._get_redis_client("redis://a/0") is not a

        # 在途创建期间 reset：锁内互斥保证旧结果不会在 reset 之后写回缓存
        started = threading.Event()

        def slow_from_url(url, **kwargs):
            started.set()
            time_module.sleep(0.3)
            return _Client(url)

        monkeypatch.setattr(redis_module.Redis, "from_url", slow_from_url)
        storage_module.reset_failure_cache()
        thread = threading.Thread(
            target=storage_module._get_redis_client, args=("redis://slow/0",)
        )
        thread.start()
        assert started.wait(2)
        storage_module.reset_failure_cache()  # 等创建完成后再清空
        thread.join(5)
        assert storage_module._redis_clients == {}
    finally:
        storage_module.reset_failure_cache()
    print("✓ Redis 客户端缓存按 URL 隔离且 reset 竞态安全")


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
    # 隔离路径必须可被只读命令（CLI stats）报告，否则空快照被当成空账号
    assert memory.get_recovered_corrupt_backup() == str(backups[0])
    print("✓ 损坏的记忆文件备份后重建（隔离路径可报告）")


def test_corrupt_backup_does_not_move_concurrently_written_file(monkeypatch):
    """解析失败后、备份前若其它线程写入了新文件，不能把有效数据当损坏文件移走。"""
    import json as json_module
    from pathlib import Path

    storage_dir = tempfile.mkdtemp()
    path = Path(storage_dir) / "u-race.json"
    path.write_text("{bad json", encoding="utf-8")
    real_load = json_module.loads
    calls = {"count": 0}

    def flaky_load(text, *args, **kwargs):
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
        return real_load(text, *args, **kwargs)

    monkeypatch.setattr(json_module, "loads", flaky_load)
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


def test_corrupt_pending_non_object_file_backed_up_before_write():
    """写前复核同样按损坏处理 [] / null（不能因「能解析」放行后永久写失败）。"""
    from pathlib import Path

    storage_dir = tempfile.mkdtemp()
    memory = LongTermMemory("u-pending", storage_dir)
    path = Path(storage_dir) / "u-pending.json"
    path.write_text("[]", encoding="utf-8")
    # 模拟 _load 因锁超时 / 备份失败以空快照启动：标记在、磁盘仍是坏文件
    memory._corrupt_pending = True  # noqa: SLF001 - 复现故障注入前置状态
    memory.save_preference("home_location", "北京")
    assert memory.get_preference("home_location") == "北京"
    backups = list(Path(storage_dir).glob("u-pending.json.corrupt-*"))
    assert backups, "非对象损坏文件必须补备份"
    assert backups[0].read_text(encoding="utf-8") == "[]"
    print("✓ 写前复核把非对象文件按损坏备份（写入不再永久失败）")


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


def test_pg_delete_duplicate_trip_keeps_last_copy():
    """PG delete_duplicate_trip：同 id 行只剩一条时拒绝删除（并发去重安全）。"""
    import contextlib

    from context.postgres_long_term_memory import PostgresLongTermMemory

    store = object.__new__(PostgresLongTermMemory)
    store.user_id = "u1"
    store._advisory_lock = lambda name: contextlib.nullcontext()
    executed: list[tuple[str, object]] = []

    def rowcount(sql, params=None):
        executed.append((sql, params))
        return 1

    store._execute_rowcount = rowcount
    assert store.delete_duplicate_trip("trip_9") is True
    delete_sql, delete_params = executed[-1]
    assert "DELETE FROM aligo_trips" in delete_sql
    assert "COUNT(*)" in delete_sql and ">= 2" in delete_sql
    assert delete_params == ("u1", "u1", "trip_9", "u1", "trip_9")

    executed.clear()
    store._execute_rowcount = lambda sql, params=None: (
        executed.append((sql, params)) or 0
    )
    assert store.delete_duplicate_trip("trip_9") is False
    assert len(executed) == 1, "只剩一条同 id 行时语句匹配 0 行（不删除）"
    print("✓ PostgreSQL delete_duplicate_trip 只剩一条时拒绝删除")


def test_pg_trip_lookup_normalizes_identity_fields():
    """PG 单条保存的去重查询按身份口径归一路线（CRLF / CR → LF）。"""
    import contextlib

    from context.postgres_long_term_memory import PostgresLongTermMemory

    store = object.__new__(PostgresLongTermMemory)
    store.user_id = "u1"
    store._conn = object()
    store._advisory_lock = lambda name: contextlib.nullcontext()
    calls: list[tuple[str, object]] = []
    store._fetchall = lambda sql, params: (calls.append((sql, params)) or [])
    store._next_trip_number = lambda: 1
    store._execute = lambda sql, params=None, **kwargs: calls.append((sql, params))

    trip_id = store._save_trip_history_locked(
        {
            "origin": "北京\r\n西城",
            "destination": "a\rb\r\nc",
            "start_date": "2026-1-5",
            "end_date": "2026-1-6",
            "purpose": "会议",
        },
        "会议",
        "a\rb\r\nc",
    )
    sql, params = calls[0]
    assert "REPLACE(REPLACE(destination" in sql
    assert "REPLACE(REPLACE(COALESCE(origin" in sql
    assert params == ("u1", "a\nb\nc", "北京\n西城")
    assert trip_id == "trip_1"
    print("✓ PostgreSQL 行程去重按身份口径归一路线")


def test_pg_chat_summary_does_not_fetch_message_bodies():
    """PG 会话概览只查计数与 DISTINCT 会话，不整表取回消息正文。"""
    from context.postgres_long_term_memory import PostgresLongTermMemory

    store = object.__new__(PostgresLongTermMemory)
    store.user_id = "u1"
    calls: list[tuple[str, object]] = []
    store._fetchone = lambda sql, params=None: (calls.append((sql, params)) or (7,))
    store._fetchall = lambda sql, params=None: (
        calls.append((sql, params)) or [("s1",), ("s2",), (None,)]
    )
    summary = store.get_chat_summary()
    assert summary["messages"] == 7
    assert summary["sessions"] == {"s1", "s2"}
    assert "COUNT(*)" in calls[0][0]
    assert "DISTINCT session_id" in calls[1][0]
    assert all("content" not in sql for sql, _ in calls), "不应查询消息正文"
    print("✓ PostgreSQL 会话概览只查计数（不读正文）")


def test_pg_delete_trips_verified_checks_identity_in_lock():
    """PG 批量去重：锁内按身份复核后按主键删除，身份不符不删。"""
    import contextlib

    from context.postgres_long_term_memory import PostgresLongTermMemory

    store = object.__new__(PostgresLongTermMemory)
    store.user_id = "u1"
    store._conn = object()
    store._advisory_lock = lambda name: contextlib.nullcontext()
    rows = [
        (1, "trip_1", "北京", "上海", "2026-12-01", "2026-12-02"),
        (2, "trip_1", "北京", "上海", "2026-12-01", "2026-12-02"),
        (3, "trip_2", "北京", "杭州", "2026-12-05", None),
    ]
    store._fetchall = lambda sql, params=None: list(rows)
    deleted: list[object] = []
    store._execute = lambda sql, params=None, **kwargs: deleted.append(params[1])

    target = {
        "trip_id": "trip_1",
        "kept_id": "trip_1",
        "prefer_last": True,
        "origin": "北京",
        "destination": "上海",
        "start_date": "2026-12-01",
        "end_date": "2026-12-02",
    }
    removed, failure = store.delete_trips_verified([target])
    assert (removed, failure) == (1, None)
    assert deleted == [2], "同 id 副本删最后一条"

    deleted.clear()
    removed, failure = store.delete_trips_verified(
        [{**target, "destination": "广州"}]
    )
    assert removed == 0 and failure is None and deleted == []
    print("✓ PostgreSQL 批量去重在锁内按身份复核")


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
    stats = memory.get_statistics()
    assert stats["total_messages"] == 1, "丢弃坏条目后计数与列表同步"
    assert stats["total_trips"] == 0
    memory.add_chat_message("user", "again", "s1")
    assert len(memory.get_chat_history()) == 2
    print("✓ 非对象历史记录在迁移时被丢弃")


def test_migrate_recomputes_counters_after_dropping_entries():
    """坏条目被丢弃后旧总数不能保留（统计会永久偏大）。"""
    storage_dir = tempfile.mkdtemp()
    path = Path(storage_dir) / "u-bad-count.json"
    path.write_text(
        json.dumps(
            {
                "chat_history": [
                    None,
                    {"role": "user", "content": "hi", "session_id": "s1"},
                ],
                "trip_history": [7],
                "statistics": {
                    "total_messages": 5,
                    "total_trips": 3,
                    "total_queries": 9,
                },
            }
        ),
        encoding="utf-8",
    )
    memory = LongTermMemory("u-bad-count", storage_dir)
    stats = memory.get_statistics()
    assert stats["total_messages"] == 1, stats
    assert stats["total_trips"] == 0, stats
    assert stats["total_queries"] == 9, "与列表无关的计数保持原值"
    print("✓ 丢弃坏条目后重算 total_messages / total_trips")


def test_migrate_missing_last_trip_number_is_not_destructive():
    """缺少 last_trip_number 是正常初始状态：不产生 .migrate-backup 与告警。"""
    storage_dir = tempfile.mkdtemp()
    path = Path(storage_dir) / "u-fresh.json"
    # 新建用户第一轮落盘后的典型形状（没有 last_trip_number）
    path.write_text(
        json.dumps(
            {
                "chat_history": [
                    {"role": "user", "content": "hi", "session_id": "s1"}
                ],
                "trip_history": [],
                "preferences": [],
                "statistics": {
                    "total_messages": 1,
                    "total_queries": 0,
                    "total_trips": 0,
                    "frequent_destinations": {},
                },
            }
        ),
        encoding="utf-8",
    )
    memory = LongTermMemory("u-fresh", storage_dir)
    memory.add_chat_message("user", "again", "s1")
    assert not (Path(storage_dir) / "u-fresh.json.migrate-backup").exists(), (
        "正常文件不应触发破坏性迁移备份"
    )
    print("✓ 缺失 last_trip_number 不再误报破坏性迁移")


def test_migrate_sanitizes_invalid_preferences():
    """type 非字符串 / 缺 value 的偏好记录在迁移时丢弃（否则整文件被隔离）。"""
    storage_dir = tempfile.mkdtemp()
    path = Path(storage_dir) / "u-bad-pref.json"
    path.write_text(
        json.dumps(
            {
                "preferences": [
                    {"type": ["hotel_brands"], "value": "汉庭"},
                    {"type": "home_location"},
                    {"type": "airlines", "value": "东航"},
                    {
                        "type": "preferences",
                        "value": [{"type": "hotel_brands"}, "x"],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    memory = LongTermMemory("u-bad-pref", storage_dir)
    assert memory.get_preference() == {"airlines": "东航"}
    # 破坏性清洗必须留备份
    assert (Path(storage_dir) / "u-bad-pref.json.migrate-backup").exists()
    print("✓ 非法偏好记录在迁移时清洗并备份")


def test_pg_data_error_wrapped_as_persistence_failure():
    """psycopg DataError（NUL 文本等）包装为 MemoryPersistenceError（503 而非 500）。"""
    import psycopg

    from context.postgres_long_term_memory import PostgresLongTermMemory
    from utils.errors import MemoryPersistenceError

    memory = PostgresLongTermMemory.__new__(PostgresLongTermMemory)
    memory._retry_errors = (psycopg.OperationalError, psycopg.InterfaceError)

    def _fail():
        raise psycopg.DataError(
            "PostgreSQL text fields cannot contain NUL (0x00) bytes"
        )

    with pytest.raises(MemoryPersistenceError):
        memory._run_with_retry(_fail)
    print("✓ psycopg DataError 包装为持久化失败")


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


def test_strict_error_message_redacts_dsn(monkeypatch):
    """strict 失败的错误文本与日志不带 DSN 凭据（userinfo / 查询串被清除）。"""
    dsn = "postgresql://user:SECRET@127.0.0.1:1/db?password=SECRET"
    _set("redis_url", "")
    _set("postgres_dsn", dsn)
    try:

        def boom(user_id, dsn_arg):
            raise RuntimeError(f"could not connect to {dsn_arg}")

        monkeypatch.setattr(storage, "PostgresLongTermMemory", boom)
        with pytest.raises(MemoryPersistenceError) as info:
            storage.create_long_term_memory(
                "u-redact", tempfile.mkdtemp(), strict=True
            )
        message = str(info.value)
        assert "SECRET" not in message, message
        assert "***@" in message
    finally:
        _restore_local()
    print("✓ strict 错误文本脱敏 DSN 凭据")


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


def test_pg_success_marks_actual_backend(monkeypatch):
    """PG 创建成功：记录实际后端为 postgres（describe_backends 同口径）。"""
    _set("redis_url", "")
    _set("postgres_dsn", "postgresql://user:pass@db.example/aligo")
    try:

        class _FakePg:
            def __init__(self, user_id, dsn):
                self.user_id = user_id
                self.dsn = dsn

        monkeypatch.setattr(storage, "PostgresLongTermMemory", _FakePg)
        memory = storage.create_long_term_memory("u-pg-ok", tempfile.mkdtemp())
        assert isinstance(memory, _FakePg)
        assert storage.describe_backends()["long_term"] == "postgres"
    finally:
        _restore_local()
    print("✓ PG 创建成功时记录实际后端")


def test_redis_import_failure_falls_back(monkeypatch):
    """redis 包未安装：短期记忆回退进程内实现（返回 None，不抛出）。"""
    storage.reset_failure_cache()
    monkeypatch.setitem(sys.modules, "redis", None)
    assert storage._get_redis_client("redis://localhost:1/0") is None
    print("✓ redis 未安装时回退 None")


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
    real_load = json_module.loads
    checked = {"n": 0}

    def load_inside_lock(text, *args, **kwargs):
        checked["n"] += 1
        assert getattr(lock, "_is_owned")(), "读取应在按路径锁内进行"
        return real_load(text, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(json_module, "loads", load_inside_lock)
        LongTermMemory("u-lock", storage_dir)
    assert checked["n"] >= 1
    print("✓ 记忆文件读取全程持锁")


def test_summary_not_starved_by_long_current_session():
    """当前会话超过总结窗口时，跨会话总结仍含其它会话与 NULL 会话历史。"""
    import asyncio

    from context.memory_manager import MemoryManager

    model = _CountingModel()
    manager = MemoryManager(
        "u-starve", "s-current", storage_path=tempfile.mkdtemp(), llm_model=model
    )
    manager.long_term.add_chat_message("user", "较早的其它会话内容", "s-other")
    manager.long_term.add_chat_message("user", "无会话归属的旧消息", None)
    for index in range(60):
        manager.long_term.add_chat_message("user", f"当前会话第 {index} 条", "s-current")
    summary = asyncio.run(manager.get_long_term_summary_async(max_messages=30))
    assert summary, "长会话不应让跨会话总结为空"
    assert model.calls == 1
    prompt_text = str(model.prompts[0])
    assert "较早的其它会话内容" in prompt_text, "总结提示词必须包含其它会话的历史"
    assert "无会话归属的旧消息" in prompt_text, "session_id 为空的旧消息仍应进入总结"
    assert "当前会话第" not in prompt_text, "当前会话的消息不应出现在跨会话总结里"
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


def test_trip_save_matches_linebreak_variants():
    """仅 CRLF 换行写法不同的同路线行程视为同一趟（导入 / 去重同一口径）。"""
    from context.long_term_memory import LongTermMemory

    memory = LongTermMemory("u-dup-crlf", tempfile.mkdtemp())
    first = memory.save_trip_history(
        {
            "origin": "北京\r\n西城",
            "destination": "a\rb\r\nc",
            "start_date": "2026-1-5",
            "end_date": "2026-1-6",
            "purpose": "会议",
        }
    )
    second = memory.save_trip_history(
        {
            "origin": "北京\n西城",
            "destination": "a\nb\nc",
            "start_date": "2026-01-05",
            "end_date": "2026-01-06",
        }
    )
    assert first == second
    trips = memory.get_trip_history(limit=None)
    assert len(trips) == 1
    assert trips[0]["purpose"] == "会议", "未提供目的时保留原值"
    print("✓ 换行 / 未补零日期不同的同一趟行程命中存储去重")


def test_delete_trips_verified_batches_single_write():
    """批量复核删除：同一把锁内按身份复核，只落盘一次（去重写放大修复）。"""
    storage_dir = tempfile.mkdtemp()
    memory = LongTermMemory("u-batch", storage_dir)
    memory.save_trip_history(
        {
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-12-01",
            "end_date": "2026-12-02",
            "purpose": "出差",
        }
    )
    # 手工注入同 id 同身份的重复副本（正常写入口径会去重，模拟历史脏数据）
    path = Path(storage_dir) / "u-batch.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    duplicate = dict(data["trip_history"][0])
    duplicate["purpose"] = "培训"
    data["trip_history"].append(duplicate)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    fresh = LongTermMemory("u-batch", storage_dir)
    saves = []
    real_save = fresh._save

    def counting_save():
        saves.append(1)
        return real_save()

    fresh._save = counting_save
    target = {
        "trip_id": "trip_1",
        "kept_id": "trip_1",
        "prefer_last": True,
        "origin": "北京",
        "destination": "上海",
        "start_date": "2026-12-01",
        "end_date": "2026-12-02",
    }
    removed, failure = fresh.delete_trips_verified([target])
    assert (removed, failure) == (1, None)
    assert len(saves) == 1, "批量删除必须只落盘一次"
    remaining = fresh.get_trip_history(limit=None)
    assert len(remaining) == 1 and remaining[0]["purpose"] == "出差"

    # 身份不一致：跳过不删
    removed, failure = fresh.delete_trips_verified(
        [{**target, "destination": "广州"}]
    )
    assert removed == 0 and failure is None
    assert len(fresh.get_trip_history(limit=None)) == 1
    print("✓ 批量复核删除一次落盘 / 身份不符跳过")


def test_delete_dirty_date_trip_keeps_frequent_destinations():
    """删除日期不可解析的行程不能把合法城市的常去计数误清零。"""
    storage_dir = tempfile.mkdtemp()
    memory = LongTermMemory("u-freq", storage_dir)
    created, updated = memory.save_trip_histories(
        [
            {
                "origin": "北京",
                "destination": "上海",
                "start_date": "2026-03-11",
                "end_date": "2026-03-12",
            },
            {
                "origin": "北京",
                "destination": "上海",
                "start_date": "2026/03/12",
                "end_date": "2026/03/13",
            },
        ]
    )
    assert (created, updated) == (2, 0)
    assert memory.get_statistics()["frequent_destinations"] == {"上海": 1}, (
        "脏日期行程不计数（与保存口径一致）"
    )
    assert memory.delete_trip("trip_2") is True
    assert memory.get_statistics()["frequent_destinations"] == {"上海": 1}, (
        "删除脏日期行程不能让合法城市的计数减免"
    )
    assert memory.delete_trip("trip_1") is True
    assert memory.get_statistics()["frequent_destinations"] == {}
    print("✓ 删除脏日期行程不误清零常去目的地")


def test_pg_frequent_destinations_skip_dirty_dates():
    """PG 常去目的地与 JSON 同口径：出发日期不可解析的行程不计入。"""
    from context.postgres_long_term_memory import PostgresLongTermMemory

    store = object.__new__(PostgresLongTermMemory)
    store.user_id = "u1"
    calls: list[tuple[str, object]] = []

    def fake_fetchall(sql, params=None):
        calls.append((sql, params))
        # 模拟旧备份恢复出的脏日期行（2026/03/12）与无日期行
        return [("上海", "2026-03-11"), ("上海", "2026/03/12"), ("杭州", None)]

    store._fetchall = fake_fetchall
    assert store.get_frequent_destinations(10) == [("上海", 1)]
    print("✓ PostgreSQL 常去目的地按日期可解析口径统计")


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

    def get_chat_history(self, session_id=None, limit=None, exclude_session_id=None):
        if session_id is not None:
            messages = [m for m in self.history if m.get("session_id") == session_id]
        elif exclude_session_id is not None:
            messages = [
                m for m in self.history if m.get("session_id") != exclude_session_id
            ]
        else:
            messages = list(self.history)
        if limit:
            return messages[-limit:]
        return messages

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


def test_summary_inputs_use_single_bounded_query():
    """跨会话总结在存储层一次取「排除当前会话的固定窗口」，不再全量数会话消息。"""
    import asyncio

    from context.memory_manager import MemoryManager, _SUMMARY_WINDOW_MESSAGES

    class _RecordingLongTerm(_FakeLongTerm):
        def __init__(self, history):
            super().__init__(history)
            self.calls: list[tuple[str | None, str | None, int | None]] = []

        def get_chat_history(
            self, session_id=None, limit=None, exclude_session_id=None
        ):
            self.calls.append((session_id, exclude_session_id, limit))
            return super().get_chat_history(
                session_id=session_id,
                limit=limit,
                exclude_session_id=exclude_session_id,
            )

    history = [
        {
            "role": "user",
            "content": f"自己的消息 {index}",
            "timestamp": f"t{index}",
            "session_id": "s-cur",
        }
        for index in range(200)
    ] + [
        {
            "role": "user",
            "content": "其它会话",
            "timestamp": "tx",
            "session_id": "s-other",
        }
    ]
    store = _RecordingLongTerm(history)
    manager = MemoryManager(
        "u-window", "s-cur", storage_path=tempfile.mkdtemp(), llm_model=_CountingModel()
    )
    manager.long_term = store
    asyncio.run(manager.get_long_term_summary_async())
    assert store.calls == [(None, "s-cur", _SUMMARY_WINDOW_MESSAGES)], (
        "应只发一次有界查询（limit=窗口），不能先拉全部历史再在 Python 过滤"
    )
    print("✓ 跨会话总结只发一次有界排除查询")


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


def test_reload_rejects_non_object_json():
    """磁盘文件是合法 JSON 但非对象（[] / null）：重载报错而不是沿用旧快照。"""
    from utils.errors import MemoryPersistenceError

    from context.long_term_memory import LongTermMemory

    root = tempfile.mkdtemp()
    memory = LongTermMemory("u-reload-list", root)
    path = Path(root) / "u-reload-list.json"
    for payload in ("[]", "null", chr(34) + "text" + chr(34)):
        path.write_text(payload, encoding="utf-8")
        try:
            memory.add_chat_message("user", "Q", "s1")
        except MemoryPersistenceError:
            pass
        else:
            raise AssertionError(f"非对象 JSON 必须报错：{payload}")
    print("✓ 非对象 JSON 重载报错")


def test_add_chat_messages_dedupe_sees_external_writes():
    """批量去重前在锁内重载：其它进程刚写入的相同消息会被跳过。"""
    from context.long_term_memory import LongTermMemory

    root = tempfile.mkdtemp()
    writer = LongTermMemory("u-batch-race", root)
    reader = LongTermMemory("u-batch-race", root)
    writer.add_chat_message("user", "X", "s1")
    added = reader.add_chat_messages(
        [{"role": "user", "content": "X", "session_id": "s1"}], dedupe=True
    )
    assert added == 0, "外部写入的相同消息必须被去重"
    assert len(writer.get_chat_history(session_id="s1")) == 1
    print("✓ 批量去重感知外部写入")


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


def test_pg_non_idempotent_call_sites_disable_replay():
    """消息 / 计数 / 行程 INSERT 与 delete_last_turn 的 DELETE 都禁止自动重放。"""
    import contextlib

    from context.postgres_long_term_memory import PostgresLongTermMemory

    memory = object.__new__(PostgresLongTermMemory)
    memory.user_id = "u-replay"
    recorded: list[tuple[str, bool]] = []
    memory._execute = lambda sql, params=None, replay_safe=True: recorded.append(
        (sql, replay_safe)
    )
    memory.add_chat_message("user", "hi", "s1")
    memory.add_chat_message(
        "user", "hi", "s1", timestamp="2026-01-01T00:00:00"
    )
    memory.increment_query_count()
    for sql, flag in recorded:
        assert flag is False, sql
    assert len(recorded) == 3

    memory._advisory_lock = lambda name: contextlib.nullcontext()
    memory._next_trip_number = lambda: 7
    memory._fetchall = lambda sql, params=None: []
    memory.save_trip_history({"destination": "上海", "start_date": "2026-01-01"})
    insert_calls = [
        (sql, flag) for sql, flag in recorded if "INSERT INTO aligo_trips" in sql
    ]
    assert insert_calls and insert_calls[-1][1] is False, insert_calls

    delete_calls: list[bool] = []
    memory._fetchall = lambda sql, params=None: [
        (2, "assistant", "a"),
        (1, "user", "q"),
    ]
    memory._run_with_retry = (
        lambda op, replay_safe=True: delete_calls.append(replay_safe) or 1
    )
    assert memory.delete_last_turn("s1", expect="q") == 1
    assert delete_calls == [False], "delete_last_turn 的 DELETE 不能自动重放"
    print("✓ PG 非幂等写路径全部禁止自动重放")


def test_pg_operations_serialized_across_threads():
    """同一实例的连接不能被两个线程同时使用（drain 超时后的旧落库线程）。"""
    import threading
    import time

    from context.postgres_long_term_memory import PostgresLongTermMemory

    memory = object.__new__(PostgresLongTermMemory)
    memory._retry_errors = ()
    active: list[int] = []
    overlaps: list[int] = []

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def execute(self, sql, params=None):
            active.append(1)
            if len(active) > 1:
                overlaps.append(1)
            time.sleep(0.01)
            active.pop()

    class _Conn:
        def cursor(self):
            return _Cursor()

    memory._conn = _Conn()

    def work():
        for _ in range(5):
            memory._execute("SELECT 1")

    threads = [threading.Thread(target=work) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert overlaps == [], "同一连接被并发使用（psycopg 非线程安全）"
    print("✓ PG 同实例操作跨线程串行化")


def test_pg_close_waits_for_inflight_operation():
    """close() 与数据库操作共用实例锁：不能关闭仍在执行的连接。"""
    import threading
    import time

    from context.postgres_long_term_memory import PostgresLongTermMemory

    memory = object.__new__(PostgresLongTermMemory)
    memory._retry_errors = ()
    active: list[int] = []
    overlapped: list[int] = []

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def execute(self, sql, params=None):
            active.append(1)
            time.sleep(0.05)
            active.pop()

    class _Conn:
        closed = False

        def cursor(self):
            return _Cursor()

        def close(self):
            if active:
                overlapped.append(1)
            self.closed = True

    conn = _Conn()
    memory._conn = conn
    worker = threading.Thread(target=lambda: memory._execute("SELECT 1"))
    worker.start()
    time.sleep(0.01)
    memory.close()
    worker.join()
    assert overlapped == [], "close 在操作执行中关闭了连接"
    assert conn.closed is True
    print("✓ PG close 等待在途操作结束")


def test_pg_add_chat_turn_locks_before_guard():
    """add_chat_turn 在同一事务内先锁会话元数据、再调守卫、最后插入。

    锁键必须与 delete_chat_session / _session_meta_guard 一致：
    「墓碑判定 + 插入」与并发删除串行化，守卫通过后删除不能抢先提交。
    """
    import contextlib as contextlib_module

    from context.postgres_long_term_memory import PostgresLongTermMemory

    events = []

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=None):
            events.append(("execute", str(sql), params))

        def executemany(self, sql, rows):
            events.append(("executemany", str(sql), list(rows)))

    class _Conn:
        def transaction(self):
            return contextlib_module.nullcontext()

        def cursor(self):
            return _Cursor()

    memory = object.__new__(PostgresLongTermMemory)
    memory.user_id = "u-order"
    memory._conn = _Conn()
    memory._retry_errors = ()

    def guard():
        events.append(("guard",))
        return True

    assert memory.add_chat_turn("问", "答", "s-lock", should_commit=guard) is True
    assert events[0][0] == "execute"
    assert "pg_advisory_xact_lock(hashtext(%s))" in events[0][1]
    assert events[0][2] == ("aligo:session_meta:u-order",)
    assert events[1] == ("guard",), "守卫必须在拿到会话锁之后调用"
    assert events[2][0] == "executemany"
    rows = events[2][2]
    assert [row[1] for row in rows] == ["user", "assistant"]
    assert [row[2] for row in rows] == ["问", "答"]

    # 守卫否决：拿到锁后直接结束，不插入任何消息
    events.clear()
    assert (
        memory.add_chat_turn(
            "问",
            "答",
            "s-lock",
            should_commit=lambda: (events.append(("guard",)), False)[1],
        )
        is False
    )
    assert [event[0] for event in events] == ["execute", "guard"]
    print("✓ PG add_chat_turn 先锁会话元数据、再守卫、再写入")


def test_pg_save_trip_histories_batches_with_one_index():
    """PG 批量保存：一次读历史建索引，锁内逐条更新/插入，编号水位一次写。"""
    import contextlib as contextlib_module

    from context.postgres_long_term_memory import PostgresLongTermMemory

    store = object.__new__(PostgresLongTermMemory)
    store.user_id = "u-pg-bulk"
    store._conn = object()
    state = {"locked": False, "events": []}

    @contextlib_module.contextmanager
    def fake_lock(name):
        assert state["locked"] is False, "同一用户锁不应嵌套获取"
        state["locked"] = True
        state["events"].append(("lock", name))
        try:
            yield
        finally:
            state["locked"] = False
            state["events"].append(("unlock", name))

    store._advisory_lock = fake_lock
    history = [
        (1, "trip_1", "北京", "上海", "2026-12-01", "2026-12-03"),
        (2, "trip_2", "北京", "广州", "2026-12-10", "2026-12-11"),
    ]
    executed = []
    inserted_params = []
    next_id = [10]

    def fetchall(sql, params=None):
        assert state["locked"] is True, "读历史必须在锁内"
        assert "FROM aligo_trips WHERE user_id" in sql
        return list(history)

    def fetchone(sql, params=None, replay_safe=True):
        assert state["locked"] is True
        if sql.startswith("SELECT last_number"):
            return (2,)
        if sql.startswith("INSERT INTO aligo_trips"):
            assert replay_safe is False, "批量 INSERT 不能自动重放"
            inserted_params.append(params)
            next_id[0] += 1
            return (next_id[0],)
        raise AssertionError(sql)

    def execute(sql, params=None):
        assert state["locked"] is True, "写入必须在锁内"
        executed.append((" ".join(sql.split()), params))

    store._fetchall = fetchall
    store._fetchone = fetchone
    store._execute = execute

    trips = [
        {
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-12-01",
            "end_date": "2026-12-03",
        },
        {
            "origin": "北京",
            "destination": "杭州",
            "start_date": "2026-12-20",
            "end_date": "2026-12-21",
            "purpose": "会议",
        },
        {
            "origin": "北京",
            "destination": "杭州",
            "start_date": "2026-12-20",
            "end_date": "2026-12-21",
        },
    ]
    assert store.save_trip_histories(trips) == (1, 2)
    updates = [
        params for sql, params in executed if sql.startswith("UPDATE aligo_trips")
    ]
    assert updates[0] == (None, None, 1), "缺省目的 / 会话保留原值"
    assert updates[1] == (None, None, 11), "批内重复命中刚插入的行"
    inserts = inserted_params
    assert len(inserts) == 1
    assert inserts[0][1] == "trip_3"
    assert inserts[0][2] == "北京" and inserts[0][4] == "2026-12-20"
    assert inserts[0][6] == "会议"
    counters = [
        params
        for sql, params in executed
        if sql.startswith("INSERT INTO aligo_trip_counters")
    ]
    assert counters == [("u-pg-bulk", 3)], "编号水位一次写入"
    assert state["events"][0][0] == "lock" and state["events"][-1][0] == "unlock"
    print("✓ PG 批量保存一次索引 / 批内去重 / 编号水位")


def test_json_add_chat_turn_guard_runs_under_write_lock():
    """JSON add_chat_turn 的守卫在 @_locked_mutation 写锁内调用。"""
    storage_dir = tempfile.mkdtemp()
    memory = LongTermMemory("u-turn-lock", storage_dir)
    owned = []

    def guard():
        owned.append(bool(getattr(memory._write_lock, "_is_owned")()))
        return True

    assert memory.add_chat_turn("问", "答", "s-lock", should_commit=guard) is True
    assert owned == [True], "守卫与写入必须在同一临界区（含重载）内"
    reopened = LongTermMemory("u-turn-lock", storage_dir)
    assert [m["content"] for m in reopened.get_chat_history(session_id="s-lock")] == [
        "问",
        "答",
    ]
    print("✓ JSON add_chat_turn 守卫在写锁内执行")


def test_json_add_chat_turn_guard_sees_concurrent_delete():
    """两实例并发：删除持锁期间落库阻塞，守卫在锁内看到删除后的历史。"""
    import threading

    storage_dir = tempfile.mkdtemp()
    memory_a = LongTermMemory("u-turn-race", storage_dir)
    assert memory_a.add_chat_turn("旧问", "旧答", "s-race") is True
    memory_b = LongTermMemory("u-turn-race", storage_dir)
    # A 的快照里仍有一轮：守卫若在锁外读取会误判为「会话仍存在」
    assert memory_a.get_chat_history(session_id="s-race"), "A 应持有删除前的快照"

    lock_held = threading.Event()
    allow_delete = threading.Event()
    delete_done = threading.Event()
    worker_started = threading.Event()
    order: list[str] = []
    guard_calls: list[bool] = []
    result: list[bool] = []

    def deleter():
        with memory_b._write_lock:
            lock_held.set()
            assert allow_delete.wait(10)
            memory_b.delete_chat_session("s-race")
            order.append("delete")
        delete_done.set()

    def guard():
        guard_calls.append(True)
        order.append("guard")
        # 会话仍存在（历史非空）才允许写入；读到旧快照会误判为 True
        return bool(memory_a.get_chat_history(session_id="s-race"))

    def writer():
        worker_started.set()
        result.append(
            memory_a.add_chat_turn("新问", "新答", "s-race", should_commit=guard)
        )

    delete_thread = threading.Thread(target=deleter)
    delete_thread.start()
    assert lock_held.wait(10), "删除线程未能持锁"
    write_thread = threading.Thread(target=writer)
    write_thread.start()
    assert worker_started.wait(10)
    # 删除线程仍持锁：落库必须阻塞在写锁上，守卫不能被提前调用
    assert not delete_done.wait(0.5)
    assert guard_calls == [], "删除持锁期间落库不能进入临界区"

    allow_delete.set()
    write_thread.join(10)
    delete_thread.join(10)
    assert not write_thread.is_alive() and not delete_thread.is_alive()
    assert guard_calls == [True]
    assert order == ["delete", "guard"], "守卫必须等删除提交后在锁内执行"
    assert result == [False], "会话已删除，在途落库必须被否决"

    reopened = LongTermMemory("u-turn-race", storage_dir)
    assert reopened.get_chat_history(session_id="s-race") == [], "被否决的一轮不能写回"
    assert reopened.get_statistics()["total_messages"] == 0
    print("✓ JSON add_chat_turn 与并发删除串行化，守卫看到删除后的历史")


def test_json_get_chat_history_excludes_session_before_limit():
    """JSON get_chat_history：先排除会话再取最近 limit 条（含 NULL 会话旧消息）。"""
    storage_dir = tempfile.mkdtemp()
    memory = LongTermMemory("u-exclude", storage_dir)
    memory.add_chat_message("user", "无会话旧消息", None)
    for index in range(3):
        memory.add_chat_message("user", f"其它会话 {index}", "s-other")
    for index in range(60):
        memory.add_chat_message("user", f"当前会话 {index}", "s-cur")

    window = memory.get_chat_history(limit=10, exclude_session_id="s-cur")
    assert [m["content"] for m in window] == [
        "无会话旧消息",
        "其它会话 0",
        "其它会话 1",
        "其它会话 2",
    ], "limit 必须作用在排除后的集合上，不能先切片再过滤"
    assert all(m.get("session_id") != "s-cur" for m in window)

    tail = memory.get_chat_history(limit=2, exclude_session_id="s-cur")
    assert [m["content"] for m in tail] == ["其它会话 1", "其它会话 2"]

    # 同时给出 session_id 与 exclude_session_id：session_id 优先（文档语义）
    only_cur = memory.get_chat_history(
        limit=2, session_id="s-cur", exclude_session_id="s-cur"
    )
    assert [m["content"] for m in only_cur] == ["当前会话 58", "当前会话 59"]
    print("✓ JSON 会话历史排除查询与 limit 顺序")


def test_pg_get_chat_history_excludes_session_in_sql():
    """PG get_chat_history：排除用 IS DISTINCT FROM 保留 NULL 会话旧消息。"""
    from datetime import datetime as datetime_cls

    from context.postgres_long_term_memory import PostgresLongTermMemory

    store = object.__new__(PostgresLongTermMemory)
    store.user_id = "u-excl"
    calls = []

    def fake_fetchall(sql, params=None):
        calls.append((" ".join(str(sql).split()), params))
        return [("assistant", "无会话旧消息", None, datetime_cls(2026, 1, 1, 10, 0, 0))]

    store._fetchall = fake_fetchall
    history = store.get_chat_history(limit=10, exclude_session_id="s-cur")
    sql, params = calls[0]
    assert "session_id IS DISTINCT FROM %s" in sql, "NULL 会话旧消息必须保留（!= 会漏掉）"
    assert "ORDER BY id DESC LIMIT %s" in sql
    assert params == ("u-excl", "s-cur", 10)
    assert history == [
        {
            "role": "assistant",
            "content": "无会话旧消息",
            "session_id": None,
            "timestamp": "2026-01-01T10:00:00",
        }
    ]

    calls.clear()
    store.get_chat_history(limit=5, session_id="s-1", exclude_session_id="s-2")
    sql, params = calls[0]
    assert "session_id = %s" in sql and "DISTINCT" not in sql
    assert params == ("u-excl", "s-1", 5), "session_id 优先，排除参数被忽略"
    print("✓ PG 会话历史排除 SQL 保留 NULL 会话消息")


def test_bulk_save_trips_scales_and_dedupes():
    """批量保存行程：一次建索引（O(N)）且去重 / 编号 / 统计语义与单条一致。"""
    import time

    storage_dir = tempfile.mkdtemp()
    memory = LongTermMemory("u-bulk-trips", storage_dir)
    rows = [
        {
            "origin": "北京",
            "destination": f"D{index}",
            "start_date": "2026-12-01",
            "end_date": "2026-12-02",
            "purpose": "出差",
        }
        for index in range(2000)
    ]
    started = time.perf_counter()
    created, updated = memory.save_trip_histories(rows)
    elapsed = time.perf_counter() - started
    assert (created, updated) == (2000, 0)
    assert elapsed < 2.0, f"批量保存 2000 条耗时 {elapsed:.2f}s，疑似仍是逐条重扫历史"
    trips = memory.get_trip_history(limit=None)
    assert len(trips) == 2000
    assert len({trip["trip_id"] for trip in trips}) == 2000
    assert memory.get_statistics()["total_trips"] == 2000

    # 同一批再存一次：全部命中既有记录（更新），不新增、不重复计数
    created, updated = memory.save_trip_histories(rows)
    assert (created, updated) == (0, 2000)
    assert len(memory.get_trip_history(limit=None)) == 2000

    # 未提供的目的 / 会话保留原值（与单条保存同一契约）
    created, updated = memory.save_trip_histories(
        [
            {
                "origin": "北京",
                "destination": "D0",
                "start_date": "2026-12-01",
                "end_date": "2026-12-02",
            }
        ]
    )
    assert (created, updated) == (0, 1)
    kept = next(trip for trip in trips if trip["destination"] == "D0")
    assert kept["purpose"] == "出差"

    # 还原备份自带 trip_id 时编号水位按实际 id 抬高
    restored = [
        {"origin": "上海", "destination": "R1", "start_date": "2026-12-05"}
    ]
    restored[0]["trip_id"] = "trip_9001"
    created, updated = memory.save_trip_histories(restored)
    assert (created, updated) == (1, 0)
    assert memory.get_statistics()["last_trip_number"] == 9001
    added = memory.save_trip_histories(
        [{"origin": "上海", "destination": "R2", "start_date": "2026-12-06"}]
    )
    assert added == (1, 0)
    newest = memory.get_trip_history(limit=1)[0]
    assert newest["trip_id"] == "trip_9002"

    reopened = LongTermMemory("u-bulk-trips", storage_dir)
    assert len(reopened.get_trip_history(limit=None)) == 2002
    print("✓ 批量行程保存一次建索引（去重 / 编号 / 统计语义一致）")


def test_pg_connect_sets_lock_timeout_and_keepalives(monkeypatch):
    """生产连接默认带 lock_timeout + TCP keepalive；statement_timeout 按需。"""
    import psycopg

    from context.postgres_long_term_memory import PostgresLongTermMemory

    captured = {}

    class _Conn:
        pass

    def fake_connect(dsn, **kwargs):
        captured["dsn"] = dsn
        captured["kwargs"] = kwargs
        return _Conn()

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    store = object.__new__(PostgresLongTermMemory)
    store.dsn = "postgresql://example/db"
    store._statement_timeout_ms = None
    store._connect()
    kwargs = captured["kwargs"]
    assert "-c lock_timeout=3000" in kwargs["options"], kwargs["options"]
    assert "statement_timeout" not in kwargs["options"], kwargs["options"]
    assert kwargs["keepalives"] == 1
    assert kwargs["keepalives_idle"] == 30
    assert kwargs["keepalives_interval"] == 10
    assert kwargs["keepalives_count"] == 3

    captured.clear()
    store._statement_timeout_ms = 3000
    store._connect()
    options = captured["kwargs"]["options"]
    assert "-c lock_timeout=3000" in options, options
    assert "-c statement_timeout=3000" in options, options
    print("✓ PG 连接默认 lock_timeout + keepalive，statement_timeout 按需")


def test_pg_schema_initialized_once_per_process():
    """每个 DSN 只在进程内建表一次；并发的构造不重复执行 DDL。"""
    import threading as threading_module

    from context import postgres_long_term_memory as pg_module

    dsn = "postgresql://schema-once/test"
    pg_module._SCHEMA_READY.discard(dsn)

    class _Cursor:
        def __init__(self, conn):
            self._conn = conn

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=None):
            self._conn.statements.append(" ".join(str(sql).split()))

    class _Conn:
        def __init__(self):
            self.statements = []

        def cursor(self):
            return _Cursor(self)

    def make_store():
        store = object.__new__(pg_module.PostgresLongTermMemory)
        store.dsn = dsn
        store._conn = _Conn()
        return store

    try:
        first = make_store()
        first._ensure_schema()
        assert any(
            statement.startswith("SELECT pg_advisory_lock")
            for statement in first._conn.statements
        ), first._conn.statements
        assert any(
            "CREATE UNIQUE INDEX" in statement
            for statement in first._conn.statements
        ), first._conn.statements
        created = sum(
            1 for statement in first._conn.statements if "CREATE TABLE" in statement
        )
        assert created >= 1

        second = make_store()
        second._ensure_schema()
        assert second._conn.statements == [], "同一 DSN 第二次构造不能再跑 DDL"

        # 并发构造：只有一个线程执行建表，其余直接跳过
        stores = [make_store() for _ in range(4)]
        threads = [
            threading_module.Thread(target=store._ensure_schema)
            for store in stores
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sum(len(store._conn.statements) for store in stores) == 0
    finally:
        pg_module._SCHEMA_READY.discard(dsn)
    print("✓ PG 表结构每个 DSN 只建一次")


def test_pg_schema_tolerates_concurrent_create_race():
    """滚动发布中其它实例先建表：DuplicateTable 后重跑 DDL，索引照建。"""
    import psycopg

    from context import postgres_long_term_memory as pg_module

    dsn = "postgresql://schema-race/test"
    pg_module._SCHEMA_READY.discard(dsn)
    calls = {"ddl": 0, "index": 0, "unlock": 0}

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=None):
            text = " ".join(str(sql).split())
            if text.startswith("CREATE TABLE"):
                calls["ddl"] += 1
                if calls["ddl"] == 1:
                    raise psycopg.errors.DuplicateTable("duplicate table")
            elif "CREATE UNIQUE INDEX" in text:
                calls["index"] += 1
            elif "pg_advisory_unlock" in text:
                calls["unlock"] += 1

    class _Conn:
        def cursor(self):
            return _Cursor()

    store = object.__new__(pg_module.PostgresLongTermMemory)
    store.dsn = dsn
    store._conn = _Conn()
    try:
        store._ensure_schema()
    finally:
        pg_module._SCHEMA_READY.discard(dsn)
    assert calls["ddl"] == 2, "目录竞争后应重跑一次 DDL"
    assert calls["index"] == 1
    assert calls["unlock"] == 1
    print("✓ 并发建表竞争被容忍并补齐")


def test_pg_update_trip_aborts_when_connection_reconnects():
    """update_trip 的「冲突检查 + 写入」跨连接重建时中止，不按过期快照更新。"""
    import contextlib

    from context.postgres_long_term_memory import PostgresLongTermMemory
    from utils.errors import MemoryPersistenceError

    store = object.__new__(PostgresLongTermMemory)
    store.user_id = "u1"
    store._advisory_lock = lambda name: contextlib.nullcontext()
    store._conn = object()
    writes = []

    def fetchone(sql, params=None):
        # 模拟读取后连接被重建（_reconnect 只重放本次查询）
        store._conn = object()
        return (42, "北京", "杭州", "2026-12-01", "2026-12-02")

    store._fetchone = fetchone
    store._fetchall = lambda sql, params=None: []
    store._execute = lambda *args, **kwargs: writes.append(args)

    with pytest.raises(MemoryPersistenceError):
        store.update_trip("trip_1", {"purpose": "出差"})
    assert writes == [], "连接重建后不能再基于旧快照写入"
    print("✓ update_trip 跨重连中止写入")


def test_pg_append_preference_aborts_when_connection_reconnects():
    """append_preference 的「读 + 合并 + 写」跨连接重建时中止，不覆盖别人更新。"""
    import contextlib

    from context.postgres_long_term_memory import PostgresLongTermMemory
    from utils.errors import MemoryPersistenceError

    store = object.__new__(PostgresLongTermMemory)
    store.user_id = "u1"
    store._preference_lock = lambda pref_type: contextlib.nullcontext()
    store._conn = object()
    saved = []

    def fake_get(pref_type=None):
        store._conn = object()
        return "汉庭"

    store.get_preference = fake_get
    store.save_preference = lambda pref_type, value: saved.append((pref_type, value))

    with pytest.raises(MemoryPersistenceError):
        store.append_preference("hotel_brands", "如家")
    assert saved == [], "连接重建后不能按旧偏好合并写回"
    print("✓ append_preference 跨重连中止写入")


def test_pg_save_trip_aborts_when_connection_reconnects():
    """单条行程保存的候选快照跨连接重建时中止，不造出重复行程。"""
    from context.postgres_long_term_memory import PostgresLongTermMemory
    from utils.errors import MemoryPersistenceError

    store = object.__new__(PostgresLongTermMemory)
    store.user_id = "u1"
    store._conn = object()

    def fetchall(sql, params=None):
        store._conn = object()
        return []

    store._fetchall = fetchall
    store._execute = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("不应写入")
    )

    with pytest.raises(MemoryPersistenceError):
        store._save_trip_history_locked(
            {"origin": "北京", "destination": "杭州", "start_date": "2026-12-01"},
            "出差",
            "杭州",
        )
    print("✓ 单条行程保存跨重连中止写入")


def test_pg_delete_duplicate_trip_is_single_atomic_statement():
    """去重删除必须是单条 SQL：重连重放时计数在服务端重新求值，不删光保留行。"""
    import contextlib as contextlib_module

    from context.postgres_long_term_memory import PostgresLongTermMemory

    executed = []

    class _Cursor:
        rowcount = 1

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=None):
            executed.append((" ".join(str(sql).split()), params))

    def make_store(rowcount):
        cursor = _Cursor()
        cursor.rowcount = rowcount
        store = object.__new__(PostgresLongTermMemory)
        store.user_id = "u-dedupe"
        store._conn = type("_Conn", (), {"cursor": lambda self: cursor})()
        store._retry_errors = (ConnectionError,)
        store._advisory_lock = lambda name: contextlib_module.nullcontext()
        return store

    assert make_store(1).delete_duplicate_trip("trip_1") is True
    sql, params = executed[0]
    assert sql.startswith("DELETE FROM aligo_trips"), sql
    assert "ORDER BY id DESC LIMIT 1" in sql and "COUNT(*)" in sql, sql
    assert len(executed) == 1, "计数 / 选行 / 删除必须是一条语句"
    assert params == (
        "u-dedupe",
        "u-dedupe",
        "trip_1",
        "u-dedupe",
        "trip_1",
    ), params

    executed.clear()
    assert make_store(0).delete_duplicate_trip("trip_1") is False
    assert len(executed) == 1
    print("✓ PG 去重删除为单条原子语句")


def test_pg_batch_aborts_when_connection_reconnects():
    """批量保存中途连接重建：索引 / 编号水位已过期，必须中止而不是继续生成 id。"""
    import contextlib as contextlib_module

    from context.postgres_long_term_memory import PostgresLongTermMemory
    from utils.errors import MemoryPersistenceError

    store = object.__new__(PostgresLongTermMemory)
    store.user_id = "u-pg-reconnect"
    store._conn = object()
    store._advisory_lock = lambda name: contextlib_module.nullcontext()
    executed = []

    def fetchall(sql, params=None):
        return []

    def fetchone(sql, params=None, replay_safe=True):
        if sql.startswith("SELECT last_number"):
            return (0,)
        if sql.startswith("INSERT INTO aligo_trips"):
            # 模拟 INSERT 期间连接失效并被 _run_with_retry 重连
            store._conn = object()
            return (5,)
        raise AssertionError(sql)

    def execute(sql, params=None):
        executed.append(" ".join(sql.split()))

    store._fetchall = fetchall
    store._fetchone = fetchone
    store._execute = execute

    with pytest.raises(MemoryPersistenceError, match="连接在批量保存中被重建"):
        store.save_trip_histories(
            [
                {
                    "origin": "北京",
                    "destination": "上海",
                    "start_date": "2026-12-01",
                }
            ]
        )
    assert not any("aligo_trip_counters" in sql for sql in executed), (
        "连接重建后不能再用过期编号水位写计数器"
    )
    print("✓ PG 批量保存连接重建时中止")


def test_frequent_destinations_tie_order_is_deterministic():
    """同数量的常去目的地按城市名排序（与 PostgreSQL ORDER BY 同口径）。"""
    memory = LongTermMemory("u-freq-order", tempfile.mkdtemp())
    memory.save_trip_history(
        {"origin": "", "destination": "杭州", "start_date": "2026-12-01"}
    )
    memory.save_trip_history(
        {"origin": "", "destination": "广州", "start_date": "2026-12-02"}
    )
    memory.save_trip_history(
        {"origin": "", "destination": "上海", "start_date": "2026-12-03"}
    )
    assert memory.get_frequent_destinations(10) == [
        ("上海", 1),
        ("广州", 1),
        ("杭州", 1),
    ]
    print("✓ 常去目的地同数量按城市名排序")


def test_bulk_save_trips_matches_sequential_saves():
    """批量保存与逐条保存的最终结果一致（去重 / 未补零日期 / 缺省目的 / 会话）。"""
    storage_dir = tempfile.mkdtemp()
    batch = LongTermMemory("u-eq-batch", storage_dir)
    sequential = LongTermMemory("u-eq-single", storage_dir)
    rows = [
        {
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-12-01",
            "end_date": "2026-12-03",
            "purpose": "出差",
        },
        {
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-12-1",
            "end_date": "2026-12-3",
            "purpose": "客户会议",
        },
        {
            "origin": "北京",
            "destination": "杭州",
            "start_date": "2026-12-10",
            "purpose": "培训",
        },
        {
            "origin": "北京",
            "destination": "杭州",
            "start_date": "2026-12-10",
            "end_date": "2026-12-10",
        },
        {
            "origin": "",
            "destination": "广州",
            "start_date": "2026-12-20",
            "end_date": "2026-12-22",
            "purpose": "会议",
            "session_id": "s-1",
        },
        {
            "origin": "",
            "destination": "广州",
            "start_date": "2026-12-20",
            "end_date": "2026-12-22",
        },
        {
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-12-01",
            "end_date": "2026-12-03",
            "purpose": "出差",
        },
    ]
    batch_counts = batch.save_trip_histories([dict(row) for row in rows])
    sequential_counts = [0, 0]
    for row in rows:
        before = len(sequential.get_trip_history(limit=None))
        sequential.save_trip_history(dict(row))
        created = int(len(sequential.get_trip_history(limit=None)) > before)
        sequential_counts[created == 0] += 1

    def snapshot(store):
        keys = (
            "trip_id",
            "origin",
            "destination",
            "start_date",
            "end_date",
            "purpose",
            "session_id",
        )
        return [
            {key: trip.get(key) for key in keys}
            for trip in store.get_trip_history(limit=None)
        ]

    assert batch_counts == tuple(sequential_counts), (
        batch_counts,
        sequential_counts,
    )
    assert snapshot(batch) == snapshot(sequential)
    assert (
        batch.get_statistics()["total_trips"]
        == sequential.get_statistics()["total_trips"]
    )
    assert batch.get_frequent_destinations(10) == sequential.get_frequent_destinations(10)
    print("✓ 批量保存与逐条保存结果一致")


def test_migrate_resets_counters_when_lists_cleared():
    """列表整体损坏被清空时，计数与常去目的地同步归零。"""
    storage_dir = tempfile.mkdtemp()
    path = Path(storage_dir) / "u-clear.json"
    path.write_text(
        json.dumps(
            {
                "chat_history": {},
                "trip_history": [7],
                "statistics": {
                    "total_messages": 7,
                    "total_trips": 3,
                    "frequent_destinations": {"上海": 3},
                },
            }
        ),
        encoding="utf-8",
    )
    memory = LongTermMemory("u-clear", storage_dir)
    stats = memory.get_statistics()
    assert stats["total_messages"] == 0, stats
    assert stats["total_trips"] == 0, stats
    assert stats["frequent_destinations"] == {}, stats
    print("✓ 列表清空后计数与常去目的地归零")


def test_migrate_rebuilds_statistics_when_missing():
    """统计段整体缺失（极老格式）时按现有列表重建，而不是补 0。"""
    storage_dir = tempfile.mkdtemp()
    path = Path(storage_dir) / "u-no-stats.json"
    path.write_text(
        json.dumps(
            {
                "chat_history": [
                    {"role": "user", "content": "hi", "session_id": "s"}
                ],
                "trip_history": [
                    {
                        "destination": "上海",
                        "start_date": "2026-01-01",
                        "end_date": "2026-01-02",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    memory = LongTermMemory("u-no-stats", storage_dir)
    stats = memory.get_statistics()
    assert stats["total_messages"] == 1, stats
    assert stats["total_trips"] == 1, stats
    assert stats["frequent_destinations"] == {"上海": 1}, stats
    print("✓ 统计段缺失时按列表重建")


def test_migrate_dedupes_preference_types():
    """重复 type 的偏好保留最后一条（与 get_preference 字典取最后一致）。"""
    storage_dir = tempfile.mkdtemp()
    path = Path(storage_dir) / "u-dupe-pref.json"
    path.write_text(
        json.dumps(
            {
                "preferences": [
                    {"type": "home_location", "value": "A"},
                    {"type": "home_location", "value": "B"},
                    {"type": "airlines", "value": "东航"},
                ]
            }
        ),
        encoding="utf-8",
    )
    memory = LongTermMemory("u-dupe-pref", storage_dir)
    prefs = memory.get_preference()
    assert prefs["home_location"] == "B", prefs
    assert prefs["airlines"] == "东航", prefs
    # 写路径也只保留一份：覆盖后字典里仍只有一条同类型
    memory.save_preference("home_location", "C")
    assert memory.get_preference()["home_location"] == "C"
    print("✓ 重复类型偏好迁移去重（保留最后一条）")


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
    test_trip_save_matches_linebreak_variants()
    test_delete_trips_verified_batches_single_write()
    test_delete_dirty_date_trip_keeps_frequent_destinations()
    test_pg_frequent_destinations_skip_dirty_dates()
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
    test_corrupt_pending_non_object_file_backed_up_before_write()
    test_null_list_fields_are_repaired()
    test_migrate_fills_statistics_defaults()
    test_migrate_repairs_bad_statistics_values()
    test_migrate_drops_non_object_entries()
    test_migrate_recomputes_counters_after_dropping_entries()
    test_pg_reconnects_after_connection_error()
    test_pg_schema_initialized_once_per_process()
    _mp = pytest.MonkeyPatch()
    try:
        test_pg_connect_sets_lock_timeout_and_keepalives(_mp)
    finally:
        _mp.undo()
    test_pg_schema_tolerates_concurrent_create_race()
    test_pg_failed_retry_wrapped_as_memory_error()
    test_falsey_preferences_included_in_context()
    test_path_locks_do_not_grow_unbounded()
    test_load_holds_path_lock()
    test_pg_delete_trip_deletes_single_row()
    test_pg_delete_duplicate_trip_keeps_last_copy()
    test_pg_trip_lookup_normalizes_identity_fields()
    test_pg_chat_summary_does_not_fetch_message_bodies()
    test_pg_delete_trips_verified_checks_identity_in_lock()
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
    test_pg_non_idempotent_call_sites_disable_replay()
    test_pg_operations_serialized_across_threads()
    test_pg_close_waits_for_inflight_operation()
    test_pg_add_chat_turn_locks_before_guard()
    test_pg_save_trip_histories_batches_with_one_index()
    test_json_add_chat_turn_guard_runs_under_write_lock()
    test_json_add_chat_turn_guard_sees_concurrent_delete()
    test_json_get_chat_history_excludes_session_before_limit()
    test_pg_get_chat_history_excludes_session_in_sql()
    test_bulk_save_trips_scales_and_dedupes()
    test_frequent_destinations_tie_order_is_deterministic()
    test_pg_batch_aborts_when_connection_reconnects()
    test_pg_delete_duplicate_trip_is_single_atomic_statement()
    test_pg_update_trip_aborts_when_connection_reconnects()
    test_pg_append_preference_aborts_when_connection_reconnects()
    test_pg_save_trip_aborts_when_connection_reconnects()
    test_bulk_save_trips_matches_sequential_saves()
    _mp = pytest.MonkeyPatch()
    try:
        test_pg_success_marks_actual_backend(_mp)
    finally:
        _mp.undo()
    _mp = pytest.MonkeyPatch()
    try:
        test_redis_import_failure_falls_back(_mp)
    finally:
        _mp.undo()
    _mp = pytest.MonkeyPatch()
    try:
        test_strict_error_message_redacts_dsn(_mp)
    finally:
        _mp.undo()
    test_migrate_resets_counters_when_lists_cleared()
    test_migrate_rebuilds_statistics_when_missing()
    test_migrate_dedupes_preference_types()
    print("\n全部通过")
