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

import pytest

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
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.AnnAssign):
            # 带类型注解的声明（``_DIRECT_RUN_SKIP: dict[str, str] = {…}``）
            target, value = node.target, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        if not (
            isinstance(target, ast.Name)
            and target.id == name
            and isinstance(value, ast.Dict)
        ):
            continue
        out: dict[str, str] = {}
        for key, item in zip(value.keys, value.values):
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and isinstance(item, ast.Constant)
                and isinstance(item.value, str)
            ):
                out[key.value] = item.value
        return out
    return {}


def test_manual_lists_cover_all_tests():
    """手写 `for case in (…, …)` 清单必须覆盖文件里所有 test_* 函数：
    漏登记会让直接运行打印「全部通过」却从不执行这些用例。确需专用
    夹具的用例在模块级 `_DIRECT_RUN_SKIP: dict[str, str] = {名称: 原因}` 中显式声明。"""
    problems: list[str] = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        block = _find_main_block(tree)
        if block is None:
            continue
        # 自动发现清单（globals() 循环）与 asyncio.run(main()) 委托
        # 无法静态枚举，天然执行全部用例
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "items"
            and isinstance(node.func.value, ast.Call)
            and isinstance(node.func.value.func, ast.Name)
            and node.func.value.func.id == "globals"
            for node in ast.walk(block)
        ) or any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "asyncio"
            and any(
                isinstance(argument, ast.Call)
                and isinstance(argument.func, ast.Name)
                and argument.func.id == "main"
                for argument in node.args
            )
            for node in ast.walk(block)
        ):
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
        # 跳过原因必须具体：零参数 / 仅 monkeypatch 的用例可以直接登记，
        # 通用「暂未纳入」会让清单静默降覆盖
        funcs = {
            node.name: [a.arg for a in node.args.posonlyargs + node.args.args]
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        }
        for name, reason in skip.items():
            params = funcs.get(name, [])
            # caplog 不能作为跳过理由：直跑提供 tests/_caplog_shim.py
            # 替身，跳过会让日志断言在直跑清单上零覆盖（假绿）
            fixture_justified = any(
                fixture in params
                for fixture in ("tmp_path", "capsys", "request")
            ) or any(
                # 原因里点名的夹具参数（如 hermetic_kb_path）同样算具体
                param in reason
                for param in params
                if param not in {"monkeypatch", "caplog"}
            )
            real_service = "真实" in reason and any(
                tag in reason
                for tag in (
                    "PostgreSQL",
                    "Redis",
                    "DSN",
                    "模型",
                    "知识库",
                    "Milvus",
                    "网络",
                    "搜索",
                    "LLM",
                    "API Key",
                )
            )
            if fixture_justified or real_service:
                continue
            problems.append(
                f"{path.name}: {name} 的跳过原因不可接受（{reason}）"
            )
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


