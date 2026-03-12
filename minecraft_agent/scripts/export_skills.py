#!/usr/bin/env python3
"""
将 ChromaDB / 内存技能库导出为可读的文件夹：每个技能一个 .js（代码）和一个 .json（元信息）。
便于人工查看、备份和复用。

- 运行 Agent（main.py）时，默认会在启动时自动导出当前技能到 data/skill_db_export，便于快速查验；
  可通过环境变量 EXPORT_SKILLS_ON_START=0 关闭自动导出。
- 也可手动执行本脚本导出或指定目录。

用法（在 minecraft_agent 目录下）：
  python scripts/export_skills.py
  python scripts/export_skills.py --out ./data/skill_db_export
  python scripts/export_skills.py --db ./data/skill_db
"""

import argparse
import json
import os
import re
import sys

# 保证可导入 agent
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

os.chdir(BASE)


def safe_filename(name: str) -> str:
    """将技能名转为安全文件名（无路径、无特殊字符）"""
    s = re.sub(r'[^\w\-.]', '_', name).strip() or "unnamed"
    return s[:120]


def export_from_chroma(skill_db_path: str, export_dir: str) -> int:
    """从 ChromaDB 导出到 export_dir。返回导出条数。"""
    try:
        import chromadb
    except ImportError:
        print("ChromaDB 未安装，无法导出。请 pip install chromadb")
        return 0

    client = chromadb.PersistentClient(path=skill_db_path)
    coll = client.get_or_create_collection("skills_v3")
    n = coll.count()
    if n == 0:
        print(f"技能库为空（{skill_db_path}）")
        return 0

    data = coll.get(include=["metadatas"])
    os.makedirs(export_dir, exist_ok=True)
    count = 0
    for meta in (data.get("metadatas") or []):
        if not meta:
            continue
        try:
            doc = meta.get("json", "{}")
            sk = json.loads(doc)
            name = sk.get("name", "unnamed")
            code = sk.get("code", "")
            safe = safe_filename(name)
            # 写入 .js（仅代码，便于直接复用）
            js_path = os.path.join(export_dir, f"{safe}.js")
            with open(js_path, "w", encoding="utf-8") as f:
                f.write(code if code else "// (empty)\n")
            # 写入 .json（完整元信息）
            json_path = os.path.join(export_dir, f"{safe}.json")
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(sk, f, ensure_ascii=False, indent=2)
            count += 1
            print(f"  {name} -> {safe}.js / {safe}.json")
        except Exception as e:
            print(f"  跳过一条: {e}")
    return count


def export_skills_to_path(skill_lib, export_dir: str, quiet: bool = False) -> int:
    """
    将技能库（SkillLibrary 实例）导出到指定目录：每个技能一个 .js + 一个 .json。
    供 main 启动时自动导出或脚本直接传入 skill_lib 时使用。
    quiet=True 时不 print 每条，只写文件。
    """
    skills = skill_lib.list_all_skills()
    if not skills:
        if not quiet:
            print("技能库为空")
        return 0

    os.makedirs(export_dir, exist_ok=True)
    count = 0
    for sk in skills:
        try:
            name = sk.get("name", "unnamed")
            code = sk.get("code", "")
            safe = safe_filename(name)
            js_path = os.path.join(export_dir, f"{safe}.js")
            with open(js_path, "w", encoding="utf-8") as f:
                f.write(code if code else "// (empty)\n")
            json_path = os.path.join(export_dir, f"{safe}.json")
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(sk, f, ensure_ascii=False, indent=2)
            count += 1
            if not quiet:
                print(f"  {name} -> {safe}.js / {safe}.json")
        except Exception as e:
            if not quiet:
                print(f"  跳过 {sk.get('name')}: {e}")
    return count


def export_from_agent_lib(export_dir: str) -> int:
    """通过 SkillLibrary 列出并导出（兼容内存库）。"""
    from agent.skill_library import SkillLibrary
    from agent.llm_router import LLMRouter

    llm = LLMRouter()
    skill_lib = SkillLibrary(llm=llm, persist_dir="./data/skill_db")
    return export_skills_to_path(skill_lib, export_dir, quiet=False)


def main():
    parser = argparse.ArgumentParser(description="导出技能库为可读的 .js + .json 文件")
    parser.add_argument("--out", "-o", default="./data/skill_db_export", help="导出目录，默认 ./data/skill_db_export")
    parser.add_argument("--db", default="./data/skill_db", help="ChromaDB 路径，默认 ./data/skill_db")
    args = parser.parse_args()

    export_dir = os.path.abspath(args.out)
    skill_db_path = os.path.abspath(args.db)

    print(f"技能库路径: {skill_db_path}")
    print(f"导出目录:   {export_dir}")

    if os.path.isdir(skill_db_path):
        count = export_from_chroma(skill_db_path, export_dir)
    else:
        print("未找到 ChromaDB 目录，尝试通过 SkillLibrary 导出（含内存库）...")
        count = export_from_agent_lib(export_dir)

    print(f"共导出 {count} 条技能。")


if __name__ == "__main__":
    main()
