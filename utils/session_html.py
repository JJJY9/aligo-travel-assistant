"""单个会话的独立 HTML 导出（CLI `session-export --html`）。

与 Web 面板「导出 HTML」同一用途：单文件、内嵌样式，适合分享 / 打印 /
离线查看。CLI 没有浏览器 DOM，这里把消息渲染为转义后的纯文本气泡
（用户消息保留换行；助手消息先经 render_stored_message 还原正文）。
"""
from datetime import datetime
from typing import Any

from utils.stored_message import render_stored_message

_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ --ink:#14262e; --ink-3:#7b8d93; --line:#e2e9e8; --accent:#d9552c;
    --bg:#f2f5f4; --surface:#ffffff; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.65 "PingFang SC", "HarmonyOS Sans SC", "Microsoft YaHei", system-ui, sans-serif; }}
  .page {{ max-width: 820px; margin: 24px auto; padding: 32px 36px; background: var(--surface);
    border-radius: 16px; box-shadow: 0 12px 40px rgba(15, 36, 42, 0.08); }}
  h1 {{ margin: 0 0 4px; font-size: 22px; }}
  .meta {{ margin-bottom: 20px; color: var(--ink-3); font-size: 12.5px; }}
  .msg {{ margin: 18px 0; }}
  .msg.user {{ display: flex; justify-content: flex-end; }}
  .msg.user .bubble {{ max-width: 85%; padding: 10px 14px; border-radius: 14px 14px 4px 14px;
    background: #162c34; color: #f3f7f6; white-space: pre-wrap; word-break: break-word; }}
  .msg.assistant .bubble {{ padding: 12px 16px; border: 1px solid var(--line);
    border-radius: 14px 14px 14px 4px; background: var(--surface);
    white-space: pre-wrap; word-break: break-word; }}
  .role {{ font-size: 12px; color: var(--ink-3); margin-bottom: 4px; }}
  .footer {{ margin-top: 24px; padding-top: 12px; border-top: 1px solid var(--line);
    color: var(--ink-3); font-size: 12px; }}
  @media print {{
    body {{ background: #fff; color: #111; }}
    .page {{ margin: 0; padding: 0; box-shadow: none; }}
    .msg.user .bubble {{ background: #f0f0f0 !important; color: #111 !important; }}
    .msg.assistant .bubble {{ border-color: #ddd; }}
    .msg, .bubble {{ break-inside: avoid; }}
  }}
</style>
</head>
<body>
<div class="page">
<h1>{title}</h1>
<div class="meta">用户 {user_id} · 会话 {session_id} · 导出于 {exported_at}</div>
{body}
<div class="footer">由 Aligo 智能旅行助手生成 · 回答由 AI 生成，请核对关键信息</div>
</div>
</body>
</html>
"""


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def message_text(message: dict[str, Any]) -> str:
    """与 Markdown 单会话导出一致的正文：用户原文 / 助手渲染正文。"""
    if message.get("role") == "user":
        return str(message.get("content", ""))
    return render_stored_message(message)


def render_session_html(
    user_id: str, session_id: str, title: str, messages: list[dict[str, Any]]
) -> str:
    """渲染单会话独立 HTML（所有动态文本转义，结构不会被内容注入）。"""
    blocks: list[str] = []
    for message in messages:
        is_user = message.get("role") == "user"
        role = "用户" if is_user else "Aligo"
        text = message_text(message).strip()
        blocks.append(
            f'<div class="msg {"user" if is_user else "assistant"}">'
            f'<div class="role">{role}</div>'
            f'<div class="bubble">{_escape(text)}</div></div>'
        )
    return _TEMPLATE.format(
        title=_escape(title),
        user_id=_escape(user_id),
        session_id=_escape(session_id),
        exported_at=_escape(datetime.now().isoformat(timespec="seconds")),
        body="\n".join(blocks),
    )
