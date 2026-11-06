"""
短期记忆 (Short-term Memory)
存储当前会话最近的对话历史，用于理解上下文和消歧

两种实现：
- ShortTermMemory：进程内列表（默认，零依赖）
- RedisShortTermMemory：Redis List + TTL（配置 REDIS_URL 后由存储工厂自动启用，
  可用于分布式部署的会话共享）
"""
import json
import logging
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# Redis 运行中故障的冷却：期间短期记忆回退进程内缓存（多实例共享暂时退化为
# 单实例），60 秒后自动重试；健康检查用它区分「配置了 Redis 但当前降级」
_REDIS_RETRY_COOLDOWN_SEC = 60.0
_redis_degraded_until = 0.0


def redis_short_term_degraded() -> bool:
    """短期记忆是否因 Redis 故障处于进程内回退状态。"""
    return time.monotonic() < _redis_degraded_until


def session_key(session_id: str, user_id: str = "") -> str:
    """Redis 中一个会话对应的键名。

    传入 user_id 时按用户隔离（``aligo:session:{user}:{id}:messages``），
    避免不同用户恰好复用同一 session_id 时共享上下文；不传时保留旧格式，
    仅用于清理历史遗留键。
    """
    if user_id:
        return f"aligo:session:{user_id}:{session_id}:messages"
    return f"aligo:session:{session_id}:messages"


# 原子地删除最后一轮（助手 + 用户）消息。ARGV[1] 为 "1" 时校验最后一条
# 用户提问的内容是否等于 ARGV[2]（不一致则不删除），避免与并发写入交错。
_TRUNCATE_LAST_TURN_LUA = """
local key = KEYS[1]
local marker = nil
if #KEYS >= 2 then marker = KEYS[2] end
local check = ARGV[1]
local expected = ARGV[2]
local marker_ttl = ARGV[3]
local cutoff = ARGV[4]
-- 故障重放的一次性标记：脚本已执行（含「无需删除」）但响应丢失时，重试凭
-- 标记直接返回 -1，不会把上一轮同样文本的对话再删一次。脚本在 Redis 中
-- 原子执行，标记与删除不存在「只置标记未删除」的中间态。
if marker then
  if redis.call('EXISTS', marker) == 1 then return -1 end
  if marker_ttl and marker_ttl ~= '' then
    redis.call('SET', marker, '1', 'EX', tonumber(marker_ttl))
  end
end
local n = redis.call('LLEN', key)
if n == 0 then return 0 end
local last_raw = redis.call('LINDEX', key, -1)
local last_ok, last = pcall(cjson.decode, last_raw)
local count = 1
local user_raw = nil
if last_ok and type(last) == 'table' and last['role'] == 'assistant' then
  if n >= 2 then
    local prev_raw = redis.call('LINDEX', key, -2)
    local prev_ok, prev = pcall(cjson.decode, prev_raw)
    if prev_ok and type(prev) == 'table' and prev['role'] == 'user' then
      count = 2
      user_raw = prev_raw
    end
  end
elseif last_ok and type(last) == 'table' and last['role'] == 'user' then
  user_raw = last_raw
end
if check == '1' then
  if user_raw == nil then return 0 end
  local msg_ok, msg = pcall(cjson.decode, user_raw)
  if not msg_ok or type(msg) ~= 'table' or msg['role'] ~= 'user' then return 0 end
  local content = msg['content']
  if type(content) ~= 'string' then return 0 end
  -- 重放删除只在「故障发生时的那一轮」上生效：故障期间其它实例新写入的
  -- 同文本轮次时间戳晚于 cutoff，不能误删
  local ts = msg['timestamp']
  if cutoff and cutoff ~= '' and type(ts) == 'string' and ts > cutoff then return 0 end
  -- Python 端用 str.strip()（全 Unicode 空白），Lua 的 %s 只覆盖 ASCII。
  -- 两侧必须按同一字符集裁剪两端空白，否则带全角空格的提问会被误判为
  -- 不一致（长期记忆删了、短期记忆还留着旧一轮）。多字节空白只按精确
  -- 字节序列用 string.sub 比对、逐个剥离，绝不写进字符类或加量词——
  -- 那会匹配到汉字（如「丠」= E4 B8 A0）的末尾字节，裁出非法 UTF-8；
  -- 也不能全局替换，否则提问内部的空白被改掉，与 expected 对不上。
  -- ws 与 Python str.isspace() 的多字节部分一致（0x1C-0x1F 用字符类处理）。
  local ws = {
    '\\194\\133', '\\194\\160', '\\225\\154\\128',
    '\\226\\128\\128', '\\226\\128\\129', '\\226\\128\\130',
    '\\226\\128\\131', '\\226\\128\\132', '\\226\\128\\133',
    '\\226\\128\\134', '\\226\\128\\135', '\\226\\128\\136',
    '\\226\\128\\137', '\\226\\128\\138', '\\226\\128\\168',
    '\\226\\128\\169', '\\226\\128\\175', '\\226\\129\\159',
    '\\227\\128\\128'
  }
  local function strip_ends(s)
    local prev
    repeat
      prev = s
      s = string.gsub(s, '^%s+', '')
      s = string.gsub(s, '%s+$', '')
      s = string.gsub(s, '^[\\28-\\31]+', '')
      s = string.gsub(s, '[\\28-\\31]+$', '')
      for _, w in ipairs(ws) do
        local size = #w
        if string.sub(s, 1, size) == w then
          s = string.sub(s, size + 1)
        end
        local last_at = #s
        if last_at >= size and string.sub(s, last_at - size + 1) == w then
          s = string.sub(s, 1, last_at - size)
        end
      end
    until s == prev
    return s
  end
  if strip_ends(content) ~= expected then return 0 end
end
if count >= n then
  redis.call('DEL', key)
else
  redis.call('LTRIM', key, 0, -count - 1)
end
return count
"""

