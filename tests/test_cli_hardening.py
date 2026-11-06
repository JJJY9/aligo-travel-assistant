"""
CLI 健壮性测试：未知命令 / 参数校验 / 导出文件名 / 管道输出 / BOM 导入

运行：python tests/test_cli_hardening.py
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


def _main_exit(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", argv)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit) as exc:
        cli.main()
    return exc.value.code, buf.getvalue()


def test_commands_hint_matches_dispatch(monkeypatch):
    """帮助里的命令清单与 main() 的分支必须一一对应（新增命令漏改一边会失配）。"""
    import re

    source = Path(__file__).resolve().parent.parent.joinpath("cli.py").read_text(
        encoding="utf-8"
    )
    commands = [
        item.strip() for item in cli._COMMANDS_HINT.split("：", 1)[1].split("/")
    ]
    branches = set(re.findall(r'arg == "([^"]+)"', source))
    assert set(commands) == branches, (
        f"提示缺少：{sorted(branches - set(commands))}；"
        f"提示多余：{sorted(set(commands) - branches)}"
    )
    print("✓ 帮助命令清单与 dispatch 分支一一对应")


def test_unknown_command_exits_2(monkeypatch):
    code, output = _main_exit(monkeypatch, ["cli.py", "frobnicate"])
    assert code == 2
    assert "未知命令" in output
    assert "可用命令" in output
    print("✓ 未知命令 → 退出码 2")


def test_help_exits_0(monkeypatch):
    code, output = _main_exit(monkeypatch, ["cli.py", "--help"])
    assert code == 0
    assert "可用命令" in output
    print("✓ --help 输出用法 → 退出码 0")


def test_kb_delete_rejects_extra_flags(monkeypatch):
    called = {"n": 0}

    def fake_delete(doc_id):
        called["n"] += 1
        return 0

    monkeypatch.setattr(cli, "run_knowledge_delete_standalone", fake_delete)
    for argv in (
        ["cli.py", "kb-delete", "3", "--force"],
        ["cli.py", "kb-delete", "3", "4"],
    ):
        code, output = _main_exit(monkeypatch, argv)
        assert code == 2
        assert "用法" in output
    assert called["n"] == 0, "非法参数不能执行删除"
    print("✓ kb-delete 多余参数 → 退出码 2 且不执行删除")


def test_kb_import_and_list_reject_extra_args(monkeypatch):
    calls = {"import": 0, "list": 0}
    monkeypatch.setattr(
        cli,
        "run_knowledge_import_standalone",
        lambda path: calls.__setitem__("import", calls["import"] + 1) or 0,
    )
    monkeypatch.setattr(
        cli,
        "run_knowledge_list_standalone",
        lambda query="", as_json=False: calls.__setitem__("list", calls["list"] + 1) or 0,
    )

    code, _ = _main_exit(monkeypatch, ["cli.py", "kb-import", "a.json", "extra"])
    assert code == 2
    code, _ = _main_exit(monkeypatch, ["cli.py", "kb-list", "a", "b"])
    assert code == 2
    assert calls == {"import": 0, "list": 0}
    print("✓ kb-import / kb-list 多余参数 → 退出码 2")


def test_safe_export_user_is_unique(monkeypatch=None):
    assert cli._safe_export_user("alice") == "alice"
    assert cli._safe_export_user("张三") != cli._safe_export_user("李四")
    assert cli._safe_export_user("a" * 40) != cli._safe_export_user("a" * 39)
    assert all(
        char.isalnum() or char in "_-"
        for char in cli._safe_export_user("张三-上海/2026")
    )
    print("✓ 导出文件名对中文 / 超长用户唯一")


def test_print_stdout_swallows_broken_pipe(monkeypatch):
    class _BrokenStdout(io.StringIO):
        def write(self, text):
            raise BrokenPipeError("closed")

        def flush(self):
            raise BrokenPipeError("closed")

    original = sys.stdout
    monkeypatch.setattr(sys, "stdout", _BrokenStdout())
    cli._print_stdout("内容")  # 不应抛异常
    monkeypatch.setattr(sys, "stdout", original)
    print("✓ 下游提前关闭时导出不抛 traceback")


def test_kb_import_accepts_utf8_bom(monkeypatch):
    from utils import knowledge_admin

    imported = {}

    def fake_import(documents, dry_run=False):
        imported["count"] = len(documents)
        return {"imported": len(documents), "skipped": 0}

    monkeypatch.setattr(knowledge_admin, "import_documents", fake_import)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "backup.json"
        path.write_text(
            json.dumps({"documents": [{"content": "测试"}]}, ensure_ascii=False),
            encoding="utf-8-sig",
        )
        code = cli.run_knowledge_import_standalone(str(path))
    assert code == 0
    assert imported["count"] == 1
    print("✓ kb-import 兼容 UTF-8 BOM")


def test_doctor_falls_back_without_rich(monkeypatch):
    """rich 缺失时 doctor 退回纯文本输出（诊断命令不依赖渲染库）。"""
    from utils import doctor as doctor_module

    async def fake_run(include_network=True):
        return [
            doctor_module.CheckResult("Python 版本", doctor_module.STATUS_OK, "3.12"),
            doctor_module.CheckResult("rich", doctor_module.STATUS_FAIL, "缺失"),
        ]

    monkeypatch.setattr(doctor_module, "run_doctor", fake_run)
    monkeypatch.setattr(cli, "Console", None)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_doctor_standalone(as_json=False)
    text = out.getvalue()
    assert code == 1
    assert "[fail] rich: 缺失" in text
    assert "存在必须处理的问题" in text
    print("✓ rich 缺失时 doctor 退回纯文本")


if __name__ == "__main__":
    for case in (
        test_unknown_command_exits_2,
        test_commands_hint_matches_dispatch,
        test_help_exits_0,
        test_kb_delete_rejects_extra_flags,
        test_kb_import_and_list_reject_extra_args,
        test_safe_export_user_is_unique,
        test_print_stdout_swallows_broken_pipe,
        test_kb_import_accepts_utf8_bom,
        test_doctor_falls_back_without_rich,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    print("\n全部通过")
