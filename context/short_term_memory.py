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
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


def session_key(session_id: str) -> str:
    """Redis 中一个会话对应的键名（存储层与会话清理共用）。"""
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
  if string.match(content, '^%s*(.-)%s*$') ~= expected then return 0 end
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
    ) -> None:
        self.client = client
        self.session_id = session_id
        self.max_turns = max_turns
        self.ttl_sec = ttl_sec
        self.key = session_key(session_id)

    def add_message(self, role: str, content: str, metadata: dict | None = None) -> None:
        message = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            "metadata": metadata or {},
        }
        pipe = self.client.pipeline()
        pipe.rpush(self.key, json.dumps(message, ensure_ascii=False))
        pipe.ltrim(self.key, -self.max_turns * 2, -1)
        pipe.expire(self.key, self.ttl_sec)
        pipe.execute()
        logger.debug("Added message to Redis short-term memory: %s", role)

    def get_recent_context(self, n_turns: int | None = None) -> list[dict[str, Any]]:
        if n_turns is None:
            raw = self.client.lrange(self.key, 0, -1)
        else:
            raw = self.client.lrange(self.key, -n_turns * 2, -1)
        return [json.loads(item) for item in raw]

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
        self.client.delete(self.key)
        logger.info("Redis short-term memory cleared: %s", self.key)

    def truncate_last_turn(self, expect: str | None = None) -> int:
        """删除 Redis 中最后一轮对话，返回删除条数（保留的条目 TTL 不变）。

        使用 Lua 脚本把「读取最后两条 → 判断 → 裁剪」放在一次原子调用里，
        避免与其它进程 / worker 追加消息的写入交错后裁错条目；``expect`` 不为
        None 时内容不一致则不修改。
        """
        count = int(
            self.client.eval(
                _TRUNCATE_LAST_TURN_LUA,
                1,
                self.key,
                "1" if expect is not None else "0",
                (expect or "").strip(),
            )
            or 0
        )
        logger.info(
            "Truncated last turn from Redis memory: %s (%s messages)", self.key, count
        )
        return count

    def get_statistics(self) -> dict[str, Any]:
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