# 按完整消息项（含时间戳，实际唯一）精确删除故障期间从影子缓存裁掉的
# 消息：LREM 0 删除所有完全相等的项，原子执行，不触碰其它实例写入的新
# 轮次。逐条条件式 LTRIM 只能在「要删的一轮仍是列表末尾」时生效；恢复
# 重放时其它实例可能已追加新消息，届时条件不成立就会把已删除的轮次留在
# Redis 里。
_REMOVE_MESSAGES_LUA = """
local key = KEYS[1]
for i = 1, #ARGV do
  redis.call('LREM', key, 0, ARGV[i])
end
return 1
"""


class _PendingJournal:
    """进程内按会话键共享的故障期操作日志。

    「删除最后一轮」对未命中会话缓存的会话会临时创建短期记忆对象、调用后
    立即丢弃；日志若只挂在实例上，排队的删除永远不会被重放。按 Redis 键
    共享后，同进程内任何后续的短期记忆实例都会先重放它。其它进程在故障
    期间无法得知这些变更（Redis 不可用），恢复后由本进程的下一次访问补上。
    """

    __slots__ = ("lock", "ops")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.ops: list[tuple[str, Any]] = []


_PENDING_JOURNALS: dict[str, _PendingJournal] = {}
_PENDING_JOURNALS_LOCK = threading.Lock()


@contextmanager
def _pending_journal(key: str) -> Iterator[_PendingJournal]:
    """获取会话键对应的操作日志并持有其锁（跨实例共享）。"""
    while True:
        with _PENDING_JOURNALS_LOCK:
            journal = _PENDING_JOURNALS.get(key)
            if journal is None:
                journal = _PendingJournal()
                _PENDING_JOURNALS[key] = journal
        journal.lock.acquire()
        with _PENDING_JOURNALS_LOCK:
            owned = _PENDING_JOURNALS.get(key) is journal
        if owned:
            try:
                yield journal
            finally:
                journal.lock.release()
            return
        # 取锁期间日志已被清理（只有空日志会被移除）：重新从注册表解析
        journal.lock.release()


def _drop_idle_journal(key: str, journal: _PendingJournal) -> None:
    """日志已空时从注册表移除（调用方持有 journal.lock）。"""
    with _PENDING_JOURNALS_LOCK:
        if _PENDING_JOURNALS.get(key) is journal:
            del _PENDING_JOURNALS[key]


def _message_timestamp(payload: str) -> str | None:
    """从一条消息 JSON 中取时间戳（无法解析时返回 None）。"""
    try:
        message = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(message, dict):
        return None
    timestamp = message.get("timestamp")
    return timestamp if isinstance(timestamp, str) else None


