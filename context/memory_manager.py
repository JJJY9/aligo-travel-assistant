"""
记忆管理器 (Memory Manager)
统一管理两层记忆，提供简单的API
"""
import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from .long_term_memory import LongTermMemory
from .storage import create_long_term_memory, create_short_term_memory
from config import STORAGE_CONFIG
from utils.json_parser import extract_json_from_async_response
from utils.preference_values import has_preference_value
from utils.turn_meta import strip_turn_meta

logger = logging.getLogger(__name__)

# 跨会话总结的输入预算：长期记忆里的助手消息是完整编排 JSON，
# 不设上限会把模型上下文撑爆，导致总结永久失败
_SUMMARY_MESSAGE_MAX_CHARS = 1200
_SUMMARY_TOTAL_MAX_CHARS = 24000
_SUMMARY_FAILURE_COOLDOWN_SEC = 60.0
# 跨会话总结的固定窗口：CLI（50）、对话与 memory-query 技能（30）等不同
# 调用方必须共用同一份缓存，否则同一轮里不同 max_messages 会让缓存互相
# 失效、每次记忆查询都重复调用一次总结 LLM
_SUMMARY_WINDOW_MESSAGES = 50


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
        # 总结失败后的短时冷却：避免模型持续报错时每轮都重试
        self._summary_failure_until = 0.0

        logger.info(f"Memory manager initialized for user {user_id}, session {session_id}")

    def _rebuild_long_term(self) -> None:
        """重建长期记忆实例。

        PostgreSQL 故障期间工厂会回退本地 JSON，恢复后下一次重建会切回 PG；
        故障窗口内写入 JSON 的消息 / 偏好不会自动合并，这里明确记录警告，
        避免用户以为数据丢失（文件仍在 data/memory/<user>.json）。
        """
        previous = self.long_term
        new_store = create_long_term_memory(self.user_id, self._storage_path)
        if (
            isinstance(previous, LongTermMemory)
            and not isinstance(new_store, LongTermMemory)
            and STORAGE_CONFIG.get("postgres_dsn")
        ):
            logger.warning(
                "PostgreSQL 已恢复：故障期间写入本地 JSON 的记忆"
                "（data/memory/%s.json）不会自动合并到 PostgreSQL",
                self.user_id,
            )
        self.long_term = new_store

    # ========== 短期记忆操作 ==========

    def persist_message(self, role: str, content: str) -> None:
        """只写长期记忆（跨会话持久化）。

        JSON 后端持有内存快照：写入前重新加载，避免本轮对话期间通过记忆
        面板写入的标题 / 置顶 / 偏好被旧快照覆盖；PostgreSQL 直接查询，
        无需重载。
        """
        if getattr(self.long_term, "snapshot_based", True):
            self._rebuild_long_term()
        self.long_term.add_chat_message(role, content, self.session_id)

    def stage_message(
        self, role: str, content: str, metadata: dict | None = None
    ) -> None:
        """只写短期记忆（上下文缓存，不落长期记忆）。

        交互式 CLI 在编排前用它暂存本轮提问：编排智能体从短期记忆读取
        「用户本轮原话」；编排失败时这条只是上下文缓存，长期记忆里不会
        留下没有回复的孤儿用户轮。
        """
        try:
            self.short_term.add_message(role, content, metadata)
        except Exception as exc:  # noqa: BLE001 - 缓存失败降级为缺少本轮上下文
            logger.warning("短期记忆写入失败（%s），本轮上下文可能缺少该消息", exc)

    def add_message(self, role: str, content: str, metadata: dict | None = None):
        """
        添加消息到短期记忆和长期记忆

        Args:
            role: 角色 (user/assistant)
            content: 消息内容
            metadata: 元数据

        先写长期、再写短期：长期失败时短期记忆不会留下一条永远不会被
        删除的孤立提问。
        """
        self.persist_message(role, content)
        self.stage_message(role, content, metadata)

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
        self._rebuild_long_term()

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
        has_prefs = any(has_preference_value(v) for v in prefs.values())
        if has_prefs:
            lines.append("【用户偏好】")
            for key, value in prefs.items():
                if has_preference_value(value):
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

    def _summary_inputs(self) -> tuple[list[dict], list[dict]]:
        """同步读取跨会话总结的输入（聊天窗口 + 行程），供 ``to_thread`` 调用。

        只取最后固定窗口条会被当前会话自身占满（长会话几十轮后其它会话
        历史永远为空），因此按「当前会话消息数 + 窗口」取数，保证至少有
        窗口条其它会话的消息。
        """
        own_messages = len(
            self.long_term.get_chat_history(session_id=self.session_id)
        )
        all_history = self.long_term.get_chat_history(
            limit=_SUMMARY_WINDOW_MESSAGES + own_messages
        )
        history_from_other_sessions = [
            msg for msg in all_history
            if msg.get("session_id") != self.session_id
        ]
        trip_history = self.long_term.get_trip_history(limit=20)
        return history_from_other_sessions, trip_history

    async def get_long_term_summary_async(self, max_messages: int = 50) -> str:
        """
        使用LLM总结长期聊天历史（异步版本）

        Args:
            max_messages: 兼容旧接口的提示值；总结窗口固定为
                ``_SUMMARY_WINDOW_MESSAGES``，保证 CLI / 对话 / memory-query
                技能等不同调用方共享同一份缓存（不同窗口会让缓存互相失效）

        Returns:
            总结后的文本
        """
        if not self.llm_model:
            return ""

        # 失败冷却先检查：冷却期内直接返回，避免每轮都为了几秒后必然
        # 被丢弃的结果做一遍阻塞查询
        if time.monotonic() < self._summary_failure_until:
            return ""

        # 聊天窗口 / 行程查询是阻塞 IO（JSON 文件、PostgreSQL 网络往返），
        # 放到线程池执行：留在事件循环线程会冻结同进程所有 SSE 流，
        # 即使随后命中总结缓存或走空历史提前返回
        history_from_other_sessions, trip_history = await asyncio.to_thread(
            self._summary_inputs
        )

        # 如果既没有聊天记录也没有行程记录，直接返回
        if not history_from_other_sessions and not trip_history:
            return ""

        # 指纹要能感知「其它会话的消息被删除后由更旧消息补位」：
        # 条数与最后时间戳可能都不变，必须比较窗口内每条消息的身份
        window = history_from_other_sessions[-_SUMMARY_WINDOW_MESSAGES:]
        history_signature = tuple(
            (
                str(item.get("session_id") or ""),
                str(item.get("timestamp") or ""),
                str(item.get("role") or ""),
                len(str(item.get("content") or "")),
            )
            for item in window
        )

        # 同一会话内其它会话的历史 / 行程未变化时复用上次总结，避免每轮多一次 LLM 调用。
        # 行程的编辑（改日期 / 目的）不会更新时间戳，指纹必须包含字段内容，
        # 否则修改行程后仍会注入旧总结。
        trip_signature = tuple(
            (
                str(trip.get("trip_id") or ""),
                str(trip.get("origin") or ""),
                str(trip.get("destination") or ""),
                str(trip.get("start_date") or ""),
                str(trip.get("end_date") or ""),
                str(trip.get("purpose") or ""),
            )
            for trip in trip_history
        )
        fingerprint = (
            len(window),
            history_signature,
            len(trip_history),
            str(trip_history[-1].get("timestamp", "")) if trip_history else "",
            trip_signature,
        )
        if self._summary_cache is not None and self._summary_cache[0] == fingerprint:
            return self._summary_cache[1]

        # 构建聊天记录文本：单条截断 + 总字符预算，按时间倒序装入后再反转，
        # 保证预算不足时保留的是较新的历史
        history_text: list[str] = []
        budget = _SUMMARY_TOTAL_MAX_CHARS
        for msg in reversed(window):
            role = msg.get("role", "unknown")
            content = str(msg.get("content", ""))
            if role == "assistant":
                content = strip_turn_meta(content)
            if len(content) > _SUMMARY_MESSAGE_MAX_CHARS:
                content = content[:_SUMMARY_MESSAGE_MAX_CHARS] + "…"
            timestamp = msg.get("timestamp", "")
            line = f"[{timestamp}] {role}: {content}"
            if history_text and len(line) > budget:
                break
            history_text.append(line)
            budget -= len(line)
            if budget <= 0:
                break
        history_text.reverse()

        history_str = "\n".join(history_text) if history_text else "（无聊天记录）"

        # 构建行程历史文本（字段存在但为 null 时不能输出字面量 None）
        trip_text = []
        for trip in trip_history:
            origin = str(trip.get("origin") or "未知")
            destination = str(trip.get("destination") or "未知")
            start_date = str(trip.get("start_date") or "")
            end_date = str(trip.get("end_date") or "")
            purpose = str(trip.get("purpose") or "旅游")
            timestamp = str(trip.get("timestamp") or "")

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
                self._summary_failure_until = 0.0
            else:
                # 网关 HTTP 200 但 choices 为空 / 纯空白：与异常同样进入冷却，
                # 否则每个回合都会重复调用一次总结 LLM，且跨会话总结始终为空
                self._summary_failure_until = (
                    time.monotonic() + _SUMMARY_FAILURE_COOLDOWN_SEC
                )
                logger.warning("Long-term summary is empty; cooldown applied")
            logger.info(f"Generated long-term memory summary ({len(summary)} chars)")
            return summary

        except Exception as e:
            self._summary_failure_until = (
                time.monotonic() + _SUMMARY_FAILURE_COOLDOWN_SEC
            )
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
        # 检查是否在事件循环中
        try:
            asyncio.get_running_loop()
            # 已经在事件循环中，不能使用 asyncio.run
            logger.warning("get_long_term_summary called from async context, please use get_long_term_summary_async instead")
            return ""
        except RuntimeError:
            # 没有运行的事件循环，可以使用 asyncio.run
            return asyncio.run(self.get_long_term_summary_async(max_messages))
