"""
记忆数据导出（JSON / Markdown）

`export` 命令的实现：把 MemoryManager.export_data() 的结果写入 JSON 与 Markdown
两份文件，便于用户备份、迁移或查看自己的数据。
"""
import json
import os
import re
from pathlib import Path
from typing import Any

from utils.atomic_write import write_bytes_atomic, write_text_atomic
from utils.console import strip_terminal_controls
from utils.json_safe import sanitize_json
from utils.session_markdown import sanitize_body
from utils.stored_message import render_stored_message

_CELL_BREAK = re.compile(r"[\r\n]+")


def _md_cell(value: Any) -> str:
    """导出表格 / 列表用的单行文本：竖线与换行会破坏 Markdown 结构，
    尖括号转义避免把用户 / 模型可控内容当 HTML 落盘；终端控制序列
    （ESC / OSC）一并剥离。右方括号同样转义：``[x](url)`` 的 ``]`` 不能
    闭合就无法形成链接 / 图片语法，导出文件在预览器里不会被点成钓鱼链接。"""
    text = strip_terminal_controls(
        str(value if value is not None else "-")
    )
    text = _CELL_BREAK.sub(" ", text)
    # 反斜杠必须先转义：值里的 `\` 与随后的 `|` 会被 Markdown 当成
    # 「转义后的竖线」，单元格行仍按 `|` 切列而错位
    text = text.replace("\\", "\\\\").replace("|", "\\|")
    text = text.replace("<", "&lt;").replace(">", "&gt;")
    return text.replace("]", "\\]")


def sanitize_backup_data(data: dict[str, Any]) -> dict[str, Any]:
    """备份数据清洗（JSON 与 Markdown 导出的统一入口）。

    ``sanitize_json`` 只处理 Python 值层：助手消息的 ``content`` 是一段
    不透明 JSON 文本，其中的 NaN / Infinity 不会被顶层清洗，导入端却会
    按「消息元数据含非有限数」整条拒绝；NUL 同理。这里额外解析助手 JSON
    文本、递归清洗后用规范 JSON 写回，并移除所有字符串里的 NUL。
    """
    # 非 dict 数据保持原口径（不清 NUL）；dict 在外层一次遍历里同时完成
    # 代理项 / 非有限数归一与 NUL 剔除，不再整树二次深拷贝
    if not isinstance(data, dict):
        return sanitize_json(data)
    cleaned = sanitize_json(data, strip_nuls=True)
    history = cleaned.get("chat_history")
    if isinstance(history, list):
        for message in history:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, str):
                continue
            text = content
            if message.get("role") != "user" and text.lstrip().startswith(("{", "[")):
                try:
                    parsed = json.loads(text)
                except (ValueError, RecursionError):
                    parsed = None
                if isinstance(parsed, (dict, list)):
                    try:
                        # 助手 JSON 内部的 NUL（含 ``\u0000`` 转义形式与
                        # 字典键）在导入端会被整条拒绝：解析后与代理项、
                        # 非有限数在同一遍清洗里剔除
                        text = json.dumps(
                            sanitize_json(parsed, strip_nuls=True),
                            ensure_ascii=False,
                        )
                    except (TypeError, ValueError):
                        pass
            message["content"] = text
    return cleaned


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


def safe_export_component(value: str, fallback: str = "user") -> str:
    """导出文件名里的单个标识分量：清洗非法字符，撞名风险时追加短哈希。

    大小写不敏感文件系统上 ``Alice`` / ``alice`` 是不同账号却解析到同一
    目录项；清洗后追加的 ``_<hex>`` 后缀与合法 ASCII ID 空间也重叠
    （如 张三 → ___ced07f，用户 ___ced07f 原样保留）。出现任一风险时
    统一追加输入的短哈希，展开后按大小写折叠仍唯一。
    """
    import hashlib
    import re

    original = str(value or "")
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", original)[:24] or fallback
    risky = (
        safe != original
        # 大小写不敏感文件系统：Alice / alice 会撞到同一文件
        or safe != safe.lower()
        # 与「清洗后名 + _<6 hex>」的生成空间重叠
        or bool(re.search(r"_[0-9a-f]{6}$", safe))
    )
    if risky:
        digest = hashlib.sha1(original.encode("utf-8")).hexdigest()[:6]
        safe = f"{safe}_{digest}"
    return safe


def safe_export_user(user_id: str) -> str:
    """导出文件名里的用户 ID（与 Web 端 ``_safe_filename`` 同一实现）。"""
    return safe_export_component(user_id, "user")


def _export_stem(data: dict[str, Any]) -> str:
    """导出文件名主干：用户清洗名 + 导出时间（去分隔符）。

    时间只提供毫秒级区分度；同名并发导出由 :func:`_claim_export_paths`
    原子占名追加 ``-1`` / ``-2`` 后缀，不依赖时钟精度。
    """
    timestamp = (
        str(data.get("exported_at", ""))
        .replace(":", "")
        .replace("-", "")[:21]
    )
    user = safe_export_user(str(data.get("user_id", "user")) or "user")
    return f"{user}_{timestamp or 'export'}"


