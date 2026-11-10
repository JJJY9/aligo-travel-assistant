"""
知识库内容管理（Web 维护面板使用）

提供知识片段的列表 / 新增 / 删除。列表只读 Milvus 集合，不加载嵌入模型；
新增需要向量化，懒加载并复用 RAGKnowledgeAgent（首次调用会加载嵌入模型）。
仅面向本地单机部署：所有写操作串行执行，避免并发插入产生相同主键。
"""
from __future__ import annotations

import contextlib
import json
import logging
import threading
import uuid
from collections import Counter
from collections.abc import Iterable
from typing import Any

from utils.knowledge_paths import COLLECTION_NAME, knowledge_db_path
from utils.milvus_client import get_milvus_client
from utils.text_chunker import split_text

logger = logging.getLogger(__name__)

# 导入时每次嵌入 / 插入的条数，控制大文件的峰值内存
# （全量扫描 query_iterator 也用它作为每批行数）
_IMPORT_CHUNK = 500
# 单条内容超过该长度时按语义边界拆片：嵌入模型只编码前 512 token
# （约 400 个汉字），更长的尾部内容对检索完全不可见
_MAX_CONTENT_CHARS = 400
# 文档大小上限（各分片内容长度之和）。回填 / 还原时按分片拼接会插入分隔符，
# 因此允许的拼接文本长度略高（每 400 字一片，20000 字最多 50 片）
DOC_MAX_CHARS = 20000
JOINED_MAX_CHARS = DOC_MAX_CHARS + 100
# 导入备份时的拼接上限：旧数据可能有很多极短的分片（每片 1 字时 20000 字
# 文档拼接后接近 40000 字），按分片数放宽；文档大小仍以 DOC_MAX_CHARS 为准，
# 该上限只拦住用空白撑大拼接文本的异常备份
_RESTORE_JOINED_MAX_CHARS = DOC_MAX_CHARS * 2 + 100
# 分片数硬上限：parts 声明来自备份元数据，无上限时 set(range(1, parts+1))
# 会被 1e12 之类的声明直接撑爆内存（恶意 / 损坏备份即可触发）；旧备份
# 可能有上千个极短分片（每片几个字），上限取 10 万留足正常空间
_GROUP_PARTS_MAX = 100000
# 导入时单条内容上限（用户输入 / 任意备份的安全边界）；从导出备份还原的
# 多分片记录是完整文档，用上面的拼接上限避免备份恢复被截断
_IMPORT_CONTENT_MAX_CHARS = 2000
# 元数据最多 10 个键（与导入清洗保持一致），分片标记只在放得下时添加
_METADATA_MAX_KEYS = 10
# 元数据键 / 值长度上限（与 Web 接口 schema 同一口径）。CLI 与导入此前
# 不受限制：写进去的超长 category 在面板按分类筛选时撞查询参数
# max_length=200 → 422，该分类下的知识永远无法筛选；备份往返也会被截断
METADATA_KEY_MAX_CHARS = 32
METADATA_VALUE_MAX_CHARS = 200
# 由拆分逻辑派生、不接受调用方回传的分片元数据
_DERIVED_CHUNK_KEYS = {"part", "parts", "group"}
_WRITE_LOCK = threading.Lock()
_agent: Any = None
# 检索路径专用的只读 Agent（create_if_missing=False）：语义检索不会在
# 「库存在但集合缺失」时静默创建空集合、把未初始化伪装成 0 结果
_read_agent: Any = None
_agent_lock = threading.Lock()


def reset_knowledge_agent() -> None:
    """丢弃缓存的 RAG Agent（测试或知识库重建后使用）。"""
    global _agent, _read_agent
    with _agent_lock:
        _agent = None
        _read_agent = None


def _get_agent() -> Any:
    """懒加载并复用 RAGKnowledgeAgent。"""
    global _agent
    if _agent is not None:
        return _agent
    with _agent_lock:
        if _agent is None:
            from agents.rag_knowledge_agent import RAGKnowledgeAgent

            # 写入 / 管理路径负责创建知识库；检索路径默认不创建空库
            _agent = RAGKnowledgeAgent(
                collection_name=COLLECTION_NAME, create_if_missing=True
            )
    return _agent


