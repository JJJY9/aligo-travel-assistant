"""
`python cli.py kb-list / kb-delete` 测试（无需 API Key、不访问网络）

运行：python tests/test_cli_kb_manage.py
"""
import contextlib
import io
import os
import sys

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


def _capture(func, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = func(*args, **kwargs)
    return code, buf.getvalue()


def test_list_unavailable(monkeypatch):
    monkeypatch.setattr(
        knowledge_stats, "knowledge_status", lambda: _status(False, "知识库未初始化")
    )
    code, out = _capture(cli.run_knowledge_list_standalone)
    assert code == 1 and "不可用" in out
    print("✓ kb-list 知识库不可用时返回 1")


def test_list_prints_items(monkeypatch):
    calls = {}

    def fake_list(limit, offset, query):
        calls.update(limit=limit, offset=offset, query=query)
        return {
            "total": 2,
            "items": [
                {"id": 7, "content": "住宿标准：一线城市 500 元/晚", "metadata": {"category": "住宿"}},
                {"id": 6, "content": "交通标准", "metadata": {}},
            ],
        }

    monkeypatch.setattr(knowledge_stats, "knowledge_status", lambda: _status())
    monkeypatch.setattr(knowledge_admin, "list_documents", fake_list)
    code, out = _capture(cli.run_knowledge_list_standalone, "住宿")
    assert code == 0
    assert calls == {"limit": 50, "offset": 0, "query": "住宿"}
    assert "共 2 条（关键词：住宿）" in out
    assert "#7 · 住宿" in out
    assert "住宿标准" in out
    print("✓ kb-list 输出总数、分类与预览")


def test_list_error(monkeypatch):
    monkeypatch.setattr(knowledge_stats, "knowledge_status", lambda: _status())

    def boom(**kwargs):
        raise RuntimeError("Milvus 故障")

    monkeypatch.setattr(knowledge_admin, "list_documents", boom)
    code, out = _capture(cli.run_knowledge_list_standalone)
    assert code == 1 and "读取失败" in out
    print("✓ kb-list 读取失败返回 1")


def test_delete_success(monkeypatch):
    calls = []

    def fake_delete(doc_id, whole_group=False):
        calls.append((doc_id, whole_group))
        return doc_id == 7

    monkeypatch.setattr(knowledge_admin, "delete_document", fake_delete)
    code, out = _capture(cli.run_knowledge_delete_standalone, 7)
    assert code == 0 and "已删除知识片段 #7" in out
    assert calls[-1] == (7, False)
    gcode, gout = _capture(cli.run_knowledge_delete_standalone, 7, whole_group=True)
    assert gcode == 0 and "已删除整组分片 #7" in gout
    assert calls[-1] == (7, True)
    print("✓ kb-delete 删除成功（单条 / 整组）")


def test_delete_not_found(monkeypatch):
    monkeypatch.setattr(
        knowledge_admin, "delete_document", lambda doc_id, whole_group=False: False
    )
    code, out = _capture(cli.run_knowledge_delete_standalone, 99)
    assert code == 1 and "片段不存在" in out
    print("✓ kb-delete 片段不存在返回 1")


def test_delete_error(monkeypatch):
    def boom(doc_id, whole_group=False):
        raise RuntimeError("Milvus 故障")

    monkeypatch.setattr(knowledge_admin, "delete_document", boom)
    code, out = _capture(cli.run_knowledge_delete_standalone, 1)
    assert code == 1 and "删除失败" in out
    print("✓ kb-delete 删除失败返回 1")


if __name__ == "__main__":
    for case in (
        test_list_unavailable,
        test_list_prints_items,
        test_list_error,
        test_delete_success,
        test_delete_not_found,
        test_delete_error,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    print("\n全部通过")
