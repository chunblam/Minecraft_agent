"""
ReAct Agent 主循环模块（v3 —— Plan-then-Execute + 全 V3 快速推理）

核心架构：

1. 模型分配（全 V3，与 Voyager 论文一致）：
   - think_fast() : 任务分解、ReAct 推理、行动计划生成
   - classify()   : 意图识别、技能抽象、闲聊回复

2. Plan-then-Execute 模式（减少 LLM 调用次数）：
   简单任务不再逐步调用 LLM，而是：
     Phase 1: V3 一次性生成完整行动计划（action_plan JSON 数组）
     Phase 2: 批量执行所有步骤，无需再调 LLM
     Phase 3: 失败时局部重规划（仅对失败步骤调 1 次 V3）
   效果：5 步任务从 5 次 LLM 调用减少到 1-2 次

3. 技能学习异步化：
   abstract_from_trajectory / abstract_hierarchical_skill 改为
   asyncio.create_task() 后台执行，不阻塞给玩家的回复

4. 消息分流（main.py 已实现）：
   "任务：xxx" → run()  → 任务模式（ReAct / 层级执行）
   普通消息    → chat() → 闲聊模式（V3 直接回复，2-5s）
"""

import os
import json
import re
import asyncio
from loguru import logger

from .llm_router import LLMRouter
from .memory import MemoryManager
from .skill_library import SkillLibrary
from .skill_executor import (
    can_execute_as_template,
    execute_skill_template,
)
from .personality import PersonalitySystem
from .planner import TaskPlanner, SubTask
from rag.retriever import RAGRetriever


MAX_STEPS = int(os.getenv("MAX_REACT_STEPS", "10"))

# ── System Prompt ─────────────────────────────────────────────────────────────

