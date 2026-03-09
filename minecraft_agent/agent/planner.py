"""
层级任务规划器 v2（Refactored）

核心变更：
1. Prompt 大幅简化 —— 去除冗余规则，专注于分解逻辑
2. 加入快速背包预检（check_can_skip）—— 直接用 game_state 判断，不走 LLM
3. SubTask 增加 required_items 字段，支持依赖检查
4. decompose() 改为同步 + 异步两种调用方式
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
    required_items: dict = field(default_factory=dict)   # {"oak_log": 5}
    can_skip_if: str = ""
    status: str = "pending"   # pending | running | done | skipped | failed

    def __repr__(self):
        return f"SubTask({self.name!r}, status={self.status})"


def check_success_criteria_met(criteria: str, game_state: dict) -> bool:
    """
    用简单规则检查 success_criteria 是否满足（不走 LLM）。

    支持格式：
      "has N <item> in inventory"
      "inventory contains <item>"
      "背包中有至少 N 个 <item>"
    """
    if not criteria:
        return False

    inventory = game_state.get("inventory", [])
    inv_map: dict[str, int] = {}
    for item in inventory:
        name = item.get("item") or item.get("name") or ""
        count = item.get("count", 1)
        # 规范化：去掉 minecraft: 前缀
        name = name.replace("minecraft:", "").lower()
        inv_map[name] = inv_map.get(name, 0) + count

    # 尝试解析 "has N item" 或 "至少 N 个 item"
    match = re.search(
        r'(?:has|至少|contains?)\s+(\d+)\s+(?:个\s*)?([a-z_\u4e00-\u9fff]+)',
        criteria.lower()
    )
    if match:
        needed = int(match.group(1))
        item_name = match.group(2).replace("minecraft:", "").lower()
        have = inv_map.get(item_name, 0)
        return have >= needed

    return False


def is_gather_subtask(subtask: SubTask) -> bool:
    """启发式判断是否是采集类子任务（可通过背包跳过）"""
    gather_keywords = ["采集", "收集", "获取", "找到", "挖", "砍", "gather", "collect", "mine", "chop"]
    text = (subtask.name + subtask.description).lower()
    return any(kw in text for kw in gather_keywords)


class TaskPlanner:
    """
    层级任务分解器。

    decompose(task, game_state) → list[SubTask]
    """

    def __init__(self, llm: LLMRouter):
        self.llm = llm

    async def decompose(self, task: str, game_state: dict) -> list[SubTask]:
        """
        将复杂任务分解为按依赖顺序的子任务列表。

        Returns:
            list[SubTask]，已按依赖顺序排列
        """
        human_msg = self._build_human_message(task, game_state)

        logger.info(f"[Planner] 开始分解任务: {task[:60]}")

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

        logger.info(f"[Planner] 分解成功: {[s.name for s in subtasks]}")
        return subtasks

    async def check_if_satisfied(
        self, subtask: SubTask, game_state: dict
    ) -> bool:
        """
        检查子任务是否已满足（可跳过）。
        优先用规则判断，规则无法判断时才走 LLM。
        """
        # 1. 规则快速判断
        if subtask.success_criteria:
            if check_success_criteria_met(subtask.success_criteria, game_state):
                return True

        # 2. 背包检查：如果 required_items 已全部存在，且是采集类任务 → 跳过
        if subtask.required_items and is_gather_subtask(subtask):
            inventory = game_state.get("inventory", [])
            inv_map: dict[str, int] = {}
            for item in inventory:
                name = (item.get("item") or item.get("name") or "").replace("minecraft:", "").lower()
                inv_map[name] = inv_map.get(name, 0) + item.get("count", 1)

            all_satisfied = all(
                inv_map.get(k.replace("minecraft:", "").lower(), 0) >= v
                for k, v in subtask.required_items.items()
            )
            if all_satisfied:
                return True

        # 3. can_skip_if：LLM 判断（仅在规则无法确定时）
        if subtask.can_skip_if:
            result = await self._llm_check_skip(subtask.can_skip_if, game_state)
            return result

        return False

    # ── 内部工具 ─────────────────────────────────────────────────────────────

    @staticmethod
    def _build_human_message(task: str, game_state: dict) -> str:
        inventory = game_state.get("inventory", [])
        if isinstance(inventory, list):
            inv_str = ", ".join(
                f"{item.get('item', item.get('name', '?'))} x{item.get('count', 1)}"
                for item in inventory[:15]
            ) or "empty"
        else:
            inv_str = str(inventory) or "empty"

        pos = game_state.get("position", {})
        if isinstance(pos, dict):
            pos_str = f"({pos.get('x', '?')}, {pos.get('y', '?')}, {pos.get('z', '?')})"
        else:
            pos_str = str(pos)

        # 兼容 mineflayer 返回的顶层 biome 或原 environment.biome
        env = game_state.get("environment", {})
        biome_str = (env.get("biome") if isinstance(env, dict) else None) or game_state.get("biome") or "unknown"

        return PLANNER_HUMAN_TEMPLATE.format(
            task=task,
            inventory=inv_str,
            position=pos_str,
            biome=biome_str,
        )

    @staticmethod
    def _parse_subtasks(raw: str) -> list[SubTask] | None:
        """解析 LLM 返回的 JSON 数组为 SubTask 列表"""
        text = raw.strip()

        # 去掉 markdown
        if "```" in text:
            lines = text.split("\n")
            text = "\n".join(l for l in lines if not l.strip().startswith("```"))

        # 修复常见 JSON 错误
        text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
        text = re.sub(r",\s*}", "}", text)
        text = re.sub(r",\s*]", "]", text)

        # 找 JSON 数组
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

    async def _llm_check_skip(self, can_skip_if: str, game_state: dict) -> bool:
        """用 LLM 判断 can_skip_if 条件是否满足"""
        inventory = game_state.get("inventory", [])
        inv_str = ", ".join(
            f"{item.get('item', '?')} x{item.get('count', 1)}"
            for item in inventory[:10]
        ) or "empty"

        prompt = (
            f"Current inventory: {inv_str}\n"
            f"Condition: {can_skip_if}\n"
            f"Is the condition satisfied? Reply with only 'yes' or 'no'."
        )

        result = await self.llm.classify(
            system_prompt="You are a Minecraft inventory checker. Answer yes or no only.",
            user_prompt=prompt,
            temperature=0.0,
        )

        if result:
            return result.strip().lower().startswith("yes")
        return False
