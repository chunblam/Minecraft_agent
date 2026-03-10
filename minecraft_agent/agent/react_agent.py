"""
VoyagerAgent — mineflayer 版执行引擎

相比 Java Fabric Mod 版的核心变化：
  - send_action() 改为调用 env.send_action()（HTTP → mineflayer）
  - 移动、挖掘由 mineflayer pathfinder 原生处理，更可靠
  - move_to 超时问题基本消失（mineflayer 自带避障）

Voyager 核心机制完整保留：
  1. 迭代重试 rollout（最多 MAX_RETRIES 次）
  2. CriticAgent 验证每次执行结果
  3. critique 注入下一轮 prompt
  4. 技能只在验证成功后存储
"""

import asyncio
import json
import os
import re
from loguru import logger

from .llm_router import LLMRouter
from .memory import MemoryManager
from .skill_library import SkillLibrary
from .critic import CriticAgent
from .personality import PersonalitySystem
from .planner import TaskPlanner, SubTask
from .prompts import (
    ACTION_SYSTEM_PROMPT,
    ACTION_HUMAN_TEMPLATE,
    CHAT_SYSTEM_PROMPT,
    INTENT_SYSTEM_PROMPT,
    COMPLEXITY_SYSTEM_PROMPT,
)

try:
    from rag.retriever import RAGRetriever
    RAG_AVAILABLE = True
except ImportError:
    RAG_AVAILABLE = False

MAX_STEPS = int(os.getenv("MAX_REACT_STEPS", "12"))
MAX_RETRIES = int(os.getenv("MAX_TASK_RETRIES", "4"))


