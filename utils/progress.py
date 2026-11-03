"""
智能体进度事件格式化

把 OrchestrationAgent / ConversationRuntime 发出的进度事件转成 CLI 可显示的一行文本。
保持纯函数，便于单元测试（无需 API Key）。
"""
from collections.abc import Callable
from typing import Any

DisplayName = Callable[[str], str]


def format_progress_event(
    event: dict[str, Any],
    display_name: DisplayName | None = None,
) -> str | None:
    """把一条进度事件格式化为文本；无法识别的事件返回 None。"""
    event_type = event.get("type")
    raw_name = str(event.get("agent_name") or "")
    name = display_name(raw_name) if display_name else raw_name

    if event_type == "agent_start":
        return f"正在执行 {name}…" if name else "正在执行智能体…"

    if event_type == "agent_done":
        mark = "✓" if event.get("status") == "success" else "✗"
        duration = event.get("duration")
        suffix = ""
        if isinstance(duration, (int, float)):
            suffix = f" {float(duration):.1f}s"
        return f"{mark} {name}{suffix}".strip()

    if event_type == "phase":
        label = event.get("label")
        return str(label) if label else None

    if event_type == "plan":
        schedule = event.get("schedule")
        count = len(schedule) if isinstance(schedule, list) else 0
        return f"已规划 {count} 个智能体" if count else "无需调度智能体"

    return None
