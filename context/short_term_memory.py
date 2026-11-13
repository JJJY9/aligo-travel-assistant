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

from utils.tombstones import DEFAULT_TOMBSTONE_TTL_SEC

logger = logging.getLogger(__name__)

# Redis 运行中故障的冷却：期间短期记忆回退进程内缓存（多实例共享暂时退化为
# 单实例），60 秒后自动重试；健康检查用它区分「配置了 Redis 但当前降级」
_REDIS_RETRY_COOLDOWN_SEC = 60.0
_redis_degraded_until = 0.0
# 最近一次 Redis 操作是否成功：冷却窗口到期不等于恢复（期间可能一次都
# 没成功过），健康接口必须看真实结果，否则 Redis 仍不可用时上报健康
_redis_available = True

# 截断脚本一次性标记的存活时间：标记要覆盖「脚本已执行、响应丢失、故障期
# 操作日志排队等待重放」的整段时间。操作日志在进程内没有过期机制，会话可能
# 隔很久才再次访问；标记若按消息 TTL（默认 1 小时）过期，迟到的重放会重新
# 执行删除脚本，把同文本的下一轮对话再删一次。取 30 天，同时保证「响应丢失
# 且操作日志被淘汰」时残留标记最终自动清理。
_OP_MARKER_TTL_SEC = 30 * 24 * 3600

# 第三方 SDK（redis-py）的连接 / 超时异常不是内置 ConnectionError /
# TimeoutError 的子类，按类名识别（与 utils/llm_resilience 的做法一致）。
# ReadOnlyError（主从切换后连到只读副本）/ OutOfMemoryError（noeviction）/
# ClusterDownError / MasterDownError / TryAgainError 是服务端可用性状态，
# 操作本身没问题，恢复后重放可以成功：若按永久错误计入毒操作，同一队首
# 连续 3 次失败就会被静默丢弃，故障期消息永久缺失、已删轮次恢复后复活。
_TRANSIENT_REDIS_ERROR_NAMES = frozenset(
    {
        "ConnectionError",
        "TimeoutError",
        "ReadOnlyError",
        "OutOfMemoryError",
        "ClusterDownError",
        "MasterDownError",
        "TryAgainError",
    }
)


def _is_transient_redis_error(exc: BaseException) -> bool:
    """是否为「Redis 暂时不可达」类故障（重试有意义，操作本身没问题）。"""
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    return any(
        cls.__name__ in _TRANSIENT_REDIS_ERROR_NAMES for cls in type(exc).__mro__
    )



def redis_short_term_degraded() -> bool:
    """短期记忆是否处于「故障重试冷却」窗口内（内部重试节流用）。"""
    return time.monotonic() < _redis_degraded_until


def redis_short_term_available() -> bool:
    """最近一次 Redis 操作是否成功（健康接口用）。

    与 :func:`redis_short_term_degraded` 的区别：冷却窗口到期但期间没有
    任何成功操作时，本函数仍返回 False——Redis 实际不可用时健康页不能
    因为「冷却结束」就报告恢复。
    """
    return _redis_available


def reset_redis_degraded() -> None:
    """清空降级冷却与失败标记（测试隔离 / 配置热更新后立即重试）。"""
    global _redis_degraded_until, _redis_available
    _redis_degraded_until = 0.0
    _redis_available = True


def note_redis_operation_failure(exc: BaseException) -> None:
    """记录一次不在 ``_call_redis`` 内的真实 Redis 操作失败（清理等）。

    与 ``_call_redis`` 的故障语义一致：连接 / 服务级错误进入 60 秒重试
    冷却并把 ``_redis_available`` 置 False（健康接口「最近一次真实操作」
    的口径保持准确）；确定性错误（WRONGTYPE / ACL）只影响单个键，不改变
    进程级状态。
    """
    global _redis_available, _redis_degraded_until
    if not _is_transient_redis_error(exc):
        return
    _redis_available = False
    _redis_degraded_until = time.monotonic() + _REDIS_RETRY_COOLDOWN_SEC


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
-- 只删除「发起时刻之前」的末尾轮次：迟到的重放（含无内容校验的无条件
-- 删除）不会误删故障期间其它实例新写入的轮次。cutoff 与消息时间戳都是
-- 各实例本地时钟的 ISO 字符串，前提是同一部署内各实例时钟基本同步
-- （容器部署通常为 UTC）；时钟偏差超过故障窗口时不保证判断正确
if cutoff and cutoff ~= '' and user_raw ~= nil then
  local cut_ok, cut_msg = pcall(cjson.decode, user_raw)
  if cut_ok and type(cut_msg) == 'table' and type(cut_msg['timestamp']) == 'string'
     and cut_msg['timestamp'] > cutoff then return 0 end
