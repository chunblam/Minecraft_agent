"""
层级任务规划器（Hierarchical Task Planner）

核心能力：
1. decompose()          - 将复杂任务拆分为有序子任务列表（调用 V3）
2. check_if_satisfied() - 判断某个子任务是否已满足可跳过（状态感知）

设计思路：
  大任务 "建造养羊场"
    ├─ 子任务1: 收集 20 块橡木木板（可跳过：背包已有）
    ├─ 子任务2: 合成 16 根栅栏
    ├─ 子任务3: 找一块平地并搭建 4×4 围栏
    └─ 子任务4: 在周围找到并把羊赶入围栏

每个子任务之间通过共享 game_state dict 传递背包/位置更新，
让下一个子任务的 LLM 推理能看到前一个子任务的成果。
"""

import json
import re
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

from .llm_router import LLMRouter


# ── Prompt ────────────────────────────────────────────────────────────────────

DECOMPOSE_SYSTEM_PROMPT = """你是 Minecraft AI Agent「晨曦」的任务规划器。

将玩家的复杂任务分解为按依赖顺序排列的子任务列表。规则：
1. 子任务按前置依赖顺序排列（材料采集 → 合成 → 建造 → 部署）
2. 每个子任务是独立可执行的具体目标，description 会直接作为 Agent 的执行指令
3. success_criteria 必须可用背包/位置状态判断（"背包中有至少 X 个 Y"）
4. required_items 填执行该子任务"已需要"的前提物品（不是产出）
5. can_skip_if 描述何时可以跳过该子任务（已有材料等情况）
6. 子任务数量控制在 2-8 个
7. 【重要】合成/附魔必须有前置"采集材料"子任务，不能假设材料凭空存在
   例如"合成钻石剑"必须先有"采集2颗钻石和1根木棍"的子任务
8. 附魔任务需要前置"积累经验等级"和"获取青金石"的考虑
9. 当前背包已有的材料可以 can_skip_if 跳过对应采集任务
10. 【导航与寻路】移动类子任务：去某方向用 move_to + direction + distance；到某坐标附近用 region_center + radius；系统自动寻路（避障、绕路、上台阶）；若遇阻 observation 会提示 mine_block 等
11. 若周围已有某类资源的坐标（nearby_resources/horizon_scan），对应采集子任务的 description 中需包含该坐标信息，便于 Agent 直接执行

只输出 JSON，不要任何额外说明：
{
  "task_summary": "对任务的一句话总结",
  "subtasks": [
    {
      "name": "子任务简短名称",
      "description": "具体执行指令（如：收集 20 块橡木板，去砍树或找木材）",
      "success_criteria": "背包中有至少 20 块 minecraft:oak_planks",
      "required_items": [],
      "can_skip_if": "背包中已有至少 20 块 minecraft:oak_planks",
      "estimated_steps": 5
    }
  ]
}"""

SATISFY_CHECK_SYSTEM_PROMPT = """你是 Minecraft 任务条件检查员。

根据当前背包状态判断一个子任务是否可以跳过。
- 如果条件已满足，回答：yes|原因（例如：yes|背包中已有 2 颗钻石）
- 如果条件未满足，只回答：no

只回答 yes|原因 或 no，不要其他内容。"""


# ── 数据结构 ───────────────────────────────────────────────────────────────────

@dataclass
class SubTask:
    """单个子任务的描述与执行状态"""
    name: str
    description: str                    # 直接用作 Agent 执行指令
    success_criteria: str               # 完成条件（用于 check_if_satisfied）
    required_items: list[str] = field(default_factory=list)
    can_skip_if: str = ""               # 跳过条件描述
    estimated_steps: int = 5
    status: str = "pending"             # pending / running / completed / skipped / failed
    result: Optional[dict] = None       # 执行结果（_run_simple 返回的 dict）


def is_gather_subtask(subtask: SubTask) -> bool:
    """
    判断是否为采集/采矿类子任务（完成条件以背包数量为准，且可能涉及挖掘后拾取）。
    """
    criteria = (subtask.success_criteria or "").strip()
    name = (subtask.name or "").strip()
    desc = (subtask.description or "").strip()
    if criteria and ("背包" in criteria or "至少" in criteria):
        return True
    gather_keywords = ["采集", "砍", "挖", "收集", "原木", "矿石", "木头", "木材", "矿"]
    return any(kw in name or kw in desc for kw in gather_keywords)