class VoyagerAgent:
    def __init__(
        self,
        llm: LLMRouter,
        memory: MemoryManager,
        skill_lib: SkillLibrary,
        personality: PersonalitySystem = None,
        retriever=None,
    ):
        self.llm = llm
        self.memory = memory
        self.skill_lib = skill_lib
        self.personality = personality or PersonalitySystem()
        self.retriever = retriever
        self.critic = CriticAgent(llm)
        self.planner = TaskPlanner(llm)

    # ═══════════════════════════════════════════════════════════════════════
    # 公共入口
    # ═══════════════════════════════════════════════════════════════════════

    async def run(self, game_state: dict, player_message: str) -> dict:
        player_name = game_state.get("player_name", "Player")
        await self.memory.add_event("user", player_message, {"player_name": player_name})

        intent = await self._classify_intent(player_message)
        if intent != "task_execution":
            return await self.chat(game_state, player_message)

        complexity = await self._classify_complexity(player_message, game_state)
        if complexity == "complex":
            result = await self._run_hierarchical(game_state, player_message)
        else:
            result = await self._rollout(player_message, game_state)

        await self.memory.add_event("agent", result.get("display_message", ""), {})
        return result

    async def chat(self, game_state: dict, player_message: str) -> dict:
        player_name = game_state.get("player_name", "Player")
        self.personality.record_interaction(player_name, "casual_chat")
        personality_hint = self.personality.get_personality_prompt(player_name)
        system = f"{CHAT_SYSTEM_PROMPT}\n\n情绪状态: {personality_hint}"
        reply = await self.llm.think_fast(system_prompt=system, user_prompt=player_message, temperature=0.8)
        return {"action_type": "chat", "display_message": reply or "嗯嗯！", "extra_data": {}}

    # ═══════════════════════════════════════════════════════════════════════
    # Voyager 核心：迭代重试 rollout
    # ═══════════════════════════════════════════════════════════════════════

    async def _rollout(self, task: str, game_state: dict, context: str = "") -> dict:
        if not context:
            context = await self._build_context(task, game_state)

        critique = ""
        last_observation = ""
        best_trajectory = []

        for attempt in range(1, MAX_RETRIES + 1):
            logger.info(f"[Rollout] '{task[:50]}' 第 {attempt}/{MAX_RETRIES} 次")

            trajectory, last_observation, _ = await self._run_steps(
                task=task, game_state=game_state,
                context=context, critique=critique,
            )
            if trajectory:
                best_trajectory = trajectory

            # Critic 验证
            success, critique = await self.critic.check_task_success(
                task=task, game_state=game_state, last_observation=last_observation,
            )
            logger.info(f"[Critic] success={success} | {critique[:60]}")

            if success:
                asyncio.create_task(self._store_verified_skill(task, best_trajectory))
                msg = self._extract_finish_message(trajectory) or f"✅ 完成：{task}"
                return {"action_type": "chat", "display_message": msg,
                        "extra_data": {"success": True, "attempts": attempt}}

            logger.warning(f"[Rollout] 第{attempt}次失败，critique={critique}")

        return {"action_type": "chat",
                "display_message": f"尝试了 {MAX_RETRIES} 次，未能完成「{task}」。{critique}",
                "extra_data": {"success": False, "critique": critique}}

    # ── ReAct 步骤循环 ────────────────────────────────────────────────────────

    async def _run_steps(
        self, task: str, game_state: dict, context: str, critique: str = ""
    ) -> tuple[list[dict], str, bool]:
        """执行最多 MAX_STEPS 步 ReAct 循环"""
        from .env import get_env
        env = get_env()

        trajectory = []
        last_obs = ""

        for step in range(1, MAX_STEPS + 1):
            logger.info(f"  Step {step}/{MAX_STEPS}")

            user_prompt = self._build_step_prompt(
                task=task, game_state=game_state, context=context,
                trajectory=trajectory, critique=critique, current_step=step,
            )

            raw = await self.llm.think_fast(
                system_prompt=ACTION_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                temperature=0.5,
            )
            if not raw:
                break

            step_data = self._parse_step_output(raw)
            if not step_data:
                logger.warning(f"  JSON 解析失败: {raw[:80]}")
                continue

            thought = step_data.get("thought", "")
            action = step_data.get("action", "chat")
            params = step_data.get("action_params", {})
            is_final = step_data.get("is_final", False)

            logger.info(f"  Thought: {thought[:80]}")
            logger.info(f"  Action : {action} | {params}")

            if action == "finish":
                trajectory.append({"step": step, "thought": thought, "action": action,
                                    "action_params": params, "observation": "finish"})
                return trajectory, "finish", True

            # 执行 action（mineflayer HTTP）
            observation, state_update = await env.send_action(
                action_type=action,
                action_params=params,
                display_message=params.get("message", ""),
            )
            if state_update:
                game_state.update(state_update)

            last_obs = observation
            logger.info(f"  Obs: {observation[:100]}")

            trajectory.append({
                "step": step, "thought": thought,
                "action": action, "action_params": params,
                "observation": observation,
            })

            if is_final:
                return trajectory, last_obs, True

        return trajectory, last_obs, False

    # ── 层级任务 ──────────────────────────────────────────────────────────────

    async def _run_hierarchical(self, game_state: dict, task: str) -> dict:
        subtasks = await self.planner.decompose(task, game_state)
        logger.info(f"[Hierarchical] 分解为 {len(subtasks)} 个子任务")

        completed, failed = [], []
        all_traj = []

        for i, st in enumerate(subtasks):
            logger.info(f"[Sub {i+1}/{len(subtasks)}] {st.name}")
            if await self.planner.check_if_satisfied(st, game_state):
                st.status = "skipped"
                completed.append(st.name)
                continue

            st.status = "running"
            ctx = await self._build_context(st.description, game_state)
            result = await self._rollout(task=st.description, game_state=game_state, context=ctx)

            if result.get("extra_data", {}).get("success"):
                st.status = "done"
                completed.append(st.name)
                all_traj.extend(result.get("extra_data", {}).get("trajectory", []))
            else:
                st.status = "failed"
                failed.append(st.name)
                if not st.can_skip_if:
                    break

        success = len(completed) >= len(subtasks) * 0.7
        if success and all_traj:
            asyncio.create_task(self._store_hierarchical_skill(task, subtasks, all_traj))

        msg = (f"完成 {len(completed)}/{len(subtasks)} 个子任务"
               + (f"（{', '.join(completed)}）" if completed else "")
               + (f"，失败：{', '.join(failed)}" if failed else ""))
        return {"action_type": "chat", "display_message": msg,
                "extra_data": {"success": success, "subtasks": [{"name": s.name, "status": s.status} for s in subtasks]}}

    # ── Prompt 构建 ────────────────────────────────────────────────────────────

    def _build_step_prompt(self, task, game_state, context, trajectory, critique, current_step):
        trimmed = {k: v for k, v in game_state.items() if k not in {"nearby_blocks", "horizon_scan"}}
        gs_str = json.dumps(trimmed, ensure_ascii=False, indent=2)

        traj_lines = []
        for t in trajectory[-6:]:
            traj_lines.append(f"Step {t['step']}: {t['action']} → {t['observation'][:80]}")
        traj_str = "\n".join(traj_lines) or "（无历史步骤）"

        critique_section = f"⚠️ PREVIOUS ATTEMPT FAILED: {critique}\nAdjust approach." if critique else ""

        return ACTION_HUMAN_TEMPLATE.format(
            task=task, context=context or "N/A",
            game_state=gs_str, trajectory=traj_str,
            critique_section=critique_section,
        )

    async def _build_context(self, task: str, game_state: dict) -> str:
        parts = []
        # RAG 知识库：注入与任务相关的 MC 文档（合成表、机制等），减少幻觉、补全模型知识
        if self.retriever:
            try:
                rag_results = await self.retriever.search(task, top_k=3)
                if rag_results:
                    parts.append("Reference (knowledge base):")
                    for r in rag_results:
                        content = (r.get("content") or "")[:600].strip()
                        if content:
                            parts.append(f"- {content}")
            except Exception:
                pass
        try:
            skills = await self.skill_lib.search_skill(task, top_k=2)
            if skills:
                parts.append("Relevant skills:")
                for item in skills:
                    sk = item["skill"]
                    steps = [s.get("description", s.get("action", "?")) for s in sk.get("steps", [])]
                    parts.append(f"- {sk.get('name')}: {sk.get('description', '')} | {' → '.join(steps[:4])}")
        except Exception:
            pass
        return "\n".join(parts)

    # ── 技能存储 ───────────────────────────────────────────────────────────────

    async def _store_verified_skill(self, task, trajectory):
        try:
            skill = await self.skill_lib.abstract_from_trajectory(task, trajectory, verified_success=True)
            if skill:
                logger.info(f"[Skill] ✅ 存储: {skill.get('name')}")
        except Exception as e:
            logger.warning(f"[Skill] 失败: {e}")

    async def _store_hierarchical_skill(self, task, subtasks, trajectories):
        summary = "\n".join(f"- [{s.status}] {s.name}" for s in subtasks)
        await self._store_verified_skill(f"{task}\nSubtasks:\n{summary}", trajectories[:15])

    # ── 分类 ───────────────────────────────────────────────────────────────────

    async def _classify_intent(self, message: str) -> str:
        msg = message.strip().lower()
        if any(kw in msg for kw in ["帮我", "去", "砍", "挖", "造", "做", "收集", "找", "建"]):
            return "task_execution"
        if any(kw in msg for kw in ["你好", "嗨", "谢谢", "晨曦"]):
            return "chat"
        result = await self.llm.classify(system_prompt=INTENT_SYSTEM_PROMPT, user_prompt=message, temperature=0.0)
        if result and "task" in result.lower():
            return "task_execution"
        return "chat"

    async def _classify_complexity(self, message: str, game_state: dict) -> str:
        msg = message.strip().lower()
        if any(kw in msg for kw in ["造", "建", "建造", "合成", "养", "围栏", "农场"]):
            return "complex"
        if any(kw in msg for kw in ["去", "砍", "挖", "找", "采集"]):
            return "simple"
        result = await self.llm.classify(system_prompt=COMPLEXITY_SYSTEM_PROMPT, user_prompt=message, temperature=0.0)
        return "complex" if result and "complex" in result.lower() else "simple"

    # ── 工具 ───────────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_step_output(raw: str) -> dict | None:
        text = raw.strip()
        if "```" in text:
            text = "\n".join(l for l in text.split("\n") if not l.strip().startswith("```"))
        text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
        text = re.sub(r",\s*}", "}", text)
        text = re.sub(r",\s*]", "]", text)
        for m in reversed(list(re.finditer(r'\{[^{}]*\}', text, re.DOTALL))):
            try:
                data = json.loads(m.group(0))
                if "action" in data:
                    return data
            except json.JSONDecodeError:
                continue
        return None

    @staticmethod
    def _extract_finish_message(trajectory: list[dict]) -> str | None:
        for step in reversed(trajectory):
            if step.get("action") == "finish":
                return step.get("action_params", {}).get("message", "")
        return None