end
if check == '1' then
  if user_raw == nil then return 0 end
  local msg_ok, msg = pcall(cjson.decode, user_raw)
  if not msg_ok or type(msg) ~= 'table' or msg['role'] ~= 'user' then return 0 end
  local content = msg['content']
  if type(content) ~= 'string' then return 0 end
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

# 故障期清空的延迟重放：只删除「清空发起时刻（cutoff）之前」写入的消息，
# 保留之后由其它实例写入的新消息。会话删除后墓碑过期、session_id 被合法
# 复用时，新会话的消息时间戳晚于 cutoff，不会被迟到的清空误删；旧对话
# 则一定早于 cutoff，会被清掉——不依赖操作年龄，也不怕故障持续很久。
# 无时间戳的条目保守保留。ARGV[1] 为空时退化为整键删除（兼容旧日志）。
_CLEAR_BEFORE_LUA = """
local key = KEYS[1]
local cut = ARGV[1]
local n = redis.call('LLEN', key)
if cut == '' then
  redis.call('DEL', key)
  return n
end
local stale = {}
for i = 0, n - 1 do
  local raw = redis.call('LINDEX', key, i)
  local ok, msg = pcall(cjson.decode, raw)
  if ok and type(msg) == 'table' and type(msg['timestamp']) == 'string'
     and msg['timestamp'] <= cut then
    stale[#stale + 1] = raw
  end
end
for _, raw in ipairs(stale) do
  redis.call('LREM', key, 0, raw)
end
return #stale
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
    -- 先 RPUSH 再登记：脚本不回滚，若先 SADD 后 RPUSH 失败（WRONGTYPE /
    -- 内存不足），op_id 已被当作「已追加」写入 applied，恢复重放会永久
    -- 跳过这条消息。RPUSH 出错时脚本中断，op_id 不落 applied，重放可补写。
    redis.call('RPUSH', key, payload)
    redis.call('SADD', applied, op_id)
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

    __slots__ = ("lock", "ops", "stuck", "started_at")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.ops: list[tuple[str, Any]] = []
        # 首条操作的入队时刻：超过删除保护窗口（墓碑 TTL）的排队操作
        # 不能再安全重放（墓碑已过期，可能复活已删除会话）
        self.started_at: float | None = None
        # 队首操作的连续失败计数：确定性错误（键类型损坏 / 载荷无法编码
        # 等）每次重试都会失败，连续三次后丢弃该操作，避免整本日志卡死
        self.stuck: tuple[str, int] = ("", 0)


_PENDING_JOURNALS: dict[str, _PendingJournal] = {}
_PENDING_JOURNALS_LOCK = threading.Lock()
# 注册表总量上限：正常路径下日志在重放后立即移除，只有长时间故障且不断
# 出现新会话时才会积累；超过上限时优先淘汰已空的最旧日志，其次丢弃最旧
# 会话的待重放操作（记录警告），避免进程内存无界增长
_PENDING_JOURNALS_MAX = 2048
# 排队操作的最大存活时间：与共享墓碑 TTL 对齐。墓碑过期后无法再判断排队
# 的新增是否属于已删除会话（纪元未参与重放检查），重放可能把已删除对话
# 写回 Redis——长时间故障后宁可有界丢弃
_PENDING_MAX_AGE_SEC = DEFAULT_TOMBSTONE_TTL_SEC


def _evict_journal_locked() -> tuple[str, int, str] | None:
    """注册表满时淘汰一条日志（调用方持有注册表锁）。

    按「空日志 → 只含新增的日志 → 任意日志」的顺序淘汰：删除类操作丢失会
    让已清空 / 已删除的轮次在恢复后复活，放到最后才牺牲。
    返回 (会话键, 丢弃条数, 类别)，由调用方在释放注册表锁后再记录日志，
    避免日志 I/O 拉长全局锁的持有时间。
    """
    for stale_key, stale in _PENDING_JOURNALS.items():
        if stale.lock.acquire(blocking=False):
            if not stale.ops:
                del _PENDING_JOURNALS[stale_key]
                stale.lock.release()
                return None
            stale.lock.release()
    for stale_key, stale in list(_PENDING_JOURNALS.items()):
        if stale.lock.acquire(blocking=False):
            only_adds = all(kind == "add" for kind, _ in stale.ops)
            if only_adds:
                dropped = len(stale.ops)
                del _PENDING_JOURNALS[stale_key]
                stale.lock.release()
                return stale_key, dropped, "消息"
            stale.lock.release()
    for stale_key, stale in list(_PENDING_JOURNALS.items()):
        if stale.lock.acquire(blocking=False):
            dropped = len(stale.ops)
            del _PENDING_JOURNALS[stale_key]
            stale.lock.release()
            return stale_key, dropped, "操作"
    return None


@contextmanager
def _pending_journal(key: str) -> Iterator[_PendingJournal]:
    """获取会话键对应的操作日志并持有其锁（跨实例共享）。"""
    while True:
        evicted = None
        with _PENDING_JOURNALS_LOCK:
            journal = _PENDING_JOURNALS.get(key)
            if journal is None:
                if len(_PENDING_JOURNALS) >= _PENDING_JOURNALS_MAX:
                    evicted = _evict_journal_locked()
                journal = _PendingJournal()
                _PENDING_JOURNALS[key] = journal
        if evicted is not None:
            logger.warning(
                "短期记忆故障操作日志过多，丢弃最旧会话（%s）的 %s 条待重放%s",
                evicted[0],
                evicted[1],
                evicted[2],
            )
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
    for _ in range(3):
        with _PENDING_JOURNALS_LOCK:
            journal = _PENDING_JOURNALS.get(key)
        if journal is None:
            return 0
        with journal.lock:
            with _PENDING_JOURNALS_LOCK:
                owned = _PENDING_JOURNALS.get(key) is journal
            if not owned:
                # 等待锁期间日志已被重放 / 淘汰替换：重新查找新日志（有界重试）
                continue
            dropped = len(journal.ops)
            journal.ops.clear()
            journal.started_at = None
            # 持锁时判空并移除：释放锁后再移除会把并发追加的操作一起丢掉
            _drop_idle_journal(key, journal)
            return dropped
    # 连续三次都遇到日志被替换（极端的同键并发）：本次放弃，下次删除再处理
    logger.debug("丢弃排队操作时日志持续被替换，跳过：%s", key)
    return 0


def queue_clear(key: str) -> None:
    """登记一次整键删除（会话删除时 Redis 清理失败的后备）。

    与 ``clear()`` 的故障路径一致：清空覆盖此前所有变更，恢复后由该会话
    的下一次访问重放；墓碑过期后复用同一 session_id 不会读到旧对话。
    """
    with _pending_journal(key) as journal:
        journal.ops.clear()
        journal.started_at = time.monotonic()
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
            "oldest_message_time": self.messages[0].get("timestamp") if self.messages else None,
            "newest_message_time": self.messages[-1].get("timestamp") if self.messages else None
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
        client_factory: Any = None,
    ) -> None:
        self.client = client
        # 启动 / 重连失败时工厂返回 None，此处保留工厂：恢复后首次访问自动
        # 重新接入共享客户端；否则实例会永久停留在影子缓存（连健康 Redis
        # 之后的实例也继续「失败」，把进程级降级标志一次次重新置位）
        self._client_factory = client_factory
        self.session_id = session_id
        self.max_turns = max_turns
        # TTL 必须 >= 1：Redis 的 EXPIRE 在超时非正数时直接删除键，配置成
        # 0（「永不过期」的常见写法）会让每次写入推入消息后立刻整体删除，
        # 短期记忆静默为空且无任何错误
        self.ttl_sec = max(1, int(ttl_sec))
        # 影子缓存最近一次与 Redis 成功交互的时刻：Redis 的 TTL 是「键在
        # 最后一次写入后闲置 ttl_sec 才过期」，按消息时间戳裁剪会把持续
        # 使用的会话里较早的轮次误删
        self._shadow_touched = time.monotonic()
        self.user_id = user_id
        self.key = session_key(session_id, user_id)
        # Redis 故障时的进程内影子缓存：对话继续可用（跨实例共享暂时失效）。
        # 成功的写入也镜像一份，故障切换时上下文与删除操作仍然正确。
        self._fallback = ShortTermMemory(max_turns=max_turns)
        # 故障期变更记入按会话键共享的操作日志（见 _PendingJournal）：
        # ("add", (JSON 项, op_id)) / ("truncate", (expect, op_id, cutoff)) /
        # ("remove", [JSON 项]) / ("clear", cutoff)，恢复后按发生顺序重放。
        # 只补写新增会在恢复后复活已删除的轮次；按影子缓存整键重写会丢掉
        # 其它实例在故障期间写入、而本对象从未见过的消息。

    @property
    def _pending_ops(self) -> list[tuple[str, Any]]:
        """当前会话键待重放操作的快照（调试与测试用，不参与业务逻辑）。"""
        with _PENDING_JOURNALS_LOCK:
            journal = _PENDING_JOURNALS.get(self.key)
        return list(journal.ops) if journal is not None else []

    def seed_fallback_context(self, messages: list[dict[str, Any]]) -> None:
        """降级窗口内把长期记忆回填的上下文写入影子缓存（不登记重放）。

        这些消息来自长期记忆，Redis 里可能本来就有：登记重放会在恢复后
        重复 RPUSH。但只放在意图上下文里时，编排器 / 子智能体读
        ``get_recent_context`` 仍是空的——写入影子缓存让本轮子智能体也能
        看到回填的历史；Redis 恢复后以真实内容校正影子缓存。
        """
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            content = message.get("content")
            if role not in ("user", "assistant") or not isinstance(content, str):
                continue
            self._fallback.add_message(role, content)

    def _append_op(
        self, journal: _PendingJournal, kind: str, payload: Any = None
    ) -> None:
        """在已持锁的日志里登记一条操作，并把新增项限制在有界范围内。"""
        if kind == "clear":
            # 清空会覆盖之前所有变更，旧操作不再需要重放
            journal.ops.clear()
            journal.started_at = None
        if journal.started_at is None:
            journal.started_at = time.monotonic()
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

    def _replay_ops(self, journal: _PendingJournal) -> None:
        ops = journal.ops
        window = self.max_turns * 2
        if ops and journal.started_at is not None:
            age = time.monotonic() - journal.started_at
            if age > _PENDING_MAX_AGE_SEC:
                # 超过删除保护窗口：墓碑可能已过期而快照 / 删除不可复原，
                # 重放排队新增可能复活已删除会话的对话，丢弃并记录条数
                logger.warning(
                    "短期记忆故障操作排队超过 %s 秒（%.0fs），丢弃 %s 条待重放操作：%s",
                    int(_PENDING_MAX_AGE_SEC),
                    age,
                    len(ops),
                    self.key,
                )
                ops.clear()
                journal.started_at = None
                journal.stuck = ("", 0)
                return
        if ops and self.user_id and self.session_id:
            # 跨进程删除墓碑：其它实例 / CLI 已删除该会话时，本进程日志里
            # 排队的新增会把已删除对话重新写回 Redis 并注入模型上下文
            # （进程内日志各管各的，删除方只能清掉自己的队列）。命中墓碑
            # 即整体丢弃，并清空影子缓存，避免故障期继续从影子回放旧对话。
            from utils.tombstones import is_session_deleted

            # force：跳过墓碑模块的失败冷却真实读取一次——本检查紧邻真实
            # 网络调用，冷却期直接返回 False 会把已删除会话的排队消息放行
            if is_session_deleted(self.user_id, self.session_id, force=True):
                logger.warning("会话已删除，丢弃短期记忆重放队列：%s", self.key)
                ops.clear()
                journal.started_at = None
                journal.stuck = ("", 0)
                self._fallback.messages = []
                return
        while ops:
            signature = repr(ops[0])
            try:
                self._replay_head(ops, window)
            except Exception as exc:  # noqa: BLE001 - 按异常类别决定是否计毒
                if _is_transient_redis_error(exc):
                    # 连接类故障只说明 Redis 暂时不可达，操作本身没有毒：
                    # 保留队首等下次恢复后重放。若把它计入「连续失败」，
                    # 每分钟一次的冷却重试会在长时间故障（约三分钟）后
                    # 静默丢弃故障期新增 / 删除，恢复后消息永久缺失或
                    # 已删除轮次复活。
                    journal.stuck = ("", 0)
                    raise
                if journal.stuck[0] == signature:
                    journal.stuck = (signature, journal.stuck[1] + 1)
                else:
                    journal.stuck = (signature, 1)
                if journal.stuck[1] >= 3:
                    # 只记录操作类型与队列长度：签名 repr 里含消息正文 /
                    # 用户提问原文，写进日志会把聊天内容泄露到日志采集
                    logger.warning(
                        "短期记忆重放操作连续失败 %s 次，丢弃队首 %s 操作"
                        "（队列剩余 %s 条）",
                        journal.stuck[1],
                        ops[0][0] if ops else "?",
                        max(0, len(ops) - 1),
                    )
                    if ops:
                        ops.pop(0)
                    journal.stuck = ("", 0)
                    continue
                raise
            journal.stuck = ("", 0)

    def _replay_head(
        self, ops: list[tuple[str, Any]], window: int
    ) -> None:
        """执行队首的一条（或一批连续的新增）操作，成功后从日志移除。"""
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
            return
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
                    str(_OP_MARKER_TTL_SEC),
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
            cutoff = payload if isinstance(payload, str) else ""
            self.client.eval(_CLEAR_BEFORE_LUA, 1, self.key, cutoff)
            ops.pop(0)

    def _call_redis(self, operation, fallback, on_failure=None):
        """执行 Redis 操作；返回 (结果, 是否走了 Redis)，故障时回退影子缓存。

        重放故障期变更、本次操作与失败登记在同一把按会话键的锁内完成：
        日志顺序始终与真实调用顺序一致（并发的清空不会让更早的失败新增
        排到清空之后）。``on_failure(journal)`` 在锁内登记失败操作。
        """
        global _redis_degraded_until, _redis_available
        if time.monotonic() < _redis_degraded_until:
            fallback_result = fallback()
            if on_failure is not None:
                with _pending_journal(self.key) as journal:
                    on_failure(journal)
            return fallback_result, False
        if self.client is None and self._client_factory is not None:
            # 工厂自身带失败冷却（不会每次 ping）：恢复后这里自动重新接入
            self.client = self._client_factory()
        if self.client is None:
            # 没有可用客户端：只走影子缓存并登记重放，不重置进程级降级标志——
            # 其它持有健康客户端的实例不应被本实例的「失败」拖累 60 秒
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
                    self._replay_ops(journal)
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
            if _is_transient_redis_error(exc):
                # 连接 / 服务级故障：本进程全部会话进入重试冷却并走影子缓存
                _redis_available = False
                _redis_degraded_until = (
                    time.monotonic() + _REDIS_RETRY_COOLDOWN_SEC
                )
                # 有工厂时丢弃可能已坏的客户端：冷却结束后经由工厂重新解析
                # （共享工厂通常直接返回缓存客户端，连接池会在下次命令时自动
                # 重连）；没有工厂的实例保持原客户端重试，行为与旧版一致
                if self._client_factory is not None:
                    self.client = None
                logger.warning(
                    "Redis 短期记忆不可用（%s），本进程回退内存缓存 %ss",
                    exc,
                    int(_REDIS_RETRY_COOLDOWN_SEC),
                )
            else:
                # 确定性错误（WRONGTYPE、ACL 拒绝等）只影响本会话键：不能
                # 把进程级冷却续期——一个中毒会话会让同进程所有健康会话
                # 在重叠窗口里都退回影子缓存（多实例上下文共享失效）
                logger.warning(
                    "短期记忆操作确定性失败（%s，会话 %s），仅本次回退：%s",
                    type(exc).__name__,
                    self.key,
                    exc,
                )
            if not fallback_done:
                fallback_result = fallback()
            return fallback_result, False
        _redis_degraded_until = 0.0
        _redis_available = True
        self._shadow_touched = time.monotonic()
        return result, True

    def _redis(self, operation, fallback):
        """执行 Redis 操作；连接级故障时回退影子缓存并进入 60 秒冷却。"""
        return self._call_redis(operation, fallback)[0]

    def _fallback_context(self, n_turns: int | None) -> list[dict[str, Any]]:
        """Redis 降级时的影子读取：按空闲时长对齐 Redis 的键 TTL 语义。

        影子缓存只按条数裁剪、从不按时间过期：Redis 键闲置超过 ``ttl_sec``
        已过期后，降级窗口仍会把旧对话送进模型（多实例删除的会话也会借
        影子回放）。Redis 的过期是「最后一次写入后闲置」，不是消息时间戳，
        因此这里只在「与 Redis 最后一次成功交互已超过 ttl_sec」时清空影子
        ——持续使用的会话不会被误删早期轮次。
        """
        if (
            self.ttl_sec > 0
            and time.monotonic() - self._shadow_touched > self.ttl_sec
        ):
            self._fallback.messages = []
        messages = self._fallback.get_recent_context()
        if n_turns is None:
            return list(messages)
        n_messages = n_turns * 2
        return messages[-n_messages:]

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
        window = self.max_turns * 2

        def op() -> list[dict[str, Any]]:
            # 总是读取整个窗口：既要返回调用方要的轮数，也要用 Redis 的
            # 真实内容校正影子缓存（重启 / LRU 淘汰后的新实例、其它实例
            # 的写入、已过期的旧消息都以这里为准）
            raw = self.client.lrange(self.key, -window, -1)
            messages = self._parse_window(raw)
            self._fallback.messages = messages[-window:]
            if n_turns is None:
                return messages
            return messages[-n_turns * 2:]

        return self._redis(op, lambda: self._fallback_context(n_turns))

    def _parse_window(self, raw) -> list[dict[str, Any]]:
        """解析窗口内的消息条目；坏元素跳过而不是让整次读取失败。

        无法解析的条目（旧版本或人工修复写入）与形状非法的条目都要跳过：
        前者按 Redis 故障处理会触发进程级冷却，后者会让下游
        ``message["role"]`` 每轮 KeyError。
        """
        messages: list[dict[str, Any]] = []
        for item in raw:
            try:
                message = json.loads(item)
            except (TypeError, ValueError, RecursionError):
                # 深嵌套 JSON 解析抛 RecursionError、超 4300 位整数字面量
                # 抛普通 ValueError（不是 JSONDecodeError）：都按坏条目跳过，
                # 不能被 _call_redis 当成 Redis 故障触发进程级降级
                logger.warning("跳过无法解析的短期记忆条目：%s", self.key)
                continue
            if (
                isinstance(message, dict)
                and isinstance(message.get("role"), str)
                and isinstance(message.get("content"), str)
            ):
                messages.append(message)
            elif isinstance(message, dict):
                logger.warning("跳过形状非法的短期记忆条目：%s", self.key)
        return messages

    def _refresh_fallback(self, window: int) -> None:
        """用 Redis 的真实内容整体替换影子缓存；读取失败时保持影子不动。

        在线截断成功后不能按 expect 在影子缓存里再删一次：影子可能没见过
        其它实例写入的最新轮次，按文本匹配会删掉仍在 Redis 的旧轮次（用户
        重复提问时）或删不掉已删除的轮次。删除是低频操作，多一次读取可接受。
        """
        try:
            raw = self.client.lrange(self.key, -window, -1)
        except Exception:  # noqa: BLE001 - 刷新失败不能保留可能已删除的轮次
            # 截断 / 清空已在 Redis 生效：影子若保留旧轮次，Redis 降级时
            # 会把已删除对话重新送进模型。保守清空，等下一次成功读取重建
            # （影子本就是尽力而为的可用性缓存，宁可暂时少上下文）。
            self._fallback.messages = []
            logger.debug("刷新短期记忆影子缓存失败：%s", self.key, exc_info=True)
            return
        self._fallback.messages = self._parse_window(raw)[-window:]

    def get_context_string(self, n_turns: int = 5) -> str:
        messages = self.get_recent_context(n_turns)
        if not messages:
            return "无历史对话"
        lines = []
        for message in messages:
            role = message.get("role")
            content = message.get("content")
            if not isinstance(role, str) or not isinstance(content, str):
                continue
            role_name = "用户" if role == "user" else "助手"
            lines.append(f"{role_name}: {content}")
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
            else:
                # 影子缓存里没有这一轮（临时对象 / 重启后未命中缓存）：
                # 恢复后按「发起时刻之前」条件删除末尾轮次——有内容校验时
                # 还需内容一致；没有校验时只靠 cutoff，迟到的重放不会误删
                # 故障期间其它实例新写入的轮次
                self._append_op(journal, "truncate", (expect, op_id, cutoff))

        raw, via_redis = self._call_redis(
            lambda: self.client.eval(
                _TRUNCATE_LAST_TURN_LUA,
                2,
                self.key,
                self._op_marker_key(op_id),
                "1" if expect is not None else "0",
                (expect or "").strip(),
                str(_OP_MARKER_TTL_SEC),
                # 在线调用同样带发起时刻：另一实例在删除发起之后写入的
                # 同文本新轮次（时间戳晚于 cutoff）不会被这次删除误删，
                # 与故障重放的判定口径一致（此前在线传空串，跨实例并发
                # 删除最后一轮可能删掉刚写入的新一轮）
                cutoff,
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
            # Redis 删除成功：以 Redis 为准整体刷新影子缓存。不能按 expect
            # 在本地再删一次——影子可能缺少其它实例写入的最新轮次，文本
            # 匹配会删掉仍在 Redis 的旧轮次（用户重复提问时）或删不掉已
            # 删除的轮次；读取失败时保持影子不动，绝不盲删
            self._refresh_fallback(self.max_turns * 2)
        logger.info(
            "Truncated last turn from Redis memory: %s (%s messages)", self.key, count
        )
        return count

    def get_statistics(self) -> dict[str, Any]:
        def op() -> dict[str, Any]:
            total = int(self.client.llen(self.key) or 0)
            oldest = newest = None

            def _timestamp(raw: Any) -> Any:
                if not raw:
                    return None
                try:
                    parsed = json.loads(raw)
                except (TypeError, ValueError, RecursionError):
                    # 含 ValueError：超 4300 位整数字面量抛的是普通
                    # ValueError，漏捕会把统计读取误判为 Redis 故障
                    return None
                # 可解析但非对象（null / 数字 / 列表）：不能让 .get 的
                # AttributeError 逃进 _call_redis 被误判为 Redis 故障、
                # 触发 60 秒进程级短期记忆降级
                return parsed.get("timestamp") if isinstance(parsed, dict) else None

            if total:
                try:
                    first = self.client.lindex(self.key, 0)
                    last = self.client.lindex(self.key, -1)
                    oldest = _timestamp(first)
                    newest = _timestamp(last)
                except (TypeError, ValueError):
                    pass
            return {
                "total_messages": total,
                "max_turns": self.max_turns,
                "oldest_message_time": oldest,
                "newest_message_time": newest,
            }

        return self._redis(op, self._fallback.get_statistics)
