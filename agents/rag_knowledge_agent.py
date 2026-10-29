"""兼容导出：RAG 知识库智能体已迁到 `skills/ask-question` 插件。"""
from agents._skill_compat import load_skill_agent

RAGKnowledgeAgent = load_skill_agent("ask-question", "RAGKnowledgeAgent")

__all__ = ["RAGKnowledgeAgent"]
