"""
长期记忆 (Long-term Memory)
持久化存储用户信息，支持跨会话访问
"""
import contextlib
import functools
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
import unicodedata
import time
import weakref
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from itertools import repeat
from pathlib import Path
from typing import Any, Iterator

from utils.chat_import import (
    filter_import_messages,
    prepare_import_messages,
    session_dedupe_key,
)
from utils.errors import MemoryPersistenceError
from utils.file_lock import file_lock
from utils.preference_values import (
    merge_preference_value,
    preference_value_problem,
    has_preference_value,
)
from utils.timestamps import chronological_key

logger = logging.getLogger(__name__)


def _trip_date_is_valid(value: object) -> bool:
    """行程出发日期是否为可解析的 YYYY-MM-DD（常去目的地统计口径）。

    常去目的地是存储维护的累计计数，只按严格 ISO 日期计入（备份保真
    保留的斜杠旧格式不计入，修正日期后自然补计）；读取侧的冲突 / 状态 /
    travel_summary 走 trip_interval 的身份口径，两者分工不同。
    """
    try:
        datetime.strptime(str(value or ""), "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _trip_date_value(value: object) -> datetime | None:
    """按身份口径解析行程日期（补零 / 斜杠归一）；不可解析返回 None。"""
    from utils.trip_import import normalize_identity_date

    try:
        return datetime.strptime(normalize_identity_date(value), "%Y-%m-%d")
    except ValueError:
        return None


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


def _session_ref_matches(value: object, session_id: str) -> bool:
    """会话元数据（置顶 / 归档）条目是否指向该会话：与消息同一字符串口径。

    旧数据里 pinned_sessions / archived_sessions 可能存整数 ID；精确类型
    成员判断既清不掉删除会话留下的整数幽灵，也会让批量恢复把 ``"42"``
    追加在 ``42`` 旁边形成重复条目。``None`` 跳过（避免 ``str(None)`` 造出
    名为 None 的幽灵引用），其余按字符串比较，与 PostgreSQL（TEXT 列）一致。
    """
    return value is not None and str(value) == session_id


def _drop_session_refs(values: Any, session_id: str) -> bool:
    """从置顶 / 归档列表中移除全部指向该会话的条目；返回是否有改动。

    一次移除所有等价条目（``42`` 与 ``"42"`` 并存的历史数据）；非列表
    形状保持原样（与既有防御一致）。
    """
    if not isinstance(values, list):
        return False
    kept = [
        item for item in values if not _session_ref_matches(item, session_id)
    ]
    if len(kept) == len(values):
        return False
    values[:] = kept
    return True


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


def _stat_fingerprint(stat_info: os.stat_result) -> tuple[int, int, int] | None:
    """文件身份指纹 (ino, size, mtime_ns)；ino 不可用时返回 None。

    所有写者都以 ``mkstemp + fsync + os.replace`` 原子替换提交，替换后
    ino 与 mtime 必变；FAT 等 mtime 粒度粗的文件系统由 ino 变化兜底。
    ctime 不参与：Windows 上 ``os.fstat``（读取句柄）与 ``os.stat``（路径）
    的 st_ctime 口径不一致，会把「刚读过的同一文件」误判为已变化。
    ino 为 0 的平台（部分网络文件系统）保守返回 None，调用方退化为每次重读。
    """
    try:
        inode = int(stat_info.st_ino)
        if inode == 0:
            return None
        return (
            inode,
            int(stat_info.st_size),
            int(stat_info.st_mtime_ns),
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _postgres_configured() -> bool:
    """是否配置了 PostgreSQL 后端（JSON 只是故障回退时的临时载体）。"""
    try:
        from config import STORAGE_CONFIG

        return bool(STORAGE_CONFIG.get("postgres_dsn"))
    except Exception:  # noqa: BLE001 - 读取失败按未配置
        return False


def _preference_cap() -> int | None:
    """偏好类型数上限（与导入 / 导出共用 ``utils.user_import`` 的常量）。"""
    try:
        from utils.user_import import _MAX_PREFERENCES

        return _MAX_PREFERENCES
    except Exception:  # noqa: BLE001 - 依赖缺失时按无上限
        return None


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
        if self._batch_owner == threading.get_ident():
            # 批量写内部：外层 write_batch 已持锁并重载过；落盘由批结束时
            # 统一完成，批内异常整批回滚（见 write_batch）
            self._dirty = True
            try:
                return func(self, *args, **kwargs)
            except BaseException:
                self._dirty = True
                raise
        with self._write_lock:
            try:
                with file_lock(self.db_path):
                    # 加载时没能确认的损坏文件在这里补复核：否则空快照会把
                    # 磁盘上的损坏文件直接覆盖且不产生 .corrupt-* 备份
                    self._ensure_corrupt_backed_up()
                    self._reload_latest()
                    # 修改开始：成功写盘由 _save 复位；中途异常 / 未落盘的
                    # 修改保持 dirty，下一次重载会把它们丢弃（现行语义）
                    self._dirty = True
                    try:
                        return func(self, *args, **kwargs)
                    except BaseException:
                        self._dirty = True
                        raise
            except TimeoutError as exc:
                # 写锁超时按持久化失败处理：不触发整轮对话重试 / 熔断器
                raise MemoryPersistenceError(f"长期记忆写锁等待超时：{exc}") from exc
            finally:
                # 写前补备份的「允许重建」只覆盖本次写入：长会话后续再遇
                # 外部隔离（主文件缺失 + 存在备份）时必须重新被守卫拦截
                self._may_recreate = False

    return wrapper


# 隔离标记存在但找不到任何可用隔离备份（标记损坏 / 备份已被清理）：
# fail-closed 的独立状态，上层据此给「无备份可恢复」的文案
_QUARANTINE_NO_BACKUP_MARKER = "__quarantine_no_backup__"


def _id_identity_key(value: str) -> str:
    """账号"同一文件"判定键：先 NFC 归一、再 casefold。

    大小写不敏感（Windows / macOS 默认）之外，macOS 的 APFS/HFS+ 文件名
    对 NFC/NFD 规范等价也不敏感：`José`（NFC）与 `Jose`+U+0301（NFD）会
    解析到同一 `<user_id>.json`。只比较 casefold 时两者不等，归属守卫被
    绕过；先做 NFC 归一才能识别这类同一文件的两个拼写。
    """
    return unicodedata.normalize("NFC", value).casefold()


def _case_conflict_message(owner: str) -> str:
    return (
        f"用户 ID 与已有记忆文件仅大小写或 Unicode 规范化形式不同"
        f"（已有 {owner}）。当前文件系统大小写 / 规范化不敏感，两个 ID 会"
        "读写同一份数据；请使用与已有账号完全一致的大小写与字符形式"
    )


def _reject_reserved_preference_type(pref_type: Any) -> None:
    """拒绝保留偏好类型 ``preferences``（JSON 存储层写路径统一守卫）。

    ``_migrate_data`` 在下一次加载时把该类型的列表值展开成独立偏好类型
    （并丢弃 / 覆盖同名条目）：写入「成功」后偏好数会突然膨胀、可把类型
    数推过上限让账号永久无法导出。Web PUT / CLI pref-set / 导入解析都已
    拒绝，存储层是智能体等直连调用方的最后一道守卫。
    """
    if pref_type == "preferences":
        raise ValueError("preferences 是保留的偏好类型，请使用具体类型名")


class LongTermMemory:
    """
    长期记忆：持久化用户信息
    - 用户偏好（家庭地址、酒店品牌、航空公司等）
    - 历史行程记录
    - 统计信息
    """

    # JSON 实现把数据快照放在内存中，多会话写入后需要重新加载
    snapshot_based = True

    def __init__(
        self,
        user_id: str,
        storage_path: str = "data/memory",
        *,
        allow_quarantined: bool = False,
        pg_fallback: bool = False,
    ):
        """
        初始化长期记忆

        Args:
            user_id: 用户ID
            storage_path: 存储路径
            allow_quarantined: 允许在「主文件缺失 + 存在隔离备份」时重建
                主文件（仅恢复入口使用：user-import / /api/import）
            pg_fallback: 本实例是 PostgreSQL 故障期的回退存储（工厂置位）：
                大小写 / 规范化冲突是瞬时状态（PG 里两账号合法并存），
                错误文案按「稍后重试」而非「更改 ID」给出
        """
        self.user_id = user_id
        # 相对路径锚定到项目根目录，避免从其它目录运行时数据写到 CWD（Issue #5）
        storage = Path(storage_path)
        if not storage.is_absolute():
            storage = Path(__file__).resolve().parent.parent / storage
        self.storage_path = str(storage)
        self.db_path = os.path.join(self.storage_path, f"{user_id}.json")
        self._write_lock = _lock_for_path(self.db_path)
        self._allow_quarantined = allow_quarantined
        self._pg_fallback = pg_fallback

        # 确保存储目录存在
        Path(self.storage_path).mkdir(parents=True, exist_ok=True)

        # 加载或初始化数据
        # 「加载时没能确认损坏文件已备份」的标记：等锁超时 / 备份失败时置位，
        # 下一次持锁写入前必须复核并补备份，否则空快照会直接覆盖损坏文件
        self._corrupt_pending = False
        # 本次启动时被隔离的损坏记忆文件路径（供 CLI 报告，无损坏为 None）
        self._corrupt_backup: str | None = None
        # 写前复核（_ensure_corrupt_backed_up）亲自补备份后允许在空快照上
        # 继续完成本次写入；构造期检测到损坏、或「主文件缺失 + 存在别人
        # 的隔离备份」都不放行写回——先打开的长会话（Web 缓存会话 /
        # CLI 交互）不能把隔离掩盖成正常写入
        self._may_recreate = False
        # 本实例「当场检测到损坏」（区别于载入时发现的历史遗留备份）：
        # 上层守卫在 PostgreSQL 故障回退 JSON 时也只认这一类
        self._fresh_quarantine = False
        # 已解析内容的磁盘指纹 (ino, size, mtime_ns)：再次读取前
        # 先 stat 比对，未变化就不重复整份解析（导出快照等读路径每请求会
        # 打开新实例，构造时刚解析过同一文件）。None = 尚未确认，必须重载
        self._loaded_stat: tuple[int, int, int] | None = None
        # 内存快照是否可能领先于磁盘（构造 / 重载成功后为 False；写操作
        # 开始置 True、成功写盘复位；写失败保持 True 并在下次重载时丢弃
        # 未落盘的修改）。长寿命实例（交互 REPL）的 reload 据此跳过重读
        self._dirty = True
        # 批量写（write_batch）：持有者线程 + 重入深度 + 是否有待落盘修改。
        # 一轮对话的多处落库合并为一次重载 + 一次写回，线程绑定保证其它
        # 线程的写请求仍各自加锁（阻塞到本批结束）
        self._batch_owner: int | None = None
        self._batch_depth = 0
        self._batch_dirty = False
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
                    # 读取耗时的大头，先读完再解析可把阻塞写者的窗口压到最短。
                    # 指纹在 read 之前 fstat：若存在原地写者，得到的是不晚于
                    # 所读字节的指纹，只会多一次重载、不会假命中
                    with open(self.db_path, "r", encoding="utf-8") as f:
                        stat_info = os.fstat(f.fileno())
                        raw = f.read()
                    data = self._migrate_data(json.loads(raw))
                    # 大小写不敏感文件系统上另一账号可能映射到同一文件：
                    # 这是身份冲突而不是文件损坏，不能走隔离分支
                    self._check_owner_case(data)
                    self._loaded_stat = _stat_fingerprint(stat_info)
                    self._dirty = False
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
                                    recheck_stat = os.fstat(f.fileno())
                                    latest_raw = f.read()
                                latest = json.loads(latest_raw)
                                # 与首次读取一致：合法 JSON 但不是对象 /
                                # 字段结构异常同样视为损坏，不能逃逸
                                if not isinstance(latest, dict):
                                    raise ValueError("记忆文件内容不是 JSON 对象")
                                latest = self._migrate_data(latest)
                            except FileNotFoundError:
                                return self._init_data()
                            except OSError as load_error:
                                # 复核仍读不了（锁定 / I-O 故障）：内容未
                                # 证实损坏，禁止改名隔离与落标记
                                raise MemoryPersistenceError(
                                    "长期记忆暂时不可读（文件可能被占用）"
                                    f"（{self.db_path}）：{load_error}"
                                ) from load_error
                            except Exception:
                                # 备份名带微秒与进程号，同一秒内多次损坏
                                # 不会互相覆盖；文件名前缀沿用磁盘真实
                                # 大小写（大小写不敏感 FS 上决定账号归属）
                                directory = (
                                    os.path.dirname(self.db_path) or "."
                                )
                                actual_name = self._actual_memory_filename(
                                    os.path.basename(self.db_path)
                                )
                                backup = os.path.join(
                                    directory,
                                    f"{actual_name}.corrupt-"
                                    f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
                                    f"-{os.getpid()}",
                                )
                                try:
                                    os.replace(self.db_path, backup)
                                except OSError as backup_error:
                                    # 文件可能仍被其它进程持有：先以空快照启动，
                                    # 但写入前必须补备份（见 _ensure_corrupt_backed_up）
                                    self._corrupt_pending = True
                                    logger.error(
                                        "备份损坏的记忆文件失败: %s", backup_error
                                    )
                                    return self._init_data()
                                if not self._record_quarantine_marker(backup):
                                    # 标记是跨实例唯一的隔离证据：写不进去就
                                    # 把损坏文件放回原位，让每个实例继续走
                                    # 「解析失败 → 隔离」路径（fail-closed），
                                    # 而不是留下无标记孤儿备份、被 DSN 回退
                                    # 的实例当成「用户没有数据」以空快照覆盖
                                    try:
                                        os.replace(backup, self.db_path)
                                    except OSError as restore_error:
                                        logger.critical(
                                            "隔离标记写入失败且无法还原损坏文件: %s",
                                            restore_error,
                                        )
                                    raise MemoryPersistenceError(
                                        "记忆文件损坏且无法记录隔离标记，"
                                        f"已拒绝以空快照启动（{self.db_path}）"
                                    )
                                self._corrupt_backup = backup
                                self._fresh_quarantine = True
                                logger.warning("损坏的记忆文件已备份到 %s", backup)
                                return self._init_data()
                            # 复核成功同样要校验大小写冲突：瞬时读失败
                            # 改道进入本分支后不能绕过 owner 检查、读到
                            # 另一账号的数据。放在内层 except 之外，避免
                            # MemoryPersistenceError 被当成损坏隔离
                            self._check_owner_case(latest)
                            self._loaded_stat = _stat_fingerprint(recheck_stat)
                            self._dirty = False
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
                quarantined, marker_backup = self._read_quarantine_marker()
                backups = self._corrupt_siblings()
                tombstone_exists = Path(f"{self.db_path}.deleted").exists()
                live_backups = backups
                if tombstone_exists:
                    # 墓碑之后没有新损坏的备份是孤儿；mtime 相等按新隔离
                    live_backups = [
                        item
                        for item in backups
                        if not self._quarantine_is_deleted_orphan(str(item))
                    ]
                evidence_backup: str | None = None
                if quarantined:
                    evidence_backup = marker_backup or (
                        str(backups[-1]) if backups else None
                    )
                elif live_backups:
                    evidence_backup = str(live_backups[-1])
                conflict_owner = self._marker_owner_conflict(evidence_backup)
                if conflict_owner is not None:
                    # 隔离证据属于仅大小写不同的另一账号（同一路径）：
                    # 恢复入口也不能接管，标记与备份原样保留
                    raise MemoryPersistenceError(
                        _case_conflict_message(conflict_owner)
                    )
                if quarantined:
                    # 隔离标记（当初隔离的实例落盘）：与当场隔离同等对待，
                    # PostgreSQL 配置不能成为空快照重建主文件的豁免
                    self._fresh_quarantine = True
                    if marker_backup:
                        self._corrupt_backup = marker_backup
                    elif backups:
                        # 标记损坏 / 缺备份字段：恢复引导指向真实备份，
                        # 并重写标记（后续实例直接读到可用路径）
                        self._corrupt_backup = str(backups[-1])
                        self._record_quarantine_marker(self._corrupt_backup)
                    logger.warning(
                        "检测到隔离标记（备份：%s）", self._corrupt_backup
                    )
                elif tombstone_exists:
                    if live_backups:
                        # 晚于墓碑的新隔离（标记写失败 / 崩溃窗口）：
                        # 仍按隔离处理，不落入「账号已删除」空快照
                        self._fresh_quarantine = True
                        self._corrupt_backup = str(live_backups[-1])
                        logger.warning(
                            "记忆主文件缺失且存在晚于墓碑的隔离备份：%s",
                            live_backups[-1],
                        )
                    else:
                        logger.info("账号数据已删除，从空快照启动")
                elif backups:
                    # 主文件已被其它进程 / 实例隔离：把标记带在新实例上，
                    # 上层守卫（CLI / Web / 导出）据此识别「数据被隔离」，
                    # 而不是当成「用户没有数据」返回空备份
                    self._corrupt_backup = str(backups[-1])
                    logger.warning(
                        "记忆主文件缺失且存在隔离备份：%s", backups[-1]
                    )
                else:
                    logger.info("No existing long-term memory, creating new")
                return self._init_data()

    def _corrupt_siblings(self) -> list[Path]:
        """主文件同目录下的 ``<user>.json.corrupt-*`` 隔离备份（按名排序）。"""
        try:
            base = Path(self.db_path)
            return sorted(base.parent.glob(base.name + ".corrupt-*"))
        except OSError:
            return []

    def _actual_memory_filename(self, name: str) -> str:
        """目标文件在磁盘上的真实文件名（保留原大小写）。

        大小写不敏感文件系统上 ``alice.json`` 可能就是 ``Alice.json``：
        隔离改名必须沿用磁盘上的真实大小写，否则备份名会把「谁先发现」
        误当成账号归属（恢复入口随后会被错误的归属校验锁死）。

        精确同名优先：大小写敏感文件系统上 ``alice`` / ``Alice`` 可以是
        两个独立账号，scandir 顺序未定义——先命中的大小写变体不能被
        当成本文件的真实名（否则隔离改名会把备份写到对方名下，本账号
        随后被归属校验永久拒绝，对方还能把它当自己的备份删掉）。
        """
        directory = os.path.dirname(self.db_path) or "."
        fallback: str | None = None
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.name == name:
                        return entry.name
                    if (
                        fallback is None
                        and entry.name.casefold() == name.casefold()
                    ):
                        fallback = entry.name
        except OSError:
            pass
        if fallback is not None:
            # 大小写不敏感别名：确认指向的就是本实例的文件（同目录下
            # 另一个独立账号的变体文件不能被采用）
            candidate = os.path.join(directory, fallback)
            target = os.path.join(directory, name)
            try:
                if os.path.samefile(candidate, target):
                    return fallback
            except OSError:
                pass
            return name
        return name

    def _quarantine_marker_path(self) -> Path:
        return Path(f"{self.db_path}.quarantine")

    def _record_quarantine_marker(self, backup: str) -> bool:
        """把「当场隔离」事实落盘（``<user>.json.quarantine``），返回是否成功。

        ``_fresh_quarantine`` 只存在于实例内存：配置了 PostgreSQL 时
        ``MemoryManager`` 每次写前重建实例，其它 worker 也会新建实例，
        重建后的实例只看到「主文件缺失 + 遗留备份」，会把隔离当成孤儿
        并以空快照重建主文件。标记让隔离跨实例可见；没有标记的历史遗留
        备份仍走 DSN 豁免，不会被误判。

        写入不能是尽力而为：标记丢失后 DSN 回退实例会把隔离当成孤儿、
        用空快照覆盖主文件（备份从此不可见）。os.replace 与 ``_save`` /
        ``_clear_quarantine_marker`` 同一 1.5s 重试预算（Windows 共享
        冲突 / 杀软扫描）；仍失败时返回 False，由调用方把损坏文件放回
        原位、继续按损坏 fail-closed，而不是留下一份无标记的孤儿备份。
        """
        marker = self._quarantine_marker_path()
        try:
            # 原子写（临时文件 + fsync + os.replace）：截断的标记会让恢复
            # 引导指向标记自身；掉电 / 磁盘满也不能留下半截 JSON
            fd, temp_path = tempfile.mkstemp(
                prefix=f".{self.user_id}.",
                suffix=".quarantine.tmp",
                dir=str(marker.parent),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(
                        {
                            "backup": backup,
                            "at": datetime.now().isoformat(),
                            # 不写 user_id：检测时文件已损坏、真实归属
                            # 不可知，写入检测者 ID 会让大小写变体劫持
                            # 归属；归属只从备份文件名推断
                        },
                        handle,
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
                deadline = time.monotonic() + 1.5
                while True:
                    try:
                        os.replace(temp_path, marker)
                        return True
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.03)
            finally:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
        except OSError as exc:
            logger.error("写入隔离标记失败: %s", exc)
            return False

    def _clear_quarantine_marker(self) -> None:
        """删除隔离标记。

        Windows 上并发读取（Web 探测）会让 unlink 报共享冲突：与 _save
        的 os.replace 同口径短暂重试；仍失败要留日志——静默吞掉会让
        恢复成功的账号被残留标记永久 503。
        """
        marker = self._quarantine_marker_path()
        deadline = time.monotonic() + 1.5
        while True:
            try:
                marker.unlink(missing_ok=True)
                return
            except OSError as exc:
                if time.monotonic() >= deadline:
                    logger.warning("删除隔离标记失败: %s", exc)
                    return
                time.sleep(0.03)

    def _read_quarantine_marker(self) -> tuple[bool, str | None]:
        """读隔离标记，返回 ``(是否隔离中, 可用备份路径)``。

        标记指向的备份已不在（恢复 / 手动清理后）时清除标记并返回
        ``(False, None)``；标记损坏 / 不可读时按「隔离中」保守处理
        （fail-closed），备份路径留给调用方从 ``.corrupt-*`` 回退——
        绝不能把标记文件本身当作隔离备份上报（用户会照提示导入标记）。
        """
        marker = self._quarantine_marker_path()
        read_failed = False
        try:
            raw = marker.read_text(encoding="utf-8")
        except FileNotFoundError:
            return False, None
        except (OSError, UnicodeError) as exc:
            # 非 UTF-8 字节（磁盘损坏 / 外部工具重存）同样是「可读性
            # 故障」，按隔离中 fail-closed（UnicodeError 不是 OSError）
            logger.debug("读取隔离标记失败: %s", exc)
            read_failed = True
            raw = ""
        if os.path.exists(self.db_path):
            # 主文件存在 = 已恢复（恢复写回 / 手工拷回备份）：标记只会在
            # 主文件被移走时写入，此时必然失效。不清除会让账号在产品内
            # 永久 503（导入重试也不清除）
            self._clear_quarantine_marker()
            return False, None
        if read_failed:
            return True, None
        try:
            payload = json.loads(raw)
        except Exception:  # noqa: BLE001 - 标记损坏按隔离中处理
            return True, None
        backup = payload.get("backup") if isinstance(payload, dict) else None
        if isinstance(backup, str) and backup:
            if os.path.exists(backup):
                if self._quarantine_is_deleted_orphan(backup):
                    # 墓碑之后没有新损坏：删除遗留的孤儿备份
                    self._clear_quarantine_marker()
                    return False, None
                return True, backup
            # 备份已不存在：恢复完成 / 手动清理后的失效标记
            self._clear_quarantine_marker()
            return False, None
        return True, None

    def _quarantine_is_deleted_orphan(self, backup: str) -> bool:
        """墓碑 + 隔离备份：删除之后没有新损坏时备份是孤儿。

        与 Web ``_quarantined_memory_backup`` / CLI ``_recovered_corrupt_backup``
        同一口径：备份 mtime 晚于墓碑说明删除后又发生过损坏，仍按隔离处理；
        mtime 无法判定时 fail-closed 按隔离处理。
        """
        tombstone = Path(f"{self.db_path}.deleted")
        try:
            if not tombstone.exists():
                return False
        except OSError:  # noqa: BLE001 - 判定失败按隔离处理
            return False
        try:
            tomb_mtime = tombstone.stat().st_mtime
            backup_mtime = os.path.getmtime(backup)
        except OSError:
            return False
        # 严格早于墓碑才是孤儿；mtime 相等落在文件时间戳同一时钟片，
        # 无法区分「删除后的新损坏」与「孤儿」，按隔离 fail-closed
        return backup_mtime < tomb_mtime

    def _owner_conflict_for_name(
        self, candidate: str, suffix: str
    ) -> str | None:
        """文件名里的账号与实例 user_id 仅大小写不同时返回其 owner。

        ``suffix`` 形如 ``.json.corrupt-`` / ``.json.migrate-backup``。
        """
        name = Path(candidate).name
        expected = f"{self.user_id}{suffix}"
        if name.startswith(expected):
            return None
        index = name.casefold().find(suffix.casefold())
        if index <= 0:
            return None
        owner = name[:index]
        if owner == self.user_id:
            return None
        # 规范化等价（NFC/NFD）与大小写变体在 macOS 上都可能指向同一文件：
        # 隔离备份 / 标记的归属判定必须与 _check_owner_case 同口径
        if _id_identity_key(owner) == _id_identity_key(self.user_id):
            return owner
        return None

    def _marker_owner_conflict(self, backup: str | None) -> str | None:
        """隔离备份 / 标记的账号归属与实例 user_id 仅大小写不同时返回 owner。

        隔离时文件已损坏、真实归属不可知；备份文件名在改名时保留了磁盘
        真实大小写（见 ``_actual_memory_filename``），是唯一可靠的归属
        证据（标记 payload 里旧版写入的 user_id 是「最先发现者」，不可信）。
        """
        if not backup:
            return None
        return self._owner_conflict_for_name(backup, ".json.corrupt-")

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
        if _id_identity_key(owner) != _id_identity_key(self.user_id):
            return
        if self._pg_fallback:
            # PostgreSQL 里两者是合法并存的账号（大小写 / 规范形式无关），
            # 只有回退本地 JSON 时才撞同一文件名：这是「稍后重试」的瞬时
            # 状态，不能提示用户去改 ID 的大小写
            raise MemoryPersistenceError(
                "PostgreSQL 暂不可用，回退本地存储时与另一账号的落地文件"
                f"仅大小写或 Unicode 规范化形式不同（已有 {owner}）；"
                "请稍后重试（PostgreSQL 恢复后本账号自动回到原存储）"
            )
        raise MemoryPersistenceError(_case_conflict_message(owner))

    def _check_owner_case_on_disk(self) -> None:
        """运维动作（delete_all）前的归属复核：主文件可解析时校验
        user_id；解析失败（损坏）按现有路径继续删除。"""
        try:
            with open(self.db_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except Exception:  # noqa: BLE001 - 损坏文件仍可删除
            return
        if isinstance(data, dict):
            self._check_owner_case(data)

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
        except OSError as exc:
            # 瞬时读失败：保留 pending、拒绝本次写入，不得隔离有效文件
            raise MemoryPersistenceError(
                f"长期记忆暂时不可读（文件可能被占用）"
                f"（{self.db_path}）：{exc}"
            ) from exc
        except Exception:  # noqa: BLE001 - 解析失败按损坏处理
            corrupted = True
        if corrupted:
            directory = os.path.dirname(self.db_path) or "."
            actual_name = self._actual_memory_filename(
                os.path.basename(self.db_path)
            )
            backup = os.path.join(
                directory,
                f"{actual_name}.corrupt-"
                f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
                f"-{os.getpid()}",
            )
            try:
                os.replace(self.db_path, backup)
            except OSError as exc:
                raise MemoryPersistenceError(
                    "记忆文件损坏且无法备份，已拒绝覆盖写入"
                    f"（可手动处理 {self.db_path}）"
                ) from exc
            if not self._record_quarantine_marker(backup):
                # 与 _load 同一取舍：无标记的孤儿备份会被 DSN 回退实例
                # 当成「用户没有数据」——把损坏文件放回原位继续按损坏
                # fail-closed（_corrupt_pending 保持，下一次写入重试隔离）
                try:
                    os.replace(backup, self.db_path)
                except OSError as restore_error:
                    logger.critical(
                        "隔离标记写入失败且无法还原损坏文件: %s", restore_error
                    )
                raise MemoryPersistenceError(
                    "记忆文件损坏且无法记录隔离标记，已拒绝覆盖写入"
                    f"（可手动处理 {self.db_path}）"
                )
            self._corrupt_backup = backup
            self._may_recreate = True
            self._fresh_quarantine = True
            logger.warning("损坏的记忆文件已补备份到 %s", backup)
        self._corrupt_pending = False

    def reload(self) -> None:
        """重新从磁盘加载最新数据（供 CLI 并发复核等只读场景调用）。"""
        with self._write_lock:
            self._reload_latest()

    def _reload_latest(self) -> None:
        """从磁盘重载最新数据（写操作前调用，避免旧快照覆盖其它写入）。"""
        try:
            # 与 _load 同一原因：句柄尽早关闭，解析在句柄关闭后进行；
            # 指纹在 read 之前取，只会多一次重载、不会假命中
            with open(self.db_path, encoding="utf-8") as f:
                reload_stat = os.fstat(f.fileno())
                raw = f.read()
            data = json.loads(raw)
        except FileNotFoundError:
            backups = self._corrupt_siblings()
            quarantined, marker_backup = self._read_quarantine_marker()
            tombstone_exists = Path(f"{self.db_path}.deleted").exists()
            live_backups = backups
            if tombstone_exists:
                live_backups = [
                    item
                    for item in backups
                    if not self._quarantine_is_deleted_orphan(str(item))
                ]
            blocking_backups = backups if not tombstone_exists else live_backups
            if quarantined:
                evidence_backup = marker_backup or (
                    str(blocking_backups[-1]) if blocking_backups else None
                )
            else:
                evidence_backup = (
                    str(blocking_backups[-1]) if blocking_backups else None
                )
            conflict_owner = self._marker_owner_conflict(evidence_backup)
            if conflict_owner is not None:
                # 隔离证据属于仅大小写不同的另一账号：写入（含恢复入口）
                # 一律拒绝，备份与标记原样保留
                raise MemoryPersistenceError(
                    _case_conflict_message(conflict_owner)
                )
            if quarantined:
                # 隔离标记（其它实例 / DSN 下的重建实例写入）：与本实例
                # 当场隔离同等对待，配置了 PostgreSQL 也不能写回
                self._fresh_quarantine = True
                if marker_backup and not self._corrupt_backup:
                    self._corrupt_backup = marker_backup
            if tombstone_exists and not quarantined:
                if not live_backups:
                    # 账号数据已被删除（无更新于墓碑的隔离证据）：从空
                    # 快照开始，不能把失效快照写回
                    self.data = self._init_data()
                    return
                # 晚于墓碑的新隔离（标记写失败 / 崩溃窗口）：即便没有
                # 标记也按隔离拒绝重建
                self._fresh_quarantine = True
                if not self._corrupt_backup:
                    self._corrupt_backup = str(live_backups[-1])
            if (
                (blocking_backups or quarantined)
                and not self._allow_quarantined
                and not self._may_recreate
                and (self._fresh_quarantine or not _postgres_configured())
            ):
                # 主文件缺失 + 存在隔离备份 / 标记：损坏文件已被隔离。
                # 沿用内存快照写回会重建主文件、让隔离备份从此不可见
                # （恢复提示与隔离守卫全部失效）；只有显式以「恢复」打开
                # 的实例（user-import / /api/import）与写前复核刚补完备份
                # 的同一次写入（_may_recreate）允许重建。
                evidence_path = (
                    str(blocking_backups[-1])
                    if blocking_backups
                    else str(self._quarantine_marker_path())
                )
                raise MemoryPersistenceError(
                    "记忆主文件缺失且存在损坏隔离备份，已拒绝重建写入"
                    f"（备份：{evidence_path}）"
                )
            return
        except OSError as exc:
            # 文件被占用 / 瞬时 I/O 故障（Windows 共享冲突、杀软扫描）：
            # 文件可能完好，不能报「数据损坏」误导用户去手工恢复
            logger.warning("长期记忆暂时不可读：%s", exc)
            raise MemoryPersistenceError(
                f"长期记忆暂时不可读（文件可能被占用）"
                f"（{self.db_path}）：{exc}"
            ) from exc
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
        self._loaded_stat = _stat_fingerprint(reload_stat)
        self._dirty = False

    @contextlib.contextmanager
    def write_batch(self) -> Iterator["LongTermMemory"]:
        """把当前线程的一组写操作合并为一次「重载 + 原子写回」。

        一轮对话的记忆更新包含偏好 / 行程 / 会话元数据等多处落库：逐次写
        会各自重载并整份重写用户记忆文件（大账号一轮 3-4 次、秒级 I/O）。
        进入时取写锁 + 跨进程文件锁并重载一次；批内 ``_persist`` 只置脏
        标记，退出时统一写盘一次；批内异常整体回滚到磁盘状态。仅对当前
        线程生效（同线程可重入），其它线程的写请求照常各自加锁、等待本批
        结束后重载，语义与逐次写一致。
        """
        tid = threading.get_ident()
        if self._batch_owner == tid:
            self._batch_depth += 1
            try:
                yield self
            finally:
                self._batch_depth -= 1
            return
        with self._write_lock:
            try:
                with file_lock(self.db_path):
                    self._ensure_corrupt_backed_up()
                    self._reload_latest()
                    self._batch_owner = tid
                    self._batch_depth = 1
                    self._batch_dirty = False
                    try:
                        yield self
                    except BaseException:
                        # 批内失败：丢弃未落盘的修改（磁盘可能仍是旧内容；
                        # 回滚失败时保持脏标记，下一次重载继续丢弃）
                        self._batch_owner = None
                        self._batch_depth = 0
                        self._batch_dirty = False
                        try:
                            self._reload_latest()
                        except Exception:  # noqa: BLE001 - 回滚尽力而为
                            self._dirty = True
                            logger.warning(
                                "批量写失败后回滚内存快照失败", exc_info=True
                            )
                        raise
                    else:
                        self._batch_owner = None
                        self._batch_depth = 0
                        if self._batch_dirty:
                            self._batch_dirty = False
                            self._persist()
            except TimeoutError as exc:
                raise MemoryPersistenceError(
                    f"长期记忆写锁等待超时：{exc}"
                ) from exc
            finally:
                self._may_recreate = False

    def reload_if_changed(self) -> bool:
        """磁盘自上次加载 / 保存以来是否没有变化（True = 当前实例仍可用）。

        供长寿命实例（交互 REPL）逐命令调用：数据未变时保持当前快照，不必
        重建实例、整份重读解析；有未落盘修改（写失败 / 写中异常）、损坏
        待复核或文件已变（其它进程经原子替换写入）时返回 False，调用方
        走原有重建路径。只取按路径进程 RLock，不等待跨进程文件锁。
        """
        with self._write_lock:
            if self._dirty or self._loaded_stat is None:
                return False
            if (
                self._corrupt_pending
                or self._fresh_quarantine
                or self._may_recreate
            ):
                return False
            return self._disk_unchanged()

    def _disk_unchanged(self) -> bool:
        """磁盘文件是否仍是上次解析时的那一份（stat 指纹一致才可跳过重载）。

        写者一律以 ``mkstemp + fsync + os.replace`` 原子替换提交，换文件身份
        （ino / ctime 必变）；FAT 等 mtime 粒度粗的文件系统由 ino 变化兜底。
        文件系统不提供稳定 ino（返回 0）时保守返回 False，退化为现状。
        """
        fingerprint = self._loaded_stat
        if fingerprint is None:
            return False
        try:
            stat_info = os.stat(self.db_path)
        except OSError:
            # 文件被删除 / 正在替换：交给 _reload_latest 的墓碑 / 隔离分支
            return False
        return _stat_fingerprint(stat_info) == fingerprint

    def _backup_before_migration(self) -> None:
        """迁移会丢弃 / 重置坏数据前把原始文件另存一份（每个文件只留首份）。

        这些修正是破坏性的（非对象消息被丢弃、非数字计数器归零），直接把
        清理后的快照写回磁盘会让原始内容不可恢复；保留原始副本便于排查。
        备份含完整用户数据：文件按同目录 mkstemp + fsync + os.replace
        原子落盘（最终路径收紧到 0600，避免与协作目录共享时被同组 / 其他
        用户读取）。已有备份若不可解析（崩溃 / 磁盘满留下的半截文件），
        重建而不是「存在即跳过」，否则破坏性迁移会永久失去可用快照；
        原子替换也避免两个进程同时首次加载时交错出混合内容的备份。
        """
        backup = f"{self.db_path}.migrate-backup"
        if os.path.exists(backup):
            try:
                with open(backup, encoding="utf-8") as existing:
                    if isinstance(json.load(existing), dict):
                        return
            except (OSError, ValueError):
                pass
            logger.warning("迁移备份不完整，重新生成：%s", backup)
        tmp_path: str | None = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                prefix=f".{self.user_id}.migrate-backup.",
                suffix=".tmp",
                dir=os.path.dirname(backup) or ".",
            )
            with os.fdopen(fd, "wb") as out, open(self.db_path, "rb") as src:
                shutil.copyfileobj(src, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp_path, backup)
            tmp_path = None
        except OSError:
            logger.warning("无法备份迁移前的记忆文件：%s", self.db_path)
            return
        finally:
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    os.remove(tmp_path)
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
            items = data[list_key]
            # 健康数据快路径（C 级 map+isinstance）：全部是 dict 时保持原列表，
            # 每次读请求不分配一份 12 万条的影子列表；任何非 dict 仍走下方
            # 原有重建路径（日志 / 计数同步 / 迁移备份语义不变）
            if all(map(isinstance, items, repeat(dict))):
                continue
            cleaned = [item for item in items if isinstance(item, dict)]
            logger.warning(
                "丢弃 %s 中 %d 条非对象记录",
                list_key,
                len(items) - len(cleaned),
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
                if not has_preference_value(pref.get("value")):
                    # 空值 = 未设置：面板 / 导出口径都不可见，却会占用类型
                    # 上限名额（面板无行可删）。与 Web PUT / CLI 拒绝空值
                    # 同口径，读入时即丢弃（写入路径下次落盘生效）
                    logger.warning(
                        "丢弃空值的偏好记录（空值=未设置）：%s", pref.get("type")
                    )
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
                    # 紧凑分隔符：indent=2 会退回纯 Python 编码器（同一份
                    # 12 万条数据实测慢 5 倍以上），且文件大 22-25%——每次
                    # 读盘解析都要为这份空白付钱。全部读取方都是整文件
                    # json.loads，对格式无契约；旧版紧凑 / 缩进文件都能读
                    json.dump(
                        self.data,
                        handle,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
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
            try:
                Path(f"{self.db_path}.deleted").unlink(missing_ok=True)
            except OSError:
                pass
            # 成功写入意味着主文件已重建（恢复入口 / 写前补备份放行 /
            # 残留标记的自愈）：标记无条件清除（无标记时是一次 no-op）。
            # 条件式清除会漏掉「健康主文件 + 残留标记」的实例——它的
            # fresh/corrupt 标志早就不在了
            self._clear_quarantine_marker()
            # 实例级隔离 / 待复核状态与标记同步复位：主文件刚刚原子重建，
            # Web 缓存会话的隔离探测（get_fresh_quarantine_backup）不能
            # 拿已自愈的旧备份路径把健康账号反复驱逐 / close
            self._fresh_quarantine = False
            self._corrupt_pending = False
            self._corrupt_backup = None
            # 写后刷新指纹：刚写入的 self.data 就是磁盘内容，本实例后续的
            # 快照读取不必再整份重载；stat 失败按「未确认」处理
            try:
                self._loaded_stat = _stat_fingerprint(os.stat(self.db_path))
            except OSError:
                self._loaded_stat = None
            self._dirty = False
            return True
        except Exception as e:
            # 落盘失败：内存里的修改没有磁盘对应物，下一次重载必须丢弃它
            self._dirty = True
            self._loaded_stat = None
            logger.error(f"Failed to save long-term memory: {e}")
            return False

    def _persist(self) -> None:
        """写盘失败时抛错，避免上层把未落盘的修改当成功。"""
        if self._batch_owner == threading.get_ident():
            # 批量写内部：只登记待落盘，批结束时统一写一次
            self._dirty = True
            self._batch_dirty = True
            return
        if not self._save():
            raise MemoryPersistenceError("长期记忆写入失败")

    def ensure_persisted(self) -> None:
        """恢复入口用：备份恢复成功但没有任何用户数据写入（仅知识片段 /
        消息全部被会话删除剔除）时显式落盘重建主文件——主文件已被隔离
        移走时，不重建会让账号在「导入成功」后持续 503。主文件已存在或
        实例不是恢复入口（allow_quarantined）时不做任何事。"""
        if not self._allow_quarantined:
            return
        with self._write_lock:
            try:
                with file_lock(self.db_path):
                    # 与 _locked_mutation 同口径的写前损坏复核：_corrupt_pending
                    # 状态下补隔离备份，恢复入口不再因重载失败无法自愈
                    self._ensure_corrupt_backed_up()
                    # 存在性检查放在锁内：并发恢复（另一进程 / 请求）在
                    # 检查与写入之间重建了主文件时，不能用本实例的隔离态
                    # 空快照把它覆盖——刷新到磁盘内容即可
                    if os.path.exists(self.db_path):
                        self._reload_latest()
                        self._corrupt_pending = False
                        self._corrupt_backup = None
                        # 主文件已恢复：连带清掉残留隔离标记（导入重试
                        # 也能解除已被残留标记锁死的账号）
                        self._clear_quarantine_marker()
                        return
                    if Path(f"{self.db_path}.deleted").exists():
                        # 账号数据已被删除：从空快照落盘（写盘后标记清除），
                        # 绝不复活实例内的陈旧快照
                        self.data = self._init_data()
                    self._persist()
            except TimeoutError as exc:
                raise MemoryPersistenceError(
                    f"长期记忆写锁等待超时：{exc}"
                ) from exc
            finally:
                self._may_recreate = False

    @_locked_mutation
    def append_preference(self, pref_type: str, value: Any) -> Any:
        """在锁内重新加载并追加一个偏好值（列表语义），返回合并后的值。

        ``get_preference() + save_preference()`` 的读改写会覆盖并发写入的
        偏好（例如本轮 LLM 调用期间用户在记忆面板修改了同一类型）。
        """
        _reject_reserved_preference_type(pref_type)
        preferences = self.data["preferences"]
        existing = None
        for pref in preferences:
            if pref.get("type") == pref_type:
                existing = pref
                break
        current = existing.get("value") if existing is not None else None
        merged = merge_preference_value(current, value)
        # 必须校验合并后的值：多轮 append 累积同样能越过 10000 字符上限
        problem = preference_value_problem(merged)
        if problem:
            raise ValueError(problem)
        if existing is not None:
            existing["value"] = merged
        else:
            # 新建类型同样受上限约束（与 save_preference / PG 后端同口径）：
            # 只拦既有类型之外的追加，越限后 /api/export 会永久 413
            cap = _preference_cap()
            # 只拦「导入前未越限、本次新增会推过上限」：已越限的历史
            # 账号（上限上线前的存量）不再被永久拒绝，与导入侧按轴基线
            # 豁免一致
            if cap is not None and len(preferences) == cap:
                raise ValueError(
                    f"偏好条目已达上限（{cap}），请先删除不用的偏好"
                )
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
        _reject_reserved_preference_type(pref_type)
        problem = preference_value_problem(value)
        if problem:
            raise ValueError(problem)
        # 查找是否已存在该类型的偏好
        preferences = self.data["preferences"]
        found = False
        # 类型上限在写路径（写锁内）复核：调用方锁外先读再写的预检
        # 挡不住并发新增，越限后 /api/export 会因偏好上限永久 413
        cap = _preference_cap()

        for pref in preferences:
            if pref.get("type") == pref_type:
                pref["value"] = value
                found = True
                break

        # 如果不存在，添加新的偏好
        if not found:
            # 只拦「导入前未越限、本次新增会推过上限」：已越限的历史
            # 账号（上限上线前的存量）不再被永久拒绝，与导入侧按轴基线
            # 豁免一致
            if cap is not None and len(preferences) == cap:
                raise ValueError(
                    f"偏好条目已达上限（{cap}），请先删除不用的偏好"
                )
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
                current = pref["value"]
                merged = list(current) if isinstance(current, list) else (
                    [current] if current else []
                )
                # 追加品牌；合并后同样受单值上限约束——PG 后端经
                # save_preference 会拒绝超限值，JSON 落库会让该偏好在
                # 面板 / CLI 永远无法编辑（两后端口径必须一致）
                if brand not in merged:
                    merged.append(brand)
                    problem = preference_value_problem(merged)
                    if problem:
                        raise ValueError(problem)
                pref["value"] = merged
                found = True
                break

        # 如果不存在，创建新的
        if not found:
            # 「不存在则创建」同样受类型上限约束（与 save_preference 同口径）
            cap = _preference_cap()
            # 只拦「导入前未越限、本次新增会推过上限」：已越限的历史
            # 账号（上限上线前的存量）不再被永久拒绝，与导入侧按轴基线
            # 豁免一致
            if cap is not None and len(preferences) == cap:
                raise ValueError(
                    f"偏好条目已达上限（{cap}），请先删除不用的偏好"
                )
            problem = preference_value_problem([brand])
            if problem:
                raise ValueError(problem)
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
                current = pref["value"]
                merged = list(current) if isinstance(current, list) else (
                    [current] if current else []
                )
                # 追加航空公司；合并后同样受单值上限约束（与
                # add_hotel_brand / save_preference / PG 后端同口径）
                if airline not in merged:
                    merged.append(airline)
                    problem = preference_value_problem(merged)
                    if problem:
                        raise ValueError(problem)
                pref["value"] = merged
                found = True
                break

        # 如果不存在，创建新的
        if not found:
            # 「不存在则创建」同样受类型上限约束（与 save_preference 同口径）
            cap = _preference_cap()
            # 只拦「导入前未越限、本次新增会推过上限」：已越限的历史
            # 账号（上限上线前的存量）不再被永久拒绝，与导入侧按轴基线
            # 豁免一致
            if cap is not None and len(preferences) == cap:
                raise ValueError(
                    f"偏好条目已达上限（{cap}），请先删除不用的偏好"
                )
            problem = preference_value_problem([airline])
            if problem:
                raise ValueError(problem)
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

        from utils.user_import import _MAX_MESSAGES

        # 写入层硬上限（与导入预检同口径）：只拦「导入前未越限、本次会
        # 推过上限」的写入。已越限的历史账号（上限上线前的存量）不再被
        # 永久拒绝——恢复 / 继续使用都无法让它在导出能力上更糟，与
        # 导入侧按轴基线豁免一致
        if (
            len(self.data["chat_history"]) <= _MAX_MESSAGES
            and len(self.data["chat_history"]) + 1 > _MAX_MESSAGES
        ):
            raise MemoryPersistenceError(
                f"聊天消息总数已达上限 {_MAX_MESSAGES}，请先清理历史消息"
            )
        self.data["chat_history"].append(message)
        self.data["statistics"]["total_messages"] += 1
        self._persist()
        logger.debug(f"Added chat message to long-term memory: {role}")

    @_locked_mutation
    def add_chat_messages(
        self,
        messages: list[dict[str, Any]],
        dedupe: bool = False,
        *,
        added_by_session: dict[str, int] | None = None,
    ) -> int:
        """批量追加聊天消息（备份恢复）：一次重载 + 一次原子写回。

        逐条调用 ``add_chat_message`` 时每次都要重载并重写整个文件
        （O(n²)，几千条消息就会阻塞数分钟），这里合并为单次写锁。
        ``dedupe=True`` 在写锁内按（会话, 角色, 内容）多重集去重；提问已在
        库中的轮次跳过其备份里的全部回答（本地优先，避免尾部孤儿回答，
        见 :mod:`utils.chat_import`），并按时间戳把恢复消息并入全局历史。
        ``added_by_session`` 传入 dict 时清空并回填本次**实际写入**的按会话
        计数：导入端「写入期间会话被删除」的迟到回滚据此只扣真正新增的
        消息（按备份段扣分会高估）。返回实际写入的条数。
        """
        now = datetime.now().isoformat()
        if added_by_session is not None:
            added_by_session.clear()
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
        from utils.user_import import _MAX_MESSAGES

        # 写入层硬上限（与导入预检同口径）：只拦「导入前未越限、本批会
        # 推过上限」的写入；已越限的历史账号恢复自身备份（含新增行）
        # 不再被永久拒绝
        if (
            len(self.data["chat_history"]) <= _MAX_MESSAGES
            and len(self.data["chat_history"]) + len(to_add) > _MAX_MESSAGES
        ):
            raise ValueError(
                f"导入后聊天消息总数将超过上限 {_MAX_MESSAGES}"
            )
        if dedupe:
            _merge_messages_chronologically(self.data["chat_history"], to_add)
        else:
            self.data["chat_history"].extend(to_add)
        added = len(to_add)
        if added_by_session is not None:
            for message in to_add:
                sid = message.get("session_id")
                if sid is None:
                    continue
                key = str(sid)
                added_by_session[key] = added_by_session.get(key, 0) + 1
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
        user_timestamp: str | None = None,
        assistant_timestamp: str | None = None,
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
        # 与短期记忆共享同一消息时间戳：内容指纹（角色+内容+时间戳）
        # 才能在回填 / 重放与实时副本之间对齐
        user_ts = user_timestamp or now
        assistant_ts = assistant_timestamp or now
        history = self.data["chat_history"]
        from utils.user_import import _MAX_MESSAGES

        # 写入层硬上限（与导入预检同口径）：每轮两条，只拦「导入前未
        # 越限、本次会推过上限」的写入；已越限的历史账号不再被永久拒绝
        if (
            len(history) <= _MAX_MESSAGES
            and len(history) + 2 > _MAX_MESSAGES
        ):
            raise MemoryPersistenceError(
                f"聊天消息总数已达上限 {_MAX_MESSAGES}，请先清理历史消息"
            )
        start = len(history)
        total_before = int(self.data["statistics"].get("total_messages") or 0)
        history.append(
            {
                "role": "user",
                "content": user_content,
                "timestamp": user_ts,
                "session_id": session_id,
            }
        )
        history.append(
            {
                "role": "assistant",
                "content": assistant_content,
                "timestamp": assistant_ts,
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
            if limit and limit > 0:
                # 正常追加序下当前会话在末尾：反向收集满 limit 即停，避免
                # 每轮为取最近 50 条而把 12 万条全量过滤成临时列表。
                # add_chat_turn 失败回滚会原地缩短同一 list，读路径本就不
                # 持锁，越界索引跳过即可（弱一致语义与整体过滤一致）
                window: list[dict[str, Any]] = []
                index = len(messages) - 1
                while index >= 0 and len(window) < limit:
                    if index < len(messages):
                        message = messages[index]
                        if not _session_id_matches(message, exclude_session_id):
                            window.append(message)
                    index -= 1
                window.reverse()
                return window
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
            # 与 PG（session_id IS NOT NULL）同口径：整数 0 是合法会话
            if isinstance(message, dict)
            and message.get("session_id") is not None
        }
        return {"messages": len(messages), "sessions": sessions}

    def get_fresh_quarantine_backup(self) -> str | None:
        """本实例当场隔离（或写前复核补备份）的损坏文件路径。

        隔离事实同时落盘 ``<user>.json.quarantine`` 标记：载入到标记的
        新实例（含 DSN 配置下每次写前重建的实例）同样按当场隔离上报。
        仅「发现历史遗留 .corrupt-*、无标记」不算——那可能是其它后端
        （PostgreSQL 故障回退期间）留下的孤儿。
        """
        return self._corrupt_backup if self._fresh_quarantine else None

    def get_active_quarantine_backup(self) -> str | None:
        """落盘隔离标记指向的备份（跨实例隔离证据，直接读磁盘）。

        与 :meth:`get_fresh_quarantine_backup` 不同，早期加载的实例（Web
        缓存会话 / CLI 长会话）也能发现其它进程刚写入的隔离。标记损坏
        但确实存在时按隔离中 fail-closed；找不到可用备份时返回
        ``_QUARANTINE_NO_BACKUP_MARKER`` 哨兵——绝不能把标记文件本身当
        备份路径上报（用户会照提示导入标记）。
        """
        quarantined, backup = self._read_quarantine_marker()
        if not quarantined:
            return None
        return backup or _QUARANTINE_NO_BACKUP_MARKER

    def has_pending_corrupt_quarantine(self) -> bool:
        """检测到损坏但隔离改名 / 等锁失败（备份尚未完成）的状态。"""
        return bool(self._corrupt_pending)

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
            or any(
                _session_ref_matches(item, session_id)
                for item in pinned_sessions
            )
            or any(
                _session_ref_matches(item, session_id)
                for item in archived_sessions
            )
        )

        if removed:
            self.data["chat_history"] = remaining
            stats = self.data["statistics"]
            stats["total_messages"] = max(0, stats.get("total_messages", 0) - removed)

        if removed or metadata_changed or cleared_links:
            self.data.get("session_titles", {}).pop(session_id, None)
            # 置顶 / 归档按字符串口径清理（历史整数条目一并移除）：
            # 只清除字符串条目会让整数幽灵永久残留，同 ID 复用后新会话
            # 被旧归档隐藏 / 异常置顶，且任何入口都无法清理
            _drop_session_refs(pinned_sessions, session_id)
            _drop_session_refs(archived_sessions, session_id)
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
            # 置顶 / 归档按字符串口径清理（历史整数条目一并移除）
            _drop_session_refs(archived_sessions, session_id)
            titles = self.data.get("session_titles")
            if isinstance(titles, dict):
                titles.pop(session_id, None)
            pinned_sessions = self.data.get("pinned_sessions") or []
            _drop_session_refs(pinned_sessions, session_id)
            # 与整会话删除同口径：清空引用该会话的行程链接，否则面板里
            # 留下永远打不开的「查看对应对话」（单轮会话编辑重发时删空）
            for trip in self.data.get("trip_history") or []:
                if not isinstance(trip, dict):
                    continue
                value = trip.get("session_id")
                if value is not None and str(value) == session_id:
                    trip["session_id"] = None
        self._persist()
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
        self._persist()
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
        # 旧数据的会话 ID 可能是整数：按字符串口径判断成员并移除全部等价
        # 条目（历史整数 + 批量恢复追加的字符串副本），否则面板「取消置顶」
        # 对旧数据一次点不掉、点两次才生效
        if pinned:
            if not any(
                _session_ref_matches(item, session_id)
                for item in pinned_sessions
            ):
                pinned_sessions.append(session_id)
        else:
            _drop_session_refs(pinned_sessions, session_id)
        self._persist()
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
        # 与置顶同口径：字符串归一判断、一次移除全部等价条目
        if archived:
            if not any(
                _session_ref_matches(item, session_id)
                for item in archived_sessions
            ):
                archived_sessions.append(session_id)
        else:
            _drop_session_refs(archived_sessions, session_id)
        self._persist()
        logger.info("Set session archived: %s -> %s", session_id, archived)
        return True

    def get_archived_sessions(self) -> list[str]:
        """返回归档会话 ID。"""
        return list(self.data.get("archived_sessions") or [])

    @_locked_mutation
    def set_session_titles(
        self, titles: dict[str, str], *, applied_out: set[str] | None = None
    ) -> int:
        """批量设置会话标题（备份恢复）：只对存在的会话生效，返回生效数。

        ``applied_out`` 传入 set 时回填本次实际写入的会话 ID：导入端在
        「写入期间会话被删除」的迟到回滚里只扣减真正恢复过的条目
        （备份为准会误扣从未生效的会话）。
        """
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
            if applied_out is not None:
                applied_out.add(session_id)
        if applied:
            self._persist()
        return applied

    @_locked_mutation
    def set_sessions_pinned(
        self,
        session_ids: list[str],
        pinned: bool = True,
        *,
        applied_out: set[str] | None = None,
    ) -> int:
        """批量置顶 / 取消置顶（备份恢复）：返回实际生效数。

        ``applied_out`` 语义同 :meth:`set_session_titles`。
        """
        applied = 0
        pinned_sessions = self.data.setdefault("pinned_sessions", [])
        existing = self._session_id_set()
        for session_id in session_ids:
            if not session_id or session_id not in existing:
                continue
            has_ref = any(
                _session_ref_matches(item, session_id)
                for item in pinned_sessions
            )
            if pinned and not has_ref:
                pinned_sessions.append(session_id)
            elif not pinned and has_ref:
                _drop_session_refs(pinned_sessions, session_id)
            applied += 1
            if applied_out is not None:
                applied_out.add(session_id)
        if applied:
            self._persist()
        return applied

    @_locked_mutation
    def set_sessions_archived(
        self,
        session_ids: list[str],
        archived: bool = True,
        *,
        applied_out: set[str] | None = None,
    ) -> int:
        """批量归档 / 恢复（备份恢复）：返回实际生效数。

        ``applied_out`` 语义同 :meth:`set_session_titles`。
        """
        applied = 0
        archived_sessions = self.data.setdefault("archived_sessions", [])
        existing = self._session_id_set()
        for session_id in session_ids:
            if not session_id or session_id not in existing:
                continue
            has_ref = any(
                _session_ref_matches(item, session_id)
                for item in archived_sessions
            )
            if archived and not has_ref:
                archived_sessions.append(session_id)
            elif not archived and has_ref:
                _drop_session_refs(archived_sessions, session_id)
            applied += 1
            if applied_out is not None:
                applied_out.add(session_id)
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
        # 不可变 UID 标识：发放在 **trip_info 之后，调用方传入的同名字段
        # 不能覆盖；导出端据此生成稳定的日历 UID（编辑行程不换事件身份）
        trip_record["uid_token"] = secrets.token_hex(8)

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
        self,
        trip_info: dict[str, Any],
        *,
        max_total: int | None = None,
        created_out: list[bool] | None = None,
    ) -> str:
        """保存行程历史，返回 trip_id。

        同路线 + 同起止日期的行程视为重复记录（多次对话规划同一趟行程），
        更新既有记录而不是追加，避免行程 / 统计 / 日历重复。
        ``max_total`` 给出时在写锁内拒绝「新建」超过上限（去重更新不受
        影响）：调用方在锁外先读长度再写的检查挡不住并发创建，越限后
        /api/export 会因条数上限永久 413。``created_out`` 给出时把写锁内
        确定的「新增（True）/ 更新（False）」追加进去：调用方按锁外长度
        差判断会被窗口内其它写入污染，误报已添加 / 已更新。
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
            # 只拦「上限内、本次会推过上限」的新增：已越限的历史账号
            # （上限上线前的存量）不再被永久拒绝，与导入侧基线豁免一致
            if current == max_total:
                raise ValueError(f"行程数量已达上限（{max_total}），请先删除旧行程")
        trip_id, created = self._apply_trip_locked(trip_info)
        self._persist()
        if created_out is not None:
            # 持久化成功后才登记：落盘失败抛错时不误报结果
            created_out.append(created)
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

        from utils.trip_import import MAX_TOTAL_TRIPS

        planned_seen: set[tuple[str, str, str, str]] = set()
        planned_created = 0
        for row in trips:
            if not isinstance(row, dict):
                continue
            key = _identity(row)
            if key is not None:
                if key in index or key in planned_seen:
                    continue
                planned_seen.add(key)
            planned_created += 1
        # 写入层硬上限（与导入预检同口径）：只拦「导入前未越限、本批
        # 会推过上限」的写入；已越限的历史账号恢复自身备份（含更新）
        # 不再被永久拒绝
        if (
            len(history) <= MAX_TOTAL_TRIPS
            and len(history) + planned_created > MAX_TOTAL_TRIPS
        ):
            raise ValueError(
                f"导入后行程总数将超过上限 {MAX_TOTAL_TRIPS}"
            )

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
                # 既有行没有 token 时继承备份里的合法 token（恢复后日历
                # UID 与备份来源一致）；既有合法 token 一律不覆盖
                from utils.trip_export import normalized_uid_token

                incoming_token = normalized_uid_token(row.get("uid_token"))
                if incoming_token and not normalized_uid_token(
                    existing.get("uid_token")
                ):
                    existing["uid_token"] = incoming_token
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
            # 备份里的 uid_token 合法时沿用（恢复后日历 UID 与备份一致）；
            # 缺失 / 非法时补发。去重更新路径不刷新既有 token。
            from utils.trip_export import normalized_uid_token

            record["uid_token"] = (
                normalized_uid_token(record.get("uid_token"))
                or secrets.token_hex(8)
            )
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
        # 走 _persist：批量写（write_batch）内部只登记脏标记、批结束统一写盘
        self._persist()

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
        from utils.trip_import import dedupe_identity

        trips = self.data.get("trip_history", [])
        removed = 0
        skipped = 0
        for target in targets:
            trip_id = str(target.get("trip_id"))
            if not trip_id:
                continue
            want = dedupe_identity(target)
            matches = [
                index
                for index, trip in enumerate(trips)
                if str(trip.get("trip_id")) == trip_id
                and dedupe_identity(trip) == want
            ]
            if not matches:
                skipped += 1
                continue
            kept_id = str(target.get("kept_id") or "")
            if kept_id and not any(
                str(trip.get("trip_id")) == kept_id
                and dedupe_identity(trip) == want
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
        if removed:
            # 必须走 _persist：write_batch 内直接 _save 会把批内其它未提交
            # 修改一并写盘，批失败后的回滚重载反而固化「半批」快照
            self._persist()
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
            # 端点侧校验基于锁外快照：分端并发更新（Web / CLI 各持旧
            # 快照）可能各自通过校验、合并后落成「返程早于出发」的倒置
            # 行程。写锁内按合并结果复核（脏日期不参与，保持容错）
            if "start_date" in fields or "end_date" in fields:
                merged_start = _trip_date_value(merged.get("start_date"))
                merged_end = _trip_date_value(merged.get("end_date"))
                if (
                    merged_start is not None
                    and merged_end is not None
                    and merged_end < merged_start
                ):
                    raise ValueError("结束日期不能早于出发日期")
            same = self._find_same_trip(merged)
            if same is not None and same is not trip:
                raise ValueError("已存在相同行程（同路线 + 同起止日期）")

            from utils.trip_export import legacy_uid_inputs, normalized_uid_token

            before_uid_inputs = legacy_uid_inputs(trip)

            old_destination = trip.get("destination")
            old_start = trip.get("start_date")
            for key in ("origin", "destination", "start_date", "end_date", "purpose"):
                if key in fields:
                    trip[key] = fields[key]
            # 历史行程没有 uid_token：只有本次真正改动了 UID 输入（路线 /
            # 日期，且按旧公式的原文投影比较）才补发——这一刻旧公式本来
            # 也会换 UID，属一次性迁移；只改目的 / 会话的编辑不补发
            # （导出仍走旧公式，字节不变）
            if (
                not normalized_uid_token(trip.get("uid_token"))
                and legacy_uid_inputs(trip) != before_uid_inputs
            ):
                trip["uid_token"] = secrets.token_hex(8)
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
        """导出一致快照：磁盘有变化时在写锁内重载，再逐项读取。

        导出（备份 / 面板 / CLI）此前逐项调用 get_*，其它线程 / 进程在两次
        读取之间写入时，统计与列表会来自不同版本（备份里总数与消息条数
        对不上）。写锁覆盖进程内线程；跨进程写入经原子替换提交，这里用
        stat 指纹判断「磁盘是否已变」——未变时复用本实例刚解析的快照（读
        接口每请求新建实例，构造时已全量解析过一次，无条件重载等于整份
        解析两遍），变了仍走 _reload_latest 的完整复核路径。
        """
        with self._write_lock:
            if not self._disk_unchanged():
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

    def delete_all(self):
        """删除所有数据（包括文件与损坏隔离备份）。

        不经过 ``_locked_mutation``：普通写路径在「主文件缺失 + 存在隔离
        备份」时会拒绝重建（防止把隔离状态掩盖成正常写入），而删除正是
        要清掉这些文件——写前重载检查在这里没有意义。

        删除后必须重置内存快照：``_reload_latest`` 把「文件不存在」当作
        无需更新，不清空快照的话，同一实例的下一次写入会把已删除的数据
        整份写回。
        """
        with self._write_lock:
            try:
                with file_lock(self.db_path):
                    # 归属复核：陈旧实例（大小写变体）不能删掉另一账号的
                    # 记忆文件与隔离备份；损坏文件仍允许删除
                    self._check_owner_case_on_disk()
                    for candidate in self._corrupt_siblings():
                        owner = self._marker_owner_conflict(str(candidate))
                        if owner is not None:
                            raise MemoryPersistenceError(
                                _case_conflict_message(owner)
                            )
                    migrate_candidate = f"{self.db_path}.migrate-backup"
                    migrate_owner = self._owner_conflict_for_name(
                        self._actual_memory_filename(
                            os.path.basename(migrate_candidate)
                        ),
                        ".json.migrate-backup",
                    )
                    if migrate_owner is not None:
                        raise MemoryPersistenceError(
                            _case_conflict_message(migrate_owner)
                        )
                    marker_expected_name = (
                        f"{os.path.basename(self.db_path)}.quarantine"
                    )
                    marker_actual_name = self._actual_memory_filename(
                        marker_expected_name
                    )
                    if (
                        marker_actual_name != marker_expected_name
                        and marker_actual_name.casefold()
                        == marker_expected_name.casefold()
                    ):
                        raise MemoryPersistenceError(
                            _case_conflict_message(
                                marker_actual_name[
                                    : marker_actual_name.casefold().find(
                                        ".json.quarantine"
                                    )
                                ]
                            )
                        )
                    # 删除墓碑先行：进程在「删主文件」与「写墓碑」之间被杀 /
                    # 掉电时，其它存活实例仍能从墓碑得知账号已删除；墓碑
                    # 创建失败时主文件尚未删除，按删除失败上报（安全）
                    try:
                        Path(f"{self.db_path}.deleted").touch(
                            exist_ok=True
                        )
                    except OSError as exc:
                        raise MemoryPersistenceError(
                            "无法写入删除标记，未执行删除："
                            f"{self.db_path}.deleted"
                        ) from exc
                    try:
                        os.remove(self.db_path)
                        logger.warning(
                            f"Deleted long-term memory file: {self.db_path}"
                        )
                    except FileNotFoundError:
                        pass
                    except OSError as exc:
                        # Windows 共享冲突等：主文件仍在、墓碑已写入
                        # （_reload_latest 仅在文件缺失时看墓碑，不影响
                        # 正常读取）——按删除未完成上报
                        raise MemoryPersistenceError(
                            "删除长期记忆主文件失败（文件可能被其它进程占用）："
                            f"{self.db_path}"
                        ) from exc
                    # 隔离备份也是该账号的记忆数据：一并删除，否则删除后的
                    # 第一次写入会因「主文件缺失 + 存在 .corrupt-*」被拒绝。
                    # 删除失败不能静默：主文件已移除、备份仍在时，后续写入会
                    # 持续被隔离守卫拒绝，必须让调用方知道删除未完成
                    failed: list[str] = []
                    for backup in self._corrupt_siblings():
                        try:
                            os.remove(backup)
                        except FileNotFoundError:
                            continue
                        except OSError:
                            failed.append(str(backup))
                    migrate_backup = f"{self.db_path}.migrate-backup"
                    try:
                        os.remove(migrate_backup)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        failed.append(migrate_backup)
                    marker = self._quarantine_marker_path()
                    try:
                        marker.unlink(missing_ok=True)
                    except OSError:
                        failed.append(str(marker))
                    # 崩溃遗留的原子写临时文件（fsync 与 replace 之间被强杀）
                    # 内含完整记忆快照：delete_all 宣称删除所有数据，必须一并
                    # 清理。持锁期间匹配到的 .tmp 不可能是活跃写者的临时文件；
                    # 前缀用本实例 user_id 的精确大小写（_save 以同一拼写
                    # 命名），避免误删仅大小写不同的另一账号的临时文件
                    tmp_prefix = f".{self.user_id}."
                    try:
                        with os.scandir(
                            os.path.dirname(self.db_path) or "."
                        ) as entries:
                            for entry in entries:
                                if not (
                                    entry.name.startswith(tmp_prefix)
                                    and entry.name.endswith(".tmp")
                                ):
                                    continue
                                try:
                                    os.remove(entry.path)
                                except FileNotFoundError:
                                    continue
                                except OSError:
                                    failed.append(entry.path)
                    except OSError:
                        # 目录不可读时不阻断已完成的删除（主文件与备份
                        # 已移除）；残留临时文件可由后续清理发现
                        logger.warning(
                            "删除长期记忆时无法扫描临时文件目录"
                        )
                    # 无论备份是否删净都先重置内存快照与隔离标记：主文件
                    # 已移除，旧快照绝不能再被写回
                    self._corrupt_backup = None
                    self._may_recreate = False
                    self._corrupt_pending = False
                    self._fresh_quarantine = False
                    self.data = self._init_data()
                    # 磁盘已无对应文件：指纹失效、快照按「未落盘」对待，
                    # 长寿命实例的 reload 必须重建而不是复用
                    self._loaded_stat = None
                    self._dirty = True
                    if failed:
                        raise MemoryPersistenceError(
                            "删除长期记忆时无法移除备份文件："
                            + "、".join(failed)
                        )
            except TimeoutError as exc:
                raise MemoryPersistenceError(
                    f"长期记忆写锁等待超时：{exc}"
                ) from exc
