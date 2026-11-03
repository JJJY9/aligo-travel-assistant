"""
记忆数据导出测试（无需 API Key、不访问网络）

覆盖：
1. MemoryManager.export_data() 汇总偏好 / 行程 / 对话历史 / 统计；
2. render_markdown() 的表格与列表渲染；
3. export_files() 写出 JSON + Markdown，JSON 可完整回读。

运行：python tests/test_export.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from context.memory_manager import MemoryManager
from utils.exporter import export_files, render_markdown

SAMPLE = {
    "user_id": "test_user",
    "session_id": "abc123",
    "exported_at": "2026-11-03T14:30:00.123456",
    "preferences": {"hotel_brands": ["汉庭", "如家"], "home_location": "北京"},
    "trip_history": [
        {
            "trip_id": "trip_1",
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-11-09",
            "end_date": "2026-11-11",
            "purpose": "出差",
        }
    ],
    "chat_history": [
        {"role": "user", "content": "帮我规划行程", "timestamp": "2026-11-03T14:00:00"},
        {"role": "assistant", "content": "好的", "timestamp": "2026-11-03T14:00:05"},
    ],
    "statistics": {
        "total_trips": 1,
        "total_messages": 2,
        "total_queries": 0,
        "frequent_destinations": {"上海": 1},
    },
}


def test_memory_manager_export_data():
    memory = MemoryManager("export_test", "sess", storage_path=tempfile.mkdtemp())
    memory.long_term.save_preference("hotel_brands", "汉庭")
    data = memory.export_data()
    assert data["user_id"] == "export_test"
    assert data["preferences"]["hotel_brands"] == "汉庭"
    assert "chat_history" in data
    assert "statistics" in data
    print("✓ MemoryManager.export_data 正常")


def test_render_markdown():
    md = render_markdown(SAMPLE)
    assert "# 差旅助手用户数据导出" in md
    assert "汉庭, 如家" in md
    assert "上海" in md and "出差" in md
    assert "## 对话历史（2 条）" in md
    assert "上海(1次)" in md
    print("✓ Markdown 渲染正常")


def test_export_files():
    out = Path(tempfile.mkdtemp())
    paths = export_files(SAMPLE, out)
    assert len(paths) == 2
    assert all(path.exists() for path in paths)

    json_path = next(path for path in paths if path.suffix == ".json")
    loaded = json.loads(json_path.read_text(encoding="utf-8"))
    assert loaded == SAMPLE
    assert json_path.stem.startswith("test_user_")

    md_path = next(path for path in paths if path.suffix == ".md")
    assert "用户偏好" in md_path.read_text(encoding="utf-8")
    print("✓ 导出文件写入与 JSON 回读正常")


if __name__ == "__main__":
    test_memory_manager_export_data()
    test_render_markdown()
    test_export_files()
    print("\n全部通过")
