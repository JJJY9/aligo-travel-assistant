"""测试自身的静态一致性检查（无需 API Key、不访问网络）。

`python tests/<file>.py` 直接运行时，各文件的 `__main__` 循环会以
`case(mp)` 调用用例；若某个用例写成 `def test_x():`，直接运行会
TypeError，CI 的 `mypy .` 也会报 call-arg。本文件用 AST 静态检查所有
tests/test_*.py，提前拦住这类签名不匹配。

运行：python tests/test_harness_consistency.py
"""
import ast
import os
import sys
from pathlib import Path

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

TESTS_DIR = Path(__file__).resolve().parent


def _main_loop_case_names(tree: ast.AST) -> list[list[str]]:
    """收集 `for case in (...)` 循环里列出的用例名。"""
    loops: list[list[str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.For):
            continue
        target = node.target
        if not (isinstance(target, ast.Name) and target.id == "case"):
            continue
        if not isinstance(node.iter, ast.Tuple):
            continue
        names = [
            element.id
            for element in node.iter.elts
            if isinstance(element, ast.Name)
        ]
        if names:
            loops.append(names)
    return loops


def _test_function_params(tree: ast.AST) -> dict[str, list[str]]:
    params: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            args = node.args
            params[node.name] = [
                argument.arg
                for argument in args.posonlyargs + args.args
            ]
    return params


def test_main_loops_pass_monkeypatch():
    """__main__ 循环里的用例必须接受至少一个位置参数（monkeypatch）。"""
    problems: list[str] = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):  # 语法问题交给 ruff / pytest
            continue
        params = _test_function_params(tree)
        for names in _main_loop_case_names(tree):
            for name in names:
                positional = params.get(name)
                if positional is None:
                    continue
                if not positional:
                    problems.append(
                        f"{path.name}: {name} 被 __main__ 循环以 case(mp) "
                        "调用但没有参数"
                    )
    assert not problems, "\n".join(problems)
    print("✓ __main__ 循环用例签名一致（均接受 monkeypatch）")


if __name__ == "__main__":
    test_main_loops_pass_monkeypatch()
    print("\n全部通过")
