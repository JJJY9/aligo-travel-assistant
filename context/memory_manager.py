"""
记忆管理器 (Memory Manager)
统一管理两层记忆，提供简单的API
"""
import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .long_term_memory import LongTermMemory
from .storage import create_long_term_memory, create_short_term_memory
from config import STORAGE_CONFIG
from utils import tombstones
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

    def session_epoch(self) -> tuple[bool, str | None]:
        """当前会话纪元快照 ``(是否读到, 值)``（删除时单调递增）。

        ``(False, None)`` 表示 Redis 未配置 / 失败冷却 / 读取故障——不能与
        「确实未删除」(True, None) 混为一谈。
        """
        return tombstones.get_session_epoch_state(self.user_id, self.session_id)

    def persist_guard(
        self,
        epoch_at_start: tuple[bool, str | None] | None,
        base: Callable[[], bool] | None = None,
    ) -> Callable[[], bool]:
        """组合落库守卫：墓碑（base / 默认共享墓碑）与「纪元未变」同时成立。

        墓碑只保留 1 小时，且「从未存在 / 并发删除」分支会撤销墓碑——删除前
        启动的长回合可能在墓碑撤销后才落库。纪元在每次删除时单调递增且
        不回退：回合开始时记下的值与提交时不同即说明本轮开始后会话被删过，
        无论墓碑是否还在都否决写入。

        纪元只有「两次都读到了」时才有比较意义：一次瞬时 Redis 故障会让
        读值不可得，按相等与否硬判会误否决未删除回合（用户看到「本轮未
        保存」）或误放行已删除会话。读不到时退回墓碑守卫的结论（与引入
        纪元前的行为一致），并把「无法校验」留在日志里。
        """

        def guard() -> bool:
            if base is not None:
                if not base():
                    return False
            elif tombstones.is_session_deleted(self.user_id, self.session_id):
                return False
            if epoch_at_start is None:
                return True
            start_available, start_value = epoch_at_start
            now_available, now_value = tombstones.get_session_epoch_state(
                self.user_id, self.session_id
            )
            if not start_available or not now_available:
                logger.warning(
                    "会话纪元不可读（开始=%s/提交=%s），本轮按墓碑结果放行：%s",
                    start_available,
                    now_available,
                    self.session_id,
                )
                return True
            return start_value == now_value

        return guard

    def persist_turn(
        self,
        user_content: str,
        assistant_content: str,
        should_commit: Callable[[], bool] | None = None,
    ) -> bool:
        """一次性提交整轮问答（用户 + 助手）到长期记忆。

        ``persist_message("user")`` 与 ``add_message("assistant")`` 分两次
        调用时，两次之间被取消（用户停止 / 断网）或第二次写失败会在长期
        记忆留下没有回答的孤儿提问。这里委托给后端在同一把写锁 / 同一个
        事务里写入两条消息，短期记忆只补助手副本（用户消息已在编排前
        通过 :meth:`stage_message` 暂存）。

        ``should_commit`` 由存储层在写入锁 / 事务内调用（会话刚被删除时
        返回 False）：整轮被跳过时助手副本也不再进短期记忆。调用方未注入
        守卫时（CLI 直接调用，没有 ConversationRuntime 的注入点）默认回退
        到共享删除墓碑——Web 面板删除的会话不会被 CLI 的在途轮次写回。

        Returns:
            是否真的写入长期记忆
        """
        if getattr(self.long_term, "snapshot_based", True):
            self._rebuild_long_term()
        if should_commit is None:
            # 无 Redis 共享通道时 is_session_deleted 快速返回 False（不同
            # 进程之间没有共享通道，也就不存在跨实例删除），行为与此前一致
            def should_commit() -> bool:
                return not tombstones.is_session_deleted(
                    self.user_id, self.session_id
                )
        committed = self.long_term.add_chat_turn(
            user_content,
            assistant_content,
            self.session_id,
            should_commit=should_commit,
        )
        if committed is False:
            logger.info(
                "会话在落库前被删除，跳过本轮写入：%s", self.session_id
            )
            return False
        self.stage_message("assistant", assistant_content)
        return True

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

    def rollback_staged_message(self, content: str) -> bool:
        """编排失败 / 取消时移除编排前暂存的用户提问（尽力而为）。

        仅当短期记忆末尾仍是这条未回答的用户消息时才删除：编排失败后
        若保留，重试同一问题会在上下文里出现两条重复提问；长期记忆里
        却没有对应轮次，两层记忆从此不一致。删除走与「删除最后一轮」
        相同的实现（Redis 端原子脚本 + 故障期日志重放），不会误删其它
        实例已写入的轮次。

        Returns:
            是否真的删除了暂存的提问
        """
        try:
            recent = self.short_term.get_recent_context()
            if not recent:
                return False
            last = recent[-1]
            if not isinstance(last, dict):
                return False
            if last.get("role") != "user" or last.get("content") != content:
                return False
            return bool(self.short_term.truncate_last_turn(expect=content))
        except Exception as exc:  # noqa: BLE001 - 回滚失败不能掩盖原始错误
            logger.warning("回滚暂存提问失败（%s），短期上下文可能残留本轮提问", exc)
            return False

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
        snapshot = getattr(self.long_term, "export_snapshot", None)
        if callable(snapshot):
            data = snapshot()
        else:
            # 未知后端（测试替身等）：逐项读取，保持旧行为
            data = {
                "preferences": self.long_term.get_preference(),
                "trip_history": self.long_term.get_trip_history(limit=None),
                "chat_history": self.long_term.get_chat_history(),
                "session_titles": self.long_term.get_session_titles(),
                "pinned_sessions": self.long_term.get_pinned_sessions(),
                "archived_sessions": self.long_term.get_archived_sessions(),
                "statistics": self.long_term.get_statistics(),
            }
        return {
            "user_id": self.user_id,
            "session_id": self.session_id,
            "exported_at": datetime.now().isoformat(),
            **data,
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

        跨会话消息在存储层直接按「排除当前会话 + 固定窗口」取数：先取全部
        历史再在 Python 里过滤会让每一步的查询量随当前会话长度线性增长
        （PostgreSQL 每条消息都带完整 content），长会话几十轮后总结的每轮
        开销越来越大。
        """
        history_from_other_sessions = self.long_term.get_chat_history(
            limit=_SUMMARY_WINDOW_MESSAGES,
            exclude_session_id=self.session_id,
        )
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
