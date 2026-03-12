#!/usr/bin/env python3
"""
按名称删除技能库中的一条技能。用于人工复核后剔除「Critic 通过但实际效果不佳」的技能。

用法（在 minecraft_agent 目录下）：
  python scripts/remove_skill.py "chop_wood"
  python scripts/remove_skill.py --list   # 先列出所有技能名称
"""

import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

os.chdir(BASE)


def list_skills(skill_db_path: str) -> list[str]:
    """返回当前技能库中所有技能名称。"""
    try:
        import chromadb
    except ImportError:
        return []

    if not os.path.isdir(skill_db_path):
        return []

    client = chromadb.PersistentClient(path=skill_db_path)
    coll = client.get_or_create_collection("skills_v3")
    if coll.count() == 0:
        return []

    data = coll.get(include=["metadatas"])
    names = []
    for meta in (data.get("metadatas") or []):
        if not meta:
            continue
        try:
            sk = json.loads(meta.get("json", "{}"))
            name = sk.get("name")
            if name:
                names.append(name)
        except Exception:
            continue
    return sorted(names)


def delete_skill(skill_db_path: str, name: str) -> bool:
    """从 ChromaDB 删除指定名称的技能。返回是否删除成功。"""
    try:
        import chromadb
    except ImportError:
        print("ChromaDB 未安装")
        return False

    if not os.path.isdir(skill_db_path):
        print(f"技能库目录不存在: {skill_db_path}")
        return False

    client = chromadb.PersistentClient(path=skill_db_path)
    coll = client.get_or_create_collection("skills_v3")
    try:
        coll.delete(ids=[name])
        print(f"已删除技能: {name}")
        return True
    except Exception as e:
        print(f"删除失败（可能不存在该 id）: {e}")
        return False


def delete_all_skills(skill_db_path: str) -> int:
    """删除技能库中全部技能。返回删除条数。"""
    try:
        import chromadb
    except ImportError:
        print("ChromaDB 未安装")
        return 0

    if not os.path.isdir(skill_db_path):
        print(f"技能库目录不存在: {skill_db_path}")
        return 0

    names = list_skills(skill_db_path)
    if not names:
        print("当前无技能，无需删除。")
        return 0

    client = chromadb.PersistentClient(path=skill_db_path)
    coll = client.get_or_create_collection("skills_v3")
    try:
        coll.delete(ids=names)
        print(f"已删除全部技能，共 {len(names)} 条: {', '.join(names)}")
        return len(names)
    except Exception as e:
        print(f"批量删除失败: {e}")
        return 0


def main():
    parser = argparse.ArgumentParser(description="按名称删除一条技能，或删除全部技能")
    parser.add_argument("name", nargs="?", help="技能名称（与存储时的 name 一致）")
    parser.add_argument("--list", "-l", action="store_true", help="列出所有技能名称后退出")
    parser.add_argument("--all", "-a", action="store_true", help="删除全部技能（会清空技能库）")
    parser.add_argument("--db", default="./data/skill_db", help="ChromaDB 路径")
    args = parser.parse_args()

    skill_db_path = os.path.abspath(args.db)

    if args.list:
        names = list_skills(skill_db_path)
        if not names:
            print("当前无技能或未找到 ChromaDB。")
            return
        print("当前技能名称（可用于 remove_skill.py <name>）：")
        for n in names:
            print(f"  {n}")
        return

    if args.all:
        n = delete_all_skills(skill_db_path)
        sys.exit(0 if n >= 0 else 1)

    if not args.name or not args.name.strip():
        print("请提供技能名称，或使用 --list 查看列表，或 --all 删除全部。")
        print("示例: python scripts/remove_skill.py chop_wood")
        print("      python scripts/remove_skill.py --all")
        sys.exit(1)

    ok = delete_skill(skill_db_path, args.name.strip())
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
