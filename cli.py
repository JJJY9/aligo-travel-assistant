#!/usr/bin/env python
"""
Aligo 商旅助手 - CLI 交互界面
使用 Rich 库实现美观的终端交互
"""
import asyncio
import os
import sys

# 添加项目根目录到路径
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_root)

import json

# 导入系统组件
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

from agents.intention_agent import IntentionAgent
from agents.orchestration_agent import OrchestrationAgent
from config import LLM_CONFIG, RESILIENCE_CONFIG, SYSTEM_CONFIG
from config_agentscope import create_chat_model, init_agentscope
from context.memory_manager import MemoryManager
from utils.circuit_breaker import CircuitBreaker, CircuitOpenError
from utils.console import configure_stdio
from utils.llm_resilience import retry_with_backoff
from utils.llm_resilience import run_health_check as check_llm_health
from utils.progress import format_progress_event
from utils.turn_meta import attach_turn_meta, build_turn_meta

# 移除其他智能体的导入，改用懒加载


class AligoCLI:
    """Aligo 商旅助手 CLI"""

    def __init__(self):
        """初始化 CLI"""
        self.console = Console()
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
            value = (
                Prompt.ask("用户ID", default="default_user").strip() or "default_user"
            )
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
            timeout_sec = SYSTEM_CONFIG.get("timeout", 60)
            self.model = create_chat_model(timeout=timeout_sec)

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

        rc = RESILIENCE_CONFIG
        max_retries = int(rc.get("max_retries", 3))

        with self.console.status("正在理解需求…", spinner="dots"):
            from agentscope.message import Msg

            # 1. 获取长期记忆摘要与上下文（原逻辑不变）
            long_term_summary = await self._get_long_term_summary(user_input)
            recent_context = self.memory_manager.short_term.get_recent_context(n_turns=5)
            context_messages = []
            if long_term_summary:
                context_messages.append(Msg(name="system", content=long_term_summary, role="system"))
            for msg in recent_context:
                context_messages.append(Msg(name=msg["role"], content=msg["content"], role=msg["role"]))
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
            except CircuitOpenError:
                raise
            except Exception:
                if self.circuit_breaker:
                    self.circuit_breaker.record_failure()
                raise

            # 3. 解析意图识别结果（原逻辑不变：解析失败则友好提示并 return）
            try:
                intention_data = json.loads(intention_result.content)
            except json.JSONDecodeError:
                self.console.print("❌ 无法理解您的需求，请重新描述", style="bold red")
                return

        # 4. 添加用户输入到短期记忆（原逻辑不变）
        self.memory_manager.add_message("user", user_input)

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
        except CircuitOpenError:
            raise
        except Exception:
            if self.circuit_breaker:
                self.circuit_breaker.record_failure()
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

        # 9. 写入长期记忆（附带执行元数据，Web 端历史会话可回看执行路线）
        meta = build_turn_meta(
            intention_data if isinstance(intention_data, dict) else {},
            result_data,
            usage=usage_delta,
            elapsed=elapsed,
        )
        self.memory_manager.add_message(
            "assistant",
            json.dumps(attach_turn_meta(result_data, meta), ensure_ascii=False),
        )

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
            self.console.print(f"🤖 调用智能体: {', '.join(agents_called)}", style="dim")

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
                if pref_value:  # 只添加有值的偏好
                    # 如果是列表，用逗号连接
                    if isinstance(pref_value, list):
                        pref_lines.append(f"• {pref_key}: {', '.join(pref_value)}")
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
                origin = trip.get("origin", "") or ""
                destination = trip.get("destination", "") or ""

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

    def _generate_human_response(self, results: list) -> bool:
        """
        根据结果生成人性化的回复
        """
        has_output = False

        for result in results:
            agent_name = result.get("agent_name", "")
            status = result.get("status", "")
            data = result.get("data", {})
            current_agent_shown = False  # 标记当前Agent是否有内容展示

            # 处理失败的智能体
            if status == "error":
                error_msg = data.get("error", "未知错误")
                agent_display_name = self._get_agent_display_name(agent_name)
                self.console.print(f"❌ {agent_display_name}执行失败: {error_msg}", style="red")
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
                
                if itinerary:
                    title = itinerary.get('title', '行程规划')
                    self.console.print(f"\n✈️  [bold cyan]{title}[/bold cyan]")
                    self.console.print(f"时长: {itinerary.get('duration', '未知')}\n")

                    # 每日行程
                    for day_plan in itinerary.get("daily_plans", []):
                        day_num = day_plan.get("day", 1)
                        self.console.print(f"[bold yellow]第 {day_num} 天[/bold yellow]")

                        # 兼容 activities 和 time_slots
                        activities = day_plan.get("activities") or day_plan.get("time_slots") or []
                        for slot in activities:
                            time = slot.get("time", "")
                            # 兼容 activity 和 location
                            activity = slot.get("activity") or slot.get("location") or ""
                            description = slot.get("description", "")
                            transport = slot.get("transport", "")

                            self.console.print(f"  {time} - {activity}")
                            if description:
                                self.console.print(f"    {description}", style="dim")
                            if transport:
                                self.console.print(f"    🚇 {transport}", style="dim")

                        # 餐食建议
                        meals = day_plan.get("meals", {})
                        if meals:
                            self.console.print()
                            if meals.get("lunch"):
                                self.console.print(f"  🍜 {meals['lunch']}", style="dim")
                            if meals.get("dinner"):
                                self.console.print(f"  🍽️  {meals['dinner']}", style="dim")
                        self.console.print()

                    # 注意事项
                    notes = itinerary.get("notes", [])
                    if notes:
                        self.console.print("[bold]📌 注意事项[/bold]")
                        for note in notes:
                            self.console.print(f"  • {note}")
                    current_agent_shown = True

            # 偏好管理
            elif agent_name == "preference":
                raw_prefs = data.get("preferences")
                # 增强：支持从 data.data.preferences 获取
                if not raw_prefs and "data" in data and isinstance(data["data"], dict):
                    raw_prefs = data["data"].get("preferences")

                if isinstance(raw_prefs, dict):
                    prefs_list = raw_prefs.get("preferences", [])
                else:
                    prefs_list = raw_prefs if isinstance(raw_prefs, list) else []

                if prefs_list:
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
                    for pref in prefs_list:
                        pref_type = pref.get("type", "")
                        pref_value = pref.get("value", "")
                        action = pref.get("action", "replace")
                        display_type = type_names.get(pref_type, pref_type)
                        action_text = "追加" if action == "append" else "设置为"
                        self.console.print(f"  • {display_type} {action_text} [cyan]{pref_value}[/cyan]")
                    current_agent_shown = True
                    has_itinerary = any(r.get("agent_name") == "itinerary_planning" for r in results)
                    if not has_itinerary:
                        self.console.print("\n💡 下次规划行程时会参考这些偏好。", style="dim")
                else:
                    # 检查是否有错误信息
                    err = data.get("error", "")
                    if err:
                        self.console.print(f"偏好未保存: {err}", style="yellow")
                        current_agent_shown = True
                    # 如果只是没提取到，可能就是没偏好，不强求显示，交给兜底逻辑

            # 事项收集
            elif agent_name == "event_collection":
                # 增强：支持从 data.data 获取
                origin = data.get("origin") or data.get("data", {}).get("origin")
                destination = data.get("destination") or data.get("data", {}).get("destination")
                start_date = data.get("start_date") or data.get("data", {}).get("start_date")
                end_date = data.get("end_date") or data.get("data", {}).get("end_date")
                missing_info = data.get("missing_info") or data.get("data", {}).get("missing_info") or []

                has_itinerary = any(r.get("agent_name") == "itinerary_planning" for r in results)
                info_shown = False
                if not has_itinerary and (destination or origin):
                    self.console.print("✓ [bold green]已收集行程信息[/bold green]")
                    if origin:
                        self.console.print(f"  • 出发地: [cyan]{origin}[/cyan]")
                    if destination:
                        self.console.print(f"  • 目的地: [cyan]{destination}[/cyan]")
                    if start_date:
                        self.console.print(f"  • 出发日期: [cyan]{start_date}[/cyan]")
                    if end_date:
                        self.console.print(f"  • 返程日期: [cyan]{end_date}[/cyan]")
                    info_shown = True

                if missing_info:
                    self.console.print(f"\n💡 还需要补充: {', '.join(missing_info)}", style="yellow")
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
                    self.console.print(f"\n{summary}")
                    current_agent_shown = True
                elif message:
                    self.console.print(f"\n{message}", style="dim")
                    current_agent_shown = True
                elif error:
                    self.console.print(f"\n{error}", style="yellow")
                    current_agent_shown = True

                if sources:
                    self.console.print("\n[bold]参考来源[/bold]")
                    for i, source in enumerate(sources[:3], 1):
                        url = source.get("url", "") if isinstance(source, dict) else str(source)
                        self.console.print(f"  {i}. {url}", style="dim")
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
                    self.console.print(f"\n{answer}")
                    current_agent_shown = True

            # 记忆查询
            elif agent_name == "memory_query":
                query_result = data.get("answer") or data.get("result") or data.get("content")
                if not query_result and "data" in data and isinstance(data["data"], dict):
                    inner = data["data"]
                    query_result = inner.get("answer") or inner.get("result") or inner.get("content")

                if query_result:
                    self.console.print(f"\n{query_result}")
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
                    self.console.print(f"\n{fallback_content}")
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

            for msg in recent_messages:
                role_name = "👤 用户" if msg["role"] == "user" else "🤖 助手"
                content = msg["content"]

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

                dialogue_table.add_row(role_name, content, time_str)

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
            self.console.print(f"LLM 服务: [red]不可用[/red] - {msg}", style="bold")
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
                trip.get("trip_id", ""),
                trip.get("origin", ""),
                trip.get("destination", ""),
                trip.get("start_date", ""),
                trip.get("purpose", "")
            )

        self.console.print(table)

    def show_preferences(self):
        """显示用户偏好"""
        prefs = self.memory_manager.long_term.get_preference()

        table = Table(title="用户偏好", show_header=True, header_style="bold magenta")
        table.add_column("类型", style="cyan")
        table.add_column("值", style="white")

        for key, value in prefs.items():
            if value:
                table.add_row(key, str(value))

        self.console.print(table)

    def export_memory(self):
        """把当前用户的长期记忆导出为 JSON 与 Markdown（exports/ 目录）"""
        from utils.exporter import export_files

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

        # 主循环
        while True:
            try:
                # 获取用户输入
                user_input = Prompt.ask("\n[cyan]>[/cyan]")

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

            except KeyboardInterrupt:
                self.console.print("\n使用 'exit' 退出", style="dim")
            except CircuitOpenError:
                self.console.print("\n[bold yellow]⚠ 服务暂时不可用，请稍后再试。[/bold yellow]", style="dim")
            except Exception as e:
                self.console.print(f"\n错误: {e}", style="red")


