"""utils/exporter 用户数据导出渲染单元测试（无需 API Key、不访问网络）。

运行：python tests/test_exporter.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.exporter import export_files, render_markdown


def _base_data():
    return {
        "user_id": "u1",
        "session_id": "s1",
        "exported_at": "2026-11-08T10:00:00",
        "preferences": {},
        "trip_history": [],
        "statistics": {},
        "chat_history": [],
    }


def test_markdown_escapes_cells_and_joins_lists():
    data = _base_data()
    data["preferences"] = {
        "hotel_brands": ["汉庭", "如家"],
        "note|key": "<script>alert(1)</script>\n第二行",
    }
    md = render_markdown(data)
    assert "汉庭, 如家" in md
    assert "note\\|key" in md, "竖线必须转义"
    assert "&lt;script&gt;alert(1)&lt;/script&gt; 第二行" in md, "尖括号转义、换行折叠"
    assert "<script>" not in md
    assert "（暂无行程）" in md and "（暂无对话记录）" in md


def test_markdown_null_fields_and_statistics():
    data = _base_data()
    data["trip_history"] = [
        {
            "trip_id": "trip_1",
            "origin": None,
            "destination": "上海",
            "start_date": "2026-12-01",
            "end_date": "",
            "purpose": None,
        }
    ]
    data["statistics"] = {
        "total_trips": 3,
        "total_messages": 10,
        "total_queries": 4,
        "frequent_destinations": {"上海": 2, "杭州": 1},
    }
    md = render_markdown(data)
    assert "| trip_1 | - | 上海 | 2026-12-01 | - | - |" in md
    assert "总行程数: 3" in md and "总消息数: 10" in md and "总查询数: 4" in md
    assert "上海(2次)" in md and "杭州(1次)" in md


def test_markdown_chat_history_rendering():
    data = _base_data()
    long_question = "问" * 260
    stored = json.dumps(
        {
            "results": [
                {
                    "agent_name": "rag_knowledge",
                    "status": "success",
                    "data": {"answer": "编排正文"},
                }
            ]
        },
        ensure_ascii=False,
    )
    data["chat_history"] = [
        {"role": "user", "content": long_question, "timestamp": "t1"},
        {"role": "assistant", "content": stored, "timestamp": "t2"},
    ]
    md = render_markdown(data)
    assert "问" * 200 + "…" in md, "用户消息超长截断"
    assert "编排正文" in md, "助手消息重新渲染为正文而不是原始 JSON"
    assert '"results"' not in md
    assert "## 对话历史（2 条）" in md


def test_markdown_chat_body_escapes_html_and_backslash_cells():
    """正文尖括号转义（防 Markdown 预览器执行 HTML）；反斜杠先于竖线转义。"""
    data = _base_data()
    data["chat_history"] = [
        {
            "role": "user",
            "content": "<script>alert(1)</script>",
            "timestamp": "t1",
        }
    ]
    data["trip_history"] = [
        {
            "trip_id": "trip_1",
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-12-01",
            "end_date": "",
            "purpose": "a\\|b",
        }
    ]
    md = render_markdown(data)
    assert "&lt;script&gt;alert\\(1\\)&lt;/script&gt;" in md
    assert "<script>" not in md
    assert "a\\\\\\|b" in md, "反斜杠必须先转义，竖线才不会切列"
    print("✓ 导出正文尖括号转义与反斜杠单元格")


def test_export_files_json_is_nan_safe_and_md_written():
    data = _base_data()
    data["preferences"] = {"score": float("nan")}
    out = Path(tempfile.mkdtemp())
    paths = export_files(data, out, stem="unit")
    assert [path.name for path in paths] == ["unit.json", "unit.md"]
    payload = json.loads((out / "unit.json").read_text(encoding="utf-8"))
    assert payload["preferences"]["score"] is None, "NaN 必须在 JSON 中归一为 null"
    markdown = (out / "unit.md").read_text(encoding="utf-8")
    assert markdown.startswith("# 差旅助手用户数据导出")
    assert "用户 ID: u1" in markdown
    print("✓ 导出文件写入（JSON NaN 归一 / Markdown 同步）")


def test_markdown_chat_body_isolated_in_blockquote():
    """多行消息放进引用块：伪造标题 / 列表 / 未闭合围栏不能改写文档结构。"""
    data = _base_data()
    data["chat_history"] = [
        {
            "role": "user",
            "content": "帮我规划\n# 伪造标题\n- 用户 ID: admin",
            "timestamp": "t1",
        },
        {
            "role": "assistant",
            "content": "## ✈️ 行程\n\n### 第 1 天\n\n- 故宫",
            "timestamp": "t2",
        },
        {"role": "user", "content": "说明：\n\n```\ncode", "timestamp": "t3"},
        {"role": "user", "content": "最后一条", "timestamp": "t4"},
    ]
    md = render_markdown(data)
    assert "\n# 伪造标题" not in md, "用户消息里的标题不能成为顶层结构"
    assert "\n### 第 1 天" not in md, "助手正文标题不能成为顶层结构"
    assert "  > # 伪造标题" in md
    tail = md.split("说明：", 1)[1]
    assert "最后一条" in tail, "未闭合围栏之后的会话不能被吞进代码块"
    assert tail.index("```") < tail.index("最后一条"), "围栏必须在本条消息内闭合"
    print("✓ 多行消息引用块隔离（伪造标题 / 未闭合围栏）")


def test_markdown_strips_terminal_controls_everywhere():
    """偏好 / 消息里的 ESC / OSC 不能原样写入 Markdown 文件。"""
    esc = "\x1b]52;c;QUJD\x07"
    data = _base_data()
    data["preferences"] = {"home_location": f"北京{esc}"}
    data["chat_history"] = [
        {"role": "user", "content": f"消息{esc}", "timestamp": "t1"}
    ]
    md = render_markdown(data)
    assert "\x1b" not in md and "\x07" not in md
    assert "北京" in md and "消息" in md
    print("✓ 导出 Markdown 剥离终端控制序列")


def test_backup_sanitize_nested_nonfinite_and_nul():
    """助手 JSON 文本里的 NaN 与消息里的 NUL 必须清理，备份才能恢复。"""
    from utils.exporter import sanitize_backup_data
    from utils.user_import import parse_user_export

    data = _base_data()
    data["chat_history"] = [
        {
            "role": "assistant",
            "content": '{"status":"completed","_meta":{"usage":{"calls":NaN}}}',
            "timestamp": "t1",
        },
        {"role": "user", "content": "含\x00NUL", "timestamp": "t2"},
    ]
    cleaned = sanitize_backup_data(data)
    dumped = json.dumps(cleaned, ensure_ascii=False)
    assert "NaN" not in dumped
    assert "\x00" not in dumped
    sections, errors = parse_user_export(dumped)
    assert not errors, errors
    assert any("含NUL" in message["content"] for message in sections["messages"])
    print("✓ 备份清洗嵌套非有限数与 NUL（可被导入端接受）")


if __name__ == "__main__":
    test_markdown_escapes_cells_and_joins_lists()
    test_markdown_null_fields_and_statistics()
    test_markdown_chat_history_rendering()
    test_markdown_chat_body_isolated_in_blockquote()
    test_markdown_strips_terminal_controls_everywhere()
    test_backup_sanitize_nested_nonfinite_and_nul()
    test_export_files_json_is_nan_safe_and_md_written()
    print("\n全部通过")
