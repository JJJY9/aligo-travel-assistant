"""
`python cli.py kb-list / kb-delete` 测试（无需 API Key、不访问网络）

运行：python tests/test_cli_kb_manage.py
"""
import contextlib
import io
import json
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
            "total": 3,
            "items": [
                {"id": 7, "content": "住宿标准：一线城市 500 元/晚", "metadata": {"category": "住宿"}},
                {"id": 6, "content": "交通标准", "metadata": {}},
                {
                    "id": 5,
                    "content": "第二段",
                    "metadata": {"category": "住宿", "part": "2", "parts": "3"},
                },
            ],
        }

    monkeypatch.setattr(knowledge_stats, "knowledge_status", lambda: _status())
    monkeypatch.setattr(knowledge_admin, "list_documents", fake_list)
    code, out = _capture(cli.run_knowledge_list_standalone, "住宿")
    assert code == 0
    assert calls == {"limit": 50, "offset": 0, "query": "住宿"}
    assert "共 3 条（关键词：住宿）" in out
    assert "#7 · 住宿" in out
    assert "住宿标准" in out
    assert "#5 · 住宿 · 分片 2/3" in out, "多分片记录应显示分片序号"
    print("✓ kb-list 输出总数、分类、分片序号与预览")


def test_list_error(monkeypatch):
    monkeypatch.setattr(knowledge_stats, "knowledge_status", lambda: _status())

    def boom(**kwargs):
        raise RuntimeError("Milvus 故障")

    monkeypatch.setattr(knowledge_admin, "list_documents", boom)
    code, out = _capture(cli.run_knowledge_list_standalone)
    assert code == 1 and "读取失败" in out
    print("✓ kb-list 读取失败返回 1")


def test_categories_unavailable(monkeypatch):
    monkeypatch.setattr(
        knowledge_stats, "knowledge_status", lambda: _status(False, "知识库未初始化")
    )
    code, out = _capture(cli.run_knowledge_categories_standalone)
    assert code == 1 and "不可用" in out
    print("✓ kb-categories 知识库不可用时返回 1")


def test_categories_prints(monkeypatch):
    monkeypatch.setattr(knowledge_stats, "knowledge_status", lambda: _status())
    monkeypatch.setattr(
        knowledge_admin,
        "list_categories",
        lambda: [{"category": "住宿", "count": 3}, {"category": "交通", "count": 1}],
    )
    code, out = _capture(cli.run_knowledge_categories_standalone)
    assert code == 0
    assert "知识分类：2 个分类，共 4 片" in out
    assert "· 住宿  3 片" in out and "· 交通  1 片" in out
    print("✓ kb-categories 输出分类分布")


def test_categories_json_and_empty(monkeypatch):
    monkeypatch.setattr(knowledge_stats, "knowledge_status", lambda: _status())
    monkeypatch.setattr(
        knowledge_admin,
        "list_categories",
        lambda: [{"category": "住宿", "count": 2}],
    )
    code, out = _capture(cli.run_knowledge_categories_standalone, as_json=True)
    assert code == 0
    data = json.loads(out)
    assert data["categories"] == [{"category": "住宿", "count": 2}]
    assert data["total"] == 2

    monkeypatch.setattr(knowledge_admin, "list_categories", lambda: [])
    code, out = _capture(cli.run_knowledge_categories_standalone)
    assert code == 0 and "暂无分类信息" in out
    print("✓ kb-categories JSON 输出与空分类")


def test_categories_error(monkeypatch):
    monkeypatch.setattr(knowledge_stats, "knowledge_status", lambda: _status())

    def boom():
        raise RuntimeError("Milvus 故障")

    monkeypatch.setattr(knowledge_admin, "list_categories", boom)
    code, out = _capture(cli.run_knowledge_categories_standalone)
    assert code == 1 and "读取失败" in out
    print("✓ kb-categories 读取失败返回 1")


def test_search_unavailable(monkeypatch):
    monkeypatch.setattr(
        knowledge_stats, "knowledge_status", lambda: _status(False, "知识库未初始化")
    )
    code, out = _capture(cli.run_knowledge_search_standalone, "住宿")
    assert code == 1 and "不可用" in out
    print("✓ kb-search 知识库不可用时返回 1")


