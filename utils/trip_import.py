"""行程 CSV 导入（`trips-export` 的逆操作）。

解析 trips-export 导出的 CSV（UTF-8 BOM，中文或英文字段名），逐行校验
日期与必填字段；非法行收集为错误而不是中断整个文件，重复行程沿用
存储层的去重语义（同路线 + 同起止日期更新目的）。
"""
import csv
import io
from datetime import datetime
from typing import Any

_HEADER_ALIASES = {
    "origin": {"出发地", "origin"},
    "destination": {"目的地", "destination"},
    "start_date": {"出发日期", "start_date"},
    "end_date": {"返程日期", "end_date"},
    "purpose": {"目的", "purpose"},
}


def _normalize_header(cell: str) -> str:
    return str(cell or "").strip().lstrip("﻿")


def _strict_date(value: str) -> "datetime | None":
    """严格 YYYY-MM-DD：不补零写法（2026-1-1）视为非法，避免绕过去重。"""
    try:
        parsed = datetime.strptime(str(value), "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    if parsed.strftime("%Y-%m-%d") != str(value):
        return None
    return parsed


def parse_trips_csv(text: str) -> tuple[list[dict[str, Any]], list[tuple[int, str]]]:
    """解析并校验行程 CSV，返回 ``(有效行程, [(行号, 原因)])``（行号从 2 起）。"""
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return [], [(1, "文件为空")]

    mapping: dict[str, int] = {}
    for index, header_cell in enumerate(header):
        name = _normalize_header(header_cell)
        for key, aliases in _HEADER_ALIASES.items():
            if name in aliases and key not in mapping:
                mapping[key] = index
                break
    missing = {"destination", "start_date"} - set(mapping)
    if missing:
        return [], [(1, f"缺少必需列：{'、'.join(sorted(missing))}")]

    def cell(row: list[str], key: str) -> str:
        index = mapping.get(key)
        if index is None or index >= len(row):
            return ""
        return str(row[index] or "").strip()

    rows: list[dict[str, Any]] = []
    errors: list[tuple[int, str]] = []
    for line_no, row in enumerate(reader, start=2):
        if not row or all(not str(item or "").strip() for item in row):
            continue
        destination = cell(row, "destination")
        start_date = cell(row, "start_date")
        end_date = cell(row, "end_date")
        if not destination:
            errors.append((line_no, "目的地为空"))
            continue
        start_dt = _strict_date(start_date)
        if start_dt is None:
            errors.append((line_no, f"出发日期格式应为 YYYY-MM-DD：{start_date}"))
            continue
        if end_date:
            end_dt = _strict_date(end_date)
            if end_dt is None:
                errors.append((line_no, f"返程日期格式应为 YYYY-MM-DD：{end_date}"))
                continue
            if end_dt < start_dt:
                errors.append((line_no, "结束日期不能早于出发日期"))
                continue
        rows.append(
            {
                "origin": cell(row, "origin"),
                "destination": destination,
                "start_date": start_date,
                "end_date": end_date,
                "purpose": cell(row, "purpose") or "出差",
            }
        )
    return rows, errors
