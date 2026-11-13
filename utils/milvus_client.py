"""
进程内共享的 Milvus Lite 客户端

同一个 ``milvus_lite.db`` 被多个 RAGKnowledgeAgent（Web 多会话、测试）或
知识库状态查询同时打开时，Milvus Lite 会因文件锁冲突失败；这里按数据库路径
缓存客户端，所有调用方复用同一个连接。进程退出时可由操作系统释放句柄，
需要强制关闭时调用 :func:`shutdown_milvus_clients`。
"""
import logging
import os
import threading
import time
import weakref
from typing import Any

logger = logging.getLogger(__name__)

# 已发放的最大实体 id（客户端对象 → (集合, 字段) → id）：弱引用键，避免
# 不同测试 / 重连的新客户端因 CPython 复用 id() 而共享旧计数
_LAST_IDS: "weakref.WeakKeyDictionary[Any, dict[tuple[str, str], int]]" = (
    weakref.WeakKeyDictionary()
)
_ID_LOCK = threading.Lock()

# gRPC 使用的 int32 上限，约 24.8 天；保持长连接避免空闲被断开
_GRPC_MAX_MS = "2147483647"
os.environ.setdefault("GRPC_KEEPALIVE_TIME_MS", _GRPC_MAX_MS)
os.environ.setdefault("GRPC_KEEPALIVE_TIMEOUT_MS", "20000")
os.environ.setdefault("GRPC_KEEPALIVE_PERMIT_WITHOUT_CALLS", "0")
os.environ.setdefault("GRPC_HTTP2_MIN_RECV_PING_INTERVAL_WITHOUT_DATA_MS", _GRPC_MAX_MS)
os.environ.setdefault("GRPC_HTTP2_MIN_PING_INTERVAL_WITHOUT_DATA_MS", _GRPC_MAX_MS)

_CLIENTS: dict[str, Any] = {}
_LOCK = threading.RLock()


def _create_client(db_path: str) -> Any:
    import pymilvus
    from pymilvus import MilvusClient

    options: dict[str, Any] = {
        "grpc_options": {
            "keepalive_time": _GRPC_MAX_MS,
            "keepalive_timeout": "20000",
            "keepalive_permit_without_calls": "0",
            "http2_min_recv_ping_interval_without_data": _GRPC_MAX_MS,
            "http2_min_ping_interval_without_data": _GRPC_MAX_MS,
        },
    }
    # pymilvus 3.x 的 ConnectionManager 按 uri 复用共享连接：close() 只解除
    # 客户端引用，不关闭底层 channel，重连会拿回同一条（可能已损坏的）连接，
    # reset_milvus_client 变成空操作。进程内已按路径缓存客户端，用 dedicated
    # 让 close() 真正关闭，重连才能建出新的 channel。2.x 没有该参数。
    try:
        major = int(str(getattr(pymilvus, "__version__", "2")).split(".", 1)[0])
    except ValueError:
        major = 2
    if major >= 3:
        options["dedicated"] = True
    return MilvusClient(db_path, **options)


def _create_client_with_retry(db_path: str) -> Any:
    """创建客户端；另一进程刚释放文件锁时可能瞬时失败，重试一次。"""
    try:
        return _create_client(db_path)
    except Exception:  # noqa: BLE001 - 仅对首次失败做一次短暂重试
        time.sleep(0.5)
        return _create_client(db_path)


def get_milvus_client(db_path: str) -> Any:
    """获取（或创建）进程内共享的 Milvus Lite 客户端。"""
    with _LOCK:
        client = _CLIENTS.get(db_path)
        if client is not None:
            return client
        client = _create_client_with_retry(db_path)
        _CLIENTS[db_path] = client
        return client


def peek_milvus_client(db_path: str) -> Any | None:
    """返回已缓存的共享客户端；不创建新连接。"""
    with _LOCK:
        return _CLIENTS.get(db_path)