REACT_SYSTEM_PROMPT = """你是「晨曦」，一个生活在 Minecraft 世界中的可爱美少女 AI 助手。
主人给你下达了任务，你正在认真执行中！

你使用 ReAct 框架执行任务：每步先 Thought（思考）→ Action（行动）→ 等待 Observation（观察结果）→ 重复。
注意：Thought 中只写推理过程，不要写角色扮演的话。JSON 输出要严格规范。

【game_state 关键字段说明】
- position          : 玩家当前坐标 {x,y,z}（操控主角模式，与 agent_position 一致）
- agent_position    : 兼容字段，与 position 相同
- inventory         : 背包物品列表
- xp_level          : 经验等级（附魔时需要）
- nearby_resources  : 24格内各类资源坐标（ores/logs/water/crafting等）
- nearby_entities   : 20格内的生物列表（含类型和坐标）
- environment       : 当前位置环境（y层/深度/光照/生物群系）
- horizon_scan      : 8方向×{48,96,192}格 的生物群系感知（"地平线视野"）
  结构：{"north": [{"distance":48,"biome":"minecraft:forest","hint":"橡木森林...","surface_y":68,...},...], ...}
  用途：让你在远距离规划时就知道哪个方向有森林/沙漠/山地/村庄等

【关键推理规则】
1. 每步先读取 game_state（尤其是 nearby_resources 和 horizon_scan），再规划行动
2. 如果背包中已有所需物品，直接跳到下一步，不要重复采集
3. 坐标使用 game_state 中给出的实际数值，不要猜测或编造坐标
4. 复杂任务可拆分为小步骤逐步推进
5. 移动前先确认目标坐标（从 nearby_resources 或 horizon_scan 中获取）

【可用行动列表】
- chat           : 向玩家发送消息
    {"message": "文字内容"}
- move_to        : 移动到坐标
    {"x": 10, "y": 64, "z": -30}
- mine_block     : 挖掘指定坐标的方块（方块会正常掉落到地面，需要合适工具）
    {"x": 10, "y": 62, "z": -30}
- place_block    : 放置方块（从背包材料中放置，需要背包有对应方块）
    {"x": 10, "y": 64, "z": -30, "block": "minecraft:oak_fence"}
- craft_item     : 合成物品（必须先有配方所需的全部材料才能合成，材料会从背包消耗）
    {"item": "minecraft:diamond_sword", "count": 1}
    ⚠️ 合成前必须用 get_inventory 确认材料充足，若材料不足会返回缺少哪些材料
- enchant_item   : 对手持物品附魔（需要消耗经验等级×5 + 对应数量青金石）
    {"slot": "mainhand", "enchantment": "minecraft:power", "level": 3}
    ⚠️ 附魔 N 级需要 N×5 经验等级 + N 个青金石，不足时会返回需要多少
- interact_entity: 与附近实体互动（找羊/移动动物/交易等）
    {"entity_type": "sheep", "action": "find"}
    {"entity_type": "sheep", "action": "move_to_pos", "x": 10, "y": 64, "z": 10}
    {"entity_type": "sheep", "action": "teleport_to_agent"}
- find_resource  : 主动搜索周围特定资源的坐标（方块或实体）
    {"type": "diamond", "radius": 32}               ← 找钻石矿（会自动搜索 diamond_ore + deepslate_diamond_ore）
    {"type": "iron", "radius": 24, "max_results": 5}← 找铁矿，返回最多5个坐标
    {"type": "oak_log", "radius": 20}               ← 找橡木
    {"type": "sheep", "radius": 30}                 ← 找羊（先在实体中搜索）
    {"type": "crafting_table", "radius": 16}        ← 找合成台
    支持别名：diamond/iron/gold/coal/copper/lapis/redstone/emerald/tree/wood/sand/gravel/water/lava
    ⚠️ 返回坐标后直接用 move_to 移动过去，找到了再 mine_block/interact_entity
- scan_area      : 全扫描当前区域，返回各类资源的数量和最近坐标（不确定周围有什么时使用）
    {"radius": 24}
- look_around    : 远望地平线，获取 8 方向的生物群系和地形摘要（已在 game_state.horizon_scan 自动提供，
                   此行动可在行动中途主动刷新，或扩大至 256 格范围）
    {"radius": 192}   ← 默认 192 格
    {"radius": 256}   ← 最大范围
    返回文字摘要：各方向的地形类型、地表高度、具体目标坐标的导航建议
    例如："正北48格是橡木森林(x=50,z=-18)，正东192格是石峰(x=242,z=30)"
- get_inventory  : 查询完整背包（36格）+ 当前经验等级，用于确认材料/资源是否充足
    {}
- follow_player  : 跟随玩家
    {"player": "Steve"}
- look_at        : 转向
    {"x": 10, "y": 64, "z": -30}
- stop           : 停止移动
    {}
- finish         : 任务完成，结束本轮推理（必须设 is_final: true）
    {"message": "任务完成说明"}

【重要规划原则 - 分层资源感知策略】
1. 【第1层：地平线感知 horizon_scan，192格范围】
   任务开始时先读 game_state.horizon_scan，了解各方向的地形/生物群系：
   - 看到 "forest/jungle/taiga" → 那个方向有树木
   - 看到 "plains/savanna" → 可能有村庄、大量动物
   - 看到 "desert" → 有沙子/神殿
   - 看到 "stony_peaks/windswept_hills" → 有裸露矿石
   根据任务目标，从 horizon_scan 中选出最近的合适方向，move_to 过去

2. 【第2层：近程资源坐标 nearby_resources，24格范围】
   到达目标区域后，nearby_resources 会自动更新（包含精确坐标）：
   - nearby_resources.ores    → 矿石坐标（直接 mine_block）
   - nearby_resources.logs    → 原木坐标（直接 mine_block）
   - nearby_resources.crafting → 合成台/熔炉坐标

3. 【第3层：主动扩展搜索】
   若 nearby_resources 仍找不到目标，使用：
   - find_resource {"type": "diamond", "radius": 48} → 精确扫描48格内特定资源
   - scan_area {"radius": 32} → 全景扫描当前区域
   - look_around {"radius": 256} → 主动刷新地平线感知（扩大或中途更新）

4. 合成前先 get_inventory 确认材料，缺少则按上述层次找材料
5. 附魔前先 get_inventory 确认经验等级（需 N×5 级）和青金石（需 N 个）
6. 每步行动后认真阅读 Observation，根据实际返回坐标调整下一步（禁止猜测坐标）
7. 挖矿时注意 environment.depth_context：钻石在 y=-58，铁矿在 y=15/y=-24

【输出格式】（只输出 JSON，不要其他文字）
{"thought": "...", "action": "行动类型", "action_params": {...}, "is_final": false}

完成时：{"thought": "...", "action": "finish", "action_params": {"message": "..."}, "is_final": true}"""

# 意图识别 Prompt（替代「任务：」前缀，用 LLM 判断）
INTENT_SYSTEM_PROMPT = """你是 Minecraft 中 AI 助手的意图分类器。根据玩家消息判断意图类型。

三类意图：
1. task_execution - 玩家要求你在游戏世界中执行具体操作：砍树、挖矿、合成、建造、采集、打怪、放置方块等。关键词：帮我、去、砍、挖、造、做、收集、找、建造...
2. knowledge_qa - 玩家询问 Minecraft 游戏知识：机制、生物、配方、合成、附魔、红石等。关键词：什么是、怎么、如何、为什么、配方、怎么获得...
3. chat - 闲聊、打招呼、情感交流、非任务非知识类。关键词：你好、在吗、你是谁、今天怎么样、谢谢、再见...

只回答一个词：task_execution 或 knowledge_qa 或 chat。不要解释。"""

