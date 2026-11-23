"""执行检查点存储（v2.0 持久化执行的基础设施）。

按幂等键把一轮编排的中间结果落盘（默认 ``data/runtime/checkpoints``），
供进程重启 / 失败重试时复用已完成的子智能体结果，避免重复调用模型。

设计：
- 每个键一个 JSON 文件（文件名 = SHA-256(key)），内容含原始 key 便于排查；
- 原子写：同目录临时文件 → fsync → ``os.replace``；
- TTL + 最大条目数：读取过期文件视为不存在并清理，写入后按 mtime 淘汰；
- 损坏文件按不存在处理并移出（不阻断调用方）；
- 进程内互斥保证同进程并发写同一键不互相覆盖；跨进程写同一键不在本层
  承诺（多实例部署应按用户 / 请求分片或换 Redis 实现）。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_TTL_SEC = 3600.0
DEFAULT_MAX_ENTRIES = 256


class CheckpointStore:
    """基于文件的任务检查点存储（单进程安全）。"""

    def __init__(
        self,
        root: str | Path = "data/runtime/checkpoints",
        ttl_sec: float = DEFAULT_TTL_SEC,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self.root = Path(root)
        self.ttl_sec = max(0.0, float(ttl_sec))
        self.max_entries = max(1, int(max_entries))
        self._lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)

    # ---------- 内部工具 ----------

    @staticmethod
    def _digest(key: str) -> str:
        return hashlib.sha256(str(key).encode("utf-8")).hexdigest()

    def _path(self, key: str) -> Path:
        return self.root / f"{self._digest(key)}.json"

    def _expired(self, path: Path, now: float) -> bool:
        if self.ttl_sec <= 0:
            return False
        try:
            return (now - path.stat().st_mtime) > self.ttl_sec
        except OSError:
            return True

    def _read(self, path: Path) -> dict[str, Any] | None:
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError):
            # 损坏 / 无法读取：移出，避免每次重试都撞同一个坏文件
            try:
                path.unlink()
            except OSError:
                pass
            return None
        return data if isinstance(data, dict) else None

    def _prune(self) -> None:
        now = time.time()
        entries: list[tuple[float, Path]] = []
        for path in self.root.glob("*.json"):
            if self._expired(path, now):
                try:
                    path.unlink()
                except OSError:
                    continue
                continue
            try:
                entries.append((path.stat().st_mtime, path))
            except OSError:
                continue
        entries.sort()
        while len(entries) > self.max_entries:
            _, path = entries.pop(0)
            try:
                path.unlink()
            except OSError:
                pass

    def _load_unlocked(self, key: str) -> dict[str, Any] | None:
        """调用方已持有 ``self._lock`` 时使用。"""
        path = self._path(key)
        if not path.exists():
            return None
        if self._expired(path, time.time()):
            try:
                path.unlink()
            except OSError:
                pass
            return None
        return self._read(path)

    # ---------- 公共接口 ----------

    def load(self, key: str) -> dict[str, Any] | None:
        """读取检查点；不存在 / 过期 / 损坏返回 None。"""
        with self._lock:
            return self._load_unlocked(key)

    def save(self, key: str, state: dict[str, Any]) -> None:
        """原子写入检查点（整份状态）。"""
        payload = {"key": str(key), **state}
        path = self._path(key)
        with self._lock:
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=self.root
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, path)
            except Exception:
                try:
                    Path(temp_name).unlink()
                except OSError:
                    pass
                raise
            self._prune()

    def update_step(self, key: str, step: str, result: Any) -> None:
        """记录某个子步骤的结果（读-改-写在进程锁内完成）。"""
        with self._lock:
            state = self._load_unlocked(key) or {}
            steps = state.get("steps")
            if not isinstance(steps, dict):
                steps = {}
            steps[str(step)] = result
            state["steps"] = steps
            # 已持有进程锁：直接构造 payload 落盘（不能再调用 load）
            payload = {"key": str(key), **state}
            path = self._path(key)
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=self.root
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, path)
            except Exception:
                try:
                    Path(temp_name).unlink()
                except OSError:
                    pass
                raise
            self._prune()

    def clear(self, key: str) -> None:
        with self._lock:
            try:
                self._path(key).unlink()
            except OSError:
                pass

    def __len__(self) -> int:
        with self._lock:
            return len(list(self.root.glob("*.json")))
