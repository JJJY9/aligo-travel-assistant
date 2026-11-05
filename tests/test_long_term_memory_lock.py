"""
JSON 长期记忆跨进程写锁测试

两个子进程同时向同一用户 JSON 写入，验证操作系统文件锁避免丢更新。
运行：python tests/test_long_term_memory_lock.py
"""
import os
import subprocess
import sys
import tempfile

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from context.long_term_memory import LongTermMemory

WORKER = """
import sys
sys.path.insert(0, {root!r})
from context.long_term_memory import LongTermMemory

memory = LongTermMemory("lockuser", sys.argv[1])
for _ in range(25):
    memory.increment_query_count()
"""


def test_cross_process_increments_are_not_lost():
    code = WORKER.format(root=project_root)
    with tempfile.TemporaryDirectory() as tmp:
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", code, tmp],
                cwd=project_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(2)
        ]
        for process in processes:
            _, err = process.communicate(timeout=90)
            assert process.returncode == 0, err.decode("utf-8", "replace")

        memory = LongTermMemory("lockuser", tmp)
        assert memory.get_statistics()["total_queries"] == 50
    print("✓ 跨进程写锁不丢更新（2 进程 × 25 次）")


if __name__ == "__main__":
    test_cross_process_increments_are_not_lost()
    print("\n全部通过")
