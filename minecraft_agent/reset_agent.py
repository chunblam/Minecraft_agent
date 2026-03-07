#!/usr/bin/env python3
"""
Agent 重置工具

用于清空技能库、长期记忆等持久化数据，在架构或数据格式变更后重新开始。

用法:
  python reset_agent.py              # 仅重置 Agent 状态（技能库 + 长期记忆），保留知识库
  python reset_agent.py --full       # 完全重置（删除整个 ChromaDB），需重新运行 load_knowledge_base.py
  python reset_agent.py --skills     # 仅清空技能库
  python reset_agent.py --memory     # 仅清空长期记忆
"""

import os
import sys
import shutil
import argparse

# 确保项目根目录在 path 中
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

from dotenv import load_dotenv
load_dotenv()


def get_chroma_path() -> str:
    chroma_path = os.getenv("CHROMA_DB_PATH", "./data/chroma_db")
    if not os.path.isabs(chroma_path):
        chroma_path = os.path.join(script_dir, chroma_path.lstrip("./"))
    return chroma_path


def reset_agent_only() -> None:
    """仅重置 Agent 状态：技能库 + 长期记忆，保留知识库 embeddings"""
    import chromadb

    chroma_path = get_chroma_path()
    if not os.path.isdir(chroma_path):
        print(f"[OK] ChromaDB 目录不存在，无需重置: {chroma_path}")
        return

    client = chromadb.PersistentClient(path=chroma_path)

    for name in ["mc_skills", "mc_long_term_memory"]:
        try:
            client.delete_collection(name)
            print(f"[OK] 已删除 collection: {name}")
        except Exception as e:
            if "does not exist" in str(e).lower() or "not found" in str(e).lower():
                print(f"[--] collection 不存在，跳过: {name}")
            else:
                print(f"[!!] 删除 {name} 失败: {e}")

    print("\n[完成] Agent 状态已重置（技能库 + 长期记忆）。知识库未改动。")


def reset_skills_only() -> None:
    """仅清空技能库"""
    import chromadb

    chroma_path = get_chroma_path()
    client = chromadb.PersistentClient(path=chroma_path)
    try:
        client.delete_collection("mc_skills")
        print("[OK] 技能库已清空")
    except Exception as e:
        if "does not exist" in str(e).lower():
            print("[--] 技能库 collection 不存在")
        else:
            print(f"[!!] 失败: {e}")


def reset_memory_only() -> None:
    """仅清空长期记忆"""
    import chromadb

    chroma_path = get_chroma_path()
    client = chromadb.PersistentClient(path=chroma_path)
    try:
        client.delete_collection("mc_long_term_memory")
        print("[OK] 长期记忆已清空")
    except Exception as e:
        if "does not exist" in str(e).lower():
            print("[--] 长期记忆 collection 不存在")
        else:
            print(f"[!!] 失败: {e}")


def reset_full() -> None:
    """完全重置：删除整个 ChromaDB 目录"""
    chroma_path = get_chroma_path()
    if not os.path.isdir(chroma_path):
        print(f"[OK] ChromaDB 目录不存在: {chroma_path}")
        return

    shutil.rmtree(chroma_path)
    print(f"[OK] 已删除 ChromaDB 目录: {chroma_path}")
    print("\n[重要] 请重新运行以下命令加载知识库:")
    print("  python load_knowledge_base.py")


def main() -> None:
    parser = argparse.ArgumentParser(description="Minecraft Agent 重置工具")
    parser.add_argument(
        "--full",
        action="store_true",
        help="完全重置（删除整个 ChromaDB），需重新运行 load_knowledge_base.py",
    )
    parser.add_argument(
        "--skills",
        action="store_true",
        help="仅清空技能库",
    )
    parser.add_argument(
        "--memory",
        action="store_true",
        help="仅清空长期记忆",
    )
    args = parser.parse_args()

    if args.full:
        reset_full()
    elif args.skills:
        reset_skills_only()
    elif args.memory:
        reset_memory_only()
    else:
        reset_agent_only()


if __name__ == "__main__":
    main()
