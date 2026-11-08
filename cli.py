#!/usr/bin/env python
"""
Aligo 商旅助手 - CLI 交互界面
使用 Rich 库实现美观的终端交互
"""
import asyncio
import contextlib
import os
import re
import signal
import sys
from typing import Any

# 添加项目根目录到路径
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_root)

import json

# 轻量组件直接导入；运行时组件（agentscope / rich / 模型）单独保护：
# doctor / skills 等诊断命令必须在这些依赖缺失时仍能启动并如实报告缺失项
from config import LLM_CONFIG, RESILIENCE_CONFIG, SYSTEM_CONFIG
from utils.circuit_breaker import CircuitBreaker, CircuitOpenError
from utils.console import PipeSafeTextIO, configure_stdio, is_pipe_error
from utils.errors import MemoryPersistenceError
from utils.llm_resilience import retry_with_backoff
from utils.llm_resilience import run_health_check as check_llm_health
from utils.preference_values import has_preference_value
from utils.progress import format_progress_event
from utils.timestamps import chronological_key
from utils.turn_meta import attach_turn_meta, build_turn_meta
from utils.version import APP_VERSION

_RUNTIME_IMPORT_ERROR: Exception | None = None
try:
    from rich.console import Console
    from rich.markup import escape
    from rich.prompt import Prompt
    from rich.table import Table

    from agents.intention_agent import IntentionAgent
    from agents.orchestration_agent import OrchestrationAgent
    from config_agentscope import create_chat_model, init_agentscope
    from context.memory_manager import MemoryManager
except Exception as exc:  # noqa: BLE001 - 依赖缺失时诊断命令仍要能运行
    _RUNTIME_IMPORT_ERROR = exc
    Console = escape = Prompt = Table = None  # type: ignore[assignment,misc]
    IntentionAgent = OrchestrationAgent = None  # type: ignore[assignment,misc]
    create_chat_model = init_agentscope = None  # type: ignore[assignment]
    MemoryManager = None  # type: ignore[assignment,misc]

# 移除其他智能体的导入，改用懒加载


