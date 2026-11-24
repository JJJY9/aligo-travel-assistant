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


def _add(client, key, payloads, window="20", mode="ordered"):
    args = [window, "3600"]
    for payload in payloads:
        args.extend([f"op-{payload}", f"hash-{payload}", mode, payload])
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
    args = ["20", "3600", "same-op", "hash-a", "ordered", payload]
    client.eval(
        _ADD_MESSAGES_LUA, 3, key, applied_key(key), seen_key(key), *args
    )
    client.eval(
        _ADD_MESSAGES_LUA, 3, key, applied_key(key), seen_key(key), *args
    )
    assert _contents(client, key) == ["a"]
    print("✓ 同 op_id 重放不重复")


def test_same_second_backfill_keeps_microsecond_order():
    """同一秒内的回填消息按微秒排序（旧实现截到秒后无法区分）。"""
    client = fakeredis.FakeRedis(decode_responses=True)
    key = "aligo:short:v1:u:s"
    for index in (0, 2):
        client.rpush(
            key,
            _message(f"m{index}", f"2026-03-11T10:00:00.{index}00000"),
        )
    _add(client, key, [_message("m1", "2026-03-11T10:00:00.100000")])
    assert _contents(client, key) == ["m0", "m1", "m2"]
    print("✓ 同秒回填按微秒顺序插入")


def test_space_separator_timestamp_is_normalized():
    """空格分隔的时间戳按 'T' 归一后再比较，不会插到更早的位置。"""
    client = fakeredis.FakeRedis(decode_responses=True)
    key = "aligo:short:v1:u:s"
    client.rpush(key, _message("a", "2026-03-11T10:00:30"))
    _add(client, key, [_message("b", "2026-03-11 10:00:20")])
    assert _contents(client, key) == ["b", "a"]
    print("✓ 空格分隔时间戳归一后排序")


def test_untimestamped_old_entry_does_not_force_append():
    """旧条目缺时间戳时跳过继续找锚点，不能把回填历史追加到窗口尾部。"""
    client = fakeredis.FakeRedis(decode_responses=True)
    key = "aligo:short:v1:u:s"
    client.rpush(
        key, json.dumps({"role": "user", "content": "no-ts", "metadata": {}})
    )
    client.rpush(key, _message("recent1", "2026-03-11T10:00:00"))
    client.rpush(key, _message("recent2", "2026-03-11T10:01:00"))
    _add(client, key, [_message("old", "2026-03-10T09:00:00")])
    assert _contents(client, key) == ["old", "no-ts", "recent1", "recent2"]
    print("✓ 旧条目缺时间戳不再强制追加")


def test_live_append_mode_ignores_clock_skew():
    """实时消息走 append：时间戳偏旧也不会被插到头部后遭 LTRIM 丢掉。"""
    client = fakeredis.FakeRedis(decode_responses=True)
    key = "aligo:short:v1:u:s"
    client.rpush(key, _message("new1", "2026-03-11T10:01:00"))
    client.rpush(key, _message("new2", "2026-03-11T10:02:00"))
    _add(
        client,
        key,
        [_message("live-skewed", "2026-03-11T09:00:00")],
        window="3",
        mode="append",
    )
    contents = _contents(client, key)
    assert contents[-1] == "live-skewed", contents
    assert "live-skewed" in contents, "时钟回拨的实时消息不能被 LTRIM 丢掉"
    print("✓ 实时追加不受时钟回拨影响")


