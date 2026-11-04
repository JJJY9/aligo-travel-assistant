"""
长期记忆 (Long-term Memory)
持久化存储用户信息，支持跨会话访问
"""
import functools
import json
import logging
import os
import re
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 同一个 JSON 文件可能被多个 LongTermMemory 实例修改（Web 请求各开一个实例、
# MemoryManager 还会重建实例），按路径共享一把进程内可重入锁，避免并发
# 读改写互相覆盖。
_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _lock_for_path(path: str) -> threading.RLock:
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(path)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[path] = lock
        return lock


def _locked_mutation(func):
    """写操作装饰器：按文件路径加锁，并在修改前重载磁盘数据。

    长期记忆是整文件快照写回：调用方可能持有回合开始时的旧快照（例如一轮
    对话进行中），直接覆盖会丢失其它请求刚完成的写入（删掉的行程复活、
    新偏好丢失）。所有修改方法统一「重载 → 修改 → 原子写回」。
    """

    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        with self._write_lock:
            self._reload_latest()
            return func(self, *args, **kwargs)

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
        self.data = self._load()
        logger.info(f"Long-term memory initialized for user: {user_id}")

    def _load(self) -> dict[str, Any]:
        """从文件加载数据"""
        if os.path.exists(self.db_path):
            try:
                with open(self.db_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    logger.debug(f"Loaded long-term memory from {self.db_path}")

                    # 数据迁移：兼容旧格式
                    data = self._migrate_data(data)
                    return data
            except Exception as e:
                logger.error(f"Failed to load long-term memory: {e}")
                return self._init_data()
        else:
            logger.info("No existing long-term memory, creating new")
            return self._init_data()

    def _reload_latest(self) -> None:
        """从磁盘重载最新数据（写操作前调用，避免旧快照覆盖其它写入）。"""
        try:
            with open(self.db_path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except Exception:  # noqa: BLE001 - 重载失败时沿用内存快照
            logger.warning("重载长期记忆失败，沿用内存快照", exc_info=True)
            return
        if isinstance(data, dict):
            self.data = self._migrate_data(data)

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
        if "statistics" not in data:
            data["statistics"] = {}
        if "total_messages" not in data.get("statistics", {}):
            data["statistics"]["total_messages"] = 0
        if "total_queries" not in data.get("statistics", {}):
            data["statistics"]["total_queries"] = 0
        if "preferences" not in data:
            data["preferences"] = []

        # 2. 迁移旧格式：字典 → 列表
        if isinstance(data.get("preferences"), dict):
            old_prefs = data["preferences"]
            new_prefs = []
            for pref_type, pref_value in old_prefs.items():
                if pref_value is not None:
                    new_prefs.append({"type": pref_type, "value": pref_value})
            data["preferences"] = new_prefs
            logger.info(f"Migrated: Converted preferences from dict to list ({len(new_prefs)} items)")

        # 3. 修复嵌套 bug（旧代码产生的错误数据）
        if isinstance(data.get("preferences"), list):
            fixed_prefs = []
            for pref in data["preferences"]:
                if isinstance(pref, dict):
                    # 错误的嵌套：{"type": "preferences", "value": [...]}
                    if pref.get("type") == "preferences" and isinstance(pref.get("value"), list):
                        for nested_pref in pref["value"]:
                            if isinstance(nested_pref, dict) and "type" in nested_pref:
                                fixed_prefs.append({"type": nested_pref["type"], "value": nested_pref["value"]})
                        logger.info("Migrated: Fixed nested preferences bug")
                    else:
                        fixed_prefs.append(pref)

            if fixed_prefs != data["preferences"]:
                data["preferences"] = fixed_prefs

        # 迁移结果在下次写入时落盘：读取路径不再无条件重写文件，
        # 避免搜索/状态等只读请求与对话写入互相覆盖。
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
                os.replace(temp_path, self.db_path)
            finally:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            logger.debug(f"Saved long-term memory to {self.db_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to save long-term memory: {e}")
            return False

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

        self._save()
        logger.info(f"Saved preference: {pref_type} = {value}")

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
        self._save()
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

        self._save()
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

        self._save()
        logger.info(f"Added airline preference: {airline}")

    @_locked_mutation
    def add_chat_message(self, role: str, content: str, session_id: str | None = None):
        """
        添加聊天消息到长期记忆

        Args:
            role: 角色 (user/assistant)
            content: 消息内容
            session_id: 会话ID（可选）
        """
        message = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            "session_id": session_id
        }

        self.data["chat_history"].append(message)
        self.data["statistics"]["total_messages"] += 1
        self._save()
        logger.debug(f"Added chat message to long-term memory: {role}")

    def get_chat_history(self, limit: int | None = None, session_id: str | None = None) -> list[dict[str, Any]]:
        """
        获取聊天历史

        Args:
            limit: 返回数量限制
            session_id: 会话ID（只返回特定会话的消息）

        Returns:
            消息列表
        """
        messages = self.data["chat_history"]

        if session_id:
            messages = [m for m in messages if m.get("session_id") == session_id]

        if limit:
            return messages[-limit:]
        return messages

    @_locked_mutation
    def delete_chat_session(self, session_id: str) -> int:
        """删除某个会话的全部聊天记录，返回删除条数。"""
        messages = self.data["chat_history"]
        remaining = [m for m in messages if m.get("session_id") != session_id]
        removed = len(messages) - len(remaining)
        titles = self.data.get("session_titles") or {}
        pinned_sessions = self.data.get("pinned_sessions") or []
        archived_sessions = self.data.get("archived_sessions") or []
        metadata_changed = (
            session_id in titles
            or session_id in pinned_sessions
            or session_id in archived_sessions
        )

        if removed:
            self.data["chat_history"] = remaining
            stats = self.data["statistics"]
            stats["total_messages"] = max(0, stats.get("total_messages", 0) - removed)

        if removed or metadata_changed:
            self.data.get("session_titles", {}).pop(session_id, None)
            if isinstance(pinned_sessions, list) and session_id in pinned_sessions:
                pinned_sessions.remove(session_id)
            if isinstance(archived_sessions, list) and session_id in archived_sessions:
                archived_sessions.remove(session_id)
            self._save()
            logger.info(
                "Deleted chat session: %s (%s messages, metadata=%s)",
                session_id,
                removed,
                metadata_changed,
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
            if message.get("session_id") == session_id
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
        # 会话消息已被删空时同步清理归档标记，避免幽灵归档
        if not any(
            message.get("session_id") == session_id
            for message in self.data["chat_history"]
        ):
            archived_sessions = self.data.get("archived_sessions") or []
            if session_id in archived_sessions:
                archived_sessions.remove(session_id)
        if not self._save():
            raise OSError("长期记忆写入失败")
        logger.info("Deleted last turn of session %s (%s messages)", session_id, count)
        return count

    @_locked_mutation
    def set_session_title(self, session_id: str, title: str) -> None:
        """设置会话自定义标题；空标题表示恢复自动标题。"""
        titles = self.data.setdefault("session_titles", {})
        cleaned = title.strip()
        if cleaned:
            titles[session_id] = cleaned[:60]
        else:
            titles.pop(session_id, None)
        if not self._save():
            raise OSError("长期记忆写入失败")
        logger.info(f"Set session title: {session_id} -> {cleaned or '(auto)'}")

    def get_session_titles(self) -> dict[str, str]:
        """返回全部会话自定义标题。"""
        return dict(self.data.get("session_titles") or {})

    @_locked_mutation
    def set_session_pinned(self, session_id: str, pinned: bool) -> None:
        """置顶 / 取消置顶会话。"""
        pinned_sessions = self.data.setdefault("pinned_sessions", [])
        if pinned and session_id not in pinned_sessions:
            pinned_sessions.append(session_id)
        elif not pinned and session_id in pinned_sessions:
            pinned_sessions.remove(session_id)
        if not self._save():
            raise OSError("长期记忆写入失败")

    def get_pinned_sessions(self) -> list[str]:
        """返回置顶会话 ID（按置顶时间先后）。"""
        return list(self.data.get("pinned_sessions") or [])

    @_locked_mutation
    def set_session_archived(self, session_id: str, archived: bool) -> None:
        """归档 / 恢复会话；归档只影响会话列表展示，不删除消息。"""
        archived_sessions = self.data.setdefault("archived_sessions", [])
        if archived and session_id not in archived_sessions:
            archived_sessions.append(session_id)
        elif not archived and session_id in archived_sessions:
            archived_sessions.remove(session_id)
        if not self._save():
            raise OSError("长期记忆写入失败")
        logger.info("Set session archived: %s -> %s", session_id, archived)

    def get_archived_sessions(self) -> list[str]:
        """返回归档会话 ID。"""
        return list(self.data.get("archived_sessions") or [])

    @_locked_mutation
    def save_trip_history(self, trip_info: dict[str, Any]) -> str:
        """保存行程历史，返回 trip_id。

        同路线 + 同起止日期的行程视为重复记录（多次对话规划同一趟行程），
        更新既有记录而不是追加，避免行程 / 统计 / 日历重复。
        """
        existing = self._find_same_trip(trip_info)
        if existing is not None:
            for key in ("purpose", "session_id"):
                value = trip_info.get(key)
                if value is not None:
                    existing[key] = value
            existing["timestamp"] = datetime.now().isoformat()
            self._save()
            logger.info(f"Updated existing trip history: {existing.get('trip_id')}")
            return str(existing.get("trip_id") or "")

        trip_record = {
            "trip_id": self._next_trip_id(),
            "timestamp": datetime.now().isoformat(),
            **trip_info
        }

        self.data["trip_history"].append(trip_record)

        # 更新统计信息
        self.data["statistics"]["total_trips"] += 1

        # 更新常去目的地统计
        destination = trip_info.get("destination")
        if destination:
            freq = self.data["statistics"]["frequent_destinations"]
            freq[destination] = freq.get(destination, 0) + 1

        self._save()
        logger.info(f"Saved trip history: {trip_record['trip_id']}")
        return str(trip_record["trip_id"])

    def _find_same_trip(self, trip_info: dict[str, Any]) -> dict[str, Any] | None:
        """查找同路线、同起止日期的既有行程（无出发日期时不参与去重）。"""
        destination = trip_info.get("destination")
        start_date = str(trip_info.get("start_date") or "")
        if not destination or not start_date:
            return None
        origin = str(trip_info.get("origin") or "")
        end_date = str(trip_info.get("end_date") or "")
        for trip in self.data.get("trip_history", []):
            if (
                trip.get("destination") == destination
                and str(trip.get("origin") or "") == origin
                and str(trip.get("start_date") or "") == start_date
                and str(trip.get("end_date") or "") == end_date
            ):
                return trip
        return None

    def _next_trip_id(self) -> str:
        """下一个行程 id：现有最大序号 + 1（删除后不复用，避免与统计/会话关联错乱）。"""
        max_number = 0
        for trip in self.data.get("trip_history", []):
            match = re.fullmatch(r"trip_(\d+)", str(trip.get("trip_id", "")))
            if match:
                max_number = max(max_number, int(match.group(1)))
        return f"trip_{max_number + 1}"

    @_locked_mutation
    def delete_trip(self, trip_id: str) -> bool:
        """删除一条历史行程并同步统计；返回是否删除。"""
        trips = self.data.get("trip_history", [])
        for index, trip in enumerate(trips):
            if str(trip.get("trip_id", "")) == str(trip_id):
                removed = trips.pop(index)
                stats = self.data.setdefault("statistics", {})
                stats["total_trips"] = max(0, int(stats.get("total_trips", 0)) - 1)
                destination = removed.get("destination")
                frequent = stats.setdefault("frequent_destinations", {})
                if destination and destination in frequent:
                    frequent[destination] = int(frequent[destination]) - 1
                    if frequent[destination] <= 0:
                        del frequent[destination]
                if not self._save():
                    raise OSError("写入长期记忆失败")
                logger.info(f"Deleted trip history: {trip_id}")
                return True
        return False

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
            for key in ("origin", "destination", "start_date", "end_date", "purpose"):
                if key in fields:
                    trip[key] = fields[key]
            new_destination = trip.get("destination")
            if old_destination != new_destination:
                stats = self.data.setdefault("statistics", {})
                frequent = stats.setdefault("frequent_destinations", {})
                if old_destination and old_destination in frequent:
                    frequent[old_destination] = int(frequent[old_destination]) - 1
                    if frequent[old_destination] <= 0:
                        del frequent[old_destination]
                if new_destination:
                    frequent[new_destination] = int(frequent.get(new_destination, 0)) + 1
            self._save()
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
        sorted_dest = sorted(freq.items(), key=lambda x: x[1], reverse=True)
        return sorted_dest[:top_n]

    @_locked_mutation
    def increment_query_count(self):
        """增加查询计数"""
        stats = self.data["statistics"]
        stats["total_queries"] = stats.get("total_queries", 0) + 1
        self._save()

    def get_statistics(self) -> dict[str, Any]:
        """获取统计信息"""
        return self.data["statistics"].copy()

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
        self._save()
        logger.info("Cleared all history (chat + trips)")

    @_locked_mutation
    def delete_all(self):
        """删除所有数据（包括文件）"""
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
            logger.warning(f"Deleted long-term memory file: {self.db_path}")
