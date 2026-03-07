"""
RAG 检索模块（LLM 语义分类版）

架构说明：
- EmbeddingClient   : 调用硅基流动 BAAI/bge-m3 API，将文本转为向量
- QueryClassifier   : 调用硅基流动 DeepSeek-V3，对用户查询语义分类，
                      确定应该搜索哪些 ChromaDB collection
- RAGRetriever      : 智能路由检索器
                      ① 若外部指定 collection_name → 直接搜索（兼容旧调用）
                      ② 否则调用 QueryClassifier 分类 → 并行搜索相关 collection
                         → 合并结果按相关度排序

【替换了旧版关键词路由的原因】
关键词匹配对语义近似但用词不同的查询（如"苦力怕怎么处理"匹配不到"战斗"关键词）
容易漏检；LLM 分类能理解意图，选出最合适的 collection 再做向量检索。
"""

import os
import json
import asyncio
import collections
from openai import AsyncOpenAI
import chromadb
from loguru import logger

from .md_loader import DocumentChunk


# ─────────────────────────────────────────────────────────────────────────────
# 默认兜底 collection（LLM 分类失败或未匹配时使用）
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_COLLECTION = "mc_general"

# ─────────────────────────────────────────────────────────────────────────────
# 知识库 Collection 目录表
# key   : ChromaDB collection 名称（与 load_knowledge_base.py 中保持一致）
# value : 该 collection 的内容描述（LLM 分类时参考此描述选择 collection）
# ─────────────────────────────────────────────────────────────────────────────
COLLECTION_CATALOG: dict[str, str] = {
    "mc_base": (
        "基础知识：游戏基础机制、游戏基础操作、游戏基础物品、游戏基础方块、游戏基础生物、游戏基础事件、游戏基础机制、游戏基础操作、游戏基础物品、游戏基础方块、游戏基础生物、游戏基础事件"
    ),
    "mc_brewing": (
        "酿造系统：药水配方、酿造台使用方法、各类原材料（发酵蜘蛛眼、烈焰粉、魔法粉等）"
        "、所有药水效果说明、喷溅药水、滞留药水"
    ),
    "mc_enchanting": (
        "附魔系统：附魔台使用、铁砧操作、附魔书获取途径、各类附魔名称与详细效果说明"
        "、经验值机制、最优附魔组合"
    ),
    "mc_trading": (
        "交易系统：村民交易机制、村民职业与工作台、各职业交易表、交易升级"
        "、绿宝石货币、流浪商人交易"
    ),
    "mc_mob_friendly": (
        "友好生物（被动型家畜与农场动物）：鸡、牛、哞菇、猪、兔子、绵羊、山羊"
        "、美西螈、蜜蜂、海豚、熊猫、行商羊驼、狼、青蛙"
        "——繁殖、掉落物、驯服、互动方式"
    ),
    "mc_mob_tameable": (
        "可驯服坐骑与运输动物：马、驴、骡、骆驼、骷髅马、僵尸马、羊驼、炽足兽"
        "——驯服方法、骑乘操作、装备鞍与马铠、箱子装载"
    ),
    "mc_mob_passive": (
        "被动型环境生物（无攻击性）：蝙蝠、鳕鱼、发光鱿鱼、鲑鱼、鱿鱼、蝌蚪"
        "、狐狸、鹦鹉、猫、豹猫、海龟、嗅探兽、悦灵、热带鱼、犰狳"
        "——刷新条件、掉落物、行为特征"
    ),
    "mc_mob_villager": (
        "村民 NPC 相关：村民行为逻辑、交易系统、村民繁殖与床位需求"
        "、村民职业、僵尸村民治疗、流浪商人"
    ),
    "mc_mob_neutral": (
        "中立型生物（平时不攻击、被激惹后反击）：北极熊、末影人（对视激怒）"
        "、蜘蛛、洞穴蜘蛛——触发条件、应对方法、掉落物"
    ),
    "mc_mob_hostile": (
        "常见敌对生物（主动攻击玩家）：僵尸、僵尸村民、骷髅、苦力怕（爬行者）"
        "、女巫、史莱姆、尸壳、骆驼尸壳、流浪者、蠹虫"
        "——AI行为、伤害、掉落物、防御与击杀技巧"
    ),
    "mc_mob_nether": (
        "下界维度生物：恶魂、快乐恶魂、烈焰人、岩浆怪、疣猪兽、僵尸疣猪兽"
        "、猪灵、僵尸猪灵、猪灵蛮兵、凋灵骷髅、嘎枝、焦骸、沼骸、旋风人"
        "——下界攻略、各生物特性与应对"
    ),
    "mc_mob_end": (
        "末地维度生物：末影龙（终极Boss、击杀方法）、末影螨、潜影贝"
        "——末地攻略、传送门激活、龙蛋获取"
    ),
    "mc_mob_aquatic": (
        "水生敌对生物：守卫者、远古守卫者（海底神殿攻略）、溺尸"
        "、鹦鹉螺、僵尸鹦鹉螺——水下战斗技巧、掉落物"
    ),
    "mc_mob_illager": (
        "灾厄村民与袭击型生物：掠夺者、卫道士、唤魔者、劫掠兽、恼鬼"
        "、监守者（深暗之境）、幻翼——袭击事件机制、不祥之兆、应对策略"
    ),
    "mc_mob_boss": (
        "Boss 与特殊构造体：凋灵（Boss生成与击杀）、铁傀儡（村庄守卫）"
        "、铜傀儡、雪傀儡、盔甲架——召唤方法、攻击模式、掉落物"
    ),
    "mc_general": (
        "通用游戏知识、游戏基础机制，当以上所有分类都不适合时使用此兜底分类"
    ),
}

