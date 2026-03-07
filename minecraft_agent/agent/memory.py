"""
三层记忆系统模块
- ShortTermMemory: 短期滑动窗口记忆（最近 N 条事件）
- LongTermMemory: 长期向量记忆（ChromaDB 持久化存储）
记忆压缩：短期记忆满时自动总结最旧事件存入长期记忆
"""

import os
import uuid
import json
from datetime import datetime
from collections import deque
from typing import Any
import chromadb
from loguru import logger


# ChromaDB collection 名称
LONG_TERM_COLLECTION = "mc_long_term_memory"


class ShortTermMemory:
    """
    短期滑动窗口记忆。
    使用双端队列维护最近 max_size 条事件记录。
    当记忆满时触发压缩，将最旧的一批事件摘要存入长期记忆。
    """

    def __init__(self, max_size: int = 20) -> None:
        """
        Args:
            max_size: 滑动窗口最大容量
        """
        self.max_size = max_size
        self._buffer: deque[dict[str, Any]] = deque(maxlen=max_size)

    def add(self, event: dict[str, Any]) -> None:
        """
        添加一条事件到短期记忆。
        事件字典建议包含 role（user/agent）、content、timestamp 字段。

        Args:
            event: 事件字典
        """
        event.setdefault("timestamp", datetime.now().isoformat())
        self._buffer.append(event)
        logger.debug(f"短期记忆添加: {event.get('role', '?')} - {str(event.get('content', ''))[:50]}")

    def get_all(self) -> list[dict[str, Any]]:
        """返回当前所有短期记忆（从旧到新）"""
        return list(self._buffer)

    def get_recent(self, n: int = 5) -> list[dict[str, Any]]:
        """
        获取最近 n 条记忆。

        Args:
            n: 返回条数

        Returns:
            最近 n 条事件列表（从旧到新）
        """
        return list(self._buffer)[-n:]

    def is_full(self) -> bool:
        """判断短期记忆是否已满"""
        return len(self._buffer) >= self.max_size

    def pop_oldest(self, n: int = 5) -> list[dict[str, Any]]:
        """
        弹出最旧的 n 条记忆（用于压缩后转存长期记忆）。

        Args:
            n: 弹出条数

        Returns:
            被弹出的事件列表
        """
        popped = []
        for _ in range(min(n, len(self._buffer))):
            popped.append(self._buffer.popleft())
        return popped

    def to_context_string(self) -> str:
        """将短期记忆格式化为 LLM 可读的对话上下文字符串"""
        lines = []
        for event in self._buffer:
            role = event.get("role", "unknown")
            content = event.get("content", "")
            lines.append(f"[{role}]: {content}")
        return "\n".join(lines)

    def clear(self) -> None:
        """清空短期记忆"""
        self._buffer.clear()

    def __len__(self) -> int:
        return len(self._buffer)


