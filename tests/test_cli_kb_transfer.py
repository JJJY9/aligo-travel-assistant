"""
`python cli.py kb-export / kb-import` 测试（无需 API Key、不访问网络）

覆盖：导出写文件、未初始化/写入失败；导入成功、文件缺失、格式错误、
超过批量上限与后端异常。

运行：python tests/test_cli_kb_transfer.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

import cli
from utils import knowledge_admin, knowledge_stats


def _status(available=True, reason=None):
    data = {"available": available, "collection": "business_travel_knowledge"}
    if reason:
        data["reason"] = reason
    return data


def _capture(func, *args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = func(*args)
    return code, buf.getvalue()


def test_export_writes_json(monkeypatch):
    monkeypatch.setattr(knowledge_stats, "knowledge_status", lambda: _status())
    monkeypatch.setattr(
        knowledge_admin,
        "export_documents",
        lambda: [{"content": "住宿标准", "metadata": {"category": "住宿"}}],
    )
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "sub" / "kb.json"
        code, out = _capture(cli.run_knowledge_export_standalone, str(target))
        assert code == 0
        assert "已导出 1 条知识片段" in out
        payload = json.loads(target.read_text(encoding="utf-8"))
        assert payload["version"] == 1
        assert payload["documents"][0]["content"] == "住宿标准"
    print("✓ kb-export 写入 JSON（自动创建目录）")


def test_export_unavailable_or_failure(monkeypatch):
    monkeypatch.setattr(
        knowledge_stats, "knowledge_status", lambda: _status(False, "知识库未初始化")
    )
    code, out = _capture(cli.run_knowledge_export_standalone, None)
    assert code == 1 and "不可用" in out

    monkeypatch.setattr(knowledge_stats, "knowledge_status", lambda: _status())

    def boom():
        raise RuntimeError("Milvus 故障")

    monkeypatch.setattr(knowledge_admin, "export_documents", boom)
    code, out = _capture(cli.run_knowledge_export_standalone, None)
    assert code == 1 and "导出失败" in out
    print("✓ kb-export 失败时给出原因并返回 1")


def _write_json(tmp, payload):
    path = Path(tmp) / "kb.json"
    path.write_text(
        payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def test_import_success(monkeypatch):
    calls = {}

    def fake_import(documents):
        calls["documents"] = documents
        return {"imported": len(documents), "skipped": 1}

    monkeypatch.setattr(knowledge_admin, "import_documents", fake_import)
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_json(
            tmp, {"documents": [{"content": "a"}, {"content": "b"}]}
        )
        code, out = _capture(cli.run_knowledge_import_standalone, str(path))
    assert code == 0
    assert "已导入 2 条知识片段，跳过 1 条" in out
    assert [item["content"] for item in calls["documents"]] == ["a", "b"]
    print("✓ kb-import 读取 documents 数组并导入")


def test_import_plain_array(monkeypatch):
    monkeypatch.setattr(
        knowledge_admin,
        "import_documents",
        lambda documents: {"imported": len(documents), "skipped": 0},
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_json(tmp, [{"content": "a"}])
        code, _ = _capture(cli.run_knowledge_import_standalone, str(path))
    assert code == 0
    print("✓ kb-import 兼容纯数组格式")


def test_import_errors(monkeypatch):
    missing = Path(tempfile.gettempdir()) / "no-such-kb-file.json"
    code, out = _capture(cli.run_knowledge_import_standalone, str(missing))
    assert code == 1 and "文件不存在" in out

    with tempfile.TemporaryDirectory() as tmp:
        bad = _write_json(tmp, "{not json")
        code, out = _capture(cli.run_knowledge_import_standalone, str(bad))
        assert code == 1 and "读取失败" in out

        empty = _write_json(tmp, {"documents": []})
        code, out = _capture(cli.run_knowledge_import_standalone, str(empty))
        assert code == 1 and "没有知识片段" in out

        good = _write_json(tmp, {"documents": [{"content": "x"}]})

        def boom(documents):
            raise RuntimeError("Milvus 故障")

        monkeypatch.setattr(knowledge_admin, "import_documents", boom)
        code, out = _capture(cli.run_knowledge_import_standalone, str(good))
        assert code == 1 and "导入失败" in out
    print("✓ kb-import 各类错误路径返回 1")


def test_import_large_file_not_capped(monkeypatch):
    """CLI 直接调用导入实现，不套用 HTTP 接口的单次 200 条上限（备份需可完整恢复）。"""
    calls = {}

    def fake_import(documents):
        calls["count"] = len(documents)
        return {"imported": len(documents), "skipped": 0}

    monkeypatch.setattr(knowledge_admin, "import_documents", fake_import)
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_json(
            tmp, {"documents": [{"content": f"item-{i}"} for i in range(250)]}
        )
        code, out = _capture(cli.run_knowledge_import_standalone, str(path))
    assert code == 0
    assert calls["count"] == 250
    assert "已导入 250 条知识片段" in out
    print("✓ kb-import 不受接口 200 条上限限制")


if __name__ == "__main__":
    for case in (
        test_export_writes_json,
        test_export_unavailable_or_failure,
        test_import_success,
        test_import_plain_array,
        test_import_errors,
        test_import_large_file_not_capped,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    print("\n全部通过")
