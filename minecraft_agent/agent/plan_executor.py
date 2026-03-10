"""
plan_executor.py — 分层执行引擎（混合方案核心）

实现文档中推荐的「综合更好的形式」：
  ┌─────────────────────────────────────────────────────────────┐
  │  输入任务                                                    │
  │    ↓                                                        │
  │  路由判断                                                    │
  │    ├── 模板命中  → SkillExecutor（0 LLM，最快）              │
  │    ├── 简单/已知 → 批量计划执行（1-2 LLM，Node.js连续执行）  │
  │    └── 复杂/多变 → ReAct 单步（N LLM，每步实时决策）         │
  └─────────────────────────────────────────────────────────────┘

批量计划执行（PlanMode）的关键：
  - LLM 一次性输出完整 action 序列（JSON 数组）
  - 通过 /execute_plan 在 Node.js 内连续执行，无 Python 往返
  - 中途状态变化时可触发 replan（局部 ReAct）
  - 达到人类玩家级别的连贯感

执行模式优先级：
  1. TEMPLATE  - 参数化技能模板（skill_executor），最快，0 额外 LLM
  2. PLAN      - 一次生成完整计划，批量 Node.js 执行
  3. REACT     - 逐步 ReAct，每步 LLM，最慢但最自适应
"""

import asyncio
import json
import re
import time
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable

import aiohttp
from loguru import logger

from .llm_router import LLMRouter
from .skill_executor import can_execute_as_template, execute_skill_template
from .prompts import (
    PLAN_GENERATION_SYSTEM_PROMPT,
    PLAN_GENERATION_HUMAN_TEMPLATE,
    ACTION_SYSTEM_PROMPT,
    ACTION_HUMAN_TEMPLATE,
)


# ─────────────────────────────────────────────────────────────────────────────
class ExecutionMode(str, Enum):
    TEMPLATE = "template"   # 参数化技能模板
    PLAN     = "plan"       # 一次生成完整计划
    REACT    = "react"      # 逐步 ReAct


@dataclass
class ExecutionResult:
    success: bool
    mode_used: ExecutionMode
    trajectory: list[dict] = field(default_factory=list)
    message: str = ""
    final_state: dict = field(default_factory=dict)
    attempts: int = 1
    replan_count: int = 0


# ─────────────────────────────────────────────────────────────────────────────
# 路由规则：哪些任务适合计划模式 vs ReAct
# ─────────────────────────────────────────────────────────────────────────────

# 适合 PLAN 模式的关键词（任务步骤可预测）
_PLAN_SUITABLE_KEYWORDS = [
    "砍", "挖", "采", "收集", "获取", "mine", "chop", "collect", "gather",
    "合成", "制作", "craft", "make",
    "炼", "冶炼", "smelt",
    "建", "放置", "build", "place",
    "去", "移动", "前往", "move", "go to",
    "找", "寻找", "find",
]

# 强制走 REACT 的关键词（高不确定性）
_REACT_REQUIRED_KEYWORDS = [
    "战斗", "攻击", "击杀", "fight", "kill", "attack",
    "逃跑", "escape",
    "探索未知", "explore",
    "如果", "遇到", "应对",  # 条件分支类
]

# 简单单步任务（可用单个 plan step 完成）
_SIMPLE_SINGLE_STEP = [
    "查看背包", "check inventory", "get_inventory",
    "停止", "stop",
    "报告位置", "where am i",
]


def route_execution_mode(
    task: str,
    matched_skill: Optional[dict],
    game_state: dict,
) -> ExecutionMode:
    """
    根据任务描述、匹配技能和游戏状态决定执行模式。

    优先级：
      1. 有可执行模板技能 → TEMPLATE
      2. 包含高不确定性关键词 → REACT
      3. 其余 → PLAN
    """
    task_lower = task.lower().strip()

    # 1. 模板技能
    if matched_skill and can_execute_as_template(matched_skill):
        logger.info(f"[Router] TEMPLATE: {matched_skill.get('name')}")
        return ExecutionMode.TEMPLATE

    # 2. 强制 ReAct
    if any(kw in task_lower for kw in _REACT_REQUIRED_KEYWORDS):
        logger.info(f"[Router] REACT: 包含高不确定性关键词")
        return ExecutionMode.REACT

    # 3. 其余走 PLAN
    logger.info(f"[Router] PLAN: 使用批量计划执行")
    return ExecutionMode.PLAN