# ─────────────────────────────────────────────────────────────────────────────
# QueryClassifier 使用的 System Prompt
# {catalog} 占位符在运行时替换为上方目录表的文本版
# ─────────────────────────────────────────────────────────────────────────────
_CLASSIFY_SYSTEM_PROMPT = """\
你是 Minecraft 游戏知识库的检索路由助手。
根据玩家的提问，根据Minecraft游戏的知识理解，识别出玩家的询问意图，并从下列知识库分类中选出最相关的 1~3 个，按相关度从高到低排列。

知识库分类：
{catalog}

【输出规则】
- 只输出一个 JSON 数组，元素为 collection 名称字符串
- 按相关度从高到低排列，最多 3 个
- 若有多个明显相关的分类，都可以选入
- 不输出任何解释文字，只输出 JSON 数组

示例输出：
["mc_mob_hostile", "mc_mob_neutral"]
"""


# ═════════════════════════════════════════════════════════════════════════════
# EmbeddingClient
# ═════════════════════════════════════════════════════════════════════════════
class EmbeddingClient:
    """
    硅基流动 BAAI/bge-m3 Embedding API 客户端。
    使用 OpenAI SDK 格式调用（兼容接口），将文本转为浮点数向量。

    内置 LRU 缓存（上限 512 条），相同文本重复调用时直接返回缓存向量，
    跳过 API 请求，显著降低技能搜索和 RAG 检索的延迟。
    """

    _CACHE_MAX_SIZE = 512

    def __init__(self) -> None:
        api_key = os.getenv("SILICONFLOW_API_KEY", "")
        base_url = os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")
        self.model = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")

        if not api_key:
            logger.warning("SILICONFLOW_API_KEY 未设置，向量化功能将失败")

        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)

        # LRU 缓存：text → embedding vector
        self._cache: collections.OrderedDict[str, list[float]] = collections.OrderedDict()

    async def embed(self, text: str) -> list[float]:
        """
        将单条文本向量化，命中缓存时直接返回。

        Args:
            text: 输入文本（建议 512 token 以内）

        Returns:
            浮点数向量，失败时返回空列表
        """
        cache_key = text.strip()

        if cache_key in self._cache:
            self._cache.move_to_end(cache_key)
            logger.debug(f"Embedding 缓存命中: {cache_key[:40]!r}")
            return self._cache[cache_key]

        try:
            response = await self.client.embeddings.create(
                model=self.model,
                input=text,
                encoding_format="float",
            )
            vector = response.data[0].embedding

            # 写缓存，超容量时淘汰最旧条目
            if len(self._cache) >= self._CACHE_MAX_SIZE:
                self._cache.popitem(last=False)
            self._cache[cache_key] = vector
            return vector

        except Exception as e:
            logger.error(f"Embedding API 调用失败: {e}", exc_info=True)
            return []

    async def embed_batch(self, texts: list[str], batch_size: int = 32) -> list[list[float]]:
        """
        批量向量化文本，自动按 batch_size 分批处理。

        Returns:
            与输入列表等长的向量列表；单条失败时对应位置为空列表
        """
        all_embeddings: list[list[float]] = []

        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            try:
                response = await self.client.embeddings.create(
                    model=self.model,
                    input=batch,
                    encoding_format="float",
                )
                # API 不保证返回顺序，按 index 排序后再取值
                sorted_data = sorted(response.data, key=lambda x: x.index)
                all_embeddings.extend([d.embedding for d in sorted_data])
                logger.debug(f"批次 {i // batch_size + 1}: 向量化 {len(batch)} 条成功")
            except Exception as e:
                logger.error(f"批量 Embedding 失败 (batch {i // batch_size + 1}): {e}")
                all_embeddings.extend([[] for _ in batch])

            # 批次间短暂等待，避免触发 API 限速
            if i + batch_size < len(texts):
                await asyncio.sleep(0.1)

        return all_embeddings


