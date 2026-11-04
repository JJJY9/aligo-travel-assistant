"""进程内请求指标（无第三方依赖）。

中间件为每个请求记录计数与耗时；``GET /api/metrics`` 返回快照，便于本地观测
与压测。多进程 / 多实例部署时各进程独立统计（与限流一致，见 README 已知限制）。
"""
import re
import threading
import time
from collections import Counter
from typing import Any

# 路径基数上限：路径可能包含会话 ID 等高基数片段，超出后归入 __other__
MAX_PATHS = 100
_OTHER_PATH = "__other__"


class RequestMetrics:
    """线程安全的请求计数 / 耗时聚合。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_at = time.time()
        self._total = 0
        self._by_status: Counter[int] = Counter()
        self._by_method: Counter[str] = Counter()
        self._by_path: Counter[str] = Counter()
        self._duration_sum_ms = 0.0
        self._duration_max_ms = 0.0

    def record(self, method: str, path: str, status: int, elapsed_ms: float) -> None:
        # 路径由 uvicorn percent-decode，可能含控制字符；指标 key 里清理掉
        safe_path = re.sub(r"[\x00-\x1f\x7f]", "?", str(path))[:200]
        with self._lock:
            self._total += 1
            self._by_status[int(status)] += 1
            self._by_method[str(method)[:10] or "UNKNOWN"] += 1
            if len(self._by_path) < MAX_PATHS or safe_path in self._by_path:
                self._by_path[safe_path] += 1
            else:
                self._by_path[_OTHER_PATH] += 1
            self._duration_sum_ms += max(0.0, float(elapsed_ms))
            self._duration_max_ms = max(self._duration_max_ms, float(elapsed_ms))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            total = self._total
            return {
                "uptime_sec": round(time.time() - self._started_at, 1),
                "requests_total": total,
                "by_status": {str(key): value for key, value in sorted(self._by_status.items())},
                "by_method": dict(self._by_method.most_common()),
                "by_path": dict(self._by_path.most_common(20)),
                "avg_duration_ms": round(self._duration_sum_ms / total, 2) if total else 0.0,
                "max_duration_ms": round(self._duration_max_ms, 2),
            }

    def reset(self) -> None:
        """清空统计（测试或运维需要时使用）。"""
        with self._lock:
            self._started_at = time.time()
            self._total = 0
            self._by_status.clear()
            self._by_method.clear()
            self._by_path.clear()
            self._duration_sum_ms = 0.0
            self._duration_max_ms = 0.0


REQUEST_METRICS = RequestMetrics()
