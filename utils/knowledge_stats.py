"""
知识库状态（Milvus Lite 集合统计）

用于 Web / CLI 展示当前 RAG 知识库是否就绪、集合名称与片段数量。
只读取集合统计，不加载 embedding 模型；客户端与知识库维护接口共享
（Milvus Lite 的 data-dir 锁在整个进程生命周期内保持，见 README 已知限制）。
"""
import contextlib
import logging
from pathlib import Path
from typing import Any

from utils.knowledge_paths import COLLECTION_NAME, default_knowledge_dir
from utils.milvus_client import get_milvus_client

logger = logging.getLogger(__name__)

DEFAULT_COLLECTION = COLLECTION_NAME


def default_db_path() -> Path:
    """返回默认知识库数据库路径（与 RAG Skill 使用同一解析逻辑）。"""
    return default_knowledge_dir() / "milvus_lite.db"


def _collection_dimension(client: Any, collection: str) -> int | None:
    """集合向量字段的维度；不可读取（旧客户端 / 异常）返回 None。"""
    try:
        description = client.describe_collection(collection)
        for field in description.get("fields") or []:
            params = field.get("params") or {}
            if "dim" in params:
                return int(params["dim"])
    except Exception as exc:  # noqa: BLE001 - 维度探测失败不影响状态主体
        logger.debug("读取集合维度失败: %s", exc)
    return None


def _loaded_embedding_dim() -> int | None:
    """当前进程已加载嵌入模型的维度（不触发加载）。"""
    try:
        from utils.embedding_model import peek_embedding_dim

        return peek_embedding_dim()
    except Exception:  # noqa: BLE001 - 可选依赖缺失时按未知处理
        return None


def knowledge_status(
    db_path: str | Path | None = None,
    collection: str = DEFAULT_COLLECTION,
    include_path: bool = True,
    detail_errors: bool = True,
    lock_timeout: float | None = None,
) -> dict[str, Any]:
    """返回知识库状态：是否可用、集合名、片段数与数据库路径。

    Web 调用方传 ``include_path=False``（不泄露本机绝对路径）、
    ``detail_errors=False``（错误只进日志）、``lock_timeout``（导入持写锁
    期间快速返回「正在写入」而不是把请求线程堆在锁上）。
    """
    db = Path(db_path) if db_path is not None else default_db_path()
    base: dict[str, Any] = {"collection": collection}
    if include_path:
        base["path"] = str(db)

    # Milvus Lite 2.x 为单文件，3.x 为目录，两者都视为已初始化
    if not db.exists():
        return {**base, "available": False, "reason": "知识库未初始化"}

    client = None
    # 只读快照用共享读锁：此前用独占写锁，纯读并发（面板 chip + 列表 +
    # 状态）会互相排队，且在写者优先下新读者会被判成「正在写入」。读锁
    # 仍与整组替换的多步写入互斥，读到中间行数的问题不受影响
    read_lock_factory: Any = None
    busy_error: type[Exception] = RuntimeError
    lock_ctx: Any = contextlib.nullcontext()
    try:
        from utils.knowledge_admin import (
            KnowledgeBusyError,
            _WRITE_LOCK,
            _read_lock,
        )

        lock_ctx = _WRITE_LOCK
        read_lock_factory = _read_lock
        busy_error = KnowledgeBusyError
    except Exception:  # noqa: BLE001 - 维护模块不可用时保持原行为
        lock_ctx = contextlib.nullcontext()
    def _snapshot(client: Any) -> dict[str, Any]:
        if not client.has_collection(collection):
            return {**base, "available": False, "reason": "集合不存在"}
        stats = client.get_collection_stats(collection)
        dimension = _collection_dimension(client, collection)
        payload = {
            **base,
            "available": True,
            "documents": int(stats.get("row_count", 0) or 0),
        }
        if dimension is not None:
            payload["dimension"] = dimension
        expected = _loaded_embedding_dim()
        if expected is not None and dimension is not None:
            payload["dimension_mismatch"] = int(dimension) != int(expected)
        return payload

    busy_payload = {
        **base,
        "available": False,
        "reason": "知识库正在写入（导入进行中），请稍后刷新",
    }
    try:
        client = get_milvus_client(str(db))
        if read_lock_factory is not None:
            try:
                lock = (
                    read_lock_factory(lock_timeout)
                    if lock_timeout is not None
                    else read_lock_factory()
                )
                with lock:
                    return _snapshot(client)
            except busy_error:
                return busy_payload
        # 旧版本维护模块（无读锁）：退回独占加锁
        if lock_timeout is not None:
            acquired = lock_ctx.acquire(timeout=lock_timeout)
            if not acquired:
                return busy_payload
            try:
                return _snapshot(client)
            finally:
                lock_ctx.release()
        with lock_ctx:
            return _snapshot(client)
    except Exception as exc:  # noqa: BLE001 - 状态查询失败不影响主流程
        logger.warning("获取知识库状态失败: %s", exc)
        reason = str(exc) if detail_errors else "状态查询失败，请稍后再试（详见服务端日志）"
        return {**base, "available": False, "reason": reason}