def _unlink_quiet(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def claim_export_path(path: str | Path) -> bool:
    """原子占名单个导出路径（``O_CREAT|O_EXCL`` 建空占位文件）。

    已存在返回 False，调用方按「文件已存在」拒绝；成功返回 True，随后用
    原子写覆盖占位文件。CLI 的 ``exists()`` 检查 + ``os.replace`` 是
    TOCTOU：并发导出都可能在文件尚不存在时通过检查，后者静默覆盖先写者
    的完整快照。占名与 :func:`_claim_export_paths` 同一口径。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        os.close(fd)
    except BaseException:
        # 占位文件已建立但 close 失败（EIO/ENOSPC 等）：自清后再上抛，
        # 避免在正式导出名上留下 0 字节文件
        _unlink_quiet(target)
        raise
    return True


def _claim_export_paths(out: Path, stem: str) -> tuple[Path, Path]:
    """原子占用一对尚未存在的 ``(json, md)`` 路径。

    时钟导出的 stem 只有约 1ms 分辨率：并发备份 / cron 重入会撞名，
    而 ``os.replace`` 会静默覆盖先写者的快照。用 ``O_CREAT | O_EXCL``
    建立空占位文件占住名字，任一文件已存在就试 ``<stem>-1``、
    ``<stem>-2``…；占名成功后原子写覆盖占位文件（写失败由调用方清理）。
    """
    index = 0
    while True:
        name = stem if index == 0 else f"{stem}-{index}"
        json_path = out / f"{name}.json"
        md_path = out / f"{name}.md"
        try:
            fd = os.open(json_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            index += 1
            continue
        try:
            os.close(fd)
        except BaseException:
            _unlink_quiet(json_path)
            raise
        try:
            fd = os.open(md_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            _unlink_quiet(json_path)
            index += 1
            continue
        except BaseException:
            # 非撞名失败（ENOSPC/EDQUOT、同名目录的 PermissionError 等）：
            # 调用方的 try 在占名之后，这里必须自清已占的 json，避免在
            # 正式备份名下留下 0 字节文件
            _unlink_quiet(json_path)
            raise
        try:
            os.close(fd)
        except BaseException:
            _unlink_quiet(json_path)
            _unlink_quiet(md_path)
            raise
        return json_path, md_path


def write_export_files(
    cleaned: dict[str, Any],
    json_bytes: bytes,
    out_dir: Path | str,
    stem: str | None = None,
) -> list[Path]:
    """写出**已清洗**数据对应的 JSON / Markdown 两个文件，返回路径列表。

    调用方已经 ``sanitize_backup_data`` 并按导出格式序列化过 JSON（上限
    预检用的就是同一串字节）时走这里，避免同一份备份再清洗一次 + 再
    ``indent=2`` 序列化一次；普通调用方用 :func:`export_files` 包装。
    ``json_bytes`` 的字节必须等于 ``json.dumps(cleaned, ensure_ascii=False,
    indent=2).encode("utf-8")``（LF，无 BOM）。

    ``stem`` 为空（时钟命名）时采用原子占名：同一毫秒内的并发 / 重试
    导出各自得到 ``-1`` / ``-2`` 后缀的独立文件，不再静默互相覆盖。
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if stem is None:
        json_path, md_path = _claim_export_paths(out, _export_stem(cleaned))
        try:
            write_bytes_atomic(json_path, json_bytes)
            write_text_atomic(md_path, render_markdown(cleaned), newline="\n")
        except BaseException:
            for leftover in (json_path, md_path):
                try:
                    os.unlink(leftover)
                except OSError:
                    pass
            raise
        return [json_path, md_path]
    json_path = out / f"{stem}.json"
    md_path = out / f"{stem}.md"
    write_bytes_atomic(json_path, json_bytes)
    write_text_atomic(md_path, render_markdown(cleaned), newline="\n")
    return [json_path, md_path]


def export_files(
    data: dict[str, Any],
    out_dir: Path | str,
    stem: str | None = None,
) -> list[Path]:
    """把导出数据写入 `<out_dir>/<stem>.json` 与 `<stem>.md`，返回文件路径列表。"""
    # 备份与 Markdown 使用同一份清洗后的数据：嵌套非有限数 / NUL / 孤立
    # 代理项会让「导出成功」的备份在恢复时被整条拒绝，或让 write_text 抛
    # UnicodeEncodeError 留下半截文件
    cleaned = sanitize_backup_data(data)
    json_bytes = json.dumps(
        cleaned, ensure_ascii=False, indent=2
    ).encode("utf-8")
    return write_export_files(cleaned, json_bytes, out_dir, stem)