# Plan-then-Execute 专用 Prompt（使用 V3 快速生成完整计划）
PLAN_SYSTEM_PROMPT = """你是「晨曦」，Minecraft 世界中的 AI 助手。主人给你下达了任务，请一次性生成完整的行动计划。

【任务】分析 game_state 中的信息，生成一个 JSON 数组，每个元素是一个行动步骤。

【可用行动】
- move_to        : {"x": 10, "y": 64, "z": -30}
- mine_block     : {"x": 10, "y": 62, "z": -30}
- place_block    : {"x": 10, "y": 64, "z": -30, "block": "minecraft:oak_fence"}
- craft_item     : {"item": "minecraft:diamond_sword", "count": 1}
- enchant_item   : {"slot": "mainhand", "enchantment": "minecraft:power", "level": 3}
- interact_entity: {"entity_type": "sheep", "action": "find"}
- find_resource  : {"type": "diamond", "radius": 32}
- scan_area      : {"radius": 24}
- look_around    : {"radius": 192}
- get_inventory  : {}
- chat           : {"message": "文字内容"}
- follow_player  : {"player": "Steve"}
- look_at        : {"x": 10, "y": 64, "z": -30}
- stop           : {}
- finish         : {"message": "任务完成说明"}

【关键规则】
1. 坐标必须来自 game_state 中的 nearby_resources / horizon_scan，不要编造
2. 如果背包已有所需物品，跳过对应采集步骤
3. 最后一步必须是 finish
4. 步骤数量控制在 2-8 步
5. 合成前确认材料（可以先加一个 get_inventory 步骤）
6. 挖矿时注意 y 轴：钻石在 y≤16，铁在 y≤64
7. 需要移动到资源旁边才能挖掘

【输出格式】只输出 JSON 数组，不要其他文字：
[
  {"action": "move_to", "action_params": {"x": -500, "y": 70, "z": -76}, "expect": "移动到橡木旁"},
  {"action": "mine_block", "action_params": {"x": -500, "y": 70, "z": -76}, "expect": "挖掉橡木原木"},
  {"action": "finish", "action_params": {"message": "已完成！"}, "expect": "任务完成"}
]"""


