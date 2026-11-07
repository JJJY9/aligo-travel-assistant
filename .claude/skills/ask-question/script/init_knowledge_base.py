"""
初始化RAG知识库 (Plugin Version)
从 .claude/skills/ask-question/data/documents 目录加载商旅相关文档并导入到向量数据库中

退出码约定（供 cron / CI 检测）：
- 0：全部文档加载、写入成功且自检检索命中；
- 1：任一文档加载失败 / 无文档 / 初始化失败 / 写入失败 / 自检失败。
文档加载失败时不会触碰现有知识库（不删库、不重建）。
"""
import contextlib
import importlib.util
import json
import os
import sys
import uuid
from datetime import datetime
from typing import Any
from pathlib import Path

# 添加项目根目录到路径 (假设脚本在 .claude/skills/ask-question/script/)
current_dir = Path(__file__).parent
project_root = current_dir.parent.parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from config_agentscope import create_chat_model
from utils.console import configure_stdio
from utils.text_chunker import split_text


# 动态加载同目录下的 agent.py
def load_rag_agent_class():
    agent_script = current_dir / "agent.py"
    spec = importlib.util.spec_from_file_location("RAGKnowledgeAgentModule", agent_script)
    module = importlib.util.module_from_spec(spec)
    sys.modules["RAGKnowledgeAgentModule"] = module
    spec.loader.exec_module(module)
    return module.RAGKnowledgeAgent

configure_stdio()
RAGKnowledgeAgent = load_rag_agent_class()

COLLECTION_NAME = "business_travel_knowledge"


def load_documents_from_directory(directory_path: str) -> tuple[list[dict], list[str]]:
    """从指定目录加载并切分所有文档。

    Returns:
        (文档片段列表, 加载失败的文件描述列表)。任一文件读取 / 解码失败都会
        记录到失败列表——调用方据此在改动知识库之前中止（缺文件的半成品
        重建会把已入库内容清掉）。
    """
    documents: list[dict] = []
    failures: list[str] = []
    doc_dir = Path(directory_path)

    if not doc_dir.exists():
        print(f"❌ 文档目录不存在: {directory_path}")
        return documents, [directory_path]

    # 获取所有.txt文件并排序
    doc_files = sorted(doc_dir.glob("*.txt"))

    if not doc_files:
        print("❌ 未找到任何文档文件 (.txt)")
        return documents, [str(doc_dir)]

    # 定义类别映射（根据文件名判断）
    category_mapping = {
        "travel_standards": "差旅规定",
        "reimbursement_policy": "报销规定",
        "booking_guide": "预订指南",
        "faq": "FAQ",
        "emergency_procedures": "应急指南",
        "platform_guide": "平台指南",
        "city_specific_tips": "城市指南",
        "environmental_initiatives": "环保倡议",
    }

    for file_path in doc_files:
        try:
            # 从文件名提取编号作为doc_id (如: 01_travel_standards.txt -> doc_001)
            filename_parts = file_path.stem.split("_", 1)
            if len(filename_parts) >= 2:
                doc_num = filename_parts[0]
                doc_key = filename_parts[1]
            else:
                doc_num = file_path.stem
                doc_key = ""

            base_doc_id = f"doc_{doc_num}"

            # 读取文件内容
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read().strip()

            if not content:
                print(f"   ⚠️  跳过空文件: {file_path.name}")
                continue

            # 提取标题（第一行）
            lines = content.split("\n")
            title = lines[0].strip() if lines else file_path.stem

            # 根据文件名确定类别
            category = "商旅知识"
            for key, cat in category_mapping.items():
                if key in doc_key:
                    category = cat
                    break

            # --- 文档切分逻辑 ---
            chunks = split_text(content)
            # 多分片文档带 group / part / parts 元数据：与知识管理导入路径
            # 一致，整组删除 / 备份还原 / 检索拼装都依赖这些标记
            group_id = uuid.uuid4().hex[:12] if len(chunks) > 1 else ""

            for i, chunk_content in enumerate(chunks):
                doc_id = f"{base_doc_id}_{i+1}"
                metadata: dict[str, Any] = {
                    "category": category,
                    "title": f"{title} (Part {i+1})",
                    "source": "商旅知识库文档",
                    "file_path": str(file_path),
                    "version": "2024版",
                    "parent_doc": file_path.name,
                }
                if group_id:
                    metadata.update(
                        {
                            "group": f"{base_doc_id}-{group_id}",
                            "part": i + 1,
                            "parts": len(chunks),
                        }
                    )
                documents.append(
                    {"id": doc_id, "content": chunk_content, "metadata": metadata}
                )

            print(f"   ✓ 加载文档: {file_path.name} -> {len(chunks)} chunks")

        except Exception as e:  # noqa: BLE001 - 记录失败并在调用方中止
            print(f"   ❌ 加载文件失败 {file_path.name}: {e}")
            failures.append(f"{file_path.name}: {e}")

    return documents, failures


