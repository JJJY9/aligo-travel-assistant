"""
PostgreSQL 长期记忆后端

启用方式：设置环境变量 ``DATABASE_URL``，例如
    postgresql://aligo:aligo@localhost:5432/aligo

- 表结构首次连接时自动创建（``aligo_`` 前缀，可重复执行）；
- 未配置或连接失败时，由 ``context.storage`` 的工厂回退到 JSON 文件实现；
- 偏好值以 JSONB 存储（字符串 / 列表等类型原样保留）。
"""
import contextlib
import functools
import logging
import re
import threading
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from typing import Any

from utils.errors import MemoryPersistenceError, PartialImportError
from utils.preference_values import merge_preference_value
from utils.timestamps import localize_naive, parse_iso_datetime


def _pg_timestamp(value: str) -> str:
    """naive 本地时间戳按本机时区解释后再写入 TIMESTAMPTZ。

    PostgreSQL 对无时区输入按会话 TimeZone（容器默认 UTC）解释；JSON 后端
    导出的 ``datetime.now().isoformat()`` 是本机时间，直接入库会整体偏移。
    """
    text = str(value)
    parsed = parse_iso_datetime(text)
    if parsed is None:
        return text
    if parsed.tzinfo is None:
        parsed = localize_naive(parsed)
    return parsed.isoformat()

logger = logging.getLogger(__name__)


def _trip_start_is_valid(value: Any) -> bool:
    """行程出发日期是否为可解析的 YYYY-MM-DD（与 JSON 后端同一统计口径）。"""
    try:
        datetime.strptime(str(value or ""), "%Y-%m-%d")
    except ValueError:
        return False
    return True

# 进程内「该 DSN 的表结构已就绪」标记：Web 每个读写接口 / 每个新会话都会
# 构造 PostgresLongTermMemory。若每次都跑整套 DDL，即使对象已存在，
# ALTER TABLE ... ADD COLUMN 仍取 aligo_trips 的 ACCESS EXCLUSIVE 锁、
# CREATE INDEX 仍取消息表 ShareLock，与导入 / 删除的长事务互相阻塞，把
# 所有新建存储的请求拖死。表结构只在进程首次连接某个 DSN 时创建一次；
# 版本升级新增表由重新部署（进程重启）触发。
_SCHEMA_READY: set[str] = set()
_SCHEMA_LOCK = threading.Lock()
# 多实例同时首次启动时串行化建表（跨进程）；滚动发布中旧实例不持此锁，
# 目录竞争错误按「对象已存在」容忍并重试一次
_SCHEMA_ADVISORY_LOCK = "aligo:schema_ddl"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS aligo_preferences (
    user_id TEXT NOT NULL,
    pref_type TEXT NOT NULL,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, pref_type)
);

