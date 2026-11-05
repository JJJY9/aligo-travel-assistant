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
import time
from datetime import datetime
from typing import Any

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
local check = ARGV[1]
local expected = ARGV[2]
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
  -- Python 端用 str.strip()（含全角空格 U+3000 / 不间断空格 U+00A0），
  -- Lua 的 %s 只覆盖 ASCII；两侧必须按同一字符集裁剪，否则带全角空格的
  -- 提问会被误判为不一致（长期记忆删了、短期记忆还留着旧一轮）
  if string.match(content, '^[%s\\194\\160\\227\\128\\128]*(.-)[%s\\194\\160\\227\\128\\128]*$') ~= expected then return 0 end
end
if count >= n then
  redis.call('DEL', key)
else
  redis.call('LTRIM', key, 0, -count - 1)
end
return count
"""


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

        self.messages.append(message)

        # 自动淘汰旧消息（保持 max_turns 轮对话）
        # 每轮 = 2条消息（用户 + 助手）
        max_messages = self.max_turns * 2
        if len(self.messages) > max_messages:
            self.messages = self.messages[-max_messages:]

        logger.debug(f"Added message to short-term memory: {role}")

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
        # Redis 故障期间新增的消息（JSON 列表项），恢复后补写进 Redis
        self._pending: list[str] = []

    def _flush_pending(self) -> None:
        """把故障期间新增的消息补写进 Redis（保持最近窗口与 TTL）。"""
        if not self._pending:
            return
        payloads = list(self._pending)
        pipe = self.client.pipeline()
        pipe.rpush(self.key, *payloads)
        pipe.ltrim(self.key, -self.max_turns * 2, -1)
        pipe.expire(self.key, self.ttl_sec)
        pipe.execute()
        self._pending.clear()

    def _call_redis(self, operation, fallback):
        """执行 Redis 操作；返回 (结果, 是否走了 Redis)，故障时回退影子缓存。"""
        global _redis_degraded_until
        if time.monotonic() < _redis_degraded_until:
            return fallback(), False
        try:
            # 恢复后的第一次 Redis 调用先补写故障期间的新消息，
            # 否则刚恢复时读到的上下文会缺少最近几轮
            self._flush_pending()
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
        self._fallback.add_message(role, content, metadata)

        def op() -> None:
            pipe = self.client.pipeline()
            pipe.rpush(self.key, payload)
            pipe.ltrim(self.key, -self.max_turns * 2, -1)
            pipe.expire(self.key, self.ttl_sec)
            pipe.execute()

        _, via_redis = self._call_redis(op, lambda: None)
        if not via_redis:
            # 故障期间的消息先记下，Redis 恢复后补写，避免恢复后上下文缺轮次
            self._pending.append(payload)
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
        self._call_redis(lambda: self.client.delete(self.key), lambda: None)
        # 无论 Redis 是否可用都清空影子缓存与待补写消息，否则故障切换后
        # 已清空的对话会从内存里“复活”
        self._fallback.clear()
        self._pending.clear()
        logger.info("Redis short-term memory cleared: %s", self.key)

    def truncate_last_turn(self, expect: str | None = None) -> int:
        """删除 Redis 中最后一轮对话，返回删除条数（保留的条目 TTL 不变）。

        使用 Lua 脚本把「读取最后两条 → 判断 → 裁剪」放在一次原子调用里，
        避免与其它进程 / worker 追加消息的写入交错后裁错条目；``expect`` 不为
        None 时内容不一致则不修改。
        """
        raw, via_redis = self._call_redis(
            lambda: self.client.eval(
                _TRUNCATE_LAST_TURN_LUA,
                1,
                self.key,
                "1" if expect is not None else "0",
                (expect or "").strip(),
            ),
            lambda: self._fallback.truncate_last_turn(expect),
        )
        count = int(raw or 0)
        if via_redis and count:
            # Redis 删除成功时同步镜像缓存，避免 Redis 再故障时把已删除的
            # 一轮从影子缓存读回来
            self._fallback.truncate_last_turn(expect)
        if count:
            # 故障期间的消息可能还没补写进 Redis，同样要从待补写列表移除
            self._pending = (
                self._pending[:-count] if count <= len(self._pending) else []
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
