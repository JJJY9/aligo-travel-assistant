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


def test_export_files_sanitizes_surrogates_in_markdown():
    """孤立代理项：JSON 与 Markdown 都能写出（不再 JSON 成功、命令却报错）。"""
    data = {
        "user_id": "u-export",
        "exported_at": "2026-01-01T00:00:00",
        "preferences": {},
        "chat_history": [
            {
                "role": "user",
                "content": "坏" + chr(0xD800) + "字",
                "timestamp": "2026-01-01T00:00:00",
            }
        ],
        "trip_history": [],
        "statistics": {},
    }
    out = Path(tempfile.mkdtemp())
    paths = export_files(data, out)
    assert len(paths) == 2
    json_text = paths[0].read_text(encoding="utf-8")
    md_text = paths[1].read_text(encoding="utf-8")
    assert "\\ud800" in json_text
    assert "\\ud800" in md_text
    print("✓ export_files 清洗孤立代理项（JSON 与 Markdown）")


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
def test_render_markdown_renders_assistant_orchestration_json():
    """助手消息的编排 JSON 要重新渲染为可读 Markdown，而不是截断的原始 JSON。"""
    from utils.exporter import render_markdown

    assistant = {
        "role": "assistant",
        "timestamp": "2026-11-03T15:13:43",
        "content": json.dumps(
            {
                "status": "completed",
                "results": [
                    {
                        "agent_name": "itinerary_planning",
                        "priority": 1,
                        "status": "success",
                        "data": {
                            "itinerary": {
                                "title": "北京三日游",
                                "duration": "3天",
                                "daily_plans": [],
                            }
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
    }
    md = render_markdown(
        {
            "user_id": "u1",
            "session_id": "s1",
            "exported_at": "2026-11-03",
            "chat_history": [
                {"role": "user", "timestamp": "t0", "content": "去北京"},
                assistant,
            ],
        }
    )
    assert "北京三日游" in md
    assert '"status": "completed"' not in md, "不能导出原始编排 JSON"
    print("✓ user-export 渲染助手编排 JSON")


def test_render_markdown_escapes_table_cells():
    """表格与列表里的竖线 / 换行 / 尖括号不能破坏导出结构。"""
    from utils.exporter import render_markdown

    md = render_markdown(
        {
            "user_id": "u1",
            "session_id": "s1",
            "exported_at": "2026-11-03",
            "preferences": {"hotel_brands": "汉庭|如家\n<script>"},
            "trip_history": [
                {
                    "trip_id": "trip_1",
                    "origin": "北京",
                    "destination": "上海|杭州",
                    "start_date": "2026-12-01",
                    "end_date": "2026-12-02",
                    "purpose": "出差\n第二行",
                }
            ],
            "statistics": {"frequent_destinations": {"上海|杭州": 2}},
            "chat_history": [],
        }
    )
    assert "汉庭\\|如家" in md
    assert "<script>" not in md and "&lt;script&gt;" in md
    table_rows = [
        line
        for line in md.splitlines()
        if line.startswith("|") and "trip_1" in line
    ]
    assert len(table_rows) == 1, "换行不能把一行表格拆成两行"
    assert "出差 第二行" in table_rows[0]
    assert "上海\\|杭州" in md
    print("✓ 导出表格转义竖线 / 换行 / 尖括号")


if __name__ == "__main__":
    test_memory_manager_export_data()
    test_render_markdown()
    test_render_markdown_renders_assistant_orchestration_json()
    test_render_markdown_escapes_table_cells()
    test_export_files()
    print("\n全部通过")