def _newest_timestamp(payloads: list[str]) -> str | None:
    """取一组消息 JSON 中最新的时间戳。"""
    newest = None
    for payload in payloads:
        timestamp = _message_timestamp(payload)
        if timestamp is not None and (newest is None or timestamp > newest):
            newest = timestamp
    return newest


class ShortTermMemory:
    """
    短期记忆：存储最近的对话历史
    - 存储最近 5-10 轮对话
    - 自动淘汰旧消息
    - 用于上下文理解
    """

    def __init__(self, max_turns: int = 10):
        """
        初始化短期记忆

        Args:
            max_turns: 最大保存轮数（一轮 = 一对用户-助手消息）
        """
        self.max_turns = max_turns
        self.messages: list[dict[str, Any]] = []

    def add_message(self, role: str, content: str, metadata: dict | None = None):
        """
        添加消息到短期记忆

        Args:
            role: 角色 (user/assistant)
            content: 消息内容
            metadata: 额外的元数据
        """
        message = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            "metadata": metadata or {}
        }
        self._append_message(message)
        logger.debug(f"Added message to short-term memory: {role}")

    def _append_message(self, message: dict[str, Any]) -> None:
        """追加一条已成形的消息（Redis 实现复用同一对象，保证两侧内容一致）。"""
        self.messages.append(message)

        # 自动淘汰旧消息（保持 max_turns 轮对话）
        # 每轮 = 2条消息（用户 + 助手）
        max_messages = self.max_turns * 2
        if len(self.messages) > max_messages:
            self.messages = self.messages[-max_messages:]

    def get_recent_context(self, n_turns: int | None = None) -> list[dict[str, Any]]:
        """
        获取最近 n 轮对话

        Args:
            n_turns: 获取轮数，默认为全部

        Returns:
            最近的消息列表
        """
        if n_turns is None:
            return self.messages.copy()

        # n轮 = 2n条消息
        n_messages = n_turns * 2
        return self.messages[-n_messages:] if len(self.messages) > n_messages else self.messages.copy()

    def get_context_string(self, n_turns: int = 5) -> str:
        """
        获取最近对话的字符串表示

        Args:
            n_turns: 获取轮数

        Returns:
            格式化的对话字符串
        """
        messages = self.get_recent_context(n_turns)
        if not messages:
            return "无历史对话"

        lines = []
        for msg in messages:
            role_name = "用户" if msg["role"] == "user" else "助手"
            lines.append(f"{role_name}: {msg['content']}")

        return "\n".join(lines)

    def clear(self):
        """清空短期记忆"""
        self.messages = []
        logger.info("Short-term memory cleared")

    def truncate_last_turn(self, expect: str | None = None) -> int:
        """删除最后一轮对话（助手 + 提问的用户），返回删除条数。

        历史以用户提问结尾（例如回答过程中被停止）时只删除该条提问。
        ``expect`` 不为 None 时先校验最后一条用户提问的内容，不一致则不做修改
        （例如这条提问并未真正写入）。
        """
        if not self.messages:
            return 0
        count = 1
        last_user = self.messages[-1] if self.messages[-1].get("role") == "user" else None
        if (
            len(self.messages) >= 2
            and self.messages[-1].get("role") == "assistant"
            and self.messages[-2].get("role") == "user"
        ):
            count = 2
            last_user = self.messages[-2]
        if expect is not None:
            if last_user is None:
                return 0
            if str(last_user.get("content", "")).strip() != expect.strip():
                logger.info("Skip truncating last turn: content mismatch")
                return 0
        del self.messages[-count:]
        logger.info("Truncated last turn from short-term memory (%s messages)", count)
        return count

    def get_statistics(self) -> dict[str, Any]:
        """获取统计信息"""
        return {
            "total_messages": len(self.messages),
            "max_turns": self.max_turns,
            "oldest_message_time": self.messages[0]["timestamp"] if self.messages else None,
            "newest_message_time": self.messages[-1]["timestamp"] if self.messages else None
        }


