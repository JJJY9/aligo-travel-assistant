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
import uuid
from typing import Any

from utils.knowledge_paths import COLLECTION_NAME, knowledge_db_path
from utils.milvus_client import get_milvus_client
from utils.text_chunker import split_text

logger = logging.getLogger(__name__)

# 单次查询上限（Milvus query 的 limit 上限为 16384），本地知识库远小于该规模
_MAX_SCAN = 16384
# 导入时每次嵌入 / 插入的条数，控制大文件的峰值内存
_IMPORT_CHUNK = 500
# 单条内容超过该长度时按语义边界拆片：嵌入模型只编码前 512 token
# （约 400 个汉字），更长的尾部内容对检索完全不可见
_MAX_CONTENT_CHARS = 400
# 导入时单条内容上限（用户输入 / 任意备份的安全边界）；从导出备份还原的
# 多分片记录是完整文档，用更大的上限避免备份恢复被截断
_IMPORT_CONTENT_MAX_CHARS = 2000
_RESTORE_CONTENT_MAX_CHARS = 20000
# 元数据最多 10 个键（与导入清洗保持一致），分片标记只在放得下时添加
_METADATA_MAX_KEYS = 10
# 由拆分逻辑派生、不接受调用方回传的分片元数据
_DERIVED_CHUNK_KEYS = {"part", "parts", "group"}
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
    category: str = "",
) -> dict[str, Any]:
    """列出知识片段（按 id 倒序，可按内容 / 元数据 / 分类过滤）。

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

    wanted_category = category.strip()
    if wanted_category:
        documents = [
            doc
            for doc in documents
            if str(doc["metadata"].get("category", "")).strip() == wanted_category
        ]

    total = len(documents)
    start = max(0, int(offset))
    size = max(0, int(limit))
    return {"total": total, "items": documents[start : start + size]}


def list_categories() -> list[dict[str, Any]]:
    """统计知识片段的分类分布（供维护面板的筛选下拉框使用）。"""
    items = list_documents(limit=_MAX_SCAN, offset=0)["items"]
    counts: dict[str, int] = {}
    for item in items:
        category = str(item.get("metadata", {}).get("category", "")).strip()
        if category:
            counts[category] = counts.get(category, 0) + 1
    return [
        {"category": name, "count": count}
        for name, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    ]


def _chunk_documents(
    text: str, metadata: dict[str, str] | None
) -> list[dict[str, Any]]:
    """长内容按语义边界拆成 ≤400 字的片段（嵌入模型 512 token 截断）。

    多分片结果带 ``part`` / ``parts`` / ``group`` 元数据：group 是本次拆分的
    稳定标识，编辑 / 删除整组时靠它定位其余分片；旧数据没有 group 时仍按
    单条处理（编辑被拒绝，避免留下无法定位的旧分片）。
    """
    chunks = (
        [text]
        if len(text) <= _MAX_CONTENT_CHARS
        else [chunk for chunk in split_text(text, max_chars=_MAX_CONTENT_CHARS) if chunk.strip()]
    )
    if not chunks:
        chunks = [text]
    # 分片标记是派生字段：调用方回传的旧 part/parts/group 必须丢弃，
    # 否则编辑后会沿用旧编号 / 旧组。派生标记是内部记账字段，不受用户
    # 元数据 10 键上限约束（该上限只约束输入 / 导入），否则用户元数据
    # 占用 8 个键时分片记录会没有任何标记、面板与整组删除都无法识别。
    base_meta = {
        key: value
        for key, value in (metadata or {}).items()
        if key not in _DERIVED_CHUNK_KEYS
    }
    group_id = uuid.uuid4().hex[:12] if len(chunks) > 1 else ""
    documents: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks):
        meta = dict(base_meta)
        if group_id:
            meta["part"] = str(index + 1)
            meta["parts"] = str(len(chunks))
            meta["group"] = group_id
        documents.append({"content": chunk, "metadata": meta})
    return documents


def add_document(content: str, metadata: dict[str, str] | None = None) -> dict[str, Any]:
    """新增知识片段；长内容按语义边界拆成多条（返回最后一条 id 与分片数）。"""
    text = content.strip()
    if not text:
        raise ValueError("知识内容不能为空")
    if not knowledge_db_path().exists():
        raise FileNotFoundError("知识库未初始化")
    documents = _chunk_documents(text, metadata)

    # 模型加载可能耗时数秒，放在锁外；锁只保护 id 生成 + 写入
    agent = _get_agent()
    with _WRITE_LOCK:
        result = agent.add_documents(documents)
    if result.get("status") != "success":
        raise RuntimeError(str(result.get("message") or "添加知识片段失败"))
    ids = result.get("ids") or []
    total = result.get("total_count")
    payload: dict[str, Any] = {
        "status": "success",
        "id": int(ids[-1]) if ids else None,
        "total_count": int(total) if total is not None else None,
    }
    if len(documents) > 1:
        payload["parts"] = len(documents)
    return payload


def _document_info(client: Any, doc_id: int) -> tuple[int, str]:
    """已有片段的分片信息：(分片数, 组 id)。

    记录不存在返回 (0, "")；非多分片或旧数据没有 group 时返回 ("",)。
    """
    rows = client.query(
        collection_name=COLLECTION_NAME,
        filter=f"id == {doc_id}",
        output_fields=["metadata"],
        limit=1,
    )
    if not rows:
        return 0, ""
    meta = _parse_metadata(rows[0].get("metadata"))
    try:
        parts = max(1, int(meta.get("parts") or 1))
    except (TypeError, ValueError):
        parts = 1
    return parts, str(meta.get("group") or "") if parts > 1 else ""


def _group_member_ids(client: Any, group: str) -> list[int]:
    """同一拆分组的其它分片 id。

    单次 query 受 limit（16384）约束，且部分引擎对 offset + limit 有上限，
    CLI 导入不受限时集合可能超过该规模：按 id 区间窗口扫描（主键范围查询），
    不依赖 offset。窗口推进到超过已见最大 id 的空窗口即结束。
    """
    if not group:
        return []
    members: list[int] = []
    window = _MAX_SCAN
    start = 0
    max_seen = 0
    while True:
        rows = client.query(
            collection_name=COLLECTION_NAME,
            filter=f"id >= {start} and id < {start + window}",
            output_fields=["id", "metadata"],
            limit=window,
        )
        rows = rows or []
        if rows:
            max_seen = max(
                max_seen, max(int(row.get("id", 0) or 0) for row in rows)
            )
            for row in rows:
                meta = _parse_metadata(row.get("metadata"))
                if str(meta.get("group") or "") == group:
                    members.append(int(row.get("id", 0) or 0))
        elif start > max_seen:
            break
        start += window
    return members


def update_document(
    doc_id: int, content: str, metadata: dict[str, str] | None = None
) -> dict[str, Any]:
    """按 id 覆盖更新知识片段（重新嵌入）；长内容其余分片以新记录追加。

    新拆分（带 group 元数据）的多分片记录按「整组替换」处理：先写新分片、
    再覆盖目标分片，最后删除同组的其余旧分片。顺序保证任一前缀失败时
    旧内容仍完整可恢复（覆盖目标放在新分片写入成功之后，失败时补偿删除
    已插入的新分片）。旧数据没有 group，单条编辑仍被拒绝（请整组删除后
    重新添加）。
    """
    text = content.strip()
    if not text:
        raise ValueError("知识内容不能为空")
    client = _client()
    _load_collection(client, COLLECTION_NAME)
    agent = _get_agent()
    documents = _chunk_documents(text, metadata)
    with _WRITE_LOCK:
        existing_parts, group = _document_info(client, int(doc_id))
        if existing_parts == 0:
            raise LookupError("知识片段不存在")
        if existing_parts > 1 and not group:
            raise ValueError(
                f"该片段属于多分片知识内容（共 {existing_parts} 段），"
                "暂不支持单条编辑：请删除整组后重新添加，避免旧分片残留"
            )
        # 先写其余新分片：失败时目标分片与旧分片原样保留，可直接重试
        extra_ids: list[int] = []
        if len(documents) > 1:
            added = agent.add_documents(documents[1:])
            if added.get("status") != "success":
                raise RuntimeError(
                    "新增分片失败，原内容未修改："
                    + str(added.get("message") or "未知错误")
                )
            extra_ids = [int(item) for item in (added.get("ids") or [])]
        first = documents[0]
        try:
            result = agent.update_document(
                int(doc_id), first["content"], first["metadata"]
            )
            if result.get("status") != "success":
                raise RuntimeError(str(result.get("message") or "更新知识片段失败"))
        except Exception:
            # 补偿：删除本次已插入的其余分片，保持原记录可重试
            if extra_ids:
                try:
                    client.delete(collection_name=COLLECTION_NAME, ids=extra_ids)
                except Exception:  # noqa: BLE001 - 补偿失败不覆盖原始错误
                    logger.warning("更新失败后补偿删除新分片失败：%s", extra_ids)
            raise
        # 目标已换成新内容后再删旧分片（旧组仅此时失去定位目标）
        removed_ids: list[int] = []
        if group:
            removed_ids = [
                member
                for member in _group_member_ids(client, group)
                if member != int(doc_id)
            ]
            if removed_ids:
                client.delete(collection_name=COLLECTION_NAME, ids=removed_ids)
    payload: dict[str, Any] = {"status": "success", "id": int(doc_id)}
    if len(documents) > 1:
        payload["parts"] = len(documents)
        payload["extra_ids"] = extra_ids
    if removed_ids:
        payload["removed_ids"] = removed_ids
    return payload


def export_documents() -> list[dict[str, Any]]:
    """导出全部知识片段（按 id 升序，便于备份 / 迁移；不含 id）。"""
    data = list_documents(limit=_MAX_SCAN, offset=0)
    items = sorted(data["items"], key=lambda item: item["id"])
    return [
        {"content": item["content"], "metadata": item["metadata"]} for item in items
    ]


def _regroup_exported_documents(
    documents: list[Any], existing: set[str]
) -> tuple[list[Any], int]:
    """把导出备份里同一 group 的分片按 part 顺序拼回整篇文档。

    导出文件包含 part/parts/group 元数据；逐条导入会把一篇文档拆成互不
    相关的单条记录（单条编辑、整组删除都无法识别）。这里先合并回整篇，
    再由 ``_chunk_documents`` 重新拆分并生成新的组标识。

    整组的每个分片都已存在于库中时整组跳过（重复导入同一备份保持幂等）；
    合并后的记录带 ``__restored_group__`` 标记，导入时使用更大的内容上限。
    返回 (合并后的文档列表, 因整组已存在而跳过的分片数)。
    """
    grouped: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    order: list[tuple[str, Any]] = []
    for item in documents:
        if not isinstance(item, dict):
            order.append(("single", item))
            continue
        metadata = item.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        group = str(metadata.get("group") or "")
        try:
            parts = int(metadata.get("parts") or 1)
        except (TypeError, ValueError):
            parts = 1
        if group and parts > 1:
            try:
                part_no = int(metadata.get("part") or 0)
            except (TypeError, ValueError):
                part_no = 0
            grouped.setdefault(group, []).append((part_no, item))
            if not any(kind == "group" and key == group for kind, key in order):
                order.append(("group", group))
        else:
            order.append(("single", item))

    merged: list[Any] = []
    skipped = 0
    for kind, key in order:
        if kind == "single":
            merged.append(key)
            continue
        pieces = sorted(grouped.get(key, []), key=lambda pair: pair[0])
        contents: list[str] = []
        base_meta: dict[str, Any] = {}
        for index, (_, piece) in enumerate(pieces):
            metadata = piece.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            content = piece.get("content")
            if isinstance(content, str):
                contents.append(content)
            if index == 0:
                base_meta = {
                    meta_key: meta_value
                    for meta_key, meta_value in metadata.items()
                    if meta_key not in _DERIVED_CHUNK_KEYS
                }
        if not contents:
            continue
        if all(stripped in existing for stripped in (c.strip() for c in contents)):
            # 整组已在库中（导出后原样恢复）：整组跳过，不重新拆分
            skipped += len(contents)
            continue
        merged.append(
            {
                "__restored_group__": True,
                "content": "\n".join(contents),
                "metadata": base_meta,
            }
        )
    return merged, skipped


def import_documents(documents: list[dict[str, Any]]) -> dict[str, Any]:
    """批量导入知识片段；空内容 / 非法项跳过，超长字段按接口上限截断。

    导出备份中的多分片记录先按 group 拼回整篇再重新拆分；整组已完整存在
    于库中时整组跳过（重复导入同一备份保持幂等，不会因重新拆分产生重复）。
    """
    items = list(documents)
    if not items:
        return {"imported": 0, "skipped": 0}
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
        merged_items, skipped = _regroup_exported_documents(items, existing)
        cleaned: list[dict[str, Any]] = []
        for item in merged_items:
            if not isinstance(item, dict):
                skipped += 1
                continue
            restored = bool(item.pop("__restored_group__", False))
            raw_content = item.get("content")
            # 只接受字符串内容：JSON null / false / 0 不能变成 "None" / "False" / "0"
            if not isinstance(raw_content, str):
                skipped += 1
                continue
            content = raw_content.strip()
            if not content:
                skipped += 1
                continue
            metadata = item.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            sanitized_meta = {
                str(key)[:32]: str(value)[:200]
                for key, value in list(metadata.items())[:_METADATA_MAX_KEYS]
            }
            # 长内容拆成 ≤400 字的片段：每段都会被嵌入，尾部内容不再不可检索
            limit = (
                _RESTORE_CONTENT_MAX_CHARS if restored else _IMPORT_CONTENT_MAX_CHARS
            )
            cleaned.extend(_chunk_documents(content[:limit], sanitized_meta))

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
            batch_no = start // _IMPORT_CHUNK + 1
            try:
                result = agent.add_documents(unique[start : start + _IMPORT_CHUNK])
            except Exception as exc:  # noqa: BLE001 - 保留已导入数量供上层展示
                raise RuntimeError(
                    f"第 {batch_no} 批导入失败（已导入 {len(imported)} 条）：{exc}"
                ) from exc
            if result.get("status") != "success":
                raise RuntimeError(
                    f"第 {batch_no} 批导入失败（已导入 {len(imported)} 条）："
                    f"{result.get('message') or '导入知识片段失败'}"
                )
            imported.extend(int(doc_id) for doc_id in (result.get("ids") or []))
    return {"imported": len(imported), "skipped": skipped}


def delete_document(doc_id: int, whole_group: bool = False) -> int:
    """按 id 删除知识片段；返回实际删除的分片数（不存在返回 0）。

    ``whole_group=True`` 时，如果该片段属于带 group 标识的多分片记录，
    同时删除同组的其余分片（旧数据没有 group 时只删除当前片段，返回 1）。

    Raises:
        FileNotFoundError: 知识库未初始化
        Exception: Milvus 查询 / 删除失败（由上层映射为 503，不能误报为 404）
    """
    client = _client()
    _load_collection(client, COLLECTION_NAME)
    with _WRITE_LOCK:
        if not _exists(client, int(doc_id)):
            return 0
        ids = [int(doc_id)]
        if whole_group:
            _, group = _document_info(client, int(doc_id))
            if group:
                ids = sorted(set(_group_member_ids(client, group)) | {int(doc_id)})
        client.delete(collection_name=COLLECTION_NAME, ids=ids)
        return len(ids)


def search_documents(query: str, top_k: int = 5) -> list[dict[str, Any]]:
    """语义检索知识片段（供 Web 维护面板、评测与集成测试验证检索链路）。

    Raises:
        FileNotFoundError: 知识库未初始化
        Exception: 嵌入 / Milvus 检索失败（由上层映射为 503，不能伪装成无结果）
    """
    # RAG Agent 构造时会「按需」新建空数据库：未初始化的知识库必须先拒绝，
    # 否则一次语义检索会把缺失的知识库悄悄建成空库并返回 200 无结果
    if not knowledge_db_path().exists():
        raise FileNotFoundError("知识库未初始化")
    agent = _get_agent()
    return agent.search_knowledge(query, top_k=top_k, strict=True)


def _exists(client: Any, doc_id: int) -> bool:
    rows = client.query(
        collection_name=COLLECTION_NAME,
        filter=f"id == {doc_id}",
        output_fields=["id"],
        limit=1,
    )
    return bool(rows)
