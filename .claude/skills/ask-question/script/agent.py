"""
RAG知识库智能体 RAGKnowledgeAgent
职责：基于向量数据库的知识检索与问答

核心功能：
1. 知识库构建：将商旅相关文档向量化并存储到Milvus Lite
2. 语义检索：根据用户查询检索最相关的知识片段
3. 知识问答：结合检索到的知识和LLM生成准确答案
4. 知识管理：支持添加、更新、删除知识库内容

技术栈：
- Milvus Lite: 轻量级向量数据库（本地存储）
- sentence-transformers: 文本向量化模型
- LLM: 用户配置的豆包模型用于生成答案

安装：
pip install milvus sentence-transformers
"""
import asyncio
import json
import logging
import os

# Add project root to sys.path
import sys
from pathlib import Path

from agentscope.agent import AgentBase
from agentscope.message import Msg

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from utils.json_parser import extract_json_from_async_response
from utils.milvus_client import (  # noqa: F401 - shutdown_milvus_clients 供外部清理
    get_milvus_client,
    next_entity_id,
    reset_milvus_client,
    shutdown_milvus_clients,
)

_GRPC_MAX_MS = '2147483647'  # gRPC 使用的 int32 上限，约 24.8 天
os.environ['GRPC_KEEPALIVE_TIME_MS'] = _GRPC_MAX_MS
os.environ['GRPC_KEEPALIVE_TIMEOUT_MS'] = '20000'
os.environ['GRPC_KEEPALIVE_PERMIT_WITHOUT_CALLS'] = '0'
os.environ['GRPC_HTTP2_MIN_RECV_PING_INTERVAL_WITHOUT_DATA_MS'] = _GRPC_MAX_MS
os.environ['GRPC_HTTP2_MIN_PING_INTERVAL_WITHOUT_DATA_MS'] = _GRPC_MAX_MS

logger = logging.getLogger(__name__)

try:
    import pymilvus  # noqa: F401 - 仅用于检测依赖是否安装
    from sentence_transformers import SentenceTransformer  # noqa: F401 - 依赖检测
    DEPENDENCIES_AVAILABLE = True
except ImportError as e:
    logger.warning(f"RAG dependencies not available: {e}")
    logger.warning("Install with: pip install pymilvus sentence-transformers")
    DEPENDENCIES_AVAILABLE = False

from utils.embedding_model import get_embedding_model
from utils.user_query import extract_user_query