_MANAGED_SOURCE = "商旅知识库文档"
_PRESERVED_BATCH = 200


def _is_managed_metadata(metadata: dict) -> bool:
    """是否由本脚本写入的受管文档片段（带 file_path 的商旅知识库来源）。"""
    return (
        str(metadata.get("source") or "") == _MANAGED_SOURCE
        and bool(metadata.get("file_path"))
    )


def _collect_preserved_documents() -> tuple[list[dict], list[str]]:
    """导出重建前集合里「非受管」的已有片段（面板 / CLI 添加）。

    - 受管文档（本脚本写入，含 file_path）的旧分片一律丢弃：源文件已修改 /
      删除时它们是被替换的过期内容，不能当作面板知识补写回去；
    - 非受管的单条片段按 strip 后内容去重；
    - 带 group 的多分片文档整组保留且**不做内容去重**（组内相同内容合法），
      parts 声明与实际分片不齐的残缺组整体丢弃并告警。

    返回 ``(可补写片段, 告警列表)``；导出失败抛 RuntimeError，调用方必须在
    删库之前中止（否则面板 / CLI 添加的知识会永久丢失）。
    """
    try:
        from utils.knowledge_admin import export_documents

        rows = export_documents()
    except Exception as exc:  # noqa: BLE001 - 导出失败必须中止重建
        raise RuntimeError(f"读取现有知识失败：{exc}") from exc
    groups: dict[str, list[dict]] = {}
    singles: list[dict] = []
    seen_singles: set[str] = set()
    for row in rows:
        content = str(row.get("content") or "")
        key = content.strip()
        if not key:
            continue
        metadata = row.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        if _is_managed_metadata(metadata):
            # 受管文档的旧切分（文件已改动 / 删除）不能补写：否则新旧两版
            # 长期共存，且旧分片会形成 parts 声明不符的残缺组
            continue
        entry = {"content": content, "metadata": metadata}
        group = str(metadata.get("group") or "")
        if group:
            groups.setdefault(group, []).append(entry)
        elif key not in seen_singles:
            seen_singles.add(key)
            singles.append(entry)
    preserved: list[dict] = []
    dropped_groups = 0
    for pieces in groups.values():
        declared = 0
        parts: set[int] = set()
        for piece in pieces:
            meta = piece["metadata"]
            try:
                parts.add(int(meta.get("part") or 0))
                declared = max(declared, int(meta.get("parts") or 0))
            except (TypeError, ValueError):
                parts.add(0)
        if declared > 1 and parts != set(range(1, declared + 1)):
            dropped_groups += 1
            continue
        preserved.extend(pieces)
    warnings: list[str] = []
    if dropped_groups:
        warnings.append(f"丢弃 {dropped_groups} 个残缺分片组（parts 声明与实际不符）")
    return preserved + singles, warnings


_BACKUP_NAME = "preserved_backup.json"


def _preserved_backup_path(knowledge_base_path: Path) -> Path:
    """重建前的保留片段备份文件（与 milvus 目录同级，不随集合重建被清）。"""
    return Path(knowledge_base_path).with_name(
        Path(knowledge_base_path).name + "." + _BACKUP_NAME
    )