def check_success_criteria_met(subtask: SubTask, game_state: dict) -> bool:
    """
    根据 success_criteria 与当前背包，确定性判断子任务是否已达成（不调 LLM）。
    仅支持「背包中有至少 N 个/块 X」形式；无法解析时返回 False。
    """
    criteria = (subtask.success_criteria or "").strip()
    if not criteria:
        return False
    # 解析：至少 N 个/块 [minecraft:]item_id 或中文名
    m = re.search(r"至少\s*(\d+)\s*(?:个|块|颗)\s*(.+)", criteria)
    if not m:
        return False
    required = int(m.group(1))
    item_hint = m.group(2).strip().strip("。，.")
    inventory = game_state.get("inventory", [])
    if not isinstance(inventory, list):
        return False
    total = 0
    for entry in inventory:
        if not isinstance(entry, dict):
            continue
        item_id = entry.get("item", "")
        count = int(entry.get("count", 1))
        if not item_id:
            continue
        if item_hint.startswith("minecraft:"):
            if item_id == item_hint or item_id.endswith(":" + item_hint.split(":")[-1]):
                total += count
        else:
            # 常见中文与 item 后缀对应，便于匹配
            hint_to_suffix = {
                "橡木原木": "oak_log", "橡木板": "oak_planks", "木板": "planks",
                "木棍": "stick", "原木": "log",
            }
            suffix = hint_to_suffix.get(item_hint) or item_hint
            if suffix in item_id or item_id.endswith("_" + suffix) or item_id.endswith("/" + suffix):
                total += count
            elif item_hint in item_id:
                total += count
    return total >= required


# ── 规划器 ─────────────────────────────────────────────────────────────────────

