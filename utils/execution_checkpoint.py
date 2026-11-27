"""执行检查点存储（v2.0 持久化执行的基础设施）。

按幂等键把一轮编排的中间结果落盘（默认数据目录下的
``runtime/checkpoints``），供进程重启 / 失败重试时复用已完成的子智能体
结果，避免重复调用模型。

设计：
- 每个键一个 JSON 文件（文件名 = SHA-256(key)），内容只存键哈希：键里
  带用户 / 会话标识，落盘原始值等于把个人数据写进运行期文件；
- 原子写：同目录临时文件 → fsync → ``os.replace``；
- TTL + 最大条目数：读取过期文件视为不存在并清理，写入后超限按用户
  公平淘汰——优先只淘汰条目最多的归属自己最旧的一条；当最大条目数在
  多个归属间并列且写入者不在其中时，退回淘汰全局最旧条目（尽力而为：
  活跃归属数达到上限时没有「任一用户都不挤掉他人」的绝对保证）。归属
  按 key / scope 首段（用户）计，只存哈希；无法解析归属的旧文件归入
  共享桶，仍按全局最旧淘汰；
- 损坏文件按不存在处理并移出（不阻断调用方）；
- 进程内互斥保证同进程并发写同一键不互相覆盖；跨进程写同一键不在本层
  承诺（多实例部署应按用户 / 请求分片或换 Redis 实现）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
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

# 检查点文件名 = SHA-256(key) 的 64 位十六进制 + .json；临时文件是同目录
# 下 ``.<64hex>.json.<随机>.tmp``。旧目录清理只删这种形态的文件（见
# :meth:`CheckpointStore._cleanup_legacy_roots`）。
_CHECKPOINT_NAME_RE = re.compile(r"^[0-9a-f]{64}\.json$")
_CHECKPOINT_TEMP_RE = re.compile(r"^\.[0-9a-f]{64}\.json\..+\.tmp$")


class _Unreadable:
    """哨兵：文件存在但暂时不可读（与「不存在 / 已损坏」区分）。

    瞬时读失败（Windows 共享冲突 / 杀软 / I/O 抖动）时保留文件是对的，
    但调用方不能把「读不到」当成「没有状态」：``update_step`` 据此跳过
    本次写入，避免整份覆盖并清空此前已保存的步骤。
    """


_UNREADABLE = _Unreadable()


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
        # 文件名 → 归属哈希缓存：裁剪只在超限时读取负载解析归属，缓存后
        # 稳态（容量已满、缓存已热）不再读盘
        self._owner_cache: dict[str, str] = {}
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

    def _read(self, path: Path) -> dict[str, Any] | _Unreadable | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            # 暂时不可读（Windows 共享冲突 / 句柄耗尽 / I/O 抖动）：内容
            # 未证实损坏，保留文件，否则重试会重复执行子智能体；用哨兵
            # 与「不存在」区分，写路径不得在未知状态上整份覆盖
            logger.debug(
                "读取检查点暂时失败，保留文件：%s（%s）", path.name, exc
            )
            return _UNREADABLE
        try:
            data = json.loads(raw)
        except ValueError:  # JSONDecodeError / UnicodeDecodeError：确实损坏
            # 损坏：移出，避免每次重试都撞同一个坏文件
            try:
                path.unlink()
            except OSError:
                pass
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _stat_or_none(path: Path) -> os.stat_result | None:
        try:
            return path.stat()
        except OSError:
            return None

    @staticmethod
    def _identity(stat_result: os.stat_result) -> tuple[int, int, int]:
        return (
            stat_result.st_ino,
            stat_result.st_mtime_ns,
            stat_result.st_size,
        )

    def _unlink_if_unchanged(
        self, path: Path, stat_result: os.stat_result | None
    ) -> bool:
        """按 stat 身份复核后再删除：同机多实例会在 stat 与 unlink 之间用
        ``os.replace`` 提交新检查点，直接 unlink 会删掉另一实例刚写入的
        新鲜条目。"""
        if stat_result is None:
            return False
        current = self._stat_or_none(path)
        if current is None:
            return False
        if self._identity(current) != self._identity(stat_result):
            return False
        try:
            path.unlink()
            return True
        except OSError:
            return False

    def _prune(self) -> None:
        now = time.time()
        entries: list[tuple[float, Path, os.stat_result]] = []
        for path in self.root.glob("*.json"):
            stat_result = self._stat_or_none(path)
            if stat_result is None:
                # 暂时 stat 不到：不按过期删除，也不参与淘汰
                continue
            if self.ttl_sec > 0 and (
                now - stat_result.st_mtime
            ) > self.ttl_sec:
                if self._unlink_if_unchanged(path, stat_result):
                    self._owner_cache.pop(path.name, None)
                continue
            entries.append((stat_result.st_mtime, path, stat_result))
        live_names = {path.name for _, path, _ in entries}
        for name in list(self._owner_cache):
            if name not in live_names:
                self._owner_cache.pop(name, None)
        entries.sort(key=lambda item: (item[0], item[1].name))
        if len(entries) <= self.max_entries:
            return
        # 只在超限时解析归属（缓存命中后不再读盘）；旧格式 / 无归属文件
        # 按共享桶（空串）处理，仍按全局最旧淘汰
        owners: dict[str, str] = {}
        for _, path, _ in entries:
            name = path.name
            cached = self._owner_cache.get(name)
            if cached is None:
                payload = self._read(path)
                cached = (
                    str(payload.get("owner_hash") or "")
                    if isinstance(payload, dict)
                    else ""
                )
            self._owner_cache[name] = cached
            owners[name] = cached
        while len(entries) > self.max_entries:
            # 全局 FIFO 会让任一用户的写入把另一用户在途回合的检查点挤出，
            # 重试因此重复执行子智能体：只淘汰条目最多的归属自己的最旧一条
            counts: dict[str, int] = {}
            for _, path, _ in entries:
                owner = owners.get(path.name, "")
                counts[owner] = counts.get(owner, 0) + 1
            victim_owner = max(counts, key=lambda owner: counts[owner])
            index = next(
                (
                    position
                    for position, (_, path, _) in enumerate(entries)
                    if owners.get(path.name, "") == victim_owner
                ),
                None,
            )
            if index is None:  # 不可达；防御性回退到全局最旧
                index = 0
            _, path, stat_result = entries.pop(index)
            self._unlink_if_unchanged(path, stat_result)
            self._owner_cache.pop(path.name, None)
            owners.pop(path.name, None)

    def _clean_temp_files(self) -> None:
        """清理崩溃 / 断电遗留的临时文件（调用方已持有 ``self._lock``）。"""
        cutoff = time.time() - max(self.ttl_sec, 60.0)
        for path in self.root.glob(".*.tmp"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue

    @staticmethod
    def _owner_bucket(key: str, scope: str | None) -> str:
        """归属段：scope / key 第一个 ``:`` 之前的部分（通常是用户 ID）。

        无法解析（无 scope 且 key 不含 ``:``）时返回空串，归入共享桶。
        """
        source = str(scope if scope else key)
        head, sep, _ = source.partition(":")
        return head if (scope or sep) else ""

    def _payload(
        self,
        key: str,
        state: dict[str, Any],
        scope: str | None = None,
        owner_digest: str | None = None,
    ) -> dict[str, Any]:
        """落盘负载：只留键哈希，不把含用户 / 会话标识的原始键写进文件。

        ``scope``（``user:session``）同样只存哈希：会话删除时按它清理
        检查点，不必落盘用户 / 会话标识。``owner_hash`` 供裁剪时按用户
        公平淘汰，同样只存哈希。
        """
        payload = {**state, "key_hash": self._digest(key)}
        if scope:
            payload["scope_hash"] = self._digest(scope)
        if owner_digest:
            payload["owner_hash"] = owner_digest
        return payload

    def _write_unlocked(
        self,
        key: str,
        state: dict[str, Any],
        scope: str | None = None,
    ) -> None:
        """原子写入（调用方已持有 ``self._lock``）。"""
        bucket = self._owner_bucket(key, scope)
        owner_digest = self._digest(bucket) if bucket else ""
        path = self._path(key)
        payload = self._payload(key, state, scope, owner_digest)
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
        self._owner_cache[path.name] = owner_digest
        self._prune()

    @staticmethod
    def _is_checkpoint_payload(path: Path) -> bool:
        """旧目录中的 ``*.json`` 是否为本模块形态的检查点。

        名字必须是 64 位十六进制 + ``.json``，且内容为 dict（旧格式带
        ``key``，现格式带 ``key_hash``）；解析失败 / 不可读一律视为不是，
        留给用户自行处理。
        """
        if not _CHECKPOINT_NAME_RE.match(path.name):
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return isinstance(data, dict) and (
            isinstance(data.get("key"), str)
            or isinstance(data.get("key_hash"), str)
        )

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
                    # 只删本模块形态的文件：wheel 部署下 CWD 可能是任意
                    # 应用目录，宽匹配会删掉别人 data/runtime/checkpoints
                    # 里的普通 *.json
                    if path.name.endswith(".tmp"):
                        if not _CHECKPOINT_TEMP_RE.match(path.name):
                            continue
                    elif not self._is_checkpoint_payload(path):
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

    def _load_unlocked(self, key: str) -> dict[str, Any] | _Unreadable | None:
        """调用方已持有 ``self._lock`` 时使用。"""
        path = self._path(key)
        try:
            stat_result = path.stat()
        except FileNotFoundError:
            return None
        except OSError:
            # 暂时 stat 不到不等于不存在：保留哨兵让写路径放弃覆盖
            return _UNREADABLE
        if self.ttl_sec > 0 and (
            time.time() - stat_result.st_mtime
        ) > self.ttl_sec:
            # 按 stat 身份复核后再删：多实例可能在读取前后用新文件替换
            self._unlink_if_unchanged(path, stat_result)
            return None
        return self._read(path)

    # ---------- 公共接口 ----------

    def load(self, key: str) -> dict[str, Any] | None:
        """读取检查点；不存在 / 过期 / 损坏 / 暂时不可读均返回 None。"""
        with self._lock:
            state = self._load_unlocked(key)
            return None if isinstance(state, _Unreadable) else state

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
            state = self._load_unlocked(key)
            if isinstance(state, _Unreadable):
                # 状态未知时整份覆盖会清空此前已保存的步骤（随后重试把
                # 已成功的子智能体全部重跑）：跳过本次写入，等下一次成功
                # 读取后再合并。丢一步结果比丢整份安全
                logger.warning(
                    "读取检查点暂时失败，跳过本次写入以免覆盖已保存步骤：%s",
                    step,
                )
                return
            state = state or {}
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
                    self._owner_cache.pop(path.name, None)
                except OSError:
                    continue
        return removed

    def clear(self, key: str) -> None:
        with self._lock:
            path = self._path(key)
            try:
                path.unlink()
            except OSError:
                pass
            self._owner_cache.pop(path.name, None)

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