# ═════════════════════════════════════════════════════════════════════════════
# QueryClassifier
# ═════════════════════════════════════════════════════════════════════════════
class QueryClassifier:
    """
    LLM 驱动的查询语义分类器。

    工作流程：
    1. 将 COLLECTION_CATALOG 格式化为文本描述交给 DeepSeek-V3
    2. 模型返回最相关的 collection 名称列表（JSON 数组）
    3. 校验返回值是否在 COLLECTION_CATALOG 中，过滤无效项
    4. 内置简单 LRU 风格缓存，相同查询不重复调用 API

    失败回退策略：
    - API 调用失败 → 返回 [DEFAULT_COLLECTION]
    - JSON 解析失败 → 返回 [DEFAULT_COLLECTION]
    - 返回的 collection 名全部无效 → 返回 [DEFAULT_COLLECTION]
    """

    # 缓存容量上限，超出时删除最早的条目
    _CACHE_MAX_SIZE = 200

    def __init__(self) -> None:
        # 优先使用 SILICONFLOW_API_KEY，兼容旧的 DEEPSEEK_API_KEY
        api_key = os.getenv("SILICONFLOW_API_KEY") or os.getenv("DEEPSEEK_API_KEY", "")
        base_url = os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")
        # 分类任务专用模型，默认使用 DeepSeek-V3（快速、成本低）
        self.model = os.getenv("RAG_CLASSIFY_MODEL", "deepseek-ai/DeepSeek-V3")

        if not api_key:
            logger.warning("未找到 SILICONFLOW_API_KEY，QueryClassifier 将无法工作")

        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)

        # 构建 system prompt（只需构建一次）
        catalog_lines = [f"- {name}: {desc}" for name, desc in COLLECTION_CATALOG.items()]
        catalog_str = "\n".join(catalog_lines)
        self._system_prompt = _CLASSIFY_SYSTEM_PROMPT.format(catalog=catalog_str)

        # 简单内存缓存：{ 标准化查询文本 → [collection, ...] }
        self._cache: dict[str, list[str]] = {}

        logger.debug(f"QueryClassifier 初始化，分类模型: {self.model}")

    async def classify(self, query: str, max_collections: int = 3) -> list[str]:
        """
        对用户查询进行语义分类，返回最相关的 collection 名称列表。

        Args:
            query: 用户的原始问题或指令
            max_collections: 最多返回多少个 collection（默认 3）

        Returns:
            按相关度降序排列的 collection 名称列表，至少含一个元素
        """
        cache_key = query.strip().lower()

        # 缓存命中，直接返回
        if cache_key in self._cache:
            logger.debug(f"分类缓存命中: {query[:40]!r}")
            return self._cache[cache_key]

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self._system_prompt},
                    {"role": "user", "content": f"玩家提问：{query}"},
                ],
                temperature=0.1,   # 极低温度，让输出尽量稳定确定
                max_tokens=128,    # 只需返回一个短 JSON 数组，无需多 token
                stream=False,
            )
            raw = response.choices[0].message.content or ""
            logger.debug(f"分类原始响应: {raw[:120]}")

            collections = self._parse_response(raw, max_collections)

        except Exception as e:
            logger.error(f"QueryClassifier API 调用失败，回退到 {DEFAULT_COLLECTION}: {e}")
            collections = [DEFAULT_COLLECTION]

        # 写缓存，超容量时淘汰最老的条目
        if len(self._cache) >= self._CACHE_MAX_SIZE:
            oldest_key = next(iter(self._cache))
            del self._cache[oldest_key]
        self._cache[cache_key] = collections

        logger.info(f"查询分类结果: {query[:50]!r} → {collections}")
        return collections

    def _parse_response(self, raw: str, max_n: int) -> list[str]:
        """
        从 LLM 原始输出中解析 JSON 数组，校验并截断到 max_n 个。

        支持：
        - 纯 JSON 数组：["mc_brewing", "mc_enchanting"]
        - markdown 代码块包裹的 JSON
        - 输出前后有多余空白行
        """
        cleaned = raw.strip()

        # 去除可能的 ```json ... ``` 或 ``` ... ``` 包裹
        if "```" in cleaned:
            lines = cleaned.split("\n")
            inner = [ln for ln in lines if not ln.strip().startswith("```")]
            cleaned = "\n".join(inner).strip()

        # 定位 JSON 数组的起止位置（防止 LLM 在数组前后附加说明文字）
        start = cleaned.find("[")
        end = cleaned.rfind("]") + 1
        if start == -1 or end <= 0:
            logger.warning(f"响应中找不到 JSON 数组: {raw[:100]!r}")
            return [DEFAULT_COLLECTION]

        try:
            parsed: list = json.loads(cleaned[start:end])
        except json.JSONDecodeError as e:
            logger.error(f"分类响应 JSON 解析失败: {e} | raw={raw[:100]!r}")
            return [DEFAULT_COLLECTION]

        # 过滤掉不在 COLLECTION_CATALOG 中的无效名称
        valid = [c for c in parsed if isinstance(c, str) and c in COLLECTION_CATALOG]
        if not valid:
            logger.warning(f"分类返回的 collection 全部无效: {parsed} | 回退到 {DEFAULT_COLLECTION}")
            return [DEFAULT_COLLECTION]

        return valid[:max_n]

    def clear_cache(self) -> None:
        """清空分类缓存（用于测试或知识库更新后重置）"""
        self._cache.clear()
        logger.info("QueryClassifier 缓存已清空")