class TaskPlanner:
    """
    层级任务规划器。

    由 ReactAgent.__init__() 创建，共享同一个 LLMRouter 实例。
    """

    def __init__(self, llm: LLMRouter) -> None:
        self.llm = llm

    # ── 任务分解 ───────────────────────────────────────────────────────────────

    async def decompose(self, task: str, game_state: dict) -> list[SubTask]:
        """
        将复杂任务分解为有序子任务列表。

        Args:
            task:       玩家的原始任务指令
            game_state: 当前游戏状态（让 LLM 知道已有哪些材料）

        Returns:
            SubTask 列表（按执行顺序排列），分解失败返回空列表
        """
        inventory_summary = self._summarize_inventory(game_state.get("inventory", []))
        nearby_resources = game_state.get("nearby_resources", {})
        resource_summary = self._summarize_nearby_resources(nearby_resources)
        position = game_state.get("position", {})
        environment = game_state.get("environment", {})
        horizon_scan = self._summarize_horizon_scan(game_state.get("horizon_scan", {}))

        user_prompt = (
            f"任务：{task}\n\n"
            f"当前背包：{inventory_summary}\n"
            f"玩家位置：x={position.get('x', 0)}, y={position.get('y', 64)}, z={position.get('z', 0)}\n"
            f"当前环境：{environment.get('depth_context', '未知')}\n"
            f"周围已知资源（24格内）：{resource_summary}\n"
            f"地平线感知（远距离地形）：{horizon_scan}\n\n"
            f"请将任务分解为子任务列表。"
        )

        raw = await self.llm.think_fast(
            system_prompt=DECOMPOSE_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.3,
        )

        if not raw:
            logger.warning("任务分解：LLM 无输出")
            return []

        subtasks = self._parse_subtasks(raw)
        if subtasks:
            names = " → ".join(s.name for s in subtasks)
            logger.info(f"任务分解完成 [{len(subtasks)} 步]: {names}")
        return subtasks

    # ── 条件检查 ───────────────────────────────────────────────────────────────

    async def check_if_satisfied(
        self, subtask: SubTask, game_state: dict
    ) -> tuple[bool, str]:
        """
        判断某个子任务是否可以跳过（条件已满足）。

        快速路径：先用 can_skip_if 关键词做启发式检查；
        若模糊则调用 LLM 进行语义判断。

        Args:
            subtask:    要检查的子任务
            game_state: 当前游戏状态

        Returns:
            (should_skip: bool, reason: str)
        """
        inventory = game_state.get("inventory", [])
        inventory_summary = self._summarize_inventory(inventory)

        # 若子任务无跳过条件则直接执行
        if not subtask.can_skip_if and not subtask.success_criteria:
            return False, ""

        user_prompt = (
            f"当前背包：{inventory_summary}\n"
            f"子任务：{subtask.name}\n"
            f"完成标准：{subtask.success_criteria}\n"
            f"跳过条件：{subtask.can_skip_if}"
        )

        result = await self.llm.classify(
            system_prompt=SATISFY_CHECK_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.1,
        )

        if result and result.lower().startswith("yes"):
            # 解析 "yes|原因"
            parts = result.split("|", 1)
            reason = parts[1].strip() if len(parts) > 1 else "条件已满足"
            return True, reason

        return False, ""

    # ── 内部工具 ───────────────────────────────────────────────────────────────

    @staticmethod
    def _summarize_inventory(inventory: list[dict]) -> str:
        """将背包列表格式化为简洁摘要供 LLM 阅读"""
        if not inventory:
            return "背包为空"
        items = []
        for item in inventory:
            item_id = item.get("item", "?")
            count = item.get("count", 1)
            items.append(f"{item_id} x{count}")
        return "、".join(items)

    @staticmethod
    def _summarize_nearby_resources(nearby_resources: dict) -> str:
        """
        将 game_state.nearby_resources 格式化为规划器可读的文字摘要。

        输出示例：
          矿石(ores): (-12,-58,5)[deepslate_diamond_ore], (8,-61,3)[deepslate_iron_ore];
          原木(logs): (5,64,10)[oak_log];
          未发现: crafting, farmable
        """
        if not nearby_resources:
            return "无（玩家周围24格内未扫描到已知资源）"

        parts = []
        for category, entries in nearby_resources.items():
            if not entries:
                continue
            category_zh = {
                "ores": "矿石", "logs": "原木", "water": "水源",
                "lava": "熔岩", "gravel": "沙砾", "sand": "沙子",
                "crafting": "制作设施", "farmable": "农业方块",
            }.get(category, category)

            coords = []
            for entry in entries[:3]:  # 最多显示3个坐标，避免 prompt 过长
                x, y, z = entry.get("x", 0), entry.get("y", 0), entry.get("z", 0)
                block = entry.get("block", "").replace("minecraft:", "")
                coords.append(f"({x},{y},{z})[{block}]")
            parts.append(f"{category_zh}: {', '.join(coords)}")

        return "；".join(parts) if parts else "周围24格内未发现常见资源"

    @staticmethod
    def _summarize_horizon_scan(horizon_scan: dict) -> str:
        """
        将 horizon_scan（8 方向 × 多距离）格式化为规划器可读的简要摘要。
        用于 decompose 时了解远距离地形，便于拆出「先去森林砍木」等子任务。
        """
        if not horizon_scan or not isinstance(horizon_scan, dict):
            return "无（未扫描）"
        dir_names = {"north": "北", "south": "南", "east": "东", "west": "西",
                     "northeast": "东北", "northwest": "西北", "southeast": "东南", "southwest": "西南"}
        parts = []
        for direction, entries in horizon_scan.items():
            if not isinstance(entries, list) or not entries:
                continue
            name = dir_names.get(direction, direction)
            # 取最近一格（48）和较远一格（96 或 192）的代表性信息
            first = entries[0] if entries else {}
            if isinstance(first, dict):
                biome = first.get("biome", "").replace("minecraft:", "")
                hint = first.get("hint", "")[:40]
                dist = first.get("distance", 48)
                parts.append(f"{name}{dist}格:{biome}({hint})")
        return "；".join(parts[:6]) if parts else "无"  # 最多 6 个方向，避免过长

    @staticmethod
    def _parse_subtasks(raw: str) -> list[SubTask]:
        """
        从 LLM 输出中解析子任务列表，兼容 ```json ... ``` 包裹格式。
        """
        cleaned = raw.strip()
        if "```" in cleaned:
            lines = cleaned.split("\n")
            in_block, inner = False, []
            for line in lines:
                if line.strip().startswith("```"):
                    in_block = not in_block
                    continue
                if in_block:
                    inner.append(line)
            cleaned = "\n".join(inner)

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.error(f"子任务 JSON 解析失败: {e}\n原文前300字符: {raw[:300]}")
            return []

        subtasks = []
        for item in data.get("subtasks", []):
            subtasks.append(SubTask(
                name=item.get("name", "未命名子任务"),
                description=item.get("description", item.get("name", "")),
                success_criteria=item.get("success_criteria", ""),
                required_items=item.get("required_items", []),
                can_skip_if=item.get("can_skip_if", ""),
                estimated_steps=item.get("estimated_steps", 5),
            ))

        return subtasks
