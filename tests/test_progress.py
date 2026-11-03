"""
CLI 进度事件格式化测试（无需 API Key）

运行：python tests/test_progress.py
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.progress import format_progress_event


def test_agent_events():
    assert (
        format_progress_event({"type": "agent_start", "agent_name": "preference"})
        == "正在执行 preference…"
    )
    assert (
        format_progress_event(
            {"type": "agent_start", "agent_name": "preference"},
            lambda name: "偏好管理",
        )
        == "正在执行 偏好管理…"
    )
    assert (
        format_progress_event(
            {"type": "agent_done", "agent_name": "preference", "status": "success", "duration": 1.234}
        )
        == "✓ preference 1.2s"
    )
    assert (
        format_progress_event({"type": "agent_done", "agent_name": "x", "status": "error"})
        == "✗ x"
    )
    print("✓ 智能体开始 / 完成事件")


def test_phase_and_plan_events():
    assert format_progress_event({"type": "phase", "label": "理解你的需求"}) == "理解你的需求"
    assert format_progress_event({"type": "plan", "schedule": [{}, {}]}) == "已规划 2 个智能体"
    assert format_progress_event({"type": "plan", "schedule": []}) == "无需调度智能体"
    assert format_progress_event({"type": "agent_start"}) == "正在执行智能体…"
    assert format_progress_event({"type": "unknown"}) is None
    print("✓ 阶段 / 计划 / 未知事件")


if __name__ == "__main__":
    test_agent_events()
    test_phase_and_plan_events()
    print("\n全部通过")
