"""
记忆管理器 (Memory Manager)
统一管理两层记忆，提供简单的API
"""
import logging
from datetime import datetime
from typing import Any

from .storage import create_long_term_memory, create_short_term_memory
from utils.json_parser import extract_json_from_async_response
from utils.turn_meta import strip_turn_meta

logger = logging.getLogger(__name__)


class MemoryManager:
    """
    记忆管理器：统一管理两层记忆
    - 短期记忆：最近对话（会话级）
    - 长期记忆：用户偏好和历史（跨会话）
    """

    def __init__(self, user_id: str, session_id: str, storage_path: str = "data/memory", llm_model=None):
        """
        初始化记忆管理器

        Args:
            user_id: 用户ID
            session_id: 会话ID
            storage_path: 长期记忆存储路径
            llm_model: LLM模型实例（用于总结长期记忆）
        """
        self.user_id = user_id
        self.session_id = session_id
        self.llm_model = llm_model
        self._storage_path = storage_path

        # 初始化两层记忆（配置 REDIS_URL / DATABASE_URL 时自动使用 Redis / PostgreSQL，
        # 未配置或连接失败时回退进程内 / JSON 实现）
        self.short_term = create_short_term_memory(
            session_id, max_turns=10, user_id=user_id
        )
        self.long_term = create_long_term_memory(user_id, storage_path)
        # 长期记忆总结缓存：同一会话内其它会话的历史未变化时不再重复调用 LLM
        self._summary_cache: tuple[tuple, str] | None = None

        logger.info(f"Memory manager initialized for user {user_id}, session {session_id}")

    # ========== 短期记忆操作 ==========

    def add_message(self, role: str, content: str, metadata: dict | None = None):
        """
        添加消息到短期记忆和长期记忆

        Args:
            role: 角色 (user/assistant)
            content: 消息内容
            metadata: 元数据
        """
        # 添加到短期记忆（当前会话）
        self.short_term.add_message(role, content, metadata)

        # JSON 后端持有内存快照：写入前重新加载，避免本轮对话期间通过记忆面板
        # 写入的标题 / 置顶 / 偏好被旧快照覆盖；PostgreSQL 直接查询，无需重载。
        if getattr(self.long_term, "snapshot_based", True):
            self.long_term = create_long_term_memory(self.user_id, self._storage_path)

        # 同时添加到长期记忆（跨会话持久化）
        self.long_term.add_chat_message(role, content, self.session_id)

    # ========== 长期记忆操作 ==========
    # 注意：大部分方法直接使用 self.short_term 和 self.long_term 即可，无需封装

    # ========== 综合查询 ==========

    def get_full_context(self) -> dict[str, Any]:
        """
        获取完整上下文（两层记忆）

        Returns:
            完整上下文字典
        """
        return {
            "short_term": {
                "recent_dialogue": self.short_term.get_recent_context(5),
                "context_string": self.short_term.get_context_string(5),
                "statistics": self.short_term.get_statistics()
            },
            "long_term": {
                "preferences": self.long_term.get_preference(),
                "chat_history": self.long_term.get_chat_history(10),
                "trip_history": self.long_term.get_trip_history(5),
                "frequent_destinations": self.long_term.get_frequent_destinations(3),
                "statistics": self.long_term.get_statistics()
            }
        }

    def export_data(self) -> dict[str, Any]:
        """导出当前用户的长期记忆数据（偏好 / 行程 / 对话历史 / 统计）。"""
        return {
            "user_id": self.user_id,
            "session_id": self.session_id,
            "exported_at": datetime.now().isoformat(),
            "preferences": self.long_term.get_preference(),
            "trip_history": self.long_term.get_trip_history(limit=None),
            "chat_history": self.long_term.get_chat_history(),
            "session_titles": self.long_term.get_session_titles(),
            "pinned_sessions": self.long_term.get_pinned_sessions(),
            "archived_sessions": self.long_term.get_archived_sessions(),
            "statistics": self.long_term.get_statistics(),
        }

    def reload_long_term(self) -> None:
        """重新从磁盘加载长期记忆。

        多会话（如 Web 界面同时开多个标签页）/ 多进程写入后，内存中的快照可能
        过期；在一轮对话开始前调用本方法可避免覆盖其它会话刚写入的数据。

        仅对基于内存快照的后端（JSON 文件）需要重建；PostgreSQL 每次调用直接
        查询数据库，重建反而会每轮新建连接，故跳过。
        """
        if not getattr(self.long_term, "snapshot_based", True):
            return
        self.long_term = create_long_term_memory(self.user_id, self._storage_path)

    def get_context_for_agent(self, long_term_summary: str | None = None) -> str:
        """
        获取用于Agent的上下文字符串

        Args:
            long_term_summary: 长期记忆总结（可选，需提前调用 get_long_term_summary_async）

        Returns:
            格式化的上下文字符串
        """
        lines = []

        # 长期记忆总结（历史会话）
        if long_term_summary:
            lines.append("【历史会话总结】")
            lines.append(long_term_summary)
            lines.append("")

        # 用户偏好
        prefs = self.long_term.get_preference()
        has_prefs = any(v for v in prefs.values() if v)
        if has_prefs:
            lines.append("【用户偏好】")
            for key, value in prefs.items():
                if value:
                    lines.append(f"- {key}: {value}")
            lines.append("")

        # 短期记忆（当前会话）
        context_str = self.short_term.get_context_string(3)
        if context_str != "无历史对话":
            lines.append("【当前会话对话】")
            lines.append(context_str)
            lines.append("")

        return "\n".join(lines) if lines else "无上下文信息"

    # ========== 会话管理 ==========

    def end_session(self):
        """结束会话"""
        self.short_term.clear()
        logger.info(f"Session ended: {self.session_id}")

    async def get_long_term_summary_async(self, max_messages: int = 50) -> str:
        """
        使用LLM总结长期聊天历史（异步版本）

        Args:
            max_messages: 最多总结的消息数量

        Returns:
            总结后的文本
        """
        if not self.llm_model:
            return ""

        # 获取长期聊天历史（排除当前会话）。只取最后 max_messages 条会被
        # 当前会话自身占满（长会话几十轮后其它会话历史永远为空），因此按
        # 「当前会话消息数 + max_messages」取窗口，保证至少有 max_messages
        # 条其它会话的消息可供总结。
        own_messages = len(
            self.long_term.get_chat_history(session_id=self.session_id)
        )
        all_history = self.long_term.get_chat_history(
            limit=max_messages + own_messages
        )
        history_from_other_sessions = [
            msg for msg in all_history
            if msg.get("session_id") != self.session_id
        ]

        # 获取行程历史
        trip_history = self.long_term.get_trip_history(limit=20)

        # 如果既没有聊天记录也没有行程记录，直接返回
        if not history_from_other_sessions and not trip_history:
            return ""

        # 同一会话内其它会话的历史 / 行程未变化时复用上次总结，避免每轮多一次 LLM 调用。
        # 行程的编辑（改日期 / 目的）不会更新时间戳，指纹必须包含字段内容，
        # 否则修改行程后仍会注入旧总结。
        trip_signature = tuple(
            (
                str(trip.get("trip_id") or ""),
                str(trip.get("start_date") or ""),
                str(trip.get("end_date") or ""),
                str(trip.get("purpose") or ""),
            )
            for trip in trip_history
        )
        fingerprint = (
            len(history_from_other_sessions),
            str(history_from_other_sessions[-1].get("timestamp", ""))
            if history_from_other_sessions
            else "",
            len(trip_history),
            str(trip_history[-1].get("timestamp", "")) if trip_history else "",
            trip_signature,
        )
        if self._summary_cache is not None and self._summary_cache[0] == fingerprint:
            return self._summary_cache[1]

        # 构建聊天记录文本
        history_text = []
        for msg in history_from_other_sessions[-max_messages:]:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if role == "assistant":
                content = strip_turn_meta(str(content))
            timestamp = msg.get("timestamp", "")
            history_text.append(f"[{timestamp}] {role}: {content}")

        history_str = "\n".join(history_text) if history_text else "（无聊天记录）"

        # 构建行程历史文本
        trip_text = []
        for trip in trip_history:
            origin = trip.get("origin", "未知")
            destination = trip.get("destination", "未知")
            start_date = trip.get("start_date", "")
            end_date = trip.get("end_date", "")
            purpose = trip.get("purpose", "旅游")
            timestamp = trip.get("timestamp", "")

            if start_date and end_date:
                trip_text.append(f"[{timestamp}] {origin} → {destination} ({start_date} 至 {end_date}) - {purpose}")
            elif start_date:
                trip_text.append(f"[{timestamp}] {origin} → {destination} ({start_date}) - {purpose}")
            else:
                trip_text.append(f"[{timestamp}] {origin} → {destination} - {purpose}")

        trip_str = "\n".join(trip_text) if trip_text else "（无行程记录）"

        # 使用LLM总结
        summarization_prompt = f"""请总结以下历史信息中的关键内容，包括：
1. 用户的旅行偏好和习惯
2. 用户询问过的重要问题
3. 用户的出行历史和目的地
4. 其他重要的上下文信息

【历史聊天记录】
{history_str}

【历史行程记录】
{trip_str}

请用简洁的语言总结（不超过200字）："""

        try:
            # 调用模型（异步调用）
            response = await self.llm_model([{"role": "user", "content": summarization_prompt}])

            # 处理模型响应（兼容非流式 ChatResponse / 流式生成器）
            summary = await extract_json_from_async_response(response)

            summary = summary.strip()
            if summary:
                self._summary_cache = (fingerprint, summary)
            logger.info(f"Generated long-term memory summary ({len(summary)} chars)")
            return summary

        except Exception as e:
            logger.error(f"Failed to generate long-term summary: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return ""

    def get_long_term_summary(self, max_messages: int = 50) -> str:
        """
        使用LLM总结长期聊天历史（同步版本）

        Args:
            max_messages: 最多总结的消息数量

        Returns:
            总结后的文本
        """
        import asyncio

        # 检查是否在事件循环中
        try:
            asyncio.get_running_loop()
            # 已经在事件循环中，不能使用 asyncio.run
            logger.warning("get_long_term_summary called from async context, please use get_long_term_summary_async instead")
            return ""
        except RuntimeError:
            # 没有运行的事件循环，可以使用 asyncio.run
            return asyncio.run(self.get_long_term_summary_async(max_messages))
