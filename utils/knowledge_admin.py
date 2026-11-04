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
# 导入时每次嵌入 / 插入的条数，控制大文件的峰值内存
_IMPORT_CHUNK = 500
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
    """列出知识片段（按 id 倒序，可按内容 / 元数据过滤）。

    Returns:
        {"total": int, "items": [{"id", "content", "metadata"}]}

    Raises:
        FileNotFoundError: 知识库未初始化
        Exception: Milvus 查询失败（由上层映射为 503，不能伪装成空库）
    """
    client = _client()
    _load_collection(client, COLLECTION_NAME)
    rows = client.query(
        collection_name=COLLECTION_NAME,
        filter="id >= 0",
        output_fields=["id", "content", "metadata"],
        limit=_MAX_SCAN,
    )

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
    if not knowledge_db_path().exists():
        raise FileNotFoundError("知识库未初始化")

    # 模型加载可能耗时数秒，放在锁外；锁只保护 id 生成 + 写入
    agent = _get_agent()
    with _WRITE_LOCK:
        result = agent.add_documents(
            [{"content": text, "metadata": dict(metadata or {})}]
        )
    if result.get("status") != "success":
        raise RuntimeError(str(result.get("message") or "添加知识片段失败"))
    ids = result.get("ids") or []
    total = result.get("total_count")
    return {
        "status": "success",
        "id": int(ids[-1]) if ids else None,
        "total_count": int(total) if total is not None else None,
    }


def update_document(
    doc_id: int, content: str, metadata: dict[str, str] | None = None
) -> dict[str, Any]:
    """按 id 覆盖更新知识片段（重新嵌入）；片段不存在时抛 LookupError。"""
    text = content.strip()
    if not text:
        raise ValueError("知识内容不能为空")
    client = _client()
    _load_collection(client, COLLECTION_NAME)
    agent = _get_agent()
    with _WRITE_LOCK:
        if not _exists(client, int(doc_id)):
            raise LookupError("知识片段不存在")
        result = agent.update_document(int(doc_id), text, dict(metadata or {}))
    if result.get("status") != "success":
        raise RuntimeError(str(result.get("message") or "更新知识片段失败"))
    return {"status": "success", "id": int(doc_id)}


def export_documents() -> list[dict[str, Any]]:
    """导出全部知识片段（按 id 升序，便于备份 / 迁移；不含 id）。"""
    data = list_documents(limit=_MAX_SCAN, offset=0)
    items = sorted(data["items"], key=lambda item: item["id"])
    return [
        {"content": item["content"], "metadata": item["metadata"]} for item in items
    ]


def import_documents(documents: list[dict[str, Any]]) -> dict[str, Any]:
    """批量导入知识片段；空内容 / 非法项跳过，超长字段按接口上限截断。"""
    cleaned: list[dict[str, Any]] = []
    skipped = 0
    for item in documents:
        if not isinstance(item, dict):
            skipped += 1
            continue
        content = str(item.get("content", "")).strip()
        if not content:
            skipped += 1
            continue
        metadata = item.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        cleaned.append(
            {
                "content": content[:2000],
                "metadata": {
                    str(key)[:32]: str(value)[:200]
                    for key, value in list(metadata.items())[:10]
                },
            }
        )

    if not cleaned:
        return {"imported": 0, "skipped": skipped}
    if not knowledge_db_path().exists():
        raise FileNotFoundError("知识库未初始化")

    agent = _get_agent()
    imported: list[int] = []
    with _WRITE_LOCK:
        # 去重判断放在锁内：并发导入（不同客户端 / 重试）不会都基于旧快照
        # 插入同一批内容
        existing = {
            str(item.get("content", "")).strip()
            for item in list_documents(limit=_MAX_SCAN)["items"]
        }
        unique: list[dict[str, Any]] = []
        for item in cleaned:
            if item["content"] in existing:
                skipped += 1
                continue
            existing.add(item["content"])
            unique.append(item)
        if not unique:
            return {"imported": 0, "skipped": skipped}

        # 分块嵌入 / 插入，控制大文件（CLI 导入）的峰值内存
        for start in range(0, len(unique), _IMPORT_CHUNK):
            result = agent.add_documents(unique[start : start + _IMPORT_CHUNK])
            if result.get("status") != "success":
                raise RuntimeError(str(result.get("message") or "导入知识片段失败"))
            imported.extend(int(doc_id) for doc_id in (result.get("ids") or []))
    return {"imported": len(imported), "skipped": skipped}


def delete_document(doc_id: int) -> bool:
    """按 id 删除知识片段；返回是否真的删除了记录。

    Raises:
        FileNotFoundError: 知识库未初始化
        Exception: Milvus 查询 / 删除失败（由上层映射为 503，不能误报为 404）
    """
    client = _client()
    _load_collection(client, COLLECTION_NAME)
    with _WRITE_LOCK:
        if _exists(client, int(doc_id)):
            client.delete(collection_name=COLLECTION_NAME, ids=[int(doc_id)])
            return True
        return False


def search_documents(query: str, top_k: int = 5) -> list[dict[str, Any]]:
    """语义检索知识片段（供评测与集成测试验证检索链路）。"""
    agent = _get_agent()
    return agent.search_knowledge(query, top_k=top_k)


def _exists(client: Any, doc_id: int) -> bool:
    rows = client.query(
        collection_name=COLLECTION_NAME,
        filter=f"id == {doc_id}",
        output_fields=["id"],
        limit=1,
    )
    return bool(rows)