def run_health_check_standalone() -> int:
    """
    独立执行健康检查（用于 `python cli.py health`）。
    不进入交互式 CLI，只检测 LLM 是否可达。
    Returns:
        0 成功，1 失败（便于脚本/监控）
    """
    import asyncio
    init_agentscope()
    ok, msg = asyncio.run(check_llm_health(
        base_url=str(LLM_CONFIG["base_url"]),
        api_key=str(LLM_CONFIG["api_key"]),
        model_name=str(LLM_CONFIG["model_name"]),
        timeout_sec=RESILIENCE_CONFIG.get("health_check_timeout_sec", 10.0),
    ))
    if ok:
        print("OK")
        return 0
    print(f"FAIL: {msg}")
    return 1


def run_doctor_standalone() -> int:
    """
    独立执行环境自检（用于 `python cli.py doctor`），不进入交互式 CLI。

    Returns:
        0 无 fail 项；1 存在必须处理的问题（便于脚本/监控）
    """
    import asyncio

    from utils.doctor import build_report_table, has_failures, run_doctor, summarize

    results = asyncio.run(run_doctor(include_network=bool(LLM_CONFIG["api_key"])))
    Console().print(build_report_table(results))
    text, style = summarize(results)
    Console().print(text, style=style)
    return 1 if has_failures(results) else 0


