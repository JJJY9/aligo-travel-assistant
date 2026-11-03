"""
知识库内容管理（Web 维护面板使用）

提供知识片段的列表 / 新增 / 删除。列表只读 Milvus 集合，不加载嵌入模型；
新增需要向量化，懒加载并复用 RAGKnowledgeAgent（首次调用会加载嵌入模型）。
仅面向本地单机部署：所有写操作串行执行，避免并发插入产生相同主键。
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any

from utils.knowledge_paths import COLLECTION_NAME, knowledge_db_path
from utils.milvus_client import get_milvus_client

logger = logging.getLogger(__name__)

# 单次查询上限（Milvus query 的 limit 上限为 16384），本地知识库远小于该规模
_MAX_SCAN = 16384
_WRITE_LOCK = threading.Lock()
_agent: Any = None
_agent_lock = threading.Lock()


def reset_knowledge_agent() -> None:
    """丢弃缓存的 RAG Agent（测试或知识库重建后使用）。"""
    global _agent
    with _agent_lock:
        _agent = None


def _get_agent() -> Any:
    """懒加载并复用 RAGKnowledgeAgent。"""
    global _agent
    if _agent is not None:
        return _agent
    with _agent_lock:
        if _agent is None:
            from agents.rag_knowledge_agent import RAGKnowledgeAgent

            _agent = RAGKnowledgeAgent(collection_name=COLLECTION_NAME)
    return _agent


def _client() -> Any:
    db = knowledge_db_path()
    if not db.exists():
        raise FileNotFoundError("知识库未初始化")
    return get_milvus_client(str(db))


def _load_collection(client: Any, collection: str) -> None:
    try:
        client.load_collection(collection)
    except Exception as exc:  # noqa: BLE001 - 2.x 无该调用 / 已加载
        logger.debug("load_collection skipped: %s", exc)


def _parse_metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def list_documents(
    limit: int = 20,
    offset: int = 0,
    query: str = "",
) -> dict[str, Any]:
    """列出知识片段（按 id 升序，可按内容 / 元数据过滤）。

    Returns:
        {"total": int, "items": [{"id", "content", "metadata"}]}
    """
    client = _client()
    _load_collection(client, COLLECTION_NAME)
    try:
        rows = client.query(
            collection_name=COLLECTION_NAME,
            filter="id >= 0",
            output_fields=["id", "content", "metadata"],
            limit=_MAX_SCAN,
        )
    except Exception as exc:  # noqa: BLE001 - 空集合等场景按无数据处理
        logger.debug("知识库查询失败: %s", exc)
        rows = []

    documents: list[dict[str, Any]] = [
        {
            "id": int(row.get("id", 0) or 0),
            "content": str(row.get("content", "")),
            "metadata": _parse_metadata(row.get("metadata")),
        }
        for row in rows or []
    ]
    documents.sort(key=lambda item: item["id"], reverse=True)

    needle = query.strip().lower()
    if needle:
        documents = [
            doc
            for doc in documents
            if needle in doc["content"].lower()
            or needle in json.dumps(doc["metadata"], ensure_ascii=False).lower()
        ]

    total = len(documents)
    start = max(0, int(offset))
    size = max(0, int(limit))
    return {"total": total, "items": documents[start : start + size]}


def add_document(content: str, metadata: dict[str, str] | None = None) -> dict[str, Any]:
    """新增一个知识片段，返回新片段 id。"""
    text = content.strip()
    if not text:
        raise ValueError("知识内容不能为空")

    with _WRITE_LOCK:
        agent = _get_agent()
        result = agent.add_documents(
            [{"content": text, "metadata": dict(metadata or {})}]
        )
    if result.get("status") != "success":
        raise RuntimeError(str(result.get("message") or "添加知识片段失败"))
    ids = result.get("ids") or []
    return {
        "status": "success",
        "id": int(ids[-1]) if ids else None,
        "total_count": int(result.get("total_count") or 0),
    }


def delete_document(doc_id: int) -> bool:
    """按 id 删除知识片段；返回是否真的删除了记录。"""
    client = _client()
    _load_collection(client, COLLECTION_NAME)
    with _WRITE_LOCK:
        if _exists(client, int(doc_id)):
            client.delete(collection_name=COLLECTION_NAME, ids=[int(doc_id)])
            return True
        return False


def _exists(client: Any, doc_id: int) -> bool:
    try:
        rows = client.query(
            collection_name=COLLECTION_NAME,
            filter=f"id == {doc_id}",
            output_fields=["id"],
            limit=1,
        )
    except Exception:  # noqa: BLE001 - 查询失败按不存在处理
        return False
    return bool(rows)
