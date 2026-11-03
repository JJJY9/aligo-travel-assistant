"""
编排结果 → Markdown 格式化

OrchestrationAgent 返回的 results 列表包含各子智能体的结构化输出；这里把它们
渲染成用户可读的 Markdown（行程、偏好更新、问答、错误等），供 Web 界面等
文本出口使用。
"""
from typing import Any

_DISPLAY_NAMES = {
    "event_collection": "事项收集",
    "preference": "偏好管理",
    "itinerary_planning": "行程规划",
    "information_query": "信息查询",
    "rag_knowledge": "知识库查询",
    "memory_query": "记忆查询",
}

_PREF_NAMES = {
    "home_location": "常驻地",
    "transportation_preference": "交通偏好",
    "hotel_brands": "酒店偏好",
    "airlines": "航空公司偏好",
    "seat_preference": "座位偏好",
    "meal_preference": "餐食偏好",
    "budget_level": "预算等级",
}


def display_name(agent_name: str) -> str:
    """智能体的中文显示名。"""
    return _DISPLAY_NAMES.get(agent_name, agent_name)


def result_to_markdown(result_data: dict[str, Any]) -> str:
    """把编排结果转换为 Markdown 文本。"""
    results = result_data.get("results") or []
    if not results:
        if result_data.get("status") == "no_agents":
            return "已记录您的需求。可以继续补充出发地、目的地、时间等信息，我来帮您规划行程。"
        return "未能获取有效结果，请重新描述您的需求。"

    blocks: list[str] = []
    for item in results:
        agent_name = item.get("agent_name", "")
        status = item.get("status")
        data = item.get("data") or {}
        if status == "error":
            error = data.get("error") or data.get("message") or "未知错误"
            blocks.append(f"> ⚠️ **{display_name(agent_name)}** 执行失败：{error}")
            continue
        rendered = _render_agent(agent_name, data)
        if rendered:
            blocks.append(rendered)

    return "\n\n".join(blocks) if blocks else "已处理您的请求。"


def _render_itinerary(itinerary: dict[str, Any]) -> str:
    lines = [f"## ✈️ {itinerary.get('title', '行程规划')}"]
    if itinerary.get("duration"):
        lines.append(f"**时长**：{itinerary['duration']}")

    for day_plan in itinerary.get("daily_plans", []):
        lines.append("")
        lines.append(f"### 第 {day_plan.get('day', '?')} 天")
        activities = day_plan.get("activities") or day_plan.get("time_slots") or []
        for slot in activities:
            time_text = slot.get("time", "")
            activity = slot.get("activity") or slot.get("location") or ""
            description = slot.get("description", "")
            transport = slot.get("transport", "")
            line = f"- **{time_text}** {activity}".strip()
            if description:
                line += f"：{description}"
            if transport:
                line += f"（🚇 {transport}）"
            lines.append(line)
        meals = day_plan.get("meals") or {}
        if meals.get("lunch"):
            lines.append(f"- 🍜 午餐：{meals['lunch']}")
        if meals.get("dinner"):
            lines.append(f"- 🍽️ 晚餐：{meals['dinner']}")

    notes = itinerary.get("notes") or []
    if notes:
        lines.append("")
        lines.append("### 📌 注意事项")
        for note in notes:
            lines.append(f"- {note}")
    return "\n".join(lines)


def _render_agent(agent_name: str, data: dict[str, Any]) -> str:
    if agent_name == "itinerary_planning":
        itinerary = data.get("itinerary") or (data.get("data") or {}).get("itinerary")
        if itinerary:
            return _render_itinerary(itinerary)
        if data.get("error"):
            return f"> ⚠️ 行程规划：{data['error']}"
        return ""

    if agent_name == "preference":
        preferences = data.get("preferences")
        if isinstance(preferences, dict):
            preferences = preferences.get("preferences", [])
        if isinstance(preferences, list) and preferences:
            lines = ["### ✅ 已更新偏好"]
            for pref in preferences:
                if not isinstance(pref, dict):
                    continue
                pref_type = pref.get("type", "")
                value = pref.get("value", "")
                action = "追加" if pref.get("action") == "append" else "设置为"
                name = _PREF_NAMES.get(pref_type, pref_type)
                lines.append(f"- {name} **{action}** {value}")
            return "\n".join(lines)
        return ""

    if agent_name == "event_collection":
        fields = [
            ("origin", "出发地"),
            ("destination", "目的地"),
            ("start_date", "出发日期"),
            ("end_date", "返程日期"),
            ("trip_purpose", "出行目的"),
        ]
        parts = []
        for key, label in fields:
            value = data.get(key) or (data.get("data") or {}).get(key)
            if value:
                parts.append(f"- {label}：{value}")
        missing = data.get("missing_info") or []
        if parts:
            text = "### ✅ 已收集行程信息\n" + "\n".join(parts)
            if missing:
                text += "\n\n💡 还需要补充：" + "、".join(str(item) for item in missing)
            return text
        if missing:
            return "💡 还需要补充：" + "、".join(str(item) for item in missing)
        return ""

    if agent_name == "information_query":
        results = data.get("results")
        if not isinstance(results, dict):
            results = data
        summary = results.get("summary") or results.get("message") or results.get("error")
        if not summary:
            return ""
        text = str(summary)
        sources = results.get("sources") or []
        if sources:
            lines = [text, "", "**参考来源**"]
            for index, source in enumerate(sources[:3], 1):
                url = source.get("url", "") if isinstance(source, dict) else str(source)
                lines.append(f"{index}. {url}")
            text = "\n".join(lines)
        return text

    if agent_name == "rag_knowledge":
        answer = data.get("answer") or data.get("content")
        if not answer:
            return ""
        text = str(answer)
        documents = data.get("retrieved_documents") or []
        doc_names: list[str] = []
        for document in documents:
            if not isinstance(document, dict):
                continue
            metadata = document.get("metadata") or {}
            name = metadata.get("parent_doc") or metadata.get("title")
            if name and name not in doc_names:
                doc_names.append(str(name))
        if doc_names:
            text += "\n\n**参考来源**：" + "、".join(doc_names[:3])

        snippets: list[str] = []
        for document in documents[:3]:
            if not isinstance(document, dict):
                continue
            excerpt = " ".join(str(document.get("content") or "").split())
            if not excerpt:
                continue
            if len(excerpt) > 140:
                excerpt = excerpt[:140] + "…"
            snippets.append(excerpt)
        if snippets:
            text += "\n\n**参考片段**"
            for snippet in snippets:
                text += f"\n\n> {snippet}"
        return text

    if agent_name == "memory_query":
        answer = data.get("answer") or data.get("result") or data.get("content")
        return str(answer) if answer else ""

    # 通用兜底：按常见字段名取文本
    for key in ("answer", "content", "result", "message", "summary", "text", "description"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""