def run_knowledge_status_standalone() -> int:
    """打印知识库状态（用于 `python cli.py kb`），不进入交互式 CLI。

    Returns:
        0 知识库可用；1 未初始化或不可用
    """
    from utils.knowledge_stats import knowledge_status

    status = knowledge_status()
    if status.get("available"):
        print(f"知识库：{status['collection']} · {status['documents']} 片段")
        print(f"数据库：{status['path']}")
        return 0
    print(f"知识库不可用：{status.get('reason', '未知原因')}")
    print(f"数据库：{status.get('path', '')}")
    return 1


def run_rag_eval_standalone(min_hit5: float = 0.8) -> int:
    """运行 RAG 检索评测（用于 `python cli.py eval`），不进入交互式 CLI。

    Returns:
        0 评测完成且 hit@5 达标（或知识库未初始化，跳过）；1 未达标或后端故障
    """
    from utils.knowledge_stats import knowledge_status

    status = knowledge_status()
    if not status.get("available"):
        reason = str(status.get("reason", "未知原因"))
        if reason in {"知识库未初始化", "集合不存在"}:
            print(f"跳过评测：{reason}")
            return 0
        # 读取失败 / 文件锁占用等属于基础设施故障，不能当作“跳过”并返回成功
        print(f"评测失败：知识库不可用（{reason}）")
        return 1

    from agents.rag_knowledge_agent import RAGKnowledgeAgent
    from utils.rag_eval import evaluate_retrieval

    try:
        agent = RAGKnowledgeAgent(collection_name=status["collection"])
    except Exception as exc:  # noqa: BLE001 - 嵌入模型缺失 / 离线下载失败等
        print(f"评测失败：无法初始化 RAG Agent（{exc}）")
        return 1
    if not agent.initialized:
        print("评测失败：RAG 依赖未安装（pip install -r requirements.txt）")
        return 1

    result = evaluate_retrieval(
        lambda query, top_k: agent.search_knowledge(query, top_k=top_k),
    )
    for row in result["rows"]:
        mark = f"#{row['rank']}" if row["rank"] else "未命中"
        print(f"  {row['query']} → {mark}")
    print(
        f"检索评测：{result['total']} 个问题 · hit@1 {result['hit1']:.0%} · "
        f"hit@3 {result['hit3']:.0%} · hit@5 {result['hit5']:.0%} · MRR {result['mrr']:.2f}"
    )
    if result["hit5"] < min_hit5:
        if result["hit5"] == 0:
            print("提示：所有问题均未命中，可能是索引未加载 / 知识库故障，而非切分策略问题")
        print(f"低于阈值 hit@5 >= {min_hit5:.0%}，请检查知识库内容或切分策略")
        return 1
    return 0