# ═════════════════════════════════════════════════════════════════════════════
# RAGRetriever
# ═════════════════════════════════════════════════════════════════════════════
class RAGRetriever:
    """
    RAG 路由检索器（LLM 语义分类版）。

    search() 方法工作流：
    ┌─────────────────────────────────────────────────────────┐
    │ search(query, collection_name=None)                     │
    │   ├── collection_name 已指定 → 直接向量检索（兼容旧调用）│
    │   └── collection_name 为 None                          │
    │         ↓                                              │
    │       QueryClassifier.classify(query)                  │
    │         ↓ 返回 [col1, col2, ...]                       │
    │       并行对每个 collection 做向量检索                  │
    │         ↓                                              │
    │       合并结果，按 score 降序排序，取 top_k             │
    └─────────────────────────────────────────────────────────┘
    """

    def __init__(self) -> None:
        chroma_path = os.getenv("CHROMA_DB_PATH", "./data/chroma_db")
        # 将相对路径转为绝对路径（相对于项目根目录）
        if not os.path.isabs(chroma_path):
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            chroma_path = os.path.join(base_dir, chroma_path.lstrip("./"))

        self.chroma_client = chromadb.PersistentClient(path=chroma_path)
        self.embedder = EmbeddingClient()
        self.classifier = QueryClassifier()
        logger.debug(f"RAGRetriever 初始化，ChromaDB 路径: {chroma_path}")

    # ──────────────────────────────────────────────────────────────────────
    # 主检索接口
    # ──────────────────────────────────────────────────────────────────────
    async def search(
        self,
        query: str,
        top_k: int = 5,
        collection_name: str | None = None,
    ) -> list[dict]:
        """
        语义检索入口。

        Args:
            query: 用户查询文本
            top_k: 最终返回结果数量
            collection_name: 若指定则直接搜该 collection（跳过 LLM 分类）

        Returns:
            结果列表，每条字典包含：
            {
                "content": str,          # 文档块原文
                "title": str,            # 所在标题
                "source": str,           # 来源文件名
                "score": float,          # 相似度分数（0~1，越高越相关）
                "collection": str,       # 来源 collection 名称
            }
        """
        if collection_name:
            # 外部直接指定 collection，跳过分类（用于测试/调试）
            return await self._search_one_collection(query, collection_name, top_k)

        # LLM 语义分类
        target_collections = await self.classifier.classify(query)

        if len(target_collections) == 1:
            # 单 collection，直接搜索
            return await self._search_one_collection(query, target_collections[0], top_k)

        # 多 collection 并行搜索，结果合并后取 top_k
        return await self._search_multi_collections(query, target_collections, top_k)

    async def _search_one_collection(
        self, query: str, collection_name: str, top_k: int
    ) -> list[dict]:
        """
        在单个 collection 中执行向量相似度检索。

        当 collection 为空时，尝试回退到 DEFAULT_COLLECTION；
        若 DEFAULT_COLLECTION 也为空则返回空列表。
        """
        try:
            collection = self.chroma_client.get_or_create_collection(name=collection_name)

            if collection.count() == 0:
                logger.debug(f"Collection '{collection_name}' 为空")
                # 若不是通用 collection 则尝试回退
                if collection_name != DEFAULT_COLLECTION:
                    logger.debug(f"回退到 {DEFAULT_COLLECTION}")
                    return await self._search_one_collection(query, DEFAULT_COLLECTION, top_k)
                return []

            embedding = await self.embedder.embed(query)
            if not embedding:
                logger.warning("向量化失败，无法执行检索")
                return []

            results = collection.query(
                query_embeddings=[embedding],
                n_results=min(top_k, collection.count()),
                include=["documents", "metadatas", "distances"],
            )

            docs = results.get("documents", [[]])[0]
            metas = results.get("metadatas", [[]])[0]
            distances = results.get("distances", [[]])[0]

            return [
                {
                    "content": doc,
                    "title": meta.get("title", ""),
                    "source": meta.get("source", ""),
                    "score": round(1.0 - float(dist), 4),   # 距离转相似度
                    "collection": collection_name,
                }
                for doc, meta, dist in zip(docs, metas, distances)
            ]

        except Exception as e:
            logger.error(f"向量检索失败 (collection={collection_name}): {e}", exc_info=True)
            return []

    async def _search_multi_collections(
        self, query: str, collections: list[str], top_k: int
    ) -> list[dict]:
        """
        并行搜索多个 collection，合并后按 score 降序返回 top_k 条结果。

        使用 asyncio.gather 并行发出多个检索请求，总耗时约等于单次检索耗时。
        """
        tasks = [
            self._search_one_collection(query, col, top_k)
            for col in collections
        ]
        results_list = await asyncio.gather(*tasks, return_exceptions=True)

        merged: list[dict] = []
        for r in results_list:
            if isinstance(r, list):
                merged.extend(r)
            else:
                logger.warning(f"某个 collection 检索出现异常: {r}")

        # 按相似度分数降序排序，截取 top_k
        merged.sort(key=lambda x: x.get("score", 0.0), reverse=True)
        return merged[:top_k]

    # ──────────────────────────────────────────────────────────────────────
    # 写入接口（供 load_knowledge_base.py 调用）
    # ──────────────────────────────────────────────────────────────────────
    def add_documents(
        self,
        collection_name: str,
        documents: list[DocumentChunk],   # 注意：参数名为 documents（与旧版 chunks 不同）
        source: str = "",
        extra_metadata: dict | None = None,
    ) -> None:
        """
        将文档块批量向量化后写入指定 collection（同步方法）。

        因为 load_knowledge_base.py 作为普通脚本（非 async）运行，
        此处用 asyncio.new_event_loop() 在同步上下文中执行异步向量化。

        Args:
            collection_name : 目标 ChromaDB collection 名称
            documents       : DocumentChunk 列表（来自 md_loader.py）
            source          : 来源标识字符串（写入每个 chunk 的 metadata）
            extra_metadata  : 额外 metadata 字段，合并到每个 chunk 的 metadata 中
        """
        if not documents:
            return

        collection = self.chroma_client.get_or_create_collection(name=collection_name)

        # 在新事件循环中同步执行批量向量化
        loop = asyncio.new_event_loop()
        try:
            texts = [chunk.content for chunk in documents]
            embeddings = loop.run_until_complete(self.embedder.embed_batch(texts))
        finally:
            loop.close()

        # 过滤向量化失败的块（embedding 为空列表表示失败）
        valid_items = [
            (chunk, emb)
            for chunk, emb in zip(documents, embeddings)
            if emb
        ]

        if not valid_items:
            logger.warning(f"全部文档块向量化失败，跳过写入 '{collection_name}'")
            return

        # 合并 chunk 自带 metadata 与 extra_metadata
        extra = extra_metadata or {}
        ids, docs, embs, metas = [], [], [], []
        for chunk, emb in valid_items:
            ids.append(chunk.chunk_id)
            docs.append(chunk.content)
            embs.append(emb)
            merged_meta = {**chunk.metadata, **extra}
            if source:
                merged_meta["source"] = source
            metas.append(merged_meta)

        # upsert：重复 chunk_id 时覆盖更新（支持重复运行脚本）
        collection.upsert(
            ids=ids,
            embeddings=embs,
            documents=docs,
            metadatas=metas,
        )
        logger.debug(f"写入 {len(valid_items)}/{len(documents)} 块 → '{collection_name}'")

    # ──────────────────────────────────────────────────────────────────────
    # 工具方法
    # ──────────────────────────────────────────────────────────────────────
    def get_collection_count(self, collection_name: str) -> int:
        """获取指定 collection 的文档数量，不存在时返回 0"""
        try:
            collection = self.chroma_client.get_collection(name=collection_name)
            return collection.count()
        except Exception:
            return 0

    def get_all_collection_stats(self) -> dict[str, int]:
        """
        返回所有已知 collection 的文档数量统计。
        用于 load_knowledge_base.py 的验证步骤。
        """
        stats = {}
        for col_name in COLLECTION_CATALOG:
            count = self.get_collection_count(col_name)
            if count > 0:
                stats[col_name] = count
        return stats
