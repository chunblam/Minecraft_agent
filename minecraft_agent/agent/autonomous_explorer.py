"""
自主探索模块（Voyager-style Automatic Curriculum）

设计目标：
  当玩家处于空闲状态时，Agent 根据当前游戏状态（附近资源、已有技能、背包内容）
  自主提出下一个值得尝试的任务，执行后将成功经验抽象为可复用技能存入库中。

工作流：
  Java Mod 每 60 秒向 Python 推送一次 game_state_update 消息
      ↓
  AutonomousExplorer.on_game_state_update(game_state)
      ↓ (仅当 Agent 空闲且距上次探索 >= COOLDOWN_SECONDS)
  _propose_next_task()  → LLM 分析资源/技能 → 输出下一个目标任务
      ↓
  _run_exploration()   → 调用 agent.run() 执行（技能学习由 _post_process_success 自动完成）

冷却机制：
  - 两次自主探索之间至少间隔 COOLDOWN_SECONDS（默认 300 秒 = 5 分钟）
  - 玩家主动发出消息（任务/聊天）会打断正在进行的探索并重置冷却
  - 探索过程中如果玩家发言，以玩家消息优先
"""

import asyncio
import time
from loguru import logger

from .llm_router import LLMRouter
from .skill_library import SkillLibrary
from .planner import TaskPlanner


# 两次自主探索之间的最短间隔（秒）
COOLDOWN_SECONDS = 300

# 自主探索最大连续失败次数，超过后增加冷却至 600 秒
MAX_CONSECUTIVE_FAILURES = 3

_CURRICULUM_SYSTEM_PROMPT = """\
你是 Minecraft AI Agent「晨曦」的自主学习规划器。

根据玩家当前所在位置的附近资源、已掌握的技能列表和背包内容，
提出一个简单、可执行、有助于提升能力的下一步目标任务。

要求：
1. 任务必须在当前位置可执行（使用 nearby_resources 中给出的资源）
2. 任务难度循序渐进：先基础资源采集，再合成工具，再进阶探索
3. 已有技能的任务不要重复（避免做重复的事）
4. 任务描述简洁具体，直接作为执行指令使用（如"收集3个橡木原木"）
5. 若周围没有可用资源，可以提出"探索周边地形"或"查看地平线寻找资源"类任务

只输出 JSON，格式：
{"task": "具体任务描述", "reason": "为什么选择这个任务（一句话）", "expected_skill": "预计学会的技能名称"}

若当前状态不适合探索（夜晚太危险、背包已满等），输出：
{"task": null, "reason": "跳过原因"}
"""


