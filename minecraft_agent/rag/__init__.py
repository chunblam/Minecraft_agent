"""
rag 包 - 检索增强生成（RAG）模块

包含：
- MarkdownLoader  : .md 知识库文件加载与按标题切块
- EmbeddingClient : 硅基流动 BAAI/bge-m3 向量化客户端
- QueryClassifier : DeepSeek-V3 语义分类器（替代旧的关键词路由）
- RAGRetriever    : 智能路由检索器（LLM 分类 → 向量搜索 → 结果融合）
"""

from .md_loader import MarkdownLoader
from .retriever import RAGRetriever, EmbeddingClient, QueryClassifier, COLLECTION_CATALOG

__all__ = [
    "MarkdownLoader",
    "EmbeddingClient",
    "QueryClassifier",
    "RAGRetriever",
    "COLLECTION_CATALOG",
]