def _dedupe_preserved(docs: list[dict]) -> list[dict]:
    """保留片段去重：单条按 strip 内容、多分片组按（组, part, 内容）。"""
    seen_single: set[str] = set()
    seen_parts: set[tuple[str, str, str]] = set()
    merged: list[dict] = []
    for doc in docs:
        content = str(doc.get("content") or "")
        key = content.strip()
        if not key:
            continue
        metadata = doc.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        group = str(metadata.get("group") or "")
        if group:
            marker = (group, str(metadata.get("part") or ""), key)
            if marker in seen_parts:
                continue
            seen_parts.add(marker)
        else:
            if key in seen_single:
                continue
            seen_single.add(key)
        merged.append({"content": content, "metadata": metadata})
    return merged


def _merge_preserved_backup(
    knowledge_base_path: Path, preserved: list[dict]
) -> tuple[list[dict], list[str]]:
    """把上次中断留下的备份并入本次保留片段（重跑即可补齐丢失部分）。"""
    path = _preserved_backup_path(knowledge_base_path)
    if not path.exists():
        return preserved, []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return preserved, [f"上次重建的保留片段备份不可读（{exc}）：{path}"]
    docs = payload.get("documents") if isinstance(payload, dict) else None
    if not isinstance(docs, list):
        return preserved, [f"上次重建的保留片段备份格式异常：{path}"]
    previous = [
        {
            "content": str(item.get("content") or ""),
            "metadata": item.get("metadata")
            if isinstance(item.get("metadata"), dict)
            else {},
        }
        for item in docs
        if isinstance(item, dict)
    ]
    return _dedupe_preserved(previous + preserved), [
        f"已并入上次未完成重建留下的 {len(previous)} 个片段（{path}）"
    ]


def _write_preserved_backup(
    knowledge_base_path: Path, preserved: list[dict]
) -> Path | None:
    """删库前把保留片段落盘：写入阶段被中断 / 失败也能在重跑时找回。"""
    if not preserved:
        return None
    path = _preserved_backup_path(knowledge_base_path)
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "documents": preserved,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return path


def _batch_by_group(rows: list[dict], size: int) -> list[list[dict]]:
    """按组分界切批：多分片文档整组进同一批（与 knowledge_admin 同口径）。"""
    batches: list[list[dict]] = []
    current: list[dict] = []
    current_group = ""
    for row in rows:
        metadata = row.get("metadata")
        group = str(metadata.get("group") or "") if isinstance(metadata, dict) else ""
        same_group = group == current_group and group != ""
        # 达到批上限后，只有在「不是同一个多分片组」时才真正切批
        if current and len(current) >= size and not same_group:
            batches.append(current)
            current = []
        current.append(row)
        current_group = group
    if current:
        batches.append(current)
    return batches


def _add_in_batches(rag_agent, rows: list[dict], label: str) -> None:
    """分批写入（每批失败重试一次）；失败抛 RuntimeError 并带进度。"""
    written = 0
    for index, batch in enumerate(_batch_by_group(rows, _PRESERVED_BATCH), 1):
        last_error: Exception | None = None
        for _ in range(2):
            try:
                result = rag_agent.add_documents(batch)
                if result.get("status") == "success":
                    last_error = None
                    break
                last_error = RuntimeError(
                    str(result.get("message") or f"{label}失败")
                )
            except Exception as exc:  # noqa: BLE001 - 记录后重试一次
                last_error = exc
        if last_error is not None:
            raise RuntimeError(
                f"{label}失败（第 {index} 批，已补 {written} 条）：{last_error}"
            )
        written += len(batch)
        print(f"   ✓ {label}进度 {written}/{len(rows)}")


def _build_agent(model, knowledge_base_path: Path):
    return RAGKnowledgeAgent(
        name="RAGKnowledgeAgent",
        model=model,
        knowledge_base_path=str(knowledge_base_path),
        collection_name=COLLECTION_NAME,
        top_k=5,
        create_if_missing=True,
    )