def test_search_success_and_json(monkeypatch):
    calls = {}

    def fake_search(query, top_k=5):
        calls.update(query=query, top_k=top_k)
        return [
            {
                "id": 7,
                "content": "一线城市住宿不超过 500 元/晚",
                "metadata": {"category": "住宿"},
                "distance": 0.1234,
            },
            {"id": 3, "content": "交通标准", "metadata": {}},
        ]

    monkeypatch.setattr(knowledge_stats, "knowledge_status", lambda: _status())
    monkeypatch.setattr(knowledge_admin, "search_documents", fake_search)
    code, out = _capture(cli.run_knowledge_search_standalone, "住宿标准", top_k=3)
    assert code == 0
    assert calls == {"query": "住宿标准", "top_k": 3}
    assert "语义检索「住宿标准」：2 条结果" in out
    assert "#7 · 住宿 · 相似度 0.123" in out
    assert "一线城市住宿不超过" in out
    assert "#3" in out and "交通标准" in out

    code, out = _capture(
        cli.run_knowledge_search_standalone, "住宿", as_json=True
    )
    assert code == 0
    data = json.loads(out)
    assert data["query"] == "住宿" and data["top_k"] == 5
    assert len(data["results"]) == 2
    print("✓ kb-search 文本 / JSON 输出")


def test_search_empty_and_error(monkeypatch):
    monkeypatch.setattr(knowledge_stats, "knowledge_status", lambda: _status())
    monkeypatch.setattr(knowledge_admin, "search_documents", lambda q, top_k=5: [])
    code, out = _capture(cli.run_knowledge_search_standalone, "不存在的内容")
    assert code == 0 and "没有与「不存在的内容」相关的内容" in out

    def boom(q, top_k=5):
        raise RuntimeError("Milvus 故障")

    monkeypatch.setattr(knowledge_admin, "search_documents", boom)
    code, out = _capture(cli.run_knowledge_search_standalone, "住宿")
    assert code == 1 and "检索失败" in out
    print("✓ kb-search 无结果 / 检索失败")


def test_kb_search_args():
    assert cli._parse_kb_search_args(["住宿"]) == ("住宿", 5, False)
    assert cli._parse_kb_search_args(["住宿", "标准", "--top-k", "3"]) == (
        "住宿 标准",
        3,
        False,
    )
    assert cli._parse_kb_search_args(["--top-k=10", "住宿", "--json"]) == (
        "住宿",
        10,
        True,
    )
    assert cli._parse_kb_search_args(["--top-k"]) is None
    assert cli._parse_kb_search_args(["--top-k", "0", "x"]) is None
    assert cli._parse_kb_search_args(["--top-k", "61", "x"]) is None
    assert cli._parse_kb_search_args(["--json"]) is None, "缺关键词"
    assert cli._parse_kb_search_args(["x", "-y"]) is None, "未知参数"
    print("✓ kb-search 参数解析")


