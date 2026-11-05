"""
JSON 长期记忆写锁测试

覆盖：
1. 三个子进程在同一屏障后各写 20 次，验证跨进程文件锁不丢更新；
2. 写锁超时映射为 MemoryPersistenceError（不触发网络重试 / 熔断）；
3. Windows 读句柄导致 os.replace 短暂失败时自动重试。

运行：python tests/test_long_term_memory_lock.py
"""
import contextlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from context.long_term_memory import LongTermMemory
from utils.errors import MemoryPersistenceError

WORKER = """
import os
import sys
import time

sys.path.insert(0, {root!r})
from context.long_term_memory import LongTermMemory

start = sys.argv[2]
deadline = time.time() + 10
while not os.path.exists(start) and time.time() < deadline:
    time.sleep(0.01)
for _ in range(20):
    # 每次重新构造实例，模拟 Web / CLI 每次请求重新打开存储的读改写路径
    LongTermMemory("lockuser", sys.argv[1]).increment_query_count()
"""


def test_cross_process_increments_are_not_lost():
    code = WORKER.format(root=project_root)
    with tempfile.TemporaryDirectory() as tmp:
        start = Path(tmp, "start")
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", code, tmp, str(start)],
                cwd=project_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(3)
        ]
        start.write_text("go", encoding="utf-8")
        for process in processes:
            _, err = process.communicate(timeout=90)
            assert process.returncode == 0, err.decode("utf-8", "replace")

        memory = LongTermMemory("lockuser", tmp)
        assert memory.get_statistics()["total_queries"] == 60
    print("✓ 跨进程写锁不丢更新（3 进程 × 20 次，同一屏障启动）")


def test_lock_timeout_maps_to_persistence_error(monkeypatch, tmp_path):
    from utils.llm_resilience import is_retriable_error

    memory = LongTermMemory("timeout", str(tmp_path))

    @contextlib.contextmanager
    def stuck(path, timeout=2.0):
        raise TimeoutError("busy")
        yield

    monkeypatch.setattr("context.long_term_memory.file_lock", stuck)
    with pytest.raises(MemoryPersistenceError):
        memory.save_preference("home_location", "北京")
    assert is_retriable_error(MemoryPersistenceError("lock timeout")) is False
    print("✓ 写锁超时映射为 MemoryPersistenceError（不重试）")


def test_save_retries_transient_replace_errors(monkeypatch, tmp_path):
    memory = LongTermMemory("retry", str(tmp_path))
    real_replace = os.replace
    calls = {"count": 0}

    def flaky_replace(src, dst):
        calls["count"] += 1
        if calls["count"] <= 2:
            raise PermissionError("reader holds the file")
        return real_replace(src, dst)

    monkeypatch.setattr("context.long_term_memory.os.replace", flaky_replace)
    memory.increment_query_count()

    assert calls["count"] == 3
    reopened = LongTermMemory("retry", str(tmp_path))
    assert reopened.get_statistics()["total_queries"] == 1
    print("✓ os.replace 短暂失败自动重试")


if __name__ == "__main__":
    test_cross_process_increments_are_not_lost()
    print("\n全部通过（monkeypatch 用例需 pytest）")
