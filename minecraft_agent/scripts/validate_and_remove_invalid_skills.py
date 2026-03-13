#!/usr/bin/env python3
"""
校验技能库中每条技能的 code 是否可安全注入执行（括号平衡、非截断）。
列出无效技能并可选择删除，用于修复「Unexpected token ')'」等因截断技能导致的卡住问题。

用法（在 minecraft_agent 目录下）：
  python scripts/validate_and_remove_invalid_skills.py              # 仅列出无效技能
  python scripts/validate_and_remove_invalid_skills.py --delete      # 列出并删除所有无效技能
  python scripts/validate_and_remove_invalid_skills.py --delete craftWorkbench  # 仅删除指定名称
  python scripts/validate_and_remove_invalid_skills.py --list        # 列出所有技能及其有效性
"""

import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

os.chdir(BASE)


def is_valid_skill_code(code: str) -> bool:
    """与 agent.skill_library.is_valid_skill_code 一致，避免导入整包依赖。"""
    if not code or not code.strip():
        return False
    s = code.strip()
    if "async" not in s or "function" not in s:
        return False
    if s.count("{") != s.count("}"):
        return False
    if "}" not in s:
        return False
    return True


def list_skills_with_codes(skill_db_path: str) -> list[dict]:
    """返回 [{"name", "description", "code", ...}, ...]。"""
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
    out = []
    for meta in (data.get("metadatas") or []):
        if not meta:
            continue
        try:
            sk = json.loads(meta.get("json", "{}"))
            if sk.get("name") or sk.get("code"):
                out.append(sk)
        except Exception:
            continue
    return out


def delete_skill_by_name(skill_db_path: str, name: str) -> bool:
    """从 ChromaDB 删除指定名称的技能。"""
    try:
        import chromadb
    except ImportError:
        return False
    if not os.path.isdir(skill_db_path):
        return False
    client = chromadb.PersistentClient(path=skill_db_path)
    coll = client.get_or_create_collection("skills_v3")
    try:
        coll.delete(ids=[name])
        return True
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(
        description="校验技能 code 有效性，列出或删除无效技能（如截断导致 Unexpected token ')'）"
    )
    parser.add_argument(
        "name",
        nargs="?",
        help="可选：仅针对该技能名称执行（与 --delete 合用即只删该条）",
    )
    parser.add_argument(
        "--db",
        default="./data/skill_db",
        help="ChromaDB 路径，默认 ./data/skill_db",
    )
    parser.add_argument(
        "--list",
        "-l",
        action="store_true",
        help="列出所有技能及其有效性（valid/invalid）",
    )
    parser.add_argument(
        "--delete",
        "-d",
        action="store_true",
        help="删除无效技能（无 name 时删全部无效，有 name 时仅删该条）",
    )
    args = parser.parse_args()

    skill_db_path = os.path.abspath(args.db)
    skills = list_skills_with_codes(skill_db_path)

    if not skills:
        print("当前技能库为空或未找到 ChromaDB。")
        return

    invalid = [sk for sk in skills if not is_valid_skill_code(sk.get("code", ""))]
    valid = [sk for sk in skills if is_valid_skill_code(sk.get("code", ""))]

    if args.list:
        print("当前技能及有效性：")
        for sk in skills:
            name = sk.get("name", "unnamed")
            ok = is_valid_skill_code(sk.get("code", ""))
            status = "valid" if ok else "invalid"
            print(f"  [{status}] {name}")
        return

    if args.name and args.delete:
        name = args.name.strip()
        sk = next((s for s in skills if s.get("name") == name), None)
        if not sk:
            print(f"未找到技能: {name}")
            sys.exit(1)
        if is_valid_skill_code(sk.get("code", "")):
            print(f"技能 {name} 有效，未删除。若仍要删除请使用 scripts/remove_skill.py {name}")
            return
        if delete_skill_by_name(skill_db_path, name):
            print(f"已删除无效技能: {name}")
        else:
            print(f"删除失败: {name}")
            sys.exit(1)
        return

    if args.delete and not args.name:
        if not invalid:
            print("未发现无效技能，无需删除。")
            return
        print(f"以下 {len(invalid)} 条技能 code 无效，将删除：")
        for sk in invalid:
            print(f"  - {sk.get('name', 'unnamed')}")
        for sk in invalid:
            name = sk.get("name", "unnamed")
            if delete_skill_by_name(skill_db_path, name):
                print(f"已删除: {name}")
            else:
                print(f"删除失败: {name}")
        return

    # 默认：仅列出无效
    if invalid:
        print(f"发现 {len(invalid)} 条无效技能（会导致注入后解析报错）：")
        for sk in invalid:
            print(f"  - {sk.get('name', 'unnamed')}")
        print("使用 --delete 删除全部无效技能，或 --delete <name> 删除指定技能。")
    else:
        print("所有技能 code 均有效。")


if __name__ == "__main__":
    main()