def test_kb_search_dispatch(monkeypatch):
    monkeypatch.setattr(knowledge_stats, "knowledge_status", lambda: _status())
    monkeypatch.setattr(
        knowledge_admin,
        "search_documents",
        lambda q, top_k=5: [{"id": 1, "content": "内容", "metadata": {}}],
    )
    monkeypatch.setattr(
        sys, "argv", ["cli.py", "kb-search", "住宿", "--top-k", "2"]
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert "语义检索「住宿」" in buf.getvalue()
    print("✓ dispatch 层 kb-search 正常执行")


def test_delete_success(monkeypatch):
    calls = []

    def fake_delete(doc_id, whole_group=False):
        calls.append((doc_id, whole_group))
        return 3 if whole_group else 1

    monkeypatch.setattr(knowledge_admin, "delete_document", fake_delete)
    code, out = _capture(cli.run_knowledge_delete_standalone, 7)
    assert code == 0 and "已删除知识片段 #7" in out
    assert calls[-1] == (7, False)
    gcode, gout = _capture(cli.run_knowledge_delete_standalone, 7, whole_group=True)
    assert gcode == 0 and "已删除整组分片 #7（共 3 片）" in gout
    assert calls[-1] == (7, True)

    # 旧记录没有 group：--group 回退为单条删除，提示不能声称删掉了整组
    monkeypatch.setattr(
        knowledge_admin, "delete_document", lambda doc_id, whole_group=False: 1
    )
    fcode, fout = _capture(cli.run_knowledge_delete_standalone, 7, whole_group=True)
    assert fcode == 0 and "未找到同组分片" in fout
    print("✓ kb-delete 删除成功（单条 / 整组 / 无组回退）")


def test_delete_not_found(monkeypatch):
    monkeypatch.setattr(
        knowledge_admin, "delete_document", lambda doc_id, whole_group=False: 0
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


def test_knowledge_dispatch_rejects_unicode_digits(monkeypatch):
    """kb-delete / kb-duplicates 的 id / limit 必须是 ASCII 十进制整数。"""
    import contextlib
    import io

    calls = []
    monkeypatch.setattr(
        cli,
        "run_knowledge_delete_standalone",
        lambda doc_id, whole_group=False: calls.append((doc_id, whole_group)) or 0,
    )
    monkeypatch.setattr(
        cli,
        "run_knowledge_duplicates_standalone",
        lambda limit=20: calls.append(("dup", limit)) or 0,
    )
    for argv in (
        ["cli.py", "kb-delete", "²"],
        ["cli.py", "kb-delete", "1" * 5000],
        ["cli.py", "kb-delete"],
        ["cli.py", "kb-duplicates", "²"],
        ["cli.py", "kb-duplicates", "1", "2"],
    ):
        monkeypatch.setattr(sys, "argv", argv)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2, argv
        assert "用法：" in out.getvalue(), argv

    monkeypatch.setattr(sys, "argv", ["cli.py", "kb-delete", "7", "--group"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0 and calls[-1] == (7, True)
    monkeypatch.setattr(sys, "argv", ["cli.py", "kb-duplicates", "0"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0 and calls[-1] == ("dup", 0)
    print("✓ kb-delete / kb-duplicates 拒绝 Unicode 数字与超长参数")


def test_duplicates_reports_groups(monkeypatch):
    monkeypatch.setattr(
        knowledge_admin,
        "find_duplicate_contents",
        lambda: [
            ("重复内容甲", [3, 9]),
            ("重复内容乙", [5, 6, 7]),
        ],
    )
    code, out = _capture(cli.run_knowledge_duplicates_standalone)
    assert code == 0
    assert "发现 2 组重复内容（共 5 条片段）" in out
    assert "#3、#9" in out
    assert "重复内容甲" in out

    # limit 限制显示组数并给出提示；limit=0 显示全部
    limited_code, limited = _capture(cli.run_knowledge_duplicates_standalone, 1)
    assert limited_code == 0 and "其余 1 组未显示" in limited
    assert "重复内容乙" not in limited
    full_code, full = _capture(cli.run_knowledge_duplicates_standalone, 0)
    assert full_code == 0 and "重复内容乙" in full and "未显示" not in full
    print("✓ kb-duplicates 列出重复组（limit）")


def test_duplicates_none_and_error(monkeypatch):
    monkeypatch.setattr(knowledge_admin, "find_duplicate_contents", lambda: [])
    code, out = _capture(cli.run_knowledge_duplicates_standalone)
    assert code == 0 and "没有重复内容的独立知识片段" in out

    def boom():
        raise RuntimeError("Milvus 故障")

    monkeypatch.setattr(knowledge_admin, "find_duplicate_contents", boom)
    code, out = _capture(cli.run_knowledge_duplicates_standalone)
    assert code == 1 and "读取失败" in out
    print("✓ kb-duplicates 无重复 / 读取失败")


if __name__ == "__main__":
    for case in (
        test_list_unavailable,
        test_list_prints_items,
        test_list_error,
        test_delete_success,
        test_delete_not_found,
        test_delete_error,
        test_knowledge_dispatch_rejects_unicode_digits,
        test_duplicates_reports_groups,
        test_duplicates_none_and_error,
        test_categories_unavailable,
        test_categories_prints,
        test_categories_json_and_empty,
        test_categories_error,
        test_search_unavailable,
        test_search_success_and_json,
        test_search_empty_and_error,
        test_kb_search_args,
        test_kb_search_dispatch,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    print("\n全部通过")