def run_knowledge_export_standalone(
    target: str | None = None, force: bool = False
) -> int:
    """导出知识库为 JSON 文件（用于 `python cli.py kb-export [path] [--force]`）。

    Returns:
        0 导出成功；1 参数 / 知识库不可用 / 文件已存在 / 写入失败
    """
    from pathlib import Path

    from utils.knowledge_admin import export_documents
    from utils.knowledge_stats import knowledge_status

    status = knowledge_status()
    if not status.get("available"):
        print(f"导出失败：知识库不可用（{status.get('reason', '未知原因')}）")
        return 1

    try:
        documents = export_documents()
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        print(f"导出失败：{exc}")
        return 1

    from datetime import datetime, timezone

    payload = {
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "documents": documents,
    }
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


def run_knowledge_import_standalone(source: str) -> int:
    """从 JSON 文件批量导入知识片段（用于 `python cli.py kb-import <file>`）。

    Returns:
        0 导入成功；1 文件 / 格式 / 知识库错误
    """
    from pathlib import Path

    from utils.knowledge_admin import import_documents

    path = Path(source)
    if not path.exists():
        print(f"文件不存在：{path}")
        return 1
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"读取失败：{exc}（文件需为 UTF-8 编码的 JSON）")
        return 1

    documents = (
        data
        if isinstance(data, list)
        else data.get("documents")
        if isinstance(data, dict)
        else None
    )
    if not isinstance(documents, list) or not documents:
        print("文件中没有知识片段（需要 documents 数组或纯数组）")
        return 1

    try:
        result = import_documents(documents)
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        print(f"导入失败：{exc}")
        return 1
    print(f"已导入 {result['imported']} 条知识片段，跳过 {result['skipped']} 条")
    return 0