class RAGKnowledgeAgent(AgentBase):
    """RAG知识库智能体"""

    def __init__(
        self,
        name: str = "RAGKnowledgeAgent",
        model=None,
        knowledge_base_path: str | None = None,
        collection_name: str = "business_travel_knowledge",
        embedding_model: str = "BAAI/bge-small-zh-v1.5",
        top_k: int = 5,
        **kwargs
    ):
        super().__init__()
        self.name = name
        self.model = model
        
        if knowledge_base_path is None:
            # 与 Web / CLI / doctor 共用同一套路径解析（Skill 数据目录优先）
            from utils.knowledge_paths import default_knowledge_dir

            knowledge_base_path = str(default_knowledge_dir())

        self.knowledge_base_path = Path(knowledge_base_path)
        self.collection_name = collection_name
        self.top_k = top_k
        from utils.skill_loader import SkillLoader
        self.skill_loader = SkillLoader()

        if not DEPENDENCIES_AVAILABLE:
            logger.error("RAG dependencies not installed. Install with: pip install pymilvus sentence-transformers")
            self.initialized = False
            return

        # 优先使用 config 中的配置（支持本地路径，避免连 HuggingFace）
        try:
            from config import RAG_CONFIG
            embedding_model = RAG_CONFIG.get("embedding_model", embedding_model)
        except Exception:
            pass

        # 若配置的是本地路径且存在，则从本地加载，否则按模型 ID 使用（会联网）
        model_path_or_id = embedding_model
        path_obj = Path(embedding_model).expanduser()
        if not path_obj.is_absolute():
            # 相对路径锚定到项目根目录，避免依赖运行时 CWD（Issue #5）
            project_root = Path(__file__).resolve().parents[4]
            path_obj = project_root / path_obj
        if path_obj.exists():
            model_path_or_id = str(path_obj.resolve())
            logger.info(f"Using local embedding model: {model_path_or_id}")
        else:
            if "/" in embedding_model or "\\" in embedding_model or embedding_model.startswith("."):
                logger.warning(
                    f"Configured embedding path does not exist: {embedding_model}，将使用 BAAI/bge-small-zh-v1.5 并尝试联网下载。"
                )
                model_path_or_id = "BAAI/bge-small-zh-v1.5"
        logger.info(f"Loading embedding model: {model_path_or_id}")
        # 进程内共享同一份嵌入模型：每个会话各建一份会重复占用 ~90 MB
        self.embedding_model = get_embedding_model(model_path_or_id)
        self.embedding_dim = self.embedding_model.get_sentence_embedding_dimension()

        # 初始化 Milvus Lite（本地文件存储）
        milvus_db_path = str(self.knowledge_base_path / "milvus_lite.db")
        logger.info(f"Initializing Milvus Lite at: {milvus_db_path}")

        self._milvus_db_path = milvus_db_path
        self._client_created_at = None  # 用于追踪客户端创建时间
        client = self.milvus_client

        # 检查collection是否存在
        if client.has_collection(collection_name):
            logger.info(f"Loaded existing collection: {collection_name}")
            self._check_embedding_dimension(client)
        else:
            # 创建新collection
            logger.info(f"Creating new collection: {collection_name}")
            client.create_collection(
                collection_name=collection_name,
                dimension=self.embedding_dim,
                metric_type="COSINE",  # 余弦相似度
                auto_id=False,
            )
            logger.info(f"Created new collection: {collection_name}")

        self.initialized = True
        logger.info("RAG Knowledge Agent (Milvus Lite) initialized successfully")

    @property
    def milvus_client(self):
        """每次从共享注册表取当前客户端，避免重连后其它会话持有已关闭句柄。"""
        return get_milvus_client(self._milvus_db_path)

    def _check_embedding_dimension(self, client) -> None:
        """集合向量维度与当前嵌入模型不一致时给出明确警告（检索会静默为空）。"""
        try:
            description = client.describe_collection(self.collection_name)
        except Exception as exc:  # noqa: BLE001 - 仅诊断，不影响初始化
            logger.debug("无法读取集合结构，跳过维度检查: %s", exc)
            return
        if not isinstance(description, dict):
            return
        fields = description.get("fields")
        if not isinstance(fields, list):
            return
        for field in fields:
            if not isinstance(field, dict) or field.get("name") != "vector":
                continue
            params = field.get("params")
            dim = params.get("dim") if isinstance(params, dict) else None
            if dim is None and isinstance(field.get("type_params"), dict):
                dim = field["type_params"].get("dim")
            try:
                if dim is not None and int(dim) != int(self.embedding_dim):
                    logger.warning(
                        "知识库向量维度 %s 与嵌入模型 %s 不一致，检索会为空；"
                        "请重新初始化知识库",
                        dim,
                        self.embedding_dim,
                    )
            except (TypeError, ValueError):
                return
            return

    def _ensure_connection(self):
        """确保 Milvus 连接正常，如果需要则重新创建客户端"""
        try:
            # 尝试一个轻量级操作来检查连接
            self.milvus_client.has_collection(self.collection_name)
        except Exception as e:
            logger.warning(f"Milvus connection issue detected: {e}, reconnecting...")
            try:
                # 丢弃并关闭旧共享客户端；属性会重新创建并取到新实例
                reset_milvus_client(self._milvus_db_path)
                self.milvus_client.has_collection(self.collection_name)
                logger.info("Milvus client reconnected successfully")
            except Exception as reconnect_error:
                logger.error(f"Failed to reconnect Milvus: {reconnect_error}")
                raise

    def add_documents(self, documents: list[dict[str, str]]) -> dict:
        """
        添加文档到知识库

        Args:
            documents: 文档列表，每个文档包含 {'content': '内容', 'metadata': {...}}

        Returns:
            添加结果统计
        """
        if not self.initialized:
            return {"status": "error", "message": "RAG Agent not initialized"}

        try:
            # 确保连接正常
            self._ensure_connection()
            # 以当前最大 id 为基准生成新 id：删除过片段后 row_count 会小于
            # 最大 id，按行数递增会与已有主键冲突
            next_id = self._next_document_id()

            # 准备数据
            data_to_insert = []
            inserted_ids = []

            for i, doc in enumerate(documents):
                # Milvus 要求 id 必须是 int64
                doc_id = next_id + i
                content = doc['content']
                metadata: dict = doc.get('metadata', {})

                # 生成向量
                embedding = self.embedding_model.encode(content).tolist()

                # Milvus 数据格式
                data_to_insert.append({
                    "id": doc_id,
                    "vector": embedding,
                    "content": content,
                    "metadata": json.dumps(metadata, ensure_ascii=False)  # 将metadata转为JSON字符串
                })
                inserted_ids.append(doc_id)

            # 批量插入到 Milvus
            self.milvus_client.insert(
                collection_name=self.collection_name,
                data=data_to_insert
            )

            # 获取总数（仅用于展示；统计失败不能把已成功的写入报成失败）
            try:
                stats = self.milvus_client.get_collection_stats(self.collection_name)
                total_count = int(stats.get("row_count", len(documents)) or 0)
            except Exception as stats_err:  # noqa: BLE001 - 写入已成功，统计失败只记录
                logger.warning(f"get_collection_stats failed after insert: {stats_err}")
                total_count = None

            logger.info(f"Successfully added {len(documents)} documents to knowledge base")
            return {
                "status": "success",
                "added_count": len(documents),
                "total_count": total_count,
                "ids": inserted_ids,
            }

        except Exception as e:
            logger.error(f"Error adding documents: {e}")
            return {"status": "error", "message": str(e)}

    def update_document(
        self, document_id: int, content: str, metadata: dict | None = None
    ) -> dict:
        """按 id 覆盖更新一个知识片段（重新计算向量后 upsert）。"""
        if not self.initialized:
            return {"status": "error", "message": "RAG Agent not initialized"}
        try:
            self._ensure_connection()
            embedding = self.embedding_model.encode(content).tolist()
            self.milvus_client.upsert(
                collection_name=self.collection_name,
                data=[
                    {
                        "id": int(document_id),
                        "vector": embedding,
                        "content": content,
                        "metadata": json.dumps(metadata or {}, ensure_ascii=False),
                    }
                ],
            )
            logger.info(f"Successfully updated document {document_id}")
            return {"status": "success", "id": int(document_id)}
        except Exception as e:
            logger.error(f"Error updating document: {e}")
            return {"status": "error", "message": str(e)}

    def _next_document_id(self) -> int:
        """返回下一个可用的文档 id（现有最大 id + 1，空集合从 1 开始）。

        查询失败时直接抛出，由 add_documents 返回错误；不能用行数兜底，
        否则删除过片段后可能与已有主键冲突并静默覆盖旧知识。
        """
        return next_entity_id(self.milvus_client, self.collection_name)

    def search_knowledge(self, query: str, top_k: int | None = None, strict: bool = False) -> list[dict]:
        """
        检索知识库

        Args:
            query: 查询文本
            top_k: 返回top k个结果
            strict: 为 True 时不吞掉异常（未初始化 / 后端故障直接抛出），
                供需要区分“空结果”与“检索失败”的管理接口使用

        Returns:
            检索结果列表
        """
        if not self.initialized:
            if strict:
                raise RuntimeError("知识库未初始化")
            return []

        # 空查询会返回任意 top-k 片段（答非所问），直接视为无结果
        if not query or not str(query).strip():
            if strict:
                raise ValueError("查询内容为空")
            return []
        query = str(query)

        try:
            # 确保连接正常
            self._ensure_connection()

            # 确保 collection 已加载后再检索：
            # Milvus Lite 3.x（Windows）新会话中 collection 默认为 released 状态，
            # 不加载会报 "call load() before search/get/query"；2.x 上该调用无副作用。
            try:
                self.milvus_client.load_collection(self.collection_name)
            except Exception as load_err:
                logger.debug(f"load_collection skipped: {load_err}")

            if top_k is None:
                k = self.top_k
            else:
                try:
                    k = int(top_k)
                except (TypeError, ValueError):
                    k = self.top_k
                if k <= 0:
                    return []

            # 生成查询向量
            query_embedding = self.embedding_model.encode(query).tolist()

            # 在 Milvus 中检索
            results = self.milvus_client.search(
                collection_name=self.collection_name,
                data=[query_embedding],
                limit=k,
                output_fields=["id", "content", "metadata"]
            )

            # 格式化结果
            retrieved_docs = []
            if results and len(results) > 0:
                for hit in results[0]:
                    # 解析metadata
                    metadata_str = hit.get("entity", {}).get("metadata", "{}")
                    try:
                        metadata = json.loads(metadata_str)
                    except Exception:
                        metadata = {}

                    retrieved_docs.append({
                        'id': hit.get("entity", {}).get("id", ""),
                        'content': hit.get("entity", {}).get("content", ""),
                        'metadata': metadata,
                        'distance': hit.get("distance", 0.0)
                    })

            logger.info(f"Retrieved {len(retrieved_docs)} documents for query: {query[:50]}")
            return retrieved_docs

        except Exception as e:
            logger.error(f"Error searching knowledge: {e}")
            if strict:
                raise
            return []

    async def reply(self, x: Msg | list[Msg] | None = None) -> Msg:
        """
        RAG问答主流程
        1. 接收用户查询
        2. 检索相关知识
        3. 结合知识生成答案
        """
        if not self.initialized:
            return Msg(
                name=self.name,
                content=json.dumps({
                    "status": "error",
                    "message": "RAG Agent not initialized. Please install dependencies: pip install pymilvus sentence-transformers"
                }),
                role="assistant"
            )

        if x is None:
            return Msg(name=self.name, content=json.dumps({}), role="assistant")

        # 获取用户查询
        if isinstance(x, list):
            content = x[-1].content if x else ""
        else:
            content = x.content

        # 尝试解析 JSON 输入 (来自 Orchestrator)，取出检索问题
        user_query = extract_user_query(content)

        # 检索相关知识。嵌入模型编码与 Milvus 查询是同步阻塞调用，
        # 放到线程池执行，避免卡住事件循环（SSE 心跳与其它会话）。
        # strict=True：后端故障要显式报错，不能伪装成「知识库没有相关信息」
        try:
            retrieved_docs = await asyncio.to_thread(
                self.search_knowledge, user_query, None, True
            )
        except Exception as exc:  # noqa: BLE001 - 知识库故障转为智能体错误
            logger.warning("知识库检索失败: %s", exc)
            # 顶层 error / message 供编排聚合与前端展示使用：聚合器按
            # result["error"] / result["message"] 提取原因，只放在
            # answer / data.error 里会被丢弃渲染成「智能体返回错误状态」
            result = {
                "status": "error",
                "query": user_query,
                "error": str(exc),
                "message": f"知识库检索失败：{exc}",
                "answer": f"知识库暂时不可用，请稍后再试（{exc}）",
                "data": {"error": str(exc)},
            }
            return Msg(
                name=self.name,
                content=json.dumps(result, ensure_ascii=False),
                role="assistant",
            )

        if not retrieved_docs:
            result = {
                "status": "no_knowledge",
                "query": user_query,
                "answer": "抱歉，我在知识库中没有找到相关信息。",
                "retrieved_documents": []
            }
            return Msg(name=self.name, content=json.dumps(result, ensure_ascii=False), role="assistant")

        # 构建知识上下文
        knowledge_context = "\n\n".join([
            f"【知识片段{i+1}】\n{doc['content']}"
            for i, doc in enumerate(retrieved_docs)
        ])

        # 如果有LLM，使用LLM生成答案
        if self.model:
            # 动态读取 Prompt 指令 (Progressive Disclosure)
            skill_instruction = self.skill_loader.get_skill_content("ask-question")
            if not skill_instruction:
                skill_instruction = "请基于知识库中的信息回答用户的问题。"

            prompt = f"""你是一个商旅知识专家。请严格基于以下知识库中的信息回答用户的问题。

【用户问题】
{user_query}

【知识库信息】
{knowledge_context}

【任务说明】
{skill_instruction}

【重要约束】
1. 如果【知识库信息】中没有包含回答用户问题所需的信息，请直接回答“抱歉，知识库中没有找到相关信息”，不要尝试根据你自己的知识编造答案。
2. 即使问题很基础，如果知识库里没写，就说不知道。
3. 请以专业、客观的语气回答。
"""

            try:
                # 调用LLM生成答案
                messages = [
                    {"role": "system", "content": "你是一个商旅知识专家。"},
                    {"role": "user", "content": prompt}
                ]
                response = await self.model(messages)

                # 获取响应内容（兼容非流式 ChatResponse / 流式生成器 / 测试 mock）
                answer = await extract_json_from_async_response(response)

                if not answer:
                    answer = "无法生成答案"
                
                # 清理 LLM 可能输出的 JSON 格式
                answer_str = answer.strip()
                if answer_str.startswith("{") and answer_str.endswith("}"):
                    try:
                        json_obj = json.loads(answer_str)
                        # 如果 LLM 输出了 {"answer": "..."} 或 {"content": "..."}
                        if isinstance(json_obj, dict):
                            answer = json_obj.get("answer") or json_obj.get("content") or answer
                    except Exception:
                        pass

            except Exception as e:
                logger.error(f"Error generating answer with LLM: {e}")
                answer = f"知识库中找到相关信息，但生成答案时出错：{e!s}"
        else:
            # 如果没有LLM，直接返回检索到的知识
            answer = "以下是知识库中的相关信息：\n\n" + knowledge_context

        result = {
            "status": "success",
            "query": user_query,
            "answer": answer,
            "retrieved_documents": [
                {
                    "content": doc['content'][:200] + "..." if len(doc['content']) > 200 else doc['content'],
                    "metadata": doc['metadata']
                }
                for doc in retrieved_docs
            ]
        }

        return Msg(name=self.name, content=json.dumps(result, ensure_ascii=False), role="assistant")

    def get_stats(self) -> dict:
        """获取知识库统计信息"""
        if not self.initialized:
            return {"status": "error", "message": "Not initialized"}

        try:
            # 确保连接正常
            self._ensure_connection()
            stats = self.milvus_client.get_collection_stats(self.collection_name)
            return {
                "status": "success",
                "collection_name": self.collection_name,
                "total_documents": stats.get("row_count", 0),
                "knowledge_base_path": str(self.knowledge_base_path)
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def close(self):
        """Milvus Lite 客户端在进程内按数据库路径共享，这里不主动关闭。

        单独关闭会影响其它共用该客户端的 RAGKnowledgeAgent；进程退出时由
        操作系统释放文件句柄。需要强制关闭时调用模块级 shutdown_milvus_clients()。
        """
        logger.debug("Milvus client is shared; skip close")

    def __del__(self):
        """析构函数，确保资源被释放"""
        self.close()