def test_redis_truncate_respects_turn_id():
    """Lua 截断按 metadata.client_turn_id 匹配；缺 id / 空串退回内容校验。"""
    from context.short_term_memory import _TRUNCATE_LAST_TURN_LUA

    client = fakeredis.FakeRedis(decode_responses=True)
    key = "aligo:short:v1:u:s"

    def push(content, turn_id):
        metadata = {"client_turn_id": turn_id} if turn_id else {}
        client.rpush(
            key,
            json.dumps(
                {
                    "role": "user",
                    "content": content,
                    "timestamp": "2026-03-11T10:00:00",
                    "metadata": metadata,
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "role": "assistant",
                    "content": "回答",
                    "timestamp": "2026-03-11T10:00:01",
                    "metadata": {},
                },
                ensure_ascii=False,
            ),
        )

    def truncate(content, turn_id):
        return client.eval(
            _TRUNCATE_LAST_TURN_LUA,
            1,
            key,
            "1",
            content,
            "",
            "",
            turn_id,
        )

    push("同一句提问", "t-other")
    assert truncate("同一句提问", "t-mine") == 0
    assert client.llen(key) == 2, "回合 id 不一致时不应删除"
    assert truncate("同一句提问", "t-other") == 2
    assert client.llen(key) == 0

    # 消息没有回合 id（长期记忆回填 / 旧数据）：退回内容匹配，不能拒删
    push("回填提问", None)
    assert truncate("回填提问", "t-mine") == 2
    assert client.llen(key) == 0

    # 空串 turn_id 视为「无回合 id」：与进程内实现口径一致
    push("空 id 提问", "t-other")
    assert truncate("空 id 提问", "") == 2
    assert client.llen(key) == 0
    print("✓ Redis 截断按回合 id 精确匹配（缺 id / 空串退回内容校验）")


def test_truncate_clears_seen_fingerprints_on_full_delete():
    """截断删空整键时一并清 seen 指纹：回填 / 重放的同批历史不被永久跳过。"""
    from context.short_term_memory import _TRUNCATE_LAST_TURN_LUA

    client = fakeredis.FakeRedis(decode_responses=True)
    key = "aligo:short:v1:u:s"
    payload = _message("Q", "2026-03-11T10:00:00")
    _add(client, key, [payload])
    assert client.scard(seen_key(key)) == 1

    count = client.eval(
        _TRUNCATE_LAST_TURN_LUA,
        1,
        key,
        "0",
        "",
        "",
        "",
        "",
        seen_key(key),
    )
    assert count == 1
    assert client.exists(key) == 0
    assert client.exists(seen_key(key)) == 0, "seen 指纹必须随消息键一起清"

    # 清空后同一内容重新回填（op_id 不同）：不能被残留 seen 跳过
    args = ["20", "3600", "op-new", "hash-new", "ordered", payload]
    client.eval(
        _ADD_MESSAGES_LUA, 3, key, applied_key(key), seen_key(key), *args
    )
    assert _contents(client, key) == ["Q"]
    print("✓ 截断整键删除同时清 seen 指纹，回填不被跳过")


def test_clear_before_and_remove_scripts_execute():
    """_CLEAR_BEFORE_LUA / _REMOVE_MESSAGES_LUA 在真实 Lua 解释器下可执行。"""
    from context.short_term_memory import (
        _CLEAR_BEFORE_LUA,
        _REMOVE_MESSAGES_LUA,
    )

    client = fakeredis.FakeRedis(decode_responses=True)
    key = "aligo:short:v1:u:s"
    old = _message("old", "2026-03-11T09:00:00")
    new = _message("new", "2026-03-11T11:00:00")
    client.rpush(key, old)
    client.rpush(key, new)
    removed = client.eval(_CLEAR_BEFORE_LUA, 1, key, "2026-03-11T10:00:00")
    assert removed == 1
    assert _contents(client, key) == ["new"]
    # cutoff 为空串：兼容旧日志的整键清空
    assert client.eval(_CLEAR_BEFORE_LUA, 1, key, "") == 1
    assert client.exists(key) == 0
    # 按完整消息项精确删除（影子缓存镜像）
    client.rpush(key, old)
    client.rpush(key, new)
    client.eval(_REMOVE_MESSAGES_LUA, 1, key, old)
    assert _contents(client, key) == ["new"]
    print("✓ 条件清空 / 精确删除脚本在真实 Lua 下执行")


if __name__ == "__main__":
    import _stdio

    _stdio.ensure_utf8()
    test_old_backfill_does_not_evict_recent_window()
    test_backfill_inserts_in_timestamp_order()
    test_new_messages_append_in_order()
    test_missing_timestamp_falls_back_to_append()
    test_same_op_id_not_duplicated()
    test_same_second_backfill_keeps_microsecond_order()
    test_space_separator_timestamp_is_normalized()
    test_untimestamped_old_entry_does_not_force_append()
    test_live_append_mode_ignores_clock_skew()
    test_redis_truncate_respects_turn_id()
    test_truncate_clears_seen_fingerprints_on_full_delete()
    test_clear_before_and_remove_scripts_execute()
    print("\n全部通过")
