"""短期记忆重放时间序测试（fakeredis + 真实 Lua 脚本）。

覆盖评审确认的缺陷：回填 / 重放的历史消息此前无条件 RPUSH 到窗口尾部，
更旧的消息会把真实的最近轮次挤出窗口；现在按 timestamp 插入到对应位置。

运行：python tests/test_short_term_replay_order.py（需 fakeredis[lua]）
"""
import json
import os
import sys

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from context.short_term_memory import (
    _ADD_MESSAGES_LUA,
    applied_key,
    seen_key,
)

fakeredis = pytest.importorskip("fakeredis")


def _message(content, timestamp):
    return json.dumps(
        {
            "role": "user",
            "content": content,
            "timestamp": timestamp,
            "metadata": {},
        },
        ensure_ascii=False,
    )


def _add(client, key, payloads, window="20"):
    args = [window, "3600"]
    for payload in payloads:
        args.extend([f"op-{payload}", f"hash-{payload}", payload])
    return client.eval(
        _ADD_MESSAGES_LUA,
        3,
        key,
        applied_key(key),
        seen_key(key),
        *args,
    )


def _contents(client, key):
    return [json.loads(raw)["content"] for raw in client.lrange(key, 0, -1)]


def test_old_backfill_does_not_evict_recent_window():
    """窗口已满时回填更旧历史：旧消息被 LTRIM 掉，最近 20 条保持不变。"""
    client = fakeredis.FakeRedis(decode_responses=True)
    key = "aligo:short:v1:u:s"
    recent = [
        _message(f"recent{i}", f"2026-03-11T10:{i:02d}:00") for i in range(20)
    ]
    for payload in recent:
        client.rpush(key, payload)
    old = [
        _message(f"old{i}", f"2026-03-10T10:{i:02d}:00") for i in range(10)
    ]
    _add(client, key, old)
    contents = _contents(client, key)
    assert len(contents) == 20, contents
    assert contents == [f"recent{i}" for i in range(20)], contents
    print("✓ 旧历史回填不挤出最近窗口")


def test_backfill_inserts_in_timestamp_order():
    """窗口未满时，夹在中间的历史消息按时间插入而不是追加到尾部。"""
    client = fakeredis.FakeRedis(decode_responses=True)
    key = "aligo:short:v1:u:s"
    for index in (0, 2, 4):
        client.rpush(
            key, _message(f"m{index}", f"2026-03-11T10:0{index}:00")
        )
    _add(
        client,
        key,
        [
            _message("m1", "2026-03-11T10:01:00"),
            _message("m3", "2026-03-11T10:03:00"),
        ],
    )
    assert _contents(client, key) == ["m0", "m1", "m2", "m3", "m4"]
    print("✓ 回填按时间序插入")


def test_new_messages_append_in_order():
    """正常实时消息（时间递增）仍追加在尾部。"""
    client = fakeredis.FakeRedis(decode_responses=True)
    key = "aligo:short:v1:u:s"
    _add(
        client,
        key,
        [
            _message("a", "2026-03-11T10:00:00"),
            _message("b", "2026-03-11T10:01:00"),
        ],
    )
    assert _contents(client, key) == ["a", "b"]
    print("✓ 新消息按时间追加")


def test_missing_timestamp_falls_back_to_append():
    """时间戳缺失的消息无法排序：退回追加，保证不丢消息。"""
    client = fakeredis.FakeRedis(decode_responses=True)
    key = "aligo:short:v1:u:s"
    _add(client, key, [_message("a", "2026-03-11T10:00:00")])
    no_ts = json.dumps({"role": "user", "content": "no-ts", "metadata": {}})
    _add(client, key, [no_ts])
    assert _contents(client, key) == ["a", "no-ts"]
    print("✓ 缺时间戳退回追加")


def test_same_op_id_not_duplicated():
    """同一 op_id 重放两次只写入一次。"""
    client = fakeredis.FakeRedis(decode_responses=True)
    key = "aligo:short:v1:u:s"
    payload = _message("a", "2026-03-11T10:00:00")
    args = ["20", "3600", "same-op", "hash-a", payload]
    client.eval(
        _ADD_MESSAGES_LUA, 3, key, applied_key(key), seen_key(key), *args
    )
    client.eval(
        _ADD_MESSAGES_LUA, 3, key, applied_key(key), seen_key(key), *args
    )
    assert _contents(client, key) == ["a"]
    print("✓ 同 op_id 重放不重复")


if __name__ == "__main__":
    import _stdio

    _stdio.ensure_utf8()
    test_old_backfill_does_not_evict_recent_window()
    test_backfill_inserts_in_timestamp_order()
    test_new_messages_append_in_order()
    test_missing_timestamp_falls_back_to_append()
    test_same_op_id_not_duplicated()
    print("\n全部通过")