CREATE TABLE IF NOT EXISTS aligo_trips (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    trip_id TEXT NOT NULL,
    origin TEXT,
    destination TEXT,
    start_date TEXT,
    end_date TEXT,
    purpose TEXT,
    session_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE aligo_trips ADD COLUMN IF NOT EXISTS session_id TEXT;
CREATE INDEX IF NOT EXISTS idx_aligo_trips_user ON aligo_trips (user_id, id);

CREATE TABLE IF NOT EXISTS aligo_trip_counters (
    user_id TEXT PRIMARY KEY,
    last_number BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS aligo_chat_messages (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    session_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_aligo_chat_user ON aligo_chat_messages (user_id, id);

CREATE TABLE IF NOT EXISTS aligo_user_stats (
    user_id TEXT PRIMARY KEY,
    total_queries INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS aligo_session_titles (
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    title TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, session_id)
);

CREATE TABLE IF NOT EXISTS aligo_pinned_sessions (
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    pinned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, session_id)
);

CREATE TABLE IF NOT EXISTS aligo_archived_sessions (
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    archived_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, session_id)
);
"""


def _session_meta_guard(func):
    """会话元数据写入装饰器：在用户级 advisory lock 内执行（与删除串行化）。

    会话级锁（``_advisory_lock``）与删除路径事务内的
    ``pg_advisory_xact_lock`` 使用同一 key、互相阻塞；选会话级是为了复用
    ``_advisory_lock`` 的重连重获逻辑（写入语句在重试时会换连接，事务级
    锁会随旧连接丢失）。
    """

    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        with self._advisory_lock(f"aligo:session_meta:{self.user_id}"):
            return func(self, *args, **kwargs)

    return wrapper


class PostgresLongTermMemory:
    """基于 PostgreSQL 的长期记忆（偏好 / 行程 / 对话历史 / 统计）。

    各方法在调用时直接查询数据库（无常驻内存快照），多会话 / 多进程写入天然一致。
    """

    # 直接查询数据库，无内存快照，无需在多轮对话前重建
    snapshot_based = False

    def __init__(self, user_id: str, dsn: str, statement_timeout_ms: int | None = None):
        import psycopg

        self.user_id = user_id
        self.dsn = dsn
        # 连接级 statement_timeout（毫秒）：为 None 时沿用服务端默认；自检
        # 探测等最坏场景需要给 DDL / 查询设上限，避免网络半通时无限等待
        self._statement_timeout_ms = statement_timeout_ms
        # psycopg 连接不是线程安全的：Web 的落库走 asyncio.to_thread，用户锁
        # 过期 / drain 超时后旧线程可能与新回合同时使用同一实例的连接。
        # 实例级 RLock 串行化所有数据库操作（含整个事务块），跨实例仍由
        # advisory lock 保证互斥
        self._conn_lock = threading.RLock()
        self._conn = self._connect()
        self._retry_errors = (psycopg.OperationalError, psycopg.InterfaceError)
        # 当前连接持有的会话级 advisory lock（锁名 -> 重入计数）。advisory lock
        # 随连接失效由服务端释放，重连后必须重新获取，否则读改写失去互斥。
        self._held_locks: dict[str, int] = {}
        self._ensure_schema()
        logger.info("PostgreSQL long-term memory initialized for user: %s", user_id)

    # ---------- 内部工具 ----------

    def _connect(self):
        import psycopg

        # connect_timeout 只覆盖建立连接：语句等表锁 / advisory lock 与网络
        # 半开时的读取还需要额外上限。lock_timeout 让等锁在 3 秒内报错
        # （由 _run_with_retry 包装为持久化失败，工厂随之冷却降级），
        # TCP keepalive 让半开连接在一分钟内被内核发现（系统默认约 2 小时，
        # 期间旧后端会继续持有 advisory lock / 行锁把重连也卡住）。
        # statement_timeout 只按调用方显式传入设置：大备份导入等合法长
        # 事务需要跑满，默认上限会误杀。
        options = ["-c lock_timeout=3000"]
        if self._statement_timeout_ms:
            options.append(
                f"-c statement_timeout={int(self._statement_timeout_ms)}"
            )
        kwargs: dict[str, Any] = {
            "autocommit": True,
            "connect_timeout": 3,
            "options": " ".join(options),
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 3,
        }
        return psycopg.connect(self.dsn, **kwargs)

    def _ensure_schema(self) -> None:
        """创建表 / 索引；同一进程内每个 DSN 只执行一次。

        表结构已存在时的 DDL 仍会取强锁（见模块顶部注释），不能放在每次
        构造 / 重连路径上；首次初始化在进程内用锁串行，跨进程用 advisory
        lock 串行，滚动发布中旧实例不持锁造成的目录竞争按「已存在」容忍。
        """
        key = self.dsn
        if key in _SCHEMA_READY:
            return
        with _SCHEMA_LOCK:
            if key in _SCHEMA_READY:
                return
            self._create_schema()
            _SCHEMA_READY.add(key)

    def _create_schema(self) -> None:
        import psycopg

        duplicate_errors: tuple[type[BaseException], ...] = tuple(
            error
            for error in (
                getattr(psycopg.errors, "DuplicateTable", None),
                getattr(psycopg.errors, "DuplicateObject", None),
                getattr(psycopg.errors, "UniqueViolation", None),
            )
            if isinstance(error, type)
        )
        with self._conn.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_lock(hashtext(%s)::bigint)",
                (_SCHEMA_ADVISORY_LOCK,),
            )
            try:
                try:
                    cursor.execute(_SCHEMA_SQL)
                except duplicate_errors:
                    # 旧实例（不持本锁）并发建表：批次在失败语句处中止，
                    # 重跑一次补齐后面的建表语句
                    logger.info("表结构已被并发创建，重试剩余 DDL")
                    cursor.execute(_SCHEMA_SQL)
                try:
                    # 历史库可能已有重复 trip_id，建索引失败只记录，不阻止后端启用
                    cursor.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS idx_aligo_trips_user_trip "
                        "ON aligo_trips (user_id, trip_id)"
                    )
                except Exception as exc:  # noqa: BLE001 - 仅提示，写入仍有 advisory lock 保护
                    logger.warning("无法创建 trip_id 唯一索引: %s", exc)
            finally:
                with contextlib.suppress(Exception):
                    cursor.execute(
                        "SELECT pg_advisory_unlock(hashtext(%s)::bigint)",
                        (_SCHEMA_ADVISORY_LOCK,),
                    )

    def _execute(
        self, sql: str, params: tuple | None = None, replay_safe: bool = True
    ) -> None:
        def run() -> None:
            with self._conn.cursor() as cursor:
                cursor.execute(sql, params)

        self._run_with_retry(run, replay_safe=replay_safe)

    def _execute_rowcount(self, sql: str, params: tuple | None = None) -> int:
        """执行写语句并返回受影响行数（重连重放语义与 _execute 一致）。"""

        def run() -> int:
            with self._conn.cursor() as cursor:
                cursor.execute(sql, params)
                return int(cursor.rowcount)

        return self._run_with_retry(run)

    def _fetchall(self, sql: str, params: tuple | None = None) -> list[tuple]:
        def run() -> list[tuple]:
            with self._conn.cursor() as cursor:
                cursor.execute(sql, params)
                return cursor.fetchall()

        return self._run_with_retry(run)

    def _fetchone(
        self, sql: str, params: tuple | None = None, replay_safe: bool = True
    ):
        def run():
            with self._conn.cursor() as cursor:
                cursor.execute(sql, params)
                return cursor.fetchone()

        return self._run_with_retry(run, replay_safe=replay_safe)

    def _reconnect(self) -> None:
        """连接失效（数据库重启 / 被服务端终止）时重建连接并确保表结构。"""
        with contextlib.suppress(Exception):
            self._conn.close()
        self._conn = self._connect()
        self._ensure_schema()
        # 旧连接上的 advisory lock 已被服务端释放：在新的连接上按重入计数
        # 重新获取，否则重试的读改写会在没有互斥保护的情况下执行（trip_N /
        # 偏好并发重复）；计数为 2 时只恢复一次会让内层解锁把锁彻底释放。
        # 直接使用游标而不再走 _run_with_retry，避免数据库不可用时无限递归重连。
        for lock_name, count in list(self._held_locks.items()):
            with self._conn.cursor() as cursor:
                for _ in range(max(1, count)):
                    cursor.execute(
                        "SELECT pg_advisory_lock(hashtext(%s)::bigint)", (lock_name,)
                    )
        logger.info("PostgreSQL 连接已重建（user=%s）", self.user_id)

    def _run_with_retry(self, operation, replay_safe: bool = True):
        """执行一次数据库操作；连接级错误时重连并重试一次。

        ``replay_safe=False`` 用于非幂等写入（如不带去重的批量插入）：
        提交后的连接错误无法区分「是否已提交」，重放会产生重复数据，
        直接按持久化失败上报，由调用方决定是否整体重试。

        重试后仍失败统一包装为 ``MemoryPersistenceError``：调用方按「存储
        暂时不可用」降级（Web 返回 503、对话运行时不计入 LLM 熔断器），
        而不是把裸 ``psycopg.OperationalError`` 当成未知 500 或 LLM 故障。
        确定性数据错误（DataError：文本含 NUL、JSONB 拒绝的 \u0000 等）
        重试不会成功，同样包装为持久化失败，而不是裸 500。
        """
        lock = getattr(self, "_conn_lock", None)
        if lock is None:
            # 部分构造的实例（测试替身）没有初始化锁：惰性补齐
            lock = threading.RLock()
            self._conn_lock = lock
        with lock:
            return self._run_with_retry_locked(operation, replay_safe)

    def _run_with_retry_locked(self, operation, replay_safe: bool) -> Any:
        import psycopg

        def _wrap(exc: Exception) -> None:
            if isinstance(exc, psycopg.Error):
                raise MemoryPersistenceError(
                    f"PostgreSQL 操作失败：{exc}"
                ) from exc
            raise exc

        try:
            return operation()
        except self._retry_errors as first_exc:
            if not replay_safe:
                raise MemoryPersistenceError(
                    f"PostgreSQL 操作失败：{first_exc}"
                ) from first_exc
            try:
                self._reconnect()
                return operation()
            except self._retry_errors as retry_exc:
                raise MemoryPersistenceError(
                    f"PostgreSQL 操作失败：{retry_exc}"
                ) from retry_exc
            except Exception as exc:  # noqa: BLE001 - 数据错误按持久化失败上报
                _wrap(exc)
        except Exception as exc:  # noqa: BLE001 - 数据错误按持久化失败上报
            _wrap(exc)
        raise AssertionError("unreachable")

    @staticmethod
    def _json(value: Any) -> Any:
        from psycopg.types.json import Json

        return Json(value)

    def _abort_if_reconnected(self, start_conn: object) -> None:
        """锁内读改写跨连接重建时中止本次写入。

        旧连接失效后 _reconnect 会先释放、再重新获取会话级 advisory lock：
        这段空档里其它实例可以完成自己的读改写。本调用按重连前读到的快照
        继续写，会基于过期数据（覆盖别人的更新 / 造出重复行），与
        save_trip_histories 的批量防护同一口径，直接按持久化失败上报让
        调用方重试。
        """
        if getattr(self, "_conn", None) is not start_conn:
            raise MemoryPersistenceError(
                "数据库连接已重建，本轮写入可能基于过期数据，请重试"
            )

    @contextlib.contextmanager
    def _advisory_lock(self, lock_name: str):
        """会话级 advisory lock：同一连接内可重入，连接重建后自动重新获取。"""
        self._execute("SELECT pg_advisory_lock(hashtext(%s)::bigint)", (lock_name,))
        self._held_locks[lock_name] = self._held_locks.get(lock_name, 0) + 1
        try:
            yield
        finally:
            count = self._held_locks.get(lock_name, 0) - 1
            if count > 0:
                self._held_locks[lock_name] = count
            else:
                self._held_locks.pop(lock_name, None)
            with contextlib.suppress(Exception):
                self._execute(
                    "SELECT pg_advisory_unlock(hashtext(%s)::bigint)", (lock_name,)
                )

    @contextlib.contextmanager
    def _preference_lock(self, pref_type: str):
        """同一用户同一偏好类型的读改写跨会话串行（会话级 advisory lock，可重入）。"""
        with self._advisory_lock(f"aligo-pref-{self.user_id}-{pref_type}"):
            yield

    # ---------- 偏好 ----------

    def save_preference(self, pref_type: str, value: Any) -> None:
        with self._preference_lock(pref_type):
            self._execute(
                """
                INSERT INTO aligo_preferences (user_id, pref_type, value)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id, pref_type)
                DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                """,
                (self.user_id, pref_type, self._json(value)),
            )

    def get_preference(self, pref_type: str | None = None) -> Any:
        if pref_type is None:
            rows = self._fetchall(
                "SELECT pref_type, value FROM aligo_preferences WHERE user_id = %s",
                (self.user_id,),
            )
            return {row[0]: row[1] for row in rows}
        row = self._fetchone(
            "SELECT value FROM aligo_preferences WHERE user_id = %s AND pref_type = %s",
            (self.user_id, pref_type),
        )
        return row[0] if row else None

    def delete_preference(self, pref_type: str) -> bool:
        """删除指定类型的偏好，返回是否确实删除。"""

        def run() -> int:
            with self._conn.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM aligo_preferences WHERE user_id = %s AND pref_type = %s",
                    (self.user_id, pref_type),
                )
                return cursor.rowcount or 0

        with self._preference_lock(pref_type):
            # 与 delete_chat_session 同口径：提交后的断连不能重放成
            # 「0 行删除 → 偏好仍存在」的假象
            removed = self._run_with_retry(run, replay_safe=False)
        if removed:
            logger.info("Deleted preference from PostgreSQL: %s", pref_type)
        return bool(removed)

    def append_preference(self, pref_type: str, value: Any) -> Any:
        """原子地追加一个偏好值（列表语义），返回合并后的值。

        用会话级 advisory lock 包住「读 + 写」，避免与本轮 LLM 调用期间
        用户在记忆面板修改同一类型偏好互相覆盖。
        """
        with self._preference_lock(pref_type):
            start_conn = getattr(self, "_conn", None)
            current = self.get_preference(pref_type)
            # 读期间连接重建：旧 advisory lock 已释放，空档里其它实例可能
            # 已追加同类型偏好，按旧值合并会覆盖别人的更新
            self._abort_if_reconnected(start_conn)
            merged = merge_preference_value(current, value)
            self.save_preference(pref_type, merged)
            return merged

    def add_hotel_brand(self, brand: str) -> None:
        current = self.get_preference("hotel_brands")
        values = list(current) if isinstance(current, list) else ([current] if current else [])
        if brand not in values:
            values.append(brand)
        self.save_preference("hotel_brands", values)

    def add_airline(self, airline: str) -> None:
        current = self.get_preference("airlines")
        values = list(current) if isinstance(current, list) else ([current] if current else [])
        if airline not in values:
            values.append(airline)
        self.save_preference("airlines", values)

    # ---------- 对话历史 ----------

    def add_chat_message(
        self,
        role: str,
        content: str,
        session_id: str | None = None,
        timestamp: str | None = None,
    ) -> None:
        if timestamp:
            # 备份恢复：保留原始时间戳（naive 按本机时区解释）。
            # INSERT 非幂等：提交后响应丢失不能自动重放（会产生重复消息）
            self._execute(
                "INSERT INTO aligo_chat_messages "
                "(user_id, role, content, session_id, created_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (self.user_id, role, content, session_id, _pg_timestamp(timestamp)),
                replay_safe=False,
            )
            return
        self._execute(
            "INSERT INTO aligo_chat_messages (user_id, role, content, session_id) VALUES (%s, %s, %s, %s)",
            (self.user_id, role, content, session_id),
            replay_safe=False,
        )

    def add_chat_turn(
        self,
        user_content: str,
        assistant_content: str,
        session_id: str | None = None,
        should_commit: Callable[[], bool] | None = None,
    ) -> bool:
        """同一事务写入一轮完整问答（用户 + 助手两条消息）。

        分开两次 INSERT 时，两次之间被取消 / 第二次失败会留下没有回答的
        孤儿提问；单事务保证两条一起提交或一起回滚。连接错误后不重放
        （无法区分事务是否已提交，重放会产生重复问答）。

        ``should_commit`` 在事务内、写入前调用：会话刚被删除（墓碑有效）
        时返回 False，整轮不写入——删除与在途落库并发时不会把已删除的
        会话写回。

        Returns:
            是否真的写入
        """

        def run() -> bool:
            with self._conn.transaction(), self._conn.cursor() as cursor:
                # 与 delete_chat_session / delete_last_turn 的会话元数据锁同一个
                # 键：「墓碑判定 + 插入」与并发删除串行化，避免守卫通过后删除
                # 先提交、写入再把已删除会话的两条消息写回
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"aligo:session_meta:{self.user_id}",),
                )
                if should_commit is not None and not should_commit():
                    return False
                cursor.executemany(
                    "INSERT INTO aligo_chat_messages "
                    "(user_id, role, content, session_id) "
                    "VALUES (%s, %s, %s, %s)",
                    [
                        (self.user_id, "user", user_content, session_id),
                        (self.user_id, "assistant", assistant_content, session_id),
                    ],
                )
            return True

        return bool(self._run_with_retry(run, replay_safe=False))

    def add_chat_messages(
        self, messages: list[dict[str, Any]], dedupe: bool = False
    ) -> int:
        """批量追加聊天消息（备份恢复）：单事务 executemany。

        ``dedupe=True`` 在同一事务内先读出（会话, 角色, 内容）多重集再过滤，
        重复导入同一备份不会追加重复消息。返回实际写入的条数。
        """
        rows: list[tuple] = []
        fallback = datetime.now().isoformat()
        for message in messages:
            role = str(message.get("role") or "")
            if role not in {"user", "assistant"}:
                continue
            timestamp = message.get("timestamp") or fallback
            rows.append(
                (
                    role,
                    str(message.get("content") or ""),
                    message.get("session_id"),
                    _pg_timestamp(timestamp),
                )
            )
        if not rows:
            return 0

        def run() -> int:
            with self._conn.transaction(), self._conn.cursor() as cursor:
                kept = rows
                if dedupe:
                    # READ COMMITTED 下两个并发导入都能在 SELECT 里看不到对方
                    # 尚未提交的行：先拿用户级事务锁串行化「查重 + 插入」，
                    # 否则并发恢复同一备份会把消息插成两倍
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))",
                        (f"aligo:chat_messages:{self.user_id}",),
                    )
                    cursor.execute(
                        "SELECT session_id, role, content FROM aligo_chat_messages "
                        "WHERE user_id = %s",
                        (self.user_id,),
                    )
                    existing = Counter(
                        (session_id, role, content)
                        for session_id, role, content in cursor.fetchall()
                    )
                    filtered = []
                    for role, content, session_id, timestamp in rows:
                        key = (session_id, role, content)
                        if existing[key] > 0:
                            existing[key] -= 1
                            continue
                        filtered.append((role, content, session_id, timestamp))
                    kept = filtered
                if not kept:
                    return 0
                cursor.executemany(
                    "INSERT INTO aligo_chat_messages "
                    "(user_id, role, content, session_id, created_at) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    [(self.user_id, *row) for row in kept],
                )
                return len(kept)

        # dedupe=True 的重放会在 SELECT 里看到已提交行并全部跳过，天然幂等；
        # 不带去重的批量插入重放会产生重复消息，禁止自动重放
        return int(self._run_with_retry(run, replay_safe=dedupe) or 0)

    def get_chat_history(
        self,
        limit: int | None = None,
        session_id: str | None = None,
        exclude_session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        where = "user_id = %s"
        params: list[Any] = [self.user_id]
        if session_id:
            where += " AND session_id = %s"
            params.append(session_id)
        elif exclude_session_id:
            # IS DISTINCT FROM 保留 session_id 为 NULL 的历史消息（旧版本写入）
            where += " AND session_id IS DISTINCT FROM %s"
            params.append(exclude_session_id)

        if limit:
            rows = self._fetchall(
                "SELECT role, content, session_id, created_at FROM aligo_chat_messages "
                f"WHERE {where} ORDER BY id DESC LIMIT %s",
                (*params, limit),
            )
            rows = list(reversed(rows))
        else:
            rows = self._fetchall(
                "SELECT role, content, session_id, created_at FROM aligo_chat_messages "
                f"WHERE {where} ORDER BY id",
                tuple(params),
            )

        return [
            {
                "role": role,
                "content": content,
                "session_id": session_id_value,
                "timestamp": created_at.isoformat(),
            }
            for role, content, session_id_value, created_at in rows
        ]

    def get_chat_summary(self) -> dict[str, Any]:
        """轻量会话概览：消息总数与有消息的会话 ID 集合（不读取消息正文）。

        CLI stats 此前用 ``get_chat_history()`` 整表取回全部正文，只为数
        条数与会话集合：数十万条历史时峰值内存超过 1GB。
        """
        row = self._fetchone(
            "SELECT COUNT(*) FROM aligo_chat_messages WHERE user_id = %s",
            (self.user_id,),
        )
        rows = self._fetchall(
            "SELECT DISTINCT session_id FROM aligo_chat_messages "
            "WHERE user_id = %s AND session_id IS NOT NULL",
            (self.user_id,),
        )
        return {
            "messages": int(row[0]) if row else 0,
            "sessions": {str(session_id) for (session_id,) in rows if session_id},
        }

    def delete_chat_session(self, session_id: str) -> int:
        """删除某个会话的全部聊天记录与元数据（单事务），返回删除条数。"""

        def run() -> int:
            with self._conn.transaction(), self._conn.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"aligo:session_meta:{self.user_id}",),
                )
                cursor.execute(
                    "DELETE FROM aligo_chat_messages "
                    "WHERE user_id = %s AND session_id = %s",
                    (self.user_id, session_id),
                )
                removed = cursor.rowcount or 0
                cursor.execute(
                    "DELETE FROM aligo_session_titles "
                    "WHERE user_id = %s AND session_id = %s",
                    (self.user_id, session_id),
                )
                cursor.execute(
                    "DELETE FROM aligo_pinned_sessions "
                    "WHERE user_id = %s AND session_id = %s",
                    (self.user_id, session_id),
                )
                cursor.execute(
                    "DELETE FROM aligo_archived_sessions "
                    "WHERE user_id = %s AND session_id = %s",
                    (self.user_id, session_id),
                )
                return removed

        # 非重放：DELETE 已在服务端提交、但连接在 COMMIT 响应阶段断开时，
        # 重连重放会匹配 0 行并把真实删除数报成 0（JSON 后端没有这条路径，
        # 总是返回真实条数；Web DELETE 与导入墓碑回滚都用该计数）。与
        # delete_last_turn 同口径：连接错误按持久化失败上报、由调用方重试
        removed = self._run_with_retry(run, replay_safe=False)
        if removed:
            logger.info(
                "Deleted chat session from PostgreSQL: %s (%s messages)", session_id, removed
            )
        return removed

    def delete_last_turn(self, session_id: str, expect: str | None = None) -> int:
        """删除某个会话最后一轮对话（助手回答 + 对应用户提问），返回删除条数。

        ``expect`` 不为 None 时先校验最后一轮的用户提问内容，不一致直接返回 0，
        避免删除上一轮已保存的对话。删除语句带「没有更新消息」条件，避免在
        多进程场景下与并发写入交错后删错回合。
        """
        rows = self._fetchall(
            "SELECT id, role, content FROM aligo_chat_messages "
            "WHERE user_id = %s AND session_id = %s ORDER BY id DESC LIMIT 2",
            (self.user_id, session_id),
        )
        if not rows:
            return 0
        ids = [rows[0][0]]
        last_user_content: str | None = None
        if len(rows) >= 2 and rows[0][1] == "assistant" and rows[1][1] == "user":
            ids.append(rows[1][0])
            last_user_content = str(rows[1][2])
        elif rows[0][1] == "user":
            last_user_content = str(rows[0][2])
        if expect is not None:
            if last_user_content is None or last_user_content.strip() != expect.strip():
                logger.info(
                    "Skip deleting last turn of session %s: content mismatch", session_id
                )
                return 0
        def _delete_rows() -> int:
            with self._conn.transaction(), self._conn.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"aligo:session_meta:{self.user_id}",),
                )
                cursor.execute(
                    "DELETE FROM aligo_chat_messages "
                    "WHERE user_id = %s AND id = ANY(%s) "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM aligo_chat_messages "
                    "  WHERE user_id = %s AND session_id = %s AND id > %s"
                    ")",
                    (self.user_id, ids, self.user_id, session_id, rows[0][0]),
                )
                removed = cursor.rowcount or 0
                # 会话消息已被删空时同步清理标题 / 置顶 / 归档标记。清理
                # 无条件执行（语句自带 NOT EXISTS，会话仍有消息时是 no-op）：
                # 即使 DELETE 影响 0 行（并发已删），标记也不该残留
                for table in (
                    "aligo_session_titles",
                    "aligo_pinned_sessions",
                    "aligo_archived_sessions",
                ):
                    cursor.execute(
                        f"DELETE FROM {table} "
                        "WHERE user_id = %s AND session_id = %s "
                        "AND NOT EXISTS ("
                        "  SELECT 1 FROM aligo_chat_messages "
                        "  WHERE user_id = %s AND session_id = %s"
                        ")",
                        (self.user_id, session_id, self.user_id, session_id),
                    )
                return removed

        # DELETE 匹配的是「当前最后一轮」：提交后响应丢失时重放可能删掉
        # 期间新到的同文本回合，不能自动重放（按持久化失败上报）
        removed = self._run_with_retry(_delete_rows, replay_safe=False)
        logger.info("Deleted last turn of session %s (%s messages)", session_id, removed)
        return removed

    def save_trip_histories(self, trips: list[dict[str, Any]]) -> tuple[int, int]:
        """批量保存行程（备份恢复 / 导入）：返回 ``(新增数, 更新数)``。

        逐条调用单条保存时，每条都要按路线查候选、并全量扫描 ``trip_id``
        生成下一个编号（O(行数 × 历史条数)），整批还持着用户 advisory lock，
        几千条导入会把并发的行程写入阻塞数秒到数十秒。这里在锁内一次读出
        历史行程构建「去重键 → 行 id」索引与编号水位，再逐条 UPDATE / INSERT；
        语义与单条保存一致（未补零日期归一、未提供的目的 / 会话保留原值、
        编号不复用）。计数直接来自本批操作，不再用锁外总数差推算。
        """
        count = len(trips)
        if not count:
            return 0, 0
        from utils.trip_import import normalize_identity_date, normalize_identity_text

        def _key(
            destination: Any, origin: Any, start_date: Any, end_date: Any
        ) -> tuple[str, str, str, str] | None:
            if not destination or not start_date:
                return None
            start_key = normalize_identity_date(start_date)
            end_key = normalize_identity_date(end_date) or start_key
            return (
                normalize_identity_text(destination),
                normalize_identity_text(origin),
                start_key,
                end_key,
            )

        with self._advisory_lock(f"aligo-trip-{self.user_id}"):
            # 连接中途重建会换掉同一把 advisory lock 的持有者：快照（索引 /
            # 编号水位）来自旧连接，期间其它实例可能已写入，继续用过期水位
            # 会生成重复 trip_id。检测到重建就中止本批（已写入部分按去重更新）。
            start_conn = self._conn

            def _abort_if_reconnected() -> None:
                if self._conn is not start_conn:
                    raise MemoryPersistenceError(
                        "PostgreSQL 连接在批量保存中被重建，请重试导入"
                    )

            rows = self._fetchall(
                "SELECT id, trip_id, origin, destination, start_date, end_date "
                "FROM aligo_trips WHERE user_id = %s ORDER BY id",
                (self.user_id,),
            )
            index: dict[tuple[str, str, str, str], int] = {}
            max_number = 0
            for row_id, trip_id, origin, destination, start, end in rows:
                match = re.fullmatch(r"trip_(\d+)", str(trip_id or ""))
                if match:
                    max_number = max(max_number, int(match.group(1)))
                key = _key(destination, origin, start, end)
                if key is not None:
                    index.setdefault(key, int(row_id))
            counter = self._fetchone(
                "SELECT last_number FROM aligo_trip_counters WHERE user_id = %s",
                (self.user_id,),
            )
            _abort_if_reconnected()
            if counter is not None:
                max_number = max(max_number, int(counter[0] or 0))

            created = 0
            updated = 0
            try:
                for trip in trips:
                    destination = trip.get("destination")
                    start_date = str(trip.get("start_date") or "")
                    origin = trip.get("origin")
                    end_date = trip.get("end_date") or start_date
                    key = _key(destination, origin, start_date, end_date)
                    existing_id = index.get(key) if key is not None else None
                    if existing_id is not None:
                        # 与 JSON 批量保存同口径：purpose 未提供（None）保留原值；
                        # session_id 键存在即覆盖（含显式 None——备份恢复时该
                        # 会话已被删除，需要清除关联，COALESCE 会把 None 当
                        # 未提供而保留旧值，留下指向已删除会话的死链）
                        if "session_id" in trip:
                            self._execute(
                                "UPDATE aligo_trips SET "
                                "purpose = COALESCE(%s, purpose), "
                                "session_id = %s "
                                "WHERE id = %s",
                                (
                                    trip.get("purpose"),
                                    trip.get("session_id"),
                                    existing_id,
                                ),
                            )
                        else:
                            self._execute(
                                "UPDATE aligo_trips SET "
                                "purpose = COALESCE(%s, purpose) "
                                "WHERE id = %s",
                                (trip.get("purpose"), existing_id),
                            )
                        _abort_if_reconnected()
                        updated += 1
                        continue
                    max_number += 1
                    inserted = self._fetchone(
                        "INSERT INTO aligo_trips "
                        "(user_id, trip_id, origin, destination, start_date, end_date, purpose, session_id) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                        (
                            self.user_id,
                            f"trip_{max_number}",
                            trip.get("origin"),
                            destination,
                            trip.get("start_date"),
                            trip.get("end_date"),
                            trip.get("purpose") or "旅游",
                            trip.get("session_id"),
                        ),
                        # 非幂等写入：连接在提交后丢失时无法区分是否已写入，重放会
                        # 产生重复行程 / 重复 trip_id，直接按持久化失败上报
                        replay_safe=False,
                    )
                    _abort_if_reconnected()
                    if key is not None and inserted is not None:
                        index[key] = int(inserted[0])
                    created += 1
                if created:
                    _abort_if_reconnected()
                    self._execute(
                        "INSERT INTO aligo_trip_counters (user_id, last_number) "
                        "VALUES (%s, %s) "
                        "ON CONFLICT (user_id) DO UPDATE SET last_number = GREATEST("
                        "aligo_trip_counters.last_number, EXCLUDED.last_number)",
                        (self.user_id, max_number),
                    )
            except Exception as exc:  # noqa: BLE001 - 已提交部分要如实上报
                raise PartialImportError(
                    f"批量保存在第 {created + updated} 条后失败：{exc}",
                    created,
                    updated,
                ) from exc
        return created, updated

    def set_session_titles(self, titles: dict[str, str]) -> int:
        """批量设置会话标题（备份恢复）：只对存在的会话生效，返回生效数。"""
        applied = 0
        for session_id, title in titles.items():
            cleaned = str(title).strip()
            if cleaned and self.set_session_title(session_id, cleaned):
                applied += 1
        return applied

    def set_sessions_pinned(self, session_ids: list[str], pinned: bool = True) -> int:
        """批量置顶 / 取消置顶（备份恢复）：返回实际生效数。"""
        applied = 0
        for session_id in session_ids:
            if session_id and self.set_session_pinned(session_id, pinned):
                applied += 1
        return applied

    def set_sessions_archived(
        self, session_ids: list[str], archived: bool = True
    ) -> int:
        """批量归档 / 恢复（备份恢复）：返回实际生效数。"""
        applied = 0
        for session_id in session_ids:
            if session_id and self.set_session_archived(session_id, archived):
                applied += 1
        return applied

    def _session_exists(self, session_id: str) -> bool:
        row = self._fetchone(
            "SELECT 1 FROM aligo_chat_messages "
            "WHERE user_id = %s AND session_id = %s LIMIT 1",
            (self.user_id, session_id),
        )
        return row is not None

    @_session_meta_guard
    def set_session_title(self, session_id: str, title: str) -> bool:
        """设置会话自定义标题；空标题表示恢复自动标题。返回会话是否存在。"""
        cleaned = title.strip()
        if not cleaned:
            if not self._session_exists(session_id):
                return False
            self._execute(
                "DELETE FROM aligo_session_titles WHERE user_id = %s AND session_id = %s",
                (self.user_id, session_id),
            )
            return True
        row = self._fetchone(
            """
            INSERT INTO aligo_session_titles (user_id, session_id, title)
            SELECT %s, %s, %s
            WHERE EXISTS (
                SELECT 1 FROM aligo_chat_messages
                WHERE user_id = %s AND session_id = %s
            )
            ON CONFLICT (user_id, session_id)
            DO UPDATE SET title = EXCLUDED.title, updated_at = now()
            RETURNING 1
            """,
            (self.user_id, session_id, cleaned[:60], self.user_id, session_id),
        )
        return row is not None

    def get_session_titles(self) -> dict[str, str]:
        """返回全部会话自定义标题。"""
        rows = self._fetchall(
            "SELECT session_id, title FROM aligo_session_titles WHERE user_id = %s",
            (self.user_id,),
        )
        return {session_id: title for session_id, title in rows}

    @_session_meta_guard
    def set_session_pinned(self, session_id: str, pinned: bool) -> bool:
        """置顶 / 取消置顶会话。返回会话是否存在。"""
        if pinned:
            row = self._fetchone(
                """
                INSERT INTO aligo_pinned_sessions (user_id, session_id)
                SELECT %s, %s
                WHERE EXISTS (
                    SELECT 1 FROM aligo_chat_messages
                    WHERE user_id = %s AND session_id = %s
                )
                ON CONFLICT DO NOTHING
                RETURNING 1
                """,
                (self.user_id, session_id, self.user_id, session_id),
            )
            if row is not None:
                return True
            # 已置顶时 ON CONFLICT DO NOTHING 不返回行：会话存在即视为成功（幂等）
            return self._session_exists(session_id)
        if not self._session_exists(session_id):
            return False
        self._execute(
            "DELETE FROM aligo_pinned_sessions WHERE user_id = %s AND session_id = %s",
            (self.user_id, session_id),
        )
        return True

    def get_pinned_sessions(self) -> list[str]:
        """返回置顶会话 ID（按置顶时间先后）。"""
        rows = self._fetchall(
            "SELECT session_id FROM aligo_pinned_sessions WHERE user_id = %s "
            "ORDER BY pinned_at, session_id",
            (self.user_id,),
        )
        return [row[0] for row in rows]

    @_session_meta_guard
    def set_session_archived(self, session_id: str, archived: bool) -> bool:
        """归档 / 恢复会话；归档只影响会话列表展示，不删除消息。返回会话是否存在。"""
        if archived:
            row = self._fetchone(
                """
                INSERT INTO aligo_archived_sessions (user_id, session_id)
                SELECT %s, %s
                WHERE EXISTS (
                    SELECT 1 FROM aligo_chat_messages
                    WHERE user_id = %s AND session_id = %s
                )
                ON CONFLICT DO NOTHING
                RETURNING 1
                """,
                (self.user_id, session_id, self.user_id, session_id),
            )
            if row is not None:
                return True
            # 已归档时 ON CONFLICT DO NOTHING 不返回行：会话存在即视为成功（幂等）
            return self._session_exists(session_id)
        if not self._session_exists(session_id):
            return False
        self._execute(
            "DELETE FROM aligo_archived_sessions WHERE user_id = %s AND session_id = %s",
            (self.user_id, session_id),
        )
        return True

    def get_archived_sessions(self) -> list[str]:
        """返回归档会话 ID（按归档时间倒序）。"""
        rows = self._fetchall(
            "SELECT session_id FROM aligo_archived_sessions WHERE user_id = %s "
            "ORDER BY archived_at DESC, session_id",
            (self.user_id,),
        )
        return [row[0] for row in rows]

    # ---------- 历史行程 ----------

    def save_trip_history(self, trip_info: dict[str, Any]) -> str:
        """保存行程历史，返回 trip_id；同路线 + 同起止日期视为重复记录，更新而不是新增。"""
        purpose = trip_info.get("purpose") or "旅游"
        destination = trip_info.get("destination")
        # 会话级 advisory lock：多线程 / 多进程同时写入同一用户时串行生成 trip_N
        with self._advisory_lock(f"aligo-trip-{self.user_id}"):
            return self._save_trip_history_locked(trip_info, purpose, destination)

    def _save_trip_history_locked(
        self,
        trip_info: dict[str, Any],
        purpose: str,
        destination: Any,
    ) -> str:
        start_conn = getattr(self, "_conn", None)
        start_date = str(trip_info.get("start_date") or "")
        origin = str(trip_info.get("origin") or "")
        end_date = str(trip_info.get("end_date") or start_date)
        if destination and start_date:
            from utils.trip_import import normalize_identity_date, normalize_identity_text

            # 历史行可能是未补零日期（2026-1-5），SQL 等值比较会把补零后的
            # 导入当成新行程：按路线取出候选行，在 Python 侧归一日期再比对。
            # 路线字段同样按存储身份口径（CRLF / CR → LF）归一后比较，否则
            # 换行写法不同的同一趟行程会被重复插入
            candidates = self._fetchall(
                "SELECT id, trip_id, start_date, end_date FROM aligo_trips "
                "WHERE user_id = %s "
                "AND REPLACE(REPLACE(destination, E'\\r\\n', E'\\n'), E'\\r', E'\\n') = %s "
                "AND REPLACE(REPLACE(COALESCE(origin, ''), E'\\r\\n', E'\\n'), E'\\r', E'\\n') = %s",
                (
                    self.user_id,
                    normalize_identity_text(destination),
                    normalize_identity_text(origin),
                ),
            )
            # 候选快照跨连接重建即失效：空档里其它实例可能已插入同行程 /
            # 删掉候选行，按旧快照更新或插入会造出重复
            self._abort_if_reconnected(start_conn)
            want_start = normalize_identity_date(start_date)
            want_end = normalize_identity_date(end_date)
            existing = next(
                (
                    row
                    for row in candidates
                    if normalize_identity_date(row[2]) == want_start
                    and (
                        normalize_identity_date(row[3]) or normalize_identity_date(row[2])
                    )
                    == want_end
                ),
                None,
            )
            if existing:
                # 与 JSON 后端一致：未提供的字段（None）保留原值，不能被默认值
                # 覆盖（手动补录同一行程会丢失来源会话 / 用户修改过的目的）
                self._execute(
                    "UPDATE aligo_trips SET "
                    "purpose = COALESCE(%s, purpose), "
                    "session_id = COALESCE(%s, session_id) "
                    "WHERE id = %s",
                    (trip_info.get("purpose"), trip_info.get("session_id"), existing[0]),
                )
                return str(existing[1] or "")

        trip_number = self._next_trip_number()
        trip_id = f"trip_{trip_number}"
        # INSERT 非幂等：提交后响应丢失自动重放会插出重复行（或撞唯一索引
        # 报错却已写入），按持久化失败上报由调用方整体重试
        self._execute(
            """
            INSERT INTO aligo_trips
                (user_id, trip_id, origin, destination, start_date, end_date, purpose, session_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                self.user_id,
                trip_id,
                trip_info.get("origin"),
                destination,
                trip_info.get("start_date"),
                trip_info.get("end_date"),
                purpose,
                trip_info.get("session_id"),
            ),
            replay_safe=False,
        )
        return trip_id

    def _next_trip_number(self) -> int:
        """下一个行程序号：持久化计数与现有最大值取大后 +1（删除后不复用）。"""
        rows = self._fetchall(
            "SELECT trip_id FROM aligo_trips WHERE user_id = %s", (self.user_id,)
        )
        max_number = 0
        for (trip_id,) in rows:
            match = re.fullmatch(r"trip_(\d+)", str(trip_id or ""))
            if match:
                max_number = max(max_number, int(match.group(1)))
        row = self._fetchone(
            "SELECT last_number FROM aligo_trip_counters WHERE user_id = %s",
            (self.user_id,),
        )
        if row is not None:
            max_number = max(max_number, int(row[0] or 0))
        number = max_number + 1
        self._execute(
            """
            INSERT INTO aligo_trip_counters (user_id, last_number)
            VALUES (%s, %s)
            ON CONFLICT (user_id)
            DO UPDATE SET last_number = GREATEST(
                aligo_trip_counters.last_number, EXCLUDED.last_number
            )
            """,
            (self.user_id, number),
        )
        return number

    def delete_trip(self, trip_id: str) -> bool:
        """删除一条历史行程；返回是否删除（统计由查询动态计算）。

        历史库可能存在重复的 trip_id（旧版按 COUNT 生成），只删除最早插入的
        那一行，不能按 trip_id 批量删除，否则一次操作会抹掉多条不同行程。
        与 save_trip_history / update_trip 共用同一条 advisory lock，避免
        与并发写入交错。
        """
        with self._advisory_lock(f"aligo-trip-{self.user_id}"):
            row = self._fetchone(
                "SELECT id FROM aligo_trips WHERE user_id = %s AND trip_id = %s "
                "ORDER BY id LIMIT 1",
                (self.user_id, trip_id),
            )
            if not row:
                return False
            self._execute(
                "DELETE FROM aligo_trips WHERE user_id = %s AND id = %s",
                (self.user_id, row[0]),
            )
            return True

    def delete_duplicate_trip(self, trip_id: str) -> bool:
        """删除重复 trip_id 中最后插入的一条（去重时保留最早记录）。

        ``delete_trip`` 删除最早一条：去重命令要保留组内第一条、删掉后面
        的副本，用它会把要保留的那条删掉、留下后来的重复行。

        只剩一条同 id 行时返回 False（不删除）：并发的两个去重请求
        （双标签页 / CLI+Web 各自计划）都会调用一次，计数与删除必须原子，
        第二次不能再把仅剩的保留行删光。

        计数、选行与删除在同一条 SQL 里完成：连接闪断时 _run_with_retry
        会重连并重放这条语句，计数子查询在重放时重新求值——先用 COUNT 读
        出计数、再 SELECT/DELETE 的写法会在重连空档里用过期计数删掉并发
        去重实例刚留下的保留行（同一 trip_id 全部消失）。
        """
        with self._advisory_lock(f"aligo-trip-{self.user_id}"):
            deleted = self._execute_rowcount(
                "DELETE FROM aligo_trips WHERE user_id = %s AND id = ("
                "    SELECT id FROM aligo_trips WHERE user_id = %s AND trip_id = %s"
                "    ORDER BY id DESC LIMIT 1"
                ") AND ("
                "    SELECT COUNT(*) FROM aligo_trips"
                "    WHERE user_id = %s AND trip_id = %s"
                ") >= 2",
                (self.user_id, self.user_id, trip_id, self.user_id, trip_id),
            )
            return deleted > 0

    def delete_trips_verified(
        self, targets: list[dict[str, Any]]
    ) -> tuple[int, str | None]:
        """批量按身份复核删除（去重专用）。

        在一次 advisory lock 内取出全部行程的身份快照，逐条复核后按主键
        删除：并发去重 / 编辑被同一把锁串行化，复核与删除之间不存在可
        穿插的写者（此前 verify 与 delete 分两次加锁，被编辑后的行程仍会
        被按 id 删掉）。连接中途重建时中止本批（已删除的保留），调用方
        重试即可。返回 ``(删除数, 失败原因或 None)``。
        """
        from utils.trip_import import trip_identity

        if not targets:
            return 0, None
        removed = 0
        try:
            with self._advisory_lock(f"aligo-trip-{self.user_id}"):
                start_conn = getattr(self, "_conn", None)
                rows = self._fetchall(
                    "SELECT id, trip_id, origin, destination, start_date, end_date "
                    "FROM aligo_trips WHERE user_id = %s ORDER BY id",
                    (self.user_id,),
                )
                self._abort_if_reconnected(start_conn)
                working = [
                    {
                        "id": int(row[0]),
                        "trip_id": str(row[1] or ""),
                        "origin": row[2],
                        "destination": row[3],
                        "start_date": row[4],
                        "end_date": row[5],
                    }
                    for row in rows
                ]
                for target in targets:
                    trip_id = str(target.get("trip_id") or "")
                    if not trip_id:
                        continue
                    want = trip_identity(target)
                    matches = [
                        row
                        for row in working
                        if row["trip_id"] == trip_id and trip_identity(row) == want
                    ]
                    if not matches:
                        continue
                    kept_id = str(target.get("kept_id") or "")
                    if kept_id and not any(
                        row["trip_id"] == kept_id and trip_identity(row) == want
                        for row in working
                    ):
                        continue
                    prefer_last = bool(target.get("prefer_last"))
                    if prefer_last and len(matches) < 2:
                        continue
                    chosen = matches[-1] if prefer_last else matches[0]
                    self._execute(
                        "DELETE FROM aligo_trips WHERE user_id = %s AND id = %s",
                        (self.user_id, chosen["id"]),
                    )
                    self._abort_if_reconnected(start_conn)
                    working.remove(chosen)
                    removed += 1
        except Exception as exc:  # noqa: BLE001 - 部分删除后失败要如实报告
            return removed, str(exc)
        return removed, None

    def update_trip(self, trip_id: str, fields: dict[str, Any]) -> bool:
        """更新一条行程的字段；返回是否存在。

        与 save_trip_history / delete_trip 共用同一条 advisory lock：
        「冲突检查 SELECT + UPDATE」必须与并发写入串行，否则两个并发更新
        可能都通过检查，造出 JSON 后端不可能出现的重复行程。

        Raises:
            ValueError: 更新后与另一条既有行程（同路线 + 同起止日期）重复
        """
        with self._advisory_lock(f"aligo-trip-{self.user_id}"):
            start_conn = getattr(self, "_conn", None)
            row = self._fetchone(
                "SELECT id, origin, destination, start_date, end_date "
                "FROM aligo_trips WHERE user_id = %s AND trip_id = %s",
                (self.user_id, trip_id),
            )
            if not row:
                return False

            current = {
                "origin": row[1],
                "destination": row[2],
                "start_date": row[3],
                "end_date": row[4],
            }
            merged = {**current, **fields}
            destination = merged.get("destination")
            start_date = str(merged.get("start_date") or "")
            if destination and start_date:
                from utils.trip_import import normalize_identity_date

                # 与 save_trip_history / JSON 后端 _find_same_trip 的去重口径
                # 一致：历史行可能是未补零日期（2026-1-5），SQL 等值比较会把
                # 补零后的更新当成不重复；按路线取候选行后在 Python 侧归一比对
                candidates = self._fetchall(
                    "SELECT id, start_date, end_date FROM aligo_trips "
                    "WHERE user_id = %s AND destination = %s "
                    "AND COALESCE(origin, '') = %s",
                    (self.user_id, destination, str(merged.get("origin") or "")),
                )
                want_start = normalize_identity_date(start_date)
                want_end = normalize_identity_date(
                    str(merged.get("end_date") or start_date)
                )
                duplicate = any(
                    candidate[0] != row[0]
                    and normalize_identity_date(candidate[1]) == want_start
                    and (
                        normalize_identity_date(candidate[2])
                        or normalize_identity_date(candidate[1])
                    )
                    == want_end
                    for candidate in candidates
                )
                if duplicate:
                    raise ValueError("已存在相同行程（同路线 + 同起止日期）")

            # 冲突检查基于上面的读快照；连接重建后快照可能已过期，
            # 空档里其它实例可能已插入同行程或删掉本行，不能再按旧快照写
            self._abort_if_reconnected(start_conn)
            updates = {
                key: fields[key]
                for key in (
                    "origin",
                    "destination",
                    "start_date",
                    "end_date",
                    "purpose",
                )
                if key in fields
            }
            if updates:
                assignments = ", ".join(f"{key} = %s" for key in updates)
                self._execute(
                    f"UPDATE aligo_trips SET {assignments} WHERE id = %s",
                    (*updates.values(), row[0]),
                )
            return True

    def get_trip_history(self, limit: int | None = 10) -> list[dict[str, Any]]:
        base = (
            "SELECT trip_id, origin, destination, start_date, end_date, purpose, session_id, created_at "
            "FROM aligo_trips WHERE user_id = %s"
        )
        if limit:
            rows = self._fetchall(base + " ORDER BY id DESC LIMIT %s", (self.user_id, limit))
            rows = list(reversed(rows))
        else:
            rows = self._fetchall(base + " ORDER BY id", (self.user_id,))

        return [
            {
                "trip_id": trip_id,
                "origin": origin,
                "destination": destination,
                "start_date": start_date,
                "end_date": end_date,
                "purpose": purpose,
                "session_id": session_id,
                "timestamp": created_at.isoformat(),
            }
            for trip_id, origin, destination, start_date, end_date, purpose, session_id, created_at in rows
        ]

    def _frequent_destination_counts(self) -> dict[str, int]:
        """常去目的地计数：只统计出发日期可解析的行程。

        与 JSON 后端的口径一致（``save_trip_histories`` / 迁移重建只统计
        ``_trip_date_is_valid`` 的行程）：备份恢复允许保真保留旧格式日期
        （如 ``2026/03/12``），纯 SQL 的 ``destination IS NOT NULL`` 会把
        这些「僵尸行程」也计入，两后端同一数据给出不同结果。
        SQL 无法精确复刻 ``strptime`` 语义（``to_date`` 会把 2026-02-31
        归一成 3 月 3 日），行程数量受写入口径限制（每用户 5000 条），
        在 Python 侧过滤成本可接受。
        """
        rows = self._fetchall(
            "SELECT destination, start_date FROM aligo_trips "
            "WHERE user_id = %s AND destination IS NOT NULL",
            (self.user_id,),
        )
        counts: dict[str, int] = {}
        for destination, start_date in rows:
            if not _trip_start_is_valid(start_date):
                continue
            key = str(destination)
            counts[key] = counts.get(key, 0) + 1
        return counts

    def get_frequent_destinations(self, top_n: int = 5) -> list[tuple]:
        counts = self._frequent_destination_counts()
        ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        return ordered[:top_n]

    # ---------- 统计 ----------

    def increment_query_count(self) -> None:
        # 计数自增非幂等：提交后响应丢失自动重放会重复计数
        self._execute(
            """
            INSERT INTO aligo_user_stats (user_id, total_queries) VALUES (%s, 1)
            ON CONFLICT (user_id)
            DO UPDATE SET total_queries = aligo_user_stats.total_queries + 1
            """,
            (self.user_id,),
            replay_safe=False,
        )

    def get_statistics(self) -> dict[str, Any]:
        trips_row = self._fetchone("SELECT COUNT(*) FROM aligo_trips WHERE user_id = %s", (self.user_id,))
        messages_row = self._fetchone(
            "SELECT COUNT(*) FROM aligo_chat_messages WHERE user_id = %s", (self.user_id,)
        )
        queries_row = self._fetchone(
            "SELECT total_queries FROM aligo_user_stats WHERE user_id = %s", (self.user_id,)
        )
        frequent = self._frequent_destination_counts()
        return {
            "total_trips": int(trips_row[0]) if trips_row else 0,
            "total_messages": int(messages_row[0]) if messages_row else 0,
            "total_queries": int(queries_row[0]) if queries_row else 0,
            "frequent_destinations": frequent,
        }

    # ---------- 清理 ----------

    def clear_history(self) -> None:
        self._execute("DELETE FROM aligo_chat_messages WHERE user_id = %s", (self.user_id,))
        self._execute("DELETE FROM aligo_trips WHERE user_id = %s", (self.user_id,))
        self._execute("DELETE FROM aligo_session_titles WHERE user_id = %s", (self.user_id,))
        self._execute("DELETE FROM aligo_pinned_sessions WHERE user_id = %s", (self.user_id,))
        self._execute("DELETE FROM aligo_archived_sessions WHERE user_id = %s", (self.user_id,))

    def delete_all(self) -> None:
        for table in (
            "aligo_chat_messages",
            "aligo_trips",
            "aligo_trip_counters",
            "aligo_preferences",
            "aligo_user_stats",
            "aligo_session_titles",
            "aligo_pinned_sessions",
            "aligo_archived_sessions",
        ):
            self._execute(f"DELETE FROM {table} WHERE user_id = %s", (self.user_id,))

    def close(self) -> None:
        # 与所有数据库操作共用同一把实例锁：drain 超时后旧落库线程仍可能
        # 在用连接，drop_session / 会话关闭不能把它从下面关掉
        lock = getattr(self, "_conn_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._conn_lock = lock
        with lock:
            try:
                self._conn.close()
            except Exception:
                pass
