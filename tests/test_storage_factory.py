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


if __name__ == "__main__":
    test_defaults_use_local_backends()
    test_bad_redis_falls_back()
    test_bad_postgres_falls_back()
    test_memory_manager_uses_factory()
    print("\n全部通过")
