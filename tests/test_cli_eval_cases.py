"""CLI `eval-cases` 评测用例生成测试（无需模型 / 网络）。

运行：python tests/test_cli_eval_cases.py
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

import cli
from utils.eval_cases import build_cases


def test_parse_eval_cases_args():
    """默认值、等号形式与非法参数。"""
    assert cli._parse_eval_cases_args([]) == (500, 7, None, False, False)
    assert cli._parse_eval_cases_args(
        ["--count", "12", "--seed=3", "--out", "cases.jsonl", "--json"]
    ) == (12, 3, "cases.jsonl", True, False)
    assert cli._parse_eval_cases_args(
        ["--out", "cases.jsonl", "--force"]
    ) == (500, 7, "cases.jsonl", False, True)
    assert cli._parse_eval_cases_args(["--count", "abc"]) is None
    assert cli._parse_eval_cases_args(["--count", "5001"]) is None
    # 0 条用例会产出空数据集却报成功（同族 eval-agent 的合法域是 1..5000）
    assert cli._parse_eval_cases_args(["--count", "0"]) is None
    assert cli._parse_eval_cases_args(["--count=0000"]) is None
    # 前导零不影响数值：05000 即 5000（合法范围内）
    assert cli._parse_eval_cases_args(["--count", "05000"]) == (
        5000, 7, None, False, False
    )
    assert cli._parse_eval_cases_args(["--seed", "-1"]) is None
    assert cli._parse_eval_cases_args(["--out"]) is None
    assert cli._parse_eval_cases_args(["--unknown"]) is None
    print("✓ eval-cases 参数解析")


def test_run_eval_cases_writes_jsonl():
    """生成 12 条用例并写入 JSONL，内容与确定性生成器一致。"""
    root = tempfile.mkdtemp(prefix="chailv_eval_cases_")
    try:
        path = os.path.join(root, "cases.jsonl")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.run_eval_cases_standalone(12, 7, path)
        assert code == 0, output.getvalue()
        assert "已生成 12 条评测用例" in output.getvalue()
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        assert len(lines) == 12, lines
        rows = [json.loads(line) for line in lines]
        assert len({row["category"] for row in rows}) == 12
        assert rows == build_cases(12, 7), "落盘内容必须与生成器一致"
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("✓ eval-cases 写入 JSONL")


def test_run_eval_cases_write_failure_is_error():
    """输出路径不可写时返回 1 并给出原因。"""
    root = tempfile.mkdtemp(prefix="chailv_eval_cases_dir_")
    try:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.run_eval_cases_standalone(3, 7, root)
        assert code == 1
        # 目标已存在（这里是同名目录）走覆盖守卫，而不是「生成失败」
        assert "文件已存在" in output.getvalue(), output.getvalue()
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("✓ eval-cases 写入失败返回错误")


def test_eval_cases_dispatch(monkeypatch):
    """CLI dispatch 把参数传给生成函数。"""
    calls: dict = {}
    monkeypatch.setattr(
        cli,
        "run_eval_cases_standalone",
        lambda **kwargs: calls.update(kwargs) or 0,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["cli.py", "eval-cases", "--count", "12", "--json"],
    )
    output = io.StringIO()
    with contextlib.redirect_stdout(output), pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert calls == {
        "count": 12,
        "seed": 7,
        "out_path": None,
        "as_json": True,
        "force": False,
    }
    monkeypatch.setattr(
        sys,
        "argv",
        ["cli.py", "eval-cases", "--count", "12", "--out", "x.jsonl", "--force"],
    )
    with contextlib.redirect_stdout(output), pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert calls["force"] is True and calls["out_path"] == "x.jsonl"
    print("✓ eval-cases dispatch")


def test_eval_cases_dispatch_invalid_args(monkeypatch):
    """非法参数退出 2 并打印用法。"""
    monkeypatch.setattr(
        sys, "argv", ["cli.py", "eval-cases", "--count", "abc"]
    )
    output = io.StringIO()
    with contextlib.redirect_stdout(output), pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert "用法：python cli.py eval-cases" in output.getvalue()
    print("✓ eval-cases 非法参数退出 2")


if __name__ == "__main__":
    import _stdio

    _stdio.ensure_utf8()
    test_parse_eval_cases_args()
    test_run_eval_cases_writes_jsonl()
    test_run_eval_cases_write_failure_is_error()
    mp = pytest.MonkeyPatch()
    try:
        test_eval_cases_dispatch(mp)
    finally:
        mp.undo()
    mp = pytest.MonkeyPatch()
    try:
        test_eval_cases_dispatch_invalid_args(mp)
    finally:
        mp.undo()
    print("\n全部通过")