class AligoCLI:
    """Aligo 商旅助手 CLI"""

    def __init__(self):
        """初始化 CLI"""
        # stdout 走管道安全包装：`| head` 提前关闭后 Rich 写入静默丢弃，
        # 不再在错误分支二次抛错打出 traceback（退出时还原见 run()）
        self.console = Console(file=PipeSafeTextIO(sys.stdout))
        self.user_id = None
        self.session_id = None
        self.memory_manager = None
        self.orchestrator = None
        self.intention_agent = None
        self.model = None
        self._agent_cache = {}  # 智能体缓存
        self.circuit_breaker = None  # 在 initialize_system 中从 RESILIENCE_CONFIG 初始化

    def print_banner(self):
        """打印欢迎横幅"""
        self.console.print("\n[bold cyan]🌏 Aligo 商旅助手[/bold cyan] - 让差旅更简单\n", style="bold")

    def print_help(self):
        """打印帮助信息"""
        table = Table(title="命令列表", show_header=True, header_style="bold magenta")
        table.add_column("命令", style="cyan", width=20)
        table.add_column("说明", style="white")

        table.add_row("help", "显示此帮助信息")
        table.add_row("status", "查看当前状态和记忆")
        table.add_row("health", "检查 LLM 服务是否可用")
        table.add_row("doctor", "环境自检（依赖 / 密钥 / 模型 / 知识库 / 连通性）")
        table.add_row("clear", "清空当前任务（保留长期记忆）")
        table.add_row("history", "查看历史行程")
        table.add_row("preferences", "查看用户偏好")
        table.add_row("export", "导出用户数据（JSON + Markdown，保存到 exports/）")
        table.add_row("exit", "退出程序")
        table.add_row("", "")
        table.add_row("[自然语言]", "直接输入您的需求，如：")
        table.add_row("", "  - 我要从上海去北京出差")
        table.add_row("", "  - 北京的住宿标准是多少")
        table.add_row("", "  - 查询明天的天气")

        self.console.print(table)

    def _prompt_user_id(self) -> str:
        """交互式询问用户 ID，并按与 Web 相同的规则校验。"""
        import re

        while True:
            try:
                value = (
                    Prompt.ask("用户ID", default="default_user").strip()
                    or "default_user"
                )
            except EOFError:
                # 管道 / IDE 运行到末尾：退出而不是抛出 traceback 后死循环
                print()
                print("未检测到输入，已退出")
                raise SystemExit(0) from None
            except (RuntimeError, ValueError, OSError) as exc:
                print(f"无法读取输入（{exc}），请改用非交互子命令或接入终端后重试")
                raise SystemExit(1) from None
            if re.fullmatch(r"[\w-]{1,64}", value) and not value.startswith("-"):
                return value
            self.console.print(
                "[red]用户 ID 只能包含中文、字母、数字、下划线和短横线[/red]"
            )

    async def initialize_system(self):
        """初始化系统 - 使用懒加载优化启动速度"""
        # 获取用户信息
        self.user_id = self._prompt_user_id()

        # 生成会话ID
        import uuid
        self.session_id = str(uuid.uuid4())[:8]

        with self.console.status("初始化中...", spinner="dots"):
            # 初始化AgentScope
            init_agentscope()

            # 初始化模型
            if not LLM_CONFIG["api_key"]:
                self.console.print(
                    "[bold red]⚠ 未检测到 API Key，请设置环境变量 LLM_API_KEY，"
                    '或创建 config_local.py（内容：API_KEY = "sk-..."）[/bold red]'
                )
                # 空 api_key 构造模型会抛 OpenAIError（未捕获 traceback 把整个
                # 进程打挂）：干净退出；无需模型的维护命令请直接用子命令
                self.console.print(
                    "[dim]未配置密钥无法进入交互模式；维护类命令请直接运行 "
                    "python cli.py <命令>（如 doctor / sessions / trips）。[/dim]"
                )
                raise SystemExit(2)
            timeout_sec = SYSTEM_CONFIG.get("timeout", 60)
            try:
                self.model = create_chat_model(timeout=timeout_sec)
            except Exception as exc:  # noqa: BLE001 - 配置/网络问题给出清晰提示
                self.console.print(
                    f"[bold red]❌ 模型初始化失败：{exc}[/bold red]"
                )
                raise SystemExit(1) from None

            # 初始化记忆管理器（传入LLM模型用于总结）
            self.memory_manager = MemoryManager(
                user_id=self.user_id,
                session_id=self.session_id,
                llm_model=self.model
            )

            # 初始化意图识别智能体（必须预加载）
            self.intention_agent = IntentionAgent(
                name="IntentionAgent",
                model=self.model
            )

            # 使用懒加载注册器（智能体在首次使用时才加载）
            from agents.lazy_agent_registry import LazyAgentRegistry
            self._agent_cache = {}
            lazy_registry = LazyAgentRegistry(
                model=self.model, 
                cache=self._agent_cache,
                memory_manager=self.memory_manager
            )

            # 预先加载关键智能体（可选，利用 preload）
            # lazy_registry.preload("memory_query", "preference")

            # 初始化协调器
            self.orchestrator = OrchestrationAgent(
                name="OrchestrationAgent",
                agent_registry=lazy_registry,
                memory_manager=self.memory_manager
            )

            # 熔断器（连接与可用性）
            rc = RESILIENCE_CONFIG
            self.circuit_breaker = CircuitBreaker(
                failure_threshold=rc.get("circuit_failure_threshold", 5),
                recovery_timeout_sec=rc.get("circuit_recovery_timeout_sec", 60.0),
                half_open_successes=rc.get("circuit_half_open_successes", 2),
            )

        self.console.print(f"✓ 就绪 (用户: {self.user_id}) - 输入 help 查看帮助\n", style="green")

    async def process_query(self, user_input: str):
        """
        处理用户查询（原逻辑保留；仅在入口加熔断检查、对 LLM 调用加重试）
        """
        import time
        start_time = time.time()
        usage_before = self.model.usage.snapshot() if hasattr(self.model, "usage") else None

        # ---------- 仅新增：熔断检查 ----------
        if self.circuit_breaker:
            try:
                self.circuit_breaker.raise_if_open()
            except CircuitOpenError:
                self.console.print(
                    "\n[bold yellow]⚠ 服务暂时不可用，请稍后再试。[/bold yellow]\n",
                    style="dim"
                )
                return

        # 半开试探名额是否已被本轮消费（记录过成功 / 失败）。真正调用 LLM
        # 之前失败或 Ctrl-C（CancelledError 不经过 record_*）时在 finally 里
        # 归还名额，否则后续查询会被熔断拒绝最多 recovery_timeout 秒
        probe_recorded = False

        try:
            rc = RESILIENCE_CONFIG
            max_retries = int(rc.get("max_retries", 3))

            # 每轮对话前重载长期记忆（与 Web 端一致）：记忆面板 / 其它进程刚写入的
            # 标题、偏好、行程必须进入本轮上下文，本进程的旧快照也不能在写回时
            # 覆盖它们
            await asyncio.to_thread(self.memory_manager.reload_long_term)

            with self.console.status("正在理解需求…", spinner="dots"):
                from agentscope.message import Msg

                # 1. 获取长期记忆摘要与上下文（原逻辑不变）
                long_term_summary = await self._get_long_term_summary(user_input)
                recent_context = self.memory_manager.short_term.get_recent_context(n_turns=5)
                context_messages = []
                if long_term_summary:
                    context_messages.append(Msg(name="system", content=long_term_summary, role="system"))
                for msg in recent_context:
                    role = msg.get("role") if isinstance(msg, dict) else None
                    content = msg.get("content") if isinstance(msg, dict) else None
                    if role not in ("user", "assistant", "system"):
                        continue
                    if not isinstance(content, str):
                        continue
                    context_messages.append(Msg(name=role, content=content, role=role))
                context_messages.append(Msg(name="user", content=user_input, role="user"))

                # 2. 意图识别（仅此调用加重试，原逻辑不变）
                intention_result = None
                try:
                    intention_result = await retry_with_backoff(
                        lambda: self.intention_agent.reply(context_messages),
                        max_retries=max_retries,
                        base_delay_sec=rc.get("retry_base_delay_sec", 1.0),
                        max_delay_sec=rc.get("retry_max_delay_sec", 30.0),
                    )
                    if self.circuit_breaker:
                        self.circuit_breaker.record_success()
                        probe_recorded = True
                except CircuitOpenError:
                    raise
                except MemoryPersistenceError:
                    # 记忆写盘失败与 LLM 健康无关，不计入熔断（与 ConversationRuntime 一致）
                    raise
                except Exception:
                    if self.circuit_breaker:
                        self.circuit_breaker.record_failure()
                        probe_recorded = True
                    raise

                # 3. 解析意图识别结果（原逻辑不变：解析失败则友好提示并 return）
                try:
                    intention_data = json.loads(intention_result.content)
                except json.JSONDecodeError:
                    self.console.print("❌ 无法理解您的需求，请重新描述", style="bold red")
                    return

            # 4. 本轮提问先只进短期记忆：编排智能体从短期记忆读取「用户本轮
            # 原话」（与 Web 的 ConversationRuntime 一致）；长期记忆仍在本轮
            # 成功后落库，编排失败 / Ctrl-C 时不留无回复的孤儿用户轮
            self.memory_manager.stage_message("user", user_input)

            # 5. 调度智能体（实时显示智能体进度）
            orchestration_result = None
            try:
                with self.console.status("正在调度智能体…", spinner="dots") as orchestration_status:

                    def on_progress(event: dict) -> None:
                        text = format_progress_event(event, self._get_agent_display_name)
                        if text:
                            orchestration_status.update(text)

                    orchestration_result = await retry_with_backoff(
                        lambda: self.orchestrator.reply(intention_result, on_event=on_progress),
                        max_retries=max_retries,
                        base_delay_sec=rc.get("retry_base_delay_sec", 1.0),
                        max_delay_sec=rc.get("retry_max_delay_sec", 30.0),
                    )
                if self.circuit_breaker:
                    self.circuit_breaker.record_success()
                    probe_recorded = True
            except CircuitOpenError:
                raise
            except MemoryPersistenceError:
                # 记忆写盘失败与 LLM 健康无关，不计入熔断（与 ConversationRuntime 一致）
                raise
            except Exception:
                if self.circuit_breaker:
                    self.circuit_breaker.record_failure()
                    probe_recorded = True
                raise

            # 6. 解析执行结果（原逻辑不变）
            try:
                result_data = json.loads(orchestration_result.content)
            except json.JSONDecodeError:
                result_data = {"error": "解析结果失败"}

            # 7. 显示调用的智能体与最终结果（原逻辑不变）
            self._display_agents_called(result_data)
            self.console.print()
            self._display_results(result_data)

            # 8. 本次查询的 LLM 用量与耗时（模型层统一统计）
            usage_delta = None
            elapsed = time.time() - start_time
            if usage_before is not None:
                after = self.model.usage.snapshot()
                usage_delta = {key: after[key] - usage_before[key] for key in usage_before}
                delta_calls = usage_delta["calls"]
                if delta_calls > 0:
                    self.console.print(
                        f"[dim]📊 本次调用 LLM {delta_calls} 次 · "
                        f"tokens {usage_delta['total_tokens']}"
                        f"（输入 {usage_delta['input_tokens']} / "
                        f"输出 {usage_delta['output_tokens']}）· "
                        f"总耗时 {elapsed:.1f}s[/dim]"
                    )

            # 9. 写入长期记忆（附带执行元数据，Web 端历史会话可回看执行路线）；
            # 用户消息的短期副本已在编排前暂存，这里只补长期记忆，避免重复
            meta = build_turn_meta(
                intention_data if isinstance(intention_data, dict) else {},
                result_data,
                usage=usage_delta,
                elapsed=elapsed,
            )
            self.memory_manager.persist_turn(
                user_input,
                json.dumps(attach_turn_meta(result_data, meta), ensure_ascii=False),
            )
        finally:
            if self.circuit_breaker and not probe_recorded:
                self.circuit_breaker.release_probe()

    def _display_agents_called(self, result_data: dict):
        """显示调用的智能体列表"""
        results = result_data.get("results", [])
        if not results:
            return

        # 收集所有调用的智能体
        agents_called = []
        for result in results:
            agent_name = result.get("agent_name", "")
            status = result.get("status", "")

            display_name = self._get_agent_display_name(agent_name)
            duration = result.get("duration")
            suffix = f" {float(duration):.1f}s" if isinstance(duration, (int, float)) else ""

            # 根据状态添加标记
            if status == "success":
                agents_called.append(f"{display_name} ✓{suffix}")
            elif status == "error":
                agents_called.append(f"{display_name} ✗{suffix}")
            else:
                agents_called.append(f"{display_name} ?{suffix}")

        if agents_called:
            self.console.print()
            self.console.print(
                f"🤖 调用智能体: {escape(', '.join(agents_called))}",
                style="dim",
            )

    def _display_results(self, result_data: dict):
        """显示执行结果 - 确保永远有回复"""
        self.console.print()

        # 获取结果列表
        results = result_data.get("results", [])

        if not results:
            # 情况1: 没有任何智能体被调用
            status = result_data.get("status", "unknown")
            if status == "no_agents":
                self.console.print("✓ 好的，我已记录下来。", style="green")
                self.console.print("\n💡 您可以继续补充信息，或者尝试：", style="dim")
                self.console.print("  • 规划行程：「帮我规划去北京的行程」", style="dim")
                self.console.print("  • 查询信息：「北京的天气怎么样」", style="dim")
                self.console.print("  • 问问题：「差旅标准是多少」", style="dim")
            else:
                self.console.print("未能获取有效结果，请重新描述您的需求。", style="yellow")
        else:
            # 情况2: 有智能体被调用，生成人性化回复
            has_output = self._generate_human_response(results)

            # 情况3: 智能体执行了但没有显示内容（兜底）
            if not has_output:
                self.console.print("✓ 已处理您的请求。", style="green")

        self.console.print()

    async def _get_long_term_summary(self, user_input: str = "") -> str:
        """
        生成长期记忆摘要，用于传递给IntentionAgent
        使用LLM总结历史聊天记录 + 结构化偏好

        Args:
            user_input: 用户输入，用于筛选相关历史行程

        Returns:
            格式化的长期记忆摘要
        """
        summary_parts = []

        # 1. 用户偏好信息（始终加载）
        prefs = self.memory_manager.long_term.get_preference()
        if prefs:
            pref_lines = ["【用户背景信息】（来自长期记忆，可用于推断缺失信息）"]

            # 遍历所有偏好，全部加载
            for pref_key, pref_value in prefs.items():
                if has_preference_value(pref_value):  # 0 / False 也是合法偏好
                    # 如果是列表，用逗号连接
                    if isinstance(pref_value, list):
                        items = [
                            str(item)
                            for item in pref_value
                            if not (
                                item is None
                                or (isinstance(item, str) and not item.strip())
                            )
                        ]
                        pref_lines.append(f"• {pref_key}: {', '.join(items)}")
                    else:
                        pref_lines.append(f"• {pref_key}: {pref_value}")

            # 只有在有具体偏好内容时才添加
            if len(pref_lines) > 1:
                summary_parts.extend(pref_lines)

        # 2. 使用LLM总结历史聊天记录
        chat_summary = await self.memory_manager.get_long_term_summary_async(max_messages=50)
        if chat_summary:
            summary_parts.append("\n【历史会话总结】")
            summary_parts.append(chat_summary)

        # 3. 智能筛选相关历史行程
        all_trips = self.memory_manager.long_term.get_trip_history(limit=None)
        if all_trips:
            # 筛选相关的行程（地点匹配）
            relevant_trips = []
            other_trips = []

            for trip in all_trips:
                origin = str(trip.get("origin") or "")
                destination = str(trip.get("destination") or "")

                # 如果用户输入提到了这个行程的地点，标记为相关
                if (origin and origin in user_input) or (destination and destination in user_input):
                    relevant_trips.append(trip)
                else:
                    other_trips.append(trip)

            # 优先显示相关的，再补充最近的
            trips_to_show = relevant_trips[:2] + other_trips[:1]  # 2条相关 + 1条最近

            if trips_to_show:
                summary_parts.append("\n【历史行程】")
                for i, trip in enumerate(trips_to_show[:3], 1):
                    origin = trip.get("origin", "未知")
                    destination = trip.get("destination", "未知")
                    start_date = trip.get("start_date", "")
                    purpose = trip.get("purpose", "")

                    # 标记相关性
                    relevance_mark = "✦ " if trip in relevant_trips else ""
                    summary_parts.append(
                        f"{i}. {relevance_mark}{origin} → {destination} ({start_date}) - {purpose}"
                    )

        return "\n".join(summary_parts) if summary_parts else ""

    @staticmethod
    def _format_result_value(value: object) -> str:
        """结果字段的展示文本：列表「、」拼接、字典 JSON，其余 str。"""
        if isinstance(value, (list, tuple)):
            items = [
                str(item)
                for item in value
                if not (
                    item is None
                    or (isinstance(item, str) and not item.strip())
                )
            ]
            return "、".join(items) or "—"
        if isinstance(value, dict):
            import json as _json

            return _json.dumps(value, ensure_ascii=False)
        return str(value)

    def _generate_human_response(self, results: list) -> bool:
        """
        根据结果生成人性化的回复
        """
        has_output = False

        for result in results:
            agent_name = result.get("agent_name", "")
            status = result.get("status", "")
            data = result.get("data", {})
            data = data if isinstance(data, dict) else {}
            current_agent_shown = False  # 标记当前Agent是否有内容展示

            # 处理失败的智能体
            if status == "error":
                error_msg = data.get("error") or data.get("message") or "未知错误"
                agent_display_name = self._get_agent_display_name(agent_name)
                self.console.print(
                    f"❌ {escape(str(agent_display_name))}执行失败: {escape(str(error_msg))}",
                    style="red",
                )
                has_output = True
                continue

            # 只处理成功的智能体 (RAG 的 no_knowledge 视为一种特殊的成功/提示)
            if status != "success" and not (agent_name == "rag_knowledge" and status == "no_knowledge"):
                continue

            # --- 特定 Agent 处理 ---

            # 行程规划
            if agent_name == "itinerary_planning":
                itinerary = data.get("itinerary")
                # 增强：支持从 data.data.itinerary 获取
                if not itinerary and "data" in data and isinstance(data["data"], dict):
                    itinerary = data["data"].get("itinerary")

                if isinstance(itinerary, str) and itinerary.strip():
                    # 模型可能直接返回一段文字行程
                    self.console.print()
                    self.console.print("✈️  [bold cyan]行程规划[/bold cyan]")
                    self.console.print(escape(itinerary))
                    current_agent_shown = True
                elif itinerary and not isinstance(itinerary, dict):
                    self.console.print()
                    self.console.print("✈️  [bold cyan]行程规划[/bold cyan]")
                    self.console.print(escape(str(itinerary)))
                    current_agent_shown = True
                elif itinerary:
                    title = escape(str(itinerary.get("title", "行程规划")))
                    self.console.print()
                    self.console.print(f"✈️  [bold cyan]{title}[/bold cyan]")
                    self.console.print(
                        f"时长: {escape(str(itinerary.get('duration', '未知')))}"
                    )
                    self.console.print()

                    # 每日行程（结构异常时按纯文本兜底，不能让整轮回答丢失）
                    daily_plans = itinerary.get("daily_plans") or []
                    if isinstance(daily_plans, dict):
                        daily_plans = list(daily_plans.values())
                    if not isinstance(daily_plans, list):
                        daily_plans = []
                    for day_plan in daily_plans:
                        if not isinstance(day_plan, dict):
                            self.console.print(escape(str(day_plan)))
                            continue
                        day_num = escape(str(day_plan.get("day", 1)))
                        self.console.print(f"[bold yellow]第 {day_num} 天[/bold yellow]")

                        # 兼容 activities 和 time_slots
                        activities = (
                            day_plan.get("activities") or day_plan.get("time_slots") or []
                        )
                        if isinstance(activities, dict):
                            activities = list(activities.values())
                        if not isinstance(activities, list):
                            activities = []
                        for slot in activities:
                            if not isinstance(slot, dict):
                                self.console.print(escape(str(slot)))
                                continue
                            time_text = escape(str(slot.get("time", "")))
                            activity = escape(
                                str(slot.get("activity") or slot.get("location") or "")
                            )
                            description = slot.get("description", "")
                            transport = slot.get("transport", "")

                            self.console.print(f"  {time_text} - {activity}")
                            if description:
                                self.console.print(
                                    f"    {escape(str(description))}", style="dim"
                                )
                            if transport:
                                self.console.print(
                                    f"    🚇 {escape(str(transport))}", style="dim"
                                )

                        # 餐食建议
                        meals = day_plan.get("meals") or {}
                        if not isinstance(meals, dict):
                            meals = {}
                        if meals:
                            self.console.print()
                            if meals.get("lunch"):
                                self.console.print(
                                    f"  🍜 {escape(str(meals['lunch']))}", style="dim"
                                )
                            if meals.get("dinner"):
                                self.console.print(
                                    f"  🍽️  {escape(str(meals['dinner']))}", style="dim"
                                )
                        self.console.print()

                    # 注意事项
                    notes = itinerary.get("notes") or []
                    if isinstance(notes, str):
                        notes = [notes]
                    if not isinstance(notes, list):
                        notes = []
                    if notes:
                        self.console.print("[bold]📌 注意事项[/bold]")
                        for note in notes:
                            self.console.print(f"  • {escape(str(note))}")
                    current_agent_shown = True

            # 偏好管理
            elif agent_name == "preference":
                nested = data.get("data")
                nested = nested if isinstance(nested, dict) else {}
                raw_prefs = data.get("preferences")
                # 增强：支持从 data.data.preferences 获取
                if not raw_prefs:
                    raw_prefs = nested.get("preferences")
                if isinstance(raw_prefs, dict) and "preferences" not in raw_prefs:
                    # 旧版偏好智能体返回 {type: value} 字典：转成条目列表，
                    # 否则回显为空、和实际已落库的内容不一致
                    raw_prefs = {
                        "preferences": [
                            {"type": key, "value": value}
                            for key, value in raw_prefs.items()
                        ]
                    }

                if isinstance(raw_prefs, dict):
                    prefs_list = raw_prefs.get("preferences", [])
                else:
                    prefs_list = raw_prefs if isinstance(raw_prefs, list) else []
                # 模型可能返回字符串 / null 等非对象条目，或把 type 返回成
                # 对象 / 数组：落库路径（orchestration_agent）只接受字符串
                # type，这里同一口径过滤——否则把 dict/list 当字典键查询会
                # 抛 TypeError，整轮回显（含落库）一起失败
                prefs_items = [
                    item
                    for item in prefs_list
                    if isinstance(item, dict) and isinstance(item.get("type"), str)
                ]

                if prefs_items:
                    self.console.print("✓ [bold green]已更新您的偏好设置[/bold green]")
                    type_names = {
                        "home_location": "常驻地",
                        "transportation_preference": "交通偏好",
                        "hotel_brands": "酒店偏好",
                        "airlines": "航空公司偏好",
                        "seat_preference": "座位偏好",
                        "meal_preference": "餐食偏好",
                        "budget_level": "预算等级"
                    }
                    for pref in prefs_items:
                        pref_type = pref.get("type", "")
                        pref_value = pref.get("value", "")
                        action = str(pref.get("action", "replace")).lower()
                        display_type = type_names.get(pref_type, pref_type)
                        # 与落库路径（orchestration_agent）和 Web 渲染器同一
                        # 口径：Append / add / 追加 都表示追加而不是覆盖
                        action_text = (
                            "追加"
                            if action in {"append", "add", "追加"}
                            else "设置为"
                        )
                        self.console.print(
                            f"  • {escape(str(display_type))} {action_text} "
                            f"[cyan]{escape(self._format_result_value(pref_value))}"
                            "[/cyan]"
                        )
                    current_agent_shown = True
                    has_itinerary = any(r.get("agent_name") == "itinerary_planning" for r in results)
                    if not has_itinerary:
                        self.console.print("\n💡 下次规划行程时会参考这些偏好。", style="dim")
                else:
                    # 检查是否有错误信息
                    err = data.get("error", "")
                    if err:
                        self.console.print(f"偏好未保存: {escape(str(err))}", style="yellow")
                        current_agent_shown = True
                    # 如果只是没提取到，可能就是没偏好，不强求显示，交给兜底逻辑

            # 事项收集
            elif agent_name == "event_collection":
                # 增强：支持从 data.data 获取（data.data 非对象时按空处理，
                # 否则 .get 抛 AttributeError 让整轮回答丢失）
                nested = data.get("data")
                nested = nested if isinstance(nested, dict) else {}
                origin = data.get("origin") or nested.get("origin")
                destination = data.get("destination") or nested.get("destination")
                start_date = data.get("start_date") or nested.get("start_date")
                end_date = data.get("end_date") or nested.get("end_date")
                missing_info = data.get("missing_info") or nested.get("missing_info") or []

                has_itinerary = any(r.get("agent_name") == "itinerary_planning" for r in results)
                info_shown = False
                if not has_itinerary and (destination or origin):
                    self.console.print("✓ [bold green]已收集行程信息[/bold green]")
                    if origin:
                        self.console.print(
                            "  • 出发地: [cyan]"
                            f"{escape(self._format_result_value(origin))}[/cyan]"
                        )
                    if destination:
                        self.console.print(
                            "  • 目的地: [cyan]"
                            f"{escape(self._format_result_value(destination))}[/cyan]"
                        )
                    if start_date:
                        self.console.print(
                            "  • 出发日期: [cyan]"
                            f"{escape(self._format_result_value(start_date))}[/cyan]"
                        )
                    if end_date:
                        self.console.print(
                            "  • 返程日期: [cyan]"
                            f"{escape(self._format_result_value(end_date))}[/cyan]"
                        )
                    info_shown = True

                if missing_info:
                    if isinstance(missing_info, str):
                        missing_items = [missing_info]
                    elif isinstance(missing_info, (list, tuple)):
                        missing_items = [str(item) for item in missing_info]
                    else:
                        missing_items = [str(missing_info)]
                    self.console.print()
                    self.console.print(
                        f"💡 还需要补充: {escape(', '.join(missing_items))}",
                        style="yellow",
                    )
                    info_shown = True
                
                if info_shown:
                    current_agent_shown = True

            # 信息查询
            elif agent_name == "information_query":
                query_results = data.get("results")
                if not query_results and "data" in data and isinstance(data["data"], dict):
                    query_results = data["data"].get("results")
                if not query_results:
                    query_results = data # 兜底：data 本身就是 results

                if not isinstance(query_results, dict):
                    query_results = {}

                summary = query_results.get("summary", "")
                sources = query_results.get("sources", []) or []
                message = query_results.get("message", "")
                error = query_results.get("error", "")

                if summary:
                    self.console.print()
                    self.console.print(escape(str(summary)))
                    current_agent_shown = True
                elif message:
                    self.console.print()
                    self.console.print(escape(str(message)), style="dim")
                    current_agent_shown = True
                elif error:
                    self.console.print()
                    self.console.print(escape(str(error)), style="yellow")
                    current_agent_shown = True

                if sources:
                    self.console.print("\n[bold]参考来源[/bold]")
                    for i, source in enumerate(sources[:3], 1):
                        url = source.get("url", "") if isinstance(source, dict) else str(source)
                        self.console.print(f"  {i}. {escape(str(url))}", style="dim")
                    current_agent_shown = True

            # RAG知识库查询
            elif agent_name == "rag_knowledge":
                answer = data.get("answer")
                if not answer and "data" in data and isinstance(data["data"], dict):
                    answer = data["data"].get("answer")
                
                # 增强：也查找 content
                if not answer:
                    answer = data.get("content") or data.get("data", {}).get("content")

                # 深度清洗
                if isinstance(answer, dict):
                    answer = answer.get("answer", str(answer))
                
                if isinstance(answer, str) and answer.strip().startswith("{") and answer.strip().endswith("}"):
                    try:
                        import json
                        json_obj = json.loads(answer)
                        if isinstance(json_obj, dict) and "answer" in json_obj:
                            answer = json_obj["answer"]
                    except Exception:
                        pass

                if answer:
                    self.console.print()
                    self.console.print(escape(str(answer)))
                    current_agent_shown = True

            # 记忆查询
            elif agent_name == "memory_query":
                query_result = data.get("answer") or data.get("result") or data.get("content")
                if not query_result and "data" in data and isinstance(data["data"], dict):
                    inner = data["data"]
                    query_result = inner.get("answer") or inner.get("result") or inner.get("content")

                if query_result:
                    self.console.print()
                    self.console.print(escape(str(query_result)))
                    current_agent_shown = True

            # --- 通用兜底 (如果特定逻辑未生效) ---
            if not current_agent_shown:
                # 尝试查找通用字段
                common_keys = ["answer", "content", "result", "message", "summary", "text", "description"]
                fallback_content = ""
                
                # 扁平查找
                for k in common_keys:
                    if k in data and isinstance(data[k], str) and data[k].strip():
                        fallback_content = data[k]
                        break
                
                # 嵌套查找 data.data
                if not fallback_content and "data" in data and isinstance(data["data"], dict):
                    for k in common_keys:
                        if k in data["data"] and isinstance(data["data"][k], str) and data["data"][k].strip():
                            fallback_content = data["data"][k]
                            break

                if fallback_content:
                    self.console.print()
                    self.console.print(escape(str(fallback_content)))
                    current_agent_shown = True
                else:
                    # 实在啥也没有，打印个成功标记，避免完全静默
                    agent_display_name = self._get_agent_display_name(agent_name)
                    self.console.print(f"✓ {agent_display_name}已完成", style="green")
                    current_agent_shown = True

            if current_agent_shown:
                has_output = True

        return has_output

    def _get_agent_display_name(self, agent_name: str) -> str:
        """获取智能体的显示名称"""
        # 与 README / LazyAgentRegistry 保持一致，仅保留已存在的 6 个子智能体
        agent_display_names = {
            "event_collection": "事项收集",
            "preference": "偏好管理",
            "itinerary_planning": "行程规划",
            "information_query": "信息查询",
            "rag_knowledge": "知识库查询",
            "memory_query": "记忆查询",
        }
        return agent_display_names.get(agent_name, agent_name)

    def show_status(self):
        """显示当前状态"""
        # 其它进程（Web 面板 / CLI）可能刚写入：先重载 JSON 快照
        self.memory_manager.reload_long_term()
        # 记忆统计
        full_context = self.memory_manager.get_full_context()
        short_term_stats = full_context["short_term"]["statistics"]
        long_term_stats = full_context["long_term"]["statistics"]

        memory_table = Table(title="记忆状态", show_header=True, header_style="bold magenta")
        memory_table.add_column("类型", style="cyan")
        memory_table.add_column("状态", style="white")

        memory_table.add_row(
            "短期记忆",
            f"{short_term_stats['total_messages']} 条消息"
        )
        memory_table.add_row(
            "长期记忆",
            f"{long_term_stats['total_trips']} 次行程"
        )
        memory_table.add_row(
            "已加载智能体",
            f"{len(self._agent_cache)} 个"
        )
        usage = getattr(self.model, "usage", None)
        if usage is not None:
            memory_table.add_row(
                "本次会话 LLM 用量",
                f"{usage.calls} 次调用 · tokens {usage.total_tokens}"
                f"（输入 {usage.input_tokens} / 输出 {usage.output_tokens}）"
            )

        self.console.print(memory_table)
        self.console.print()

        # 历史对话
        recent_messages = self.memory_manager.short_term.get_recent_context(n_turns=5)
        if recent_messages:
            dialogue_table = Table(title="最近对话 (最多5轮)", show_header=True, header_style="bold cyan")
            dialogue_table.add_column("角色", style="cyan", width=8)
            dialogue_table.add_column("内容", style="white", width=60)
            dialogue_table.add_column("时间", style="dim", width=12)

            from utils.stored_message import render_stored_message

            for msg in recent_messages:
                role_name = "👤 用户" if msg["role"] == "user" else "🤖 助手"
                content = (
                    str(msg.get("content", ""))
                    if msg.get("role") == "user"
                    else render_stored_message(msg)
                )

                # 截断过长的内容
                if len(content) > 100:
                    content = content[:100] + "..."

                # 格式化时间
                timestamp = msg.get("timestamp", "")
                if timestamp:
                    from datetime import datetime
                    try:
                        dt = datetime.fromisoformat(timestamp)
                        time_str = dt.strftime("%H:%M:%S")
                    except Exception:
                        time_str = ""
                else:
                    time_str = ""

                dialogue_table.add_row(
                    escape(str(role_name)), escape(str(content)), escape(str(time_str))
                )

            self.console.print(dialogue_table)
            self.console.print()

    async def run_health_check(self):
        """在会话内执行健康检查并显示熔断器状态"""
        if self.circuit_breaker:
            status = self.circuit_breaker.get_status()
            self.console.print(f"[bold]熔断器[/bold]: {status['state']}", style="cyan")
        ok, msg = await check_llm_health(
            base_url=LLM_CONFIG["base_url"],
            api_key=LLM_CONFIG["api_key"],
            model_name=LLM_CONFIG["model_name"],
            timeout_sec=RESILIENCE_CONFIG.get("health_check_timeout_sec", 10.0),
        )
        if ok:
            self.console.print("LLM 服务: [green]正常[/green]", style="bold")
        else:
            self.console.print(
                f"LLM 服务: [red]不可用[/red] - {escape(str(msg))}",
                style="bold",
            )
        self.console.print()

    async def run_doctor(self):
        """在会话内执行环境自检（依赖 / 密钥 / 模型 / 知识库 / 连通性）"""
        from utils.doctor import build_report_table, run_doctor, summarize

        results = await run_doctor(include_network=bool(LLM_CONFIG["api_key"]))
        self.console.print(build_report_table(results))
        text, style = summarize(results)
        self.console.print(text, style=style)
        self.console.print()

    def show_history(self):
        """显示历史行程"""
        self.memory_manager.reload_long_term()
        history = self.memory_manager.long_term.get_trip_history(10)

        if not history:
            self.console.print("暂无历史行程", style="yellow")
            return

        table = Table(title="历史行程", show_header=True, header_style="bold magenta")
        table.add_column("ID", style="cyan")
        table.add_column("出发地", style="white")
        table.add_column("目的地", style="white")
        table.add_column("日期", style="white")
        table.add_column("目的", style="white")

        for trip in history:
            table.add_row(
                escape(str(trip.get("trip_id", ""))),
                escape(str(trip.get("origin", ""))),
                escape(str(trip.get("destination", ""))),
                escape(str(trip.get("start_date", ""))),
                escape(str(trip.get("purpose", ""))),
            )

        self.console.print(table)

    def show_preferences(self):
        """显示用户偏好"""
        self.memory_manager.reload_long_term()
        prefs = self.memory_manager.long_term.get_preference()

        table = Table(title="用户偏好", show_header=True, header_style="bold magenta")
        table.add_column("类型", style="cyan")
        table.add_column("值", style="white")

        for key, value in prefs.items():
            # 0 / False 是合法偏好值（预算为 0、不要靠窗）：真值判断会把
            # 整行藏掉，与 standalone preferences 和 Web 面板不一致
            if has_preference_value(value):
                table.add_row(
                    escape(str(key)),
                    escape(self._format_result_value(value)),
                )

        self.console.print(table)

    def export_memory(self):
        """把当前用户的长期记忆导出为 JSON 与 Markdown（exports/ 目录）"""
        from utils.exporter import export_files

        self.memory_manager.reload_long_term()
        data = self.memory_manager.export_data()
        paths = export_files(data, "exports")
        for path in paths:
            self.console.print(f"✓ 已导出: {path}", style="green")

    async def run(self):
        """运行 CLI"""
        # 打印横幅
        self.print_banner()

        # 初始化系统
        await self.initialize_system()

        # 自己接管 SIGINT：asyncio.run 的默认处理器只取消主任务，而「提示符
        # 下按的 Ctrl-C」这个取消要等到下一条命令的第一个 await 才送达，会
        # 把刚输入的命令静默取消；关闭阶段（Runner.close）对主任务的取消也
        # 会被 REPL 当成用户中断吞掉，进程卡在提示符不退出。这里记录取消
        # 来源：用户 Ctrl-C 才回提示符，关闭取消继续向上抛。
        main_task = asyncio.current_task()
        interrupt = {"count": 0, "user": False, "at_prompt": False}

        def _on_sigint(signum, frame):
            if main_task is None or main_task.done():
                return
            interrupt["count"] += 1
            if interrupt["count"] > 1:
                # 连按两次：强制退出（与 asyncio 默认处理器同一语义）。
                # 关闭阶段还会取消主任务，先清掉用户标记让这次取消按关闭
                # 处理（否则会被 REPL 吞掉、gather 永远等不到主任务）
                interrupt["count"] = 0
                interrupt["user"] = False
                raise KeyboardInterrupt
            if interrupt["at_prompt"]:
                # 提示符处（同步 input）按 Ctrl-C：直接打断 input 回到新
                # 提示符。不能改为取消任务——任务此刻在同步代码中，取消会
                # 挂起到下一条命令的第一个 await 才送达，把刚输入的命令
                # 静默取消（C 实现的 Task 也无法事后撤回挂起的取消）
                interrupt["count"] = 0
                interrupt["user"] = False
                raise KeyboardInterrupt
            interrupt["user"] = True
            main_task.cancel()

        previous_handler = None
        try:
            previous_handler = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, _on_sigint)
        except (ValueError, OSError):
            # 非主线程 / 受限环境：无法安装，沿用 asyncio 处理器
            previous_handler = None

        def _clear_pending_cancel() -> None:
            task = asyncio.current_task()
            if task is None:
                return
            if hasattr(task, "uncancel"):
                # 取消已送达后回退计数，避免连锁取消（3.11+）
                while task.cancelling():
                    task.uncancel()

        try:
            # 主循环
            while True:
                try:
                    # 获取用户输入。stdin 不可用只可能发生在 Prompt.ask 本身：
                    # 收窄在这里捕获；放到整个循环体会误伤 MemoryPersistenceError
                    # （RuntimeError 子类），一次记忆写盘失败就被当成 stdin 故障退出
                    try:
                        interrupt["at_prompt"] = True
                        try:
                            user_input = Prompt.ask("\n[cyan]>[/cyan]")
                        finally:
                            interrupt["at_prompt"] = False
                    except (RuntimeError, ValueError) as exc:
                        self.console.print(
                            f"\n无法读取输入（{exc}），已退出", style="dim"
                        )
                        break

                    if interrupt["count"]:
                        # 提示符下按过 Ctrl-C：取消还挂在任务上，会在本命令
                        # 的第一个 await 处才送达——先清掉，让输入的命令正常
                        # 执行（回到提示符，而不是取消下一条命令）
                        interrupt["count"] = 0
                        interrupt["user"] = False
                        _clear_pending_cancel()
                        self.console.print("已取消上一步操作", style="dim")

                    if not user_input.strip():
                        continue

                    # 处理命令
                    command = user_input.strip().lower()

                    if command == "exit":
                        self.memory_manager.end_session()
                        self.console.print("再见！", style="cyan")
                        break
                    elif command == "help":
                        self.print_help()
                    elif command == "status":
                        self.show_status()
                    elif command == "health":
                        await self.run_health_check()
                    elif command == "doctor":
                        await self.run_doctor()
                    elif command == "clear":
                        self.memory_manager.short_term.clear()
                        self.console.print("✓ 已清空短期记忆", style="green")
                    elif command == "history":
                        self.show_history()
                    elif command == "preferences":
                        self.show_preferences()
                    elif command == "export":
                        self.export_memory()
                    else:
                        # 处理自然语言查询
                        await self.process_query(user_input)

                except EOFError:
                    # 管道 / IDE 末尾：退出，避免每次循环都报错刷屏
                    self.console.print()
                    self.console.print("未检测到输入，已退出", style="dim")
                    break
                except asyncio.CancelledError:
                    # 只有 SIGINT 处理器自己发起的取消才回到提示符；
                    # asyncio.run 关闭阶段对主任务的取消必须继续向上抛，
                    # 否则 Runner.close() 的 gather 永远等不到主任务结束
                    if not interrupt["user"]:
                        raise
                    interrupt["user"] = False
                    interrupt["count"] = 0
                    _clear_pending_cancel()
                    self.console.print("\n使用 'exit' 退出", style="dim")
                except KeyboardInterrupt:
                    interrupt["user"] = False
                    interrupt["count"] = 0
                    _clear_pending_cancel()
                    self.console.print("\n使用 'exit' 退出", style="dim")
                except CircuitOpenError:
                    self.console.print("\n[bold yellow]⚠ 服务暂时不可用，请稍后再试。[/bold yellow]", style="dim")
                except Exception as e:
                    self.console.print(f"\n错误: {e}", style="red")
        finally:
            if previous_handler is not None:
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(signal.SIGINT, previous_handler)