class LongTermMemory:
    """
    基于 ChromaDB 的长期向量记忆。
    使用硅基流动 BAAI/bge-m3 API 进行向量化后存储。
    支持语义相似度检索历史事件。
    """

    def __init__(self) -> None:
        chroma_path = os.getenv("CHROMA_DB_PATH", "./data/chroma_db")
        # 转为相对于项目根目录的绝对路径
        if not os.path.isabs(chroma_path):
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            chroma_path = os.path.join(base_dir, chroma_path.lstrip("./"))

        self.client = chromadb.PersistentClient(path=chroma_path)
        self._collection = self.client.get_or_create_collection(
            name=LONG_TERM_COLLECTION,
            metadata={"description": "Minecraft Agent 长期事件记忆"},
        )
        # 延迟导入 EmbeddingClient 避免循环依赖
        from rag.retriever import EmbeddingClient
        self._embedder = EmbeddingClient()
        logger.info(f"长期记忆初始化完成，当前记录数: {self._collection.count()}")

    async def store(self, content: str, metadata: dict[str, Any] | None = None) -> None:
        """
        将文本内容向量化后存入长期记忆。

        Args:
            content: 要存储的文本（事件摘要、反思报告等）
            metadata: 附加元数据（如时间戳、类型标签）
        """
        try:
            embedding = await self._embedder.embed(content)
            doc_id = str(uuid.uuid4())
            meta = metadata or {}
            meta["timestamp"] = meta.get("timestamp", datetime.now().isoformat())
            meta["content_preview"] = content[:100]

            self._collection.add(
                ids=[doc_id],
                embeddings=[embedding],
                documents=[content],
                metadatas=[meta],
            )
            logger.debug(f"长期记忆存储成功: {content[:50]}...")
        except Exception as e:
            logger.error(f"长期记忆存储失败: {e}", exc_info=True)

    async def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """
        根据查询语义检索最相关的长期记忆。

        Args:
            query: 查询文本
            top_k: 返回条数

        Returns:
            相关记忆列表，每条包含 content 和 metadata
        """
        try:
            embedding = await self._embedder.embed(query)
            results = self._collection.query(
                query_embeddings=[embedding],
                n_results=min(top_k, max(1, self._collection.count())),
                include=["documents", "metadatas", "distances"],
            )

            memories = []
            docs = results.get("documents", [[]])[0]
            metas = results.get("metadatas", [[]])[0]
            distances = results.get("distances", [[]])[0]

            for doc, meta, dist in zip(docs, metas, distances):
                memories.append({
                    "content": doc,
                    "metadata": meta,
                    "relevance_score": 1 - dist,  # 转为相似度分数
                })
            return memories

        except Exception as e:
            logger.error(f"长期记忆检索失败: {e}", exc_info=True)
            return []

    def count(self) -> int:
        """返回长期记忆中的记录总数"""
        return self._collection.count()


class MemoryManager:
    """
    记忆管理器：协调短期记忆和长期记忆的交互。
    负责自动压缩：当短期记忆满时，将最旧事件摘要存入长期记忆。
    """

    def __init__(self) -> None:
        max_size = int(os.getenv("SHORT_TERM_MEMORY_SIZE", "20"))
        self.short_term = ShortTermMemory(max_size=max_size)
        self.long_term = LongTermMemory()

    async def add_event(self, role: str, content: str, extra: dict | None = None) -> None:
        """
        添加一条事件，必要时触发自动压缩。

        Args:
            role: 事件角色（user/agent/system）
            content: 事件内容
            extra: 附加信息（如 action_type、timestamp）
        """
        event = {"role": role, "content": content, **(extra or {})}

        # 短期记忆满时自动压缩最旧的 5 条
        if self.short_term.is_full():
            await self._compress_to_long_term(compress_count=5)

        self.short_term.add(event)

    async def _compress_to_long_term(self, compress_count: int = 5) -> None:
        """
        将短期记忆中最旧的事件压缩为摘要存入长期记忆。

        Args:
            compress_count: 压缩条数
        """
        old_events = self.short_term.pop_oldest(compress_count)
        if not old_events:
            return

        # 将多条事件合并为一段摘要文本
        summary_parts = []
        for event in old_events:
            summary_parts.append(f"[{event.get('role', '?')}] {event.get('content', '')}")
        summary = "\n".join(summary_parts)

        await self.long_term.store(
            content=summary,
            metadata={
                "type": "compressed_short_term",
                "event_count": len(old_events),
                "earliest_timestamp": old_events[0].get("timestamp", ""),
            },
        )
        logger.info(f"已压缩 {compress_count} 条短期记忆至长期记忆")

    async def get_relevant_context(self, query: str) -> str:
        """
        获取与当前查询相关的综合上下文（短期 + 检索的长期）。

        Args:
            query: 当前任务/问题描述

        Returns:
            格式化的上下文字符串
        """
        top_k = int(os.getenv("LONG_TERM_MEMORY_TOP_K", "5"))
        long_term_results = await self.long_term.search(query, top_k=top_k)

        context_parts = []

        # 添加相关长期记忆
        if long_term_results:
            context_parts.append("【相关历史记忆】")
            for mem in long_term_results:
                context_parts.append(f"- {mem['content'][:200]}")

        # 添加近期短期记忆
        recent = self.short_term.get_recent(n=10)
        if recent:
            context_parts.append("\n【近期对话记录】")
            for event in recent:
                context_parts.append(f"[{event.get('role', '?')}]: {event.get('content', '')}")

        return "\n".join(context_parts)
