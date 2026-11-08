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

from utils.errors import MemoryPersistenceError
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

        kwargs: dict[str, Any] = {"autocommit": True, "connect_timeout": 3}
        if self._statement_timeout_ms:
            kwargs["options"] = (
                f"-c statement_timeout={int(self._statement_timeout_ms)}"
            )
        return psycopg.connect(self.dsn, **kwargs)

    def _ensure_schema(self) -> None:
        with self._conn.cursor() as cursor:
            cursor.execute(_SCHEMA_SQL)
            try:
                # 历史库可能已有重复 trip_id，建索引失败只记录，不阻止后端启用
                cursor.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_aligo_trips_user_trip "
                    "ON aligo_trips (user_id, trip_id)"
                )
            except Exception as exc:  # noqa: BLE001 - 仅提示，写入仍有 advisory lock 保护
                logger.warning("无法创建 trip_id 唯一索引: %s", exc)

    def _execute(
        self, sql: str, params: tuple | None = None, replay_safe: bool = True
    ) -> None:
        def run() -> None:
            with self._conn.cursor() as cursor:
                cursor.execute(sql, params)

        self._run_with_retry(run, replay_safe=replay_safe)

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
            removed = self._run_with_retry(run)
        if removed:
            logger.info("Deleted preference from PostgreSQL: %s", pref_type)
        return bool(removed)

    def append_preference(self, pref_type: str, value: Any) -> Any:
        """原子地追加一个偏好值（列表语义），返回合并后的值。

        用会话级 advisory lock 包住「读 + 写」，避免与本轮 LLM 调用期间
        用户在记忆面板修改同一类型偏好互相覆盖。
        """
        with self._preference_lock(pref_type):
            current = self.get_preference(pref_type)
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

        removed = self._run_with_retry(run)
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
        from utils.trip_import import normalize_identity_date

        def _key(
            destination: Any, origin: Any, start_date: Any, end_date: Any
        ) -> tuple[str, str, str, str] | None:
            if not destination or not start_date:
                return None
            start_key = normalize_identity_date(start_date)
            end_key = normalize_identity_date(end_date) or start_key
            return (str(destination), str(origin or ""), start_key, end_key)

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
            for trip in trips:
                destination = trip.get("destination")
                start_date = str(trip.get("start_date") or "")
                origin = trip.get("origin")
                end_date = trip.get("end_date") or start_date
                key = _key(destination, origin, start_date, end_date)
                existing_id = index.get(key) if key is not None else None
                if existing_id is not None:
                    # 与单条保存一致：未提供的字段（None）保留原值
                    self._execute(
                        "UPDATE aligo_trips SET "
                        "purpose = COALESCE(%s, purpose), "
                        "session_id = COALESCE(%s, session_id) "
                        "WHERE id = %s",
                        (trip.get("purpose"), trip.get("session_id"), existing_id),
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
        start_date = str(trip_info.get("start_date") or "")
        origin = str(trip_info.get("origin") or "")
        end_date = str(trip_info.get("end_date") or start_date)
        if destination and start_date:
            from utils.trip_import import normalize_identity_date

            # 历史行可能是未补零日期（2026-1-5），SQL 等值比较会把补零后的
            # 导入当成新行程：按路线取出候选行，在 Python 侧归一日期再比对
            candidates = self._fetchall(
                "SELECT id, trip_id, start_date, end_date FROM aligo_trips "
                "WHERE user_id = %s AND destination = %s AND COALESCE(origin, '') = %s",
                (self.user_id, destination, origin),
            )
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
        （双标签页 / CLI+Web 各自计划）都会调用一次，计数与删除在同一把
        advisory lock 内完成，第二次不能再把仅剩的保留行删光。
        """
        with self._advisory_lock(f"aligo-trip-{self.user_id}"):
            count_row = self._fetchone(
                "SELECT COUNT(*) FROM aligo_trips WHERE user_id = %s AND trip_id = %s",
                (self.user_id, trip_id),
            )
            if not count_row or int(count_row[0] or 0) < 2:
                return False
            row = self._fetchone(
                "SELECT id FROM aligo_trips WHERE user_id = %s AND trip_id = %s "
                "ORDER BY id DESC LIMIT 1",
                (self.user_id, trip_id),
            )
            if not row:
                return False
            self._execute(
                "DELETE FROM aligo_trips WHERE user_id = %s AND id = %s",
                (self.user_id, row[0]),
            )
            return True

    def update_trip(self, trip_id: str, fields: dict[str, Any]) -> bool:
        """更新一条行程的字段；返回是否存在。

        与 save_trip_history / delete_trip 共用同一条 advisory lock：
        「冲突检查 SELECT + UPDATE」必须与并发写入串行，否则两个并发更新
        可能都通过检查，造出 JSON 后端不可能出现的重复行程。

        Raises:
            ValueError: 更新后与另一条既有行程（同路线 + 同起止日期）重复
        """
        with self._advisory_lock(f"aligo-trip-{self.user_id}"):
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

    def get_frequent_destinations(self, top_n: int = 5) -> list[tuple]:
        rows = self._fetchall(
            """
            SELECT destination, COUNT(*) AS trip_count
            FROM aligo_trips
            WHERE user_id = %s AND destination IS NOT NULL
            GROUP BY destination
            ORDER BY trip_count DESC, destination
            LIMIT %s
            """,
            (self.user_id, top_n),
        )
        return [(row[0], int(row[1])) for row in rows]

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
        frequent = {
            destination: int(count)
            for destination, count in self._fetchall(
                """
                SELECT destination, COUNT(*) FROM aligo_trips
                WHERE user_id = %s AND destination IS NOT NULL
                GROUP BY destination
                """,
                (self.user_id,),
            )
        }
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
