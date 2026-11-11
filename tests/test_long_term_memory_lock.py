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


def test_save_fsyncs_before_atomic_replace(monkeypatch, tmp_path):
    """保存先 fsync 再 os.replace：掉电后已确认成功的写入必须可解析。"""
    memory = LongTermMemory("fsync-order", str(tmp_path))
    order: list[str] = []
    real_replace = os.replace
    real_fsync = os.fsync

    def spy_fsync(fd):
        order.append("fsync")
        return real_fsync(fd)

    def spy_replace(src, dst):
        order.append("replace")
        return real_replace(src, dst)

    monkeypatch.setattr("context.long_term_memory.os.fsync", spy_fsync)
    monkeypatch.setattr("context.long_term_memory.os.replace", spy_replace)
    memory.increment_query_count()
    assert "fsync" in order and "replace" in order
    assert order.index("fsync") < order.index("replace"), order
    print("✓ 保存先 fsync 再原子替换")


def test_load_closes_handle_before_parse(monkeypatch, tmp_path):
    """解析前关闭读句柄：Windows 上打开中的句柄会阻塞其它进程的替换。"""
    import builtins
    import json as json_module

    from context import long_term_memory as module

    memory = LongTermMemory("close-first", str(tmp_path))
    memory.save_preference("home_location", "北京")
    path = memory.db_path
    real_open = builtins.open
    opened = []

    class _Tracked:
        def __init__(self, handle):
            self._handle = handle

        def __getattr__(self, name):
            return getattr(self._handle, name)

        def __enter__(self):
            self._handle.__enter__()
            return self

        def __exit__(self, *args):
            return self._handle.__exit__(*args)

        def read(self, *args):
            return self._handle.read(*args)

        @property
        def closed(self):
            return self._handle.closed

    def tracking_open(file, *args, **kwargs):
        handle = real_open(file, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if str(file) == path and "r" in str(mode):
            wrapped = _Tracked(handle)
            opened.append(wrapped)
            return wrapped
        return handle

    real_loads = json_module.loads
    closed_states: list[bool] = []

    def spy_loads(text):
        closed_states.extend(handle.closed for handle in opened)
        return real_loads(text)

    monkeypatch.setattr(builtins, "open", tracking_open)
    monkeypatch.setattr(module.json, "loads", spy_loads)
    reopened = LongTermMemory("close-first", str(tmp_path))
    assert reopened.get_preference("home_location") == "北京"
    assert closed_states and all(closed_states), "解析时读句柄必须已关闭"
    print("✓ 读句柄在解析前关闭")


def test_default_lock_timeout_covers_network_guard(tmp_path):
    """默认锁等待必须覆盖锁内 Redis 守卫的网络等待（旧 2 秒不够）。"""
    import threading
    import time as time_module

    from utils import file_lock as lock_module

    assert lock_module.DEFAULT_TIMEOUT_SEC >= 5.0

    target = str(tmp_path / "slow-holder.json")
    held = threading.Event()
    release = threading.Event()

    def holder():
        with lock_module.file_lock(target, timeout=5):
            held.set()
            release.wait(5)

    thread = threading.Thread(target=holder)
    thread.start()
    assert held.wait(5)
    timer = threading.Timer(2.2, release.set)
    timer.start()
    begin = time_module.monotonic()
    with lock_module.file_lock(target):
        waited = time_module.monotonic() - begin
    thread.join(5)
    timer.cancel()
    assert waited >= 2.0, "默认等待应超过旧实现的 2 秒上限"
    print("✓ 默认文件锁等待覆盖网络守卫场景")


def test_file_lock_normalizes_oserror(monkeypatch, tmp_path):
    """权限 / 路径类 OSError 也按契约转成 TimeoutError（调用方只捕获它）。"""
    import builtins

    from utils import file_lock as lock_module

    real_open = builtins.open

    def denied(path, *args, **kwargs):
        if str(path).endswith(".lock"):
            raise PermissionError("denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", denied)
    with pytest.raises(TimeoutError):
        with lock_module.file_lock(str(tmp_path / "demo.json")):
            pass
    print("✓ 文件锁把 OSError 归一为 TimeoutError")


def test_file_lock_close_error_does_not_mask_timeout(monkeypatch, tmp_path):
    """初始字节写盘失败时 close 重抛的 OSError 不能覆盖 TimeoutError 契约。"""
    from utils import file_lock as lock_module

    class _FailingHandle:
        def close(self):
            raise OSError(28, "No space left on device")

    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: _FailingHandle())

    def fail_write(handle):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(lock_module, "_ensure_lockable", fail_write)
    with pytest.raises(TimeoutError):
        with lock_module.file_lock(str(tmp_path / "demo.json")):
            pass
    print("✓ close 重抛 OSError 不覆盖 TimeoutError 契约")


