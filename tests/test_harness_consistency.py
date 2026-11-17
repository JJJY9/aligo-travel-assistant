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
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        ):
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


def test_main_blocks_guard_utf8_output():
    """每个直跑清单的 __main__ 必须切换 stdout/stderr 到 UTF-8（或调用
    共享守卫 tests/_stdio.py）：GBK 控制台下第一个 ✓ 即
    UnicodeEncodeError，后续用例全部不执行却看起来「跑过」。"""
    marker = 'if __name__ == "__main__":'
    problems: list[str] = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        if marker not in text:
            continue
        block = text[text.rfind(marker):]
        guarded = "_stdio.ensure_utf8()" in block or (
            "reconfigure(" in block and 'encoding="utf-8"' in block
        )
        if not guarded:
            problems.append(path.name)
    assert not problems, "缺 UTF-8 守卫：" + ", ".join(problems)
    print("✓ 直跑清单均切换 UTF-8 输出")

def _find_main_block(tree: ast.Module):
    """定位模块顶层的 if __name__ == "__main__" 节点。"""
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if isinstance(test, ast.Compare):
            left = test.left
            if (
                isinstance(left, ast.Name)
                and left.id == "__name__"
                and test.comparators
                and isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value == "__main__"
            ):
                return node
    return None


def _module_literal_dict(tree: ast.Module, name: str) -> dict[str, str]:
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not (isinstance(target, ast.Name) and target.id == name):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        out: dict[str, str] = {}
        for key, value in zip(node.value.keys, node.value.values):
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                out[key.value] = value.value
        return out
    return {}


def test_manual_lists_cover_all_tests():
    """手写 `for case in (…, …)` 清单必须覆盖文件里所有 test_* 函数：
    漏登记会让直接运行打印「全部通过」却从不执行这些用例。确需专用
    夹具的用例在模块级 `_DIRECT_RUN_SKIP = {名称: 原因}` 中显式声明。"""
    problems: list[str] = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        block = _find_main_block(tree)
        if block is None:
            continue
        tuple_loops = [
            node
            for node in ast.walk(block)
            if isinstance(node, ast.For)
            and isinstance(node.target, ast.Name)
            and node.target.id == "case"
            and isinstance(node.iter, ast.Tuple)
        ]
        if not tuple_loops:
            continue
        tests = [
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        ]
        refs = {node.id for node in ast.walk(block) if isinstance(node, ast.Name)}
        skip = _module_literal_dict(tree, "_DIRECT_RUN_SKIP")
        missing = [
            name for name in tests if name not in refs and name not in skip
        ]
        if missing:
            problems.append(f"{path.name}: {', '.join(missing)}")
    assert not problems, chr(10).join(problems)
    print("✓ 手写直跑清单覆盖全部用例（或显式跳过）")


def test_case_loops_do_not_call_tests_after_undo():
    """`for case in (…)` 循环体里除 `case(mp)` 外不能再调用其它用例：
    例如在 `mp.undo()` 之后调用 `test_x(mp)` 会让补丁登记到已撤销的
    MonkeyPatch 上，既不会撤销、又每轮重复执行，还会泄漏到后续用例。"""
    problems: list[str] = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        block = _find_main_block(tree)
        if block is None:
            continue
        for loop in ast.walk(block):
            if not (
                isinstance(loop, ast.For)
                and isinstance(loop.target, ast.Name)
                and loop.target.id == "case"
                and isinstance(loop.iter, ast.Tuple)
            ):
                continue
            for stmt in loop.body:
                for node in ast.walk(stmt):
                    if not isinstance(node, ast.Call):
                        continue
                    func = node.func
                    if (
                        isinstance(func, ast.Name)
                        and func.id.startswith("test_")
                    ):
                        problems.append(
                            f"{path.name}: 循环体调用 {func.id}()"
                            "（应加入手写清单）"
                        )
    assert not problems, chr(10).join(problems)
    print("✓ case 循环内不重复调用用例")



if __name__ == "__main__":
    import sys

    # Windows 控制台 / 重定向管道默认 GBK：✓ 等字符会抛 UnicodeEncodeError，
    # 断言通过也会以 traceback 退出；先切 UTF-8（不支持时保持原状）
    try:
        for _stream in (sys.stdout, sys.stderr):
            _reconfigure = getattr(_stream, "reconfigure", None)
            if callable(_reconfigure):
                _reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        pass
    test_main_loops_pass_monkeypatch()
    test_main_blocks_guard_utf8_output()
    test_manual_lists_cover_all_tests()
    test_case_loops_do_not_call_tests_after_undo()
    print("\n全部通过")
