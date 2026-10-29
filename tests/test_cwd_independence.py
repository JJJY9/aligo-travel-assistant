"""
CWD 无关性回归测试（Issue #5，无需 API Key）

验证从项目根目录以外的位置运行时：
1. LazyAgentRegistry 仍能发现全部 6 个技能；
2. LongTermMemory 的相对存储路径锚定项目根，而不是运行时 CWD。

运行：python tests/test_cwd_independence.py
"""
import os
import subprocess
import sys
import tempfile
import uuid

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)


def test_registry_from_other_cwd():
    """从系统临时目录运行注册器，应仍发现 6 个技能"""
    code = (
        f"import sys; sys.path.insert(0, r'{project_root}');"
        "from agents.lazy_agent_registry import LazyAgentRegistry;"
        "r = LazyAgentRegistry(model=None, cache={});"
        "print(len(r._skill_map))"
    )

    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tempfile.gettempdir(),
        capture_output=True, text=True, timeout=120,
    )
    count = (out.stdout or "").strip().splitlines()[-1] if out.stdout else ""
    assert count == "6", (
        f"从其它目录运行时发现技能数 = {count!r}，期望 '6'（Issue #5 复现）。\n"
        f"stderr: {out.stderr[-300:]}"
    )
    print("✓ 其它 CWD 下技能发现正常（6 个）")


def test_memory_path_anchored_to_project_root():
    """相对存储路径应解析到项目根目录，而不是运行时 CWD"""
    from context.long_term_memory import LongTermMemory

    other_cwd = tempfile.mkdtemp(prefix="chailv_cwd_")
    old_cwd = os.getcwd()
    os.chdir(other_cwd)  # 模拟从其它目录启动
    try:
        memory = LongTermMemory(f"cwd-probe-{uuid.uuid4().hex[:8]}", "data/memory")
        try:
            assert os.path.abspath(memory.db_path).startswith(os.path.abspath(project_root)), (
                f"db_path 应锚定项目根，实际: {memory.db_path}"
            )
            # 初始化不落盘，触发一次写入后文件应出现在项目根下
            memory.add_chat_message("user", "ping")
            assert os.path.exists(memory.db_path), "存储文件应在项目根 data/memory 下创建"
            print("✓ 长期记忆相对路径锚定项目根")
        finally:
            if os.path.exists(memory.db_path):
                os.remove(memory.db_path)
    finally:
        os.chdir(old_cwd)


if __name__ == "__main__":
    test_registry_from_other_cwd()
    test_memory_path_anchored_to_project_root()
    print("\n全部通过")
