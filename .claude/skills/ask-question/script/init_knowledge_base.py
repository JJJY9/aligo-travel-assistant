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
import sys
import uuid
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
                metadata = {
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


def _collect_preserved_documents(rag_agent, new_contents: set[str]) -> list[dict]:
    """导出重建前集合里「不在本次文件集合中」的已有片段（面板 / CLI 添加）。

    重建会删除整个集合；用户通过面板 / CLI 添加、只存在于 Milvus 的片段
    必须先导出，重建后补写回来（同内容分片去重，保留原 group 等元数据）。
    """
    try:
        from utils.knowledge_admin import export_documents

        rows = export_documents()
    except Exception as exc:  # noqa: BLE001 - 读取失败不阻断重建，但提示风险
        print(f"   ⚠️  读取现有知识失败（{exc}），本次重建不会保留库中已有片段")
        return []
    preserved: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        content = str(row.get("content") or "")
        key = content.strip()
        if not key or key in new_contents or key in seen:
            continue
        seen.add(key)
        metadata = row.get("metadata")
        preserved.append(
            {
                "content": content,
                "metadata": metadata if isinstance(metadata, dict) else {},
            }
        )
    return preserved


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

        # 3. 创建 RAG Agent：旧集合维度不一致时先删旧集合再重建
        print("3. 初始化RAG Agent...")
        print(f"   知识库路径: {knowledge_base_path}")
        try:
            rag_agent = _build_agent(model, knowledge_base_path)
        except RuntimeError as exc:
            if "维度" not in str(exc):
                raise
            print(f"   ⚠️  {exc}")
            print("   正在删除旧集合后按当前嵌入模型重建...")
            _drop_collection_for_rebuild(knowledge_base_path)
            rag_agent = _build_agent(model, knowledge_base_path)

        if not rag_agent.initialized:
            print("❌ RAG Agent初始化失败")
            return 1
        print("✓ RAG Agent初始化成功")
        print()

        # 4. 重建集合（先导出库中已有、文件之外添加的片段用于补写）
        print("4. 重建知识集合...")
        collection_exists = rag_agent.milvus_client.has_collection(
            rag_agent.collection_name
        )
        preserved: list[dict] = []
        if collection_exists:
            new_contents = {doc["content"].strip() for doc in documents}
            preserved = _collect_preserved_documents(rag_agent, new_contents)
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
            result = rag_agent.add_documents(documents)
            if result.get("status") != "success":
                raise RuntimeError(str(result.get("message") or "添加文档失败"))
            if preserved:
                backfill = rag_agent.add_documents(preserved)
                if backfill.get("status") != "success":
                    raise RuntimeError(
                        "库内已有片段补写失败："
                        f"{backfill.get('message', 'Unknown error')}"
                    )
        except Exception as exc:  # noqa: BLE001 - 重建失败时尽力回滚已有片段
            if preserved:
                with contextlib.suppress(Exception):
                    rag_agent.add_documents(preserved)
            print(f"❌ 添加文档失败: {exc}")
            return 1
        print(f"✓ 成功添加 {result.get('added_count')} 个片段")
        print(f"✓ 知识库总文档数: {result.get('total_count')}")
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