# ─────────────────────────────────────────────────────────────────────────────
class PlanExecutor:
    """
    分层执行引擎。

    对外接口：
        result = await executor.execute(task, game_state, context, env_url)
    """

    # 计划执行时，最多重新规划几次
    MAX_REPLAN = 2
    # 批量执行时，每执行 N 步回 Python 检查状态（0 = 只在末尾检查）
    OBSERVE_INTERVAL = 4
    # 单次 LLM 生成计划的最大 step 数
    MAX_PLAN_STEPS = 20

    def __init__(
        self,
        llm: LLMRouter,
        env_url: str = "http://localhost:3000",
        session: Optional[aiohttp.ClientSession] = None,
    ):
        self.llm = llm
        self.env_url = env_url
        self._session = session

    # ── 主入口 ────────────────────────────────────────────────────────────────

    async def execute(
        self,
        task: str,
        game_state: dict,
        context: str = "",
        matched_skill: Optional[dict] = None,
        react_step_fn: Optional[Callable] = None,  # 外部传入的 ReAct 单步函数
    ) -> ExecutionResult:
        """
        执行任务，自动选择最优模式。

        Args:
            task:           任务描述
            game_state:     当前游戏状态
            context:        RAG + 技能库检索到的上下文
            matched_skill:  预先匹配的技能（可为 None）
            react_step_fn:  ReAct 模式下的单步执行函数（来自 VoyagerAgent）
        """
        mode = route_execution_mode(task, matched_skill, game_state)

        if mode == ExecutionMode.TEMPLATE:
            return await self._execute_template(task, matched_skill, game_state)

        if mode == ExecutionMode.PLAN:
            result = await self._execute_plan(task, game_state, context)
            # 计划失败则降级 ReAct
            if not result.success and react_step_fn:
                logger.info("[PlanExecutor] 计划失败，降级到 ReAct")
                result = await react_step_fn(task, game_state, context,
                                             critique=f"Plan failed: {result.message}")
                result.mode_used = ExecutionMode.REACT
            return result

        # REACT
        if react_step_fn:
            result = await react_step_fn(task, game_state, context)
            result.mode_used = ExecutionMode.REACT
            return result

        # 兜底（没有传入 react_step_fn）
        return ExecutionResult(
            success=False, mode_used=mode,
            message="No react_step_fn provided for REACT mode"
        )

    # ── TEMPLATE 模式 ─────────────────────────────────────────────────────────

    async def _execute_template(
        self,
        task: str,
        skill: dict,
        game_state: dict,
    ) -> ExecutionResult:
        """通过 skill_executor 执行参数化技能模板（0 额外 LLM）"""
        logger.info(f"[PlanExecutor:TEMPLATE] {skill.get('name')}")

        async def _do_action(action_type: str, params: dict) -> str:
            return await self._http_step(action_type, params)

        try:
            success, trajectory, message = await execute_skill_template(
                skill=skill,
                task=task,
                execute_action_fn=_do_action,
            )
            final_state = await self._http_observe()
            return ExecutionResult(
                success=success,
                mode_used=ExecutionMode.TEMPLATE,
                trajectory=trajectory,
                message=message,
                final_state=final_state,
            )
        except Exception as e:
            logger.error(f"[PlanExecutor:TEMPLATE] 执行异常: {e}")
            return ExecutionResult(
                success=False, mode_used=ExecutionMode.TEMPLATE,
                message=str(e)
            )

    # ── PLAN 模式 ─────────────────────────────────────────────────────────────

    async def _execute_plan(
        self,
        task: str,
        game_state: dict,
        context: str,
        critique: str = "",
        replan_depth: int = 0,
    ) -> ExecutionResult:
        """
        一次生成完整计划 → Node.js 批量执行 → 检查结果 → 按需 replan。
        """
        if replan_depth > self.MAX_REPLAN:
            return ExecutionResult(
                success=False, mode_used=ExecutionMode.PLAN,
                message=f"超过最大重规划次数 {self.MAX_REPLAN}",
                replan_count=replan_depth,
            )

        t0 = time.monotonic()

        # ── 1. 生成计划 ───────────────────────────────────────────────────────
        plan = await self._generate_plan(task, game_state, context, critique)
        if not plan or not plan.get("steps"):
            return ExecutionResult(
                success=False, mode_used=ExecutionMode.PLAN,
                message="LLM 未能生成有效计划",
                replan_count=replan_depth,
            )

        steps = plan["steps"][:self.MAX_PLAN_STEPS]
        logger.info(
            f"[PlanExecutor:PLAN] 生成 {len(steps)} 步计划 "
            f"(replan_depth={replan_depth}, llm={time.monotonic()-t0:.1f}s)"
        )
        for i, s in enumerate(steps):
            logger.debug(f"  [{i+1}] {s.get('action')} {s.get('params', {})}")

        # ── 2. 批量执行 ───────────────────────────────────────────────────────
        batch_result = await self._batch_execute(steps)
        trajectory = self._batch_to_trajectory(steps, batch_result.get("results", []))

        final_state = batch_result.get("final_state", {})
        success_count = batch_result.get("success_count", 0)
        fail_count = batch_result.get("fail_count", 0)

        # ── 3. 快速成功判断 ───────────────────────────────────────────────────
        if fail_count == 0 or plan.get("accept_partial", False):
            logger.info(f"[PlanExecutor:PLAN] 完成 {success_count} 步，耗时 {time.monotonic()-t0:.1f}s")
            return ExecutionResult(
                success=True,
                mode_used=ExecutionMode.PLAN,
                trajectory=trajectory,
                message=f"计划执行成功（{success_count}/{len(steps)} 步）",
                final_state=final_state,
                replan_count=replan_depth,
            )

        # ── 4. 部分失败 → 分析原因 → 重规划 ─────────────────────────────────
        failed_steps = [
            r for r in batch_result.get("results", [])
            if not r.get("success") and not r.get("skipped")
        ]
        fail_reason = failed_steps[0].get("observation", "unknown") if failed_steps else "unknown"
        new_critique = (
            f"Step {failed_steps[0].get('step_index', '?')} failed: "
            f"{failed_steps[0].get('action_type', '?')} → {fail_reason[:120]}"
        ) if failed_steps else f"Plan failed after {success_count} steps"

        logger.warning(f"[PlanExecutor:PLAN] 部分失败，重规划... critique={new_critique[:80]}")

        return await self._execute_plan(
            task=task,
            game_state=final_state or game_state,
            context=context,
            critique=new_critique,
            replan_depth=replan_depth + 1,
        )

    async def _generate_plan(
        self,
        task: str,
        game_state: dict,
        context: str,
        critique: str = "",
    ) -> Optional[dict]:
        """调用 LLM 生成完整 JSON 计划"""
        # 精简 game_state，只保留 LLM 需要的字段
        trimmed_gs = {
            k: v for k, v in game_state.items()
            if k in {"position", "inventory", "equipment", "health", "hunger",
                     "nearby_resources", "nearby_entities", "biome", "time", "dimension"}
        }

        critique_section = (
            f"\n⚠️ PREVIOUS PLAN FAILED: {critique}\nAvoid the same mistake."
            if critique else ""
        )

        system_prompt = PLAN_GENERATION_SYSTEM_PROMPT.format(max_steps=self.MAX_PLAN_STEPS)
        user_prompt = PLAN_GENERATION_HUMAN_TEMPLATE.format(
            task=task,
            context=context or "N/A",
            game_state=json.dumps(trimmed_gs, ensure_ascii=False),
            critique_section=critique_section,
            max_steps=self.MAX_PLAN_STEPS,
        )

        raw = await self.llm.think_fast(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.3,
        )
        if not raw:
            return None

        return self._parse_plan_json(raw)

    # ── HTTP 工具 ─────────────────────────────────────────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=120)
            )
        return self._session

    async def _batch_execute(self, steps: list[dict]) -> dict:
        """POST /execute_plan 到 mineflayer，返回原始结果"""
        # 将计划 step 格式转为 mineflayer 期望的格式
        mf_steps = []
        for s in steps:
            mf_steps.append({
                "action_type":    s.get("action", s.get("action_type", "")),
                "action_params":  s.get("params", s.get("action_params", {})),
                "display_message": s.get("description", ""),
                "condition":      s.get("condition"),
                "stop_on_fail":   s.get("stop_on_fail", True),
            })

        session = await self._get_session()
        try:
            async with session.post(
                f"{self.env_url}/execute_plan",
                json={
                    "steps": mf_steps,
                    "stop_on_failure": True,
                    "observe_interval": self.OBSERVE_INTERVAL,
                },
                timeout=aiohttp.ClientTimeout(total=300),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"[PlanExecutor] /execute_plan HTTP {resp.status}: {text[:200]}")
                    return {"results": [], "success_count": 0, "fail_count": len(mf_steps)}
                return await resp.json()
        except asyncio.TimeoutError:
            logger.error("[PlanExecutor] /execute_plan 超时（300s）")
            return {"results": [], "success_count": 0, "fail_count": len(mf_steps)}
        except Exception as e:
            logger.error(f"[PlanExecutor] /execute_plan 请求失败: {e}")
            return {"results": [], "success_count": 0, "fail_count": len(mf_steps)}

    async def _http_step(self, action_type: str, params: dict) -> str:
        """单步执行（供 TEMPLATE 模式使用）"""
        session = await self._get_session()
        try:
            async with session.post(
                f"{self.env_url}/step",
                json={"action_type": action_type, "action_params": params},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                data = await resp.json()
                return data.get("observation", "")
        except Exception as e:
            return f"[ERROR] {e}"

    async def _http_observe(self) -> dict:
        """获取当前游戏状态"""
        session = await self._get_session()
        try:
            async with session.post(
                f"{self.env_url}/observe",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                return await resp.json()
        except Exception:
            return {}

    # ── 工具方法 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _batch_to_trajectory(plan_steps: list[dict], results: list[dict]) -> list[dict]:
        """将批量执行结果转为轨迹格式"""
        trajectory = []
        for i, (plan_step, result) in enumerate(zip(plan_steps, results)):
            trajectory.append({
                "step": i + 1,
                "thought": plan_step.get("description", ""),
                "action": result.get("action_type", plan_step.get("action", "")),
                "action_params": result.get("action_params", plan_step.get("params", {})),
                "observation": result.get("observation", ""),
                "success": result.get("success", False),
            })
        return trajectory

    @staticmethod
    def _parse_plan_json(raw: str) -> Optional[dict]:
        """解析 LLM 输出的计划 JSON"""
        text = raw.strip()

        # 剥离 markdown 代码块
        if "```" in text:
            lines = text.split("\n")
            text = "\n".join(l for l in lines if not l.strip().startswith("```"))

        # 修复 JSON 常见错误
        text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
        text = re.sub(r",\s*}", "}", text)
        text = re.sub(r",\s*]", "]", text)

        # 尝试提取 JSON 对象
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            text = match.group(0)

        try:
            data = json.loads(text)
            # 支持直接输出 steps 数组（向后兼容）
            if isinstance(data, list):
                return {"steps": data}
            if isinstance(data, dict) and "steps" in data:
                return data
            return None
        except json.JSONDecodeError as e:
            logger.warning(f"[PlanExecutor] JSON 解析失败: {e} | raw[:100]={raw[:100]}")
            return None