def next_entity_id(
    client: Any,
    collection_name: str,
    id_field: str = "id",
    max_scan: int = 16384,
) -> int:
    """返回集合内下一个可用 ``id``（空集合从 1 开始；进程内单调不复用）。

    先 ``load_collection``：Milvus Lite 3.x 新进程中集合默认 released，
    未加载时 query 会直接报错。查询失败时向上抛出，而不是退回行数兜底——
    删除过片段后 row_count 小于最大 id，按行数递增会与已有主键冲突，
    而 Milvus 对重复主键的 insert 会静默覆盖旧实体（数据丢失）。

    单次 query 最多返回 ``max_scan`` 行且顺序不确定；集合超过该规模时
    按 ``id >= 上一批最大值 + 1`` 逐批扫描，避免只看到任意子集而取到
    一个偏小的「最大值」、与已有主键冲突。
    """
    try:
        client.load_collection(collection_name)
    except Exception as exc:  # noqa: BLE001 - 2.x 无该调用；查询失败会在下面暴露
        logger.debug("load_collection before next id skipped: %s", exc)
    max_id = 0
    while True:
        rows = client.query(
            collection_name=collection_name,
            filter=f"{id_field} >= {max_id + 1}",
            output_fields=[id_field],
            limit=max_scan,
        )
        if not rows:
            break
        batch_max = max(int(row.get(id_field, 0) or 0) for row in rows)
        if batch_max <= max_id:
            break
        max_id = batch_max
        if len(rows) < max_scan:
            break
    # 进程内单调递增：删除当前最大 id 后也不复用——否则持有旧 id 的客户端
    # 会把删改操作落到另一条新知识上；客户端重连（缓存重建）后重新起算
    with _ID_LOCK:
        per_client = _LAST_IDS.get(client)
        if per_client is None:
            per_client = {}
            _LAST_IDS[client] = per_client
        state_key = (collection_name, id_field)
        candidate = max(max_id + 1, per_client.get(state_key, 0) + 1)
        per_client[state_key] = candidate
    return candidate


def record_used_entity_id(
    client: Any,
    collection_name: str,
    last_used: int,
    id_field: str = "id",
) -> None:
    """登记一批插入实际占用的最大 id：批量插入后水位要越过整批。

    ``next_entity_id`` 只把返回的「下一个」登记为水位；一次插入 N 条时
    其余 N-1 个 id 不在水位里，删除整批后新增会复用这些已删除的 id，
    持有旧 id 的标签页 / 脚本会把改删静默落到新知识上。
    """
    with _ID_LOCK:
        per_client = _LAST_IDS.get(client)
        if per_client is None:
            per_client = {}
            _LAST_IDS[client] = per_client
        state_key = (collection_name, id_field)
        per_client[state_key] = max(per_client.get(state_key, 0), int(last_used))


def reset_milvus_client(db_path: str) -> None:
    """关闭并移除某个数据库路径的共享客户端（重连时使用）。

    关闭放在锁内：并发 reset / get 时，不会有线程在 pop 之后、close 之前
    拿到新建客户端，也不会把正在被关闭的对象再发给调用方。已经持有旧
    客户端的在途请求无法阻止（Milvus Lite 不支持事务性替换）——调用方
    失败后由 ``_ensure_connection`` 重试。
    """
    with _LOCK:
        client = _CLIENTS.pop(db_path, None)
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响重连
                pass


def shutdown_milvus_clients() -> None:
    """关闭并清空所有共享客户端（测试或进程退出前使用）。

    关闭同样放在锁内，避免与并发的 ``get_milvus_client`` 交错。注意
    milvus-lite 的本地 server / 数据库句柄不一定随 ``close()`` 释放，
    数据库文件锁可能保持到进程退出（README 已知限制）。
    """
    with _LOCK:
        clients = list(_CLIENTS.values())
        _CLIENTS.clear()
        for client in clients:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响退出
                pass
