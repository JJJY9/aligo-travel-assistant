"""行程约束求解（v2.0）：把必去项分配到天，保证每日时间窗不超限。

优先使用 OR-Tools CP-SAT（可选依赖；未安装时自动退回确定性贪心），
两种实现共享同一输出结构。plan-trip 用它在生成 prompt 里给出可行的
必去项分配建议，再由模型补全叙述；校验层仍独立复核最终行程。
"""

from __future__ import annotations

from typing import Any, Sequence

DEFAULT_DAY_START = 9 * 60
DEFAULT_DAY_END = 18 * 60
DEFAULT_VISIT_MIN = 120
DEFAULT_TRAVEL_MIN = 30


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _build_schedule(
    names: Sequence[str],
    assignments: list[list[str]],
    day_start: int,
    visit_min: int,
    travel_min: int,
    solver: str,
) -> dict[str, Any]:
    days: list[dict[str, Any]] = []
    for index, day_names in enumerate(assignments, 1):
        cursor = day_start
        items: list[dict[str, str]] = []
        for name in day_names:
            if items:
                cursor += travel_min
            start, end = cursor, cursor + visit_min
            items.append(
                {"name": name, "start": _hhmm(start), "end": _hhmm(end)}
            )
            cursor = end
        days.append({"day": index, "items": items})
    assigned = {name for day in assignments for name in day}
    unscheduled = [name for name in names if name not in assigned]
    return {"days": days, "unscheduled": unscheduled, "solver": solver}


def _greedy_assign(
    names: Sequence[str],
    days: int,
    day_start: int,
    day_end: int,
    visit_min: int,
    travel_min: int,
) -> list[list[str]]:
    assignments: list[list[str]] = [[] for _ in range(max(0, days))]
    day_index = 0
    cursor = day_start
    for name in names:
        while day_index < len(assignments):
            travel = travel_min if assignments[day_index] else 0
            end = cursor + travel + visit_min
            if end <= day_end:
                assignments[day_index].append(name)
                cursor = end
                break
            day_index += 1
            cursor = day_start
    return assignments


def _cp_sat_assign(
    names: Sequence[str],
    days: int,
    day_start: int,
    day_end: int,
    visit_min: int,
    travel_min: int,
) -> list[list[str]] | None:
    """OR-Tools CP-SAT 容量分配；不可用 / 不可行时返回 None。"""
    try:
        from ortools.sat.python import cp_model
    except Exception:  # noqa: BLE001 - 可选依赖
        return None
    if not names or days <= 0:
        return None
    model = cp_model.CpModel()
    slots = {
        (item, day): model.NewBoolVar(f"x_{item}_{day}")
        for item in range(len(names))
        for day in range(days)
    }
    for item in range(len(names)):
        model.Add(sum(slots[item, day] for day in range(days)) <= 1)
    capacity = max(0, day_end - day_start)
    per_item = visit_min + travel_min
    for day in range(days):
        # 当天第一项不需要通勤：k 项实际耗时 visit*k + travel*(k-1)，
        # 即 (visit+travel)*k <= capacity + travel；旧约束少算一次通勤，
        # 紧凑时间窗下会把可行项误判为未安排
        model.Add(
            sum(slots[item, day] * per_item for item in range(len(names)))
            <= capacity + travel_min
        )
    model.Maximize(sum(slots.values()))
    solver = cp_model.CpSolver()
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None
    assignments: list[list[str]] = [[] for _ in range(days)]
    for item, name in enumerate(names):
        for day in range(days):
            if solver.Value(slots[item, day]):
                assignments[day].append(name)
                break
    return assignments


def schedule_must_see(
    must_see: Sequence[str],
    days: int,
    day_start: int = DEFAULT_DAY_START,
    day_end: int = DEFAULT_DAY_END,
    visit_min: int = DEFAULT_VISIT_MIN,
    travel_min: int = DEFAULT_TRAVEL_MIN,
) -> dict[str, Any]:
    """把必去项分配到 ``days`` 天，返回每日时间建议与未能安排项。

    输出：``{"days": [{"day", "items": [{"name", "start", "end"}]}],
    "unscheduled": [...], "solver": "cp-sat"|"greedy"}``。
    """
    names: list[str] = []
    for name in must_see:
        text = str(name).strip()
        # 保序去重：重复项会重复占用每日容量挤掉其它项，而 _build_schedule
        # 用集合判定「已安排」时又会漏报真正的未安排项
        if text and text not in names:
            names.append(text)
    # 事件抽取给出的天数可能异常大：上限 366 天，避免为每天建列表 / CP-SAT
    # 变量导致内存与耗时失控
    days = max(0, min(366, int(days)))
    day_start = max(0, min(24 * 60, int(day_start)))
    day_end = max(day_start, min(24 * 60, int(day_end)))
    visit_min = max(1, int(visit_min))
    travel_min = max(0, int(travel_min))

    assignments = _cp_sat_assign(
        names, days, day_start, day_end, visit_min, travel_min
    )
    solver = "cp-sat"
    if assignments is None:
        assignments = _greedy_assign(
            names, days, day_start, day_end, visit_min, travel_min
        )
        solver = "greedy"
    return _build_schedule(
        names, assignments, day_start, visit_min, travel_min, solver
    )


def render_schedule(schedule: dict[str, Any]) -> str:
    """把求解结果渲染为给模型看的简短文本（无必去项时返回空串）。"""
    lines: list[str] = []
    for day in schedule.get("days") or []:
        items = day.get("items") or []
        if not items:
            continue
        rendered = "、".join(
            f"{item['name']}({item['start']}-{item['end']})" for item in items
        )
        lines.append(f"Day {day['day']}: {rendered}")
    unscheduled = schedule.get("unscheduled") or []
    if unscheduled:
        lines.append("时间窗不足以安排的必去项：" + "、".join(unscheduled))
    return "\n".join(lines)