def run_knowledge_list_standalone(query: str = "", limit: int = 50) -> int:
    """列出知识片段（用于 `python cli.py kb-list [关键词]`）。

    Returns:
        0 列出成功；1 知识库不可用或读取失败
    """
    from utils.knowledge_admin import list_documents
    from utils.knowledge_stats import knowledge_status

    status = knowledge_status()
    if not status.get("available"):
        print(f"知识库不可用：{status.get('reason', '未知原因')}")
        return 1
    try:
        data = list_documents(limit=limit, offset=0, query=query)
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        print(f"读取失败：{exc}")
        return 1

    items = data.get("items") or []
    total = data.get("total", len(items))
    suffix = f"（关键词：{query}）" if query else ""
    print(f"共 {total} 条{suffix}，显示前 {len(items)} 条（按 id 倒序）")
    for item in items:
        meta = item.get("metadata") or {}
        category = meta.get("category") or meta.get("title") or ""
        preview = " ".join(str(item.get("content", "")).split())[:60]
        tag = f" · {category}" if category else ""
        print(f"  #{item['id']}{tag}  {preview}")
    return 0


def run_knowledge_delete_standalone(doc_id: int) -> int:
    """删除一个知识片段（用于 `python cli.py kb-delete <id>`）。

    Returns:
        0 删除成功；1 片段不存在或删除失败
    """
    from utils.knowledge_admin import delete_document

    try:
        removed = delete_document(doc_id)
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        print(f"删除失败：{exc}")
        return 1
    if not removed:
        print(f"片段不存在：#{doc_id}")
        return 1
    print(f"已删除知识片段 #{doc_id}")
    return 0


