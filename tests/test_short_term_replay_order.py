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
    tomb_key,
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
        4,
        key,
        applied_key(key),
        seen_key(key),
        tomb_key(key),
        *args,
    )


def _truncate_count(raw):
    """截断脚本返回 ``{count, deleted}`` JSON；非 JSON 时是 -1 / 0。"""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str) and raw.strip().startswith("{"):
        return json.loads(raw)["count"]
    return int(raw or 0)


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
        _ADD_MESSAGES_LUA,
        4,
        key,
        applied_key(key),
        seen_key(key),
        tomb_key(key),
        *args,
    )
    client.eval(
        _ADD_MESSAGES_LUA,
        4,
        key,
        applied_key(key),
        seen_key(key),
        tomb_key(key),
        *args,
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
        return _truncate_count(
            client.eval(
                _TRUNCATE_LAST_TURN_LUA,
                1,
                key,
                "1",
                content,
                "",
                "",
                turn_id,
            )
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


def test_truncate_records_tombstones_and_clears_seen():
    """截断删空：seen 清（回填恢复窗口裁掉的更早轮次）、tomb 写墓碑。

    实现在 ``RedisShortTermMemory.truncate_last_turn``：删除明细写进
    ``:tomb`` 后，重放 / 回填同内容的已删除消息被墓碑拦截。
    """
    from context.short_term_memory import (
        RedisShortTermMemory,
        message_content_id,
    )

    client = fakeredis.FakeRedis(decode_responses=True)
    memory = RedisShortTermMemory(client, "s1", max_turns=5, user_id="u1")
    memory.add_message("user", "Q")
    key = memory.key
    assert client.scard(seen_key(key)) == 1
    stored = client.lindex(key, 0)

    assert memory.truncate_last_turn("Q") == 1
    assert client.exists(key) == 0
    assert client.scard(seen_key(key)) == 0, (
        "截断删空必须清 seen：回填要能恢复被窗口裁掉（而非被删除）的更早轮次"
    )
    assert client.scard(tomb_key(key)) == 1, "被删除消息的指纹写入 :tomb 墓碑"

    # 同一内容重放（长期记忆回填 / 故障重放）：被墓碑拦截，不复活
    message = json.loads(stored)
    client.eval(
        _ADD_MESSAGES_LUA,
        4,
        key,
        applied_key(key),
        seen_key(key),
        tomb_key(key),
        "20",
        "3600",
        "op-replay",
        message_content_id(message),
        "ordered",
        stored,
    )
    assert client.llen(key) == 0, "已删除的轮次不能被重放复活"

    # 对照：未删除、只是被窗口裁掉的更早消息仍可由回填恢复
    _add(client, key, [_message("older", "2026-03-10T10:00:00")])
    assert _contents(client, key) == ["older"]
    print("✓ 截断删空清 seen、写墓碑；重放不复活且回填仍可用")


def test_truncate_marker_returns_deleted_details_for_replay():
    """响应丢失后的重放：命中 :op 标记返回删除明细，供调用方补写墓碑。"""
    from context.short_term_memory import _TRUNCATE_LAST_TURN_LUA

    client = fakeredis.FakeRedis(decode_responses=True)
    key = "aligo:short:v1:u:s"
    marker = f"{key}:op:lost"
    payload = _message("Q", "2026-03-11T10:00:00")
    client.rpush(key, payload)
    first = client.eval(
        _TRUNCATE_LAST_TURN_LUA, 2, key, marker, "0", "", "600", "", "", "", "3600"
    )
    assert _truncate_count(first) == 1
    assert client.exists(key) == 0
    # 标记本身只存计数：已删除的用户内容不随 30 天标记长期滞留
    marker_value = json.loads(client.get(marker))
    assert marker_value["count"] == 1 and "deleted" not in marker_value
    assert int(client.ttl(f"{marker}:del")) > 0
    second = client.eval(
        _TRUNCATE_LAST_TURN_LUA, 2, key, marker, "0", "", "600", "", "", "", "3600"
    )
    data = json.loads(second)
    assert data["count"] == 1 and data["deleted"] == [payload], data
    print("✓ 截断标记存删除明细，重放可取回补写墓碑")


def test_clear_before_and_remove_scripts_execute():
    """_CLEAR_BEFORE_LUA / _REMOVE_MESSAGES_LUA 在真实 Lua 下执行并清 seen。"""
    from context.short_term_memory import (
        _CLEAR_BEFORE_LUA,
        _REMOVE_MESSAGES_LUA,
    )

    client = fakeredis.FakeRedis(decode_responses=True)
    key = "aligo:short:v1:u:s"
    seen = seen_key(key)
    tomb = tomb_key(key)
    old = _message("old", "2026-03-11T09:00:00")
    new = _message("new", "2026-03-11T11:00:00")
    client.rpush(key, old)
    client.rpush(key, new)
    client.sadd(seen, "hash-old", "hash-new")
    removed = client.eval(
        _CLEAR_BEFORE_LUA, 3, key, seen, tomb, "2026-03-11T10:00:00"
    )
    assert removed == 1
    assert _contents(client, key) == ["new"]
    assert client.scard(seen) == 2, "仍有存活消息时保留 seen（重放不重复写入）"

    # 空格分隔且晚于 cutoff 的消息：归一后比较，迟到的清空重放不误删
    client.rpush(key, _message("later", "2026-03-11 12:00:00"))
    assert (
        client.eval(_CLEAR_BEFORE_LUA, 3, key, seen, tomb, "2026-03-11T10:00:00")
        == 0
    )
    assert _contents(client, key) == ["new", "later"]
    client.lrem(key, 0, _message("later", "2026-03-11 12:00:00"))

    # 删空整表：seen 与 tomb 一并清掉（清空后旧历史应由回填恢复）
    client.sadd(tomb, "hash-deleted")
    assert (
        client.eval(_CLEAR_BEFORE_LUA, 3, key, seen, tomb, "2026-03-12T00:00:00")
        == 1
    )
    assert client.exists(key) == 0
    assert client.exists(seen) == 0
    assert client.exists(tomb) == 0

    # cutoff 为空串：兼容旧日志的整键清空，seen / tomb 同样清
    client.rpush(key, old)
    client.sadd(seen, "hash-old")
    client.sadd(tomb, "hash-deleted")
    assert client.eval(_CLEAR_BEFORE_LUA, 3, key, seen, tomb, "") == 1
    assert client.exists(key) == 0 and client.exists(seen) == 0
    assert client.exists(tomb) == 0

    # 按完整消息项精确删除（影子缓存镜像）：删空时清 seen，已删除内容
    # 由调用方写入 :tomb 墓碑（见 RedisShortTermMemory 的 remove 重放路径）
    client.rpush(key, old)
    client.rpush(key, new)
    client.sadd(seen, "hash-old", "hash-new")
    client.eval(_REMOVE_MESSAGES_LUA, 2, key, seen, old)
    assert _contents(client, key) == ["new"]
    assert client.scard(seen) == 2, "仍有存活消息时保留 seen"
    client.eval(_REMOVE_MESSAGES_LUA, 2, key, seen, new)
    assert client.exists(key) == 0
    assert client.exists(seen) == 0, "删空后 seen 清（回填可恢复更早轮次）"
    print("✓ 条件清空清 seen+tomb / 精确删除删空清 seen")


def test_truncate_cutoff_skips_identified_turn():
    """未来时间戳：无 / 不匹配回合 id 时 cutoff 拒删；完全匹配的回合 id
    是精确身份、跳过 cutoff（时钟超前不挡住显式删除）；空格分隔时间戳
    归一后比较。"""
    from context.short_term_memory import _TRUNCATE_LAST_TURN_LUA

    client = fakeredis.FakeRedis(decode_responses=True)
    key = "aligo:short:v1:u:s"
    cutoff = "2026-11-24T10:00:00"
    future = "2099-01-01T00:00:00"

    def push_turn(timestamp):
        client.rpush(
            key,
            json.dumps(
                {
                    "role": "user",
                    "content": "Q",
                    "timestamp": timestamp,
                    "metadata": {"client_turn_id": "t1"},
                },
                ensure_ascii=False,
            ),
        )
        client.rpush(
            key,
            json.dumps(
                {
                    "role": "assistant",
                    "content": "A",
                    "timestamp": timestamp,
                    "metadata": {},
                },
                ensure_ascii=False,
            ),
        )

    push_turn(future)
    assert (
        _truncate_count(
            client.eval(
                _TRUNCATE_LAST_TURN_LUA, 1, key, "1", "Q", "", cutoff, ""
            )
        )
        == 0
    ), "无回合 id 时未来时间戳应被 cutoff 拒删"
    assert (
        _truncate_count(
            client.eval(
                _TRUNCATE_LAST_TURN_LUA, 1, key, "1", "Q", "", cutoff, "t-other"
            )
        )
        == 0
    ), "回合 id 不匹配时拒删"
    assert (
        _truncate_count(
            client.eval(
                _TRUNCATE_LAST_TURN_LUA, 1, key, "1", "Q", "", cutoff, "t1"
            )
        )
        == 2
    ), "完全匹配的回合 id 是精确身份，跳过 cutoff"
    assert client.llen(key) == 0

    # 空格分隔的未来时间戳：归一后比较仍判未来（未修前原始比较会误判过去）
    push_turn("2099-01-01 00:00:00")
    _truncate_count(
        client.eval(
            _TRUNCATE_LAST_TURN_LUA, 1, key, "1", "Q", "", cutoff, ""
        )
    )
    assert client.llen(key) == 2, "空格分隔时间戳必须归一并按未来拒删"
    print("✓ 截断 cutoff 与回合 id 精确身份的优先级（含时间戳归一）")


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
    test_truncate_records_tombstones_and_clears_seen()
    test_truncate_marker_returns_deleted_details_for_replay()
    test_clear_before_and_remove_scripts_execute()
    test_truncate_cutoff_skips_identified_turn()
    print("\n全部通过")
