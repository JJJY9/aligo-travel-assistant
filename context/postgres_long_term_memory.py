"""
PostgreSQL 长期记忆后端

启用方式：设置环境变量 ``DATABASE_URL``，例如
    postgresql://aligo:aligo@localhost:5432/aligo

- 表结构首次连接时自动创建（``aligo_`` 前缀，可重复执行）；
- 未配置或连接失败时，由 ``context.storage`` 的工厂回退到 JSON 文件实现；
- 偏好值以 JSONB 存储（字符串 / 列表等类型原样保留）。
"""
import contextlib
import logging
import re
from typing import Any

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


class PostgresLongTermMemory:
    """基于 PostgreSQL 的长期记忆（偏好 / 行程 / 对话历史 / 统计）。

    各方法在调用时直接查询数据库（无常驻内存快照），多会话 / 多进程写入天然一致。
    """

    # 直接查询数据库，无内存快照，无需在多轮对话前重建
    snapshot_based = False

    def __init__(self, user_id: str, dsn: str):
        import psycopg

        self.user_id = user_id
        self.dsn = dsn
        self._conn = psycopg.connect(dsn, autocommit=True, connect_timeout=3)
        self._ensure_schema()
        logger.info("PostgreSQL long-term memory initialized for user: %s", user_id)

    # ---------- 内部工具 ----------

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

    def _execute(self, sql: str, params: tuple | None = None) -> None:
        with self._conn.cursor() as cursor:
            cursor.execute(sql, params)

    def _fetchall(self, sql: str, params: tuple | None = None) -> list[tuple]:
        with self._conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()

    def _fetchone(self, sql: str, params: tuple | None = None):
        with self._conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchone()

    @staticmethod
    def _json(value: Any) -> Any:
        from psycopg.types.json import Json

        return Json(value)

    # ---------- 偏好 ----------

    def save_preference(self, pref_type: str, value: Any) -> None:
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
        with self._conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM aligo_preferences WHERE user_id = %s AND pref_type = %s",
                (self.user_id, pref_type),
            )
            removed = cursor.rowcount or 0
        if removed:
            logger.info("Deleted preference from PostgreSQL: %s", pref_type)
        return bool(removed)

    def append_preference(self, pref_type: str, value: Any) -> Any:
        """原子地追加一个偏好值（列表语义），返回合并后的值。

        用会话级 advisory lock 包住「读 + 写」，避免与本轮 LLM 调用期间
        用户在记忆面板修改同一类型偏好互相覆盖。
        """
        lock_name = f"aligo-pref-{self.user_id}-{pref_type}"
        with self._conn.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_lock(hashtext(%s)::bigint)", (lock_name,)
            )
        try:
            current = self.get_preference(pref_type)
            if isinstance(current, list):
                merged = list(current)
                if value not in merged:
                    merged.append(value)
            elif current == value:
                merged = [value]
            elif current:
                merged = [current, value]
            else:
                merged = [value]
            self.save_preference(pref_type, merged)
            return merged
        finally:
            with contextlib.suppress(Exception):
                with self._conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_unlock(hashtext(%s)::bigint)",
                        (lock_name,),
                    )

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

    def add_chat_message(self, role: str, content: str, session_id: str | None = None) -> None:
        self._execute(
            "INSERT INTO aligo_chat_messages (user_id, role, content, session_id) VALUES (%s, %s, %s, %s)",
            (self.user_id, role, content, session_id),
        )

    def get_chat_history(
        self,
        limit: int | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        where = "user_id = %s"
        params: list[Any] = [self.user_id]
        if session_id:
            where += " AND session_id = %s"
            params.append(session_id)

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
        """删除某个会话的全部聊天记录，返回删除条数。"""
        with self._conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM aligo_chat_messages WHERE user_id = %s AND session_id = %s",
                (self.user_id, session_id),
            )
            removed = cursor.rowcount or 0
            cursor.execute(
                "DELETE FROM aligo_session_titles WHERE user_id = %s AND session_id = %s",
                (self.user_id, session_id),
            )
            cursor.execute(
                "DELETE FROM aligo_pinned_sessions WHERE user_id = %s AND session_id = %s",
                (self.user_id, session_id),
            )
            cursor.execute(
                "DELETE FROM aligo_archived_sessions WHERE user_id = %s AND session_id = %s",
                (self.user_id, session_id),
            )
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
        with self._conn.cursor() as cursor:
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
        if removed:
            # 会话消息已被删空时同步清理归档标记，避免幽灵归档
            self._execute(
                "DELETE FROM aligo_archived_sessions "
                "WHERE user_id = %s AND session_id = %s "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM aligo_chat_messages "
                "  WHERE user_id = %s AND session_id = %s"
                ")",
                (self.user_id, session_id, self.user_id, session_id),
            )
        logger.info("Deleted last turn of session %s (%s messages)", session_id, removed)
        return removed

    def set_session_title(self, session_id: str, title: str) -> None:
        """设置会话自定义标题；空标题表示恢复自动标题。"""
        cleaned = title.strip()
        if not cleaned:
            self._execute(
                "DELETE FROM aligo_session_titles WHERE user_id = %s AND session_id = %s",
                (self.user_id, session_id),
            )
            return
        self._execute(
            """
            INSERT INTO aligo_session_titles (user_id, session_id, title)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id, session_id)
            DO UPDATE SET title = EXCLUDED.title, updated_at = now()
            """,
            (self.user_id, session_id, cleaned[:60]),
        )

    def get_session_titles(self) -> dict[str, str]:
        """返回全部会话自定义标题。"""
        rows = self._fetchall(
            "SELECT session_id, title FROM aligo_session_titles WHERE user_id = %s",
            (self.user_id,),
        )
        return {session_id: title for session_id, title in rows}

    def set_session_pinned(self, session_id: str, pinned: bool) -> None:
        """置顶 / 取消置顶会话。"""
        if pinned:
            self._execute(
                "INSERT INTO aligo_pinned_sessions (user_id, session_id) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (self.user_id, session_id),
            )
        else:
            self._execute(
                "DELETE FROM aligo_pinned_sessions WHERE user_id = %s AND session_id = %s",
                (self.user_id, session_id),
            )

    def get_pinned_sessions(self) -> list[str]:
        """返回置顶会话 ID（按置顶时间先后）。"""
        rows = self._fetchall(
            "SELECT session_id FROM aligo_pinned_sessions WHERE user_id = %s "
            "ORDER BY pinned_at, session_id",
            (self.user_id,),
        )
        return [row[0] for row in rows]

    def set_session_archived(self, session_id: str, archived: bool) -> None:
        """归档 / 恢复会话；归档只影响会话列表展示，不删除消息。"""
        if archived:
            self._execute(
                "INSERT INTO aligo_archived_sessions (user_id, session_id) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (self.user_id, session_id),
            )
        else:
            self._execute(
                "DELETE FROM aligo_archived_sessions WHERE user_id = %s AND session_id = %s",
                (self.user_id, session_id),
            )

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
        lock_name = f"aligo-trip-{self.user_id}"
        with self._conn.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_lock(hashtext(%s)::bigint)", (lock_name,)
            )
        try:
            return self._save_trip_history_locked(trip_info, purpose, destination)
        finally:
            with contextlib.suppress(Exception):
                with self._conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_unlock(hashtext(%s)::bigint)",
                        (lock_name,),
                    )

    def _save_trip_history_locked(
        self,
        trip_info: dict[str, Any],
        purpose: str,
        destination: Any,
    ) -> str:
        start_date = str(trip_info.get("start_date") or "")
        if destination and start_date:
            existing = self._fetchone(
                """
                SELECT id, trip_id FROM aligo_trips
                WHERE user_id = %s
                  AND destination = %s
                  AND origin IS NOT DISTINCT FROM %s
                  AND start_date = %s
                  AND end_date IS NOT DISTINCT FROM %s
                LIMIT 1
                """,
                (
                    self.user_id,
                    destination,
                    trip_info.get("origin"),
                    start_date,
                    trip_info.get("end_date"),
                ),
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
        )
        return trip_id

    def _next_trip_number(self) -> int:
        """下一个行程序号：现有最大 trip_N 的 N + 1（删除后不复用）。"""
        rows = self._fetchall(
            "SELECT trip_id FROM aligo_trips WHERE user_id = %s", (self.user_id,)
        )
        max_number = 0
        for (trip_id,) in rows:
            match = re.fullmatch(r"trip_(\d+)", str(trip_id or ""))
            if match:
                max_number = max(max_number, int(match.group(1)))
        return max_number + 1

    def delete_trip(self, trip_id: str) -> bool:
        """删除一条历史行程；返回是否删除（统计由查询动态计算）。

        历史库可能存在重复的 trip_id（旧版按 COUNT 生成），只删除最早插入的
        那一行，不能按 trip_id 批量删除，否则一次操作会抹掉多条不同行程。
        """
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

    def update_trip(self, trip_id: str, fields: dict[str, Any]) -> bool:
        """更新一条行程的字段；返回是否存在。

        Raises:
            ValueError: 更新后与另一条既有行程（同路线 + 同起止日期）重复
        """
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
            same = self._fetchone(
                """
                SELECT id FROM aligo_trips
                WHERE user_id = %s
                  AND id <> %s
                  AND destination = %s
                  AND origin IS NOT DISTINCT FROM %s
                  AND start_date = %s
                  AND end_date IS NOT DISTINCT FROM %s
                LIMIT 1
                """,
                (
                    self.user_id,
                    row[0],
                    destination,
                    merged.get("origin"),
                    start_date,
                    merged.get("end_date"),
                ),
            )
            if same:
                raise ValueError("已存在相同行程（同路线 + 同起止日期）")

        updates = {
            key: fields[key]
            for key in ("origin", "destination", "start_date", "end_date", "purpose")
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
        self._execute(
            """
            INSERT INTO aligo_user_stats (user_id, total_queries) VALUES (%s, 1)
            ON CONFLICT (user_id)
            DO UPDATE SET total_queries = aligo_user_stats.total_queries + 1
            """,
            (self.user_id,),
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
            "aligo_preferences",
            "aligo_user_stats",
            "aligo_session_titles",
            "aligo_pinned_sessions",
            "aligo_archived_sessions",
        ):
            self._execute(f"DELETE FROM {table} WHERE user_id = %s", (self.user_id,))

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
