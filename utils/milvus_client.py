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
from typing import Any

logger = logging.getLogger(__name__)

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
    from pymilvus import MilvusClient

    return MilvusClient(
        db_path,
        grpc_options={
            "keepalive_time": _GRPC_MAX_MS,
            "keepalive_timeout": "20000",
            "keepalive_permit_without_calls": "0",
            "http2_min_recv_ping_interval_without_data": _GRPC_MAX_MS,
            "http2_min_ping_interval_without_data": _GRPC_MAX_MS,
        },
    )


def get_milvus_client(db_path: str) -> Any:
    """获取（或创建）进程内共享的 Milvus Lite 客户端。"""
    with _LOCK:
        client = _CLIENTS.get(db_path)
        if client is not None:
            return client
        client = _create_client(db_path)
        _CLIENTS[db_path] = client
        return client


def peek_milvus_client(db_path: str) -> Any | None:
    """返回已缓存的共享客户端；不创建新连接。"""
    with _LOCK:
        return _CLIENTS.get(db_path)


def create_temporary_milvus_client(db_path: str) -> Any:
    """创建一个不缓存的临时客户端（调用方负责 close，避免长期占用文件锁）。"""
    return _create_client(db_path)


def reset_milvus_client(db_path: str) -> None:
    """关闭并移除某个数据库路径的共享客户端（重连时使用）。"""
    with _LOCK:
        client = _CLIENTS.pop(db_path, None)
    if client is not None:
        try:
            client.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响重连
            pass


def shutdown_milvus_clients() -> None:
    """关闭并清空所有共享客户端（测试或进程退出前使用）。"""
    with _LOCK:
        clients = list(_CLIENTS.values())
        _CLIENTS.clear()
    for client in clients:
        try:
            client.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响退出
            pass
