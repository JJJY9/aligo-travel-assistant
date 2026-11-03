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


if __name__ == "__main__":
    test_defaults_use_local_backends()
    test_bad_redis_falls_back()
    test_bad_postgres_falls_back()
    test_memory_manager_uses_factory()
    test_long_term_delete_session()
    test_long_term_delete_preference()
    test_session_titles()
    test_session_pinned()
    test_summary_cache_avoids_repeated_calls()
    test_reload_long_term_respects_backend()
    print("\n全部通过")
