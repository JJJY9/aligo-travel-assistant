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

# 故障期「整键删除」操作的有效期：会话墓碑只保留 300 秒，超过这个窗口
# 后 session_id 可能已被合法复用，迟到的清空会把新会话的对话删掉
_CLEAR_OP_MAX_AGE_SEC = 600.0


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


def applied_key(key: str) -> str:
    """会话消息键对应的「已追加 op_id」集合键（幂等重放用，TTL 同消息）。"""
    return f"{key}:applied"


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
  -- 同文本轮次时间戳晚于 cutoff，不能误删。cutoff 与消息时间戳都是各实例
  -- 本地时钟的 ISO 字符串，此保护前提是同一部署内各实例时钟基本同步
  -- （容器部署通常为 UTC）；时钟偏差超过故障窗口时不保证判断正确
  local ts = msg['timestamp']
  if cutoff and cutoff ~= '' and type(ts) == 'string' and ts > cutoff then return 0 end
  -- Python 端用 str.strip()（全 Unicode 空白），Lua 的 %s 只覆盖 ASCII。
  -- 两侧必须按同一字符集裁剪两端空白，否则带全角空格的提问会被误判为
  -- 不一致（长期记忆删了、短期记忆还留着旧一轮）。多字节空白只按精确
  -- 字节序列用 string.sub 比对，绝不写进字符类或加量词——那会匹配到
  -- 汉字（如「丠」= E4 B8 A0）的末尾字节，裁出非法 UTF-8；也不能全局
  -- 替换，否则提问内部的空白被改掉，与 expected 对不上。
  -- ws 与 Python str.isspace() 的多字节部分一致（ASCII 空白与 0x1C-0x1F
  -- 在 ws_at 里按字节判断）。用「逐字节前进 / 回退」的线性扫描，而不是
  -- 每轮 gsub 整串：4000 字的纯空白提问在 Redis 单线程里不会退化到
  -- O(n^2) 的长阻塞。
  local ws = {
    '\\194\\133', '\\194\\160', '\\225\\154\\128',
    '\\226\\128\\128', '\\226\\128\\129', '\\226\\128\\130',
    '\\226\\128\\131', '\\226\\128\\132', '\\226\\128\\133',
    '\\226\\128\\134', '\\226\\128\\135', '\\226\\128\\136',
    '\\226\\128\\137', '\\226\\128\\138', '\\226\\128\\168',
    '\\226\\128\\169', '\\226\\128\\175', '\\226\\129\\159',
    '\\227\\128\\128'
  }
  local function ascii_ws(byte)
    if byte == nil then return false end
    return byte == 32 or (byte >= 9 and byte <= 13) or (byte >= 28 and byte <= 31)
  end
  local function ws_at(s, pos)
    if ascii_ws(string.byte(s, pos)) then return 1 end
    for _, w in ipairs(ws) do
      if string.sub(s, pos, pos + #w - 1) == w then return #w end
    end
    return 0
  end
  local function ws_before(s, pos)
    if ascii_ws(string.byte(s, pos)) then return 1 end
    for _, w in ipairs(ws) do
      local size = #w
      if pos >= size and string.sub(s, pos - size + 1, pos) == w then
        return size
      end
    end
    return 0
  end
  local function strip_ends(s)
    local total = #s
    local head = 1
    while head <= total do
      local size = ws_at(s, head)
      if size == 0 then break end
      head = head + size
    end
    local tail = total
    while tail >= head do
      local size = ws_before(s, tail)
      if size == 0 then break end
      tail = tail - size
    end
    if tail < head then return '' end
    return string.sub(s, head, tail)
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

# 幂等追加消息：以 op_id 在 applied 集合中记账，与 RPUSH 在同一脚本里原子
# 完成。故障期重放时同一条消息（同 op_id）不会重复追加；而只按消息列表
# 去重无法区分「已追加但被新消息挤出窗口」与「从未写入」——前者应跳过，
# 后者必须补写，靠窗口内容判断会静默丢消息。
# ARGV: 窗口大小、TTL、然后每两条为一组 (op_id, payload)。
_ADD_MESSAGES_LUA = """
local key = KEYS[1]
local applied = KEYS[2]
local window = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local pushed = 0
for i = 3, #ARGV, 2 do
  local op_id = ARGV[i]
  local payload = ARGV[i + 1]
  if redis.call('SISMEMBER', applied, op_id) == 0 then
    redis.call('SADD', applied, op_id)
    redis.call('RPUSH', key, payload)
    pushed = pushed + 1
  end
end
if pushed > 0 then
  redis.call('LTRIM', key, -window, -1)
  redis.call('EXPIRE', key, ttl)
  redis.call('EXPIRE', applied, ttl)
end
return pushed
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
# 注册表总量上限：正常路径下日志在重放后立即移除，只有长时间故障且不断
# 出现新会话时才会积累；超过上限时优先淘汰已空的最旧日志，其次丢弃最旧
# 会话的待重放操作（记录警告），避免进程内存无界增长
_PENDING_JOURNALS_MAX = 2048


def _evict_journal_locked() -> None:
    """注册表满时淘汰一条日志（调用方持有注册表锁）。

    按「空日志 → 只含新增的日志 → 任意日志」的顺序淘汰：删除类操作丢失会
    让已清空 / 已删除的轮次在恢复后复活，放到最后才牺牲。
    """
    for stale_key, stale in _PENDING_JOURNALS.items():
        if stale.lock.acquire(blocking=False):
            if not stale.ops:
                del _PENDING_JOURNALS[stale_key]
                stale.lock.release()
                return
            stale.lock.release()
    for stale_key, stale in list(_PENDING_JOURNALS.items()):
        if stale.lock.acquire(blocking=False):
            only_adds = all(kind == "add" for kind, _ in stale.ops)
            if only_adds:
                dropped = len(stale.ops)
                del _PENDING_JOURNALS[stale_key]
                stale.lock.release()
                logger.warning(
                    "短期记忆故障操作日志过多，丢弃最旧会话的 %s 条待重放消息",
                    dropped,
                )
                return
            stale.lock.release()
    for stale_key, stale in list(_PENDING_JOURNALS.items()):
        if stale.lock.acquire(blocking=False):
            dropped = len(stale.ops)
            del _PENDING_JOURNALS[stale_key]
            stale.lock.release()
            logger.warning(
                "短期记忆故障操作日志过多，丢弃最旧会话的 %s 条待重放操作",
                dropped,
            )
            return


@contextmanager
def _pending_journal(key: str) -> Iterator[_PendingJournal]:
    """获取会话键对应的操作日志并持有其锁（跨实例共享）。"""
    while True:
        with _PENDING_JOURNALS_LOCK:
            journal = _PENDING_JOURNALS.get(key)
            if journal is None:
                if len(_PENDING_JOURNALS) >= _PENDING_JOURNALS_MAX:
                    _evict_journal_locked()
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
        # 取锁期间日志已被清理或淘汰：重新从注册表解析
        journal.lock.release()


def _drop_idle_journal(key: str, journal: _PendingJournal) -> None:
    """日志已空时从注册表移除（调用方持有 journal.lock）。"""
    with _PENDING_JOURNALS_LOCK:
        if _PENDING_JOURNALS.get(key) is journal:
            del _PENDING_JOURNALS[key]


def discard_pending_ops(key: str) -> int:
    """丢弃某个会话键排队的故障期操作（会话删除 / 清空时调用）。

    返回被丢弃的操作条数。删除会话后复用的同一 session_id 不能把旧会话
    排队的消息或删除动作重放进新会话。
    """
    while True:
        with _PENDING_JOURNALS_LOCK:
            journal = _PENDING_JOURNALS.get(key)
        if journal is None:
            return 0
        with journal.lock:
            with _PENDING_JOURNALS_LOCK:
                owned = _PENDING_JOURNALS.get(key) is journal
            if not owned:
                # 等待锁期间日志已被重放 / 淘汰替换：重新查找新日志
                continue
            dropped = len(journal.ops)
            journal.ops.clear()
            # 持锁时判空并移除：释放锁后再移除会把并发追加的操作一起丢掉
            _drop_idle_journal(key, journal)
            return dropped


def queue_clear(key: str) -> None:
    """登记一次整键删除（会话删除时 Redis 清理失败的后备）。

    与 ``clear()`` 的故障路径一致：清空覆盖此前所有变更，恢复后由该会话
    的下一次访问重放；墓碑过期后复用同一 session_id 不会读到旧对话。
    """
    with _pending_journal(key) as journal:
        journal.ops.clear()
        journal.ops.append(("clear", datetime.now().isoformat()))


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

    def _append_op(
        self, journal: _PendingJournal, kind: str, payload: Any = None
    ) -> None:
        """在已持锁的日志里登记一条操作，并把新增项限制在有界范围内。"""
        if kind == "clear":
            # 清空会覆盖之前所有变更，旧操作不再需要重放
            journal.ops.clear()
        journal.ops.append((kind, payload))
        cap = max(64, self.max_turns * 8)
        add_indexes = [
            i for i, (op_kind, _) in enumerate(journal.ops) if op_kind == "add"
        ]
        # 只丢最旧的新增项：恢复后的窗口内容仍与全程在线写入一致
        for index in reversed(add_indexes[: max(len(add_indexes) - cap, 0)]):
            del journal.ops[index]
        # 删除类操作单独限额（clear 是「覆盖此前全部变更」的单条记录，不参与
        # 裁剪）：极端情况下丢一条最旧的删除可能复活一轮已删除对话，但没有
        # 上限会让单个会话在长时间故障中无界占用内存
        delete_indexes = [
            i for i, (op_kind, _) in enumerate(journal.ops)
            if op_kind in ("truncate", "remove")
        ]
        excess = len(delete_indexes) - cap
        if excess > 0:
            logger.warning(
                "短期记忆故障操作日志过多，丢弃 %s 条最早的删除操作", excess
            )
            for index in reversed(delete_indexes[:excess]):
                del journal.ops[index]

    def _op_marker_key(self, op_id: str) -> str:
        """截断脚本的一次性标记键（与删除在同一脚本里原子写入）。"""
        return f"{self.key}:op:{op_id}"

    def _replay_ops(self, ops: list[tuple[str, Any]]) -> None:
        window = self.max_turns * 2
        while ops:
            kind, payload = ops[0]
            if kind == "add":
                # 连续新增合并为一次脚本调用（最多 100 条）：脚本按 op_id
                # 幂等，逐条往返会把恢复后的首次调用拖成几十次网络往返
                batch: list[tuple[str, str]] = []
                consumed = 0
                for op_kind, op_payload in ops:
                    if op_kind != "add" or len(batch) >= 100:
                        break
                    consumed += 1
                    batch.append(op_payload)
                arguments: list[str] = [str(window), str(self.ttl_sec)]
                for op_payload, op_id in batch:
                    arguments.extend([op_id, op_payload])
                self.client.eval(
                    _ADD_MESSAGES_LUA, 2, self.key, applied_key(self.key), *arguments
                )
                # 这些操作已成功出队、不会再重放，回收对应的去重记录；
                # 回收失败只是多留几条无用记录（集合带 TTL），不回调错误
                try:
                    self.client.srem(
                        applied_key(self.key), *[op_id for _, op_id in batch]
                    )
                except Exception:  # noqa: BLE001 - 清理失败不影响重放结果
                    logger.debug("清理已重放 applied 记录失败：%s", self.key)
                del ops[:consumed]
                continue
            if kind == "truncate":
                expect, op_id, cutoff = payload
                result = int(
                    self.client.eval(
                        _TRUNCATE_LAST_TURN_LUA,
                        2,
                        self.key,
                        self._op_marker_key(op_id),
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
                # 拿到脚本响应后标记不会再被使用，删除避免残留到 TTL
                try:
                    self.client.delete(self._op_marker_key(op_id))
                except Exception:  # noqa: BLE001 - 标记带 TTL，删除失败也会过期
                    logger.debug("清理截断标记失败：%s", self.key)
                ops.pop(0)
            elif kind == "remove":
                if payload:
                    self.client.eval(_REMOVE_MESSAGES_LUA, 1, self.key, *payload)
                ops.pop(0)
            else:  # clear
                cutoff = payload if isinstance(payload, str) else None
                if cutoff is not None:
                    try:
                        age = (
                            datetime.now() - datetime.fromisoformat(cutoff)
                        ).total_seconds()
                    except ValueError:
                        age = 0.0
                    if age > _CLEAR_OP_MAX_AGE_SEC:
                        # 迟到太久的整键删除可能命中已合法复用的新会话
                        logger.warning(
                            "短期记忆故障期清空已过期（%s 秒），跳过：%s",
                            int(age),
                            self.key,
                        )
                        ops.pop(0)
                        continue
                self.client.delete(self.key, applied_key(self.key))
                ops.pop(0)

    def _call_redis(self, operation, fallback, on_failure=None):
        """执行 Redis 操作；返回 (结果, 是否走了 Redis)，故障时回退影子缓存。

        重放故障期变更、本次操作与失败登记在同一把按会话键的锁内完成：
        日志顺序始终与真实调用顺序一致（并发的清空不会让更早的失败新增
        排到清空之后）。``on_failure(journal)`` 在锁内登记失败操作。
        """
        global _redis_degraded_until
        if time.monotonic() < _redis_degraded_until:
            fallback_result = fallback()
            if on_failure is not None:
                with _pending_journal(self.key) as journal:
                    on_failure(journal)
            return fallback_result, False
        fallback_result: Any = None
        fallback_done = False
        try:
            with _pending_journal(self.key) as journal:
                try:
                    self._replay_ops(journal.ops)
                    result = operation()
                except Exception:
                    # 先执行回退（截断需要知道影子缓存里具体删了哪些消息），
                    # 再在锁内登记失败操作；重放中途失败时本次操作还没发出，
                    # 但影子缓存里已删除的轮次同样必须登记，否则恢复后复活
                    fallback_result = fallback()
                    fallback_done = True
                    if on_failure is not None:
                        on_failure(journal)
                    raise
                if not journal.ops:
                    _drop_idle_journal(self.key, journal)
        except Exception as exc:  # noqa: BLE001 - 短期缓存故障不能中断对话
            _redis_degraded_until = time.monotonic() + _REDIS_RETRY_COOLDOWN_SEC
            logger.warning(
                "Redis 短期记忆不可用（%s），本进程回退内存缓存 %ss",
                exc,
                int(_REDIS_RETRY_COOLDOWN_SEC),
            )
            if not fallback_done:
                fallback_result = fallback()
            return fallback_result, False
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
        op_id = uuid.uuid4().hex
        # 影子缓存复用同一个消息对象：时间戳与 Redis 中的 JSON 完全一致，
        # 故障期截断时才能按完整消息项精确删除 Redis 里的对应项
        self._fallback._append_message(message)

        def op() -> None:
            # 幂等追加：op_id 写入 applied 集合与 RPUSH 在同一脚本里完成，
            # 故障重放时同一消息不会重复追加（响应丢失语义下也成立）
            self.client.eval(
                _ADD_MESSAGES_LUA,
                2,
                self.key,
                applied_key(self.key),
                str(self.max_turns * 2),
                str(self.ttl_sec),
                op_id,
                payload,
            )

        _, via_redis = self._call_redis(
            op,
            lambda: None,
            lambda journal: self._append_op(journal, "add", (payload, op_id)),
        )
        if via_redis:
            # 成功拿到响应后 op_id 不会再被重放，从 applied 集合移除：集合
            # 只保留「写入结果不确定」的少量条目，不随消息总量增长
            self._forget_applied(op_id)
        logger.debug("Added message to Redis short-term memory: %s", role)

    def _forget_applied(self, op_id: str) -> None:
        try:
            self.client.srem(applied_key(self.key), op_id)
        except Exception:  # noqa: BLE001 - 清理失败只留下一条无用记录
            logger.debug("清理 applied 记录失败：%s", self.key)

    def _forget_marker(self, op_id: str) -> None:
        try:
            self.client.delete(self._op_marker_key(op_id))
        except Exception:  # noqa: BLE001 - 标记带 TTL，清理失败也会自动过期
            logger.debug("清理截断标记失败：%s", self.key)

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
        cutoff = datetime.now().isoformat()

        def on_failure(journal: _PendingJournal) -> None:
            # 故障期间删不掉 Redis 里的旧内容：恢复后重放一次删除（该操作
            # 会覆盖日志中更早的变更，_append_op 已按此合并）
            self._append_op(journal, "clear", cutoff)

        self._call_redis(
            lambda: self.client.delete(self.key, applied_key(self.key)),
            lambda: None,
            on_failure,
        )
        # 无论 Redis 是否可用都清空影子缓存，否则故障切换后
        # 已清空的对话会从内存里“复活”
        self._fallback.clear()
        logger.info("Redis short-term memory cleared: %s", self.key)

    def truncate_last_turn(self, expect: str | None = None) -> int:
        """删除 Redis 中最后一轮对话，返回删除条数（保留的条目 TTL 不变）。

        使用 Lua 脚本把「读取最后两条 → 判断 → 裁剪」放在一次原子调用里，
        避免与其它进程 / worker 追加消息的写入交错后裁错条目；``expect`` 不为
        None 时内容不一致则不修改。在线调用与故障重放使用同一个一次性标记
        （op_id）：脚本已在服务端执行但响应丢失时，重放会凭标记直接跳过，
        不会把上一轮同文本的对话再删一次。
        """
        removed: list[dict[str, Any]] = []
        op_id = uuid.uuid4().hex
        cutoff = datetime.now().isoformat()

        def fallback() -> int:
            tail = self._fallback.messages[-2:]
            count = self._fallback.truncate_last_turn(expect)
            if count:
                # 记录从影子缓存删除的完整消息，恢复后按项精确删除
                removed[:] = tail[len(tail) - count:]
            return count

        def on_failure(journal: _PendingJournal) -> None:
            if removed:
                # 故障期间在影子缓存里删掉了这一轮：恢复后按完整消息项精确
                # 删除。不能用「删除列表末尾一轮」——重放发生在本进程冷却
                # 结束后的下一次调用，期间其它实例可能已追加新消息，末尾
                # 已不是这一轮，条件删除会失效、把已删除的轮次留在 Redis。
                # 影子缓存可能没见过其它实例写入的更新轮次：此处只删本对象
                # 确实写入过的消息，宁可少删也不误删别人的轮次。
                self._append_op(
                    journal,
                    "remove",
                    [json.dumps(m, ensure_ascii=False) for m in removed],
                )
            elif expect is not None:
                # 影子缓存里没有这一轮（临时对象 / 重启后未命中缓存），
                # 但调用方给出了内容校验：恢复后仍可条件式删除——只在
                # Redis 末尾仍是该提问、且时间戳不晚于本次删除发起时刻
                # （cutoff）时生效
                self._append_op(journal, "truncate", (expect, op_id, cutoff))

        raw, via_redis = self._call_redis(
            lambda: self.client.eval(
                _TRUNCATE_LAST_TURN_LUA,
                2,
                self.key,
                self._op_marker_key(op_id),
                "1" if expect is not None else "0",
                (expect or "").strip(),
                str(self.ttl_sec),
                "",
            ),
            fallback,
            on_failure,
        )
        count = int(raw or 0)
        if via_redis:
            # 已拿到响应，标记不会再被重放使用；删除后键空间只保留失败
            # 场景的少量标记（未删除的成功会随 TTL 过期）
            self._forget_marker(op_id)
        if via_redis and count:
            # Redis 删除成功时同步镜像缓存，避免 Redis 再故障时把已删除的
            # 一轮从影子缓存读回来
            self._fallback.truncate_last_turn(expect)
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
