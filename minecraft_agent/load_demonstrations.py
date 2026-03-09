"""
加载演示技能（Demonstration Skills）

模仿 MineDojo：用预置的人类操作示范作为基础技能，Agent 从零开始时即可复用。
可将 data/demonstrations/*.json 中的技能加载到 ChromaDB 技能库。

用法：
  python load_demonstrations.py              # 加载 data/demonstrations/ 下所有 JSON
  python load_demonstrations.py --dir PATH   # 指定目录
  python load_demonstrations.py --dry-run   # 仅预览，不写入

注意：重复运行会追加技能（可能重复）。若需从头加载，请先运行 reset_agent.py 清空技能库。
"""

import argparse
import asyncio
import json
from pathlib import Path

from agent.skill_library import SkillLibrary


def main():
    parser = argparse.ArgumentParser(description="加载演示技能到技能库")
    parser.add_argument("--dir", default=None, help="演示 JSON 目录，默认 data/demonstrations")
    parser.add_argument("--dry-run", action="store_true", help="仅打印，不写入")
    args = parser.parse_args()

    base = Path(__file__).parent
    demo_dir = Path(args.dir) if args.dir else base / "data" / "demonstrations"
    if not demo_dir.exists():
        print(f"[!] 目录不存在: {demo_dir}")
        return 1

    json_files = sorted(demo_dir.glob("*.json"))
    if not json_files:
        print(f"[!] 未找到 JSON 文件: {demo_dir}")
        return 1

    async def load_all():
        lib = SkillLibrary()
        loaded = 0
        for fp in json_files:
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    skill = json.load(f)
                if args.dry_run:
                    print(f"[dry-run] 将加载: {skill.get('skill_name', '?')} <- {fp.name}")
                else:
                    ok = await lib.load_demonstration(skill, source="demonstration")
                    if ok:
                        loaded += 1
            except Exception as e:
                print(f"[!!] 加载失败 {fp.name}: {e}")
        return loaded

    n = asyncio.run(load_all())
    print(f"完成，共加载 {n} 个演示技能")
    return 0


if __name__ == "__main__":
    exit(main())