def run_trips_dedupe_standalone(user_id: str, dry_run: bool = False) -> int:
    """清理历史行程中的完全重复记录（用于 `python cli.py trips-dedupe <user_id> [--dry-run]`）。

    同路线 + 同起止日期视为重复：保留最早一条，删除其余记录（统计同步更新）。
    ``dry_run`` 仅列出将被删除的记录，不做修改。

    Returns:
        0 清理完成（含无重复 / dry-run）；1 参数 / 读取 / 删除失败
    """
    import re

    if not re.fullmatch(r"[\w-]{1,64}", user_id):
        print("用户 ID 只能包含中文、字母、数字、下划线和短横线")
        return 1

    from config import STORAGE_CONFIG
    from context.long_term_memory import LongTermMemory
    from context.storage import create_long_term_memory

    try:
        store = create_long_term_memory(user_id, "data/memory")
    except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
        print(f"打开长期记忆失败：{exc}")
        return 1

    try:
        # PostgreSQL 配置了但连接失败时，工厂会静默回退本地 JSON；维护命令
        # 必须确保清理的是目标后端，避免对过期本地数据“成功”而真实库未处理
        if STORAGE_CONFIG.get("postgres_dsn") and isinstance(store, LongTermMemory):
            print("PostgreSQL 已配置但连接失败（已回退本地 JSON），未执行去重；请检查数据库后重试")
            return 1

        try:
            trips = store.get_trip_history(limit=None)
        except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
            print(f"读取行程失败：{exc}")
            return 1

        groups: dict[tuple[str, str, str, str], list[str]] = {}
        for trip in trips:
            start_date = str(trip.get("start_date") or "")
            if not start_date:
                continue
            key = (
                str(trip.get("origin") or ""),
                str(trip.get("destination") or ""),
                start_date,
                str(trip.get("end_date") or ""),
            )
            groups.setdefault(key, []).append(str(trip.get("trip_id") or ""))

        if all(len(ids) <= 1 for ids in groups.values()):
            print(f"用户 {user_id}：没有重复行程（共 {len(trips)} 条）")
            return 0

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
            print(f"读取行程失败：{exc}")
            return 1

        duplicates = [
            trip_id
            for ids in groups.values()
            for trip_id in ids[1:]
            if ids[0] in fresh_ids
        ]

        if dry_run:
            if not duplicates:
                print(f"用户 {user_id}：没有需要删除的重复行程（共 {len(trips)} 条）")
                return 0
            by_id = {str(trip.get("trip_id") or ""): trip for trip in trips}
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
        try:
            for trip_id in duplicates:
                if trip_id and store.delete_trip(trip_id):
                    removed += 1
        except Exception as exc:  # noqa: BLE001 - 部分删除后失败要如实报告
            print(f"删除过程中失败：{exc}（已删除 {removed} 条）")
            return 1
        if duplicates:
            print(f"用户 {user_id}：删除 {removed} 条重复行程（保留最早记录）")
        else:
            print(f"用户 {user_id}：没有需要删除的重复行程（共 {len(trips)} 条）")
        return 0
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
        if timestamp:
            entry["updated_at"] = timestamp
        if not entry["title"] and message.get("role") == "user":
            title = " ".join(str(message.get("content", "")).split())
            if title:
                entry["title"] = title[:40]
    for entry in grouped.values():
        entry["title"] = str(
            titles.get(entry["session_id"]) or entry["title"] or "新对话"
        )
        entry["pinned"] = entry["session_id"] in pinned
        entry["archived"] = entry["session_id"] in archived
    return sorted(grouped.values(), key=lambda item: item["updated_at"], reverse=True)


def run_sessions_list_standalone(
    user_id: str, archived_only: bool = False, include_all: bool = False
) -> int:
    """列出用户会话（用于 `python cli.py sessions <user_id> [--archived|--all]`）。

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

    if not visible:
        print(
            f"用户 {user_id}：没有{scope}会话"
            f"（共 {len(summaries)} 个，归档 {archived_total} 个）"
        )
        return 0

    print(
        f"用户 {user_id}：{len(visible)} 个{scope}会话"
        f"（共 {len(summaries)} 个，归档 {archived_total} 个）"
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
    return 0


def run_session_archive_standalone(
    user_id: str, session_id: str, archived: bool = True
) -> int:
    """归档 / 恢复一个会话（用于 `python cli.py session-archive <user> <session> [--restore]`）。

    Returns:
        0 操作成功；1 参数 / 读取 / 写入失败
    """
    import re

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
            exists = bool(store.get_chat_history(session_id=session_id))
        except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
            print(f"读取会话失败：{exc}")
            return 1
        if not exists:
            print(f"会话 {session_id} 不存在或没有消息")
            return 1
        try:
            store.set_session_archived(session_id, archived)
        except Exception as exc:  # noqa: BLE001 - CLI 场景直接展示原因
            print(f"操作失败：{exc}")
            return 1
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()
    print(f"已{'归档' if archived else '恢复'}会话 {session_id}")
    return 0


def _parse_sessions_flags(args: list[str]) -> tuple[bool, bool] | None:
    """解析 sessions 的 --archived / --all；未知参数或同时使用返回 None。"""
    flags = [item.lower() for item in args]
    if any(flag not in {"--archived", "--all"} for flag in flags):
        return None
    if "--archived" in flags and "--all" in flags:
        return None
    return "--archived" in flags, "--all" in flags


def _parse_archive_flags(args: list[str]) -> bool | None:
    """解析 session-archive 的 --restore；未知参数返回 None。"""
    flags = [item.lower() for item in args]
    if any(flag != "--restore" for flag in flags):
        return None
    return "--restore" in flags


def run_session_export_standalone(
    user_id: str,
    session_id: str,
    target: str | None = None,
    to_stdout: bool = False,
    force: bool = False,
) -> int:
    """导出单个会话为 Markdown（`python cli.py session-export <user> <session> [path] [--stdout] [--force]`）。

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
    title = title or "Aligo 对话"

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
        print(text)
        return 0

    path = Path(target) if target else Path("exports") / f"aligo_{session_id}.md"
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


