"""
行程 CSV 渲染（Web API 与 CLI 导出共用）

- 输出 UTF-8 BOM，便于 Excel 直接打开中文表头；
- 对以 = + - @ 等开头的内容加前导单引号，防止 CSV 公式注入。
"""
import csv
import io
from typing import Any


def _csv_safe(value: object) -> str:
    text = str(value or "")
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


def render_trips_csv(trips: list[dict[str, Any]]) -> str:
    """把行程列表渲染为带 BOM 的 CSV 文本（含表头）。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["出发地", "目的地", "出发日期", "返程日期", "目的"])
    for trip in trips:
        writer.writerow(
            [
                _csv_safe(trip.get("origin")),
                _csv_safe(trip.get("destination")),
                trip.get("start_date") or "",
                trip.get("end_date") or "",
                _csv_safe(trip.get("purpose")),
            ]
        )
    return "\ufeff" + buffer.getvalue()