class AutonomousExplorer:
    """
    Voyager-style 自主探索器。

    由 main.py 实例化，接收 Java Mod 推送的 game_state_update 消息后触发探索循环。
    """

    def __init__(self, llm: LLMRouter, skill_lib: SkillLibrary) -> None:
        self.llm = llm
        self.skill_lib = skill_lib

        self._is_running: bool = False          # 是否正在执行自主探索任务
        self._last_explored: float = 0.0        # 上次探索完成的时间戳
        self._consecutive_failures: int = 0     # 连续失败次数
        self._agent = None                      # 延迟注入，避免循环引用

        logger.info("AutonomousExplorer 初始化完成")

    def set_agent(self, agent) -> None:
        """注入 ReactAgent 实例（在 main.py 中调用，避免循环引用）"""
        self._agent = agent

    def notify_player_active(self) -> None:
        """
        玩家主动发言时调用，重置探索状态。
        如果正在执行探索任务，标记为被打断（当前探索会自然结束后不再继续）。
        """
        if self._is_running:
            logger.info("[Explorer] 玩家主动发言，自主探索让步")
        # 重置冷却，玩家活跃时暂停探索
        self._last_explored = time.monotonic()

    async def on_game_state_update(self, game_state: dict) -> None:
        """
        接收 Java Mod 推送的周期性游戏状态更新，决定是否触发自主探索。

        此方法会被 main.py 以 asyncio.create_task() 方式调用，不阻塞消息循环。
        """
        if self._agent is None:
            logger.debug("[Explorer] Agent 未注入，跳过探索")
            return

        if self._is_running:
            logger.debug("[Explorer] 探索任务进行中，跳过本次触发")
            return

        # 冷却期检查（连续失败时延长冷却）
        cooldown = COOLDOWN_SECONDS * (2 if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES else 1)
        elapsed = time.monotonic() - self._last_explored
        if elapsed < cooldown:
            logger.debug(f"[Explorer] 冷却中（{elapsed:.0f}s / {cooldown}s），跳过")
            return

        # 基础安全检查：夜晚且光照低时跳过
        time_val = game_state.get("time", 0)
        environment = game_state.get("environment", {})
        is_dark = environment.get("is_dark", False)
        health = game_state.get("health", 20)
        if time_val > 13000 and is_dark and health < 10:
            logger.info("[Explorer] 夜晚危险（血量低），跳过本次探索")
            return

        player_name = game_state.get("player_name", "Player")
        logger.info(f"[Explorer] 触发自主探索（玩家: {player_name}）")

        task = await self._propose_next_task(game_state)
        if task:
            await self._run_exploration(task, game_state)
        else:
            logger.info("[Explorer] LLM 建议跳过本次探索")

    async def _propose_next_task(self, game_state: dict) -> str | None:
        """
        调用 LLM 根据当前游戏状态提出下一个自主探索目标。

        Returns:
            任务描述字符串，或 None（LLM 建议跳过时）
        """
        import json

        # 构建精简上下文
        nearby_resources = game_state.get("nearby_resources", {})
        inventory = game_state.get("inventory", [])
        environment = game_state.get("environment", {})
        position = game_state.get("position", {})

        # 已知技能列表（取名称即可）
        known_skills = await self._get_known_skill_names()

        resource_summary = self._summarize_resources(nearby_resources)
        inventory_summary = self._summarize_inventory(inventory)

        user_prompt = (
            f"玩家位置：x={position.get('x', 0):.0f}, y={position.get('y', 64):.0f}, z={position.get('z', 0):.0f}\n"
            f"环境：{environment.get('biome_hint', '未知')}（{environment.get('depth_context', '地表')}）\n"
            f"游戏时间：{game_state.get('time', 0)}（12000+ 为夜晚）\n"
            f"血量：{game_state.get('health', 20)}/20\n"
            f"背包：{inventory_summary}\n"
            f"附近资源（24格内）：{resource_summary}\n"
            f"已掌握技能：{', '.join(known_skills) if known_skills else '暂无'}\n"
        )

        raw = await self.llm.classify(
            system_prompt=_CURRICULUM_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.6,
        )

        if not raw:
            return None

        try:
            # 提取 JSON
            import re
            match = re.search(r'\{.*\}', raw, re.DOTALL)
            if not match:
                return None
            data = json.loads(match.group())
            task = data.get("task")
            reason = data.get("reason", "")
            if task:
                logger.info(f"[Explorer] 提出任务: 「{task}」| 理由: {reason}")
                return task
            else:
                logger.info(f"[Explorer] LLM 建议跳过: {reason}")
                return None
        except Exception as e:
            logger.warning(f"[Explorer] 课程 JSON 解析失败: {e} | raw: {raw[:200]}")
            return None

    async def _run_exploration(self, task: str, game_state: dict) -> None:
        """
        执行自主探索任务（调用 agent.run），技能学习由 _post_process_success 自动完成。
        """
        self._is_running = True
        logger.info(f"[Explorer] 开始执行自主任务: 「{task}」")

        try:
            result = await self._agent.run(game_state, task)
            is_success = result.get("action_type") != "error" and not result.get("extra_data", {}).get("fallback")

            if is_success:
                self._consecutive_failures = 0
                logger.success(f"[Explorer] 自主任务完成: 「{task}」")
            else:
                self._consecutive_failures += 1
                logger.warning(
                    f"[Explorer] 自主任务未完成: 「{task}」| "
                    f"连续失败 {self._consecutive_failures} 次"
                )
        except Exception as e:
            self._consecutive_failures += 1
            logger.error(f"[Explorer] 自主任务执行异常: {e}", exc_info=True)
        finally:
            self._is_running = False
            self._last_explored = time.monotonic()

    async def _get_known_skill_names(self) -> list[str]:
        """获取技能库中已有技能的名称列表（用于避免重复学习）"""
        try:
            # 用通用词搜索，获取代表性技能
            results = await self.skill_lib.search_skills("采集 合成 挖掘 探索", top_k=10)
            names = []
            for r in results:
                skill = r.get("skill", {})
                name = skill.get("skill_name", "")
                if name:
                    names.append(name)
            return names
        except Exception:
            return []

    @staticmethod
    def _summarize_resources(nearby_resources: dict) -> str:
        """将 nearby_resources 格式化为简洁摘要"""
        if not nearby_resources:
            return "无"
        parts = []
        category_zh = {
            "ores": "矿石", "logs": "原木", "water": "水源",
            "lava": "熔岩", "gravel": "沙砾", "sand": "沙子",
            "crafting": "制作台", "farmable": "农业",
        }
        for cat, entries in nearby_resources.items():
            if not entries:
                continue
            zh = category_zh.get(cat, cat)
            sample = entries[0]
            block = sample.get("block", "").replace("minecraft:", "")
            parts.append(f"{zh}({block})×{len(entries)}")
        return "、".join(parts) if parts else "无"

    @staticmethod
    def _summarize_inventory(inventory: list[dict]) -> str:
        """将背包列表格式化为简洁摘要"""
        if not inventory:
            return "空"
        items = [
            f"{item.get('item', '?').replace('minecraft:', '')} x{item.get('count', 1)}"
            for item in inventory[:8]
        ]
        suffix = f"...等{len(inventory)}种物品" if len(inventory) > 8 else ""
        return "、".join(items) + suffix