def _drop_collection_for_rebuild(knowledge_base_path: Path) -> None:
    """维度不一致等导致 Agent 构造失败时，先删除旧集合再重建。"""
    from utils.milvus_client import get_milvus_client

    db_path = str(knowledge_base_path / "milvus_lite.db")
    client = get_milvus_client(db_path)
    with contextlib.suppress(Exception):
        client.drop_collection(COLLECTION_NAME)


def main() -> int:
    print("=" * 70)
    print("初始化RAG知识库 (Plugin Version) - With Chunking")
    print("=" * 70)
    print()

    rag_agent = None
    try:
        # 定义路径
        skill_root = current_dir.parent
        knowledge_base_path = skill_root / "data" / "rag_knowledge"
        documents_dir = skill_root / "data" / "documents"

        # 1. 先加载文档：任一文件失败在触碰知识库之前中止
        print(f"1. 从 {documents_dir} 加载文档...")
        documents, failures = load_documents_from_directory(str(documents_dir))
        if failures:
            print()
            print("❌ 有文档加载失败，已中止（知识库保持原样）：")
            for item in failures:
                print(f"   - {item}")
            return 1
        if not documents:
            print("❌ 未加载到任何文档")
            return 1
        print(f"✓ 成功切分并加载 {len(documents)} 个片段")
        print()

        # 2. 创建模型（知识库导入阶段不会调用模型，仅保持 Agent 接口一致）
        print("2. 创建模型...")
        model = create_chat_model()
        print("✓ 模型创建成功")
        print()

        knowledge_base_path.mkdir(parents=True, exist_ok=True)

        # 3. 创建 RAG Agent：旧集合维度不一致时先「导出保留片段」再删旧集合
        print("3. 初始化RAG Agent...")
        print(f"   知识库路径: {knowledge_base_path}")
        preserved: list[dict] = []
        preserved_ready = False
        backup_path: Path | None = None
        try:
            rag_agent = _build_agent(model, knowledge_base_path)
        except RuntimeError as exc:
            if "维度" not in str(exc):
                raise
            print(f"   ⚠️  {exc}")
            # 顺序关键：先导出面板 / CLI 添加的片段，导出失败即中止——
            # 先删集合后导出会让这些知识在换嵌入模型时永久丢失
            print("   正在导出库内已有片段（维度重建前保留）...")
            try:
                preserved, warnings = _collect_preserved_documents()
            except RuntimeError as export_error:
                print(f"❌ {export_error}，已中止（知识库保持原样）")
                return 1
            for warning in warnings:
                print(f"   ⚠️  {warning}")
            preserved, backup_notes = _merge_preserved_backup(
                knowledge_base_path, preserved
            )
            for note in backup_notes:
                print(f"   ⚠️  {note}")
            backup_path = _write_preserved_backup(knowledge_base_path, preserved)
            print(
                f"   已导出 {len(preserved)} 个片段，删除旧集合并按当前嵌入模型重建..."
            )
            preserved_ready = True
            _drop_collection_for_rebuild(knowledge_base_path)
            rag_agent = _build_agent(model, knowledge_base_path)

        if not rag_agent.initialized:
            print("❌ RAG Agent初始化失败")
            return 1
        print("✓ RAG Agent初始化成功")
        print()

        # 4. 重建集合（先导出库中已有、文件之外添加的片段用于补写）
        print("4. 重建知识集合...")
        if preserved_ready:
            print("   旧集合已在维度自愈时重建，直接进入写入")
        else:
            collection_exists = rag_agent.milvus_client.has_collection(
                rag_agent.collection_name
            )
            if collection_exists:
                # 导出失败必须在删库之前中止：库内面板 / CLI 添加的片段
                # 只存在于 Milvus，先删后导会永久丢失
                try:
                    preserved, warnings = _collect_preserved_documents()
                except RuntimeError as export_error:
                    print(f"❌ {export_error}，已中止（知识库保持原样）")
                    return 1
                for warning in warnings:
                    print(f"   ⚠️  {warning}")
                preserved, backup_notes = _merge_preserved_backup(
                    knowledge_base_path, preserved
                )
                for note in backup_notes:
                    print(f"   ⚠️  {note}")
                backup_path = _write_preserved_backup(
                    knowledge_base_path, preserved
                )
                if preserved:
                    print(f"   已导出 {len(preserved)} 个库内已有片段（重建后补写）")
                print("   ⚠️  正在删除旧 Collection 并重建...")
                rag_agent.milvus_client.drop_collection(rag_agent.collection_name)
                rag_agent.milvus_client.create_collection(
                    collection_name=rag_agent.collection_name,
                    dimension=rag_agent.embedding_dim,
                    metric_type="COSINE",
                    auto_id=False,
                )
                print("   ✓ Collection 重建完成")

        # 5. 写入文档（内置文件 + 重建前导出的已有片段）
        print("5. 将文档添加到RAG知识库...")
        try:
            # 分批写入（每批 200 条、失败重试一次）：一次性巨型调用在补写
            # 失败时无法定位进度，失败即丢失面板 / CLI 添加的知识
            _add_in_batches(rag_agent, documents, "内置文档写入")
            if preserved:
                _add_in_batches(rag_agent, preserved, "库内已有片段补写")
        except Exception as exc:  # noqa: BLE001 - 写入失败反映在退出码
            print(f"❌ 添加文档失败: {exc}")
            if preserved:
                hint = (
                    f"已备份到 {backup_path}，" if backup_path is not None else ""
                )
                print(
                    "   面板 / CLI 添加的片段本次未能全部补写，"
                    f"{hint}重跑脚本会自动并入并补齐"
                )
            return 1
        print(f"✓ 成功写入 {len(documents)} 个内置片段")
        if preserved:
            print(f"✓ 成功补写 {len(preserved)} 个库内已有片段")
        print()

        # 6. 统计信息
        print("6. 知识库统计信息:")
        stats = rag_agent.get_stats()
        if stats.get("status") == "success":
            print(f"   - Collection: {stats.get('collection_name')}")
            print(f"   - 文档数量: {stats.get('total_documents')}")
            print(f"   - 存储路径: {stats.get('knowledge_base_path')}")
        print()

        # 7. 自检检索（strict：后端故障不能被吞成「未命中」）
        print("7. 测试知识检索...")
        test_queries = [
            "出差住宿标准是多少？",
            "航班延误了怎么办？",
            "机票应该提前多久预订？",
        ]
        hits = 0
        try:
            for query in test_queries:
                print(f"\n   查询: {query}")
                results = rag_agent.search_knowledge(query, top_k=2, strict=True)
                if results:
                    hits += len(results)
                    for i, doc in enumerate(results, 1):
                        metadata = doc.get("metadata") or {}
                        if isinstance(metadata, str):
                            import json

                            try:
                                metadata = json.loads(metadata)
                            except Exception:  # noqa: BLE001 - 元数据损坏时只影响展示
                                metadata = {}
                        title = (
                            metadata.get("title", "Unknown")
                            if isinstance(metadata, dict)
                            else "Unknown"
                        )
                        distance = doc.get("distance", 0.0)
                        print(f"      [{i}] {title} (相似度: {distance:.3f})")
                else:
                    print("   ⚠️  未找到相关文档")
        except Exception as exc:  # noqa: BLE001 - 自检失败要反映在退出码
            print(f"❌ 自检检索失败: {exc}")
            return 1
        if not hits:
            print("❌ 自检检索全部未命中，知识库可能不可用")
            return 1

        # 全部写入与自检都成功：保留片段备份已完成使命，清理掉
        if backup_path is not None and backup_path.exists():
            try:
                backup_path.unlink()
            except OSError as exc:
                print(f"   ⚠️  清理保留片段备份失败：{exc}")
        print()
        print("=" * 70)
        print("知识库初始化完成！")
        print("=" * 70)
        return 0

    finally:
        # 确保资源被正确清理
        if rag_agent:
            print("\n正在清理资源...")
            try:
                rag_agent.close()
            except Exception:
                pass
            print("✓ 资源清理完成")


if __name__ == "__main__":
    sys.exit(main())