class RedisShortTermMemory:
    """基于 Redis 的短期记忆。

    - 每条消息以 JSON 存入 Redis List：``aligo:session:{session_id}:messages``
    - 滑动窗口：LTRIM 只保留最近 ``max_turns * 2`` 条消息
    - TTL：默认 1 小时（``REDIS_TTL_SEC``），会话闲置后自动过期
    """

    def __init__(
        self,
        client: Any,
        session_id: str,
        max_turns: int = 10,
        ttl_sec: int = 3600,
        user_id: str = "",
    ) -> None:
        self.client = client
        self.session_id = session_id
        self.max_turns = max_turns
        self.ttl_sec = ttl_sec
        self.user_id = user_id
        self.key = session_key(session_id, user_id)
        # Redis 故障时的进程内影子缓存：对话继续可用（跨实例共享暂时失效）。
        # 成功的写入也镜像一份，故障切换时上下文与删除操作仍然正确。
        self._fallback = ShortTermMemory(max_turns=max_turns)
        # 故障期变更记入按会话键共享的操作日志（见 _PendingJournal）：
        # ("add", JSON 项) / ("truncate", (expect, op_id, cutoff)) /
        # ("remove", [JSON 项]) / ("clear", None)，恢复后按发生顺序重放。
        # 只补写新增会在恢复后复活已删除的轮次；按影子缓存整键重写会丢掉
        # 其它实例在故障期间写入、而本对象从未见过的消息。

    @property
    def _pending_ops(self) -> list[tuple[str, Any]]:
        """当前会话键待重放操作的快照（调试与测试用，不参与业务逻辑）。"""
        with _PENDING_JOURNALS_LOCK:
            journal = _PENDING_JOURNALS.get(self.key)
        return list(journal.ops) if journal is not None else []

    def _queue_op(self, kind: str, payload: Any = None) -> None:
        """记录一条故障期操作，并把日志限制在有界范围内。"""
        with _pending_journal(self.key) as journal:
            if kind == "clear":
                # 清空会覆盖之前所有变更，旧操作不再需要重放
                journal.ops.clear()
            journal.ops.append((kind, payload))
            self._trim_journal(journal.ops)

    def _trim_journal(self, ops: list[tuple[str, Any]]) -> None:
        """日志有界：新增项与删除项分别限额，互不挤占。"""
        add_cap = max(64, self.max_turns * 8)
        add_indexes = [i for i, (kind, _) in enumerate(ops) if kind == "add"]
        excess = len(add_indexes) - add_cap
        if excess > 0:
            # 只丢最旧的新增项：恢复后窗口内容仍与全程在线写入一致
            for index in reversed(add_indexes[:excess]):
                del ops[index]
        delete_cap = add_cap * 4
        delete_indexes = [i for i, (kind, _) in enumerate(ops) if kind != "add"]
        excess = len(delete_indexes) - delete_cap
        if excess > 0:
            logger.warning(
                "短期记忆故障操作日志过多，丢弃 %s 条最早的删除操作", excess
            )
            for index in reversed(delete_indexes[:excess]):
                del ops[index]

    def _replay_pending(self) -> None:
        """Redis 恢复后的第一次调用：按顺序重放故障期间的变更。

        每条操作成功后立即从日志移除：重放中途再次失败时，已完成的部分
        不会重复执行。新增项按完整消息（含时间戳）与 Redis 已有内容去重，
        覆盖「写入已在服务端生效但响应丢失」的至少一次语义；截断类操作带
        一次性标记与故障时刻 cutoff，重试与故障期间新写入的同文本轮次都
        不会被误删。
        """
        with _pending_journal(self.key) as journal:
            if not journal.ops:
                _drop_idle_journal(self.key, journal)
                return
            try:
                self._replay_ops(journal.ops)
            finally:
                if not journal.ops:
                    _drop_idle_journal(self.key, journal)

    def _replay_ops(self, ops: list[tuple[str, Any]]) -> None:
        window = self.max_turns * 2
        seen: set[str] | None = None
        newest: str | None = None
        while ops:
            kind, payload = ops[0]
            if kind == "add":
                if seen is None:
                    existing = self.client.lrange(self.key, -window, -1) or []
                    seen = set(existing)
                    newest = _newest_timestamp(existing)
                # 连续新增合并为一次 pipeline：故障期可能积压几十条，
                # 逐条 MULTI/EXEC 会把恢复后的首次调用拖成几十次往返
                batch: list[str] = []
                consumed = 0
                for op_kind, op_payload in ops:
                    if op_kind != "add" or consumed >= 100:
                        break
                    consumed += 1
                    if op_payload in seen:
                        continue
                    timestamp = _message_timestamp(op_payload)
                    if (
                        timestamp is not None
                        and newest is not None
                        and timestamp < newest
                    ):
                        # 已在 Redis 生效、又被其它实例的新消息挤出窗口：
                        # 再追加会以旧消息身份挤掉更新的内容
                        logger.debug("短期记忆重放跳过已过期的故障期消息")
                        continue
                    batch.append(op_payload)
                if batch:
                    pipe = self.client.pipeline()
                    pipe.rpush(self.key, *batch)
                    pipe.ltrim(self.key, -window, -1)
                    pipe.expire(self.key, self.ttl_sec)
                    pipe.execute()
                    seen.update(batch)
                    batch_newest = _newest_timestamp(batch)
                    if batch_newest is not None and (
                        newest is None or batch_newest > newest
                    ):
                        newest = batch_newest
                del ops[:consumed]
                continue
            if kind == "truncate":
                expect, op_id, cutoff = payload
                result = int(
                    self.client.eval(
                        _TRUNCATE_LAST_TURN_LUA,
                        2,
                        self.key,
                        f"{self.key}:op:{op_id}",
                        "1" if expect is not None else "0",
                        (expect or "").strip(),
                        str(self.ttl_sec),
                        cutoff or "",
                    )
                    or 0
                )
                if result == 0:
                    logger.warning(
                        "短期记忆故障期截断重放未匹配（列表末尾已变化），已跳过：%s",
                        self.key,
                    )
                ops.pop(0)
            elif kind == "remove":
                if payload:
                    self.client.eval(_REMOVE_MESSAGES_LUA, 1, self.key, *payload)
                ops.pop(0)
            else:  # clear
                self.client.delete(self.key)
                ops.pop(0)
            seen = None
            newest = None

    def _call_redis(self, operation, fallback):
        """执行 Redis 操作；返回 (结果, 是否走了 Redis)，故障时回退影子缓存。"""
        global _redis_degraded_until
        if time.monotonic() < _redis_degraded_until:
            return fallback(), False
        try:
            # 恢复后的第一次 Redis 调用先重放故障期间的变更，
            # 否则刚恢复时读到的上下文会缺少最近几轮或复活已删除轮次
            self._replay_pending()
            result = operation()
        except Exception as exc:  # noqa: BLE001 - 短期缓存故障不能中断对话
            _redis_degraded_until = time.monotonic() + _REDIS_RETRY_COOLDOWN_SEC
            logger.warning(
                "Redis 短期记忆不可用（%s），本进程回退内存缓存 %ss",
                exc,
                int(_REDIS_RETRY_COOLDOWN_SEC),
            )
            return fallback(), False
        _redis_degraded_until = 0.0
        return result, True

    def _redis(self, operation, fallback):
        """执行 Redis 操作；连接级故障时回退影子缓存并进入 60 秒冷却。"""
        return self._call_redis(operation, fallback)[0]

    def add_message(self, role: str, content: str, metadata: dict | None = None) -> None:
        message = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            "metadata": metadata or {},
        }
        payload = json.dumps(message, ensure_ascii=False)
        # 影子缓存复用同一个消息对象：时间戳与 Redis 中的 JSON 完全一致，
        # 故障期截断时才能按完整消息项精确删除 Redis 里的对应项
        self._fallback._append_message(message)

        def op() -> None:
            pipe = self.client.pipeline()
            pipe.rpush(self.key, payload)
            pipe.ltrim(self.key, -self.max_turns * 2, -1)
            pipe.expire(self.key, self.ttl_sec)
            pipe.execute()

        _, via_redis = self._call_redis(op, lambda: None)
        if not via_redis:
            # 故障期间的消息记入操作日志，Redis 恢复后按顺序补写
            self._queue_op("add", payload)
        logger.debug("Added message to Redis short-term memory: %s", role)

    def get_recent_context(self, n_turns: int | None = None) -> list[dict[str, Any]]:
        def op() -> list[dict[str, Any]]:
            if n_turns is None:
                raw = self.client.lrange(self.key, 0, -1)
            else:
                raw = self.client.lrange(self.key, -n_turns * 2, -1)
            return [json.loads(item) for item in raw]

        return self._redis(op, lambda: self._fallback.get_recent_context(n_turns))

    def get_context_string(self, n_turns: int = 5) -> str:
        messages = self.get_recent_context(n_turns)
        if not messages:
            return "无历史对话"
        lines = []
        for message in messages:
            role_name = "用户" if message["role"] == "user" else "助手"
            lines.append(f"{role_name}: {message['content']}")
        return "\n".join(lines)

    def clear(self) -> None:
        _, via_redis = self._call_redis(
            lambda: self.client.delete(self.key), lambda: None
        )
        if not via_redis:
            # 故障期间删不掉 Redis 里的旧内容：恢复后重放一次删除（该操作
            # 会覆盖日志中更早的变更，_queue_op 已按此合并）
            self._queue_op("clear")
        # 无论 Redis 是否可用都清空影子缓存，否则故障切换后
        # 已清空的对话会从内存里“复活”
        self._fallback.clear()
        logger.info("Redis short-term memory cleared: %s", self.key)

    def truncate_last_turn(self, expect: str | None = None) -> int:
        """删除 Redis 中最后一轮对话，返回删除条数（保留的条目 TTL 不变）。

        使用 Lua 脚本把「读取最后两条 → 判断 → 裁剪」放在一次原子调用里，
        避免与其它进程 / worker 追加消息的写入交错后裁错条目；``expect`` 不为
        None 时内容不一致则不修改。
        """
        removed: list[dict[str, Any]] = []

        def fallback() -> int:
            tail = self._fallback.messages[-2:]
            count = self._fallback.truncate_last_turn(expect)
            if count:
                # 记录从影子缓存删除的完整消息，恢复后按项精确删除
                removed[:] = tail[len(tail) - count:]
            return count

        raw, via_redis = self._call_redis(
            lambda: self.client.eval(
                _TRUNCATE_LAST_TURN_LUA,
                1,
                self.key,
                "1" if expect is not None else "0",
                (expect or "").strip(),
            ),
            fallback,
        )
        count = int(raw or 0)
        if via_redis and count:
            # Redis 删除成功时同步镜像缓存，避免 Redis 再故障时把已删除的
            # 一轮从影子缓存读回来
            self._fallback.truncate_last_turn(expect)
        if not via_redis:
            if removed:
                # 故障期间在影子缓存里删掉了这一轮：恢复后按完整消息项精确
                # 删除。不能用「删除列表末尾一轮」——重放发生在本进程冷却
                # 结束后的下一次调用，期间其它实例可能已追加新消息，末尾
                # 已不是这一轮，条件删除会失效、把已删除的轮次留在 Redis。
                # 影子缓存可能没见过其它实例写入的更新轮次：此处只删本对象
                # 确实写入过的消息，宁可少删也不误删别人的轮次。
                self._queue_op(
                    "remove", [json.dumps(m, ensure_ascii=False) for m in removed]
                )
            elif expect is not None:
                # 影子缓存里没有这一轮（临时对象 / 重启后未命中缓存），
                # 但调用方给出了内容校验：恢复后仍可条件式删除——只在
                # Redis 末尾仍是该提问、且时间戳不晚于本次故障期删除
                # （cutoff）时生效；一次性标记保证响应丢失重试不会删两次
                self._queue_op(
                    "truncate",
                    (expect, uuid.uuid4().hex, datetime.now().isoformat()),
                )
        logger.info(
            "Truncated last turn from Redis memory: %s (%s messages)", self.key, count
        )
        return count

    def get_statistics(self) -> dict[str, Any]:
        def op() -> dict[str, Any]:
            total = int(self.client.llen(self.key) or 0)
            oldest = newest = None
            if total:
                try:
                    first = self.client.lindex(self.key, 0)
                    last = self.client.lindex(self.key, -1)
                    oldest = json.loads(first).get("timestamp") if first else None
                    newest = json.loads(last).get("timestamp") if last else None
                except (TypeError, json.JSONDecodeError):
                    pass
            return {
                "total_messages": total,
                "max_turns": self.max_turns,
                "oldest_message_time": oldest,
                "newest_message_time": newest,
            }

        return self._redis(op, self._fallback.get_statistics)