def run_health_check_standalone(as_json: bool = False) -> int:
    """
    独立执行健康检查（用于 `python cli.py health`）。
    不进入交互式 CLI，只检测 LLM 是否可达。
    Args:
        as_json: 输出机器可读的 JSON（便于监控 / 脚本）
    Returns:
        0 成功，1 失败（便于脚本/监控）
    """
    import asyncio
    import contextlib
    import io

    if init_agentscope is None:
        message = (
            "缺少运行时依赖，无法执行健康检查"
            "（pip install -r requirements.txt 或 pip install -e .）"
        )
        if as_json:
            print(
                json.dumps(
                    {"ok": False, "message": message},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"FAIL: {message}")
        return 1

    def _probe() -> tuple[bool, str]:
        init_agentscope()
        return asyncio.run(
            check_llm_health(
                base_url=str(LLM_CONFIG["base_url"]),
                api_key=str(LLM_CONFIG["api_key"]),
                model_name=str(LLM_CONFIG["model_name"]),
                timeout_sec=RESILIENCE_CONFIG.get("health_check_timeout_sec", 10.0),
            )
        )

    if as_json:
        # init_agentscope 会打印版本信息；JSON 模式屏蔽 stdout，保证输出可解析
        with contextlib.redirect_stdout(io.StringIO()):
            ok, msg = _probe()
        print(json.dumps({"ok": ok, "message": msg}, ensure_ascii=False, indent=2))
    else:
        ok, msg = _probe()
        if ok:
            print("OK")
        else:
            print(f"FAIL: {msg}")
    return 0 if ok else 1


def run_doctor_standalone(
    as_json: bool = False, include_network: bool | None = None
) -> int:
    """
    独立执行环境自检（用于 `python cli.py doctor`），不进入交互式 CLI。

    Args:
        as_json: 输出机器可读的 JSON（便于监控 / 脚本）
        include_network: 是否检查 LLM 连通性；None 表示有 API Key 时检查

    Returns:
        0 无 fail 项；1 存在必须处理的问题（便于脚本/监控）
    """
    import asyncio

    from utils.doctor import build_report_table, has_failures, run_doctor, summarize

    if include_network is None:
        include_network = bool(LLM_CONFIG["api_key"])
    results = asyncio.run(run_doctor(include_network=include_network))
    failed = has_failures(results)
    if as_json:
        print(
            json.dumps(
                {
                    "status": "fail" if failed else "ok",
                    "checks": [
                        {"name": item.name, "status": item.status, "detail": item.detail}
                        for item in results
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1 if failed else 0
    if Console is not None:
        Console().print(build_report_table(results))
        text, style = summarize(results)
        Console().print(text, style=style)
    else:
        # rich 缺失时退回纯文本：依赖缺失本身也要能被如实看到
        for item in results:
            print(f"[{item.status}] {item.name}: {item.detail}")
        text, _style = summarize(results)
        print(text)
    return 1 if failed else 0


def _parse_json_flag(args: list[str], usage: str) -> bool | None:
    """解析可选 ``--json``（大小写不敏感、可重复）；非法参数打印用法并返回 None。"""
    if all(item.lower() == "--json" for item in args):
        return bool(args)
    print(f"用法：{usage}")
    return None


def _parse_strict_date(value: str):
    """严格解析 YYYY-MM-DD；不补零写法（2026-1-1）返回 None。

    strptime 会接受未补零的日期，同一天换种写法即可绕过去重、生成重复行程。
    """
    from datetime import datetime

    try:
        parsed = datetime.strptime(str(value), "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    if parsed.strftime("%Y-%m-%d") != str(value):
        return None
    return parsed


def _valid_user_id(value: str) -> bool:
    """用户 ID 规则与 API 一致：中文 / 字母 / 数字 / 下划线 / 短横线，1..64 字符。"""
    return (
        bool(re.fullmatch(r"[\w-]{1,64}", value))
        and not value.startswith("-")
    )


def _parse_user_and_json_args(args: list[str]) -> tuple[str, bool] | None:
    """解析 ``<user_id> [--json]``（位置不限、大小写不敏感、可重复）。"""
    user_id = None
    as_json = False
    for item in args:
        if item.lower() == "--json":
            as_json = True
        elif item.startswith("-"):
            return None
        elif user_id is None:
            user_id = item
        else:
            return None
    if user_id is None or not _valid_user_id(user_id):
        return None
    return user_id, as_json


def _parse_eval_args(args: list[str]) -> tuple[bool, float] | None:
    """解析 eval 的 ``[--json] [--min-hit5 X]``；非法参数返回 None。

    ``--min-hit5`` 取 0..1 的小数（默认 0.8），用于判定评测是否达标。
    """
    as_json = False
    min_hit5 = 0.8
    index = 0
    while index < len(args):
        item = args[index]
        lowered = item.lower()
        if lowered == "--json":
            as_json = True
        elif lowered == "--min-hit5" or lowered.startswith("--min-hit5="):
            if lowered == "--min-hit5":
                index += 1
                if index >= len(args):
                    return None
                value = args[index]
            else:
                value = item[len("--min-hit5="):]
            if not re.fullmatch(r"[0-9]+(\.[0-9]+)?", value):
                # 与 _parse_cli_int 一致：拒绝 Unicode 数字 / 下划线 / 空白
                return None
            parsed = float(value)
            if not 0.0 <= parsed <= 1.0:
                return None
            min_hit5 = parsed
        else:
            return None
        index += 1
    return as_json, min_hit5


def _parse_doctor_args(args: list[str]) -> tuple[bool, bool] | None:
    """解析 doctor 的 ``[--json] [--offline]``；非法参数返回 None。"""
    as_json = False
    include_network = True
    for item in args:
        if item == "--json":
            as_json = True
        elif item in {"--offline", "--no-network"}:
            include_network = False
        else:
            return None
    return as_json, include_network


def run_skills_standalone(as_json: bool = False) -> int:
    """列出技能目录与已发现的技能（用于 `python cli.py skills`）。

    Args:
        as_json: 输出机器可读的 JSON（便于监控 / 脚本）

    Returns:
        0 至少发现一个技能；1 技能目录缺失或没有可加载技能
    """
    from utils.skill_paths import resolve_skills_root, skill_scripts

    root = resolve_skills_root()
    scripts = skill_scripts(root)
    if as_json:
        print(
            json.dumps(
                {
                    "root": str(root),
                    "skills": sorted(scripts),
                    "ok": bool(scripts),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if scripts else 1
    if not scripts:
        if not root.is_dir():
            print(f"未找到技能目录：{root}")
            print("请检查安装，或用环境变量 ALIGO_SKILLS_DIR 指定技能目录")
        else:
            print(f"技能目录没有可加载的技能：{root}")
            print("每个技能需要包含 script/agent.py")
        return 1
    print(f"技能目录：{root}")
    print(f"已发现 {len(scripts)} 个技能：")
    for name in sorted(scripts):
        print(f"  - {name}")
    return 0


def run_weather_standalone(city: str, as_json: bool = False) -> int:
    """查询城市天气（用于 `python cli.py weather <城市>`）。

    Args:
        city: 城市名
        as_json: 输出完整 JSON（便于脚本 / 监控）

    Returns:
        0 查询成功；1 城市不存在或天气接口不可用
    """
    from utils.weather import WeatherError, fetch_weather

    try:
        data = fetch_weather(city)
    except WeatherError as exc:
        if as_json:
            print(
                json.dumps(
                    {"ok": False, "city": city, "error": str(exc)},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"天气查询失败：{exc}")
        return 1

    if as_json:
        print(json.dumps({**data, "ok": True}, ensure_ascii=False, indent=2))
        return 0

    def _temp(value: object) -> str:
        return "?" if value is None else str(value)

    current = data.get("current") or {}
    print(
        f"{data.get('city') or city} 当前天气：{current.get('description') or '未知'}，"
        f"气温 {_temp(current.get('temperature'))}°C，"
        f"湿度 {_temp(current.get('humidity'))}%"
    )
    daily = data.get("daily") or []
    if daily:
        print("未来几天：")
        for day in daily:
            print(
                f"  {day.get('date')}: {day.get('description') or '未知'}，"
                f"{_temp(day.get('min'))}~{_temp(day.get('max'))}°C"
            )
    if data.get("stale"):
        print("（天气数据可能不是最新）")
    return 0


def run_knowledge_status_standalone(as_json: bool = False) -> int:
    """打印知识库状态（用于 `python cli.py kb`），不进入交互式 CLI。

    Args:
        as_json: 输出机器可读的 JSON（便于监控 / 脚本）

    Returns:
        0 知识库可用；1 未初始化或不可用
    """
    from utils.knowledge_stats import knowledge_status

    status = knowledge_status()
    if as_json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0 if status.get("available") else 1
    if status.get("available"):
        print(f"知识库：{status['collection']} · {status['documents']} 片段")
        print(f"数据库：{status['path']}")
        return 0
    print(f"知识库不可用：{status.get('reason', '未知原因')}")
    print(f"数据库：{status.get('path', '')}")
    return 1


def run_rag_eval_standalone(min_hit5: float = 0.8, as_json: bool = False) -> int:
    """运行 RAG 检索评测（用于 `python cli.py eval`），不进入交互式 CLI。

    Args:
        min_hit5: hit@5 达标阈值
        as_json: 输出机器可读的 JSON（便于监控 / 脚本）

    Returns:
        0 评测完成且 hit@5 达标（或知识库未初始化，跳过）；1 未达标或后端故障
    """
    from utils.knowledge_stats import knowledge_status

    status = knowledge_status()
    if not status.get("available"):
        reason = str(status.get("reason", "未知原因"))
        if reason in {"知识库未初始化", "集合不存在"}:
            if as_json:
                print(
                    json.dumps(
                        {"ok": True, "skipped": True, "reason": reason},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            else:
                print(f"跳过评测：{reason}")
            return 0
        # 读取失败 / 文件锁占用等属于基础设施故障，不能当作“跳过”并返回成功
        if as_json:
            print(
                json.dumps(
                    {"ok": False, "skipped": False, "reason": reason},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"评测失败：知识库不可用（{reason}）")
        return 1

    try:
        from agents.rag_knowledge_agent import RAGKnowledgeAgent
        from utils.rag_eval import evaluate_retrieval

        agent = RAGKnowledgeAgent(collection_name=status["collection"])
    except Exception as exc:  # noqa: BLE001 - 依赖 / 技能加载失败与 Agent 初始化同口径
        if as_json:
            print(
                json.dumps(
                    {"ok": False, "error": f"无法初始化 RAG Agent: {exc}"},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"评测失败：无法初始化 RAG Agent（{exc}）")
        return 1
    if not agent.initialized:
        if as_json:
            print(
                json.dumps(
                    {"ok": False, "error": "RAG 依赖未安装"},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print("评测失败：RAG 依赖未安装（pip install -r requirements.txt）")
        return 1

    try:
        from config import RAG_CONFIG

        result = evaluate_retrieval(
            lambda query, top_k: agent.search_knowledge(
                query, top_k=top_k, strict=True
            ),
            # 与问答链路同一相似度阈值：低于阈值的命中不计入（否则评测
            # 可以 100% 通过而真实回答全是「没有找到相关信息」）
            min_score=float(RAG_CONFIG.get("min_score", 0.5)),
        )
    except Exception as exc:  # noqa: BLE001 - 检索后端故障不能报成 0% 命中
        if as_json:
            print(
                json.dumps(
                    {"ok": False, "error": f"检索后端不可用：{exc}"},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"评测失败：检索后端不可用（{exc}）")
        return 1
    passed = result["hit5"] >= min_hit5
    if as_json:
        payload = dict(result)
        payload.update({"ok": passed, "skipped": False, "threshold": min_hit5})
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if passed else 1

    for row in result["rows"]:
        mark = f"#{row['rank']}" if row["rank"] else "未命中"
        print(f"  {row['query']} → {mark}")
    print(
        f"检索评测：{result['total']} 个问题 · hit@1 {result['hit1']:.0%} · "
        f"hit@3 {result['hit3']:.0%} · hit@5 {result['hit5']:.0%} · MRR {result['mrr']:.2f}"
    )
    if not passed:
        if result["hit5"] == 0:
            print("提示：所有问题均未命中，可能是索引未加载 / 知识库故障，而非切分策略问题")
        print(f"低于阈值 hit@5 >= {min_hit5:.0%}，请检查知识库内容或切分策略")
        return 1
    return 0


def run_knowledge_export_standalone(
    target: str | None = None, force: bool = False, to_stdout: bool = False
) -> int:
    """导出知识库为 JSON（`python cli.py kb-export [path] [--stdout] [--force]`）。

    ``--stdout`` 把 JSON 直接打到标准输出（便于管道给 jq 等），与指定路径
    互斥；默认写入 ``exports/knowledge-export.json``。

    Returns:
        0 导出成功；1 参数 / 知识库不可用 / 文件已存在 / 写入失败
    """
    from pathlib import Path

    from utils.knowledge_admin import export_documents
    from utils.knowledge_stats import knowledge_status

    def _fail(message: str) -> int:
        # --stdout 是机器可读通道：失败也用 JSON，避免下游拿到非 JSON 文本
        if to_stdout:
            _print_stdout(
                json.dumps(
                    {"ok": False, "error": message},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(message)
        return 1

    status = knowledge_status()
    if not status.get("available"):
        return _fail(
            f"导出失败：知识库不可用（{status.get('reason', '未知原因')}）"
        )

    try:
        documents = export_documents()
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        return _fail(f"导出失败：{exc}")

    from datetime import datetime, timezone

    payload = {
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "documents": documents,
    }
    if to_stdout:
        _print_stdout(
            json.dumps(
                {"ok": True, **payload}, ensure_ascii=False, indent=2
            )
        )
        return 0
    path = Path(target) if target else Path("exports") / "knowledge-export.json"
    # 与 trips-export / session-export 一致：默认不覆盖已有文件，
    # 避免把用户数据（如误把 data/memory/*.json 当作导出路径）静默替换
    if path.exists() and not force:
        print(f"文件已存在：{path}（使用 --force 覆盖或指定其它路径）")
        return 1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        print(f"写入失败：{exc}")
        return 1
    print(f"已导出 {len(documents)} 条知识片段到 {path}")
    return 0


def run_knowledge_import_standalone(
    source: str, dry_run: bool = False, as_json: bool = False
) -> int:
    """从 JSON 文件批量导入知识片段（`python cli.py kb-import <file> [--dry-run] [--json]`）。

    ``--dry-run`` 只校验 / 去重并报告将导入的数量，不加载嵌入模型、不写入。
    ``--json`` 输出结构化结果（含跳过原因），供脚本消费。

    Returns:
        0 导入成功；1 文件 / 格式 / 知识库错误
    """
    from pathlib import Path

    from utils.knowledge_admin import import_documents

    def _fail(message: str) -> int:
        if as_json:
            print(
                json.dumps(
                    {"ok": False, "error": message},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(message)
        return 1

    path = Path(source)
    if not path.exists():
        return _fail(f"文件不存在：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        # RecursionError：结构性深层嵌套（如 '['*3000）不是 ValueError 子类，
        # 不捕获会让 --json 模式打印 traceback、stdout 无结构化输出
        return _fail(f"读取失败：{exc}（文件需为 UTF-8 编码的 JSON）")

    documents = (
        data
        if isinstance(data, list)
        else data.get("documents")
        if isinstance(data, dict)
        else None
    )
    if not isinstance(documents, list) or not documents:
        return _fail("文件中没有知识片段（需要 documents 数组或纯数组）")

    try:
        result = import_documents(documents, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        return _fail(f"导入失败：{exc}")
    invalid = int(result.get("skipped_invalid", 0) or 0)
    duplicate = int(result.get("skipped_duplicate", 0) or 0)
    skipped = int(result.get("skipped", invalid + duplicate) or 0)
    if as_json:
        payload: dict[str, Any] = {
            "ok": True,
            "dry_run": dry_run,
            "imported": int(result.get("imported", 0) or 0),
            "skipped": skipped,
            "skipped_invalid": invalid,
            "skipped_duplicate": duplicate,
        }
        if dry_run:
            payload["would_import"] = int(result.get("would_import", 0) or 0)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    detail_parts = []
    if duplicate:
        detail_parts.append(f"{duplicate} 条已存在")
    if invalid:
        detail_parts.append(f"{invalid} 条无效")
    detail = f"（{' / '.join(detail_parts)}）" if detail_parts else ""
    if dry_run:
        print(
            f"预览：将导入 {result.get('would_import', 0)} 条知识片段，"
            f"跳过 {skipped} 条{detail}（未写入）"
        )
        return 0
    print(f"已导入 {result['imported']} 条知识片段，跳过 {skipped} 条{detail}")
    return 0


def run_knowledge_list_standalone(
    query: str = "", limit: int = 50, as_json: bool = False
) -> int:
    """列出知识片段（`python cli.py kb-list [关键词] [--json]`）。

    ``--json`` 输出 ``{ok, query, total, count, items}``（items 为原始
    id / content / metadata）。

    Returns:
        0 列出成功；1 知识库不可用或读取失败
    """
    from utils.knowledge_admin import list_documents
    from utils.knowledge_stats import knowledge_status

    status = knowledge_status()
    if not status.get("available"):
        reason = status.get("reason", "未知原因")
        if as_json:
            print(
                json.dumps(
                    {"ok": False, "error": f"知识库不可用：{reason}"},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"知识库不可用：{reason}")
        return 1
    try:
        data = list_documents(limit=limit, offset=0, query=query)
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        if as_json:
            print(
                json.dumps(
                    {"ok": False, "error": f"读取失败：{exc}"},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"读取失败：{exc}")
        return 1

    items = data.get("items") or []
    total = data.get("total", len(items))
    if as_json:
        from utils.json_safe import sanitize_json

        print(
            json.dumps(
                sanitize_json(
                    {
                        "ok": True,
                        "query": query,
                        "total": total,
                        "count": len(items),
                        "items": items,
                    }
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    suffix = f"（关键词：{query}）" if query else ""
    print(f"共 {total} 条{suffix}，显示前 {len(items)} 条（按 id 倒序）")
    for item in items:
        meta = item.get("metadata") or {}
        category = meta.get("category") or meta.get("title") or ""
        preview = " ".join(str(item.get("content", "")).split())[:60]
        tag = f" · {category}" if category else ""
        chunk = ""
        try:
            parts = int(meta.get("parts") or 1)
        except (TypeError, ValueError):
            parts = 1
        if parts > 1:
            chunk = f" · 分片 {meta.get('part') or '?'}/{parts}"
        print(f"  #{item['id']}{tag}{chunk}  {preview}")
    return 0


def run_knowledge_categories_standalone(as_json: bool = False) -> int:
    """打印知识片段分类统计（用于 `python cli.py kb-categories`）。

    Args:
        as_json: 输出机器可读的 JSON（便于监控 / 脚本）

    Returns:
        0 读取成功；1 知识库不可用或读取失败
    """
    from utils.knowledge_admin import list_categories
    from utils.knowledge_stats import knowledge_status

    status = knowledge_status()
    if not status.get("available"):
        reason = status.get("reason", "未知原因")
        if as_json:
            print(
                json.dumps(
                    {"ok": False, "error": f"知识库不可用：{reason}"},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"知识库不可用：{reason}")
        return 1
    try:
        categories = list_categories()
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        if as_json:
            print(
                json.dumps(
                    {"ok": False, "error": f"读取失败：{exc}"},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"读取失败：{exc}")
        return 1

    total = sum(int(item.get("count") or 0) for item in categories)
    if as_json:
        print(
            json.dumps(
                {"categories": categories, "total": total},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if not categories:
        print("暂无分类信息（知识片段未设置 category 元数据）")
        return 0
    print(f"知识分类：{len(categories)} 个分类，共 {total} 片")
    for item in categories:
        print(f"  · {item['category']}  {item['count']} 片")
    return 0


def run_knowledge_search_standalone(
    query: str, top_k: int = 5, as_json: bool = False
) -> int:
    """语义检索知识片段（`python cli.py kb-search <关键词> [--top-k N] [--json]`）。

    与 Web 面板的语义检索、RAG 问答共用同一条检索链路（嵌入模型 + Milvus）。

    Returns:
        0 检索成功（含无结果）；1 知识库不可用或检索失败
    """
    from utils.knowledge_admin import search_documents
    from utils.knowledge_stats import knowledge_status

    def _fail(error: str) -> int:
        if as_json:
            print(
                json.dumps(
                    {"ok": False, "error": error},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(error)
        return 1

    status = knowledge_status()
    if not status.get("available"):
        return _fail(f"知识库不可用：{status.get('reason', '未知原因')}")
    try:
        results = search_documents(query, top_k=top_k)
    except FileNotFoundError:
        return _fail("知识库未初始化")
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        return _fail(f"检索失败：{exc}")

    if as_json:
        print(
            json.dumps(
                {"query": query, "top_k": top_k, "results": results},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if not results:
        print(f"没有与「{query}」相关的内容")
        return 0
    print(f"语义检索「{query}」：{len(results)} 条结果（按相似度排序）")
    for item in results:
        metadata = item.get("metadata") or {}
        category = metadata.get("category") or metadata.get("title") or ""
        preview = " ".join(str(item.get("content", "")).split())[:60]
        bits: list[str] = []
        if item.get("id") is not None:
            bits.append(f"#{item['id']}")
        if category:
            bits.append(str(category))
        distance = item.get("distance")
        if isinstance(distance, (int, float)):
            bits.append(f"相似度 {float(distance):.3f}")
        prefix = " · ".join(bits)
        if prefix and preview:
            print(f"  {prefix}  {preview}")
        elif prefix:
            print(f"  {prefix}")
        elif preview:
            print(f"  {preview}")
    return 0


def run_knowledge_show_standalone(doc_id: int, as_json: bool = False) -> int:
    """查看知识片段（多分片按整篇拼接，`python cli.py kb-show <id> [--json]`）。

    与面板「编辑整份文档」同一实现（``get_document_group``）：带 group 的
    记录按 part 顺序拼接为整篇，便于在终端核对拆分结果。

    Returns:
        0 查看成功；1 片段不存在 / 知识库不可用 / 读取失败
    """
    from utils.knowledge_admin import get_document_group
    from utils.knowledge_stats import knowledge_status

    def _fail(error: str) -> int:
        if as_json:
            print(
                json.dumps(
                    {"ok": False, "error": error},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(error)
        return 1

    status = knowledge_status()
    if not status.get("available"):
        return _fail(f"知识库不可用：{status.get('reason', '未知原因')}")
    try:
        document = get_document_group(doc_id)
    except FileNotFoundError:
        return _fail("知识库未初始化")
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        return _fail(f"读取失败：{exc}")
    if document is None:
        return _fail(f"知识点 #{doc_id} 不存在")
    if as_json:
        print(json.dumps(document, ensure_ascii=False, indent=2))
        return 0
    parts = int(document.get("parts") or 1)
    group = str(document.get("group") or "")
    content = str(document.get("content") or "")
    size = int(document.get("size") or len(content))
    meta = [f"#{doc_id}"]
    if parts > 1:
        meta.append(f"整篇 {parts} 片")
    if group:
        meta.append(f"group {group}")
    meta.append(f"{size} 字")
    print(" · ".join(meta))
    print()
    print(content)
    return 0


def run_stats_standalone(user_id: str, as_json: bool = False) -> int:
    """用户数据概览（`python cli.py stats <user_id> [--json]`）。

    只读汇总：偏好条数、行程统计（总数 / 出行天数 / 到访城市 / 进行中 /
    即将出行）、会话与消息数（含归档数），与 Web 记忆面板同口径。

    Returns:
        0 汇总成功；1 参数 / 读取失败
    """
    import re

    from utils.travel_stats import summarize_travel

    if not re.fullmatch(r"[\w-]{1,64}", user_id) or user_id.startswith("-"):
        print("用户 ID 只能包含中文、字母、数字、下划线和短横线")
        return 1
    try:
        store = _open_cli_store(user_id)
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        print(f"打开长期记忆失败：{exc}")
        return 1
    try:
        try:
            preferences = store.get_preference()
            trips = store.get_trip_history(limit=None)
            history = store.get_chat_history()
            archived = store.get_archived_sessions() or []
        except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
            print(f"读取数据失败：{exc}")
            return 1
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()

    sessions = {str(item.get("session_id")) for item in history if item.get("session_id")}
    travel = summarize_travel(trips)
    # 与面板 / 状态接口同口径：null / 空白 / 全空列表不计入偏好条数
    visible_preferences = {
        key: value
        for key, value in preferences.items()
        if has_preference_value(value)
    }
    payload: dict[str, Any] = {
        "user_id": user_id,
        "preferences": len(visible_preferences),
        "travel": travel,
        "sessions": {
            "total": len(sessions),
            "messages": len(history),
            "archived": len(archived),
        },
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(f"用户 {user_id} 概览")
    print(f"  · 偏好 {payload['preferences']} 条")
    print(
        f"  · 行程 {travel['total_trips']} 条（出行 {travel['total_days']} 天 / "
        f"到访 {travel['cities']} 个城市 / 进行中 {travel['active']} / "
        f"即将出行 {travel['upcoming']}）"
    )
    print(
        f"  · 会话 {payload['sessions']['total']} 个"
        f"（消息 {payload['sessions']['messages']} 条，"
        f"归档 {payload['sessions']['archived']}）"
    )
    return 0


def run_user_export_standalone(
    user_id: str, target_dir: str | None = None, with_knowledge: bool = False
) -> int:
    """把用户长期记忆导出为 JSON + Markdown
    （`python cli.py user-export <user_id> [dir] [--with-knowledge]`）。

    ``--with-knowledge`` 把全局知识库片段一并写入 JSON（一体化备份，便于
    迁移到另一台机器后一次恢复）。

    与交互模式的 export 命令输出同两份文件（utils.exporter.export_files）：
    偏好、历史行程、统计与全部对话历史；默认写入 `exports/`。

    Returns:
        0 导出成功；1 参数 / 读取 / 写入失败
    """
    import re
    from datetime import datetime
    from pathlib import Path

    from utils.exporter import export_files

    if not re.fullmatch(r"[\w-]{1,64}", user_id) or user_id.startswith("-"):
        print("用户 ID 只能包含中文、字母、数字、下划线和短横线")
        return 1
    try:
        store = _open_cli_store(user_id)
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        print(f"打开长期记忆失败：{exc}")
        return 1
    try:
        try:
            data = {
                "user_id": user_id,
                "session_id": "",
                "exported_at": datetime.now().isoformat(timespec="seconds"),
                "preferences": store.get_preference(),
                "trip_history": store.get_trip_history(limit=None),
                "chat_history": store.get_chat_history(),
                "session_titles": store.get_session_titles(),
                "pinned_sessions": store.get_pinned_sessions(),
                "archived_sessions": store.get_archived_sessions(),
                "statistics": store.get_statistics(),
            }
        except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
            print(f"读取用户数据失败：{exc}")
            return 1
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()

    if with_knowledge:
        from utils.knowledge_admin import export_documents

        try:
            data["knowledge"] = export_documents()
        except Exception as exc:  # noqa: BLE001 - 备份不完整时中止而不是静默
            print(f"读取知识库失败：{exc}")
            return 1

    out_dir = Path(target_dir) if target_dir else Path("exports")
    try:
        paths = export_files(data, out_dir)
    except OSError as exc:
        print(f"写入失败：{exc}")
        return 1
    for path in paths:
        print(f"已导出：{path}")
    return 0


def run_user_import_standalone(
    user_id: str,
    json_path: str,
    as_json: bool = False,
    dry_run: bool = False,
    skip_knowledge: bool = False,
) -> int:
    """从 user-export 的 JSON 备份恢复用户数据
    （`python cli.py user-import <user_id> <json_path> [--dry-run] [--json]`）。

    恢复偏好、行程、聊天消息与会话元数据（标题 / 置顶 / 归档）；统计字段
    由存储层在写入时自行累计。重复导入幂等：行程按存储层去重，聊天消息按
    （会话, 角色, 内容）多重集去重。非法条目逐条跳过并列出。
    ``dry_run=True`` 只预览将新增 / 更新 / 跳过的数量，不写库。

    Returns:
        0 至少恢复一类数据（含部分条目被跳过）；1 参数 / 读取 / 写入失败
        或没有可恢复的数据
    """
    import io
    import re

    from utils.user_import import (
        MAX_BACKUP_BYTES,
        import_user_data,
        parse_user_export,
        preview_user_import,
    )

    def _fail(message: str) -> int:
        if as_json:
            print(
                json.dumps(
                    {"ok": False, "error": message},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(message)
        return 1

    if not re.fullmatch(r"[\w-]{1,64}", user_id) or user_id.startswith("-"):
        return _fail("用户 ID 只能包含中文、字母、数字、下划线和短横线")
    if not json_path or json_path.startswith("-"):
        return _fail("请提供 JSON 备份文件路径")
    try:
        if os.path.getsize(json_path) > MAX_BACKUP_BYTES:
            return _fail("备份文件超过 50 MB，请拆分后恢复")
    except OSError:
        # 不存在 / 不可读交给下面的读取步骤给出具体原因
        pass
    try:
        text = io.open(json_path, "r", encoding="utf-8-sig").read()
    except FileNotFoundError:
        return _fail(f"文件不存在：{json_path}")
    except UnicodeDecodeError as exc:
        return _fail(f"读取备份失败：{exc}（文件需为 UTF-8 编码）")
    except OSError as exc:
        return _fail(f"读取备份失败：{exc}")

    sections, errors = parse_user_export(text)
    if skip_knowledge:
        sections.pop("knowledge", None)
    # 只含 knowledge 段的备份在 --skip-knowledge 后没有用户数据可恢复：
    # sections 里仍有空列表键（truthy），必须按「内容是否为空」判断
    has_user_data = any(
        sections.get(key)
        for key in (
            "preferences",
            "trips",
            "messages",
            "titles",
            "pinned",
            "archived",
            "knowledge",
        )
    )
    if not sections or not has_user_data:
        hint = (
            "（备份只含知识片段，已按 --skip-knowledge 跳过）"
            if skip_knowledge
            else ""
        )
        detail = "；".join(errors[:5])
        return _fail(
            f"没有可恢复的数据{hint}{f'（{detail}）' if detail else ''}"
        )

    try:
        store = _open_cli_store(user_id)
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        return _fail(f"打开长期记忆失败：{exc}")
    try:
        try:
            if dry_run:
                counts = preview_user_import(store, sections)
            else:
                counts = import_user_data(store, sections)
        except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
            action = "预览" if dry_run else "写入"
            return _fail(f"{action}失败：{exc}")
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()

    knowledge_error = str(counts.pop("knowledge_error", "") or "")
    if as_json:
        print(
            json.dumps(
                {
                    "ok": not knowledge_error,
                    "user_id": user_id,
                    "path": json_path,
                    "dry_run": dry_run,
                    "skip_knowledge": skip_knowledge,
                    **counts,
                    **({"knowledge_error": knowledge_error} if knowledge_error else {}),
                    "skipped": errors,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        prefix = "预览（未写入）：" if dry_run else "已恢复："
        summary = (
            f"{prefix}偏好 {counts['preferences']} 项，行程新增 "
            f"{counts['trips_created']} 条 / 更新 {counts['trips_updated']} 条，"
            f"聊天消息 {counts['messages_added']} 条"
            f"（跳过重复 {counts['messages_skipped']} 条），标题 "
            f"{counts['titles']} 个，置顶 {counts['pinned']} 个，归档 "
            f"{counts['archived']} 个"
        )
        if dry_run:
            summary += f"，知识片段 {counts.get('knowledge', 0)} 条"
        elif "knowledge_imported" in counts:
            summary += (
                f"，知识片段新增 {counts['knowledge_imported']} 条"
                f"（跳过 {counts['knowledge_skipped']} 条）"
            )
        print(summary)
        if knowledge_error:
            print(f"❌ {knowledge_error}（用户数据已恢复，可稍后重跑补齐知识片段）")
        for error in errors[:10]:
            print(f"  跳过：{error}")
    return 1 if knowledge_error else 0


def _parse_kb_write_args(
    args: list[str],
) -> tuple[list[str], dict[str, str]] | None:
    """解析 kb-add / kb-update 的 ``<file> [--category X] [--title Y]``。

    返回 (位置参数, 元数据)；未知参数、缺值或位置参数数量不符由调用方检查。
    """
    positional: list[str] = []
    metadata: dict[str, str] = {}
    index = 0
    while index < len(args):
        item = args[index]
        lowered = item.lower()
        if lowered in ("--category", "--title") or lowered.startswith(
            ("--category=", "--title=")
        ):
            key = "category" if lowered.startswith("--category") else "title"
            if "=" in item:
                value = item.split("=", 1)[1]
            else:
                index += 1
                if index >= len(args):
                    return None
                value = args[index]
            if not value.strip() or value.startswith("-"):
                return None
            metadata[key] = value.strip()
        elif item.startswith("-"):
            return None
        else:
            positional.append(item)
        index += 1
    return positional, metadata


def _read_kb_file(path: str) -> str | None:
    from pathlib import Path

    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        print(f"读取文件失败：{exc}（文件需为 UTF-8 编码）")
        return None
    except OSError as exc:
        print(f"读取文件失败：{exc}")
        return None
    if not text.strip():
        print("知识内容不能为空")
        return None
    return text


def run_knowledge_add_standalone(
    path: str, metadata: dict[str, str] | None = None
) -> int:
    """从文件新增知识片段（`python cli.py kb-add <file> [--category X] [--title Y]`）。

    长内容与 Web / 导入一致按语义边界自动拆分（返回 id 为最后一片）。

    Returns:
        0 添加成功；1 文件 / 知识库 / 写入失败
    """
    from utils.knowledge_admin import add_document
    from utils.knowledge_stats import knowledge_status

    status = knowledge_status()
    if not status.get("available"):
        print(f"知识库不可用：{status.get('reason', '未知原因')}")
        return 1
    text = _read_kb_file(path)
    if text is None:
        return 1
    try:
        result = add_document(text, metadata)
    except FileNotFoundError:
        print("知识库未初始化")
        return 1
    except ValueError as exc:
        print(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        print(f"添加失败：{exc}")
        return 1
    parts = int(result.get("parts") or 1)
    total = result.get("total_count")
    suffix = f"，拆分为 {parts} 片" if parts > 1 else ""
    count = f"（共 {total} 条{suffix}）" if total is not None else ""
    print(f"已添加知识片段 #{result.get('id')}{count}")
    return 0


def run_knowledge_update_standalone(
    doc_id: int, path: str, metadata: dict[str, str] | None = None
) -> int:
    """从文件覆盖更新知识片段（`python cli.py kb-update <id> <file> [--category X] [--title Y]`）。

    带 group 的多分片记录按整组替换（与面板「编辑整份」同一实现）。

    Returns:
        0 更新成功；1 文件 / 知识库 / 更新失败
    """
    from utils.knowledge_admin import get_document, update_document
    from utils.knowledge_stats import knowledge_status

    status = knowledge_status()
    if not status.get("available"):
        print(f"知识库不可用：{status.get('reason', '未知原因')}")
        return 1
    text = _read_kb_file(path)
    if text is None:
        return 1
    # 只传 --category/--title 时 update_document 会整体重建 metadata：
    # 先读出现有记录，把库中已有的 category/title/source/parent_doc 等
    # 字段并入（本次显式提供的值覆盖），与面板整份编辑的语义一致
    try:
        existing = get_document(doc_id)
    except FileNotFoundError:
        print("知识库未初始化")
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        print(f"读取片段失败：{exc}")
        return 1
    if existing is None:
        print(f"知识片段 {doc_id} 不存在")
        return 1
    merged_metadata = {
        str(key)[:32]: str(value)[:200]
        for key, value in (existing.get("metadata") or {}).items()
    }
    merged_metadata.update(metadata or {})
    try:
        result = update_document(doc_id, text, merged_metadata)
    except FileNotFoundError:
        print("知识库未初始化")
        return 1
    except ValueError as exc:
        print(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        print(f"更新失败：{exc}")
        return 1
    parts = int(result.get("parts") or 1)
    suffix = f"，拆分为 {parts} 片" if parts > 1 else ""
    print(f"已更新知识片段 #{doc_id}{suffix}")
    leftover = result.get("leftover_ids") or []
    if leftover:
        ids = ", ".join(str(item) for item in leftover)
        print(f"注意：{len(leftover)} 个旧分片删除失败（id：{ids}），可稍后重试删除")
    return 0


def _parse_cli_int(value: str) -> int | None:
    """ASCII 十进制整数解析（用于 <id> / limit 参数）。

    ``str.isdigit()`` 对 '²' 等 Unicode 数字返回 True，直接 int() 会抛
    ValueError；超长数字串（>4300 位）在 Python 3.12 也会被拒绝。
    """
    if not value.isascii() or not value.isdigit():
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _parse_kb_search_args(args: list[str]) -> tuple[str, int, bool] | None:
    """解析 kb-search 的 <关键词> [--top-k N] [--json]。

    关键词可由多个词组成（以空格拼接）；--top-k 取 1..50 的 ASCII 十进制
    整数；未知参数或缺关键词返回 None。
    """
    top_k = 5
    as_json = False
    words: list[str] = []
    index = 0
    while index < len(args):
        item = args[index]
        lowered = item.lower()
        if lowered == "--json":
            as_json = True
        elif lowered == "--top-k" or lowered.startswith("--top-k="):
            if lowered == "--top-k":
                index += 1
                if index >= len(args):
                    return None
                value = args[index]
            else:
                value = item[len("--top-k="):]
            parsed = _parse_cli_int(value)
            if parsed is None or not 1 <= parsed <= 50:
                return None
            top_k = parsed
        elif item.startswith("-"):
            return None
        else:
            words.append(item)
        index += 1
    query = " ".join(words).strip()
    if not query:
        return None
    return query, top_k, as_json


def run_knowledge_delete_standalone(doc_id: int, whole_group: bool = False) -> int:
    """删除一个知识片段（用于 `python cli.py kb-delete <id> [--group]`）。

    ``--group`` 时连同同组分片一起删除（仅新拆分、带 group 元数据的记录）。

    Returns:
        0 删除成功；1 片段不存在或删除失败
    """
    from utils.knowledge_admin import delete_document

    try:
        removed = delete_document(doc_id, whole_group=whole_group)
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        print(f"删除失败：{exc}")
        return 1
    if not removed:
        print(f"片段不存在：#{doc_id}")
        return 1
    if whole_group and removed > 1:
        print(f"已删除整组分片 #{doc_id}（共 {removed} 片）")
    elif whole_group:
        print(f"未找到同组分片，已删除单个片段 #{doc_id}")
    else:
        print(f"已删除知识片段 #{doc_id}")
    return 0


def run_knowledge_dedupe_standalone(
    dry_run: bool = False, as_json: bool = False
) -> int:
    """清理内容完全相同的独立知识片段组（`python cli.py kb-dedupe [--dry-run] [--json]`）。

    每组保留最小的 id，删除其余记录。只处理不带 group 的独立片段：多分片
    记录内部的相同段落是正常的，必须通过整份编辑 / 整组删除维护，按单条
    删除会破坏整篇文档。

    Returns:
        0 清理完成（含无重复 / dry-run）；1 读取 / 删除失败
    """
    from utils.knowledge_admin import delete_document, find_duplicate_contents

    def _fail(message: str, **extra: Any) -> int:
        if as_json:
            print(
                json.dumps(
                    {"ok": False, "error": message, **extra},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(message)
        return 1

    try:
        groups = find_duplicate_contents()
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        return _fail(f"读取知识库失败：{exc}")

    plan: list[dict[str, Any]] = []
    for content, ids in groups:
        keep = min(ids)
        remove = sorted(doc_id for doc_id in ids if doc_id != keep)
        if remove:
            plan.append({"content": content, "keep": keep, "remove": remove})
    total_remove = sum(len(item["remove"]) for item in plan)

    if dry_run or not plan:
        if as_json:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "dry_run": dry_run,
                        "groups": plan,
                        "would_remove": total_remove,
                        "removed": 0,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if not plan:
            print("没有重复内容的独立知识片段")
            return 0
        print(
            f"发现 {len(plan)} 组重复内容，将删除 {total_remove} 条"
            "（保留每组最小 id）："
        )
        for item in plan:
            preview = " ".join(item["content"].split())[:60]
            id_text = "、".join(f"#{doc_id}" for doc_id in item["remove"])
            print(f"  保留 #{item['keep']}，删除 {id_text}  {preview}")
        print("去掉 --dry-run 即可执行删除")
        return 0

    removed = 0
    failure: str | None = None
    removed_ids: list[int] = []
    for item in plan:
        for doc_id in item["remove"]:
            try:
                count = delete_document(doc_id)
            except Exception as exc:  # noqa: BLE001 - 部分删除后失败要如实报告
                failure = f"删除 #{doc_id} 失败：{exc}（已删除 {removed} 条）"
                break
            removed += int(count or 0)
            removed_ids.append(doc_id)
        if failure:
            break
    if as_json:
        if failure is not None:
            return _fail(failure, removed=removed, removed_ids=removed_ids)
        print(
            json.dumps(
                {
                    "ok": True,
                    "dry_run": False,
                    "groups": plan,
                    "would_remove": total_remove,
                    "removed": removed,
                    "removed_ids": removed_ids,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if failure is not None:
        print(failure)
        return 1
    print(f"已删除 {removed} 条重复知识片段（{len(plan)} 组，保留每组最小 id）")
    return 0


def run_knowledge_duplicates_standalone(
    limit: int = 20, as_json: bool = False
) -> int:
    """列出内容完全相同的独立知识片段组（`python cli.py kb-duplicates [limit] [--json]`）。

    ``limit`` 为 0 时显示全部；只统计不带 group 的独立片段（多分片记录内部
    的相同段落属于同一篇文档，不能按单条重复处理）。``--json`` 输出结构化
    结果（total_groups / total_fragments / shown / groups），供脚本消费。

    Returns:
        0 扫描成功（有无重复都算成功）；1 读取失败
    """
    from utils.knowledge_admin import find_duplicate_contents

    try:
        groups = find_duplicate_contents()
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        if as_json:
            print(
                json.dumps(
                    {"ok": False, "error": str(exc)},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"读取失败：{exc}")
        return 1
    shown = groups if limit <= 0 else groups[:limit]
    entries = [
        {
            "content": content,
            "preview": " ".join(content.split())[:60],
            "ids": list(ids),
        }
        for content, ids in shown
    ]
    total = sum(len(ids) for _, ids in groups)
    if as_json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "total_groups": len(groups),
                    "total_fragments": total,
                    "shown": len(shown),
                    "groups": entries,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if not groups:
        print("没有重复内容的独立知识片段")
        return 0
    print(f"发现 {len(groups)} 组重复内容（共 {total} 条片段）：")
    for entry in entries:
        joined = "、".join(f"#{doc_id}" for doc_id in entry["ids"])
        print(f"  {len(entry['ids'])} 条 · {joined}  {entry['preview']}")
    if len(groups) > len(shown):
        print(f"  … 其余 {len(groups) - len(shown)} 组未显示（limit=0 显示全部）")
    print("提示：以上均为未分组的独立片段，确认保留哪一份后可删除其余")
    return 0


def run_trips_dedupe_standalone(
    user_id: str, dry_run: bool = False, as_json: bool = False
) -> int:
    """清理历史行程中的完全重复记录
    （`python cli.py trips-dedupe <user_id> [--dry-run] [--json]`）。

    同路线 + 同起止日期视为重复：保留最早一条，删除其余记录（统计同步更新）。
    ``dry_run`` 仅列出将被删除的记录，不做修改；``--json`` 输出结构化结果
    （将删除 / 已删除条目与无法自动处理的 trip_id）。

    Returns:
        0 清理完成（含无重复 / dry-run）；1 参数 / 读取 / 删除失败
    """
    import re

    def _fail(message: str, **extra: Any) -> int:
        if as_json:
            print(
                json.dumps(
                    {"ok": False, "error": message, **extra},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(message)
        return 1

    def _emit(payload: dict[str, Any]) -> None:
        if as_json:
            print(
                json.dumps(
                    {"ok": True, **payload}, ensure_ascii=False, indent=2
                )
            )

    if not re.fullmatch(r"[\w-]{1,64}", user_id):
        return _fail("用户 ID 只能包含中文、字母、数字、下划线和短横线")

    from config import STORAGE_CONFIG
    from context.long_term_memory import LongTermMemory
    from context.storage import create_long_term_memory

    try:
        store = create_long_term_memory(user_id, "data/memory")
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        return _fail(f"打开长期记忆失败：{exc}")

    try:
        # PostgreSQL 配置了但连接失败时，工厂会静默回退本地 JSON；维护命令
        # 必须确保清理的是目标后端，避免对过期本地数据“成功”而真实库未处理
        if STORAGE_CONFIG.get("postgres_dsn") and isinstance(store, LongTermMemory):
            return _fail(
                "PostgreSQL 已配置但连接失败（已回退本地 JSON），"
                "未执行去重；请检查数据库后重试"
            )

        try:
            trips = store.get_trip_history(limit=None)
        except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
            return _fail(f"读取行程失败：{exc}")

        groups: dict[tuple[str, str, str, str], list[str]] = {}
        for trip in trips:
            start_date = str(trip.get("start_date") or "")
            if not start_date:
                continue
            key = (
                str(trip.get("origin") or ""),
                str(trip.get("destination") or ""),
                start_date,
                str(trip.get("end_date") or start_date),
            )
            groups.setdefault(key, []).append(str(trip.get("trip_id") or ""))
        by_id = {str(trip.get("trip_id") or ""): trip for trip in trips}

        def _entry(trip_id: str) -> dict[str, Any]:
            trip = by_id.get(trip_id, {})
            return {
                "trip_id": trip_id,
                "origin": str(trip.get("origin") or ""),
                "destination": str(trip.get("destination") or ""),
                "start_date": str(trip.get("start_date") or ""),
                "end_date": str(trip.get("end_date") or ""),
            }

        if all(len(ids) <= 1 for ids in groups.values()):
            _emit(
                {
                    "user_id": user_id,
                    "dry_run": dry_run,
                    "total_trips": len(trips),
                    "duplicates": [],
                    "ambiguous_ids": [],
                    "removed": 0,
                }
            )
            if not as_json:
                print(f"用户 {user_id}：没有重复行程（共 {len(trips)} 条）")
            return 0

        # 同一 trip_id 出现在多条不同行程里时，delete_trip 可能删错行：
        # 这类 id 不自动处理，提示人工清理
        id_groups: dict[str, set[tuple[str, str, str, str]]] = {}
        for key, ids in groups.items():
            for trip_id in ids:
                id_groups.setdefault(trip_id, set()).add(key)
        ambiguous = {trip_id for trip_id, keys in id_groups.items() if len(keys) > 1}
        if ambiguous and not as_json:
            print(
                "警告：以下 trip_id 同时属于多条不同行程，无法安全自动删除，"
                "请人工处理：" + "、".join(sorted(ambiguous))
            )

        # 删除前重新读取：若某组保留的那条已被并发删除，跳过该组，
        # 避免把仅剩的副本也删掉。JSON 后端的 get_trip_history 返回进程启动时
        # 的快照，必须先 reload，否则复核永远是“未变化”
        reload = getattr(store, "reload", None)
        if callable(reload):
            reload()
        try:
            fresh_ids = {
                str(trip.get("trip_id") or "")
                for trip in store.get_trip_history(limit=None)
            }
        except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
            return _fail(f"读取行程失败：{exc}")

        duplicate_entries = [
            (ids[0], trip_id)
            for ids in groups.values()
            for trip_id in ids[1:]
            if ids[0] in fresh_ids and trip_id not in ambiguous
        ]
        duplicates = [trip_id for _, trip_id in duplicate_entries]

        if dry_run:
            _emit(
                {
                    "user_id": user_id,
                    "dry_run": True,
                    "total_trips": len(trips),
                    "would_remove": len(duplicates),
                    "duplicates": [_entry(trip_id) for trip_id in duplicates],
                    "ambiguous_ids": sorted(ambiguous),
                    "removed": 0,
                }
            )
            if as_json:
                return 0
            if not duplicates:
                print(f"用户 {user_id}：没有需要删除的重复行程（共 {len(trips)} 条）")
                return 0
            print(
                f"用户 {user_id}：发现 {len(duplicates)} 条重复行程（--dry-run，不删除）："
            )
            for trip_id in duplicates:
                trip = by_id.get(trip_id, {})
                route = f"{trip.get('origin') or '—'} → {trip.get('destination') or '—'}"
                dates = (
                    " ~ ".join(
                        part
                        for part in (trip.get("start_date"), trip.get("end_date"))
                        if part
                    )
                    or "日期未记录"
                )
                print(f"  将删除 {trip_id}（{route}，{dates}）")
            print("去掉 --dry-run 即可执行删除")
            return 0

        removed = 0
        failure: str | None = None
        try:
            for kept_id, trip_id in duplicate_entries:
                if not trip_id:
                    continue
                # 与组内保留项同 id 的副本（旧版给两条行程生成过同一个
                # trip_id）：delete_trip 删最早一条，会把要保留的记录删掉、
                # 留下后来的重复行——同 id 时必须删除最后插入的一条
                deleter = (
                    store.delete_duplicate_trip
                    if trip_id == kept_id
                    and hasattr(store, "delete_duplicate_trip")
                    else store.delete_trip
                )
                if deleter(trip_id):
                    removed += 1
        except Exception as exc:  # noqa: BLE001 - 部分删除后失败要如实报告
            failure = str(exc)
        if as_json:
            if failure is not None:
                return _fail(
                    f"删除过程中失败：{failure}（已删除 {removed} 条）",
                    removed=removed,
                    duplicates=[_entry(trip_id) for trip_id in duplicates],
                )
            _emit(
                {
                    "user_id": user_id,
                    "dry_run": False,
                    "total_trips": len(trips),
                    "removed": removed,
                    "duplicates": [_entry(trip_id) for trip_id in duplicates],
                    "ambiguous_ids": sorted(ambiguous),
                }
            )
            return 1 if ambiguous else 0
        if failure is not None:
            print(f"删除过程中失败：{failure}（已删除 {removed} 条）")
            return 1
        if duplicates:
            print(f"用户 {user_id}：删除 {removed} 条重复行程（保留最早记录）")
        else:
            print(f"用户 {user_id}：没有需要删除的重复行程（共 {len(trips)} 条）")
        return 1 if ambiguous else 0
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()


def _open_cli_store(user_id: str):
    """打开长期记忆；PostgreSQL 配置错误回退 JSON 时拒绝执行维护命令。"""
    from config import STORAGE_CONFIG
    from context.long_term_memory import LongTermMemory
    from context.storage import create_long_term_memory

    store = create_long_term_memory(user_id, "data/memory")
    if STORAGE_CONFIG.get("postgres_dsn") and isinstance(store, LongTermMemory):
        close = getattr(store, "close", None)
        if callable(close):
            close()
        raise RuntimeError(
            "PostgreSQL 已配置但连接失败（已回退本地 JSON），请检查数据库后重试"
        )
    return store


def _local_timestamp_text(value: str) -> str:
    """把 ISO 时间转换为本地时区的展示文本。

    PostgreSQL 返回带时区的 UTC 时间，JSON 后端存本地 naive 时间；直接截断
    ISO 字符串会让同一条会话在两种后端下显示相差数小时。
    """
    from datetime import datetime

    text = str(value or "")
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return text[:19].replace("T", " ")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _session_summaries(store) -> list[dict]:
    """聚合会话摘要（标题 / 消息数 / 最近时间 / 置顶 / 归档），按更新时间倒序。"""
    from utils.stored_message import render_stored_message

    history = store.get_chat_history()
    titles = store.get_session_titles() or {}
    pinned = {str(item) for item in (store.get_pinned_sessions() or [])}
    archived = {str(item) for item in (store.get_archived_sessions() or [])}
    grouped: dict[str, dict] = {}
    for message in history:
        session_id = message.get("session_id")
        if not session_id:
            continue
        sid = str(session_id)
        entry = grouped.setdefault(
            sid,
            {"session_id": sid, "title": "", "messages": 0, "updated_at": ""},
        )
        entry["messages"] += 1
        timestamp = str(message.get("timestamp") or "")
        if timestamp and (
            not entry["updated_at"]
            or chronological_key(timestamp) >= chronological_key(entry["updated_at"])
        ):
            # 取转录里「最大时间戳」而不是最后一条：恢复备份按时间升序追加，
            # 旧消息排在末尾时最近更新会被错误回拨
            entry["updated_at"] = timestamp
        if not entry["title"] and message.get("role") == "user":
            title = " ".join(str(message.get("content", "")).split())
            if title:
                entry["title"] = title[:40]
        # 助手消息存的是编排 JSON：用渲染后的正文做关键词匹配（与 Web
        # 面板同口径），直接匹配原始 JSON 会把「回答里有没有某个词」判错
        if message.get("role") == "user":
            raw_text = str(message.get("content", ""))
        else:
            raw_text = render_stored_message(message)
        content = " ".join(raw_text.split())
        if content:
            entry.setdefault("_parts", []).append(content)
    for entry in grouped.values():
        entry["title"] = str(
            titles.get(entry["session_id"]) or entry["title"] or "新对话"
        )
        entry["pinned"] = entry["session_id"] in pinned
        entry["archived"] = entry["session_id"] in archived
        # 标题 + 消息内容（已归一空白）供 --query 关键词过滤与命中片段
        entry["_parts"] = [entry["title"], *entry.pop("_parts", [])]
    return sorted(
        grouped.values(),
        key=lambda item: chronological_key(item["updated_at"]),
        reverse=True,
    )


def run_sessions_list_standalone(
    user_id: str,
    archived_only: bool = False,
    include_all: bool = False,
    query: str = "",
    as_json: bool = False,
) -> int:
    """列出用户会话（`python cli.py sessions <user_id> [--archived|--all] [--query 关键词] [--json]`）。

    ``query`` 在标题与消息内容中做不区分大小写的包含匹配，命中时附带片段；
    ``as_json`` 输出机器可读的会话摘要列表（含命中片段）。

    Returns:
        0 列出成功（含无会话）；1 参数 / 读取失败
    """
    import re

    if not re.fullmatch(r"[\w-]{1,64}", user_id) or user_id.startswith("-"):
        print("用户 ID 只能包含中文、字母、数字、下划线和短横线")
        return 1
    try:
        store = _open_cli_store(user_id)
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        print(f"打开长期记忆失败：{exc}")
        return 1
    try:
        try:
            summaries = _session_summaries(store)
        except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
            print(f"读取会话失败：{exc}")
            return 1
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()

    archived_total = sum(1 for item in summaries if item["archived"])
    total_all = len(summaries)
    needle = query.strip().lower()
    if needle:
        matched = []
        for item in summaries:
            hit = next(
                (part for part in item["_parts"] if needle in part.lower()), None
            )
            if hit is not None:
                position = hit.lower().find(needle)
                start = max(0, position - 20)
                item["_snippet"] = hit[start : start + 40]
                matched.append(item)
        summaries = matched
    if include_all:
        visible = summaries
        scope = "全部"
    elif archived_only:
        visible = [item for item in summaries if item["archived"]]
        scope = "归档"
    else:
        visible = [item for item in summaries if not item["archived"]]
        scope = "未归档"
        # 与 Web 侧栏一致：置顶会话排在前面
        visible.sort(key=lambda item: not item["pinned"])

    keyword = f"（关键词：{query.strip()}）" if needle else ""
    if as_json:
        print(
            json.dumps(
                {
                    "user_id": user_id,
                    "total": len(visible),
                    "archived_total": archived_total,
                    "sessions": [
                        {
                            "session_id": item["session_id"],
                            "title": item["title"],
                            "messages": item["messages"],
                            "updated_at": item["updated_at"],
                            "pinned": item["pinned"],
                            "archived": item["archived"],
                            **(
                                {"snippet": item["_snippet"]}
                                if item.get("_snippet")
                                else {}
                            ),
                        }
                        for item in visible
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if not visible:
        if needle:
            print(
                f"用户 {user_id}：没有匹配{keyword}的{scope}会话"
                f"（共 {total_all} 个，归档 {archived_total} 个）"
            )
        else:
            print(
                f"用户 {user_id}：没有{scope}会话"
                f"（共 {total_all} 个，归档 {archived_total} 个）"
            )
        return 0

    print(
        f"用户 {user_id}：{len(visible)} 个{scope}会话{keyword}"
        f"（共 {total_all} 个，归档 {archived_total} 个）"
    )
    for item in visible:
        if item["archived"]:
            status = "归档"
        elif item["pinned"]:
            status = "置顶"
        else:
            status = "—"
        updated = _local_timestamp_text(item["updated_at"]) or "—"
        title = item["title"][:30]
        print(
            f"  [{status}] {item['session_id']:<16} {title:<32}"
            f" {item['messages']:>3} 条  {updated}"
        )
        if item.get("_snippet"):
            print(f"       > {item['_snippet']}")
    return 0


def run_session_archive_standalone(
    user_id: str, session_id: str, archived: bool = True, as_json: bool = False
) -> int:
    """归档 / 恢复一个会话
    （`python cli.py session-archive <user> <session> [--restore] [--json]`）。

    Returns:
        0 操作成功；1 参数 / 读取 / 写入失败
    """
    import re

    def _fail(message: str) -> int:
        if as_json:
            print(
                json.dumps(
                    {"ok": False, "error": message},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(message)
        return 1

    if not re.fullmatch(r"[\w-]{1,64}", user_id) or user_id.startswith("-"):
        return _fail("用户 ID 只能包含中文、字母、数字、下划线和短横线")
    if not re.fullmatch(r"[\w-]{1,64}", session_id) or session_id.startswith("-"):
        return _fail("会话 ID 只能包含中文、字母、数字、下划线和短横线")
    try:
        store = _open_cli_store(user_id)
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        return _fail(f"打开长期记忆失败：{exc}")
    try:
        try:
            exists = bool(store.get_chat_history(session_id=session_id))
        except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
            return _fail(f"读取会话失败：{exc}")
        if not exists:
            return _fail(f"会话 {session_id} 不存在或没有消息")
        try:
            applied = store.set_session_archived(session_id, archived)
            if applied is False:
                return _fail(f"会话 {session_id} 操作未生效（可能已被删除）")
        except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
            return _fail(f"操作失败：{exc}")
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()
    if as_json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "user_id": user_id,
                    "session_id": session_id,
                    "archived": archived,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"已{'归档' if archived else '恢复'}会话 {session_id}")
    return 0


def run_session_delete_standalone(
    user_id: str, session_id: str, as_json: bool = False
) -> int:
    """删除一个会话的全部消息
    （`python cli.py session-delete <user> <session> [--json]`）。

    与面板 DELETE ``/api/sessions/{id}`` 同一语义：长期记忆中该会话的消息
    全部删除，并清理短期记忆（Redis 会话键与故障期重放登记）；配置
    ``REDIS_URL`` 时同时写入共享删除墓碑（与 Web 相同的 ``aligo:deleted``
    键，TTL 300 秒），其它实例的旧标签页下一次请求即 404。CLI 是单次
    进程、不持有在途对话；若 Web 服务正在同一会话上流式回复，请先停止再
    删除（与其它 CLI 破坏性命令一致）。

    Returns:
        0 删除成功；1 参数 / 读取 / 删除失败（含会话不存在）
    """
    import re

    from context.storage import clear_short_term_session

    def _fail(message: str) -> int:
        if as_json:
            print(
                json.dumps(
                    {"ok": False, "error": message},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(message)
        return 1

    if not re.fullmatch(r"[\w-]{1,64}", user_id) or user_id.startswith("-"):
        return _fail("用户 ID 只能包含中文、字母、数字、下划线和短横线")
    if not re.fullmatch(r"[\w-]{1,64}", session_id) or session_id.startswith("-"):
        return _fail("会话 ID 只能包含中文、字母、数字、下划线和短横线")
    try:
        store = _open_cli_store(user_id)
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        return _fail(f"打开长期记忆失败：{exc}")
    try:
        try:
            exists = bool(store.get_chat_history(session_id=session_id))
        except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
            return _fail(f"读取会话失败：{exc}")
        if not exists:
            return _fail(f"会话 {session_id} 不存在或没有消息")
        try:
            removed = store.delete_chat_session(session_id)
        except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
            return _fail(f"删除会话失败：{exc}")
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()
    # 短期记忆清理失败不阻断：长期记忆已删除，残留由故障重放 / 过期回收兜底
    clear_short_term_session(session_id, user_id)
    # 共享删除墓碑：配置 REDIS_URL 时其它 Web 实例 / 进程的旧标签页下一次
    # 请求即 404，不会被进程内缓存把已删除内容写回；未配置 Redis 时不同
    # 进程之间没有共享通道，提示用户在旧页面刷新
    from utils.tombstones import mark_session_deleted

    shared_tombstone = mark_session_deleted(user_id, session_id)
    if as_json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "user_id": user_id,
                    "session_id": session_id,
                    "deleted": removed,
                    "shared_tombstone": shared_tombstone,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"已删除会话 {session_id}（{removed} 条消息）")
        if not shared_tombstone:
            print(
                "提示：未启用共享删除墓碑（REDIS_URL），其它已打开的 Web "
                "页面请刷新，避免继续向该会话发送消息"
            )
    return 0


def run_session_title_standalone(
    user_id: str,
    session_id: str,
    title: str,
    as_json: bool = False,
) -> int:
    """设置会话标题（`python cli.py session-title <user> <session> <标题|--reset> [--json]`）。

    空标题 / ``--reset`` 表示恢复自动标题（由第一条消息生成）。与面板
    PUT ``/api/sessions/{id}/title`` 同一限制：标题最多 60 个字符。

    Returns:
        0 设置成功；1 参数 / 读取 / 写入失败（含会话不存在）
    """
    import re

    def _fail(message: str) -> int:
        if as_json:
            print(
                json.dumps(
                    {"ok": False, "error": message},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(message)
        return 1

    if not re.fullmatch(r"[\w-]{1,64}", user_id) or user_id.startswith("-"):
        return _fail("用户 ID 只能包含中文、字母、数字、下划线和短横线")
    if not re.fullmatch(r"[\w-]{1,64}", session_id) or session_id.startswith("-"):
        return _fail("会话 ID 只能包含中文、字母、数字、下划线和短横线")
    cleaned = title.strip()
    if len(cleaned) > 60:
        return _fail("标题最多 60 个字符")

    try:
        store = _open_cli_store(user_id)
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        return _fail(f"打开长期记忆失败：{exc}")
    try:
        try:
            exists = bool(store.get_chat_history(session_id=session_id))
        except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
            return _fail(f"读取会话失败：{exc}")
        if not exists:
            return _fail(f"会话 {session_id} 不存在或没有消息")
        try:
            applied = store.set_session_title(session_id, cleaned)
            if applied is False:
                return _fail(f"会话 {session_id} 操作未生效（可能已被删除）")
        except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
            return _fail(f"操作失败：{exc}")
        try:
            saved = str((store.get_session_titles() or {}).get(session_id) or "")
        except Exception:  # noqa: BLE001 - 读回失败时按提交值展示
            saved = cleaned
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()

    if as_json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "user_id": user_id,
                    "session_id": session_id,
                    "title": saved,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if saved:
        print(f"已设置会话 {session_id} 标题：{saved}")
    else:
        print(f"已恢复会话 {session_id} 的自动标题")
    return 0


def run_session_pin_standalone(
    user_id: str, session_id: str, pinned: bool = True, as_json: bool = False
) -> int:
    """置顶 / 取消置顶会话
    （`python cli.py session-pin <user> <session> [--off] [--json]`）。

    Returns:
        0 操作成功；1 参数 / 读取 / 写入失败（含会话不存在）
    """
    import re

    def _fail(message: str) -> int:
        if as_json:
            print(
                json.dumps(
                    {"ok": False, "error": message},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(message)
        return 1

    if not re.fullmatch(r"[\w-]{1,64}", user_id) or user_id.startswith("-"):
        return _fail("用户 ID 只能包含中文、字母、数字、下划线和短横线")
    if not re.fullmatch(r"[\w-]{1,64}", session_id) or session_id.startswith("-"):
        return _fail("会话 ID 只能包含中文、字母、数字、下划线和短横线")

    try:
        store = _open_cli_store(user_id)
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        return _fail(f"打开长期记忆失败：{exc}")
    try:
        try:
            exists = bool(store.get_chat_history(session_id=session_id))
        except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
            return _fail(f"读取会话失败：{exc}")
        if not exists:
            return _fail(f"会话 {session_id} 不存在或没有消息")
        try:
            applied = store.set_session_pinned(session_id, pinned)
            if applied is False:
                return _fail(f"会话 {session_id} 操作未生效（可能已被删除）")
        except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
            return _fail(f"操作失败：{exc}")
        try:
            current = [str(item) for item in store.get_pinned_sessions() or []]
            saved = session_id in current
        except Exception:  # noqa: BLE001 - 读回失败时按提交值展示
            saved = pinned
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()

    if as_json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "user_id": user_id,
                    "session_id": session_id,
                    "pinned": saved,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    print(f"已{'置顶' if saved else '取消置顶'}会话 {session_id}")
    return 0


def run_session_show_standalone(
    user_id: str, session_id: str, as_json: bool = False
) -> int:
    """查看单个会话的消息与状态
    （`python cli.py session-show <user> <session> [--json]`）。

    文本输出按时间顺序列出消息（角色 + 本地时间 + 内容预览，助手消息先
    重新渲染为 Markdown），``--json`` 输出原始消息与标题 / 置顶 / 归档状态。

    Returns:
        0 查看成功；1 参数 / 读取失败（含会话不存在）
    """
    import re

    from utils.stored_message import render_stored_message

    def _fail(message: str) -> int:
        if as_json:
            print(
                json.dumps(
                    {"ok": False, "error": message},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(message)
        return 1

    if not re.fullmatch(r"[\w-]{1,64}", user_id) or user_id.startswith("-"):
        return _fail("用户 ID 只能包含中文、字母、数字、下划线和短横线")
    if not re.fullmatch(r"[\w-]{1,64}", session_id) or session_id.startswith("-"):
        return _fail("会话 ID 只能包含中文、字母、数字、下划线和短横线")

    try:
        store = _open_cli_store(user_id)
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        return _fail(f"打开长期记忆失败：{exc}")
    try:
        try:
            messages = store.get_chat_history(session_id=session_id)
        except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
            return _fail(f"读取会话失败：{exc}")
        if not messages:
            return _fail(f"会话 {session_id} 不存在或没有消息")
        title = str((store.get_session_titles() or {}).get(session_id) or "")
        pinned = session_id in [
            str(item) for item in store.get_pinned_sessions() or []
        ]
        archived = session_id in [
            str(item) for item in store.get_archived_sessions() or []
        ]
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()

    if as_json:
        from utils.json_safe import sanitize_json

        print(
            json.dumps(
                sanitize_json(
                    {
                        "ok": True,
                        "user_id": user_id,
                        "session_id": session_id,
                        "title": title,
                        "pinned": pinned,
                        "archived": archived,
                        "messages": messages,
                    }
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    label = title or "（自动标题）"
    flags = [
        part
        for part in (("置顶" if pinned else ""), ("已归档" if archived else ""))
        if part
    ]
    suffix = f"（{' / '.join(flags)}）" if flags else ""
    print(
        f"会话 {session_id}：{label}{suffix} · {len(messages)} 条消息"
    )
    for message in messages:
        role = str(message.get("role") or "assistant")
        time_text = _local_timestamp_text(str(message.get("timestamp") or ""))
        content = " ".join(render_stored_message(message).split())
        preview = content[:80] + ("…" if len(content) > 80 else "")
        print(f"  [{time_text}] {role}: {preview}")
    return 0


def _parse_sessions_flags(args: list[str]) -> tuple[bool, bool, str, bool] | None:
    """解析 sessions 的 --archived / --all / --query 关键词 / --json。

    未知参数、同时使用 --archived 与 --all、或 --query 缺少非空值
    时返回 None。
    """
    archived_only = include_all = False
    query = ""
    as_json = False
    index = 0
    while index < len(args):
        flag = args[index].lower()
        if flag == "--archived":
            archived_only = True
        elif flag == "--all":
            include_all = True
        elif flag == "--json":
            as_json = True
        elif flag == "--query" or flag.startswith("--query="):
            if flag == "--query":
                index += 1
                if index >= len(args):
                    return None
                value = args[index]
            else:
                value = args[index][len("--query="):]
            if not value.strip() or value.startswith("-"):
                return None
            query = value
        else:
            return None
        index += 1
    if archived_only and include_all:
        return None
    return archived_only, include_all, query, as_json


def _parse_archive_flags(args: list[str]) -> tuple[bool, bool] | None:
    """解析 session-archive 的 --restore / --json；未知参数返回 None。"""
    restore = False
    as_json = False
    for item in args:
        lowered = item.lower()
        if lowered == "--restore":
            restore = True
        elif lowered == "--json":
            as_json = True
        else:
            return None
    return restore, as_json


def run_session_export_standalone(
    user_id: str,
    session_id: str,
    target: str | None = None,
    to_stdout: bool = False,
    force: bool = False,
    html: bool = False,
) -> int:
    """导出单个会话为 Markdown / HTML
    （`python cli.py session-export <user> <session> [path] [--stdout] [--force] [--html]`）。

    Returns:
        0 导出成功；1 参数 / 读取 / 写入失败或会话不存在
    """
    import re
    from datetime import datetime
    from pathlib import Path

    from utils.stored_message import render_stored_message

    if not re.fullmatch(r"[\w-]{1,64}", user_id) or user_id.startswith("-"):
        print("用户 ID 只能包含中文、字母、数字、下划线和短横线")
        return 1
    if not re.fullmatch(r"[\w-]{1,64}", session_id) or session_id.startswith("-"):
        print("会话 ID 只能包含中文、字母、数字、下划线和短横线")
        return 1
    try:
        store = _open_cli_store(user_id)
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        print(f"打开长期记忆失败：{exc}")
        return 1
    try:
        try:
            messages = store.get_chat_history(session_id=session_id)
            titles = store.get_session_titles()
        except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
            print(f"读取会话失败：{exc}")
            return 1
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()

    if not messages:
        print(f"会话 {session_id} 不存在或没有消息")
        return 1

    title = str(titles.get(session_id) or "")
    if not title:
        for message in messages:
            if message.get("role") == "user":
                title = " ".join(str(message.get("content", "")).split())[:40]
                break
    # 换行会伪造导出结构：先折叠空白；尖括号的转义按出口区分——HTML 导出
    # 由 render_session_html 统一 _escape（预转义会双重转义成 &amp;lt;），
    # Markdown 导出用 sanitize_title
    title = " ".join(title.split()) or "Aligo 对话"

    if html:
        from utils.session_html import render_session_html

        text = render_session_html(user_id, session_id, title, messages)
    else:
        from utils.session_markdown import sanitize_title

        title = sanitize_title(title)
        lines = [
            f"# {title}",
            "",
            f"- 用户：{user_id}",
            f"- 会话：{session_id}",
            f"- 导出时间：{datetime.now().isoformat(timespec='seconds')}",
            "",
        ]
        for message in messages:
            is_user = message.get("role") == "user"
            role = "用户" if is_user else "Aligo"
            content = (
                str(message.get("content", ""))
                if is_user
                else render_stored_message(message)
            )
            lines.extend([f"## {role}", "", content.strip(), ""])
        text = "\n".join(lines)

    if to_stdout:
        _print_stdout(text)
        return 0

    suffix = ".html" if html else ".md"
    path = Path(target) if target else Path("exports") / f"aligo_{session_id}{suffix}"
    if path.exists() and not force:
        print(f"文件已存在：{path}（使用 --force 覆盖或指定其它路径）")
        return 1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        print(f"写入失败：{exc}")
        return 1
    print(f"已导出会话 {session_id} 到 {path}（{len(messages)} 条消息）")
    return 0


def run_sessions_export_standalone(
    user_id: str,
    target: str | None = None,
    to_stdout: bool = False,
    force: bool = False,
    html: bool = False,
) -> int:
    """导出全部会话为单个 Markdown 或 HTML
    （`python cli.py sessions-export <user> [path] [--stdout] [--force] [--html]`）。

    ``html=True`` 时渲染为独立 HTML 合集（与单会话 `session-export --html`
    同一转义口径），默认后缀改为 ``.html``。

    Returns:
        0 导出成功；1 参数 / 读取 / 写入失败或没有会话
    """
    import re
    from pathlib import Path

    if not re.fullmatch(r"[\w-]{1,64}", user_id) or user_id.startswith("-"):
        print("用户 ID 只能包含中文、字母、数字、下划线和短横线")
        return 1
    try:
        store = _open_cli_store(user_id)
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        print(f"打开长期记忆失败：{exc}")
        return 1
    try:
        try:
            messages = store.get_chat_history()
            titles = store.get_session_titles()
        except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
            print(f"读取会话失败：{exc}")
            return 1
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()

    if not messages:
        print(f"用户 {user_id}：没有可导出的会话")
        return 1

    if html:
        from utils.session_html import render_all_sessions_html

        text, count = render_all_sessions_html(user_id, messages, titles)
        suffix = ".html"
    else:
        from utils.session_markdown import render_all_sessions_markdown

        text, count = render_all_sessions_markdown(user_id, messages, titles)
        suffix = ".md"

    if to_stdout:
        _print_stdout(text)
        return 0

    safe_user = _safe_export_user(user_id)
    path = (
        Path(target)
        if target
        else Path("exports") / f"aligo_sessions_{safe_user}{suffix}"
    )
    if path.exists() and not force:
        print(f"文件已存在：{path}（使用 --force 覆盖或指定其它路径）")
        return 1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        print(f"写入失败：{exc}")
        return 1
    print(f"已导出 {count} 个会话到 {path}（{len(messages)} 条消息）")
    return 0


def run_trips_export_standalone(
    user_id: str,
    target: str | None = None,
    to_stdout: bool = False,
    force: bool = False,
    ics: bool = False,
    query: str = "",
    html: bool = False,
) -> int:
    """导出全部行程
    （`python cli.py trips-export <user_id> [path] [--stdout] [--force] [--ics|--html] [--query 关键词]`）。

    ``ics=True`` 时导出 iCalendar（与 Web 的 /api/trips/calendar 同一实现），
    ``html=True`` 时导出可打印的 HTML 台账（与 Web 的 /api/trips/export?
    format=html 同一实现），否则导出带 BOM 的 CSV；``query`` 先按面板口径
    过滤再导出。

    Returns:
        0 导出成功；1 参数 / 读取 / 写入失败
    """
    import re
    from pathlib import Path

    from utils.trip_conflicts import filter_trips_by_query
    from utils.trip_export import render_trips_csv, render_trips_html, render_trips_ics

    if not re.fullmatch(r"[\w-]{1,64}", user_id) or user_id.startswith("-"):
        print("用户 ID 只能包含中文、字母、数字、下划线和短横线")
        return 1
    try:
        store = _open_cli_store(user_id)
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        print(f"打开长期记忆失败：{exc}")
        return 1
    try:
        try:
            trips = store.get_trip_history(limit=None)
        except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
            print(f"读取行程失败：{exc}")
            return 1
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()

    needle = query.strip()
    if needle:
        trips = filter_trips_by_query(trips, needle)

    skipped = 0
    if ics:
        text = render_trips_ics(trips, user_id)
        default_name = "aligo_trips.ics"
        # 按整行统计：行程字段（SUMMARY / DESCRIPTION）里出现的同名字符串
        # 不能把导出条数放大、让"跳过 N 条"失真
        exported = sum(
            1 for line in text.splitlines() if line.strip() == "BEGIN:VEVENT"
        )
        skipped = max(0, len(trips) - exported)
    elif html:
        text = render_trips_html(trips, user_id)
        default_name = "aligo_trips.html"
        exported = len(trips)
    else:
        text = render_trips_csv(trips)
        default_name = "aligo_trips.csv"
        exported = len(trips)
    if to_stdout:
        if ics:
            # ICS 行尾已是 \r\n：文本模式在 Windows 会再次翻译；优先写字节
            buffer = getattr(sys.stdout, "buffer", None)
            if buffer is not None:
                try:
                    sys.stdout.flush()
                    buffer.write(text.encode("utf-8"))
                    buffer.flush()
                except OSError as exc:
                    # 与 _print_stdout 一致：下游提前关闭（| head / 读端未读
                    # 即退出）时静默收尾——Windows 上是 Errno 22 而不是
                    # BrokenPipeError，只认 BrokenPipeError 会漏掉并打 traceback
                    if not is_pipe_error(exc):
                        raise
                    try:
                        devnull = os.open(os.devnull, os.O_WRONLY)
                        os.dup2(devnull, sys.stdout.fileno())
                    except Exception:
                        pass
            else:
                _print_stdout(text, end="")
        else:
            _print_stdout(text, end="")
        return 0

    path = Path(target) if target else Path("exports") / default_name
    if path.exists() and not force:
        print(f"文件已存在：{path}（使用 --force 覆盖或指定其它路径）")
        return 1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if ics:
            path.write_bytes(text.encode("utf-8"))
        else:
            path.write_text(text, encoding="utf-8")
    except OSError as exc:
        print(f"写入失败：{exc}")
        return 1
    suffix = "（iCalendar）" if ics else ("（HTML）" if html else "")
    filter_note = f"，筛选「{needle}」" if needle else ""
    skipped_note = f"，跳过 {skipped} 条缺少出发日期" if skipped else ""
    print(f"已导出 {exported} 条行程到 {path}{suffix}{filter_note}{skipped_note}")
    return 0


_TRIP_ADD_FLAGS = ("--origin", "--destination", "--start", "--end", "--purpose")


def _parse_trip_add_args(args: list[str]) -> dict[str, Any] | None:
    """解析 trip-add 参数：``<user_id> --destination X --start YYYY-MM-DD [...]``。

    支持 ``--flag value`` 与 ``--flag=value`` 两种写法、``--json`` 任意位置；
    未知参数、缺值、位置参数数量不符或缺少必填项时返回 None。
    """
    values: dict[str, str] = {}
    positional: list[str] = []
    as_json = False
    index = 0
    while index < len(args):
        item = args[index]
        lowered = item.lower()
        if lowered == "--json":
            as_json = True
            index += 1
            continue
        flag = next(
            (
                name
                for name in _TRIP_ADD_FLAGS
                if lowered == name or lowered.startswith(name + "=")
            ),
            None,
        )
        if flag is None:
            if item.startswith("-"):
                return None
            positional.append(item)
            index += 1
            continue
        if "=" in item:
            value = item.split("=", 1)[1]
        else:
            index += 1
            if index >= len(args):
                return None
            value = args[index]
        if not value.strip() or value.startswith("-"):
            return None
        values[flag[2:]] = value.strip()
        index += 1
    if (
        len(positional) != 1
        or not values.get("destination")
        or not values.get("start")
    ):
        return None
    return {"user_id": positional[0], "values": values, "as_json": as_json}


def run_trip_add_standalone(
    user_id: str,
    origin: str,
    destination: str,
    start_date: str,
    end_date: str | None = None,
    purpose: str = "出差",
    as_json: bool = False,
) -> int:
    """保存一条行程（`python cli.py trip-add ... [--json]`）。

    与 Web 面板 / 对话写入同一存储路径：同路线同日期视为同一趟行程（更新
    既有记录而不是追加），并提示与其它行程的日期重叠。``--json`` 输出结构化
    结果（trip_id / created / conflicts）。

    Returns:
        0 保存成功；1 参数 / 读取 / 写入失败
    """
    import re

    from utils.trip_conflicts import find_saved_trip, trip_conflicts

    def _fail(message: str) -> int:
        if as_json:
            print(
                json.dumps(
                    {"ok": False, "error": message},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(message)
        return 1

    if not re.fullmatch(r"[\w-]{1,64}", user_id) or user_id.startswith("-"):
        return _fail("用户 ID 只能包含中文、字母、数字、下划线和短横线")
    start_dt = _parse_strict_date(start_date)
    if start_dt is None:
        return _fail(f"出发日期格式应为 YYYY-MM-DD：{start_date}")
    end_dt = None
    if end_date:
        end_dt = _parse_strict_date(end_date)
        if end_dt is None:
            return _fail(f"返程日期格式应为 YYYY-MM-DD：{end_date}")
        if end_dt < start_dt:
            return _fail("结束日期不能早于出发日期")

    try:
        store = _open_cli_store(user_id)
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        return _fail(f"打开长期记忆失败：{exc}")
    try:
        try:
            before = len(store.get_trip_history(limit=None))
        except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
            return _fail(f"读取行程失败：{exc}")
        record = {
            "origin": origin,
            "destination": destination,
            "start_date": start_date,
            "end_date": end_date,
            "purpose": purpose or "出差",
        }
        try:
            trip_id = store.save_trip_history(record)
        except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
            return _fail(f"保存行程失败：{exc}")
        try:
            trips = store.get_trip_history(limit=None)
        except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
            return _fail(f"读取行程失败：{exc}")
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()

    created = len(trips) > before
    # 按「id + 关键字段」定位本次保存的那条并按身份排除：历史里存在重复
    # trip_id 时，按 id 整条排除会连带隐藏其它行程的真实重叠
    saved_record = find_saved_trip(trips, trip_id, record)
    conflicts = trip_conflicts(
        trips,
        start_dt,
        end_dt or start_dt,
        exclude_id=trip_id,
        exclude_trip=saved_record,
    )
    if as_json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "user_id": user_id,
                    "trip_id": trip_id,
                    "saved": True,
                    "created": created,
                    "conflicts": [
                        {
                            "trip_id": str(trip.get("trip_id") or ""),
                            "origin": str(trip.get("origin") or ""),
                            "destination": str(trip.get("destination") or ""),
                            "start_date": str(trip.get("start_date") or ""),
                            "end_date": str(trip.get("end_date") or ""),
                        }
                        for trip in conflicts
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    dates = (
        start_date
        if not end_date or end_date == start_date
        else f"{start_date} ~ {end_date}"
    )
    print(
        f"{'已添加' if created else '已更新'}行程 #{trip_id}"
        f"（{origin or '—'} → {destination}，{dates}）"
    )
    if conflicts:
        ids = "、".join(f"#{str(item.get('trip_id') or '')}" for item in conflicts)
        print(f"注意：与 {len(conflicts)} 条行程日期重叠：{ids}")
    return 0


def run_trip_delete_standalone(
    user_id: str, trip_id: str, as_json: bool = False
) -> int:
    """删除一条行程（`python cli.py trip-delete <user_id> <trip_id> [--json]`）。

    与 Web 面板同一保护：同一 trip_id 对应多条行程（历史数据异常）时拒绝
    自动删除，提示先运行 trips-dedupe 清理。

    Returns:
        0 删除成功；1 参数 / 读取 / 删除失败（含行程不存在）
    """
    import re

    def _fail(message: str) -> int:
        if as_json:
            print(
                json.dumps(
                    {"ok": False, "error": message},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(message)
        return 1

    if not re.fullmatch(r"[\w-]{1,64}", user_id) or user_id.startswith("-"):
        return _fail("用户 ID 只能包含中文、字母、数字、下划线和短横线")
    if not re.fullmatch(r"[\w-]{1,64}", trip_id) or trip_id.startswith("-"):
        return _fail("行程 ID 只能包含中文、字母、数字、下划线和短横线")

    try:
        store = _open_cli_store(user_id)
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        return _fail(f"打开长期记忆失败：{exc}")
    try:
        try:
            trips = store.get_trip_history(limit=None)
        except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
            return _fail(f"读取行程失败：{exc}")
        matches = [trip for trip in trips if str(trip.get("trip_id")) == trip_id]
        if len(matches) > 1:
            return _fail(
                "该 trip_id 对应多条行程，请先运行 python cli.py trips-dedupe 清理"
            )
        if not matches:
            return _fail(f"行程 {trip_id} 不存在")
        try:
            deleted = store.delete_trip(trip_id)
        except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
            return _fail(f"删除行程失败：{exc}")
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()
    if not deleted:
        return _fail(f"行程 {trip_id} 不存在")

    trip = matches[0]
    origin = str(trip.get("origin") or "") or "—"
    destination = str(trip.get("destination") or "") or "—"
    start_date = str(trip.get("start_date") or "")
    end_date = str(trip.get("end_date") or "")
    dates = (
        start_date
        if not end_date or end_date == start_date
        else f"{start_date} ~ {end_date}"
    ) or "日期未记录"
    if as_json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "user_id": user_id,
                    "trip_id": trip_id,
                    "deleted": True,
                    "origin": "" if origin == "—" else origin,
                    "destination": "" if destination == "—" else destination,
                    "start_date": start_date,
                    "end_date": end_date,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    print(f"已删除行程 #{trip_id}（{origin} → {destination}，{dates}）")
    return 0


def _parse_trip_update_args(args: list[str]) -> dict[str, Any] | None:
    """解析 trip-update 参数：``<user_id> <trip_id> [--origin X ...] [--json]``。

    至少提供一个可更新字段；未知参数、缺值或位置参数数量不符返回 None。
    """
    values: dict[str, str] = {}
    positional: list[str] = []
    as_json = False
    index = 0
    while index < len(args):
        item = args[index]
        lowered = item.lower()
        if lowered == "--json":
            as_json = True
            index += 1
            continue
        flag = next(
            (
                name
                for name in _TRIP_ADD_FLAGS
                if lowered == name or lowered.startswith(name + "=")
            ),
            None,
        )
        if flag is None:
            if item.startswith("-"):
                return None
            positional.append(item)
            index += 1
            continue
        if "=" in item:
            value = item.split("=", 1)[1]
        else:
            index += 1
            if index >= len(args):
                return None
            value = args[index]
        if not value.strip() or value.startswith("-"):
            return None
        values[flag[2:]] = value.strip()
        index += 1
    if len(positional) != 2 or not values:
        return None
    return {
        "user_id": positional[0],
        "trip_id": positional[1],
        "values": values,
        "as_json": as_json,
    }


def run_trip_update_standalone(
    user_id: str,
    trip_id: str,
    fields: dict[str, str],
    as_json: bool = False,
) -> int:
    """按字段更新一条行程（`python cli.py trip-update ... [--json]`）。

    与 Web 面板同一语义：只修改提供的字段；日期按合并后的行程校验；更新后
    提示与其它行程的日期重叠；同一 trip_id 对应多条行程时拒绝操作。

    Returns:
        0 更新成功；1 参数 / 读取 / 写入失败（含行程不存在）
    """
    import re

    from utils.trip_conflicts import trip_conflicts

    def _fail(message: str) -> int:
        if as_json:
            print(
                json.dumps(
                    {"ok": False, "error": message},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(message)
        return 1

    if not re.fullmatch(r"[\w-]{1,64}", user_id) or user_id.startswith("-"):
        return _fail("用户 ID 只能包含中文、字母、数字、下划线和短横线")
    if not re.fullmatch(r"[\w-]{1,64}", trip_id) or trip_id.startswith("-"):
        return _fail("行程 ID 只能包含中文、字母、数字、下划线和短横线")
    mapped = {
        {"start": "start_date", "end": "end_date"}.get(key, key): value
        for key, value in fields.items()
    }
    if not mapped:
        return _fail("没有需要更新的字段")
    if "destination" in mapped and not mapped["destination"].strip():
        return _fail("目的地不能为空")

    try:
        store = _open_cli_store(user_id)
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        return _fail(f"打开长期记忆失败：{exc}")
    try:
        try:
            trips = store.get_trip_history(limit=None)
        except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
            return _fail(f"读取行程失败：{exc}")
        matches = [trip for trip in trips if str(trip.get("trip_id")) == trip_id]
        if len(matches) > 1:
            return _fail(
                "该 trip_id 对应多条行程，请先运行 python cli.py trips-dedupe 清理"
            )
        if not matches:
            return _fail(f"行程 {trip_id} 不存在")
        merged = {**matches[0], **mapped}

        # 只严格校验本次提交的日期字段：合并结果里的历史脏日期不能阻断
        # 仅修改目的 / 出发地等其它字段的请求（与 Web PUT 同口径）
        start_dt = end_dt = None
        for key, label in (("start_date", "出发日期"), ("end_date", "返程日期")):
            raw = str(merged.get(key) or "")
            if not raw:
                continue
            parsed = _parse_strict_date(raw)
            if parsed is None:
                if key in mapped:
                    return _fail(f"{label}格式应为 YYYY-MM-DD：{raw}")
                continue
            if key == "start_date":
                start_dt = parsed
            else:
                end_dt = parsed
        if start_dt is None and "end_date" in mapped:
            return _fail("填写返程日期前请先设置出发日期")
        if start_dt is not None and end_dt is not None and end_dt < start_dt:
            return _fail("结束日期不能早于出发日期")

        try:
            updated = store.update_trip(trip_id, mapped)
        except ValueError as exc:
            return _fail(str(exc))
        except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
            return _fail(f"更新行程失败：{exc}")
        if not updated:
            return _fail(f"行程 {trip_id} 不存在")
        try:
            trips = store.get_trip_history(limit=None)
        except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
            return _fail(f"读取行程失败：{exc}")
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()

    saved = next(
        (trip for trip in trips if str(trip.get("trip_id")) == trip_id),
        {"trip_id": trip_id, **merged},
    )
    conflicts = (
        trip_conflicts(trips, start_dt, end_dt or start_dt, exclude_id=trip_id)
        if start_dt is not None
        else []
    )
    origin = str(saved.get("origin") or "")
    destination = str(saved.get("destination") or "")
    start_text = str(saved.get("start_date") or "")
    end_text = str(saved.get("end_date") or "")
    dates = (
        start_text
        if not end_text or end_text == start_text
        else f"{start_text} ~ {end_text}"
    ) or "日期未记录"
    if as_json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "user_id": user_id,
                    "trip_id": trip_id,
                    "updated": True,
                    "origin": origin,
                    "destination": destination,
                    "start_date": start_text,
                    "end_date": end_text,
                    "conflicts": [
                        {
                            "trip_id": str(trip.get("trip_id") or ""),
                            "origin": str(trip.get("origin") or ""),
                            "destination": str(trip.get("destination") or ""),
                            "start_date": str(trip.get("start_date") or ""),
                            "end_date": str(trip.get("end_date") or ""),
                        }
                        for trip in conflicts
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    print(
        f"已更新行程 #{trip_id}"
        f"（{origin or '—'} → {destination or '—'}，{dates}）"
    )
    if conflicts:
        ids = "、".join(f"#{str(item.get('trip_id') or '')}" for item in conflicts)
        print(f"注意：与 {len(conflicts)} 条行程日期重叠：{ids}")
    return 0


# 行程状态过滤：英文键与面板标签都接受（active/upcoming/past ↔ 进行中/…）
_TRIP_STATUS_FILTERS = {
    "active": "进行中",
    "upcoming": "即将出行",
    "past": "已结束",
    "进行中": "进行中",
    "即将出行": "即将出行",
    "已结束": "已结束",
}


def _parse_trips_list_args(
    args: list[str],
) -> tuple[str, str, bool, str] | None:
    """解析 trips 的 ``<user_id> [--query 关键词] [--status S] [--json]``。

    ``--query`` / ``--status`` 支持 ``--x 值`` 与 ``--x=值``，``--json``
    不限位置；未知参数、缺值、非法用户 ID、非法状态或位置参数过多返回 None。
    """
    import re

    user_id: str | None = None
    query = ""
    status = ""
    as_json = False
    index = 0
    while index < len(args):
        item = args[index]
        lowered = item.lower()
        if lowered == "--json":
            as_json = True
            index += 1
            continue
        if lowered == "--status" or lowered.startswith("--status="):
            if "=" in item:
                value = item.split("=", 1)[1]
            else:
                index += 1
                if index >= len(args):
                    return None
                value = args[index]
            label = _TRIP_STATUS_FILTERS.get(value.strip())
            if not label:
                return None
            status = value.strip()
            index += 1
            continue
        if lowered == "--query" or lowered.startswith("--query="):
            if "=" in item:
                value = item.split("=", 1)[1]
            else:
                index += 1
                if index >= len(args):
                    return None
                value = args[index]
            if not value.strip() or value.startswith("-"):
                return None
            query = value.strip()
            index += 1
            continue
        if item.startswith("-") or user_id is not None:
            return None
        user_id = item
        index += 1
    if (
        user_id is None
        or not re.fullmatch(r"[\w-]{1,64}", user_id)
        or user_id.startswith("-")
    ):
        return None
    return user_id, query, as_json, status


def run_trips_import_standalone(
    user_id: str,
    csv_path: str,
    as_json: bool = False,
    as_ics: bool = False,
    dry_run: bool = False,
) -> int:
    """从 CSV / iCalendar 导入行程
    （`python cli.py trips-import <user_id> <path> [--ics] [--dry-run] [--json]`）。

    ``dry_run=True`` 只预览将新增 / 更新 / 跳过的数量，不写库。

    解析 ``trips-export`` 导出的 CSV（UTF-8 BOM，中英文字段名均可）或
    ``trips-export --ics`` 导出的事件（``--ics`` 或内容以 BEGIN:VCALENDAR
    开头时自动识别），逐条校验；重复行程沿用存储层去重语义（同路线 +
    同起止日期更新目的）。非法行 / 事件收集后在输出里列出，不中断其余项。

    Returns:
        0 至少导入一行（含部分项被跳过）；1 参数 / 读取 / 写入失败或没有可导入行
    """
    import io
    import re

    from utils.trip_import import (
        parse_trips_csv,
        parse_trips_ics,
        trip_identity,
    )

    def _fail(message: str) -> int:
        if as_json:
            print(
                json.dumps(
                    {"ok": False, "error": message},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(message)
        return 1

    if not re.fullmatch(r"[\w-]{1,64}", user_id) or user_id.startswith("-"):
        return _fail("用户 ID 只能包含中文、字母、数字、下划线和短横线")
    if not csv_path or csv_path.startswith("-"):
        return _fail("请提供 CSV / ICS 文件路径")
    try:
        text = io.open(csv_path, "r", encoding="utf-8-sig").read()
    except FileNotFoundError:
        return _fail(f"文件不存在：{csv_path}")
    except UnicodeDecodeError as exc:
        return _fail(f"读取导入文件失败：{exc}（文件需为 UTF-8 编码）")
    except OSError as exc:
        return _fail(f"读取导入文件失败：{exc}")

    is_ics = as_ics or text.lstrip("﻿ \t\r\n").upper().startswith("BEGIN:VCALENDAR")
    if is_ics:
        rows, errors = parse_trips_ics(text)
    else:
        rows, errors = parse_trips_csv(text)
    if not rows:
        if is_ics:
            detail = "；".join(
                reason if index == 0 else f"事件{index}：{reason}"
                for index, reason in errors[:5]
            )
        else:
            detail = "；".join(f"第{line}行：{reason}" for line, reason in errors[:5])
        return _fail(f"没有可导入的行程{f'（{detail}）' if detail else ''}")

    try:
        store = _open_cli_store(user_id)
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        return _fail(f"打开长期记忆失败：{exc}")
    try:
        if dry_run:
            # 预览：按存储层同一去重键模拟新增 / 更新，不写库
            try:
                pending_keys = {
                    trip_identity(trip)
                    for trip in store.get_trip_history(limit=None)
                }
            except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
                return _fail(f"读取行程失败：{exc}")
            created = 0
            updated = 0
            for row in rows:
                key = trip_identity(row)
                if key in pending_keys:
                    updated += 1
                else:
                    created += 1
                    pending_keys.add(key)
        else:
            try:
                created, updated = store.save_trip_histories(rows)
            except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
                return _fail(f"写入行程失败：{exc}")
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()
    if as_json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "user_id": user_id,
                    "path": csv_path,
                    "format": "ics" if is_ics else "csv",
                    "dry_run": dry_run,
                    "rows": len(rows),
                    "created": created,
                    "updated": updated,
                    "skipped": [
                        {"line": line, "reason": reason}
                        for line, reason in errors
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        unit = "个事件" if is_ics else "行"
        skipped_note = f"，跳过 {len(errors)} 项" if errors else ""
        head = (
            f"预览（未写入）：将从 {csv_path} 导入 {len(rows)} {unit}"
            if dry_run
            else f"已从 {csv_path} 导入 {len(rows)} {unit}"
        )
        print(f"{head}：新增 {created} 条，更新 {updated} 条{skipped_note}")
        for line, reason in errors:
            label = f"事件{line}" if is_ics else f"第{line}行"
            print(f"  跳过{label}：{reason}")
    return 0


def run_trips_list_standalone(
    user_id: str,
    as_json: bool = False,
    query: str = "",
    status_filter: str = "",
) -> int:
    """列出用户行程
    （`python cli.py trips <user_id> [--query 关键词] [--status S] [--json]`）。

    ``query`` 按路线 / 起止日期 / 目的做不区分大小写的过滤（与面板关键词
    筛选一致，冲突提示仍按完整历史计算）；``status_filter`` 接受
    active / upcoming / past（或面板标签 进行中 / 即将出行 / 已结束），
    与面板状态徽标同一口径，无日期的行程在按状态过滤时不出现；
    ``as_json`` 输出原始行程（含冲突列表与状态标签）。

    Returns:
        0 列出成功（含无行程 / 无匹配）；1 参数 / 读取失败
    """
    import re

    from utils.trip_conflicts import (
        filter_trips_by_query,
        trip_state_label,
        trips_with_conflicts,
    )

    if not re.fullmatch(r"[\w-]{1,64}", user_id) or user_id.startswith("-"):
        print("用户 ID 只能包含中文、字母、数字、下划线和短横线")
        return 1
    try:
        store = _open_cli_store(user_id)
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        print(f"打开长期记忆失败：{exc}")
        return 1
    try:
        try:
            trips = store.get_trip_history(limit=None)
        except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
            print(f"读取行程失败：{exc}")
            return 1
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()

    if not trips:
        if as_json:
            print(
                json.dumps(
                    {"user_id": user_id, "count": 0, "trips": []},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        print(f"用户 {user_id}：还没有行程记录")
        return 0

    # 与 Web 记忆面板一致：新→旧展示，并标注状态与日期重叠
    from utils.json_safe import sanitize_json

    views = trips_with_conflicts(trips)
    needle = query.strip()
    status_label = _TRIP_STATUS_FILTERS.get(status_filter.strip(), "")
    if status_label:
        views = [
            trip for trip in views if trip_state_label(trip) == status_label
        ]
    if needle:
        views = filter_trips_by_query(views, needle)
        if not views:
            if as_json:
                print(
                    json.dumps(
                        {
                            "user_id": user_id,
                            "query": query.strip(),
                            "status": status_label,
                            "count": 0,
                            "trips": [],
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
            notes = []
            if status_label:
                notes.append(f"状态为「{status_label}」")
            if needle:
                notes.append(f"匹配「{needle}」")
            print(f"用户 {user_id}：没有{'且'.join(notes)}的行程")
            return 0
    if as_json:
        print(
            json.dumps(
                sanitize_json(
                    {
                        "user_id": user_id,
                        "query": query.strip(),
                        "status": status_label,
                        "count": len(views),
                        "trips": [
                            {"state": trip_state_label(trip), **trip}
                            for trip in views
                        ],
                    }
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if needle or status_label:
        notes = []
        if status_label:
            notes.append(f"状态为「{status_label}」")
        if needle:
            notes.append(f"匹配「{needle}」")
        header = (
            f"用户 {user_id}：共 {len(views)} 条{'且'.join(notes)}的行程（新→旧）"
        )
    else:
        header = f"用户 {user_id}：共 {len(views)} 条行程（新→旧）"
    print(header)
    for trip in reversed(views):
        start = trip.get("start_date") or "日期未记录"
        end = trip.get("end_date") or ""
        dates = f"{start} ~ {end}" if end and end != start else str(start)
        route = f"{trip.get('origin') or '—'} → {trip.get('destination') or '—'}"
        marks = []
        state = trip_state_label(trip)
        if state:
            marks.append(f"[{state}]")
        conflicts = trip.get("conflicts") or []
        if conflicts:
            routes = "、".join(
                f"{item.get('origin') or '—'} → {item.get('destination') or '—'}"
                for item in conflicts
            )
            marks.append(f"⚠ 与 {routes} 日期重叠")
        purpose = trip.get("purpose") or "出行"
        line = f"  {dates}  {route}  {' '.join(marks)}".rstrip()
        print(f"{line}  {purpose}")
    return 0


def run_preferences_list_standalone(user_id: str, as_json: bool = False) -> int:
    """列出用户偏好（`python cli.py preferences <user_id> [--json]`）。

    ``as_json`` 输出机器可读的原始偏好值（列表 / 布尔值保持原类型）。

    Returns:
        0 列出成功（含无偏好）；1 参数 / 读取失败
    """
    import re

    from utils.result_formatter import preference_label

    if not re.fullmatch(r"[\w-]{1,64}", user_id) or user_id.startswith("-"):
        print("用户 ID 只能包含中文、字母、数字、下划线和短横线")
        return 1
    try:
        store = _open_cli_store(user_id)
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        print(f"打开长期记忆失败：{exc}")
        return 1
    try:
        try:
            preferences = store.get_preference()
        except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
            print(f"读取偏好失败：{exc}")
            return 1
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()

    # 与面板 / has_preference_value 同口径：null / 空白 / 全空列表不列出，
    # 列表内的 null 与空白项逐项剔除（此前渲染成「东航、None」）
    visible = {
        key: value
        for key, value in preferences.items()
        if has_preference_value(value)
    }

    def _list_items(value: list) -> list[str]:
        return [
            str(item)
            for item in value
            if not (
                item is None or (isinstance(item, str) and not item.strip())
            )
        ]

    if as_json:
        from utils.json_safe import sanitize_json

        print(
            json.dumps(
                sanitize_json(
                    {
                        "user_id": user_id,
                        "count": len(visible),
                        "preferences": {
                            key: visible[key] for key in sorted(visible)
                        },
                    }
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if not visible:
        print(f"用户 {user_id}：还没有偏好记录")
        return 0

    print(f"用户 {user_id}：{len(visible)} 条偏好")
    for pref_type in sorted(visible, key=preference_label):
        value = visible[pref_type]
        if isinstance(value, list):
            text = "、".join(_list_items(value)) or "—"
        elif value is None or value == "":
            text = "—"
        elif isinstance(value, bool):
            text = "是" if value else "否"
        else:
            text = str(value)
        print(f"  {preference_label(pref_type)}：{text}")
    return 0


def run_preference_set_standalone(
    user_id: str, pref_type: str, value: str, as_json: bool = False
) -> int:
    """新增 / 覆盖一条偏好（`python cli.py pref-set <user_id> <type> <value> [--json]`）。

    与 Web 面板的 PUT ``/api/preferences/{type}`` 同一存储语义：值按原样
    保存；列表类偏好可重复设置（对话侧按合并语义追加去重）。

    Returns:
        0 保存成功；1 参数 / 读写失败
    """
    import re

    from utils.result_formatter import preference_label

    def _fail(message: str) -> int:
        if as_json:
            print(
                json.dumps(
                    {"ok": False, "error": message},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(message)
        return 1

    if not re.fullmatch(r"[\w-]{1,64}", user_id) or user_id.startswith("-"):
        return _fail("用户 ID 只能包含中文、字母、数字、下划线和短横线")
    if not re.fullmatch(r"[\w-]{1,64}", pref_type) or pref_type.startswith("-"):
        return _fail("偏好类型只能包含中文、字母、数字、下划线和短横线")
    text_value = value.strip()
    if not text_value:
        return _fail("偏好值不能为空")

    try:
        store = _open_cli_store(user_id)
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        return _fail(f"打开长期记忆失败：{exc}")
    try:
        try:
            store.save_preference(pref_type, text_value)
            saved = store.get_preference(pref_type)
        except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
            return _fail(f"保存偏好失败：{exc}")
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()

    if as_json:
        from utils.json_safe import sanitize_json

        print(
            json.dumps(
                sanitize_json(
                    {
                        "ok": True,
                        "user_id": user_id,
                        "type": pref_type,
                        "value": saved,
                    }
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if isinstance(saved, list):
        display = "、".join(str(item) for item in saved) or "—"
    elif isinstance(saved, bool):
        display = "是" if saved else "否"
    elif saved is None or saved == "":
        display = "—"
    else:
        display = str(saved)
    print(f"已设置偏好 {preference_label(pref_type)}：{display}")
    return 0


def run_preference_delete_standalone(
    user_id: str, pref_type: str, as_json: bool = False
) -> int:
    """删除一条偏好（`python cli.py pref-delete <user_id> <type> [--json]`）。

    Returns:
        0 删除成功；1 参数 / 读写失败（含偏好未设置）
    """
    import re

    from utils.result_formatter import preference_label

    def _fail(message: str) -> int:
        if as_json:
            print(
                json.dumps(
                    {"ok": False, "error": message},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(message)
        return 1

    if not re.fullmatch(r"[\w-]{1,64}", user_id) or user_id.startswith("-"):
        return _fail("用户 ID 只能包含中文、字母、数字、下划线和短横线")
    if not re.fullmatch(r"[\w-]{1,64}", pref_type) or pref_type.startswith("-"):
        return _fail("偏好类型只能包含中文、字母、数字、下划线和短横线")

    try:
        store = _open_cli_store(user_id)
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        return _fail(f"打开长期记忆失败：{exc}")
    try:
        try:
            removed = store.delete_preference(pref_type)
        except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
            return _fail(f"删除偏好失败：{exc}")
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()
    if not removed:
        return _fail(f"偏好 {pref_type} 未设置或已删除")
    if as_json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "user_id": user_id,
                    "type": pref_type,
                    "deleted": True,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    print(f"已删除偏好 {preference_label(pref_type)}")
    return 0


def _parse_kb_export_args(args: list[str]) -> tuple[str | None, bool, bool] | None:
    """解析 kb-export 的 [path] [--stdout] [--force]；冲突 / 未知参数返回 None。"""
    target: str | None = None
    force = False
    to_stdout = False
    for item in args:
        lowered = item.lower()
        if lowered == "--force":
            force = True
        elif lowered == "--stdout":
            to_stdout = True
        elif item.startswith("-"):
            return None
        elif target is None:
            target = item
        else:
            return None
    if to_stdout and target is not None:
        return None
    return target, force, to_stdout


def _parse_session_export_args(
    args: list[str],
) -> tuple[str | None, bool, bool, bool] | None:
    """解析 session-export 的 [path] [--stdout] [--force] [--html]；非法返回 None。"""
    to_stdout = False
    force = False
    html = False
    paths: list[str] = []
    for item in args:
        lowered = item.lower()
        if lowered == "--stdout":
            to_stdout = True
        elif lowered == "--force":
            force = True
        elif lowered == "--html":
            html = True
        elif item.startswith("-"):
            return None
        else:
            paths.append(item)
    if len(paths) > 1 or (to_stdout and paths):
        return None
    return (paths[0] if paths else None), to_stdout, force, html


def _parse_export_args(
    args: list[str], allow_ics: bool = False
) -> tuple[str | None, bool, bool, bool, str] | None:
    """解析导出命令的 [path] [--stdout] [--force] [--ics] [--query 关键词]。

    ``--ics`` / ``--query`` 只在 ``allow_ics=True``（trips-export）时接受；
    冲突 / 未知参数返回 None。
    """
    to_stdout = False
    force = False
    ics = False
    query = ""
    paths: list[str] = []
    index = 0
    while index < len(args):
        item = args[index]
        lowered = item.lower()
        if lowered == "--stdout":
            to_stdout = True
        elif lowered == "--force":
            force = True
        elif lowered == "--ics" and allow_ics:
            ics = True
        elif allow_ics and (
            lowered == "--query" or lowered.startswith("--query=")
        ):
            if "=" in item:
                value = item.split("=", 1)[1]
            else:
                index += 1
                if index >= len(args):
                    return None
                value = args[index]
            if not value.strip() or value.startswith("-"):
                return None
            query = value.strip()
        elif item.startswith("-"):
            return None
        else:
            paths.append(item)
        index += 1
    if len(paths) > 1 or (to_stdout and paths):
        return None
    return (paths[0] if paths else None), to_stdout, force, ics, query


_COMMANDS_HINT = (
    "可用命令：health / doctor / skills / weather / stats / kb / eval / kb-export / "
    "kb-import / kb-list / kb-search / kb-show / kb-add / kb-update / "
    "kb-categories / kb-delete / kb-duplicates / kb-dedupe / "
    "preferences / pref-set / pref-delete / "
    "trips / trip-add / trip-update / trip-delete / trips-dedupe / sessions / session-archive / session-delete / session-title / session-pin / session-show / session-export / "
    "sessions-export / trips-export / trips-import / user-export / user-import"
)


def _safe_export_user(user_id: str) -> str:
    """导出文件名里的用户 ID：清洗非法字符，撞名风险时追加短哈希。"""
    import hashlib
    import re

    safe = re.sub(r"[^A-Za-z0-9_-]", "_", user_id)[:24] or "user"
    if safe != user_id:
        # 中文 / 超长用户 ID 清洗后可能撞名，追加短哈希避免 --force 覆盖他人导出
        digest = hashlib.sha1(user_id.encode("utf-8")).hexdigest()[:6]
        safe = f"{safe}_{digest}"
    return safe


def _print_stdout(text: str, end: str = "\n") -> None:
    """输出导出内容；下游提前关闭（如 `| head`）时静默收尾，不抛 traceback。"""
    try:
        print(text, end=end)
    except OSError as exc:
        # 仅吞掉管道类错误，真实的写失败（磁盘满等）照常抛出
        if not is_pipe_error(exc):
            raise
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except Exception:
            pass


def main():
    """主函数"""
    # 一次性命令不读 stdin：跳过编码探测，避免已连接但无数据的管道卡住启动
    configure_stdio(probe_stdin=len(sys.argv) == 1)
    if len(sys.argv) > 1 and not isinstance(sys.stdout, PipeSafeTextIO):
        # 一次性命令同样可能被下游（| head）提前关闭：列表 / 导出 / JSON
        # 输出全部走管道安全包装，不再打 traceback（与交互模式同一口径）
        sys.stdout = PipeSafeTextIO(sys.stdout)
    if len(sys.argv) > 1:
        arg = sys.argv[1].strip().lower()
        if arg in {"--version", "-v"}:
            print(f"Aligo 差旅助手 {APP_VERSION}")
            sys.exit(0)
        if arg == "health":
            health_json = _parse_json_flag(sys.argv[2:], "python cli.py health [--json]")
            if health_json is None:
                sys.exit(2)
            sys.exit(run_health_check_standalone(as_json=health_json))
        if arg == "doctor":
            doctor_args = _parse_doctor_args(sys.argv[2:])
            if doctor_args is None:
                print("用法：python cli.py doctor [--json] [--offline]")
                sys.exit(2)
            doctor_json, doctor_network = doctor_args
            sys.exit(
                run_doctor_standalone(
                    as_json=doctor_json, include_network=doctor_network
                )
            )
        if arg == "skills":
            skills_json = _parse_json_flag(sys.argv[2:], "python cli.py skills [--json]")
            if skills_json is None:
                sys.exit(2)
            sys.exit(run_skills_standalone(as_json=skills_json))
        if arg == "weather":
            weather_args = sys.argv[2:]
            weather_json = "--json" in weather_args
            cities = [item for item in weather_args if item != "--json"]
            if len(cities) != 1 or cities[0].startswith("-"):
                print("用法：python cli.py weather <城市> [--json]")
                sys.exit(2)
            sys.exit(run_weather_standalone(cities[0], as_json=weather_json))
        if arg == "kb":
            kb_json = _parse_json_flag(sys.argv[2:], "python cli.py kb [--json]")
            if kb_json is None:
                sys.exit(2)
            sys.exit(run_knowledge_status_standalone(as_json=kb_json))
        if arg == "eval":
            parsed_eval = _parse_eval_args(sys.argv[2:])
            if parsed_eval is None:
                print("用法：python cli.py eval [--json] [--min-hit5 0.8]")
                sys.exit(2)
            eval_json, eval_min_hit5 = parsed_eval
            sys.exit(
                run_rag_eval_standalone(
                    as_json=eval_json, min_hit5=eval_min_hit5
                )
            )
        if arg == "kb-export":
            parsed_kb_export = _parse_kb_export_args(sys.argv[2:])
            if parsed_kb_export is None:
                print(
                    "用法：python cli.py kb-export [path] [--stdout] [--force]"
                )
                sys.exit(2)
            kb_target, kb_force, kb_stdout = parsed_kb_export
            sys.exit(
                run_knowledge_export_standalone(
                    kb_target, force=kb_force, to_stdout=kb_stdout
                )
            )
        if arg == "kb-import":
            target = None
            dry_run = False
            as_json = False
            valid = True
            for item in sys.argv[2:]:
                lowered = item.lower()
                if lowered == "--dry-run":
                    dry_run = True
                elif lowered == "--json":
                    as_json = True
                elif item.startswith("-") or target is not None:
                    valid = False
                    break
                else:
                    target = item
            if not valid or target is None:
                print("用法：python cli.py kb-import <file.json> [--dry-run] [--json]")
                sys.exit(2)
            sys.exit(
                run_knowledge_import_standalone(
                    target, dry_run=dry_run, as_json=as_json
                )
            )
        if arg == "kb-list":
            positional: list[str] = []
            as_json = False
            valid = True
            for item in sys.argv[2:]:
                if item.lower() == "--json":
                    as_json = True
                elif item.startswith("-") or len(positional) >= 1:
                    valid = False
                    break
                else:
                    positional.append(item)
            if not valid:
                print("用法：python cli.py kb-list [关键词] [--json]")
                sys.exit(2)
            sys.exit(
                run_knowledge_list_standalone(
                    positional[0] if positional else "", as_json=as_json
                )
            )
        if arg == "kb-search":
            parsed_search = _parse_kb_search_args(sys.argv[2:])
            if parsed_search is None:
                print(
                    "用法：python cli.py kb-search <关键词> [--top-k N] [--json]"
                )
                sys.exit(2)
            search_query, search_top_k, search_json = parsed_search
            sys.exit(
                run_knowledge_search_standalone(
                    search_query, top_k=search_top_k, as_json=search_json
                )
            )
        if arg == "kb-show":
            show_args = sys.argv[2:]
            show_json = "--json" in [item.lower() for item in show_args]
            ids = [
                item
                for item in show_args
                if item.lower() != "--json"
            ]
            doc_id = _parse_cli_int(ids[0]) if len(ids) == 1 else None
            if doc_id is None or any(item.startswith("-") for item in ids):
                print("用法：python cli.py kb-show <id> [--json]")
                sys.exit(2)
            sys.exit(run_knowledge_show_standalone(doc_id, as_json=show_json))
        if arg in ("kb-add", "kb-update"):
            usage = (
                "用法：python cli.py kb-add <file> [--category X] [--title Y]"
                "；kb-update <id> <file> [--category X] [--title Y]"
            )
            parsed_write = _parse_kb_write_args(sys.argv[2:])
            if parsed_write is None:
                print(usage)
                sys.exit(2)
            write_pos, write_meta = parsed_write
            if arg == "kb-add" and len(write_pos) == 1:
                sys.exit(
                    run_knowledge_add_standalone(write_pos[0], write_meta or None)
                )
            if arg == "kb-update" and len(write_pos) == 2:
                write_id = _parse_cli_int(write_pos[0])
                if write_id is not None:
                    sys.exit(
                        run_knowledge_update_standalone(
                            write_id, write_pos[1], write_meta or None
                        )
                    )
            print(usage)
            sys.exit(2)
        if arg == "kb-categories":
            kb_categories_json = _parse_json_flag(
                sys.argv[2:], "python cli.py kb-categories [--json]"
            )
            if kb_categories_json is None:
                sys.exit(2)
            sys.exit(run_knowledge_categories_standalone(as_json=kb_categories_json))
        if arg == "kb-delete":
            args = sys.argv[2:]
            doc_id = _parse_cli_int(args[0]) if args else None
            if (
                doc_id is None
                or len(args) > 2
                or any(extra != "--group" for extra in args[1:])
            ):
                print("用法：python cli.py kb-delete <id> [--group]")
                sys.exit(2)
            sys.exit(
                run_knowledge_delete_standalone(
                    doc_id, whole_group="--group" in args[1:]
                )
            )
        if arg == "kb-duplicates":
            limit = 20
            as_json = False
            seen_limit = False
            valid = True
            for item in sys.argv[2:]:
                if item.lower() == "--json":
                    as_json = True
                    continue
                parsed = _parse_cli_int(item) if not seen_limit else None
                if parsed is None:
                    valid = False
                    break
                seen_limit = True
                limit = parsed
            if not valid:
                print(
                    "用法：python cli.py kb-duplicates [limit] [--json]"
                    "（0 显示全部，默认 20）"
                )
                sys.exit(2)
            sys.exit(
                run_knowledge_duplicates_standalone(limit, as_json=as_json)
            )
        if arg == "kb-dedupe":
            dry_run = False
            as_json = False
            valid = True
            for item in sys.argv[2:]:
                lowered = item.lower()
                if lowered == "--dry-run":
                    dry_run = True
                elif lowered == "--json":
                    as_json = True
                else:
                    valid = False
                    break
            if not valid:
                print(
                    "用法：python cli.py kb-dedupe [--dry-run] [--json]"
                    "（每组保留最小 id）"
                )
                sys.exit(2)
            sys.exit(
                run_knowledge_dedupe_standalone(
                    dry_run=dry_run, as_json=as_json
                )
            )
        if arg == "stats":
            parsed_stats = _parse_user_and_json_args(sys.argv[2:])
            if parsed_stats is None:
                print("用法：python cli.py stats <user_id> [--json]")
                sys.exit(2)
            stats_user, stats_json = parsed_stats
            sys.exit(run_stats_standalone(stats_user, as_json=stats_json))
        if arg == "preferences":
            parsed_pref = _parse_user_and_json_args(sys.argv[2:])
            if parsed_pref is None:
                print("用法：python cli.py preferences <user_id> [--json]")
                sys.exit(2)
            pref_user, pref_json = parsed_pref
            sys.exit(
                run_preferences_list_standalone(pref_user, as_json=pref_json)
            )
        if arg == "pref-set":
            target = other = value = None
            as_json = False
            valid = True
            for item in sys.argv[2:]:
                if item.lower() == "--json":
                    as_json = True
                elif item.startswith("-") or value is not None:
                    valid = False
                    break
                elif target is None:
                    target = item
                elif other is None:
                    other = item
                else:
                    value = item
            if not valid or value is None:
                print(
                    "用法：python cli.py pref-set <user_id> <偏好类型> <值> [--json]"
                )
                sys.exit(2)
            sys.exit(
                run_preference_set_standalone(
                    target, other, value, as_json=as_json
                )
            )
        if arg == "pref-delete":
            target = other = None
            as_json = False
            valid = True
            for item in sys.argv[2:]:
                if item.lower() == "--json":
                    as_json = True
                elif item.startswith("-") or other is not None:
                    valid = False
                    break
                elif target is None:
                    target = item
                else:
                    other = item
            if not valid or other is None:
                print(
                    "用法：python cli.py pref-delete <user_id> <偏好类型> [--json]"
                )
                sys.exit(2)
            sys.exit(
                run_preference_delete_standalone(
                    target, other, as_json=as_json
                )
            )
        if arg == "trip-add":
            parsed_trip_add = _parse_trip_add_args(sys.argv[2:])
            if parsed_trip_add is None:
                print(
                    "用法：python cli.py trip-add <user_id> --destination X "
                    "--start YYYY-MM-DD [--origin X] [--end YYYY-MM-DD] "
                    "[--purpose X] [--json]"
                )
                sys.exit(2)
            trip_values = parsed_trip_add["values"]
            sys.exit(
                run_trip_add_standalone(
                    parsed_trip_add["user_id"],
                    trip_values.get("origin", ""),
                    trip_values["destination"],
                    trip_values["start"],
                    trip_values.get("end"),
                    trip_values.get("purpose", "出差"),
                    as_json=parsed_trip_add["as_json"],
                )
            )
        if arg == "trip-update":
            parsed_trip_update = _parse_trip_update_args(sys.argv[2:])
            if parsed_trip_update is None:
                print(
                    "用法：python cli.py trip-update <user_id> <trip_id> "
                    "[--origin X] [--destination X] [--start YYYY-MM-DD] "
                    "[--end YYYY-MM-DD] [--purpose X] [--json]"
                )
                sys.exit(2)
            sys.exit(
                run_trip_update_standalone(
                    parsed_trip_update["user_id"],
                    parsed_trip_update["trip_id"],
                    parsed_trip_update["values"],
                    as_json=parsed_trip_update["as_json"],
                )
            )
        if arg == "trip-delete":
            target = None
            other = None
            as_json = False
            valid = True
            for item in sys.argv[2:]:
                if item.lower() == "--json":
                    as_json = True
                elif item.startswith("-") or other is not None:
                    valid = False
                    break
                elif target is None:
                    target = item
                else:
                    other = item
            if not valid or target is None or other is None:
                print("用法：python cli.py trip-delete <user_id> <trip_id> [--json]")
                sys.exit(2)
            sys.exit(
                run_trip_delete_standalone(target, other, as_json=as_json)
            )
        if arg == "trips":
            parsed_trips_list = _parse_trips_list_args(sys.argv[2:])
            if parsed_trips_list is None:
                print(
                    "用法：python cli.py trips <user_id> [--query 关键词] "
                    "[--status active|upcoming|past] [--json]"
                )
                sys.exit(2)
            (
                trips_user,
                trips_query,
                trips_list_json,
                trips_status,
            ) = parsed_trips_list
            sys.exit(
                run_trips_list_standalone(
                    trips_user,
                    as_json=trips_list_json,
                    query=trips_query,
                    status_filter=trips_status,
                )
            )
        if arg == "trips-dedupe":
            if len(sys.argv) < 3 or sys.argv[2].startswith("-"):
                print(
                    "用法：python cli.py trips-dedupe <user_id> [--dry-run] [--json]"
                )
                sys.exit(2)
            dry_run = False
            as_json = False
            valid = True
            for item in sys.argv[3:]:
                lowered = item.lower()
                if lowered == "--dry-run":
                    dry_run = True
                elif lowered == "--json":
                    as_json = True
                else:
                    valid = False
                    break
            if not valid:
                print(
                    "用法：python cli.py trips-dedupe <user_id> [--dry-run] [--json]"
                )
                sys.exit(2)
            sys.exit(
                run_trips_dedupe_standalone(
                    sys.argv[2], dry_run=dry_run, as_json=as_json
                )
            )
        if arg == "sessions":
            usage = (
                "用法：python cli.py sessions <user_id> "
                "[--archived|--all] [--query 关键词] [--json]"
            )
            if len(sys.argv) < 3 or sys.argv[2].startswith("-"):
                print(usage)
                sys.exit(2)
            parsed = _parse_sessions_flags(sys.argv[3:])
            if parsed is None:
                print(usage)
                sys.exit(2)
            archived_only, include_all, query, sessions_json = parsed
            sys.exit(
                run_sessions_list_standalone(
                    sys.argv[2],
                    archived_only=archived_only,
                    include_all=include_all,
                    query=query,
                    as_json=sessions_json,
                )
            )
        if arg == "session-archive":
            if (
                len(sys.argv) < 4
                or sys.argv[2].startswith("-")
                or sys.argv[3].startswith("-")
            ):
                print(
                    "用法：python cli.py session-archive <user_id> <session_id> "
                    "[--restore] [--json]"
                )
                sys.exit(2)
            parsed_archive = _parse_archive_flags(sys.argv[4:])
            if parsed_archive is None:
                print(
                    "用法：python cli.py session-archive <user_id> <session_id> "
                    "[--restore] [--json]"
                )
                sys.exit(2)
            restore, as_json = parsed_archive
            sys.exit(
                run_session_archive_standalone(
                    sys.argv[2],
                    sys.argv[3],
                    archived=not restore,
                    as_json=as_json,
                )
            )
        if arg == "session-delete":
            positional = []
            as_json = False
            valid = True
            for item in sys.argv[2:]:
                lowered = item.lower()
                if lowered == "--json":
                    as_json = True
                elif item.startswith("-") or len(positional) >= 2:
                    valid = False
                    break
                else:
                    positional.append(item)
            if not valid or len(positional) != 2:
                print(
                    "用法：python cli.py session-delete <user_id> <session_id> "
                    "[--json]"
                )
                sys.exit(2)
            sys.exit(
                run_session_delete_standalone(
                    positional[0], positional[1], as_json=as_json
                )
            )
        if arg == "session-title":
            positional: list[str] = []
            as_json = False
            reset = False
            valid = True
            for item in sys.argv[2:]:
                lowered = item.lower()
                if lowered == "--json":
                    as_json = True
                elif lowered == "--reset":
                    reset = True
                elif item.startswith("-") or len(positional) >= 3:
                    valid = False
                    break
                else:
                    positional.append(item)
            if (
                not valid
                or len(positional) < 2
                or (reset and len(positional) == 3)
                or (not reset and len(positional) != 3)
            ):
                print(
                    "用法：python cli.py session-title <user_id> <session_id> "
                    "<标题|--reset> [--json]"
                )
                sys.exit(2)
            title = "" if reset else positional[2]
            sys.exit(
                run_session_title_standalone(
                    positional[0], positional[1], title, as_json=as_json
                )
            )
        if arg == "session-pin":
            positional = []
            as_json = False
            unpin = False
            valid = True
            for item in sys.argv[2:]:
                lowered = item.lower()
                if lowered == "--json":
                    as_json = True
                elif lowered == "--off":
                    unpin = True
                elif item.startswith("-") or len(positional) >= 2:
                    valid = False
                    break
                else:
                    positional.append(item)
            if not valid or len(positional) != 2:
                print(
                    "用法：python cli.py session-pin <user_id> <session_id> "
                    "[--off] [--json]"
                )
                sys.exit(2)
            sys.exit(
                run_session_pin_standalone(
                    positional[0],
                    positional[1],
                    pinned=not unpin,
                    as_json=as_json,
                )
            )
        if arg == "session-show":
            positional = []
            as_json = False
            valid = True
            for item in sys.argv[2:]:
                if item.lower() == "--json":
                    as_json = True
                elif item.startswith("-") or len(positional) >= 2:
                    valid = False
                    break
                else:
                    positional.append(item)
            if not valid or len(positional) != 2:
                print(
                    "用法：python cli.py session-show <user_id> <session_id> [--json]"
                )
                sys.exit(2)
            sys.exit(
                run_session_show_standalone(
                    positional[0], positional[1], as_json=as_json
                )
            )
        if arg == "session-export":
            usage = (
                "用法：python cli.py session-export <user_id> <session_id> "
                "[path] [--stdout] [--force] [--html]"
            )
            if (
                len(sys.argv) < 4
                or sys.argv[2].startswith("-")
                or sys.argv[3].startswith("-")
            ):
                print(usage)
                sys.exit(2)
            parsed_export = _parse_session_export_args(sys.argv[4:])
            if parsed_export is None:
                print(usage)
                sys.exit(2)
            target, to_stdout, force, html = parsed_export
            sys.exit(
                run_session_export_standalone(
                    sys.argv[2],
                    sys.argv[3],
                    target=target,
                    to_stdout=to_stdout,
                    force=force,
                    html=html,
                )
            )
        if arg == "sessions-export":
            usage = (
                "用法：python cli.py sessions-export <user_id> [path] "
                "[--stdout] [--force] [--html]"
            )
            if len(sys.argv) < 3 or sys.argv[2].startswith("-"):
                print(usage)
                sys.exit(2)
            raw_export_args = sys.argv[3:]
            export_args = [
                item for item in raw_export_args if item.lower() != "--html"
            ]
            parsed_export = _parse_export_args(export_args)
            if parsed_export is None:
                print(usage)
                sys.exit(2)
            target, to_stdout, force, _, _ = parsed_export
            sys.exit(
                run_sessions_export_standalone(
                    sys.argv[2],
                    target=target,
                    to_stdout=to_stdout,
                    force=force,
                    html=len(export_args) != len(raw_export_args),
                )
            )
        if arg == "user-export":
            with_knowledge = "--with-knowledge" in sys.argv[2:]
            export_args = [
                item
                for item in sys.argv[2:]
                if item != "--with-knowledge"
            ]
            if (
                not 1 <= len(export_args) <= 2
                or not _valid_user_id(export_args[0])
                or (len(export_args) == 2 and export_args[1].startswith("-"))
                or any(item.startswith("-") for item in export_args)
            ):
                print(
                    "用法：python cli.py user-export <user_id> [dir] "
                    "[--with-knowledge]"
                )
                sys.exit(2)
            sys.exit(
                run_user_export_standalone(
                    export_args[0],
                    export_args[1] if len(export_args) == 2 else None,
                    with_knowledge=with_knowledge,
                )
            )
        if arg == "user-import":
            positional = []
            as_json = False
            dry_run = False
            skip_knowledge = False
            valid = True
            for item in sys.argv[2:]:
                lowered = item.lower()
                if lowered == "--json":
                    as_json = True
                elif lowered == "--dry-run":
                    dry_run = True
                elif lowered == "--skip-knowledge":
                    skip_knowledge = True
                elif item.startswith("-") or len(positional) >= 2:
                    valid = False
                    break
                else:
                    positional.append(item)
            if not valid or len(positional) != 2:
                print(
                    "用法：python cli.py user-import <user_id> <json_path> "
                    "[--dry-run] [--skip-knowledge] [--json]"
                )
                sys.exit(2)
            sys.exit(
                run_user_import_standalone(
                    positional[0],
                    positional[1],
                    as_json=as_json,
                    dry_run=dry_run,
                    skip_knowledge=skip_knowledge,
                )
            )
        if arg == "trips-export":
            trips_usage = (
                "用法：python cli.py trips-export <user_id> [path] "
                "[--stdout] [--force] [--ics|--html] [--query 关键词]"
            )
            if len(sys.argv) < 3 or sys.argv[2].startswith("-"):
                print(trips_usage)
                sys.exit(2)
            raw_export_args = sys.argv[3:]
            export_args = [
                item for item in raw_export_args if item.lower() != "--html"
            ]
            html_flag = len(export_args) != len(raw_export_args)
            parsed_export = _parse_export_args(export_args, allow_ics=True)
            if parsed_export is None:
                print(trips_usage)
                sys.exit(2)
            target, to_stdout, force, ics, export_query = parsed_export
            if html_flag and ics:
                # 两种格式互斥：同时指定时不能静默只导一种
                print(trips_usage)
                sys.exit(2)
            sys.exit(
                run_trips_export_standalone(
                    sys.argv[2],
                    target=target,
                    to_stdout=to_stdout,
                    force=force,
                    ics=ics,
                    query=export_query,
                    html=html_flag,
                )
            )
        if arg == "trips-import":
            import_args = [
                a
                for a in sys.argv[2:]
                if a not in {"--json", "--ics", "--dry-run"}
            ]
            want_json = "--json" in sys.argv[2:]
            want_ics = "--ics" in sys.argv[2:]
            want_dry_run = "--dry-run" in sys.argv[2:]
            if (
                len(import_args) != 2
                or not _valid_user_id(import_args[0])
                or import_args[1].startswith("-")
            ):
                print(
                    "用法：python cli.py trips-import <user_id> <csv_path> "
                    "[--ics] [--dry-run] [--json]"
                )
                sys.exit(2)
            sys.exit(
                run_trips_import_standalone(
                    import_args[0],
                    import_args[1],
                    as_json=want_json,
                    as_ics=want_ics,
                    dry_run=want_dry_run,
                )
            )
        if arg in {"help", "--help", "-h"}:
            print(_COMMANDS_HINT)
            print("不带参数进入交互模式；--version 查看版本")
            sys.exit(0)
        print(f"未知命令：{sys.argv[1]}")
        print(_COMMANDS_HINT)
        print("不带参数进入交互模式；--version 查看版本")
        sys.exit(2)
    # 交互模式需要完整运行时依赖（agentscope / rich / 模型配置）
    if _RUNTIME_IMPORT_ERROR is not None:
        print(f"缺少运行时依赖：{_RUNTIME_IMPORT_ERROR}")
        print("请先安装依赖（pip install -r requirements.txt 或 pip install -e .）后重试")
        sys.exit(1)

    cli = AligoCLI()
    try:
        asyncio.run(cli.run())
    except KeyboardInterrupt:
        # 查询进行中按 Ctrl-C：asyncio.run 会取消任务后抛到这里
        print()
        print("已退出")
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
