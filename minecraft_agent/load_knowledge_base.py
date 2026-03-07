"""
知识库初始化脚本（一次性运行）
将 data/knowledge_base/ 目录下的 .md 文件按文件夹结构加载进 ChromaDB

目录结构示例：
data/knowledge_base/
├── brewing/
│   └── brewing_clean.md
├── enchanting/
│   └── enchanting_clean.md
└── mob/
    ├── 1友好家畜与农场动物-...md
    ├── 2可驯服坐骑与运输动物-...md
    └── ...（11个分类文件）

运行方式: python load_knowledge_base.py
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from loguru import logger

sys.path.insert(0, os.path.dirname(__file__))
load_dotenv()

from rag.md_loader import MarkdownLoader
from rag.retriever import RAGRetriever


# ─────────────────────────────────────────────────────────────────
# 路由策略1（优先）：按文件夹名直接映射 collection
# key = 知识库目录下的文件夹名（完全匹配，不区分大小写）
# value = ChromaDB collection 名称
# ─────────────────────────────────────────────────────────────────
FOLDER_ROUTING: dict[str, str] = {
    "brewing":    "mc_brewing",       # 酿造
    "enchanting": "mc_enchanting",    # 附魔
    "mob":        "mc_mob",           # 所有生物（统一放一个 collection，内部用 metadata 区分）
    "trade":      "mc_trading",       # 交易
    "base":       "mc_base",          # 基础知识
    # 如果以后你新增了文件夹，在这里加一行就行：
    # "crafting":   "mc_crafting",
    # "redstone":   "mc_redstone",
    # "building":   "mc_building",
}

# ─────────────────────────────────────────────────────────────────
# 路由策略2（备用）：mob 文件夹内按文件名数字前缀细分 sub-collection
# 如果你希望每类生物单独一个 collection 而不是全合并，
# 把下面的 USE_MOB_SUBCOLLECTIONS 改为 True
# ─────────────────────────────────────────────────────────────────
USE_MOB_SUBCOLLECTIONS = True   # ← 改 True 则每类生物单独存

MOB_FILE_ROUTING: dict[str, str] = {
    "1友好":  "mc_mob_friendly",      # 鸡、牛、哞菇、猪、兔
    "2可驯":  "mc_mob_tameable",      # 马、驴、骡、骆驼
    "3被动":  "mc_mob_passive",       # 蝙蝠、鳕鱼、发光鱿鱼
    "4村民":  "mc_mob_villager",      # 村民、流浪商人
    "5中立":  "mc_mob_neutral",       # 北极熊、末影人、蜘蛛
    "6常见敌对": "mc_mob_hostile",    # 僵尸、骷髅、苦力怕
    "7下界":  "mc_mob_nether",        # 恶魂、烈焰人、猪灵
    "8末地":  "mc_mob_end",           # 末影龙、末影螨、潜影贝
    "9水生":  "mc_mob_aquatic",       # 守卫者、溺尸
    "10灾厄": "mc_mob_illager",       # 掠夺者、卫道士
    "11boss": "mc_mob_boss",          # 凋灵、铁傀儡（注意：文件名前缀小写匹配）
}

DEFAULT_COLLECTION = "mc_general"


def route_file(folder_name: str, filename: str) -> str:
    """
    决定一个文件应该写入哪个 ChromaDB collection。
    
    优先级：
    1. USE_MOB_SUBCOLLECTIONS=True 且在 mob 文件夹 → 按文件名前缀细分
    2. 文件夹名在 FOLDER_ROUTING 中 → 用文件夹路由
    3. 都不匹配 → DEFAULT_COLLECTION
    
    Args:
        folder_name: 文件所在子文件夹名（如 "mob"、"brewing"）
        filename:    文件名（如 "5中立型生物-北极熊...md"）
    Returns:
        collection 名称字符串
    """
    folder_lower = folder_name.lower()

    # ── mob 细分模式 ────────────────────────────────────────
    if USE_MOB_SUBCOLLECTIONS and folder_lower == "mob":
        file_lower = filename.lower()
        for prefix, collection in MOB_FILE_ROUTING.items():
            if file_lower.startswith(prefix.lower()):
                return collection
        logger.warning(f"mob 文件 '{filename}' 未匹配到细分规则，使用 mc_mob")
        return "mc_mob"

    # ── 文件夹直接映射 ──────────────────────────────────────
    if folder_lower in FOLDER_ROUTING:
        return FOLDER_ROUTING[folder_lower]

    # ── 兜底 ────────────────────────────────────────────────
    logger.warning(f"文件夹 '{folder_name}' 未在 FOLDER_ROUTING 中配置，使用: {DEFAULT_COLLECTION}")
    return DEFAULT_COLLECTION


def scan_knowledge_base(base_path: str) -> list[tuple[str, str, str]]:
    """
    递归扫描知识库目录，返回所有 .md 文件的信息。

    Returns:
        list of (文件绝对路径, 所在文件夹名, 文件名)
        例：[('/path/mob/5中立...md', 'mob', '5中立...md'), ...]
    """
    results = []
    base = Path(base_path)

    if not base.exists():
        logger.error(f"知识库目录不存在: {base_path}")
        logger.info("请创建 data/knowledge_base/ 并放入 .md 文件后重新运行")
        return results

    # 扫描一级子文件夹（brewing/, enchanting/, mob/ 等）
    for folder in sorted(base.iterdir()):
        if folder.is_dir():
            md_files = sorted(folder.glob("*.md"))
            for md_file in md_files:
                results.append((str(md_file), folder.name, md_file.name))

    # 也扫描根目录下的 .md 文件（如果有）
    for md_file in sorted(base.glob("*.md")):
        results.append((str(md_file), "_root", md_file.name))

    return results


def load_all_knowledge(knowledge_base_path: str) -> None:
    """
    主加载函数：扫描所有 .md 文件，按路由规则写入 ChromaDB。
    
    files: list[tuple[str, str, str]] = [('/path/mob/5中立...md', 'mob', '5中立...md'), ...]
    files[0][0]: file_path; files[0][1]: folder_name; files[0][2]: filename;
    folder_name: str = 'mob'
    filename: str = '5中立...md'
    collection_name: str = 'mc_mob'
    
    """
    files = scan_knowledge_base(knowledge_base_path)

    if not files:
        logger.warning("没有找到任何 .md 文件，请检查目录结构")
        return

    logger.info(f"发现 {len(files)} 个 .md 文件")

    loader = MarkdownLoader()
    retriever = RAGRetriever()

    total_chunks = 0
    collection_stats: dict[str, int] = {}
    failed_files: list[str] = []

    for file_path, folder_name, filename in files:
        collection_name = route_file(folder_name, filename)
        logger.info(f"[{folder_name}/{filename}] → collection: {collection_name}")

        try:
            # 按 # 一级标题切块
            chunks = loader.load_and_split(file_path)

            if not chunks:
                logger.warning(f"  文件切块结果为空，检查文件是否有 # 标题: {filename}")
                continue

            logger.info(f"  切块数量: {len(chunks)} 块")

            # 写入 ChromaDB，附带 metadata 方便后续过滤
            retriever.add_documents(
                collection_name=collection_name,
                documents=chunks,
                source=filename,
                # metadata 会存入每个 chunk，检索时可按此过滤
                extra_metadata={
                    "folder": folder_name,
                    "filename": filename,
                }
            )

            total_chunks += len(chunks)
            collection_stats[collection_name] = (
                collection_stats.get(collection_name, 0) + len(chunks)
            )
            logger.success(f"  ✓ 成功写入 {len(chunks)} 块")

        except Exception as e:
            logger.error(f"  ✗ 处理失败: {e}", exc_info=True)
            failed_files.append(filename)

    # ── 汇总报告 ────────────────────────────────────────────
    logger.success("=" * 55)
    logger.success(f"知识库加载完成！共写入 {total_chunks} 个文档块")
    logger.success("各 collection 统计:")
    for col, count in sorted(collection_stats.items()):
        logger.success(f"  {col:<30} {count:>4} 块")
    if failed_files:
        logger.error(f"以下文件处理失败: {failed_files}")
    logger.success("=" * 55)


def verify_collections(retriever: RAGRetriever) -> None:
    """验证各 collection 数据写入情况"""
    logger.info("验证 ChromaDB 数据...")

    # 收集所有可能用到的 collection 名称
    all_collections: set[str] = set(FOLDER_ROUTING.values())
    if USE_MOB_SUBCOLLECTIONS:
        all_collections |= set(MOB_FILE_ROUTING.values())
    all_collections.add(DEFAULT_COLLECTION)

    for col_name in sorted(all_collections):
        try:
            count = retriever.get_collection_count(col_name)
            if count > 0:
                logger.success(f"  {col_name}: {count} 条 ✓")
            else:
                logger.debug(f"  {col_name}: 空（无对应文件，正常）")
        except Exception as e:
            logger.warning(f"  {col_name}: 验证时出错 - {e}")


# ─────────────────────────────────────────────────────────────────
# 可选：一次性测试检索（加载完后验证效果）
# ─────────────────────────────────────────────────────────────────
def test_retrieval(retriever: RAGRetriever) -> None:
    """用几条测试查询验证检索效果"""
    test_cases = [
        ("末影人被注视会怎样",  "mc_mob"),          # 应该在 mob collection 中找到
        ("酿造力量药水需要什么材料", "mc_brewing"),   # 应该在 brewing 中找到
        ("锋利附魔有什么效果",  "mc_enchanting"),    # 应该在 enchanting 中找到
    ]
    logger.info("开始测试检索效果...")
    for query, expected_collection in test_cases:
        try:
            results = retriever.search(query, collection_name=expected_collection, top_k=1)
            if results:
                title = results[0].get("title", "未知")
                score = results[0].get("score", 0)
                logger.success(f'  查询: "{query}"')
                logger.success(f'    → 命中: {title} (相似度: {score:.3f})')
            else:
                logger.warning(f'  查询: "{query}" → 未找到结果')
        except Exception as e:
            logger.warning(f"  测试查询失败: {e}")


if __name__ == "__main__":
    logger.info("=" * 55)
    logger.info("Minecraft Agent 知识库初始化工具")
    logger.info(f"生物细分模式: {'开启' if USE_MOB_SUBCOLLECTIONS else '关闭（全部写入 mc_mob）'}")
    logger.info("=" * 55)

    # 知识库路径（可通过 .env 的 KNOWLEDGE_BASE_PATH 覆盖）
    knowledge_path = os.getenv("KNOWLEDGE_BASE_PATH", "./data/knowledge_base")
    script_dir = os.path.dirname(os.path.abspath(__file__))
    knowledge_path = os.path.join(script_dir, knowledge_path.lstrip("./"))

    logger.info(f"知识库路径: {knowledge_path}")

    # 加载
    load_all_knowledge(knowledge_path)

    # 验证
    retriever = RAGRetriever()
    verify_collections(retriever)

    # 测试检索（可选，确认效果）
    # test_retrieval(retriever)