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


def _capture(func, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = func(*args, **kwargs)
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


def test_export_refuses_overwrite_without_force(monkeypatch):
    """kb-export 默认不覆盖已有文件（与 trips-export / session-export 一致）。"""
    monkeypatch.setattr(knowledge_stats, "knowledge_status", lambda: _status())
    monkeypatch.setattr(
        knowledge_admin,
        "export_documents",
        lambda: [{"content": "住宿标准", "metadata": {"category": "住宿"}}],
    )
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "keep.json"
        target.write_text('{"keep": true}', encoding="utf-8")
        code, out = _capture(cli.run_knowledge_export_standalone, str(target))
        assert code == 1 and "文件已存在" in out
        assert json.loads(target.read_text(encoding="utf-8")) == {"keep": True}

        code, out = _capture(
            lambda path: cli.run_knowledge_export_standalone(path, force=True),
            str(target),
        )
        assert code == 0 and "已导出 1 条知识片段" in out
        assert "documents" in json.loads(target.read_text(encoding="utf-8"))
    print("✓ kb-export 默认拒绝覆盖，--force 覆盖")


def test_kb_export_dispatch_strict(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli,
        "run_knowledge_export_standalone",
        lambda target, force=False, to_stdout=False: (
            calls.append((target, force, to_stdout)) or 0
        ),
    )
    monkeypatch.setattr(sys, "argv", ["cli.py", "kb-export", "a.json", "--force"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert calls == [("a.json", True, False)]

    monkeypatch.setattr(sys, "argv", ["cli.py", "kb-export", "--stdout"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert calls[-1] == (None, False, True)

    for argv in (
        ["cli.py", "kb-export", "--unknown"],
        ["cli.py", "kb-export", "a.json", "b.json"],
        ["cli.py", "kb-export", "a.json", "--stdout"],
    ):
        monkeypatch.setattr(sys, "argv", argv)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2, argv
        assert "用法：python cli.py kb-export" in out.getvalue()
    print("✓ kb-export 严格参数解析（含 --stdout 互斥）")


def test_export_to_stdout_writes_no_file(monkeypatch):
    """--stdout 直接打印 JSON，不写入任何文件。"""
    monkeypatch.setattr(knowledge_stats, "knowledge_status", lambda: _status())
    monkeypatch.setattr(
        knowledge_admin,
        "export_documents",
        lambda: [{"content": "住宿标准", "metadata": {"category": "住宿"}}],
    )
    code, out = _capture(
        cli.run_knowledge_export_standalone,
        None,
        to_stdout=True,
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["version"] == 1
    assert payload["documents"][0]["content"] == "住宿标准"
    print("✓ kb-export --stdout 打印 JSON 且不写文件")


def test_import_rejects_non_utf8_file(monkeypatch):
    """GBK 备份文件应给出「读取失败」而不是抛 UnicodeDecodeError。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "gbk.json"
        path.write_bytes('{"documents": [{"content": "中文"}]}'.encode("gbk"))
        code, out = _capture(cli.run_knowledge_import_standalone, str(path))
    assert code == 1
    assert "读取失败" in out and "UTF-8" in out
    print("✓ kb-import 非 UTF-8 文件给出读取失败")


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

    def fake_import(documents, dry_run=False):
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
        lambda documents, dry_run=False: {"imported": len(documents), "skipped": 0},
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

        def boom(documents, dry_run=False):
            raise RuntimeError("Milvus 故障")

        monkeypatch.setattr(knowledge_admin, "import_documents", boom)
        code, out = _capture(cli.run_knowledge_import_standalone, str(good))
        assert code == 1 and "导入失败" in out
    print("✓ kb-import 各类错误路径返回 1")


def test_import_large_file_not_capped(monkeypatch):
    """CLI 直接调用导入实现，不套用 HTTP 接口的单次 200 条上限（备份需可完整恢复）。"""
    calls = {}

    def fake_import(documents, dry_run=False):
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


def test_import_dry_run(monkeypatch):
    calls = {}

    def fake_import(documents, dry_run=False):
        calls["dry_run"] = dry_run
        return {"imported": 0, "skipped": 1, "would_import": len(documents)}

    monkeypatch.setattr(knowledge_admin, "import_documents", fake_import)
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_json(
            tmp, {"documents": [{"content": "a"}, {"content": "b"}]}
        )
        code, out = _capture(
            cli.run_knowledge_import_standalone, str(path), dry_run=True
        )
    assert code == 0
    assert calls["dry_run"] is True
    assert "预览：将导入 2 条知识片段，跳过 1 条（未写入）" in out
    print("✓ kb-import --dry-run 预览不写入")


def test_import_reports_skip_reasons(monkeypatch):
    """文本输出要区分「已存在」与「无效」，与面板 / Web 接口一致。"""
    monkeypatch.setattr(
        knowledge_admin,
        "import_documents",
        lambda documents, dry_run=False: {
            "imported": 2,
            "skipped": 3,
            "skipped_invalid": 2,
            "skipped_duplicate": 1,
        },
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_json(tmp, {"documents": [{"content": "a"}]})
        code, out = _capture(cli.run_knowledge_import_standalone, str(path))
    assert code == 0
    assert "已导入 2 条知识片段，跳过 3 条（1 条已存在 / 2 条无效）" in out
    print("✓ kb-import 文本输出区分跳过原因")


def test_import_json_output(monkeypatch):
    """--json 输出结构化结果（正常 / 预览两种形态）。"""
    results = [
        {"imported": 2, "skipped": 3, "skipped_invalid": 1, "skipped_duplicate": 2},
        {
            "imported": 0,
            "skipped": 1,
            "skipped_invalid": 1,
            "skipped_duplicate": 0,
            "would_import": 4,
        },
    ]
    calls: list[bool] = []

    def fake_import(documents, dry_run=False):
        calls.append(dry_run)
        return results[len(calls) - 1]

    monkeypatch.setattr(knowledge_admin, "import_documents", fake_import)
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_json(tmp, {"documents": [{"content": "a"}]})
        code, out = _capture(
            cli.run_knowledge_import_standalone, str(path), as_json=True
        )
        payload = json.loads(out)
        assert code == 0
        assert payload == {
            "ok": True,
            "dry_run": False,
            "imported": 2,
            "skipped": 3,
            "skipped_invalid": 1,
            "skipped_duplicate": 2,
        }

        code, out = _capture(
            cli.run_knowledge_import_standalone,
            str(path),
            dry_run=True,
            as_json=True,
        )
        payload = json.loads(out)
        assert code == 0
        assert payload["ok"] is True and payload["dry_run"] is True
        assert payload["would_import"] == 4
    print("✓ kb-import --json 正常 / 预览输出")


def test_import_json_errors(monkeypatch):
    """--json 模式下错误也要输出结构化 JSON 并返回 1。"""
    missing = Path(tempfile.gettempdir()) / "no-such-kb-file-json.json"
    code, out = _capture(
        cli.run_knowledge_import_standalone, str(missing), as_json=True
    )
    payload = json.loads(out)
    assert code == 1 and payload["ok"] is False and "文件不存在" in payload["error"]

    with tempfile.TemporaryDirectory() as tmp:
        bad = _write_json(tmp, "{not json")
        code, out = _capture(
            cli.run_knowledge_import_standalone, str(bad), as_json=True
        )
        assert code == 1 and json.loads(out)["ok"] is False

        good = _write_json(tmp, {"documents": [{"content": "x"}]})

        def boom(documents, dry_run=False):
            raise RuntimeError("Milvus 故障")

        monkeypatch.setattr(knowledge_admin, "import_documents", boom)
        code, out = _capture(
            cli.run_knowledge_import_standalone, str(good), as_json=True
        )
        assert code == 1 and "Milvus" in json.loads(out)["error"]
    print("✓ kb-import --json 错误输出")


def test_kb_import_dispatch_flags(monkeypatch):
    """--json / --dry-run 位置不限、大小写不敏感；未知参数退出码 2。"""
    calls: list[tuple[str, bool, bool]] = []
    monkeypatch.setattr(
        cli,
        "run_knowledge_import_standalone",
        lambda source, dry_run=False, as_json=False: (
            calls.append((source, dry_run, as_json)) or 0
        ),
    )
    for argv, expected in (
        (["cli.py", "kb-import", "a.json"], ("a.json", False, False)),
        (
            ["cli.py", "kb-import", "--json", "a.json", "--dry-run"],
            ("a.json", True, True),
        ),
        (["cli.py", "kb-import", "a.json", "--JSON"], ("a.json", False, True)),
    ):
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0, argv
        assert calls[-1] == expected, argv
    for argv in (
        ["cli.py", "kb-import"],
        ["cli.py", "kb-import", "--dry-run"],
        ["cli.py", "kb-import", "a.json", "b.json"],
        ["cli.py", "kb-import", "a.json", "--bad"],
    ):
        monkeypatch.setattr(sys, "argv", argv)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2, argv
        assert "--json" in out.getvalue()
    print("✓ kb-import 参数解析（--json / --dry-run 任意顺序）")


if __name__ == "__main__":
    for case in (
        test_export_writes_json,
        test_export_refuses_overwrite_without_force,
        test_kb_export_dispatch_strict,
        test_export_to_stdout_writes_no_file,
        test_export_unavailable_or_failure,
        test_import_success,
        test_import_plain_array,
        test_import_errors,
        test_import_rejects_non_utf8_file,
        test_import_large_file_not_capped,
        test_import_dry_run,
        test_import_reports_skip_reasons,
        test_import_json_output,
        test_import_json_errors,
        test_kb_import_dispatch_flags,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    print("\n全部通过")
