"""
记忆数据导出（JSON / Markdown）

`export` 命令的实现：把 MemoryManager.export_data() 的结果写入 JSON 与 Markdown
两份文件，便于用户备份、迁移或查看自己的数据。
"""
import json
from pathlib import Path
from typing import Any


def render_markdown(data: dict[str, Any]) -> str:
    """把导出数据渲染为 Markdown 文本。"""
    lines = [
        "# 差旅助手用户数据导出",
        "",
        f"- 用户 ID: {data.get('user_id', '-')}",
        f"- 会话 ID: {data.get('session_id', '-')}",
        f"- 导出时间: {data.get('exported_at', '-')}",
        "",
        "## 用户偏好",
        "",
    ]

    preferences = data.get("preferences") or {}
    if preferences:
        lines.append("| 类型 | 值 |")
        lines.append("| --- | --- |")
        for key, value in preferences.items():
            if isinstance(value, list):
                value = ", ".join(str(item) for item in value)
            lines.append(f"| {key} | {value} |")
    else:
        lines.append("（暂无偏好）")

    lines += ["", "## 历史行程", ""]
    trips = data.get("trip_history") or []
    if trips:
        lines.append("| ID | 出发地 | 目的地 | 出发日期 | 返程日期 | 目的 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for trip in trips:
            lines.append(
                f"| {trip.get('trip_id', '')} | {trip.get('origin') or '-'} | "
                f"{trip.get('destination') or '-'} | {trip.get('start_date') or '-'} | "
                f"{trip.get('end_date') or '-'} | {trip.get('purpose') or '-'} |"
            )
    else:
        lines.append("（暂无行程）")

    statistics = data.get("statistics") or {}
    if statistics:
        lines += ["", "## 统计", ""]
        lines.append(f"- 总行程数: {statistics.get('total_trips', 0)}")
        lines.append(f"- 总消息数: {statistics.get('total_messages', 0)}")
        lines.append(f"- 总查询数: {statistics.get('total_queries', 0)}")
        frequent = statistics.get("frequent_destinations") or {}
        if frequent:
            lines.append("- 常去目的地: " + ", ".join(f"{city}({count}次)" for city, count in frequent.items()))

    chats = data.get("chat_history") or []
    lines += ["", f"## 对话历史（{len(chats)} 条）", ""]
    if chats:
        for message in chats:
            role = "用户" if message.get("role") == "user" else "助手"
            content = str(message.get("content", ""))
            if len(content) > 200:
                content = content[:200] + "…"
            lines.append(f"- **{role}**（{message.get('timestamp', '')}）：{content}")
    else:
        lines.append("（暂无对话记录）")

    lines.append("")
    return "\n".join(lines)


def export_files(
    data: dict[str, Any],
    out_dir: Path | str,
    stem: str | None = None,
) -> list[Path]:
    """把导出数据写入 `<out_dir>/<stem>.json` 与 `<stem>.md`，返回文件路径列表。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    if stem is None:
        timestamp = str(data.get("exported_at", "")).replace(":", "").replace("-", "")[:15]
        stem = f"{data.get('user_id', 'user')}_{timestamp or 'export'}"

    json_path = out / f"{stem}.json"
    md_path = out / f"{stem}.md"
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(data), encoding="utf-8")
    return [json_path, md_path]
