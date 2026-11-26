"""执行检查点存储（v2.0 持久化执行的基础设施）。

按幂等键把一轮编排的中间结果落盘（默认数据目录下的
``runtime/checkpoints``），供进程重启 / 失败重试时复用已完成的子智能体
结果，避免重复调用模型。

设计：
- 每个键一个 JSON 文件（文件名 = SHA-256(key)），内容只存键哈希：键里
  带用户 / 会话标识，落盘原始值等于把个人数据写进运行期文件；
- 原子写：同目录临时文件 → fsync → ``os.replace``；
- TTL + 最大条目数：读取过期文件视为不存在并清理，写入后按 mtime 淘汰；
- 损坏文件按不存在处理并移出（不阻断调用方）；
- 进程内互斥保证同进程并发写同一键不互相覆盖；跨进程写同一键不在本层
  承诺（多实例部署应按用户 / 请求分片或换 Redis 实现）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from utils.atomic_write import fsync_parent_dir, replace_with_retry
from utils.paths import default_data_root, package_root

logger = logging.getLogger(__name__)

DEFAULT_TTL_SEC = 3600.0
DEFAULT_MAX_ENTRIES = 256


class CheckpointStore:
    """基于文件的任务检查点存储（单进程安全）。"""

    def __init__(
        self,
        root: str | Path | None = None,
        ttl_sec: float = DEFAULT_TTL_SEC,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        # 默认路径必须跟长期数据目录走：此前是 CWD 相对字面量，从其它目录
        # 启动时检查点散落在任意工作目录，且默认路径不在 .gitignore 里
        explicit_root = root is not None
        self.root = (
            Path(root) if root is not None
            else default_data_root() / "runtime" / "checkpoints"
        )
        self.ttl_sec = max(0.0, float(ttl_sec))
        self.max_entries = max(1, int(max_entries))
        self._lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._clean_temp_files()
            self._prune()
        if not explicit_root:
            # 旧版本默认目录是 CWD 相对路径：升级后这些文件不会再被读取，
            # 但含原始用户 / 会话键；仅当使用默认根时清理已过 TTL 的残片
            self._cleanup_legacy_roots()

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

    def _clean_temp_files(self) -> None:
        """清理崩溃 / 断电遗留的临时文件（调用方已持有 ``self._lock``）。"""
        cutoff = time.time() - max(self.ttl_sec, 60.0)
        for path in self.root.glob(".*.tmp"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue

    def _payload(
        self,
        key: str,
        state: dict[str, Any],
        scope: str | None = None,
    ) -> dict[str, Any]:
        """落盘负载：只留键哈希，不把含用户 / 会话标识的原始键写进文件。

        ``scope``（``user:session``）同样只存哈希：会话删除时按它清理
        检查点，不必落盘用户 / 会话标识。
        """
        payload = {**state, "key_hash": self._digest(key)}
        if scope:
            payload["scope_hash"] = self._digest(scope)
        return payload

    def _write_unlocked(
        self,
        key: str,
        state: dict[str, Any],
        scope: str | None = None,
    ) -> None:
        """原子写入（调用方已持有 ``self._lock``）。"""
        path = self._path(key)
        payload = self._payload(key, state, scope)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=self.root
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            # Windows 上检查点文件被其它进程短暂打开时 replace 会
            # PermissionError（与长期记忆 _save 同口径的有界重试）
            replace_with_retry(temp_name, path)
            fsync_parent_dir(path)
        except Exception:
            try:
                Path(temp_name).unlink()
            except OSError:
                pass
            raise
        self._prune()

    def _cleanup_legacy_roots(self) -> None:
        """清理升级前按 CWD 相对路径写下的过期检查点。

        旧版本默认目录是 ``<当前工作目录>/data/runtime/checkpoints``，
        升级后新默认根随数据目录走，旧文件不会再被任何读取路径触达，
        但其中可能含原始用户 / 会话键。只删除已超过 TTL 的文件：仍可能
        被旧进程使用的近期文件保留，避免误删活跃检查点。
        """
        try:
            current = self.root.resolve()
        except OSError:
            current = self.root
        candidates = [
            Path.cwd() / "data" / "runtime" / "checkpoints",
            package_root() / "data" / "runtime" / "checkpoints",
        ]
        cutoff = time.time() - max(self.ttl_sec, DEFAULT_TTL_SEC)
        removed = 0
        for legacy in candidates:
            try:
                resolved = legacy.resolve()
            except OSError:
                continue
            if resolved == current or not legacy.is_dir():
                continue
            for path in list(legacy.glob("*.json")) + list(legacy.glob(".*.tmp")):
                try:
                    if path.stat().st_mtime >= cutoff:
                        continue
                    path.unlink()
                    removed += 1
                except OSError:
                    continue
            try:
                legacy.rmdir()
            except OSError:
                pass
        if removed:
            logger.info("已清理旧版检查点目录过期文件 %s 个", removed)

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

    def save(
        self, key: str, state: dict[str, Any], *, scope: str | None = None
    ) -> None:
        """原子写入检查点（整份状态）。"""
        with self._lock:
            self._write_unlocked(key, state, scope)

    def update_step(
        self,
        key: str,
        step: str,
        result: Any,
        *,
        scope: str | None = None,
        task_digest: str | None = None,
    ) -> None:
        """记录某个子步骤的结果（读-改-写在进程锁内完成）。

        ``task_digest`` 可选：记录该步骤对应的任务摘要（计划字段哈希），
        复用方据此确认「同键重试的计划没有变化」；不传时保持旧行为。
        """
        with self._lock:
            state = self._load_unlocked(key) or {}
            steps = state.get("steps")
            if not isinstance(steps, dict):
                steps = {}
            steps[str(step)] = result
            state["steps"] = steps
            if task_digest is not None:
                step_keys = state.get("step_keys")
                if not isinstance(step_keys, dict):
                    step_keys = {}
                step_keys[str(step)] = task_digest
                state["step_keys"] = step_keys
            self._write_unlocked(key, state, scope)

    def clear_scope(self, scope: str) -> int:
        """删除属于某作用域（``user:session``）的全部检查点，返回条数。"""
        if not scope:
            return 0
        digest = self._digest(scope)
        removed = 0
        with self._lock:
            for path in self.root.glob("*.json"):
                data = self._read(path)
                if not isinstance(data, dict):
                    continue
                if data.get("scope_hash") != digest:
                    continue
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    continue
        return removed

    def clear(self, key: str) -> None:
        with self._lock:
            try:
                self._path(key).unlink()
            except OSError:
                pass

    def __len__(self) -> int:
        with self._lock:
            return len(list(self.root.glob("*.json")))


def clear_session_checkpoints(
    user_id: str, session_id: str, store: CheckpointStore | None = None
) -> int:
    """删除某用户会话的执行检查点（DELETE 会话时调用），返回删除条数。

    检查点里是子智能体结果（含用户提问与行程内容）：会话删除后不能等
    TTL 自然过期。清理失败只记日志（删除操作本身已完成），残留由 TTL
    与条目上限兜底。
    """
    try:
        # ``store or ...`` 会把「空存储」当成缺省：CheckpointStore 定义了
        # __len__，没有文件时真值为假，传入的自定义 store 会被静默忽略、
        # 清理落到默认目录
        target = store if store is not None else CheckpointStore()
        return target.clear_scope(f"{user_id}:{session_id}")
    except Exception as exc:  # noqa: BLE001 - 清理失败不阻断会话删除
        logger.warning("清理会话执行检查点失败: %s", exc)
        return 0
