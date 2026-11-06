"""跨进程文件锁（JSON 长期记忆写操作）。

Windows 使用 ``msvcrt`` 字节锁，其他平台使用 ``fcntl.flock``；锁文件与数据文件
同目录（``<data>.lock``）。进程异常退出时由操作系统释放锁。

同一进程内的并发由 ``LongTermMemory`` 的按路径 RLock 串行化，本模块只负责
CLI 与 Web 等**不同进程**之间的互斥。
"""
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

if os.name == "nt":  # pragma: no cover - 平台分支在 Windows 上执行
    import msvcrt
else:  # pragma: no cover - 平台分支在非 Windows 上执行
    import fcntl

# 锁等待上限：单次写盘是毫秒级；Web 侧文件锁在事件循环上同步等待，
# 上限过大会在跨进程争用时冻结整个服务，这里限制为 2 秒
DEFAULT_TIMEOUT_SEC = 2.0


def _ensure_lockable(handle) -> None:
    """保证锁文件至少有一个字节（Windows 字节锁需要锁住有效区域）。"""
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()


def _acquire(handle, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"无法获取文件锁：{handle.name}") from None
            time.sleep(0.05)


def _release(handle) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
    except OSError:
        # 释放失败通常意味着句柄已失效；关闭句柄时操作系统仍会释放锁
        pass


@contextmanager
def file_lock(path: str, timeout: float = DEFAULT_TIMEOUT_SEC) -> Iterator[None]:
    """独占锁定 ``<path>.lock``；拿不到锁（含权限 / 路径故障）抛 ``TimeoutError``。"""
    lock_path = Path(f"{path}.lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+b")
    except OSError as exc:
        # 契约只承诺 TimeoutError：权限不足 / 路径不可写等 OSError 也要按
        # 「拿不到锁」处理，调用方（长期记忆）统一转成 MemoryPersistenceError；
        # 裸 OSError 会被重试层当成可重试的网络错误
        raise TimeoutError(f"无法获取文件锁：{lock_path}（{exc}）") from exc
    try:
        try:
            _ensure_lockable(handle)
        except OSError as exc:
            raise TimeoutError(f"无法获取文件锁：{lock_path}（{exc}）") from exc
        _acquire(handle, timeout)
        try:
            yield
        finally:
            _release(handle)
    finally:
        handle.close()