def run_trips_export_standalone(
    user_id: str,
    target: str | None = None,
    to_stdout: bool = False,
    force: bool = False,
) -> int:
    """导出全部行程为 CSV（`python cli.py trips-export <user_id> [path] [--stdout] [--force]`）。

    Returns:
        0 导出成功；1 参数 / 读取 / 写入失败
    """
    import re
    from pathlib import Path

    from utils.trip_export import render_trips_csv

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

    text = render_trips_csv(trips)
    if to_stdout:
        print(text, end="")
        return 0

    path = Path(target) if target else Path("exports") / "aligo_trips.csv"
    if path.exists() and not force:
        print(f"文件已存在：{path}（使用 --force 覆盖或指定其它路径）")
        return 1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        print(f"写入失败：{exc}")
        return 1
    print(f"已导出 {len(trips)} 条行程到 {path}")
    return 0


def run_trips_list_standalone(user_id: str) -> int:
    """列出用户行程（用于 `python cli.py trips <user_id>`）。

    Returns:
        0 列出成功（含无行程）；1 参数 / 读取失败
    """
    import re

    from utils.trip_conflicts import trip_state_label, trips_with_conflicts

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
        print(f"用户 {user_id}：还没有行程记录")
        return 0

    # 与 Web 记忆面板一致：新→旧展示，并标注状态与日期重叠
    views = trips_with_conflicts(trips)
    print(f"用户 {user_id}：共 {len(views)} 条行程（新→旧）")
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


def _parse_kb_export_args(args: list[str]) -> tuple[str | None, bool] | None:
    """解析 kb-export 的 [path] [--force]；冲突 / 未知参数返回 None。"""
    target: str | None = None
    force = False
    for item in args:
        lowered = item.lower()
        if lowered == "--force":
            force = True
        elif item.startswith("-"):
            return None
        elif target is None:
            target = item
        else:
            return None
    return target, force


def _parse_export_args(
    args: list[str],
) -> tuple[str | None, bool, bool] | None:
    """解析导出命令的 [path] [--stdout] [--force]；冲突 / 未知参数返回 None。"""
    to_stdout = False
    force = False
    paths: list[str] = []
    for item in args:
        lowered = item.lower()
        if lowered == "--stdout":
            to_stdout = True
        elif lowered == "--force":
            force = True
        elif item.startswith("-"):
            return None
        else:
            paths.append(item)
    if len(paths) > 1 or (to_stdout and paths):
        return None
    return (paths[0] if paths else None), to_stdout, force


