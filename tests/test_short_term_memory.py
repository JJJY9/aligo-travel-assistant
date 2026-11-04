"""
短期记忆（进程内实现）测试（无需 Redis / API Key）

运行：python tests/test_short_term_memory.py
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from context.short_term_memory import ShortTermMemory


def _seeded() -> ShortTermMemory:
    memory = ShortTermMemory()
    for role, content in [
        ("user", "问题 1"),
        ("assistant", "回答 1"),
        ("user", "问题 2"),
        ("assistant", "回答 2"),
    ]:
        memory.add_message(role, content)
    return memory


def test_truncate_pair():
    memory = _seeded()
    assert memory.truncate_last_turn() == 2
    assert [item["content"] for item in memory.messages] == ["问题 1", "回答 1"]
    print("✓ 删除最后一轮（成对）")


def test_truncate_expect_mismatch_keeps_history():
    memory = _seeded()
    assert memory.truncate_last_turn("其它问题") == 0
    assert len(memory.messages) == 4, "内容不一致时不应删除任何消息"
    print("✓ expect 不一致时保留历史")


def test_truncate_trailing_user_only():
    memory = ShortTermMemory()
    memory.add_message("user", "只有提问")
    assert memory.truncate_last_turn("只有提问") == 1
    assert memory.messages == []
    print("✓ 只剩提问时只删除提问")


def test_truncate_empty():
    assert ShortTermMemory().truncate_last_turn() == 0
    print("✓ 空历史返回 0")


if __name__ == "__main__":
    test_truncate_pair()
    test_truncate_expect_mismatch_keeps_history()
    test_truncate_trailing_user_only()
    test_truncate_empty()
    print("\n全部通过")
