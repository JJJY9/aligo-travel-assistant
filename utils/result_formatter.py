"""
编排结果 → Markdown 格式化

OrchestrationAgent 返回的 results 列表包含各子智能体的结构化输出；这里把它们
渲染成用户可读的 Markdown（行程、偏好更新、问答、错误等），供 Web 界面等
文本出口使用。
"""
from typing import Any

import logging

logger = logging.getLogger(__name__)

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


def preference_label(pref_type: str) -> str:
    """偏好类型的中文显示名（未知类型原样返回）。"""
    return _PREF_NAMES.get(pref_type, pref_type)


def _pref_value_text(value: Any) -> str:
    """偏好 / 事项字段的可读文本：列表逐项连接，不再输出 Python repr。"""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "、".join(
            text for text in (_pref_value_text(item) for item in value) if text
        )
    if isinstance(value, dict):
        rendered = []
        for key, item in value.items():
            text = _pref_value_text(item)
            if text:
                rendered.append(f"{key}：{text}")
        return "、".join(rendered)
    return str(value)


def result_to_markdown(result_data: dict[str, Any]) -> str:
    """把编排结果转换为 Markdown 文本。"""
    results = result_data.get("results") or []
    if not results:
        if result_data.get("status") == "no_agents":
            return "已记录您的需求。可以继续补充出发地、目的地、时间等信息，我来帮您规划行程。"
        return "未能获取有效结果，请重新描述您的需求。"

    blocks: list[str] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        agent_name = item.get("agent_name", "")
        status = item.get("status")
        data = item.get("data")
        if not isinstance(data, dict):
            # 智能体可能返回字符串 / 列表 / null：包一层，渲染器不再崩溃
            data = {"result": data}
        if status == "error":
            error = data.get("error") or data.get("message") or "未知错误"
            blocks.append(f"> ⚠️ **{display_name(agent_name)}** 执行失败：{error}")
            continue
        try:
            rendered = _render_agent(agent_name, data)
        except Exception:  # noqa: BLE001 - 渲染失败不能让整轮回答 500
            logger.warning("渲染智能体结果失败: %s", agent_name, exc_info=True)
            rendered = _fallback_text(data)
        if rendered:
            blocks.append(rendered)

    return "\n\n".join(blocks) if blocks else "已处理您的请求。"


def _fallback_text(data: Any) -> str:
    """渲染器无法处理该结构时的兜底：尽量取出可读文本。"""
    if isinstance(data, str):
        return data.strip()
    if isinstance(data, dict):
        for key in ("answer", "content", "message", "summary", "text", "output"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return ""


def _render_itinerary(itinerary: Any) -> str:
    if isinstance(itinerary, str):
        return f"## ✈️ 行程规划\n\n{itinerary}"
    if not isinstance(itinerary, dict):
        return f"## ✈️ 行程规划\n\n{itinerary}"
    lines = [f"## ✈️ {itinerary.get('title') or '行程规划'}"]
    if itinerary.get("duration"):
        lines.append(f"**时长**：{itinerary['duration']}")

    daily_plans = itinerary.get("daily_plans")
    if isinstance(daily_plans, dict):
        daily_plans = list(daily_plans.values())
    if not isinstance(daily_plans, list):
        daily_plans = []
    for day_plan in daily_plans:
        lines.append("")
        if not isinstance(day_plan, dict):
            lines.append(f"### {day_plan}")
            continue
        day = day_plan.get("day")
        lines.append(f"### 第 {'?' if day is None else day} 天")
        activities = day_plan.get("activities") or day_plan.get("time_slots") or []
        if isinstance(activities, dict):
            activities = list(activities.values())
        if not isinstance(activities, list):
            activities = []
        for slot in activities:
            if not isinstance(slot, dict):
                lines.append(f"- {slot}")
                continue
            time_text = slot.get("time") or ""
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
        if not isinstance(meals, dict):
            meals = {}
        if meals.get("lunch"):
            lines.append(f"- 🍜 午餐：{meals['lunch']}")
        if meals.get("dinner"):
            lines.append(f"- 🍽️ 晚餐：{meals['dinner']}")

    notes = itinerary.get("notes") or []
    if isinstance(notes, str):
        notes = [notes]
    if not isinstance(notes, list):
        notes = []
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
        legacy_preferences: dict[str, Any] | None = None
        if isinstance(preferences, dict):
            if isinstance(preferences.get("preferences"), list):
                preferences = preferences["preferences"]
            else:
                # 旧格式：{type: value} 的偏好字典（历史模型输出）。此前按
                # 嵌套列表解包，dict 没有 "preferences" 键时渲染成空
                legacy_preferences = preferences
                preferences = None
        if legacy_preferences:
            lines = ["### ✅ 已更新偏好"]
            for pref_type, value in legacy_preferences.items():
                if pref_type in {"has_preferences", "error"} or value is None:
                    continue
                name = preference_label(pref_type)
                lines.append(f"- {name} **设置为** {_pref_value_text(value)}")
            return "\n".join(lines) if len(lines) > 1 else ""
        if isinstance(preferences, list) and preferences:
            lines = ["### ✅ 已更新偏好"]
            for pref in preferences:
                if not isinstance(pref, dict):
                    continue
                pref_type = pref.get("type", "")
                value = pref.get("value", "")
                # 与落库路径一致的 action 变体（Append / add / 追加）
                action_raw = str(pref.get("action") or "replace").strip().lower()
                action = "追加" if action_raw in {"append", "add", "追加"} else "设置为"
                name = preference_label(pref_type)
                text_value = _pref_value_text(value)
                if not pref_type or not text_value:
                    # 与落库路径一致：null / 空值不会写入偏好，回显同样跳过
                    # （否则出现「已更新偏好 设置为 None」但实际未保存）
                    continue
                lines.append(f"- {name} **{action}** {text_value}")
            return "\n".join(lines) if len(lines) > 1 else ""
        return ""

    if agent_name == "event_collection":
        fields = [
            ("origin", "出发地"),
            ("destination", "目的地"),
            ("start_date", "出发日期"),
            ("end_date", "返程日期"),
            ("trip_purpose", "出行目的"),
        ]
        raw_nested = data.get("data")
        nested: dict[Any, Any] = raw_nested if isinstance(raw_nested, dict) else {}
        parts = []
        for key, label in fields:
            value = data.get(key) or nested.get(key)
            if value:
                parts.append(f"- {label}：{_pref_value_text(value)}")
        missing = data.get("missing_info") or []
        # 模型可能返回单个字符串：直接迭代会被拆成单字
        if isinstance(missing, str):
            missing = [missing]
        if not isinstance(missing, list):
            missing = []
        if parts:
            text = "### ✅ 已收集行程信息\n" + "\n".join(parts)
            if missing:
                text += "\n\n💡 还需要补充：" + "、".join(
                    str(item) for item in missing
                )
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
        if not isinstance(documents, list):
            documents = []
        doc_names: list[str] = []
        for document in documents:
            if not isinstance(document, dict):
                continue
            metadata = document.get("metadata") or {}
            doc_name = metadata.get("parent_doc") or metadata.get("title")
            if doc_name and doc_name not in doc_names:
                doc_names.append(str(doc_name))
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
    for key in (
        "answer",
        "content",
        "result",
        "message",
        "summary",
        "text",
        "description",
        "output",
    ):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""