def main():
    """主函数"""
    configure_stdio()
    if len(sys.argv) > 1:
        arg = sys.argv[1].strip().lower()
        if arg == "health":
            sys.exit(run_health_check_standalone())
        if arg == "doctor":
            sys.exit(run_doctor_standalone())
        if arg == "kb":
            sys.exit(run_knowledge_status_standalone())
        if arg == "eval":
            sys.exit(run_rag_eval_standalone())
        if arg == "kb-export":
            parsed_kb_export = _parse_kb_export_args(sys.argv[2:])
            if parsed_kb_export is None:
                print("用法：python cli.py kb-export [path] [--force]")
                sys.exit(2)
            kb_target, kb_force = parsed_kb_export
            sys.exit(run_knowledge_export_standalone(kb_target, force=kb_force))
        if arg == "kb-import":
            if len(sys.argv) < 3:
                print("用法：python cli.py kb-import <file.json>")
                sys.exit(2)
            sys.exit(run_knowledge_import_standalone(sys.argv[2]))
        if arg == "kb-list":
            keywords = sys.argv[2] if len(sys.argv) > 2 else ""
            sys.exit(run_knowledge_list_standalone(keywords))
        if arg == "kb-delete":
            if len(sys.argv) < 3 or not sys.argv[2].isdigit():
                print("用法：python cli.py kb-delete <id>")
                sys.exit(2)
            sys.exit(run_knowledge_delete_standalone(int(sys.argv[2])))
        if arg == "trips":
            if len(sys.argv) != 3 or sys.argv[2].startswith("-"):
                print("用法：python cli.py trips <user_id>")
                sys.exit(2)
            sys.exit(run_trips_list_standalone(sys.argv[2]))
        if arg == "trips-dedupe":
            if len(sys.argv) < 3 or sys.argv[2].startswith("-"):
                print("用法：python cli.py trips-dedupe <user_id> [--dry-run]")
                sys.exit(2)
            flags = [item.lower() for item in sys.argv[3:]]
            if any(flag != "--dry-run" for flag in flags):
                print("用法：python cli.py trips-dedupe <user_id> [--dry-run]")
                sys.exit(2)
            sys.exit(
                run_trips_dedupe_standalone(
                    sys.argv[2], dry_run="--dry-run" in flags
                )
            )
        if arg == "sessions":
            if len(sys.argv) < 3 or sys.argv[2].startswith("-"):
                print("用法：python cli.py sessions <user_id> [--archived|--all]")
                sys.exit(2)
            parsed = _parse_sessions_flags(sys.argv[3:])
            if parsed is None:
                print("用法：python cli.py sessions <user_id> [--archived|--all]")
                sys.exit(2)
            archived_only, include_all = parsed
            sys.exit(
                run_sessions_list_standalone(
                    sys.argv[2],
                    archived_only=archived_only,
                    include_all=include_all,
                )
            )
        if arg == "session-archive":
            if (
                len(sys.argv) < 4
                or sys.argv[2].startswith("-")
                or sys.argv[3].startswith("-")
            ):
                print(
                    "用法：python cli.py session-archive <user_id> <session_id> [--restore]"
                )
                sys.exit(2)
            restore = _parse_archive_flags(sys.argv[4:])
            if restore is None:
                print(
                    "用法：python cli.py session-archive <user_id> <session_id> [--restore]"
                )
                sys.exit(2)
            sys.exit(
                run_session_archive_standalone(
                    sys.argv[2],
                    sys.argv[3],
                    archived=not restore,
                )
            )
        if arg == "session-export":
            if (
                len(sys.argv) < 4
                or sys.argv[2].startswith("-")
                or sys.argv[3].startswith("-")
            ):
                print(
                    "用法：python cli.py session-export <user_id> <session_id> [path] [--stdout] [--force]"
                )
                sys.exit(2)
            parsed_export = _parse_export_args(sys.argv[4:])
            if parsed_export is None:
                print(
                    "用法：python cli.py session-export <user_id> <session_id> [path] [--stdout] [--force]"
                )
                sys.exit(2)
            target, to_stdout, force = parsed_export
            sys.exit(
                run_session_export_standalone(
                    sys.argv[2],
                    sys.argv[3],
                    target=target,
                    to_stdout=to_stdout,
                    force=force,
                )
            )
        if arg == "trips-export":
            if len(sys.argv) < 3 or sys.argv[2].startswith("-"):
                print(
                    "用法：python cli.py trips-export <user_id> [path] [--stdout] [--force]"
                )
                sys.exit(2)
            parsed_export = _parse_export_args(sys.argv[3:])
            if parsed_export is None:
                print(
                    "用法：python cli.py trips-export <user_id> [path] [--stdout] [--force]"
                )
                sys.exit(2)
            target, to_stdout, force = parsed_export
            sys.exit(
                run_trips_export_standalone(
                    sys.argv[2],
                    target=target,
                    to_stdout=to_stdout,
                    force=force,
                )
            )
    cli = AligoCLI()
    asyncio.run(cli.run())


if __name__ == "__main__":
    main()
