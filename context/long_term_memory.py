"""
长期记忆 (Long-term Memory)
持久化存储用户信息，支持跨会话访问
"""
import functools
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import weakref
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from utils.chat_import import (
    filter_import_messages,
    prepare_import_messages,
    session_dedupe_key,
)
from utils.errors import MemoryPersistenceError
from utils.file_lock import file_lock
from utils.preference_values import merge_preference_value
from utils.timestamps import chronological_key

logger = logging.getLogger(__name__)


def _trip_date_is_valid(value: object) -> bool:
    """行程出发日期是否为可解析的 YYYY-MM-DD（常去目的地统计口径）。"""
    try:
        datetime.strptime(str(value or ""), "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _session_id_matches(message: dict[str, Any], session_id: str) -> bool:
    """消息是否属于该会话：跳过无归属的旧消息（None），其余按字符串比较。

    旧数据里 session_id 可能是 None（无会话归属）或非字符串：``str(None)
    == "None"`` 会造出名为 None 的幽灵会话（标题 / 置顶 / 归档接口对
    ``PUT /api/sessions/None/...`` 返回 200 并落库），纯粹相等比较又让整数
    id 的会话「列表可见、详情为空、无法删除」。字符串化并跳过 None 同时
    解决两者，与 PostgreSQL（TEXT 列）口径一致。
    """
    value = message.get("session_id")
    return value is not None and str(value) == session_id


def _session_dedupe_key(value: object) -> str | None:
    """批量恢复的消息去重键：与 :func:`_session_id_matches` 同口径。

    旧数据里 session_id 可能是整数（如 123），导出经 user_import 解析后
    会字符串化（"123"）：两边不归一化会让同一会话的每条消息在恢复备份时
    重复插入（「重复导入幂等」失效、total_messages 翻倍），与 PostgreSQL
    （TEXT 列）行为分叉。
    """
    return session_dedupe_key(value)


def _merge_messages_chronologically(
    history: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> bool:
    """把恢复消息按时间戳并入聊天历史并整体稳定排序，返回顺序是否变化。

    恢复消息与既有历史一起按 ``chronological_key`` 稳定排序：旧版本恢复
    追加在会话末尾的乱序历史随之归位（与 PostgreSQL 按 ``created_at``
    读取一致），目标库中不存在的新会话也按时间落位，不会挤占「最近 N
    条」跨会话窗口。同一时刻的消息保持原有相对顺序（既有消息在前，恢复
    消息在后）；无时间戳的存量消息按最早处理。顺序未变化时不要求调用方
    重写整份文件。
    """
    if not incoming and not history:
        return False
    original = list(history)
    combined = original + list(incoming)
    combined.sort(key=lambda message: chronological_key(message.get("timestamp")))
    changed = len(combined) != len(original) or any(
        before is not after for before, after in zip(original, combined)
    )
    history[:] = combined
    return changed

# 同一个 JSON 文件可能被多个 LongTermMemory 实例修改（Web 请求各开一个实例、
# MemoryManager 还会重建实例），按路径共享一把进程内可重入锁，避免并发
# 读改写互相覆盖。弱引用：实例释放后锁自动移除，不随用户数无限增长。
_PATH_LOCKS: weakref.WeakValueDictionary[str, threading.RLock] = (
    weakref.WeakValueDictionary()
)
_PATH_LOCKS_GUARD = threading.Lock()


def _lock_for_path(path: str) -> threading.RLock:
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(path)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[path] = lock
        return lock


def _locked_mutation(func):
    """写操作装饰器：加锁（进程内 RLock + 跨进程文件锁）后重载再修改。

    长期记忆是整文件快照写回：调用方可能持有回合开始时的旧快照（例如一轮
    对话进行中），直接覆盖会丢失其它请求刚完成的写入（删掉的行程复活、
    新偏好丢失）。所有修改方法统一「重载 → 修改 → 原子写回」。

    同一进程内用按路径 RLock 串行化；CLI 与 Web 同时写同一用户时再叠加
    ``<data>.lock`` 跨进程文件锁，避免后写覆盖先写。
    """

    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        with self._write_lock:
            try:
                with file_lock(self.db_path):
                    # 加载时没能确认的损坏文件在这里补复核：否则空快照会把
                    # 磁盘上的损坏文件直接覆盖且不产生 .corrupt-* 备份
                    self._ensure_corrupt_backed_up()
                    self._reload_latest()
                    return func(self, *args, **kwargs)
            except TimeoutError as exc:
                # 写锁超时按持久化失败处理：不触发整轮对话重试 / 熔断器
                raise MemoryPersistenceError(f"长期记忆写锁等待超时：{exc}") from exc

    return wrapper


class LongTermMemory:
    """
    长期记忆：持久化用户信息
    - 用户偏好（家庭地址、酒店品牌、航空公司等）
    - 历史行程记录
    - 统计信息
    """

    # JSON 实现把数据快照放在内存中，多会话写入后需要重新加载
    snapshot_based = True

    def __init__(self, user_id: str, storage_path: str = "data/memory"):
        """
        初始化长期记忆

        Args:
            user_id: 用户ID
            storage_path: 存储路径
        """
        self.user_id = user_id
        # 相对路径锚定到项目根目录，避免从其它目录运行时数据写到 CWD（Issue #5）
        storage = Path(storage_path)
        if not storage.is_absolute():
            storage = Path(__file__).resolve().parent.parent / storage
        self.storage_path = str(storage)
        self.db_path = os.path.join(self.storage_path, f"{user_id}.json")
        self._write_lock = _lock_for_path(self.db_path)

        # 确保存储目录存在
        Path(self.storage_path).mkdir(parents=True, exist_ok=True)

        # 加载或初始化数据
        # 「加载时没能确认损坏文件已备份」的标记：等锁超时 / 备份失败时置位，
        # 下一次持锁写入前必须复核并补备份，否则空快照会直接覆盖损坏文件
        self._corrupt_pending = False
        # 本次启动时被隔离的损坏记忆文件路径（供 CLI 报告，无损坏为 None）
        self._corrupt_backup: str | None = None
        self.data = self._load()
        logger.info(f"Long-term memory initialized for user: {user_id}")

    def _load(self) -> dict[str, Any]:
        """从文件加载数据。

        读取全程持有按路径的写锁：Windows 上打开的文件句柄会阻止其它线程 /
        实例的 ``os.replace``（WinError 5），并发写盘会被误报为失败。锁是
        可重入的，下方复核分支可安全再次进入。
        """
        with self._write_lock:
            if os.path.exists(self.db_path):
                try:
                    # 读句柄尽早关闭：Windows 上任何打开中的句柄都会让其它
                    # 进程的 os.replace 失败（WinError 5）。大文件的解析占
                    # 读取耗时的大头，先读完再解析可把阻塞写者的窗口压到最短
                    with open(self.db_path, "r", encoding="utf-8") as f:
                        raw = f.read()
                    data = self._migrate_data(json.loads(raw))
                    # 大小写不敏感文件系统上另一账号可能映射到同一文件：
                    # 这是身份冲突而不是文件损坏，不能走隔离分支
                    self._check_owner_case(data)
                    logger.debug(f"Loaded long-term memory from {self.db_path}")
                    return data
                except MemoryPersistenceError:
                    raise
                except Exception as e:
                    logger.error(f"Failed to load long-term memory: {e}")
                    # 复核与备份都必须持有跨进程文件锁：其它进程的写操作在
                    # 同一把锁内用原子替换提交，不持锁的 os.replace 可能把
                    # 对方刚写入的有效文件当作损坏文件移走
                    try:
                        with file_lock(self.db_path):
                            try:
                                with open(self.db_path, "r", encoding="utf-8") as f:
                                    latest_raw = f.read()
                                latest = json.loads(latest_raw)
                                # 与首次读取一致：合法 JSON 但不是对象 /
                                # 字段结构异常同样视为损坏，不能逃逸
                                if not isinstance(latest, dict):
                                    raise ValueError("记忆文件内容不是 JSON 对象")
                                latest = self._migrate_data(latest)
                            except FileNotFoundError:
                                return self._init_data()
                            except Exception:
                                # 备份名带微秒与进程号，同一秒内多次损坏
                                # 不会互相覆盖
                                backup = (
                                    f"{self.db_path}.corrupt-"
                                    f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
                                    f"-{os.getpid()}"
                                )
                                try:
                                    os.replace(self.db_path, backup)
                                    self._corrupt_backup = backup
                                    logger.warning("损坏的记忆文件已备份到 %s", backup)
                                except OSError as backup_error:
                                    # 文件可能仍被其它进程持有：先以空快照启动，
                                    # 但写入前必须补备份（见 _ensure_corrupt_backed_up）
                                    self._corrupt_pending = True
                                    logger.error(
                                        "备份损坏的记忆文件失败: %s", backup_error
                                    )
                                return self._init_data()
                            logger.info("记忆文件在加载失败后已被并发更新，改用最新内容")
                            return latest
                    except TimeoutError as lock_error:
                        # 等锁超时：不能把可能有效的数据当损坏文件移走，
                        # 以空快照启动；写入前会在持锁状态补做复核 / 备份
                        self._corrupt_pending = True
                        logger.error(
                            "加载损坏记忆文件时等待文件锁超时: %s", lock_error
                        )
                        return self._init_data()
            else:
                logger.info("No existing long-term memory, creating new")
                return self._init_data()

    def _check_owner_case(self, data: dict[str, Any]) -> None:
        """大小写不敏感文件系统上防止跨账号读写同一记忆文件。

        ``Alice`` / ``alice`` 在 Windows / macOS 默认文件系统上指向同一个
        ``<user_id>.json``；文件里记录着创建它的 user_id。请求的 ID 与记录
        仅大小写不同时拒绝访问，避免读到 / 写坏另一账号的行程与对话。
        PostgreSQL 后端按列存储，不受影响。
        """
        owner = data.get("user_id")
        if not isinstance(owner, str) or not owner or owner == self.user_id:
            return
        if owner.casefold() != self.user_id.casefold():
            return
        raise MemoryPersistenceError(
            f"用户 ID 与已有记忆文件仅大小写不同（已有 {owner}）。当前文件系统"
            "大小写不敏感，两个 ID 会读写同一份数据；请使用与已有账号完全一致"
            "的大小写"
        )

    def _ensure_corrupt_backed_up(self) -> None:
        """写操作前的损坏文件复核（调用方必须已持有跨进程文件锁）。

        ``_load`` 在等锁超时 / 备份失败时以空快照启动，磁盘上可能仍是一份
        无法解析的损坏文件：``_reload_latest`` 会静默沿用内存快照，随后的
        写入直接覆盖文件，原本可能可手工恢复的数据永久丢失。这里在持锁
        状态下重新复核：文件已恢复为合法 JSON 就交给 ``_reload_latest``
        采用；仍然损坏则补备份；备份再次失败拒绝写入。
        """
        if not self._corrupt_pending:
            return
        corrupted = False
        try:
            with open(self.db_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 合法 JSON 但顶层不是对象（[] / null / 字符串）与解析失败同一
            # 口径：_load / _reload_latest 都按损坏处理。只验证「能解析」
            # 会清掉标记，随后每次写入都在 _reload_latest 抛错且永不备份
            corrupted = not isinstance(data, dict)
        except FileNotFoundError:
            self._corrupt_pending = False
            return
        except Exception:  # noqa: BLE001 - 解析失败按损坏处理
            corrupted = True
        if corrupted:
            backup = (
                f"{self.db_path}.corrupt-"
                f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
                f"-{os.getpid()}"
            )
            try:
                os.replace(self.db_path, backup)
            except OSError as exc:
                raise MemoryPersistenceError(
                    "记忆文件损坏且无法备份，已拒绝覆盖写入"
                    f"（可手动处理 {self.db_path}）"
                ) from exc
            self._corrupt_backup = backup
            logger.warning("损坏的记忆文件已补备份到 %s", backup)
        self._corrupt_pending = False

    def reload(self) -> None:
        """重新从磁盘加载最新数据（供 CLI 并发复核等只读场景调用）。"""
        with self._write_lock:
            self._reload_latest()

    def _reload_latest(self) -> None:
        """从磁盘重载最新数据（写操作前调用，避免旧快照覆盖其它写入）。"""
        try:
            # 与 _load 同一原因：句柄尽早关闭，解析在句柄关闭后进行
            with open(self.db_path, encoding="utf-8") as f:
                raw = f.read()
            data = json.loads(raw)
        except FileNotFoundError:
            return
        except Exception as exc:  # noqa: BLE001 - 损坏文件不能静默沿用旧快照
            # 沿用旧快照写回会覆盖磁盘上更新（或已损坏）的数据、把损坏掩盖成
            # 「正常写入」；与载入路径一致按持久化失败处理，交给上层映射 503
            logger.warning("重载长期记忆失败：%s", exc)
            raise MemoryPersistenceError(
                f"长期记忆重载失败（{self.db_path}）：{exc}"
            ) from exc
        if not isinstance(data, dict):
            # 合法 JSON 但不是对象（[] / null / 字符串）：与解析失败同等对待，
            # 静默沿用旧快照会把下一次写入建立在错误的磁盘状态上
            logger.warning("长期记忆重载失败：内容不是 JSON 对象")
            raise MemoryPersistenceError(
                f"长期记忆重载失败（{self.db_path}）：内容不是 JSON 对象"
            )
        migrated = self._migrate_data(data)
        self._check_owner_case(migrated)
        self.data = migrated

    def _backup_before_migration(self) -> None:
        """迁移会丢弃 / 重置坏数据前把原始文件另存一份（每个文件只留首份）。

        这些修正是破坏性的（非对象消息被丢弃、非数字计数器归零），直接把
        清理后的快照写回磁盘会让原始内容不可恢复；保留原始副本便于排查。
        备份含完整用户数据：copyfile 按 umask 新建文件，POSIX 上收紧到
        0600，避免与协作目录共享时被同组 / 其他用户读取。
        """
        backup = f"{self.db_path}.migrate-backup"
        if os.path.exists(backup):
            return
        try:
            shutil.copyfile(self.db_path, backup)
        except OSError:
            logger.warning("无法备份迁移前的记忆文件：%s", self.db_path)
            return
        try:
            os.chmod(backup, 0o600)
        except OSError:  # Windows 只映射只读位；权限本就不按 POSIX 位控制
            pass
        logger.warning("迁移修复前已备份原始记忆文件到 %s", backup)

    def _migrate_data(self, data: dict[str, Any]) -> dict[str, Any]:
        """
        迁移旧数据格式到新格式

        Args:
            data: 原始数据

        Returns:
            迁移后的数据
        """
        # 1. 确保必需字段存在
        if "chat_history" not in data:
            data["chat_history"] = []
        if not isinstance(data.get("session_titles"), dict):
            data["session_titles"] = {}
        if not isinstance(data.get("pinned_sessions"), list):
            data["pinned_sessions"] = []
        if not isinstance(data.get("archived_sessions"), list):
            data["archived_sessions"] = []
        if "trip_history" not in data:
            data["trip_history"] = []
        stats_missing = not isinstance(data.get("statistics"), dict)
        if stats_missing:
            data["statistics"] = {}
        stats = data["statistics"]
        destructive = False
        # 计数器存在但为 null / 字符串 / 布尔时按 0 处理：这类文件是合法
        # JSON，不修复的话每次写入（+= 1）都会抛 TypeError，而且永远不会
        # 进入损坏备份 / 自愈路径。缺失键（新建用户第一轮落盘后缺
        # last_trip_number）是正常初始状态：静默补 0，不算破坏性修正，
        # 不能为此把整份正常文件复制成 .migrate-backup
        # 缺失的计数器要按列表重建（与 PG 的 COUNT(*) 同口径）：只补 0 会让
        # 统计永久低于真实条数，且各删除路径按差值扣减后仍偏小
        missing_counters: set[str] = set()
        for counter in (
            "total_messages",
            "total_queries",
            "total_trips",
            "last_trip_number",
        ):
            if counter not in stats:
                stats[counter] = 0
                missing_counters.add(counter)
                continue
            value = stats.get(counter)
            if not isinstance(value, int) or isinstance(value, bool):
                stats[counter] = 0
                destructive = True
                missing_counters.add(counter)
        frequent_rebuild = not isinstance(stats.get("frequent_destinations"), dict)
        if not frequent_rebuild:
            # 计数值被写成字符串 / null 时 += 1 同样 TypeError：归零并保留城市键
            frequent = stats["frequent_destinations"]
            cleaned_frequent: dict[str, int] = {}
            frequent_changed = False
            for city, count in frequent.items():
                key = str(city)
                if isinstance(count, int) and not isinstance(count, bool):
                    cleaned_frequent[key] = count
                else:
                    cleaned_frequent[key] = 0
                    frequent_changed = True
                if key != city:
                    frequent_changed = True
            stats["frequent_destinations"] = cleaned_frequent
            if frequent_changed:
                destructive = True
        else:
            stats["frequent_destinations"] = {}
        if "preferences" not in data:
            data["preferences"] = []

        # 字段存在但为 null / 类型错误时按空集合处理：这类文件是合法 JSON，
        # 若不在读取时修复，每次 get_* / 写入都会抛 TypeError，且永远不会备份
        chats_changed = trips_changed = False
        for list_key in ("chat_history", "trip_history"):
            if not isinstance(data.get(list_key), list):
                data[list_key] = []
                destructive = True
                if list_key == "chat_history":
                    chats_changed = True
                else:
                    trips_changed = True
                continue
            cleaned = [item for item in data[list_key] if isinstance(item, dict)]
            if len(cleaned) != len(data[list_key]):
                logger.warning(
                    "丢弃 %s 中 %d 条非对象记录",
                    list_key,
                    len(data[list_key]) - len(cleaned),
                )
                destructive = True
                # 计数与列表同步：丢弃坏条目后各删除路径按差值扣减，
                # 旧总数会让统计永久偏大（总数与列表长度必须一致）
                if list_key == "chat_history":
                    stats["total_messages"] = len(cleaned)
                    chats_changed = True
                else:
                    stats["total_trips"] = len(cleaned)
                    trips_changed = True
            data[list_key] = cleaned
        if stats_missing:
            # 统计段整体缺失（极老格式）：按现有列表重建，而不是把计数器
            # 补 0 后与列表长度永久脱节
            chats_changed = trips_changed = True
        # 计数器合法但与列表长度不符（缺 chat_history 键后补空列表 /
        # 旧版本把计数写大）：只向下对齐到真实条数——与 PG 的 COUNT(*)
        # 口径一致，删除路径按差值扣减后不再永久偏大
        if (
            isinstance(stats.get("total_messages"), int)
            and stats["total_messages"] > len(data["chat_history"])
        ):
            stats["total_messages"] = len(data["chat_history"])
            chats_changed = True
        if (
            isinstance(stats.get("total_trips"), int)
            and stats["total_trips"] > len(data["trip_history"])
        ):
            stats["total_trips"] = len(data["trip_history"])
            trips_changed = True
        if chats_changed or "total_messages" in missing_counters:
            stats["total_messages"] = len(data["chat_history"])
        if trips_changed or "total_trips" in missing_counters:
            stats["total_trips"] = len(data["trip_history"])
        if trips_changed or frequent_rebuild:
            # 行程列表被清空 / 丢弃，或常去目的地整段类型错误时，按当前
            # 列表重建（与 PG 的动态统计同口径）：否则面板会继续显示已不
            # 存在的城市，或保留清零后的空表；逐项计数损坏仍按原语义归零
            # 并保留城市键
            stats["frequent_destinations"] = Counter(
                str(trip.get("destination") or "")
                for trip in data["trip_history"]
                if isinstance(trip, dict)
                and trip.get("destination")
                and _trip_date_is_valid(trip.get("start_date"))
            )

        if not isinstance(data.get("preferences"), (dict, list)):
            data["preferences"] = []
            destructive = True

        # 2. 迁移旧格式：字典 → 列表
        if isinstance(data.get("preferences"), dict):
            old_prefs = data["preferences"]
            new_prefs = []
            for pref_type, pref_value in old_prefs.items():
                if pref_value is not None:
                    new_prefs.append({"type": pref_type, "value": pref_value})
            data["preferences"] = new_prefs
            destructive = True
            logger.info(f"Migrated: Converted preferences from dict to list ({len(new_prefs)} items)")

        # 3. 修复嵌套 bug（旧代码产生的错误数据）并清洗非法偏好条目：
        #    - type 非字符串（旧版本把数组 / 对象写进 type）会让 get_preference
        #      以不可哈希值作字典键抛 TypeError（状态 / 会话接口永久 503，
        #      删除接口永远删不掉），必须丢弃
        #    - 缺 "value" 的条目会让嵌套迁移下标取值抛 KeyError，整份文件
        #      被误判为损坏并隔离为空数据，必须丢弃
        if isinstance(data.get("preferences"), list):
            fixed_prefs = []
            for pref in data["preferences"]:
                if not isinstance(pref, dict):
                    logger.warning("丢弃非对象的偏好记录：%r", pref)
                    destructive = True
                    continue
                if not isinstance(pref.get("type"), str):
                    logger.warning("丢弃 type 非字符串的偏好记录：%r", pref.get("type"))
                    destructive = True
                    continue
                if "value" not in pref:
                    logger.warning("丢弃缺少 value 的偏好记录：%s", pref.get("type"))
                    destructive = True
                    continue
                # 错误的嵌套：{"type": "preferences", "value": [...]}
                if pref.get("type") == "preferences" and isinstance(pref.get("value"), list):
                    for nested_pref in pref["value"]:
                        if (
                            isinstance(nested_pref, dict)
                            and isinstance(nested_pref.get("type"), str)
                            and "value" in nested_pref
                        ):
                            fixed_prefs.append(
                                {
                                    "type": nested_pref["type"],
                                    "value": nested_pref["value"],
                                }
                            )
                        else:
                            logger.warning(
                                "丢弃嵌套偏好中的非法条目：%r", nested_pref
                            )
                            destructive = True
                    logger.info("Migrated: Fixed nested preferences bug")
                else:
                    fixed_prefs.append(pref)

            if fixed_prefs != data["preferences"]:
                data["preferences"] = fixed_prefs

        # 迁移结果在下次写入时落盘：读取路径不再无条件重写文件，
        # 避免搜索/状态等只读请求与对话写入互相覆盖。
        # 同一 type 的重复偏好（旧迁移 bug 产物）：存储写路径只更新第一条、
        # 而 get_preference()（状态 / 上下文 / 导出）取最后一条——写后仍读到
        # 旧值。保留最后一条并按首次出现的位置排序
        if isinstance(data.get("preferences"), list):
            by_type: dict[str, dict[str, Any]] = {}
            for pref in data["preferences"]:
                if isinstance(pref, dict) and isinstance(pref.get("type"), str):
                    by_type[str(pref["type"])] = pref
            if len(by_type) != len(data["preferences"]):
                logger.warning(
                    "偏好存在重复类型，保留最后一条（%d -> %d）",
                    len(data["preferences"]),
                    len(by_type),
                )
                data["preferences"] = list(by_type.values())
                destructive = True

        if destructive:
            # 所有破坏性修正（含最后的偏好去重）都完成后才备份：此前在
            # 去重之前就备份，唯一破坏性修正是去重时不会生成 .migrate-backup
            self._backup_before_migration()

        return data

    def _init_data(self) -> dict[str, Any]:
        """初始化数据结构"""
        return {
            "user_id": self.user_id,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "preferences": [],  # 偏好列表: [{"type": "home_location", "value": "天津"}, ...]
            "chat_history": [],  # 所有聊天记录（跨会话）
            "session_titles": {},  # 会话自定义标题 {session_id: title}
            "pinned_sessions": [],  # 置顶会话 ID（列表有序）
            "archived_sessions": [],  # 归档会话 ID（列表展示时默认隐藏）
            "trip_history": [],  # 所有行程记录
            "statistics": {
                "total_trips": 0,
                "total_messages": 0,
                "total_queries": 0,
                "frequent_destinations": {}
            }
        }

    def _save(self) -> bool:
        """原子写入数据文件（临时文件 + os.replace），返回是否写入成功。"""
        try:
            self.data["updated_at"] = datetime.now().isoformat()
            directory = os.path.dirname(self.db_path) or "."
            fd, temp_path = tempfile.mkstemp(
                prefix=f".{self.user_id}.", suffix=".tmp", dir=directory
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(self.data, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    # 持久化屏障：os.replace 只保证原子替换，不保证临时文件
                    # 数据已落盘（NTFS 的 rename 元数据可能先于数据页写回）。
                    # fsync 后「保存成功」才意味着掉电/崩溃后文件仍可解析
                    os.fsync(handle.fileno())
                # Windows 上另一个进程短暂打开数据文件时 os.replace 会报
                # PermissionError / WinError 5：读者已改为读完立即关闭句柄，
                # 但大文件读取仍可能占数百毫秒，重试预算与文件锁的等待上限
                # 对齐（约 1.5 秒）再放弃
                deadline = time.monotonic() + 1.5
                while True:
                    try:
                        os.replace(temp_path, self.db_path)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.03)
            finally:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            logger.debug(f"Saved long-term memory to {self.db_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to save long-term memory: {e}")
            return False

    def _persist(self) -> None:
        """写盘失败时抛错，避免上层把未落盘的修改当成功。"""
        if not self._save():
            raise MemoryPersistenceError("长期记忆写入失败")

    @_locked_mutation
    def append_preference(self, pref_type: str, value: Any) -> Any:
        """在锁内重新加载并追加一个偏好值（列表语义），返回合并后的值。

        ``get_preference() + save_preference()`` 的读改写会覆盖并发写入的
        偏好（例如本轮 LLM 调用期间用户在记忆面板修改了同一类型）。
        """
        preferences = self.data["preferences"]
        existing = None
        for pref in preferences:
            if pref.get("type") == pref_type:
                existing = pref
                break
        current = existing.get("value") if existing is not None else None
        merged = merge_preference_value(current, value)
        if existing is not None:
            existing["value"] = merged
        else:
            preferences.append({"type": pref_type, "value": merged})
        self._persist()
        # 偏好值可能是用户隐私内容（证件 / 航班等）：日志只记类型与数量
        logger.info(f"Appended preference {pref_type} (total {len(merged)})")
        return merged

    @_locked_mutation
    def save_preference(self, pref_type: str, value: Any):
        """
        保存用户偏好（列表格式）

        Args:
            pref_type: 偏好类型
            value: 偏好值
        """
        # 查找是否已存在该类型的偏好
        preferences = self.data["preferences"]
        found = False

        for pref in preferences:
            if pref.get("type") == pref_type:
                pref["value"] = value
                found = True
                break

        # 如果不存在，添加新的偏好
        if not found:
            preferences.append({"type": pref_type, "value": value})

        self._persist()
        logger.info(f"Saved preference: {pref_type}")

    def get_preference(self, pref_type: str | None = None) -> Any:
        """
        获取用户偏好

        Args:
            pref_type: 偏好类型，None返回字典格式的全部偏好

        Returns:
            偏好值或偏好字典
        """
        preferences = self.data["preferences"]

        if pref_type is None:
            # 返回字典格式，方便调用方使用
            result = {}
            for pref in preferences:
                result[pref.get("type")] = pref.get("value")
            return result
        else:
            # 查找特定类型的偏好
            for pref in preferences:
                if pref.get("type") == pref_type:
                    return pref.get("value")
            return None

    @_locked_mutation
    def delete_preference(self, pref_type: str) -> bool:
        """删除指定类型的偏好，返回是否确实删除。"""
        preferences = self.data["preferences"]
        remaining = [pref for pref in preferences if pref.get("type") != pref_type]
        if len(remaining) == len(preferences):
            return False
        self.data["preferences"] = remaining
        self._persist()
        logger.info(f"Deleted preference: {pref_type}")
        return True

    @_locked_mutation
    def add_hotel_brand(self, brand: str):
        """添加酒店品牌偏好（追加到列表）"""
        # 查找 hotel_brands 偏好
        preferences = self.data["preferences"]
        found = False

        for pref in preferences:
            if pref.get("type") == "hotel_brands":
                # 确保 value 是列表
                if not isinstance(pref["value"], list):
                    pref["value"] = [pref["value"]] if pref["value"] else []

                # 追加品牌
                if brand not in pref["value"]:
                    pref["value"].append(brand)
                found = True
                break

        # 如果不存在，创建新的
        if not found:
            preferences.append({"type": "hotel_brands", "value": [brand]})

        self._persist()
        logger.info(f"Added hotel brand preference: {brand}")

    @_locked_mutation
    def add_airline(self, airline: str):
        """添加航空公司偏好（追加到列表）"""
        # 查找 airlines 偏好
        preferences = self.data["preferences"]
        found = False

        for pref in preferences:
            if pref.get("type") == "airlines":
                # 确保 value 是列表
                if not isinstance(pref["value"], list):
                    pref["value"] = [pref["value"]] if pref["value"] else []

                # 追加航空公司
                if airline not in pref["value"]:
                    pref["value"].append(airline)
                found = True
                break

        # 如果不存在，创建新的
        if not found:
            preferences.append({"type": "airlines", "value": [airline]})

        self._persist()
        logger.info(f"Added airline preference: {airline}")

    @_locked_mutation
    def add_chat_message(
        self,
        role: str,
        content: str,
        session_id: str | None = None,
        timestamp: str | None = None,
    ):
        """
        添加聊天消息到长期记忆

        Args:
            role: 角色 (user/assistant)
            content: 消息内容
            session_id: 会话ID（可选）
            timestamp: ISO 时间戳（可选；备份恢复时保留原始时间，
                缺省用当前时间）
        """
        message = {
            "role": role,
            "content": content,
            "timestamp": timestamp or datetime.now().isoformat(),
            "session_id": session_id
        }

        self.data["chat_history"].append(message)
        self.data["statistics"]["total_messages"] += 1
        self._persist()
        logger.debug(f"Added chat message to long-term memory: {role}")

    @_locked_mutation
    def add_chat_messages(
        self, messages: list[dict[str, Any]], dedupe: bool = False
    ) -> int:
        """批量追加聊天消息（备份恢复）：一次重载 + 一次原子写回。

        逐条调用 ``add_chat_message`` 时每次都要重载并重写整个文件
        （O(n²)，几千条消息就会阻塞数分钟），这里合并为单次写锁。
        ``dedupe=True`` 在写锁内按（会话, 角色, 内容）多重集去重；提问已在
        库中的轮次跳过其备份里的全部回答（本地优先，避免尾部孤儿回答，
        见 :mod:`utils.chat_import`），并按时间戳把恢复消息并入全局历史。
        返回实际写入的条数。
        """
        now = datetime.now().isoformat()
        prepared = prepare_import_messages(messages)
        had_incoming = bool(prepared)
        if dedupe:
            history = self.data["chat_history"]
            existing = Counter(
                (
                    _session_dedupe_key(item.get("session_id")),
                    item.get("role"),
                    str(item.get("content") or ""),
                )
                for item in history
            )
            prepared = filter_import_messages(prepared, existing)
        to_add: list[dict[str, Any]] = []
        for message in prepared:
            role = str(message.get("role") or "")
            if role not in {"user", "assistant"}:
                continue
            to_add.append(
                {
                    "role": role,
                    "content": str(message.get("content") or ""),
                    "timestamp": message.get("timestamp") or now,
                    "session_id": message.get("session_id"),
                }
            )
        if not to_add:
            if dedupe and had_incoming:
                # 幂等恢复（没有新增消息）也把旧版本追加在末尾的乱序历史
                # 归位：否则升级后重复恢复同一备份永远看不到修复。顺序
                # 未变化时不重写整份文件。
                if _merge_messages_chronologically(self.data["chat_history"], []):
                    self._persist()
            return 0
        if dedupe:
            _merge_messages_chronologically(self.data["chat_history"], to_add)
        else:
            self.data["chat_history"].extend(to_add)
        added = len(to_add)
        statistics = self.data.setdefault("statistics", {})
        statistics["total_messages"] = int(statistics.get("total_messages") or 0) + added
        self._persist()
        logger.info("Bulk imported %d chat messages", added)
        return added

    @_locked_mutation
    def add_chat_turn(
        self,
        user_content: str,
        assistant_content: str,
        session_id: str | None = None,
        should_commit: Callable[[], bool] | None = None,
    ) -> bool:
        """原子追加一轮完整问答（用户 + 助手两条消息）。

        分成两次 ``add_chat_message`` 时，两次写回之间被取消 / 第二次写盘
        失败会把「有提问没有回答」的半轮落盘；这里合并为单次写锁 + 单次
        写回，写回失败时同时回滚内存中的两条追加（磁盘保持原状）。

        ``should_commit`` 在持有写锁时调用：会话刚被删除（墓碑有效）时返回
        False，整轮不写入——在途落库与删除并发时，检查与写入在同一把锁内
        完成，不会把已删除的会话写回。

        Returns:
            是否真的写入
        """
        if should_commit is not None and not should_commit():
            return False
        now = datetime.now().isoformat()
        history = self.data["chat_history"]
        start = len(history)
        total_before = int(self.data["statistics"].get("total_messages") or 0)
        history.append(
            {
                "role": "user",
                "content": user_content,
                "timestamp": now,
                "session_id": session_id,
            }
        )
        history.append(
            {
                "role": "assistant",
                "content": assistant_content,
                "timestamp": now,
                "session_id": session_id,
            }
        )
        self.data["statistics"]["total_messages"] = total_before + 2
        try:
            self._persist()
        except BaseException:
            del history[start:]
            self.data["statistics"]["total_messages"] = total_before
            raise
        logger.debug("Added chat turn to long-term memory")
        return True

    def get_chat_history(
        self,
        limit: int | None = None,
        session_id: str | None = None,
        exclude_session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        获取聊天历史

        Args:
            limit: 返回数量限制
            session_id: 会话ID（只返回特定会话的消息）
            exclude_session_id: 排除该会话的消息（跨会话总结取「其它会话
                的最近窗口」用，与 session_id 互斥，同时给出时忽略本参数）

        Returns:
            消息列表
        """
        messages = self.data["chat_history"]

        if session_id:
            messages = [m for m in messages if _session_id_matches(m, session_id)]
        elif exclude_session_id:
            messages = [
                m
                for m in messages
                if not _session_id_matches(m, exclude_session_id)
            ]

        if limit:
            return messages[-limit:]
        return messages

    def get_chat_summary(self) -> dict[str, Any]:
        """轻量会话概览：消息总数与有消息的会话 ID 集合。

        只读汇总命令（CLI stats）只需要计数与去重集合，不应完整物化每条
        消息（PostgreSQL 后端会整表取回正文）。
        """
        messages = self.data.get("chat_history") or []
        sessions = {
            str(message.get("session_id"))
            for message in messages
            if isinstance(message, dict) and message.get("session_id")
        }
        return {"messages": len(messages), "sessions": sessions}

    def get_recovered_corrupt_backup(self) -> str | None:
        """本次实例启动时被隔离的损坏记忆文件路径（无损坏为 None）。"""
        return self._corrupt_backup

    @_locked_mutation
    def delete_chat_session(self, session_id: str) -> int:
        """删除某个会话的全部聊天记录，返回删除条数。

        引用该会话的行程保留、清空会话链接（与会话删除后不可见 / 备份
        恢复跳过已删除会话的口径一致，避免面板出现永远打不开的会话链接）。
        """
        messages = self.data["chat_history"]
        remaining = [
            m for m in messages if not _session_id_matches(m, session_id)
        ]
        removed = len(messages) - len(remaining)
        titles = self.data.get("session_titles") or {}
        pinned_sessions = self.data.get("pinned_sessions") or []
        archived_sessions = self.data.get("archived_sessions") or []
        cleared_links = 0
        for trip in self.data.get("trip_history") or []:
            if not isinstance(trip, dict):
                continue
            value = trip.get("session_id")
            if value is not None and str(value) == session_id:
                trip["session_id"] = None
                cleared_links += 1
        metadata_changed = (
            session_id in titles
            or session_id in pinned_sessions
            or session_id in archived_sessions
        )

        if removed:
            self.data["chat_history"] = remaining
            stats = self.data["statistics"]
            stats["total_messages"] = max(0, stats.get("total_messages", 0) - removed)

        if removed or metadata_changed or cleared_links:
            self.data.get("session_titles", {}).pop(session_id, None)
            if isinstance(pinned_sessions, list) and session_id in pinned_sessions:
                pinned_sessions.remove(session_id)
            if isinstance(archived_sessions, list) and session_id in archived_sessions:
                archived_sessions.remove(session_id)
            self._persist()
            logger.info(
                "Deleted chat session: %s (%s messages, metadata=%s, trip_links=%s)",
                session_id,
                removed,
                metadata_changed,
                cleared_links,
            )
        return removed

    @_locked_mutation
    def delete_last_turn(self, session_id: str, expect: str | None = None) -> int:
        """删除某个会话最后一轮对话（助手回答 + 对应用户提问），返回删除条数。

        ``expect`` 不为 None 时会校验最后一轮的用户提问内容：不一致说明这条
        提问并未真正写入（例如意图识别阶段就失败），此时不删除任何内容，
        避免误删上一轮已保存的对话。
        """
        history = self.data["chat_history"]
        indexes = [
            index
            for index, message in enumerate(history)
            if _session_id_matches(message, session_id)
        ]
        if not indexes:
            return 0
        count = 1
        last_user_index = indexes[-1] if history[indexes[-1]].get("role") == "user" else None
        if (
            len(indexes) >= 2
            and history[indexes[-1]].get("role") == "assistant"
            and history[indexes[-2]].get("role") == "user"
        ):
            count = 2
            last_user_index = indexes[-2]
        if expect is not None:
            if last_user_index is None:
                return 0
            actual = str(history[last_user_index].get("content", "")).strip()
            if actual != expect.strip():
                logger.info(
                    "Skip deleting last turn of session %s: content mismatch", session_id
                )
                return 0
        removed_indexes = set(indexes[-count:])
        self.data["chat_history"] = [
            message
            for index, message in enumerate(history)
            if index not in removed_indexes
        ]
        stats = self.data["statistics"]
        stats["total_messages"] = max(0, stats.get("total_messages", 0) - count)
        # 会话消息已被删空时同步清理元数据，避免幽灵归档 / 幽灵标题 / 幽灵置顶
        if not any(
            _session_id_matches(message, session_id)
            for message in self.data["chat_history"]
        ):
            archived_sessions = self.data.get("archived_sessions") or []
            if session_id in archived_sessions:
                archived_sessions.remove(session_id)
            titles = self.data.get("session_titles")
            if isinstance(titles, dict):
                titles.pop(session_id, None)
            pinned_sessions = self.data.get("pinned_sessions") or []
            if isinstance(pinned_sessions, list) and session_id in pinned_sessions:
                pinned_sessions.remove(session_id)
            # 与整会话删除同口径：清空引用该会话的行程链接，否则面板里
            # 留下永远打不开的「查看对应对话」（单轮会话编辑重发时删空）
            for trip in self.data.get("trip_history") or []:
                if not isinstance(trip, dict):
                    continue
                value = trip.get("session_id")
                if value is not None and str(value) == session_id:
                    trip["session_id"] = None
        if not self._save():
            raise MemoryPersistenceError("长期记忆写入失败")
        logger.info("Deleted last turn of session %s (%s messages)", session_id, count)
        return count

    def _session_exists(self, session_id: str) -> bool:
        """当前快照里是否还有该会话的消息（写元数据前的存在性检查）。"""
        return any(
            _session_id_matches(message, session_id)
            for message in self.data.get("chat_history", [])
        )

    def _session_id_set(self) -> set[str]:
        """当前快照里存在消息的会话 ID 集合（批量元数据恢复一次扫描）。"""
        return {
            str(message.get("session_id"))
            for message in self.data.get("chat_history", [])
            if isinstance(message, dict)
            and message.get("session_id") is not None
        }

    @_locked_mutation
    def set_session_title(self, session_id: str, title: str) -> bool:
        """设置会话自定义标题；空标题表示恢复自动标题。返回会话是否存在。"""
        if not self._session_exists(session_id):
            return False
        titles = self.data.setdefault("session_titles", {})
        cleaned = title.strip()
        if cleaned:
            titles[session_id] = cleaned[:60]
        else:
            titles.pop(session_id, None)
        if not self._save():
            raise MemoryPersistenceError("长期记忆写入失败")
        logger.info(f"Set session title: {session_id} (len={len(cleaned)})")
        return True

    def get_session_titles(self) -> dict[str, str]:
        """返回全部会话自定义标题。"""
        return dict(self.data.get("session_titles") or {})

    @_locked_mutation
    def set_session_pinned(self, session_id: str, pinned: bool) -> bool:
        """置顶 / 取消置顶会话。返回会话是否存在。"""
        if not self._session_exists(session_id):
            return False
        pinned_sessions = self.data.setdefault("pinned_sessions", [])
        if pinned and session_id not in pinned_sessions:
            pinned_sessions.append(session_id)
        elif not pinned and session_id in pinned_sessions:
            pinned_sessions.remove(session_id)
        if not self._save():
            raise MemoryPersistenceError("长期记忆写入失败")
        return True

    def get_pinned_sessions(self) -> list[str]:
        """返回置顶会话 ID（按置顶时间先后）。"""
        return list(self.data.get("pinned_sessions") or [])

    @_locked_mutation
    def set_session_archived(self, session_id: str, archived: bool) -> bool:
        """归档 / 恢复会话；归档只影响会话列表展示，不删除消息。返回会话是否存在。"""
        if not self._session_exists(session_id):
            return False
        archived_sessions = self.data.setdefault("archived_sessions", [])
        if archived and session_id not in archived_sessions:
            archived_sessions.append(session_id)
        elif not archived and session_id in archived_sessions:
            archived_sessions.remove(session_id)
        if not self._save():
            raise MemoryPersistenceError("长期记忆写入失败")
        logger.info("Set session archived: %s -> %s", session_id, archived)
        return True

    def get_archived_sessions(self) -> list[str]:
        """返回归档会话 ID。"""
        return list(self.data.get("archived_sessions") or [])

    @_locked_mutation
    def set_session_titles(self, titles: dict[str, str]) -> int:
        """批量设置会话标题（备份恢复）：只对存在的会话生效，返回生效数。"""
        applied = 0
        stored = self.data.setdefault("session_titles", {})
        # 一次扫描构建会话集合：逐条 _session_exists 是 O(k×N)，大备份
        # （20 万消息会话上限）会把恢复拖成分钟级
        existing = self._session_id_set()
        for session_id, title in titles.items():
            cleaned = str(title).strip()
            if not cleaned or session_id not in existing:
                continue
            stored[session_id] = cleaned[:60]
            applied += 1
        if applied:
            self._persist()
        return applied

    @_locked_mutation
    def set_sessions_pinned(self, session_ids: list[str], pinned: bool = True) -> int:
        """批量置顶 / 取消置顶（备份恢复）：返回实际生效数。"""
        applied = 0
        pinned_sessions = self.data.setdefault("pinned_sessions", [])
        existing = self._session_id_set()
        for session_id in session_ids:
            if not session_id or session_id not in existing:
                continue
            if pinned and session_id not in pinned_sessions:
                pinned_sessions.append(session_id)
            elif not pinned and session_id in pinned_sessions:
                pinned_sessions.remove(session_id)
            applied += 1
        if applied:
            self._persist()
        return applied

    @_locked_mutation
    def set_sessions_archived(
        self, session_ids: list[str], archived: bool = True
    ) -> int:
        """批量归档 / 恢复（备份恢复）：返回实际生效数。"""
        applied = 0
        archived_sessions = self.data.setdefault("archived_sessions", [])
        existing = self._session_id_set()
        for session_id in session_ids:
            if not session_id or session_id not in existing:
                continue
            if archived and session_id not in archived_sessions:
                archived_sessions.append(session_id)
            elif not archived and session_id in archived_sessions:
                archived_sessions.remove(session_id)
            applied += 1
        if applied:
            self._persist()
        return applied

    def _apply_trip_locked(self, trip_info: dict[str, Any]) -> tuple[str, bool]:
        """在写锁内保存一条行程，返回 ``(trip_id, 是否新建)``（不落盘）。"""
        existing = self._find_same_trip(trip_info)
        if existing is not None:
            for key in ("purpose", "session_id"):
                value = trip_info.get(key)
                if value is not None:
                    existing[key] = value
            existing["timestamp"] = datetime.now().isoformat()
            trip_id = str(existing.get("trip_id") or "")
            if not trip_id:
                # 旧数据里可能有没有 trip_id 的历史行程：去重命中时补发
                # 编号，否则调用方拿到空串，定位不到刚保存的行、把自己
                # 报成冲突（面板重复提示重叠）
                trip_id = self._next_trip_id()
                existing["trip_id"] = trip_id
            return trip_id, False

        trip_record = {
            "trip_id": self._next_trip_id(),
            "timestamp": datetime.now().isoformat(),
            **trip_info
        }

        self.data["trip_history"].append(trip_record)

        # 更新统计信息
        self.data["statistics"]["total_trips"] += 1

        # 更新常去目的地统计：与批量保存 / 删除 / 迁移重建同口径，
        # 出发日期不可解析的行程（备份保真保留的旧格式）不计数
        destination = trip_info.get("destination")
        if destination and _trip_date_is_valid(trip_info.get("start_date")):
            freq = self.data["statistics"]["frequent_destinations"]
            freq[destination] = freq.get(destination, 0) + 1
        return str(trip_record["trip_id"]), True

    @_locked_mutation
    def save_trip_history(
        self, trip_info: dict[str, Any], *, max_total: int | None = None
    ) -> str:
        """保存行程历史，返回 trip_id。

        同路线 + 同起止日期的行程视为重复记录（多次对话规划同一趟行程），
        更新既有记录而不是追加，避免行程 / 统计 / 日历重复。
        ``max_total`` 给出时在写锁内拒绝「新建」超过上限（去重更新不受
        影响）：调用方在锁外先读长度再写的检查挡不住并发创建，越限后
        /api/export 会因条数上限永久 413。
        """
        if max_total is None:
            # 主写入路径（编排器 / 面板之外的调用方）不传参也要拦截：
            # 超过上限后 /api/export 与 CLI user-export 会永久 413
            try:
                from utils.trip_import import MAX_TOTAL_TRIPS

                max_total = MAX_TOTAL_TRIPS
            except Exception:  # noqa: BLE001 - 依赖缺失时按无上限
                max_total = None
        if max_total is not None and self._find_same_trip(trip_info) is None:
            current = len(self.data.get("trip_history") or [])
            if current >= max_total:
                raise ValueError(f"行程数量已达上限（{max_total}），请先删除旧行程")
        trip_id, created = self._apply_trip_locked(trip_info)
        self._persist()
        if created:
            logger.info(f"Saved trip history: {trip_id}")
        else:
            logger.info(f"Updated existing trip history: {trip_id}")
        return trip_id

    @_locked_mutation
    def save_trip_histories(self, trips: list[dict[str, Any]]) -> tuple[int, int]:
        """批量保存行程（备份恢复 / 导入）：一次重载 + 一次原子写回。

        逐条调用 ``_apply_trip_locked`` 时，每条都要全量扫描历史找重复、
        再全量扫描生成下一个 id（O(行数 × 历史条数)），几千条导入会被拖成
        分钟级。这里先建一次「去重键 → 记录」索引与最大编号，再逐条写入；
        语义与单条保存一致（同路线 + 同起止日期更新既有记录、未提供的
        目的 / 会话保留原值、删除后的编号不复用）。返回 ``(新增数, 更新数)``。
        """
        from utils.trip_import import normalize_identity_date, normalize_identity_text

        def _identity(trip: dict[str, Any]) -> tuple[str, str, str, str] | None:
            destination = trip.get("destination")
            start = normalize_identity_date(trip.get("start_date"))
            if not destination or not start:
                return None
            origin = normalize_identity_text(trip.get("origin"))
            end = normalize_identity_date(trip.get("end_date")) or start
            return (normalize_identity_text(destination), origin, start, end)

        history = self.data.setdefault("trip_history", [])
        stats = self.data.setdefault("statistics", {})
        index: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        # 已占用的 trip_id：备份里的原编号只在未被其它行程占用时沿用，
        # 否则两趟不同行程共享 trip_id（按 id 删除 / 更新会误伤）
        used_trip_ids: set[str] = set()
        max_number = int(stats.get("last_trip_number") or 0)
        for trip in history:
            key = _identity(trip)
            trip_id_text = str(trip.get("trip_id") or "")
            if trip_id_text:
                used_trip_ids.add(trip_id_text)
            if key is not None:
                index.setdefault(key, trip)
            match = re.fullmatch(r"trip_(\d+)", str(trip.get("trip_id", "")))
            if match:
                max_number = max(max_number, int(match.group(1)))

        created = 0
        updated = 0
        for row in trips:
            key = _identity(row)
            existing = index.get(key) if key is not None else None
            if existing is not None:
                for field in ("purpose",):
                    value = row.get(field)
                    if value is not None:
                        existing[field] = value
                # session_id 键存在即覆盖（含显式 None：备份恢复时该会话
                # 已被删除 / 命中墓碑，需要清除关联）；键缺失保留原值
                if "session_id" in row:
                    existing["session_id"] = row.get("session_id")
                existing["timestamp"] = (
                    row.get("timestamp") or datetime.now().isoformat()
                )
                updated += 1
                continue
            max_number += 1
            record: dict[str, Any] = {
                "trip_id": f"trip_{max_number}",
                "timestamp": datetime.now().isoformat(),
                **row,
            }
            # 备份恢复的行可能自带 trip_id：未被其它行程占用时沿用原编号，
            # 并抬高编号水位（否则同批后续记录可能与旧编号冲突）；与既有
            # 行程冲突 / 非法时退回本次生成的新编号，保持 id 全局唯一
            backup_id = str(record.get("trip_id") or "")
            match = re.fullmatch(r"trip_(\d+)", backup_id)
            if match and backup_id not in used_trip_ids:
                max_number = max(max_number, int(match.group(1)))
            else:
                record["trip_id"] = f"trip_{max_number}"
            used_trip_ids.add(str(record["trip_id"]))
            stats["last_trip_number"] = max_number
            history.append(record)
            stats["total_trips"] = int(stats.get("total_trips", 0)) + 1
            destination = record.get("destination")
            if destination and _trip_date_is_valid(record.get("start_date")):
                # 与面板「到访城市」同口径：日期不可解析的僵尸行程只计入
                # 总行程数，不累计常去目的地
                frequent = stats.setdefault("frequent_destinations", {})
                frequent[destination] = int(frequent.get(destination, 0)) + 1
            if key is not None:
                index[key] = record
            created += 1
        if created or updated:
            self._persist()
        logger.info("Bulk saved %d trips (%d new)", created + updated, created)
        return created, updated

    def _find_same_trip(self, trip_info: dict[str, Any]) -> dict[str, Any] | None:
        """查找同路线、同起止日期的既有行程（无出发日期时不参与去重）。"""
        from utils.trip_import import normalize_identity_date, normalize_identity_text

        destination = trip_info.get("destination")
        start_date = normalize_identity_date(trip_info.get("start_date"))
        if not destination or not start_date:
            return None
        destination_key = normalize_identity_text(destination)
        origin = normalize_identity_text(trip_info.get("origin"))
        end_date = normalize_identity_date(trip_info.get("end_date")) or start_date
        for trip in self.data.get("trip_history", []):
            if (
                normalize_identity_text(trip.get("destination")) == destination_key
                and normalize_identity_text(trip.get("origin")) == origin
                and normalize_identity_date(trip.get("start_date")) == start_date
                and (
                    normalize_identity_date(trip.get("end_date"))
                    or normalize_identity_date(trip.get("start_date"))
                )
                == end_date
            ):
                return trip
        return None

    def _next_trip_id(self) -> str:
        """下一个行程 id：持久化序号 + 1（删除后不复用，避免与旧 ID 混淆）。"""
        stats = self.data.setdefault("statistics", {})
        max_number = int(stats.get("last_trip_number") or 0)
        for trip in self.data.get("trip_history", []):
            match = re.fullmatch(r"trip_(\d+)", str(trip.get("trip_id", "")))
            if match:
                max_number = max(max_number, int(match.group(1)))
        number = max_number + 1
        stats["last_trip_number"] = number
        return f"trip_{number}"

    def _remove_trip_row(self, trips: list[dict], index: int) -> None:
        """从内存快照移除行程行并同步统计（不落盘）。"""
        removed = trips.pop(index)
        stats = self.data.setdefault("statistics", {})
        stats["total_trips"] = max(0, int(stats.get("total_trips", 0)) - 1)
        destination = removed.get("destination")
        frequent = stats.setdefault("frequent_destinations", {})
        # 与保存 / 迁移重建同口径：只有出发日期可解析的行程计入常去目的地。
        # 备份恢复可能保真保留 ``2026/03/12`` 这类旧格式日期，它们在保存
        # 时不计数；删除时不做同样的过滤会把合法城市的计数误清零
        if (
            destination
            and destination in frequent
            and _trip_date_is_valid(removed.get("start_date"))
        ):
            frequent[destination] = int(frequent[destination]) - 1
            if frequent[destination] <= 0:
                del frequent[destination]

    def _drop_trip_row(self, trips: list[dict], index: int) -> None:
        """移除行程行并同步统计；写盘失败按持久化错误抛出。"""
        self._remove_trip_row(trips, index)
        if not self._save():
            raise MemoryPersistenceError("写入长期记忆失败")

    @_locked_mutation
    def delete_trip(self, trip_id: str) -> bool:
        """删除一条历史行程并同步统计；返回是否删除。"""
        trips = self.data.get("trip_history", [])
        for index, trip in enumerate(trips):
            if str(trip.get("trip_id", "")) == str(trip_id):
                self._drop_trip_row(trips, index)
                logger.info(f"Deleted trip history: {trip_id}")
                return True
        return False

    @_locked_mutation
    def delete_duplicate_trip(self, trip_id: str) -> bool:
        """删除重复 trip_id 中最后插入的一条（去重时保留最早记录）。

        ``delete_trip`` 删除最早一条：旧版本可能给两条不同行程生成同一个
        trip_id，去重命令要保留组内第一条、删掉后面的副本，用它会把要
        保留的那条删掉、留下后来的重复行。

        只剩一条同 id 行时返回 False（不删除）：两个并发的去重请求
        （双标签页 / CLI+Web）各自的计划都会执行一次本方法，第二次必须
        不能把仅剩的保留行删光。
        """
        trips = self.data.get("trip_history", [])
        indices = [
            index
            for index, trip in enumerate(trips)
            if str(trip.get("trip_id", "")) == str(trip_id)
        ]
        if len(indices) < 2:
            return False
        self._drop_trip_row(trips, indices[-1])
        logger.info(f"Deleted duplicate trip history: {trip_id}")
        return True

    @_locked_mutation
    def delete_trips_verified(
        self, targets: list[dict[str, Any]]
    ) -> tuple[int, str | None]:
        """批量按身份复核删除（去重专用）：一次重载 + 一次原子写回。

        ``targets`` 每项含 ``trip_id`` 与计划身份（``origin`` /
        ``destination`` / ``start_date`` / ``end_date``），可选 ``kept_id``
        （保留项须仍以该身份存在）与 ``prefer_last``（同 id 副本删最后
        一条）。身份与当前记录不一致、行或保留项已消失时跳过并计数告警。

        逐条调用 ``delete_trip`` 时每次都会整文件重载 + 整文件写回
        （O(重复数 × 文件大小)），这里在同一把写锁内完成复核与删除、只
        落盘一次；复核与删除也因此不可被并发写者穿插（verify→delete 的
        TOCTOU 窗口关闭）。返回 ``(删除数, 失败原因或 None)``。
        """
        from utils.trip_import import trip_identity

        trips = self.data.get("trip_history", [])
        removed = 0
        skipped = 0
        for target in targets:
            trip_id = str(target.get("trip_id"))
            if not trip_id:
                continue
            want = trip_identity(target)
            matches = [
                index
                for index, trip in enumerate(trips)
                if str(trip.get("trip_id")) == trip_id
                and trip_identity(trip) == want
            ]
            if not matches:
                skipped += 1
                continue
            kept_id = str(target.get("kept_id") or "")
            if kept_id and not any(
                str(trip.get("trip_id")) == kept_id
                and trip_identity(trip) == want
                for trip in trips
            ):
                skipped += 1
                continue
            prefer_last = bool(target.get("prefer_last"))
            if prefer_last and len(matches) < 2:
                # 同 id 只剩一条身份副本：并发去重已清过，不能再删仅剩的保留行
                skipped += 1
                continue
            self._remove_trip_row(trips, matches[-1] if prefer_last else matches[0])
            removed += 1
        if removed and not self._save():
            raise MemoryPersistenceError("写入长期记忆失败")
        if skipped:
            logger.warning(
                "去重跳过 %d 条：计划生成后行程已变更或保留项已删除", skipped
            )
        return removed, None

    @_locked_mutation
    def update_trip(self, trip_id: str, fields: dict[str, Any]) -> bool:
        """更新一条行程的字段；返回是否存在。

        Raises:
            ValueError: 更新后与另一条既有行程（同路线 + 同起止日期）重复
        """
        for trip in self.data.get("trip_history", []):
            if str(trip.get("trip_id", "")) != str(trip_id):
                continue
            merged = {**trip, **fields}
            same = self._find_same_trip(merged)
            if same is not None and same is not trip:
                raise ValueError("已存在相同行程（同路线 + 同起止日期）")

            old_destination = trip.get("destination")
            old_start = trip.get("start_date")
            for key in ("origin", "destination", "start_date", "end_date", "purpose"):
                if key in fields:
                    trip[key] = fields[key]
            new_destination = trip.get("destination")
            new_start = trip.get("start_date")
            # 常去目的地按「出发日期可解析」计数（与批量保存 / 删除 / 迁移
            # 重建及 PostgreSQL 同口径）：只有贡献状态发生变化的行程才调整，
            # 脏日期行程既不能扣掉合法城市的计数、也不能被虚增
            old_contributes = bool(old_destination) and _trip_date_is_valid(old_start)
            new_contributes = bool(new_destination) and _trip_date_is_valid(new_start)
            if old_destination != new_destination or old_contributes != new_contributes:
                stats = self.data.setdefault("statistics", {})
                frequent = stats.setdefault("frequent_destinations", {})
                if old_contributes and old_destination in frequent:
                    frequent[old_destination] = int(frequent[old_destination]) - 1
                    if frequent[old_destination] <= 0:
                        del frequent[old_destination]
                if new_contributes:
                    frequent[new_destination] = int(
                        frequent.get(new_destination, 0)
                    ) + 1
            self._persist()
            logger.info("Updated trip history: %s", trip_id)
            return True
        return False

    def get_trip_history(self, limit: int | None = 10) -> list[dict[str, Any]]:
        """
        获取历史行程

        Args:
            limit: 返回数量限制

        Returns:
            行程列表
        """
        return self.data["trip_history"][-limit:] if limit else self.data["trip_history"]

    def get_frequent_destinations(self, top_n: int = 5) -> list[tuple]:
        """
        获取常去目的地

        Args:
            top_n: 返回前N个

        Returns:
            [(destination, count), ...]
        """
        freq = self.data["statistics"]["frequent_destinations"]
        # 同数量时按城市名排序：与 PostgreSQL 的 ORDER BY trip_count DESC,
        # destination 同一口径，展示顺序不再随写入顺序变化
        sorted_dest = sorted(
            freq.items(), key=lambda item: (-int(item[1]), str(item[0]))
        )
        return sorted_dest[:top_n]

    @_locked_mutation
    def increment_query_count(self):
        """增加查询计数"""
        stats = self.data["statistics"]
        stats["total_queries"] = stats.get("total_queries", 0) + 1
        self._persist()
    def get_statistics(self) -> dict[str, Any]:
        """获取统计信息"""
        return self.data["statistics"].copy()

    def export_snapshot(self) -> dict[str, Any]:
        """导出一致快照：写锁内重载后逐项读取，避免与并发写入交错。

        导出（备份 / 面板 / CLI）此前逐项调用 get_*，其它线程 / 进程在两次
        读取之间写入时，统计与列表会来自不同版本（备份里总数与消息条数
        对不上）。写锁同时覆盖进程内线程与跨进程文件锁。
        """
        with self._write_lock:
            self._reload_latest()
            return {
                "preferences": self.get_preference(),
                "trip_history": self.get_trip_history(limit=None),
                "chat_history": self.get_chat_history(),
                "session_titles": self.get_session_titles(),
                "pinned_sessions": self.get_pinned_sessions(),
                "archived_sessions": self.get_archived_sessions(),
                "statistics": self.get_statistics(),
            }

    @_locked_mutation
    def clear_history(self):
        """清空历史记录（保留偏好）"""
        self.data["chat_history"] = []
        self.data["trip_history"] = []
        self.data["session_titles"] = {}
        self.data["pinned_sessions"] = []
        self.data["archived_sessions"] = []
        self.data["statistics"]["total_trips"] = 0
        self.data["statistics"]["total_messages"] = 0
        self.data["statistics"]["frequent_destinations"] = {}
        self._persist()
        logger.info("Cleared all history (chat + trips)")

    @_locked_mutation
    def delete_all(self):
        """删除所有数据（包括文件）。

        删除后必须重置内存快照：``_reload_latest`` 把「文件不存在」当作
        无需更新，不清空快照的话，同一实例的下一次写入会把已删除的数据
        整份写回。
        """
        with self._write_lock:
            try:
                os.remove(self.db_path)
                logger.warning(f"Deleted long-term memory file: {self.db_path}")
            except FileNotFoundError:
                pass
            self.data = self._init_data()
