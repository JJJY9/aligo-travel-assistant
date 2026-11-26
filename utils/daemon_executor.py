"""守护线程池：进程退出不被在途后台任务拖住。

``ThreadPoolExecutor`` 默认创建非守护 worker，并把它们登记进
``concurrent.futures.thread._threads_queues``：解释器退出时 atexit 的
``_python_exit`` 会对登记线程逐个 ``join()``（daemon 标记不豁免），
分钟级的导入 / 天气 / 搜索请求会让停服被拖到请求结束。

这里沿用 CPython 内部线程创建逻辑（3.10-3.12 稳定），只把线程标记为
daemon 且跳过登记，进程退出时在途任务随守护线程直接结束。只在「结果
仅用于当轮响应、没有退出前必须刷盘的副作用」的后台任务上使用。
"""
from __future__ import annotations

import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures.thread import _worker
from typing import Any


class DaemonThreadPoolExecutor(ThreadPoolExecutor):
    """worker 为守护线程且不登记 atexit join 的线程池。"""

    def _adjust_thread_count(self) -> None:  # type: ignore[override]
        if self._idle_semaphore.acquire(timeout=0):  # type: ignore[attr-defined]
            return

        def weakref_cb(_: Any, q: Any = self._work_queue) -> None:  # type: ignore[attr-defined]
            q.put(None)

        num_threads = len(self._threads)  # type: ignore[attr-defined]
        if num_threads < self._max_workers:  # type: ignore[attr-defined]
            thread = threading.Thread(
                name=f"{self._thread_name_prefix or self}_{num_threads}",
                target=_worker,
                args=(
                    weakref.ref(self, weakref_cb),
                    self._work_queue,  # type: ignore[attr-defined]
                    self._initializer,  # type: ignore[attr-defined]
                    self._initargs,  # type: ignore[attr-defined]
                ),
                daemon=True,
            )
            thread.start()
            self._threads.add(thread)  # type: ignore[attr-defined]
            # 刻意不登记到 concurrent.futures.thread._threads_queues：atexit
            # 的 _python_exit 会对登记过的线程逐个 join（daemon=True 不豁免）