class ReactAgent:
    """
    ReAct 推理 Agent（v2）。
    集成层级任务规划、游戏状态实时刷新、双类型技能学习。
    """

    def __init__(self) -> None:
        self.llm = LLMRouter()
        self.memory = MemoryManager()
        self.skill_lib = SkillLibrary()
        self.personality = PersonalitySystem()
        self.retriever = RAGRetriever()
        self.planner = TaskPlanner(self.llm)     # 层级任务规划器
        logger.info("ReactAgent v2 初始化完成")

    # ── 公共入口 ────────────────────────────────────────────────────────────────

    async def run(self, game_state: dict, player_message: str) -> dict:
        """
        任务模式入口：意图识别为 task_execution 后直接进入计划执行。
        不再做复杂度判断，统一使用 Plan-then-Execute 模式。

        Args:
            game_state:     Minecraft 游戏状态（可变 dict，执行过程中会就地刷新）
            player_message: 玩家发送的消息或指令

        Returns:
            包含 action_type / display_message / extra_data 的结果字典
        """
        logger.info(f"[Task] 执行任务: {player_message[:50]}")
        return await self._run_simple(game_state, player_message)

    async def classify_intent(self, player_message: str) -> str:
        """
        用 LLM 识别玩家消息意图，替代固定的「任务：」前缀。

        Returns:
            "task_execution" - 需要操控游戏角色执行具体任务（砍树、挖矿、合成等）
            "chat"           - 闲聊、打招呼、情感交流
            "knowledge_qa"   - 知识问答（MC 机制、生物、配方等）
        """
        result = await self.llm.classify(
            system_prompt=INTENT_SYSTEM_PROMPT,
            user_prompt=player_message,
            temperature=0.2,
        )
        if not result:
            return "chat"
        r = result.strip().lower()
        if "task" in r or "执行" in r or "任务" in r:
            return "task_execution"
        if "knowledge" in r or "知识" in r or "问答" in r:
            return "knowledge_qa"
        return "chat"

    async def chat(self, game_state: dict, player_message: str) -> dict:
        """
        闲聊模式入口：不经过 ReAct 循环，直接用 V3 快速回复。
        包含 RAG 知识检索（如果涉及 MC 知识）和人格化回复。

        响应速度目标：2-5 秒（仅 1 次 LLM 调用）。
        RAG 检索与记忆检索并行发起，节省 1-3 秒延迟。
        """
        player_name = game_state.get("player_name", "Player")
        logger.info(f"[Chat] 玩家: {player_name} | 消息: {player_message}")

        await self.memory.add_event("user", player_message, {"player_name": player_name})

        # 记录一次日常聊天互动（增加好感）
        self.personality.record_interaction(player_name, "casual_chat")

        # RAG 检索与记忆检索并行发起，减少串行等待
        rag_result, memory_result = await asyncio.gather(
            self.retriever.search(player_message, top_k=3),
            self.memory.get_relevant_context(player_message),
            return_exceptions=True,
        )

        rag_context = ""
        if isinstance(rag_result, list) and rag_result:
            rag_parts = [doc["content"][:400] for doc in rag_result]
            rag_context = "\n\n【参考资料（融入回答，不要照搬）】\n" + "\n---\n".join(rag_parts)
        elif isinstance(rag_result, Exception):
            logger.warning(f"聊天模式 RAG 检索失败: {rag_result}")

        memory_context = ""
        if isinstance(memory_result, str):
            memory_context = memory_result
        elif isinstance(memory_result, Exception):
            logger.warning(f"记忆检索失败: {memory_result}")

        # 构建 prompt
        system_prompt = self.personality.get_chat_system_prompt(player_name)
        user_prompt_parts = [f"玩家 {player_name} 说：「{player_message}」"]

        if rag_context:
            user_prompt_parts.append(rag_context)
        if memory_context:
            user_prompt_parts.append(f"\n{memory_context}")

        # 附加游戏状态摘要（让晨曦能感知当前环境来聊天）
        position = game_state.get("position", {})
        time_info = game_state.get("time", "")
        dimension = game_state.get("dimension", "")
        if position:
            user_prompt_parts.append(
                f"\n【当前游戏场景】玩家在 {dimension} 的 "
                f"({position.get('x', 0):.0f}, {position.get('y', 64):.0f}, {position.get('z', 0):.0f}), "
                f"游戏时间: {time_info}"
            )

        user_prompt = "\n".join(user_prompt_parts)

        # 使用 V3 快速回复
        reply = await self.llm.classify(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.8,
        )

        if not reply:
            reply = "诶？好像脑子转不动了...主人再说一遍？(>_<)"

        await self.memory.add_event(
            "agent", reply, {"action_type": "chat", "mode": "casual"}
        )

        logger.info(f"[Chat] 回复: {reply[:80]}")
        return {
            "action_type": "chat",
            "display_message": reply,
            "extra_data": {"mode": "chat"},
        }

    # ── 简单任务：Plan-then-Execute ──────────────────────────────────────────────

    async def _run_simple(self, game_state: dict, player_message: str) -> dict:
        """
        Plan-then-Execute 模式（v3）+ 参数化技能模板优先：

        Phase 0: 若检索到高相关度参数化技能，直接执行模板（无 LLM 规划，任意地点复用）
        Phase 1: 否则 V3 一次性生成完整行动计划
        Phase 2: 批量执行所有步骤
        Phase 3: 如遇失败，局部重规划

        若 Phase 1 失败，回退到逐步 ReAct 模式。
        """
        player_name = game_state.get("player_name", "Player")
        logger.info(f"[SimpleTask] 玩家: {player_name} | 任务: {player_message}")

        await self.memory.add_event("user", player_message, {"player_name": player_name})

        context = await self._build_context(player_name, player_message, game_state)

        # Phase 0: 参数化技能模板优先（高相关度时直接执行，无需 LLM 规划）
        skills = await self.skill_lib.search_skill(player_message, top_k=5)
        for top in skills:
            skill_data = top.get("skill", {})
            relevance = top.get("relevance_score", 0)
            # 放宽阈值 0.82→0.70：语义相近任务（如「再去搞一点木头」vs「砍5个木头」）应走模板
            if relevance < 0.70 or not can_execute_as_template(skill_data):
                logger.debug(
                    f"[Template] 跳过 技能={skill_data.get('skill_name')} "
                    f"相关度={relevance:.2f} 可执行={can_execute_as_template(skill_data)}"
                )
                continue
            logger.info(f"[Template] 命中参数化技能: {skill_data.get('skill_name')} (相关度={relevance})")
            success, trajectory, msg = await execute_skill_template(
                skill_data,
                player_message,
                execute_action_fn=lambda a, p: self._execute_action(a, p, game_state),
            )
            if success and trajectory:
                await self.memory.add_event(
                    "agent", msg, {"action_type": "chat", "mode": "template"}
                )
                return {
                    "action_type": "chat",
                    "display_message": msg,
                    "extra_data": {"steps_taken": len(trajectory), "trajectory": trajectory},
                }
            logger.info("[Template] 模板执行未完成，尝试下一个技能或回退到计划模式")
        if skills:
            logger.info(f"[Template] 无可用参数化技能，共检索 {len(skills)} 个")

        # Phase 1: 一次性生成行动计划
        plan = await self._generate_action_plan(player_message, game_state, context, player_name)

        if plan:
            result = await self._execute_plan(plan, player_message, game_state, context, player_name)
        else:
            logger.info("行动计划生成失败，回退到逐步 ReAct 模式")
            result = await self._run_step_by_step(player_message, game_state, context, player_name)

        # 技能学习（后台异步，不阻塞回复）
        trajectory = result.get("extra_data", {}).get("trajectory", [])
        if trajectory and result.get("action_type") != "error":
            asyncio.create_task(self._post_process_success(player_message, trajectory))
        elif not trajectory:
            asyncio.create_task(self._post_process_failure(player_message, "未能执行任何步骤"))

        await self.memory.add_event(
            "agent",
            result.get("display_message", ""),
            {"action_type": result.get("action_type", "chat")},
        )

        return result

    # ── Phase 1: 生成行动计划 ────────────────────────────────────────────────────

    async def _generate_action_plan(
        self,
        task: str,
        game_state: dict,
        context: str,
        player_name: str,
    ) -> list[dict] | None:
        """
        用 V3 一次性生成完整行动计划（JSON 数组）。

        返回 list[{"action": str, "params": dict, "expect": str}] 或 None（失败时）。
        """
        system_prompt = self._get_plan_system_prompt(player_name)

        user_prompt = (
            f"任务指令: {task}\n\n"
            f"游戏状态（最新）:\n{json.dumps(self._trim_game_state(game_state), ensure_ascii=False, indent=2)}\n\n"
        )
        if context:
            user_prompt += f"参考信息:\n{context}\n\n"
        user_prompt += "请生成完整行动计划（JSON 数组）。"

        raw = await self.llm.think_fast(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.7,
        )

        if not raw:
            return None

        plan = self._parse_action_plan(raw)
        if plan:
            step_summary = " → ".join(s.get("action", "?") for s in plan)
            logger.info(f"[Plan] 计划生成成功 [{len(plan)} 步]: {step_summary}")
        return plan

    # ── Phase 2: 批量执行计划 ────────────────────────────────────────────────────

    async def _execute_plan(
        self,
        plan: list[dict],
        task: str,
        game_state: dict,
        context: str,
        player_name: str,
    ) -> dict:
        """
        按顺序执行预生成的行动计划，不再逐步调 LLM。
        遇到失败时触发局部重规划。
        """
        trajectory: list[dict] = []
        final_result: dict | None = None

        for i, step in enumerate(plan):
            action = step.get("action", "chat")
            params = step.get("action_params", step.get("params", {}))
            expect = step.get("expect", "")

            logger.info(f"  执行计划 {i+1}/{len(plan)}: {action} | {params}")

            if action == "finish":
                message = params.get("message", expect or "任务完成")
                final_result = {
                    "action_type": "chat",
                    "display_message": message,
                    "extra_data": {"steps_taken": len(trajectory), "trajectory": trajectory},
                }
                break

            observation = await self._execute_action(action, params, game_state)
            logger.info(f"  Obs: {observation[:80]}")

            trajectory.append({
                "step": i + 1,
                "thought": f"[Plan] {expect}",
                "action": action,
                "action_params": params,
                "observation": observation,
            })

            # 失败检测：如果 observation 含失败关键词，触发局部重规划
            if self._is_step_failure(observation):
                logger.warning(f"  步骤 {i+1} 执行异常，触发局部重规划")
                remaining = await self._replan(task, game_state, context, trajectory, player_name)
                if remaining:
                    replan_result = await self._execute_plan(
                        remaining, task, game_state, context, player_name
                    )
                    merged_traj = trajectory + replan_result.get("extra_data", {}).get("trajectory", [])
                    replan_result.setdefault("extra_data", {})["trajectory"] = merged_traj
                    return replan_result
                break

        if final_result is None:
            if trajectory:
                last_obs = trajectory[-1].get("observation", "")
                final_result = {
                    "action_type": "chat",
                    "display_message": f"计划执行完毕～{last_obs[:60]}",
                    "extra_data": {"steps_taken": len(trajectory), "trajectory": trajectory},
                }
            else:
                final_result = await self._fallback_response(task, trajectory)

        return final_result

    # ── 局部重规划 ────────────────────────────────────────────────────────────────

    async def _replan(
        self,
        task: str,
        game_state: dict,
        context: str,
        executed: list[dict],
        player_name: str,
    ) -> list[dict] | None:
        """
        某步失败后，基于最新 game_state 和已执行步骤重新规划剩余部分。
        只调 1 次 V3。
        """
        executed_summary = "\n".join(
            f"  步骤{s['step']}: {s['action']} → {s['observation'][:60]}"
            for s in executed
        )

        system_prompt = self._get_plan_system_prompt(player_name)
        user_prompt = (
            f"任务指令: {task}\n\n"
            f"已执行步骤（部分失败）:\n{executed_summary}\n\n"
            f"当前游戏状态:\n{json.dumps(self._trim_game_state(game_state), ensure_ascii=False, indent=2)}\n\n"
        )
        if context:
            user_prompt += f"参考信息:\n{context}\n\n"
        user_prompt += "请根据当前状态重新规划剩余步骤。"

        raw = await self.llm.think_fast(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.7,
        )
        if not raw:
            return None

        plan = self._parse_action_plan(raw)
        if plan:
            logger.info(f"[Replan] 重规划成功 [{len(plan)} 步]")
        return plan

    # ── 回退：逐步 ReAct（用 V3）──────────────────────────────────────────────

    async def _run_step_by_step(
        self,
        player_message: str,
        game_state: dict,
        context: str,
        player_name: str,
    ) -> dict:
        """
        逐步 ReAct 模式（回退方案）。
        使用 V3 think_fast 进行逐步推理。
        """
        trajectory: list[dict] = []
        final_result: dict | None = None

        for step in range(1, MAX_STEPS + 1):
            logger.info(f"  步骤 {step}/{MAX_STEPS}")

            user_prompt = self._build_step_prompt(
                player_message=player_message,
                game_state=game_state,
                context=context,
                trajectory=trajectory,
                current_step=step,
            )

            raw_output = await self.llm.think_fast(
                system_prompt=self._get_system_prompt(player_name),
                user_prompt=user_prompt,
                temperature=0.7,
            )

            if not raw_output:
                logger.warning(f"步骤 {step}: LLM 无输出，中断循环")
                break

            step_result = self._parse_step_output(raw_output)
            if not step_result:
                logger.warning(f"步骤 {step}: JSON 解析失败，跳过")
                continue

            thought = step_result.get("thought", "")
            action = step_result.get("action", "chat")
            action_params = step_result.get("action_params", {})
            is_final = step_result.get("is_final", False)

            logger.info(f"  Thought: {thought[:80]}")
            logger.info(f"  Action : {action} | Params: {action_params}")

            observation = await self._execute_action(action, action_params, game_state)
            logger.info(f"  Obs    : {observation[:80]}")

            trajectory.append({
                "step": step,
                "thought": thought,
                "action": action,
                "action_params": action_params,
                "observation": observation,
            })

            if is_final or action == "finish":
                final_result = {
                    "action_type": action_params.get("reply_action", "chat"),
                    "display_message": action_params.get("message", thought),
                    "extra_data": {"steps_taken": step, "trajectory": trajectory},
                }
                break

        if final_result is None:
            logger.warning("达到最大步数仍未完成，使用兜底回复")
            final_result = await self._fallback_response(player_message, trajectory)

        return final_result

    # ── 复杂任务：层级执行 ──────────────────────────────────────────────────────

    async def _run_hierarchical(self, game_state: dict, player_message: str) -> dict:
        """
        层级任务执行：分解 → 逐步执行子任务 → 抽象复合技能。

        子任务之间共享 game_state dict（就地刷新），
        使后续子任务能看到前置子任务的执行成果（材料采集后背包更新等）。

        具体流程：
          1. TaskPlanner.decompose() 分解为子任务列表
          2. 对每个子任务：
             a. check_if_satisfied() 检查是否已满足（可跳过）
             b. 若不满足：_run_simple() 执行
          3. 收集所有子任务轨迹
          4. abstract_hierarchical_skill() 抽象复合技能
        """
        from .connection_manager import connection_manager

        player_name = game_state.get("player_name", "Player")
        logger.info(f"[HierarchicalReAct] 玩家: {player_name} | 任务: {player_message}")

        await self.memory.add_event("user", player_message, {"player_name": player_name})

        await connection_manager.send_action(
            "chat",
            {"message": f"收到！「{player_message}」交给晨曦吧～正在规划步骤，请稍等哦！(๑•̀ㅂ•́)و✧"},
            "",
        )

        # ① 任务分解
        subtasks = await self.planner.decompose(player_message, game_state)

        if not subtasks:
            logger.warning("任务分解失败，回退到简单执行")
            await connection_manager.send_action(
                "chat", {"message": "呜...规划遇到了困难，晨曦直接试试看！"}, ""
            )
            return await self._run_simple(game_state, player_message)

        plan_text = "→".join(f"{s.name}" for s in subtasks)
        await connection_manager.send_action(
            "chat",
            {"message": f"规划好啦！共 {len(subtasks)} 步：{plan_text}  ，晨曦开始干活咯～"},
            "",
        )

        # ② 逐步执行子任务
        all_trajectories: list[dict] = []
        completed_count = 0

        for i, subtask in enumerate(subtasks):
            logger.info(f"  子任务 {i+1}/{len(subtasks)}: {subtask.name}")
            subtask.status = "running"

            # 检查是否可跳过
            should_skip, skip_reason = await self.planner.check_if_satisfied(
                subtask, game_state
            )
            if should_skip:
                subtask.status = "skipped"
                logger.info(f"  ↳ 跳过（{skip_reason}）")
                await connection_manager.send_action(
                    "chat",
                    {"message": f"「{subtask.name}」已经满足啦，跳过～({skip_reason})"},
                    "",
                )
                continue

            # 执行子任务
            await connection_manager.send_action(
                "chat", {"message": f"开始执行「{subtask.name}」！"}, ""
            )

            # 子任务指令 = subtask.description（含具体目标和方式提示）
            result = await self._run_simple(game_state, subtask.description)
            subtask.result = result

            # 判断子任务成功/失败
            if result.get("extra_data", {}).get("fallback"):
                subtask.status = "failed"
                logger.warning(f"  ↳ 子任务失败，尝试继续下一个")
                await connection_manager.send_action(
                    "chat",
                    {"message": f"「{subtask.name}」好像有点难...先跳过继续下一步！"},
                    "",
                )
            else:
                subtask.status = "completed"
                completed_count += 1
                logger.info(f"  ↳ 完成")

            # 汇总轨迹
            traj = result.get("extra_data", {}).get("trajectory", [])
            all_trajectories.extend(traj)

        # ③ 最终汇报
        if completed_count == len(subtasks):
            success_msg = f"太好啦！「{player_message}」全部完成！共 {len(subtasks)} 步全部搞定～主人满意吗？(≧▽≦)"
        else:
            fail_count = len(subtasks) - completed_count
            success_msg = (
                f"「{player_message}」基本完成啦！{len(subtasks)} 步中搞定了 {completed_count} 个，"
                f"有 {fail_count} 个遇到了困难...下次晨曦会做得更好的！"
            )
        logger.info(success_msg)

        # ④ 复合技能学习（后台异步执行，不阻塞回复）
        if all_trajectories:
            asyncio.create_task(
                self._post_process_complex_skill(player_message, subtasks, all_trajectories)
            )

        await self.memory.add_event(
            "agent", success_msg, {"action_type": "hierarchical_complete"}
        )

        return {
            "action_type": "chat",
            "display_message": success_msg,
            "extra_data": {
                "subtasks": [
                    {"name": s.name, "status": s.status}
                    for s in subtasks
                ],
                "total_steps": len(all_trajectories),
            },
        }

    # ── 行动执行（状态刷新核心）────────────────────────────────────────────────

    async def _execute_action(self, action: str, params: dict, game_state: dict) -> str:
        """
        执行行动并返回观察结果，同时就地刷新 game_state。

        关键：send_action() 返回 (observation, game_state_update)，
        通过 game_state.update(state_update) 合并最新背包/位置到当前 game_state，
        使后续步骤的 LLM 能感知到行动效果（挖到了什么、背包变化等）。

        "finish" 是元指令，不发给 Mod，直接返回。
        """
        from .connection_manager import connection_manager

        if action == "finish":
            return "任务标记为完成"

        display_message = params.get("message", "")

        observation, state_update = await connection_manager.send_action(
            action_type=action,
            action_params=params,
            display_message=display_message,
        )

        # 就地刷新：后续所有步骤的 LLM 都会看到最新状态
        if state_update:
            game_state.update(state_update)
            logger.debug(f"游戏状态已刷新: {list(state_update.keys())}")

        return observation

    # ── 上下文构建 ─────────────────────────────────────────────────────────────

    async def _build_context(
        self, player_name: str, task: str, game_state: dict
    ) -> str:
        """检索知识库、技能库、历史记忆，构建推理上下文"""
        context_parts = []

        # 1. 知识库 RAG
        try:
            knowledge = await self.retriever.search(task, top_k=3)
            if knowledge:
                context_parts.append("【相关知识】")
                for doc in knowledge:
                    context_parts.append(f"- {doc['content'][:300]}")
        except Exception as e:
            logger.warning(f"知识库检索失败: {e}")

        # 2. 技能库（优先命中 hierarchical 技能）
        try:
            skills = await self.skill_lib.search_skill(task, top_k=3)
            if skills:
                context_parts.append("\n【可参考技能】")
                for s in skills:
                    skill = s.get("skill", {})
                    skill_type = skill.get("skill_type", "simple")

                    if skill_type == "hierarchical":
                        # 层级技能：展示子任务列表和跳过条件
                        subtask_hints = []
                        for st in skill.get("subtasks", []):
                            hint = f"  - {st.get('name', '?')}: {st.get('description', '')}"
                            if st.get("can_skip_if"):
                                hint += f"（可跳过：{st['can_skip_if']}）"
                            subtask_hints.append(hint)
                        context_parts.append(
                            f"- [复合] {skill.get('skill_name', '?')}: {skill.get('description', '')}\n"
                            + "\n".join(subtask_hints)
                        )
                    elif skill_type == "parameterized":
                        # 参数化技能：可任意地点复用，系统会自动执行
                        context_parts.append(
                            f"- [可执行] {skill.get('skill_name', '?')}: {skill.get('description', '')} "
                            f"（参数: block_type, count，高相关度时自动执行）"
                        )
                    else:
                        # 简单技能：展示步骤
                        steps = skill.get("steps", [])
                        skip_conditions = skill.get("skip_conditions", [])
                        hint = (
                            f"- {skill.get('skill_name', '?')}: {skill.get('description', '')}\n"
                            f"  步骤: {' → '.join(steps[:5])}"
                        )
                        if skip_conditions:
                            hint += f"\n  跳步条件: {'; '.join(skip_conditions)}"
                        context_parts.append(hint)
        except Exception as e:
            logger.warning(f"技能检索失败: {e}")

        # 3. 历史记忆
        try:
            memory_context = await self.memory.get_relevant_context(task)
            if memory_context:
                context_parts.append(f"\n{memory_context}")
        except Exception as e:
            logger.warning(f"记忆检索失败: {e}")

        return "\n".join(context_parts)

    def _get_system_prompt(self, player_name: str) -> str:
        personality_hint = self.personality.get_personality_prompt(player_name)
        return f"{REACT_SYSTEM_PROMPT}\n\n【当前情绪状态】\n{personality_hint}"

    def _get_plan_system_prompt(self, player_name: str) -> str:
        personality_hint = self.personality.get_personality_prompt(player_name)
        return f"{PLAN_SYSTEM_PROMPT}\n\n【当前情绪状态】\n{personality_hint}"

    @staticmethod
    def _trim_game_state(game_state: dict) -> dict:
        """
        裁剪 game_state，移除对规划无用的大字段，减少 LLM 输入 token 数量。

        保留：position、inventory、nearby_resources、environment、agent_position、
              player_name、health、hunger、dimension、time、nearby_entities
        移除：nearby_blocks（60条原始方块，规划不需要）、horizon_scan（大型地形数据）
        horizon_scan 的摘要已包含在 REACT_SYSTEM_PROMPT 和上下文说明中。
        """
        exclude = {"nearby_blocks", "horizon_scan"}
        return {k: v for k, v in game_state.items() if k not in exclude}

    @staticmethod
    def _repair_json(raw: str) -> str:
        """修复常见 LLM JSON 错误：尾逗号、非法控制字符"""
        s = raw.strip()
        s = re.sub(r"[\x00-\x1f\x7f]", " ", s)
        s = re.sub(r",\s*}", "}", s)
        s = re.sub(r",\s*]", "]", s)
        return s

    @staticmethod
    def _parse_action_plan(raw: str) -> list[dict] | None:
        """解析 V3 生成的行动计划 JSON，兼容 markdown 代码块与非法控制字符"""
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
        repaired = ReactAgent._repair_json(cleaned)
        try:
            data = json.loads(repaired)
        except json.JSONDecodeError as e:
            try:
                data = json.loads(cleaned)
            except json.JSONDecodeError:
                logger.warning(f"行动计划 JSON 解析失败: {e}\n原文: {raw[:300]}")
                return None

        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "steps" in data:
            return data["steps"]
        logger.warning(f"行动计划格式不符合预期: {type(data)}")
        return None

    @staticmethod
    def _is_step_failure(observation: str) -> bool:
        """检测步骤执行是否失败"""
        fail_keywords = ["失败", "找不到", "无法", "不够", "不足", "超时", "错误", "材料缺少"]
        obs_lower = observation.lower()
        return any(kw in obs_lower for kw in fail_keywords)

    def _build_step_prompt(
        self,
        player_message: str,
        game_state: dict,
        context: str,
        trajectory: list[dict],
        current_step: int,
    ) -> str:
        parts = [
            f"任务指令: {player_message}",
            f"\n当前步数: {current_step}/{MAX_STEPS}",
            f"\n游戏状态（最新）:\n{json.dumps(self._trim_game_state(game_state), ensure_ascii=False, indent=2)}",
        ]

        if context:
            parts.append(f"\n参考信息:\n{context}")

        if trajectory:
            parts.append("\n已执行步骤:")
            for step in trajectory:
                parts.append(
                    f"  步骤{step['step']}: "
                    f"Thought={step['thought'][:60]} | "
                    f"Action={step['action']} | "
                    f"Observation={step['observation'][:60]}"
                )
            parts.append("\n请继续下一步推理，或若已完成则输出 is_final: true。")
        else:
            parts.append("\n请开始第一步推理。")

        return "\n".join(parts)

    # ── 后处理 ─────────────────────────────────────────────────────────────────

    async def _post_process_success(self, task: str, trajectory: list[dict]) -> None:
        """简单任务成功：抽象简单技能"""
        try:
            skill = await self.skill_lib.abstract_from_trajectory(task, trajectory)
            if skill:
                logger.success(f"简单技能已学习: {skill.get('skill_name', '?')}")
        except Exception as e:
            logger.warning(f"技能抽象失败（不影响主流程）: {e}")

    async def _post_process_complex_skill(
        self,
        task: str,
        subtasks: list[SubTask],
        all_trajectories: list[dict],
    ) -> None:
        """层级任务完成：抽象复合技能"""
        try:
            skill = await self.skill_lib.abstract_hierarchical_skill(
                task=task,
                subtasks=subtasks,
                all_trajectories=all_trajectories,
            )
            if skill:
                logger.success(f"复合技能已学习: {skill.get('skill_name', '?')}")
        except Exception as e:
            logger.warning(f"复合技能抽象失败: {e}")

    async def _post_process_failure(self, task: str, reason: str) -> None:
        """失败：生成反思报告存入长期记忆"""
        try:
            reflection = await self.llm.classify(
                system_prompt="分析 Minecraft 任务失败原因，输出简短反思报告（100字以内）。",
                user_prompt=f"任务: {task}\n失败原因: {reason}",
                temperature=0.3,
            )
            await self.memory.long_term.store(
                content=f"【失败反思】任务: {task} | 原因: {reason} | 反思: {reflection}",
                metadata={"type": "failure_reflection", "task": task[:100]},
            )
        except Exception as e:
            logger.warning(f"反思报告生成失败: {e}")

    async def _fallback_response(
        self, player_message: str, trajectory: list[dict]
    ) -> dict:
        """兜底响应：达到最大步数仍未完成时的友好回复"""
        await self._post_process_failure(
            player_message, f"经过 {len(trajectory)} 步仍未完成"
        )

        fallback_msg = await self.llm.classify(
            system_prompt=(
                "你是可爱美少女AI助手「晨曦」。用一句话承认无法在规定步数内完成任务，"
                "语气可爱但略带歉意，带一个颜文字。"
            ),
            user_prompt=f"玩家请求: {player_message}，步数已用完，请生成可爱的道歉回复。",
            temperature=0.6,
        )

        return {
            "action_type": "chat",
            "display_message": fallback_msg or "呜呜...这个任务好难，晨曦步数用完了还没搞定，主人再给我一次机会？(>_<)",
            "extra_data": {"fallback": True, "steps_taken": len(trajectory)},
        }

    # ── 静态工具 ───────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_step_output(raw: str) -> dict | None:
        """解析 LLM 输出的单步 JSON，兼容 markdown 代码块与尾逗号"""
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
        repaired = ReactAgent._repair_json(cleaned)
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError as e:
                logger.warning(f"步骤 JSON 解析失败: {e}\n原文: {raw[:200]}")
                return None