def _get_read_agent() -> Any:
    """只读检索用的 Agent：不创建空库 / 空集合。"""
    global _read_agent
    if _read_agent is not None:
        return _read_agent
    with _agent_lock:
        if _read_agent is not None:
            return _read_agent
        from agents.rag_knowledge_agent import RAGKnowledgeAgent

        agent = RAGKnowledgeAgent(
            collection_name=COLLECTION_NAME, create_if_missing=False
        )
        # initialized=False（集合缺失 / 依赖未装）的实例不能缓存：用户随后
        # 通过面板导入或初始化脚本补齐集合后，检索应自动恢复而不是永久
        # 报「知识库缺少集合」直到进程重启（LazyAgentRegistry 同款守卫）
        if getattr(agent, "initialized", True) is not False:
            _read_agent = agent
        return agent


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

    全量扫描（``query_iterator``）后在内存中排序 / 过滤 / 分页：单次 query
    受 16384 条上限约束，直接用 offset 翻页在更大的集合上会翻不到后面的行。
    扫描与写锁互斥：整组替换分多步写库，读到中间状态会出现「只看到部分
    分片」的列表；单机部署下让读等待写完成即可。

    Returns:
        {"total": int, "items": [{"id", "content", "metadata"}]}

    Raises:
        FileNotFoundError: 知识库未初始化
        Exception: Milvus 查询失败（由上层映射为 503，不能伪装成空库）
    """
    # 集合加载（首次冷启动可能耗时数百毫秒到数秒）放在写锁外：
    # Milvus Lite 的 load_collection 会阻塞等待加载完成，不应占用写锁
    client = _client()
    _load_collection(client, COLLECTION_NAME)
    with _WRITE_LOCK:
        return _list_documents(client, limit, offset, query, category)


def _list_documents(
    client: Any, limit: int, offset: int, query: str, category: str
) -> dict[str, Any]:
    documents: list[dict[str, Any]] = [
        {
            "id": int(row.get("id", 0) or 0),
            "content": str(row.get("content", "")),
            "metadata": _parse_metadata(row.get("metadata")),
        }
        for row in _iter_all_rows(client)
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
    # 分类统计要覆盖全部片段（列表已全量扫描，这里只是不设分页上限）
    items = list_documents(limit=10**9, offset=0)["items"]
    counts: dict[str, int] = {}
    for item in items:
        category = str(item.get("metadata", {}).get("category", "")).strip()
        if category:
            counts[category] = counts.get(category, 0) + 1
    return [
        {"category": name, "count": count}
        for name, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    ]


def _restore_pieces(parts: Any, fallback: str) -> list[str]:
    """还原分片：优先使用备份里的原始分片（保持边界），否则按拼接文本重切。

    分片数量超过单批上限时，无分隔合并相邻分片（内容不变）以减少批内条目；
    单个分片超过 400 字（旧数据）按语义边界切开。
    """
    raw: list[str] = []
    if isinstance(parts, list):
        raw = [
            str(piece).strip()
            for piece in parts
            if isinstance(piece, str) and str(piece).strip()
        ]
    if not raw:
        text = fallback.strip()
        if not text:
            return []
        if len(text) <= _MAX_CONTENT_CHARS:
            return [text]
        return [
            piece.strip()
            for piece in split_text(text, _MAX_CONTENT_CHARS)
            if piece.strip()
        ]
    pieces: list[str] = []
    for piece in raw:
        if len(piece) <= _MAX_CONTENT_CHARS:
            pieces.append(piece)
        else:
            pieces.extend(
                sub.strip()
                for sub in split_text(piece, _MAX_CONTENT_CHARS)
                if sub.strip()
            )
    if len(pieces) > _IMPORT_CHUNK:
        merged: list[str] = []
        buffer = ""
        for piece in pieces:
            if buffer and len(buffer) + len(piece) > _MAX_CONTENT_CHARS:
                merged.append(buffer)
                buffer = piece
            else:
                buffer += piece
        if buffer:
            merged.append(buffer)
        pieces = merged
    return pieces


def _validate_metadata_lengths(metadata: dict[str, Any]) -> None:
    """元数据键 / 值长度与 Web 接口共享同一上限，超长直接拒绝（不静默截断）。"""
    for meta_key, meta_value in metadata.items():
        key_text = str(meta_key)
        if len(key_text) > METADATA_KEY_MAX_CHARS:
            raise ValueError(
                f"元数据键不能超过 {METADATA_KEY_MAX_CHARS} 字：{key_text[:20]}…"
            )
        if len(str(meta_value)) > METADATA_VALUE_MAX_CHARS:
            raise ValueError(
                f"元数据值不能超过 {METADATA_VALUE_MAX_CHARS} 字（键 {key_text}）"
            )


def _chunk_documents(
    text: str,
    metadata: dict[str, str] | None,
    group_override: str = "",
) -> list[dict[str, Any]]:
    """长内容按语义边界拆成 ≤400 字的片段（嵌入模型 512 token 截断）。

    多分片结果带 ``part`` / ``parts`` / ``group`` 元数据：group 是本次拆分的
    稳定标识，编辑 / 删除整组时靠它定位其余分片；旧数据没有 group 时仍按
    单条处理（编辑被拒绝，避免留下无法定位的旧分片）。

    分片一律 strip：备份还原先把分片用换行拼回整篇再重新切分，切分残留
    的行首 / 行尾换行若原样入库，会把文档撑过 20000 字上限（导出→导入
    不再闭合）。``group_override`` 供备份还原沿用原组号，重试补齐残缺组
    时能按内容与编号续写。
    """
    chunks = (
        [text.strip()]
        if len(text) <= _MAX_CONTENT_CHARS
        else [
            chunk.strip()
            for chunk in split_text(text, max_chars=_MAX_CONTENT_CHARS)
            if chunk.strip()
        ]
    )
    if not chunks:
        chunks = [text.strip()]
    # 分片标记是派生字段：调用方回传的旧 part/parts/group 必须丢弃，
    # 否则编辑后会沿用旧编号 / 旧组。派生标记是内部记账字段，不受用户
    # 元数据 10 键上限约束（该上限只约束输入 / 导入），否则用户元数据
    # 占用 8 个键时分片记录会没有任何标记、面板与整组删除都无法识别。
    base_meta = {
        key: value
        for key, value in (metadata or {}).items()
        if key not in _DERIVED_CHUNK_KEYS
    }
    _validate_metadata_lengths(base_meta)
    group_id = (
        (group_override or uuid.uuid4().hex[:12]) if len(chunks) > 1 else ""
    )
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
    if len(text) > DOC_MAX_CHARS:
        # Web / CLI / 导入共用同一文档大小上限：超长内容按 400 字拆片后
        # 会变成数百次嵌入调用，必须让调用方先拆分
        raise ValueError(
            f"知识内容超过上限（{DOC_MAX_CHARS} 字），请拆分后分多次添加"
        )
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


def _iter_all_rows(client: Any):
    """遍历集合的全部行。

    单次 query 受 limit（16384）与 offset 组合上限约束；query_iterator 由
    引擎分批下发，集合超过单页规模时也不会漏行（CLI 导入不受条数限制）。
    """
    iterator = client.query_iterator(
        collection_name=COLLECTION_NAME,
        filter="id >= 0",
        batch_size=_IMPORT_CHUNK,
        output_fields=["id", "content", "metadata"],
    )
    try:
        while True:
            batch = iterator.next()
            if not batch:
                break
            yield from batch
    finally:
        close = getattr(iterator, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()


def _group_member_ids(client: Any, group: str) -> list[int]:
    """同一拆分组的其它分片 id（遍历全部行，不受单页 16384 条限制）。"""
    if not group:
        return []
    members: list[int] = []
    for row in _iter_all_rows(client):
        meta = _parse_metadata(row.get("metadata"))
        if str(meta.get("group") or "") == group:
            members.append(int(row.get("id", 0) or 0))
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
    if len(text) > JOINED_MAX_CHARS:
        # 整份编辑提交的是分片空串拼接文本（与 get_document_group 同口径）：
        # 留 100 字余量给边界场景，实际入库大小由下方 stored_size 校验
        raise ValueError(
            f"知识内容超过上限（{DOC_MAX_CHARS} 字，整份编辑最多 "
            f"{JOINED_MAX_CHARS} 字），请拆分后重试"
        )
    client = _client()
    _load_collection(client, COLLECTION_NAME)
    agent = _get_agent()
    documents = _chunk_documents(text, metadata)
    stored_size = sum(len(document["content"]) for document in documents)
    if stored_size > DOC_MAX_CHARS:
        # 提交文本含分片分隔符时可能略超上限，但重新分片后的实际入库总长
        # 才是文档大小：原样保存一次不能再把文档撑过上限
        raise ValueError(
            f"重新分片后的文档大小超过上限（{stored_size} 字 > {DOC_MAX_CHARS}），"
            "请拆分后重试"
        )
    with _WRITE_LOCK:
        existing_parts, group = _document_info(client, int(doc_id))
        if existing_parts == 0:
            raise LookupError("知识片段不存在")
        if existing_parts > 1 and not group:
            raise ValueError(
                f"该片段属于多分片知识内容（共 {existing_parts} 段），"
                "暂不支持单条编辑：请删除整组后重新添加，避免旧分片残留"
            )
        # 旧组成员在写入前扫描：扫描失败时还没有插入任何新分片，不会留下
        # 无法补偿的孤儿新 group（此前覆盖目标后才扫描，失败即残留）
        old_ids: list[int] = []
        if group:
            old_ids = [
                member
                for member in _group_member_ids(client, group)
                if member != int(doc_id)
            ]
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
        except Exception as original:
            # 补偿：删除本次已插入的其余分片，保持原记录可重试。逐条删除，
            # 失败项随异常一起报出（只写日志会让孤儿分片参与检索且无主可查）
            failed_compensation: list[int] = []
            for item_id in extra_ids:
                try:
                    client.delete(collection_name=COLLECTION_NAME, ids=[item_id])
                except Exception as exc:  # noqa: BLE001 - 汇总后随原始错误抛出
                    logger.warning("更新失败后补偿删除分片 %s 失败：%s", item_id, exc)
                    failed_compensation.append(item_id)
            if failed_compensation:
                raise RuntimeError(
                    "更新失败且补偿删除未完成，残留分片 "
                    f"{failed_compensation}：请稍后重试，或运行 "
                    f"`python cli.py kb-delete <id> --group` 删除整组后重新添加"
                    "（kb-dedupe 不处理组内分片）"
                ) from original
            raise
        # 目标已换成新内容后再删旧分片（旧组仅此时失去定位目标）。
        # 逐条删除：某一片失败不应连累其余旧分片，失败项在响应里报告，
        # 调用方可按 leftover_ids 重试清理
        removed_ids: list[int] = []
        leftover_ids: list[int] = []
        if group:
            for member in old_ids:
                try:
                    client.delete(collection_name=COLLECTION_NAME, ids=[member])
                except Exception as exc:  # noqa: BLE001 - 保留其余分片继续清理
                    logger.warning("删除旧分片 %s 失败：%s", member, exc)
                    leftover_ids.append(member)
                else:
                    removed_ids.append(member)
    payload: dict[str, Any] = {"status": "success", "id": int(doc_id)}
    if len(documents) > 1:
        payload["parts"] = len(documents)
        payload["extra_ids"] = extra_ids
    if removed_ids:
        payload["removed_ids"] = removed_ids
    if leftover_ids:
        payload["leftover_ids"] = leftover_ids
    return payload


def export_documents() -> list[dict[str, Any]]:
    """导出全部知识片段（按 id 升序，便于备份 / 迁移；不含 id）。

    全量扫描（query_iterator），集合超过单页 16384 条时也不会截断。
    与写锁互斥：避免导到整组替换的中间状态（备份里分片缺失或新旧混杂）。
    """
    # 集合加载放在写锁外（load_collection 会阻塞等待加载完成）
    client = _client()
    _load_collection(client, COLLECTION_NAME)
    with _WRITE_LOCK:
        return _export_documents(client)


def _export_documents(client: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = [
        {
            "id": int(row.get("id", 0) or 0),
            "content": str(row.get("content", "")),
            "metadata": _parse_metadata(row.get("metadata")),
        }
        for row in _iter_all_rows(client)
    ]
    items.sort(key=lambda item: item["id"])
    return [
        {"content": item["content"], "metadata": item["metadata"]} for item in items
    ]


def _merge_exported_group(
    key: str, pieces: list[tuple[int, dict[str, Any]]], existing_counts: Counter[str]
) -> tuple[dict[str, Any] | None, int]:
    """合并一个 group 的分片；返回 (合并后的记录, 因整组已存在而跳过的分片数)。

    ``None`` 表示整组已在库中（不导入）。结构非法的备份（parts 声明异常 /
    part 编号重复 / 分片不全）抛 ValueError，由调用方决定整体报错还是
    按组降级跳过。
    """
    declared = 0
    contents: list[str] = []
    seen_parts: set[int] = set()
    base_meta: dict[str, Any] = {}
    for index, (_, piece) in enumerate(pieces):
        metadata = piece.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        try:
            part_no = int(metadata.get("part") or 0)
            declared = max(declared, int(metadata.get("parts") or 0))
        except (TypeError, ValueError, OverflowError):
            part_no = 0
        if declared > _GROUP_PARTS_MAX:
            raise ValueError(
                f"备份中的多分片文档 parts 声明异常（group={key}，"
                f"parts={declared}）：请检查备份来源"
            )
        if part_no:
            if part_no in seen_parts:
                # 重复 part 编号会把同一段内容叠进正文（内容变多、丢片）
                raise ValueError(
                    f"备份中的多分片文档 part 编号重复（group={key}，part={part_no}）："
                    "请检查导出源后重新导出"
                )
            seen_parts.add(part_no)
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
        return None, 0
    if declared > 1:
        missing = set(range(1, declared + 1)) - seen_parts
        if len(pieces) < declared or missing:
            # 分片不全 / 编号跳号：按残片重组会把一篇文档拆成几段分别导入
            detail = (
                "，缺少 part "
                + "、".join(str(item) for item in sorted(missing))
                if missing
                else ""
            )
            raise ValueError(
                f"备份中的多分片文档不完整（group={key}，收到 {len(pieces)}/{declared} 片{detail}）："
                "请整组一起导入"
            )
    stripped_contents = [content.strip() for content in contents]
    # 多重集判定：一组里两片同文时，库里只存在一份不能算「整组已存在」
    # （Counter 相减只保留正差值：组内多重集必须是库内容多重集的子集）
    if not (Counter(stripped_contents) - existing_counts):
        # 整组已在库中（导出后原样恢复）：整组跳过，不重新拆分
        return None, len(contents)
    return (
        {
            # 沿用原组号：重试补齐残缺组时能按内容续写而不会产生第二份文档
            "__restored_group__": key,
            # 还原上限按 strip 后的分片长度之和判断：分片入库前会 strip，
            # 空白不会进入正文，按含空白长度计会误伤旧备份 / 被空白绕过
            "__restored_size__": sum(len(text) for text in stripped_contents),
            # 原始分片直接作为入库分片：避免「拼回整篇再重切」改变分片
            # 边界（会让残缺组补齐时按内容对不上、产生重复文档）
            "__restored_parts__": stripped_contents,
            # 空串拼接：分片是原文的连续片段，拼接即原文；用换行分隔会把
            # 分隔符写进正文（文档越还原越大、导出→导入不再闭合）
            "content": "".join(stripped_contents),
            "metadata": base_meta,
        },
        0,
    )


def _regroup_exported_documents(
    documents: list[Any],
    existing: Iterable[str],
    problems: "list[dict[str, Any]] | None" = None,
) -> tuple[list[Any], int]:
    """把导出备份里同一 group 的分片按 part 顺序拼回整篇文档。

    导出文件包含 part/parts/group 元数据；逐条导入会把一篇文档拆成互不
    相关的单条记录（单条编辑、整组删除都无法识别）。这里先合并回整篇，
    再由 ``_chunk_documents`` 重新拆分并生成新的组标识。

    整组的每个分片都已存在于库中时整组跳过（重复导入同一备份保持幂等）；
    合并后的记录带 ``__restored_group__`` 标记，导入时使用更大的内容上限。
    返回 (合并后的文档列表, 因整组已存在而跳过的分片数)。

    ``problems`` 不为 None 时按组降级：结构非法的组（声明异常 / 重复
    part / 分片不全）跳过并记入 problems（含组号、原因与片数），其它组
    照常导入——一个坏组不能让整份备份永远无法恢复；为 None 时保持原
    语义直接抛 ValueError（供严格调用方使用）。
    """
    grouped: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    order: list[tuple[str, Any]] = []
    ordered_groups: set[str] = set()
    for item in documents:
        if not isinstance(item, dict):
            order.append(("single", item))
            continue
        metadata = item.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        group = str(metadata.get("group") or "")
        try:
            parts = int(metadata.get("parts") or 1)
        except (TypeError, ValueError, OverflowError):
            parts = 1
        if group and parts > 1:
            try:
                part_no = int(metadata.get("part") or 0)
            except (TypeError, ValueError, OverflowError):
                part_no = 0
            grouped.setdefault(group, []).append((part_no, item))
            if group not in ordered_groups:
                ordered_groups.add(group)
                order.append(("group", group))
        else:
            order.append(("single", item))

    merged: list[Any] = []
    skipped = 0
    # 库内容计数只建一次：原实现每组重建 Counter(existing)，导入成本
    # O(文档组数 × 库内容数)，大备份在写锁内会跑成分钟级
    existing_counts_base = Counter(existing)
    for kind, key in order:
        if kind == "single":
            merged.append(key)
            continue
        pieces = sorted(grouped.get(key, []), key=lambda pair: pair[0])
        try:
            entry, skipped_count = _merge_exported_group(
                key, pieces, existing_counts_base
            )
        except ValueError as exc:
            if problems is None:
                raise
            problems.append(
                {"group": key, "reason": str(exc), "parts": len(pieces)}
            )
            continue
        if entry is None:
            skipped += skipped_count
            continue
        merged.append(entry)
    return merged, skipped


def get_document_group(doc_id: int) -> dict[str, Any] | None:
    """返回片段所属整篇文档（多分片按 part 顺序拼接）与分片数。

    不存在返回 None；单条 / 无 group 的旧分片返回其自身内容。供面板
    「编辑整份文档」回填：整组保存时由 ``update_document`` 整组替换。

    拼接用空串（分片按 part 顺序原样拼接即原文，与导入还原同口径；用换行
    分隔会把分隔符带进编辑框，长文档超过提交上限后原样保存也会失败）。
    文档大小（``size``）即拼接文本长度，编辑回填与上限判断同一口径。读取在写锁内进行，避免读到整组
    替换过程中的中间状态；集合加载（可能阻塞等待）放在写锁外。
    """
    client = _client()
    _load_collection(client, COLLECTION_NAME)
    with _WRITE_LOCK:
        parts, group = _document_info(client, int(doc_id))
        if parts == 0:
            return None
        if not group:
            rows = client.query(
                collection_name=COLLECTION_NAME,
                filter=f"id == {int(doc_id)}",
                output_fields=["content"],
                limit=1,
            )
            content = str(rows[0].get("content", "")) if rows else ""
            return {
                "id": int(doc_id),
                "group": "",
                "parts": parts,
                "size": len(content),
                "content": content,
            }
        pieces: list[tuple[int, str]] = []
        for member in _group_member_ids(client, group):
            rows = client.query(
                collection_name=COLLECTION_NAME,
                filter=f"id == {member}",
                output_fields=["content", "metadata"],
                limit=1,
            )
            if not rows:
                continue
            meta = _parse_metadata(rows[0].get("metadata"))
            try:
                part_no = int(meta.get("part") or 0)
            except (TypeError, ValueError):
                part_no = 0
            pieces.append((part_no, str(rows[0].get("content", ""))))
        pieces.sort(key=lambda pair: pair[0])
        # 空串拼接：分片是原文的连续片段（入库时 strip 后直接拼接），用换行
        # 分隔会把分隔符带进编辑框——文档越大越超提交上限，原样保存都失败
        joined = "".join(text for _, text in pieces)
        return {
            "id": int(doc_id),
            "group": group,
            "parts": len(pieces) or parts,
            "size": len(joined),
            "content": joined,
        }


def find_duplicate_contents() -> list[tuple[str, list[int]]]:
    """返回内容完全相同的**独立**片段组（按首次出现顺序），供人工清理。

    只统计可单删的独立片段：带 ``group`` 的新版多分片记录，以及旧版
    多分片记录（``parts > 1`` 但没有 group）内部出现相同段落都是正常的，
    必须通过整份编辑 / 整组删除维护，按单条重复删除会破坏整篇文档。
    """
    client = _client()
    _load_collection(client, COLLECTION_NAME)
    seen: dict[str, list[tuple[int, bool]]] = {}
    for row in _iter_all_rows(client):
        content = str(row.get("content", "")).strip()
        if not content:
            continue
        meta = _parse_metadata(row.get("metadata"))
        try:
            parts = int(meta.get("parts") or 1)
        except (TypeError, ValueError):
            parts = 1
        # 旧数据可能只有 parts 没有 group：同样是多分片文档，不能按单条处理
        grouped = bool(meta.get("group")) or parts > 1
        seen.setdefault(content, []).append(
            (int(row.get("id", 0) or 0), grouped)
        )
    duplicates: list[tuple[str, list[int]]] = []
    for content, entries in seen.items():
        standalone = [doc_id for doc_id, grouped in entries if not grouped]
        if len(standalone) < 2:
            continue
        duplicates.append((content, standalone))
    return duplicates


def _existing_contents() -> list[str]:
    """库中全部片段内容的多重集（全量扫描，不受单页 16384 条限制）。

    返回列表而不是集合：同内容的重复分片要逐条计数——用集合会让
    「组内两片同文且都已入库」的组被误判为缺片而重复补写。
    """
    client = _client()
    # CLI 新进程里 collection 默认可能是 released 状态，先加载再迭代
    _load_collection(client, COLLECTION_NAME)
    return [
        str(row.get("content", "")).strip() for row in _iter_all_rows(client)
    ]


def _row_content(client: Any, doc_id: int) -> str | None:
    """按 id 读取片段内容（不存在返回 None）。"""
    rows = client.query(
        collection_name=COLLECTION_NAME,
        filter=f"id == {int(doc_id)}",
        output_fields=["content"],
        limit=1,
    )
    if not rows:
        return None
    return str(rows[0].get("content", ""))


def cleanup_duplicate_contents(dry_run: bool = True) -> dict[str, Any]:
    """扫描并（可选）删除内容完全相同的独立片段，每组保留最小 id。

    与 ``kb-duplicates`` / ``kb-dedupe`` 同一判定口径。真正删除时扫描与
    删除在同一把写锁内完成，并在删除前逐条复核「保留片段仍在、内容仍与
    计划一致、不是多分片记录」，避免按过期 id 删掉并发编辑后的新内容；
    并发下已消失 / 已变化的片段记入 ``skipped``，删除异常记入 ``failed``。
    """
    plan: list[dict[str, Any]] = []

    def _build_plan() -> None:
        plan.clear()
        for content, ids in find_duplicate_contents():
            keep = min(ids)
            remove = sorted(doc_id for doc_id in ids if doc_id != keep)
            if not remove:
                continue
            plan.append(
                {
                    "preview": " ".join(content.split())[:60],
                    "content": content,
                    "keep": keep,
                    "remove": remove,
                }
            )

    if dry_run:
        _build_plan()
        return {
            "dry_run": True,
            "groups": len(plan),
            "planned": sum(len(item["remove"]) for item in plan),
            "removed": 0,
            "skipped": [],
            "failed": [],
            "plan": plan,
        }

    with _WRITE_LOCK:
        _build_plan()
        client = _client()
        _load_collection(client, COLLECTION_NAME)
        removed = 0
        skipped: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for item in plan:
            expected = item["content"].strip()
            keep_content = _row_content(client, item["keep"])
            if keep_content is None or keep_content.strip() != expected:
                for doc_id in item["remove"]:
                    skipped.append(
                        {"id": doc_id, "reason": "保留片段已不存在或内容已变化"}
                    )
                continue
            for doc_id in item["remove"]:
                current = _row_content(client, doc_id)
                if current is None:
                    skipped.append({"id": doc_id, "reason": "片段不存在或已被删除"})
                    continue
                if current.strip() != expected:
                    skipped.append({"id": doc_id, "reason": "内容已被修改，已跳过"})
                    continue
                try:
                    parts, group = _document_info(client, doc_id)
                    if group or parts > 1:
                        skipped.append(
                            {"id": doc_id, "reason": "已变为多分片记录，已跳过"}
                        )
                        continue
                    client.delete(collection_name=COLLECTION_NAME, ids=[int(doc_id)])
                except Exception as exc:  # noqa: BLE001 - 单条失败不影响其它组
                    logger.warning("删除重复知识片段 %s 失败: %s", doc_id, exc)
                    failed.append({"id": doc_id, "error": str(exc)})
                    continue
                removed += 1
        return {
            "dry_run": False,
            "groups": len(plan),
            "planned": sum(len(item["remove"]) for item in plan),
            "removed": removed,
            "skipped": skipped,
            "failed": failed,
            "plan": plan,
        }


def _existing_group_layout() -> dict[str, list[tuple[str, int, int]]]:
    """内容 → [(group, part, parts), ...]：补齐残缺组前校验新旧分片布局一致。

    同一内容在库里可能对应多行（组内两片同文是支持的数据形态，独立重复
    副本也会产生同文行）：以内容为键只保留最后一行会让 occupied_parts 看
    不到其它已被占用的 part，补写时写出重复 part 的畸形组。
    """
    client = _client()
    _load_collection(client, COLLECTION_NAME)
    layout: dict[str, list[tuple[str, int, int]]] = {}
    for row in _iter_all_rows(client):
        content = str(row.get("content", "")).strip()
        if not content:
            continue
        meta = _parse_metadata(row.get("metadata"))
        try:
            parts = int(meta.get("parts") or 1)
        except (TypeError, ValueError):
            parts = 1
        try:
            part = int(meta.get("part") or 0)
        except (TypeError, ValueError):
            part = 0
        layout.setdefault(content, []).append(
            (str(meta.get("group") or ""), part, parts)
        )
    return layout


def _encoding_problem(value: str) -> str | None:
    """文本能否按 UTF-8 编码：孤立代理项（``"\\ud800"``）能通过 JSON 解析，
    但嵌入模型 / Milvus 序列化阶段会抛 UnicodeEncodeError，让整批导入失败。"""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return "包含无法编码的字符（孤立代理项）"
    return None


def _metadata_encoding_problem(metadata: Any) -> str | None:
    """元数据键 / 字符串值同样参与序列化：任意一项不可编码即整条无效。"""
    if not isinstance(metadata, dict):
        return None
    for meta_key, meta_value in metadata.items():
        for candidate in (meta_key, meta_value):
            if isinstance(candidate, str):
                problem = _encoding_problem(candidate)
                if problem:
                    return f"元数据{problem}"
    return None


def import_documents(
    documents: list[dict[str, Any]], dry_run: bool = False
) -> dict[str, Any]:
    """批量导入知识片段；空内容 / 非法项跳过，超长字段按接口上限截断。

    导出备份中的多分片记录先按 group 拼回整篇再重新拆分；整组已完整存在
    于库中时整组跳过（重复导入同一备份保持幂等，不会因重新拆分产生重复）。
    ``dry_run`` 只做校验 / 去重并返回 ``would_import``，不加载嵌入模型、
    不写入任何内容。
    """
    items = list(documents)
    if not items:
        return {
            "imported": 0,
            "skipped": 0,
            "skipped_invalid": 0,
            "skipped_duplicate": 0,
        }

    # 与数据库无关的校验先做：全部为空 / 非法时不需要知识库，直接返回
    valid: list[Any] = []
    invalid = 0
    # 组内出现无效成员（空内容 / 非字符串）时整组跳过：只剔除该成员会让
    # 分组校验看到「缺片」并拒绝整份备份，其它合法文档也一起导入失败
    invalid_groups: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            invalid += 1
            continue
        raw_content = item.get("content")
        meta = item.get("metadata")
        group = str(meta.get("group") or "") if isinstance(meta, dict) else ""
        # 只接受字符串内容：JSON null / false / 0 不能变成 "None" / "False" / "0"
        if not isinstance(raw_content, str) or not raw_content.strip():
            if group:
                invalid_groups.add(group)
            invalid += 1
            continue
        # 内容 / 元数据必须能按 UTF-8 编码：孤立代理项能通过 JSON 解析与
        # 预览（dry-run 不编码），但正式导入在嵌入 / 序列化阶段整批失败
        # （503，且预览与导入结论相反）。单条坏数据不该让整批导入失败，
        # 这里与空内容同一口径按无效项跳过（组内成员整组跳过）。
        problem = _encoding_problem(raw_content) or _metadata_encoding_problem(meta)
        if problem:
            if group:
                invalid_groups.add(group)
            invalid += 1
            logger.warning("导入内容%s，已跳过", problem)
            continue
        # 还原标记是内部记账字段：用户 JSON 自带同名键时一律剥离，
        # 否则可以伪造标记绕过单条 / 文档大小上限
        cleaned = {
            key: value
            for key, value in item.items()
            if key
            not in {"__restored_group__", "__restored_size__", "__restored_parts__"}
        }
        valid.append(cleaned)
    if invalid_groups:
        kept: list[Any] = []
        removed = 0
        for entry in valid:
            meta = entry.get("metadata")
            group = str(meta.get("group") or "") if isinstance(meta, dict) else ""
            if group and group in invalid_groups:
                removed += 1
                continue
            kept.append(entry)
        if removed:
            invalid += removed
            logger.warning(
                "备份中有 %d 个分片组含无效分片（空内容 / 非字符串），整组跳过：%s",
                len(invalid_groups),
                sorted(invalid_groups)[:5],
            )
        valid = kept
    if not valid:
        payload: dict[str, Any] = {
            "imported": 0,
            "skipped": invalid,
            "skipped_invalid": invalid,
            "skipped_duplicate": 0,
        }
        if dry_run:
            # 预览的返回结构保持稳定：没有可导入项时 would_import 为 0
            payload["would_import"] = 0
        return payload
    if not knowledge_db_path().exists():
        raise FileNotFoundError("知识库未初始化")

    # 先做与库内容无关的备份完整性校验，避免为坏备份加载嵌入模型（首次可能
    # 下载 bge-small-zh）；结构非法的分片组（声明异常 / 重复 part / 分片不全）
    # 按组降级跳过并计入 skipped_groups——一个坏组不能让整份备份永远无法
    # 恢复（与组内无效分片整组跳过同一口径）。库快照相关处理仍在锁内。
    pre_problems: list[dict[str, Any]] = []
    pre_merged, _ = _regroup_exported_documents(
        valid, set(), problems=pre_problems
    )
    if not pre_merged:
        dropped = sum(int(item.get("parts") or 0) for item in pre_problems)
        payload = {
            "imported": 0,
            "skipped": invalid + dropped,
            "skipped_invalid": invalid + dropped,
            "skipped_duplicate": 0,
        }
        if pre_problems:
            payload["skipped_groups"] = pre_problems
        if dry_run:
            payload["would_import"] = 0
        return payload

    # 嵌入模型加载放在写锁外：导入期间其它知识库接口不应因为模型初始化
    # 而全部阻塞；dry-run 不加载模型
    agent = None if dry_run else _get_agent()

    imported: list[int] = []
    with _WRITE_LOCK:
        # 去重判断放在锁内：并发导入（不同客户端 / 重试）不会都基于旧快照
        # 插入同一批内容；快照走全量扫描（多重集），超过单页规模或同文
        # 重复分片时判重都不会失真
        existing = _existing_contents()
        # 内容可能在同一 group 内重复（两段完全相同的正文各占一个 part）：
        # 用多重集而不是集合计数，一份已存在只能抵消一个分片
        existing_counts: Counter[str] = Counter(existing)
        problems: list[dict[str, Any]] = []
        merged_items, group_skipped = _regroup_exported_documents(
            valid, existing, problems=problems
        )
        # 结构非法的组整组跳过：片数计入 skipped_invalid，明细给到调用方
        invalid += sum(int(item.get("parts") or 0) for item in problems)
        duplicate = group_skipped
        document_chunks: list[list[dict[str, Any]]] = []
        # 与 document_chunks 平行的还原组号（非还原文档为空串），用于识别
        # 「备份组与库内片段部分重合」的布局冲突
        document_groups: list[str] = []
        for item in merged_items:
            restored_group = str(item.pop("__restored_group__", "") or "")
            restored = bool(restored_group)
            restored_size = int(item.pop("__restored_size__", 0) or 0)
            raw_content = item.get("content")
            if not isinstance(raw_content, str):
                invalid += 1
                continue
            content = raw_content.strip()
            if not content:
                invalid += 1
                continue
            metadata = item.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            sanitized_meta = {
                str(key)[:METADATA_KEY_MAX_CHARS]: str(value)[:METADATA_VALUE_MAX_CHARS]
                for key, value in list(metadata.items())[:_METADATA_MAX_KEYS]
            }
            # 长内容拆成 ≤400 字的片段：每段都会被嵌入，尾部内容不再不可检索；
            # 备份还原的拼接长度等于 strip 后总长，交给下面的 DOC_MAX_CHARS 校验
            limit = _IMPORT_CONTENT_MAX_CHARS
            if not restored and len(content) > limit:
                # 超过单条导入上限：计入 skipped_invalid 并记录警告，而不是
                # 静默截断（截断会悄悄丢掉后半段、用户以为已完整入库）；
                # 单条坏数据也不该让整批导入失败
                invalid += 1
                logger.warning(
                    "导入内容超过上限（%d 字 > %d），已跳过", len(content), limit
                )
                continue
            if restored:
                restored_parts = item.pop("__restored_parts__", None)
                pieces = _restore_pieces(restored_parts, content)
                stored_size = sum(len(piece) for piece in pieces)
                if not pieces or stored_size > DOC_MAX_CHARS or (
                    restored_size and restored_size > DOC_MAX_CHARS
                ):
                    invalid += 1
                    logger.warning(
                        "备份中的文档超过还原上限（%d 字 > %d），已跳过",
                        max(stored_size, restored_size),
                        DOC_MAX_CHARS,
                    )
                    continue
                total_parts = len(pieces)
                chunks: list[dict[str, Any]] = []
                for index, piece in enumerate(pieces, start=1):
                    meta = dict(sanitized_meta)
                    if total_parts > 1:
                        meta["part"] = str(index)
                        meta["parts"] = str(total_parts)
                        meta["group"] = restored_group
                    chunks.append({"content": piece, "metadata": meta})
            else:
                chunks = _chunk_documents(content, sanitized_meta)
            document_chunks.append(chunks)
            document_groups.append(restored_group)

        unique: list[dict[str, Any]] = []
        for doc_chunks, restored_group in zip(document_chunks, document_groups):
            doc_group = restored_group
            if not doc_group and len(doc_chunks) > 1:
                # 普通导入的多分片文档同样带本次生成的 group：部分分片命中
                # 内容去重时必须校验布局，否则缺失分片会以「全新 group、
                # part=2/parts=2」单独入库——一个没有 part 1 的残缺组会让
                # 之后整份知识备份的导入 / 编辑永远失败
                doc_group = str(
                    (doc_chunks[0].get("metadata") or {}).get("group") or ""
                )
            # existing 存的是 strip 后的内容；分片内容可能带切分残留的
            # 换行，必须同样 strip 后比较，否则重复导入不幂等。组内重复
            # 内容按多重集消费：一份已存在只抵消一个分片，缺失的同文分片
            # 仍要补齐（整组跳过会让残缺组永久留在库里）。消费按「本篇」
            # 归还——库内内容对后续文档仍然可用来判重
            keys = [piece["content"].strip() for piece in doc_chunks]
            missing = []
            consumed: Counter[str] = Counter()
            for chunk, key in zip(doc_chunks, keys):
                if existing_counts[key] > 0:
                    existing_counts[key] -= 1
                    consumed[key] += 1
                    continue
                missing.append(chunk)
            duplicate += len(doc_chunks) - len(missing)
            if not missing:
                existing_counts.update(consumed)
                continue
            missing_keys = {id(piece) for piece in missing}
            if len(missing) != len(doc_chunks) and doc_group:
                # 备份组与库内片段部分重合：库内旧分片的 group/part/parts
                # 必须与本次布局一致才能安全补齐（例如上次导入中途失败的
                # 同一篇文档）；编辑后重新导入的文档分片布局已变化，补写会
                # 留下 parts 声明与实际成员错位的半组，保守跳过并提示正确维护方式
                layout: dict[str, list[tuple[str, int, int]]] | None
                try:
                    layout = _existing_group_layout()
                except Exception as exc:  # noqa: BLE001 - 无法校验布局时不冒险补写
                    logger.warning("扫描库内分片布局失败（%s），跳过补写组 %s", exc, doc_group)
                    layout = None
                conflicts = []
                cross_group = False
                if layout is None:
                    conflicts.append(doc_group)
                else:
                    for chunk, key in zip(doc_chunks, keys):
                        if id(chunk) in missing_keys:
                            # 缺失分片不参与布局比对（同文重复时第二片是
                            # 待补写项，不对应库里已有的那一行）
                            continue
                        candidates = layout.get(key, [])
                        chunk_meta = chunk.get("metadata")
                        want_part = int((chunk_meta or {}).get("part") or 0)
                        want_parts = int((chunk_meta or {}).get("parts") or 1)
                        # 同一内容可能对应库内多行（组内同文分片 / 独立副本）：
                        # 任一行与本篇布局一致即可按原布局补齐
                        if any(
                            old_group == doc_group
                            and old_parts == want_parts
                            and (not want_part or old_part == want_part)
                            for old_group, old_part, old_parts in candidates
                        ):
                            continue
                        if any(
                            old_group == doc_group
                            for old_group, _, _ in candidates
                        ):
                            conflicts.append(key[:20])
                        else:
                            # 命中的片段属于其它组（库内另一篇文档）或本批
                            # 前一文档尚未落库的同文分片：本组无法按原布局
                            # 补齐（补写会缺 part 1 / 与别人共享同一片）
                            cross_group = True
                        break
                if cross_group and not conflicts:
                    # 组结构无法成立，但独特分片不能静默丢失：去掉派生分片
                    # 标记，以独立片段入库（整篇编辑退化为逐片段维护，内容
                    # 仍可检索；共享片段已在库中 / 本批写入，不重复落库）
                    logger.warning(
                        "文档组 %s 的共享片段属于其它组，缺失分片将以独立片段入库",
                        doc_group,
                    )
                    for chunk in missing:
                        meta = dict(chunk.get("metadata") or {})
                        for derived in ("part", "parts", "group"):
                            meta.pop(derived, None)
                        unique.append(
                            {"content": chunk["content"], "metadata": meta}
                        )
                        existing_counts[chunk["content"].strip()] += 1
                    existing_counts.update(consumed)
                    # 命中片段的重复计数已在上面统一 +1（971 行），这里只
                    # 登记新入库的独立片段供后续文档判重
                    continue
                if conflicts:
                    logger.warning(
                        "备份中的文档组 %s 与库内已有片段布局不一致，已跳过该组："
                        "请整份编辑或 kb-delete --group 后重新导入",
                        doc_group,
                    )
                    existing_counts.update(consumed)
                    duplicate += len(missing)
                    continue
                # 缺失分片同样要校验组内 part 号是否已被其它内容占用：
                # 库里的同组旧分片内容被改过时其 part 号仍占着，直接补写
                # 同号分片会产生重复 part 的畸形组（之后整份备份再也导不进去）
                if layout is not None and doc_group:
                    occupied_parts = {
                        old_part
                        for candidates in layout.values()
                        for old_group, old_part, _ in candidates
                        if old_group == doc_group and old_part
                    }
                    missing_parts: set[int] = set()
                    for chunk in missing:
                        chunk_meta = chunk.get("metadata") or {}
                        try:
                            want_part = int(chunk_meta.get("part") or 0)
                        except (TypeError, ValueError, OverflowError):
                            want_part = 0
                        if want_part:
                            missing_parts.add(want_part)
                    if missing_parts & occupied_parts:
                        logger.warning(
                            "备份中的文档组 %s 缺失分片的 part 号已被库内其它内容"
                            "占用，已跳过该组：请整份编辑或 kb-delete --group 后重新导入",
                            doc_group,
                        )
                        existing_counts.update(consumed)
                        duplicate += len(missing)
                        continue
            # 部分分片已存在（上次导入中途失败 / 组内重复）：只补缺失分片，
            # 补写后组内成员按内容齐全——整组跳过会把残缺组永久留在库里
            if len(missing) != len(doc_chunks):
                total_parts = max(
                    int(piece["metadata"].get("parts") or 1)
                    for piece in doc_chunks
                )
                for missing_piece in missing:
                    missing_piece["metadata"]["parts"] = str(total_parts)
            existing_counts.update(consumed)
            for missing_piece in missing:
                existing_counts[missing_piece["content"].strip()] += 1
            unique.extend(missing)
        if not unique:
            result: dict[str, Any] = {
                "imported": 0,
                "skipped": invalid + duplicate,
                "skipped_invalid": invalid,
                "skipped_duplicate": duplicate,
            }
            if problems:
                result["skipped_groups"] = problems
            if dry_run:
                # 预览的返回结构保持稳定：没有可导入项时 would_import 为 0
                result["would_import"] = 0
            return result
        if dry_run:
            # 预览：只做校验 / 去重，不加载嵌入模型、不写入
            payload = {
                "imported": 0,
                "skipped": invalid + duplicate,
                "skipped_invalid": invalid,
                "skipped_duplicate": duplicate,
                "would_import": len(unique),
            }
            if problems:
                payload["skipped_groups"] = problems
            return payload

        # 按「文档」分批：同一 group 的分片必须落在同一批（批间失败会在库
        # 中留下 parts 声明大于实际成员的残缺组）。只在组起始处切批
        assert agent is not None  # dry-run 已在上面提前返回
        batches: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        for entry in unique:
            meta = entry["metadata"]
            starts_group = str(meta.get("part") or "") == "1" or not str(
                meta.get("group") or ""
            )
            if starts_group and len(current) >= _IMPORT_CHUNK:
                batches.append(current)
                current = []
            current.append(entry)
        if current:
            batches.append(current)
        for batch_no, batch in enumerate(batches, start=1):
            try:
                result = agent.add_documents(batch)
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
    result = {
        "imported": len(imported),
        "skipped": invalid + duplicate,
        "skipped_invalid": invalid,
        "skipped_duplicate": duplicate,
    }
    if problems:
        result["skipped_groups"] = problems
    return result


def get_document(doc_id: int) -> dict[str, Any] | None:
    """按 id 读取单个片段（含完整 metadata）；不存在返回 None。"""
    client = _client()
    _load_collection(client, COLLECTION_NAME)
    with _WRITE_LOCK:
        rows = client.query(
            collection_name=COLLECTION_NAME,
            filter=f"id == {int(doc_id)}",
            output_fields=["id", "content", "metadata"],
            limit=1,
        )
    if not rows:
        return None
    row = rows[0]
    return {
        "id": int(row.get("id", 0) or 0),
        "content": str(row.get("content", "")),
        "metadata": _parse_metadata(row.get("metadata")),
    }


def delete_document(doc_id: int, whole_group: bool = False) -> int:
    """按 id 删除知识片段；返回实际删除的分片数（不存在返回 0）。

    ``whole_group=True`` 时，如果该片段属于带 group 标识的多分片记录，
    同时删除同组的其余分片（旧数据没有 group 时只删除当前片段，返回 1）。
    ``whole_group=False`` 时拒绝对多分片记录的单条删除：只删一片会留下
    「parts 声明大于实际成员」的残缺组，整份编辑 / 备份恢复都会失效。

    Raises:
        ValueError: 对多分片记录做单条删除
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
        else:
            parts, group = _document_info(client, int(doc_id))
            # 旧版多分片记录可能只有 parts>1 没有 group：单条删除同样会留下
            # 「parts 声明大于实际成员」的残缺组，必须按整组语义处理
            if group or parts > 1:
                raise ValueError(
                    "该片段属于多分片知识内容，不能单条删除：请整组删除"
                    "（CLI 加 --group / 接口加 group=1）"
                )
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
    agent = _get_read_agent()
    return agent.search_knowledge(query, top_k=top_k, strict=True)


def _exists(client: Any, doc_id: int) -> bool:
    rows = client.query(
        collection_name=COLLECTION_NAME,
        filter=f"id == {doc_id}",
        output_fields=["id"],
        limit=1,
    )
    return bool(rows)
