"""
记忆数据导出（JSON / Markdown）

`export` 命令的实现：把 MemoryManager.export_data() 的结果写入 JSON 与 Markdown
两份文件，便于用户备份、迁移或查看自己的数据。
"""
import json
import re
from pathlib import Path
from typing import Any

from utils.console import strip_terminal_controls
from utils.json_safe import sanitize_json
from utils.session_markdown import sanitize_body
from utils.stored_message import render_stored_message

_CELL_BREAK = re.compile(r"[\r\n]+")


def _md_cell(value: Any) -> str:
    """导出表格 / 列表用的单行文本：竖线与换行会破坏 Markdown 结构，
    尖括号转义避免把用户 / 模型可控内容当 HTML 落盘；终端控制序列
    （ESC / OSC）一并剥离。"""
    text = strip_terminal_controls(
        str(value if value is not None else "-")
    )
    text = _CELL_BREAK.sub(" ", text)
    # 反斜杠必须先转义：值里的 `\` 与随后的 `|` 会被 Markdown 当成
    # 「转义后的竖线」，单元格行仍按 `|` 切列而错位
    text = text.replace("\\", "\\\\").replace("|", "\\|")
    return text.replace("<", "&lt;").replace(">", "&gt;")


def _strip_nul_deep(value: Any) -> Any:
    """递归移除字符串中的 NUL：备份导入会整条拒绝含 NUL 的消息，
    导出侧必须先清洗，否则「导出成功」的备份恢复时静默丢消息。"""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [_strip_nul_deep(item) for item in value]
    if isinstance(value, dict):
        return {key: _strip_nul_deep(item) for key, item in value.items()}
    return value


def sanitize_backup_data(data: dict[str, Any]) -> dict[str, Any]:
    """备份数据清洗（JSON 与 Markdown 导出的统一入口）。

    ``sanitize_json`` 只处理 Python 值层：助手消息的 ``content`` 是一段
    不透明 JSON 文本，其中的 NaN / Infinity 不会被顶层清洗，导入端却会
    按「消息元数据含非有限数」整条拒绝；NUL 同理。这里额外解析助手 JSON
    文本、递归清洗后用规范 JSON 写回，并移除所有字符串里的 NUL。
    """
    cleaned = sanitize_json(data)
    if not isinstance(cleaned, dict):
        return cleaned
    history = cleaned.get("chat_history")
    if isinstance(history, list):
        for message in history:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, str):
                continue
            text = content.replace("\x00", "")
            if message.get("role") != "user" and text.lstrip().startswith(("{", "[")):
                try:
                    parsed = json.loads(text)
                except (ValueError, RecursionError):
                    parsed = None
                if isinstance(parsed, (dict, list)):
                    try:
                        text = json.dumps(
                            sanitize_json(parsed), ensure_ascii=False
                        )
                    except (TypeError, ValueError):
                        pass
            message["content"] = text
    return _strip_nul_deep(cleaned)


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
            lines.append(f"| {_md_cell(key)} | {_md_cell(value)} |")
    else:
        lines.append("（暂无偏好）")

    lines += ["", "## 历史行程", ""]
    trips = data.get("trip_history") or []
    if trips:
        lines.append("| ID | 出发地 | 目的地 | 出发日期 | 返程日期 | 目的 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for trip in trips:
            lines.append(
                f"| {_md_cell(trip.get('trip_id', ''))} | "
                f"{_md_cell(trip.get('origin') or '-')} | "
                f"{_md_cell(trip.get('destination') or '-')} | "
                f"{_md_cell(trip.get('start_date') or '-')} | "
                f"{_md_cell(trip.get('end_date') or '-')} | "
                f"{_md_cell(trip.get('purpose') or '-')} |"
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
            lines.append(
                "- 常去目的地: "
                + ", ".join(
                    f"{_md_cell(city)}({_md_cell(count)}次)"
                    for city, count in frequent.items()
                )
            )

    chats = data.get("chat_history") or []
    lines += ["", f"## 对话历史（{len(chats)} 条）", ""]
    if chats:
        for message in chats:
            is_user = message.get("role") == "user"
            role = "用户" if is_user else "助手"
            if is_user:
                content = strip_terminal_controls(
                    str(message.get("content", ""))
                )
                if len(content) > 200:
                    content = content[:200] + "…"
            else:
                # 助手消息存的是编排结果 JSON：与 Web / session-export 共用
                # render_stored_message 重新渲染（此前导出的是截断 200 字的
                # 原始 JSON，回答正文完全不可读）
                content = render_stored_message(message)
            # 正文放进引用块：消息里的多行标题 / 列表 / 分隔线 / 未闭合
            # 代码围栏不能改写整份导出文档的结构；sanitize_body 同时负责
            # 尖括号 / 链接转义与围栏平衡（与会话导出同一实现）
            lines.append(
                f"- **{role}**（{_md_cell(message.get('timestamp', ''))}）："
            )
            for body_line in sanitize_body(content).split("\n"):
                lines.append(f"  {body_line}" if body_line else "  >")
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

    # 备份与 Markdown 使用同一份清洗后的数据：嵌套非有限数 / NUL / 孤立
    # 代理项会让「导出成功」的备份在恢复时被整条拒绝，或让 write_text 抛
    # UnicodeEncodeError 留下半截文件
    cleaned = sanitize_backup_data(data)
    json_path = out / f"{stem}.json"
    md_path = out / f"{stem}.md"
    json_path.write_text(
        json.dumps(cleaned, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    md_path.write_text(render_markdown(cleaned), encoding="utf-8")
    return [json_path, md_path]
