"""
PostgreSQL 长期记忆后端

启用方式：设置环境变量 ``DATABASE_URL``，例如
    postgresql://aligo:aligo@localhost:5432/aligo

- 表结构首次连接时自动创建（``aligo_`` 前缀，可重复执行）；
- 未配置或连接失败时，由 ``context.storage`` 的工厂回退到 JSON 文件实现；
- 偏好值以 JSONB 存储（字符串 / 列表等类型原样保留）。
"""
import logging
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
        if removed:
            logger.info(
                "Deleted chat session from PostgreSQL: %s (%s messages)", session_id, removed
            )
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

    # ---------- 历史行程 ----------

    def save_trip_history(self, trip_info: dict[str, Any]) -> None:
        row = self._fetchone(
            "SELECT COUNT(*) FROM aligo_trips WHERE user_id = %s",
            (self.user_id,),
        )
        trip_number = int(row[0]) + 1 if row else 1
        self._execute(
            """
            INSERT INTO aligo_trips
                (user_id, trip_id, origin, destination, start_date, end_date, purpose, session_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                self.user_id,
                f"trip_{trip_number}",
                trip_info.get("origin"),
                trip_info.get("destination"),
                trip_info.get("start_date"),
                trip_info.get("end_date"),
                trip_info.get("purpose") or "旅游",
                trip_info.get("session_id"),
            ),
        )

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

    def delete_all(self) -> None:
        for table in (
            "aligo_chat_messages",
            "aligo_trips",
            "aligo_preferences",
            "aligo_user_stats",
        ):
            self._execute(f"DELETE FROM {table} WHERE user_id = %s", (self.user_id,))

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