def _first_lock_timeout(monkeypatch):
    """让第一次 file_lock 抛 TimeoutError（模拟加载时等锁超时）。"""
    from context import long_term_memory as lt

    real_lock = lt.file_lock
    calls = {"count": 0}

    @contextlib.contextmanager
    def first_timeout(path, timeout=2.0):
        calls["count"] += 1
        if calls["count"] == 1:
            raise TimeoutError("busy")
        with real_lock(path, timeout=timeout):
            yield

    monkeypatch.setattr("context.long_term_memory.file_lock", first_timeout)
    return calls


def test_corrupt_file_backed_up_before_overwrite(monkeypatch, tmp_path):
    """加载等锁超时 → 首次写入持锁补备份，不能直接覆盖损坏文件。"""
    from context import long_term_memory as lt

    path = Path(tmp_path) / "pending.json"
    path.write_text("{not json", encoding="utf-8")
    calls = _first_lock_timeout(monkeypatch)

    memory = lt.LongTermMemory("pending", str(tmp_path))
    assert calls["count"] == 1
    assert memory._corrupt_pending is True
    assert memory.get_chat_history() == []

    memory.add_chat_message("user", "hi", "s1")
    backups = list(Path(tmp_path).glob("pending.json.corrupt-*"))
    assert len(backups) == 1, "损坏文件必须先备份再覆盖"
    assert backups[0].read_text(encoding="utf-8") == "{not json"
    assert memory._corrupt_pending is False
    repaired = LongTermMemory("pending", str(tmp_path))
    assert [item["content"] for item in repaired.get_chat_history()] == ["hi"]
    print("✓ 等锁超时后首次写入补备份损坏文件")


def test_corrupt_file_write_refused_when_backup_fails(monkeypatch, tmp_path):
    """备份始终失败（文件被其它进程持有）：拒绝覆盖，损坏文件保持原样。"""
    from context import long_term_memory as lt

    path = Path(tmp_path) / "blocked.json"
    path.write_text("{not json", encoding="utf-8")
    _first_lock_timeout(monkeypatch)

    memory = lt.LongTermMemory("blocked", str(tmp_path))

    def locked_replace(src, dst):
        raise PermissionError("held by another process")

    monkeypatch.setattr("context.long_term_memory.os.replace", locked_replace)
    with pytest.raises(MemoryPersistenceError) as info:
        memory.add_chat_message("user", "hi", "s1")
    assert "无法备份" in str(info.value)
    assert path.read_text(encoding="utf-8") == "{not json", "损坏文件不能被覆盖"
    assert memory._corrupt_pending is True, "失败后保持标记，下次写入重试备份"
    print("✓ 备份失败时拒绝覆盖损坏文件")


def test_file_lock_times_out_while_held(tmp_path):
    """锁被同一进程的另一个句柄持有时等待到超时；释放后可再次获取。"""
    import time

    from utils import file_lock as lock_module

    target = str(tmp_path / "held.json")
    with lock_module.file_lock(target):
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            with lock_module.file_lock(target, timeout=0.15):
                pass
        assert time.monotonic() - started >= 0.1, "应按 timeout 等待而不是立即失败"
    # 释放（_release 正常路径）后可重新获取同一个锁文件
    with lock_module.file_lock(target, timeout=0.5):
        pass
    print("✓ 锁竞争超时与释放后重入")


def test_file_lock_unlock_failure_is_swallowed(monkeypatch, tmp_path):
    """解锁失败只意味着句柄失效（关闭时由系统释放），不能覆盖业务异常。"""
    from utils import file_lock as lock_module

    if os.name == "nt":
        target_module = lock_module.msvcrt
        real_unlock = target_module.locking
        unlock_flag = target_module.LK_UNLCK

        def _flaky_locking(fd, mode, size):
            if mode == unlock_flag:
                raise OSError("handle gone")
            return real_unlock(fd, mode, size)

        monkeypatch.setattr(target_module, "locking", _flaky_locking)
    else:
        target_module = lock_module.fcntl
        real_unlock = target_module.flock
        unlock_flag = target_module.LOCK_UN

        def _flaky_flock(fd, op):
            if op & unlock_flag:
                raise OSError("handle gone")
            return real_unlock(fd, op)

        monkeypatch.setattr(target_module, "flock", _flaky_flock)

    with lock_module.file_lock(str(tmp_path / "unlock-fail.json")):
        pass  # 退出上下文不得抛异常
    print("✓ 解锁失败被吞掉（不会覆盖业务异常）")


if __name__ == "__main__":
    test_cross_process_increments_are_not_lost()
    print("\n全部通过（monkeypatch 用例需 pytest）")