def _monkeypatch_reuse_problems(source: str, name: str) -> list[str]:
    """分析 ``__main__`` 块里「复用已撤销 MonkeyPatch」问题。

    补丁变量名不限于 ``mp``（``_mp`` / ``mp2`` 等同理）：凡是从
    ``*.MonkeyPatch()`` 赋值的名字都按同一状态机跟踪。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    problems: list[str] = []
    block = _find_main_block(tree)
    if block is None:
        return problems
    events: list[tuple[int, int, int, str, str]] = []
    patch_vars: set[str] = set()
    for node in ast.walk(block):
        lineno = int(getattr(node, "lineno", 0))
        col = int(getattr(node, "col_offset", 0))
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            value = node.value
        if (
            target is not None
            and isinstance(target, ast.Name)
            and value is not None
            and isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute)
            and value.func.attr == "MonkeyPatch"
        ):
            patch_vars.add(target.id)
            # 事件按 (行, 列) 还原源码顺序；rank 仅作同列兜底
            events.append((lineno, col, 0, "new", target.id))
            continue
        if not isinstance(node, ast.Call):
            if (
                isinstance(node, ast.For)
                and isinstance(node.target, ast.Name)
                and node.target.id == "case"
            ):
                # 循环清单：case(mp) 的补丁实例必须在循环体内新建；
                # 循环外创建会让每轮用例共享同一补丁作用域（跨轮泄漏）
                inner_new = {
                    target.id
                    for stmt in ast.walk(node)
                    if isinstance(stmt, ast.Assign)
                    and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)
                    for target in [stmt.targets[0]]
                }
                for stmt in ast.walk(node):
                    if not (
                        isinstance(stmt, ast.Call)
                        and isinstance(stmt.func, ast.Name)
                        and stmt.func.id == "case"
                    ):
                        continue
                    for arg in stmt.args:
                        if (
                            isinstance(arg, ast.Name)
                            and arg.id in patch_vars
                            and arg.id not in inner_new
                        ):
                            problems.append(
                                f"{name}:{stmt.lineno} case({arg.id}) 在循环内"
                                "复用循环外创建的 MonkeyPatch：每轮用例"
                                "共享同一补丁作用域，补丁会跨轮泄漏"
                            )
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "undo"
            and isinstance(func.value, ast.Name)
            and func.value.id in patch_vars
        ):
            events.append(
                (int(node.lineno), int(node.col_offset), 1, "undo", func.value.id)
            )
        elif isinstance(func, ast.Name) and (
            func.id.startswith("test_") or func.id == "case"
        ):
            arg_values: list[ast.expr] = list(node.args) + [
                keyword.value for keyword in node.keywords
            ]
            for arg in arg_values:
                if isinstance(arg, ast.Name) and arg.id in patch_vars:
                    events.append(
                        (
                            int(node.lineno),
                            int(node.col_offset),
                            2,
                            f"call:{func.id}",
                            arg.id,
                        )
                    )
                elif (
                    isinstance(arg, ast.Call)
                    and isinstance(arg.func, ast.Attribute)
                    and arg.func.attr == "MonkeyPatch"
                ):
                    # 内联新建的实例没有任何引用可撤销：补丁会泄漏到
                    # 后续用例 / 进程结束
                    problems.append(
                        f"{name}:{arg.lineno} {func.id} 收到内联新建的 "
                        "pytest.MonkeyPatch()：无法撤销，补丁会泄漏"
                    )
    events.sort()
    states: dict[str, str] = {var: "none" for var in patch_vars}
    last_call: dict[str, str] = {}
    for lineno, _col, _rank, kind, var in events:
        if kind in ("new", "undo"):
            states[var] = kind
            last_call.pop(var, None)
        elif kind.startswith("call:"):
            if states.get(var) != "new":
                problems.append(
                    f"{name}:{lineno} {kind[5:]} 复用了已撤销的 "
                    f"MonkeyPatch（{var}）"
                )
            elif var in last_call:
                # 一个补丁实例跨多个用例：后一个用例运行在前一个的补丁
                # 泄漏作用域里（全局模块替身如 asyncio.sleep / shutil
                # 会一直生效），直跑清单会互相污染
                problems.append(
                    f"{name}:{lineno} {kind[5:]} 与上一个用例共享同一 "
                    f"MonkeyPatch（{var}）且未撤销：补丁会泄漏到下一个用例"
                )
            else:
                last_call[var] = kind[5:]
    for var, state in states.items():
        if state == "new":
            # 清单跑完仍未撤销：全局模块替身泄漏到进程末尾（py3.14 -
            # 等后续语句 / 测试清理都可能被污染）
            problems.append(
                f"{name}: 补丁实例 {var} 创建后从未撤销（补丁会泄漏）"
            )
    return problems


def test_main_blocks_do_not_reuse_undone_monkeypatch():
    """__main__ 里 `test_x(mp)` 之前必须有一个晚于最近一次
    `mp.undo()` 的 `mp = pytest.MonkeyPatch()`：复用已撤销的补丁实例
    会泄漏补丁（注册到不会再 undo 的实例上）并重复执行用例。"""
    problems: list[str] = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        problems.extend(_monkeypatch_reuse_problems(source, path.name))
    assert not problems, chr(10).join(problems)
    print("✓ __main__ 不复用已撤销的 MonkeyPatch")


def test_monkeypatch_reuse_check_covers_renamed_vars():
    """自检不能只认变量名 ``mp``：``_mp`` / ``mp2`` 的复用同样要检出，
    重新创建后再调用不算复用（内联新建的实例天然安全）。"""
    reused = (
        "import pytest\n"
        "if __name__ == '__main__':\n"
        "    _mp = pytest.MonkeyPatch()\n"
        "    try:\n"
        "        test_a(_mp)\n"
        "    finally:\n"
        "        _mp.undo()\n"
        "    test_a(_mp)\n"
    )
    problems = _monkeypatch_reuse_problems(reused, "synthetic.py")
    assert problems and "_mp" in problems[0], problems

    recreated = (
        "import pytest\n"
        "if __name__ == '__main__':\n"
        "    mp2 = pytest.MonkeyPatch()\n"
        "    try:\n"
        "        test_a(mp2)\n"
        "    finally:\n"
        "        mp2.undo()\n"
        "    mp2 = pytest.MonkeyPatch()\n"
        "    try:\n"
        "        test_a(mp2)\n"
        "    finally:\n"
        "        mp2.undo()\n"
    )
    assert _monkeypatch_reuse_problems(recreated, "synthetic.py") == []

    shared = (
        "import pytest\n"
        "if __name__ == '__main__':\n"
        "    mp = pytest.MonkeyPatch()\n"
        "    try:\n"
        "        test_a(mp)\n"
        "        test_b(mp)\n"
        "    finally:\n"
        "        mp.undo()\n"
    )
    problems = _monkeypatch_reuse_problems(shared, "synthetic.py")
    assert problems and "共享同一" in problems[0], problems

    same_line = (
        "import pytest\n"
        "if __name__ == '__main__':\n"
        "    mp = pytest.MonkeyPatch(); test_a(mp)\n"
        "    mp.undo()\n"
    )
    assert _monkeypatch_reuse_problems(same_line, "synthetic.py") == []

    # 同行「调用后撤销」是合法顺序（按列号还原，undo 不能恒排在前）
    call_then_undo = (
        "import pytest\n"
        "if __name__ == '__main__':\n"
        "    mp = pytest.MonkeyPatch()\n"
        "    test_a(mp); mp.undo()\n"
    )
    assert (
        _monkeypatch_reuse_problems(call_then_undo, "synthetic.py") == []
    )

    # 同行「撤销后调用」必须报问题
    undo_then_call = (
        "import pytest\n"
        "if __name__ == '__main__':\n"
        "    mp = pytest.MonkeyPatch()\n"
        "    test_a(mp)\n"
        "    mp.undo(); test_a(mp)\n"
    )
    problems = _monkeypatch_reuse_problems(undo_then_call, "synthetic.py")
    assert problems and "复用了已撤销" in problems[0], problems

    # 内联新建无法撤销：必须报问题（此前被当作「天然安全」）
    inline = (
        "import pytest\n"
        "if __name__ == '__main__':\n"
        "    test_a(pytest.MonkeyPatch())\n"
    )
    problems = _monkeypatch_reuse_problems(inline, "synthetic.py")
    assert problems and "内联新建" in problems[0], problems

    # 创建后从未撤销同样必须报问题（全局替身泄漏到进程末尾）
    never_undone = (
        "import pytest\n"
        "if __name__ == '__main__':\n"
        "    mp = pytest.MonkeyPatch()\n"
        "    test_a(mp)\n"
    )
    problems = _monkeypatch_reuse_problems(never_undone, "synthetic.py")
    assert problems and "从未撤销" in problems[0], problems
    # 循环清单的 case(mp) 同样是补丁使用：mp 在循环外创建、多轮复用必须报
    loop_reuse = (
        "import pytest\n"
        "if __name__ == '__main__':\n"
        "    mp = pytest.MonkeyPatch()\n"
        "    for case in (test_a, test_b):\n"
        "        case(mp)\n"
    )
    problems = _monkeypatch_reuse_problems(loop_reuse, "synthetic.py")
    assert problems and "共享同一" in problems[0], problems

    # 每轮新建 / 撤销的安全循环写法不报问题
    loop_ok = (
        "import pytest\n"
        "if __name__ == '__main__':\n"
        "    for case in (test_a, test_b):\n"
        "        mp = pytest.MonkeyPatch()\n"
        "        try:\n"
        "            case(mp)\n"
        "        finally:\n"
        "            mp.undo()\n"
    )
    assert _monkeypatch_reuse_problems(loop_ok, "synthetic.py") == []
    print("✓ MonkeyPatch 复用自检覆盖任意变量名")


def test_direct_run_files_install_tmp_registry():
    """直跑文件若用 ``tempfile.mkdtemp()`` 自建目录，__main__ 必须接入
    临时目录登记表（直接调用 ``_tmp_registry.install()``，或经共享的
    ``_stdio.ensure_utf8()``——它已顺带安装）。否则直跑结束后
    含记忆 JSON 的目录会永久留在系统临时目录。"""
    marker = 'if __name__ == "__main__":'
    problems: list[str] = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        if marker not in text or "tempfile.mkdtemp(" not in text:
            continue
        block = text[text.rfind(marker):]
        if "_tmp_registry.install()" in block or "_stdio.ensure_utf8()" in block:
            continue
        problems.append(path.name)
    assert not problems, "缺临时目录登记：" + ", ".join(problems)
    print("✓ 直跑清单接入临时目录登记")



def test_direct_run_kb_fixture_is_isolated():
    """直跑清单的 kb_fixture 块必须接入 ``_direct_run_kb`` 隔离：只替换
    ``_existing_knowledge_rows`` / 探针时，``python tests/xxx.py`` 的
    规划路径仍会打开真实 Milvus，结论与 pytest / CI（无库）不一致。"""
    problems: list[str] = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        if "kb_fixture.setattr(" not in text:
            continue
        if "_direct_run_kb.isolate(" not in text:
            problems.append(path.name)
    assert not problems, "缺知识库路径隔离：" + ", ".join(problems)
    print("✓ 直跑 kb_fixture 接入知识库路径隔离")


def test_direct_run_kb_isolate_patches_path():
    """``_direct_run_kb.isolate`` 必须真正把知识库路径指向缺失文件并在
    ``undo`` 后恢复：只查源码文本的守卫在 helper 失效 / 被改名时仍会
    全绿，这里直接运行一次确认隔离生效。"""
    import _direct_run_kb

    from utils import knowledge_admin

    original = knowledge_admin.knowledge_db_path
    fixture = pytest.MonkeyPatch()
    try:
        missing = _direct_run_kb.isolate(fixture)
        patched = Path(str(knowledge_admin.knowledge_db_path()))
        assert patched == missing, (patched, missing)
        assert not patched.exists()
    finally:
        fixture.undo()
    assert knowledge_admin.knowledge_db_path is original
    print("✓ 直跑知识库隔离 helper 实际生效")


if __name__ == "__main__":
    import _tmp_registry

    _tmp_registry.install()
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
    test_main_blocks_do_not_reuse_undone_monkeypatch()
    test_monkeypatch_reuse_check_covers_renamed_vars()
    test_direct_run_kb_fixture_is_isolated()
    test_direct_run_kb_isolate_patches_path()
    test_direct_run_files_install_tmp_registry()
    print("\n全部通过")
