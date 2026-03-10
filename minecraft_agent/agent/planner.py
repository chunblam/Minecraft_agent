"""
层级任务规划器 v3（RAG 增强版）

核心升级：
  1. decompose() 支持传入 retriever，注入 RAG 上下文
  2. PLANNER_HUMAN_TEMPLATE 含 {rag_context} 占位符
  3. 并发：RAG 检索与 LLM 调用可并发（调用方 react_agent 已处理）
"""

import json
import re
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

from .llm_router import LLMRouter
from .prompts import PLANNER_SYSTEM_PROMPT, PLANNER_HUMAN_TEMPLATE


@dataclass
class SubTask:
    name: str
    description: str
    success_criteria: str = ""
    required_items: dict = field(default_factory=dict)
    can_skip_if: str = ""
    status: str = "pending"   # pending | running | done | skipped | failed

    def __repr__(self):
        return f"SubTask({self.name!r}, status={self.status})"


def check_success_criteria_met(criteria: str, game_state: dict) -> bool:
    if not criteria:
        return False

    inventory = game_state.get("inventory", [])
    inv_map: dict[str, int] = {}
    for item in inventory:
        name  = item.get("item") or item.get("name") or ""
        count = item.get("count", 1)
        name  = name.replace("minecraft:", "").lower()
        inv_map[name] = inv_map.get(name, 0) + count

    match = re.search(
        r'(?:has|至少|contains?)\s+(\d+)\s+(?:个\s*)?([a-z_\u4e00-\u9fff]+)',
        criteria.lower()
    )
    if match:
        needed    = int(match.group(1))
        item_name = match.group(2).replace("minecraft:", "").lower()
        have      = inv_map.get(item_name, 0)
        return have >= needed

    return False


def is_gather_subtask(subtask: "SubTask") -> bool:
    gather_kw = ["采集", "收集", "获取", "找到", "挖", "砍",
                 "gather", "collect", "mine", "chop", "find"]
    text = (subtask.name + subtask.description).lower()
    return any(kw in text for kw in gather_kw)


class TaskPlanner:
    def __init__(self, llm: LLMRouter):
        self.llm = llm

    async def decompose(
        self,
        task: str,
        game_state: dict,
        retriever=None,              # ★ 可选 RAGRetriever
    ) -> list["SubTask"]:
        """将复杂任务分解为有序子任务列表"""

        # ★ 并发获取 RAG 上下文（如果有 retriever）
        rag_context = ""
        if retriever:
            try:
                docs = await retriever.search(task, top_k=3)
                if docs:
                    snippets = [
                        f"  - {d.get('title','')}: {d.get('content','')[:200]}"
                        for d in docs if d.get("score", 0) > 0.3
                    ]
                    if snippets:
                        rag_context = "\nRelevant Minecraft knowledge:\n" + "\n".join(snippets)
            except Exception as e:
                logger.debug(f"[Planner] RAG 检索失败: {e}")

        human_msg = self._build_human_message(task, game_state, rag_context)

        logger.info(f"[Planner] 分解任务: {task[:60]}")
        raw = await self.llm.think_fast(
            system_prompt=PLANNER_SYSTEM_PROMPT,
            user_prompt=human_msg,
            temperature=0.3,
        )

        if not raw:
            logger.warning("[Planner] LLM 无输出，返回单步兜底")
            return [SubTask(name=task, description=task)]

        subtasks = self._parse_subtasks(raw)
        if not subtasks:
            logger.warning("[Planner] 解析失败，返回单步兜底")
            return [SubTask(name=task, description=task)]

        logger.info(f"[Planner] 分解为: {[s.name for s in subtasks]}")
        return subtasks

    async def check_if_satisfied(self, subtask: "SubTask", game_state: dict) -> bool:
        # 1. 规则快速判断
        if subtask.success_criteria:
            if check_success_criteria_met(subtask.success_criteria, game_state):
                return True

        # 2. 背包检查（采集类）
        if subtask.required_items and is_gather_subtask(subtask):
            inventory = game_state.get("inventory", [])
            inv_map: dict[str, int] = {}
            for item in inventory:
                name = (item.get("item") or item.get("name") or "").replace("minecraft:", "").lower()
                inv_map[name] = inv_map.get(name, 0) + item.get("count", 1)

            all_ok = all(
                inv_map.get(k.replace("minecraft:", "").lower(), 0) >= v
                for k, v in subtask.required_items.items()
            )
            if all_ok:
                return True

        # 3. can_skip_if 快速解析（避免 LLM 调用）
        if subtask.can_skip_if:
            cond = subtask.can_skip_if.strip()
            # 格式：has:item:N
            if cond.startswith("has:"):
                parts = cond.split(":")
                if len(parts) >= 3:
                    item_name = parts[1]
                    count     = int(parts[2]) if parts[2].isdigit() else 1
                    return check_success_criteria_met(
                        f"has {count} {item_name}", game_state
                    )
            # 回退到 LLM 判断
            return await self._llm_check_skip(cond, game_state)

        return False

    # ── 内部工具 ─────────────────────────────────────────────────────────────

    @staticmethod
    def _build_human_message(
        task: str, game_state: dict, rag_context: str = ""
    ) -> str:
        inventory = game_state.get("inventory", [])
        inv_str = ", ".join(
            f"{item.get('item', item.get('name', '?'))} x{item.get('count', 1)}"
            for item in inventory[:15]
        ) or "empty"

        pos = game_state.get("position", {})
        pos_str = (f"({pos.get('x','?')}, {pos.get('y','?')}, {pos.get('z','?')})"
                   if isinstance(pos, dict) else str(pos))

        env = game_state.get("environment", {})
        biome = (env.get("biome") if isinstance(env, dict) else None) \
                or game_state.get("biome", "unknown")

        return PLANNER_HUMAN_TEMPLATE.format(
            task=task,
            inventory=inv_str,
            position=pos_str,
            biome=biome,
            rag_context=rag_context,
        )

    @staticmethod
    def _parse_subtasks(raw: str) -> list["SubTask"] | None:
        text = raw.strip()
        if "```" in text:
            lines = text.split("\n")
            text  = "\n".join(l for l in lines if not l.strip().startswith("```"))

        text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
        text = re.sub(r",\s*}", "}", text)
        text = re.sub(r",\s*]",  "]", text)

        match = re.search(r'\[.*\]', text, re.DOTALL)
        if match:
            text = match.group(0)

        try:
            data = json.loads(text)
            if not isinstance(data, list):
                return None

            result = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                st = SubTask(
                    name=item.get("name", "未命名子任务"),
                    description=item.get("description", item.get("name", "")),
                    success_criteria=item.get("success_criteria", ""),
                    required_items=item.get("required_items", {}),
                    can_skip_if=item.get("can_skip_if", ""),
                )
                result.append(st)

            return result if result else None
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"[Planner] JSON 解析失败: {e}")
            return None

    async def _llm_check_skip(self, condition: str, game_state: dict) -> bool:
        inventory = game_state.get("inventory", [])
        inv_str = ", ".join(
            f"{item.get('item','?')} x{item.get('count',1)}"
            for item in inventory[:10]
        ) or "empty"

        result = await self.llm.classify(
            system_prompt="You are a Minecraft inventory checker. Answer yes or no only.",
            user_prompt=f"Inventory: {inv_str}\nCondition: {condition}\nSatisfied? yes/no",
            temperature=0.0,
        )
        return bool(result and result.strip().lower().startswith("yes"))
