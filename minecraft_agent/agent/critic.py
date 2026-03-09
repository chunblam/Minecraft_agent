"""
Critic Agent — 参照 Voyager critic.py 实现

核心作用：
  每次任务执行完毕后，由 Critic 读取当前游戏状态，
  严格判断任务是否「真正」完成，并给出具体的批评建议。

  失败时的 critique 会被注入到下一次重试的 prompt 中，
  让 Action Agent 知道上次哪里出错了，从而做出调整。

  这是 Voyager 最关键的机制 —— 没有 Critic，Agent 永远不知道自己失败了。
"""

import json
import re
from loguru import logger

from .llm_router import LLMRouter
from .prompts import CRITIC_SYSTEM_PROMPT, CRITIC_HUMAN_TEMPLATE


class CriticAgent:
    """
    任务成功验证器。

    check_task_success() 返回 (success: bool, critique: str)：
      success = True  → 任务完成，可以进入下一个任务
      success = False → 失败，critique 描述了具体缺失/错误，应注入重试 prompt
    """

    MAX_PARSE_RETRIES = 3

    def __init__(self, llm: LLMRouter):
        self.llm = llm

    async def check_task_success(
        self,
        task: str,
        game_state: dict,
        last_observation: str,
        max_retries: int = 3,
    ) -> tuple[bool, str]:
        """
        判断 task 是否真正完成。

        Args:
            task:             任务描述
            game_state:       当前完整游戏状态
            last_observation: 最后一步行动的 observation 字符串

        Returns:
            (success, critique)
        """
        human_message = self._build_human_message(task, game_state, last_observation)

        for attempt in range(max_retries):
            raw = await self.llm.classify(
                system_prompt=CRITIC_SYSTEM_PROMPT,
                user_prompt=human_message,
                temperature=0.0,
            )
            if not raw:
                logger.warning(f"[Critic] LLM 无输出，attempt={attempt+1}")
                continue

            result = self._parse_response(raw)
            if result is not None:
                success, critique = result
                logger.info(f"[Critic] task='{task[:40]}' success={success} | {critique[:60]}")
                return success, critique

            logger.warning(f"[Critic] 解析失败 attempt={attempt+1}, raw={raw[:100]}")

        # 解析全部失败 → 保守返回失败，附带默认 critique
        logger.error("[Critic] 多次解析失败，默认返回失败")
        return False, "无法验证任务状态，请重试"

    # ── 内部工具 ─────────────────────────────────────────────────────────────

    def _build_human_message(
        self, task: str, game_state: dict, last_observation: str
    ) -> str:
        """构建 Critic 的人类消息，提取关键游戏状态字段"""
        pos = game_state.get("position") or game_state.get("agent_position", {})
        if isinstance(pos, dict):
            position_str = f"x={pos.get('x', '?'):.1f}, y={pos.get('y', '?'):.1f}, z={pos.get('z', '?'):.1f}" \
                if all(isinstance(pos.get(k), (int, float)) for k in ['x', 'y', 'z']) \
                else str(pos)
        else:
            position_str = str(pos)

        # 格式化背包
        inventory = game_state.get("inventory", [])
        if isinstance(inventory, list):
            inv_str = ", ".join(
                f"{item.get('item', item.get('name', '?'))} x{item.get('count', 1)}"
                for item in inventory[:20]
            ) or "empty"
        else:
            inv_str = str(inventory) or "empty"

        # 附近实体
        entities = game_state.get("nearby_entities", [])
        if isinstance(entities, list):
            ent_str = ", ".join(
                f"{e.get('type', e.get('name', '?'))}"
                for e in entities[:10]
            ) or "none"
        else:
            ent_str = str(entities) or "none"

        health = game_state.get("health", "?")
        xp = game_state.get("xp_level", game_state.get("experience_level", "?"))

        return CRITIC_HUMAN_TEMPLATE.format(
            task=task,
            position=position_str,
            inventory=inv_str,
            nearby_entities=ent_str,
            health=health,
            xp_level=xp,
            last_observation=last_observation[:200] if last_observation else "N/A",
        )

    @staticmethod
    def _parse_response(raw: str) -> tuple[bool, str] | None:
        """解析 Critic 的 JSON 响应"""
        # 清理 markdown code block
        text = raw.strip()
        if "```" in text:
            lines = text.split("\n")
            text = "\n".join(
                line for line in lines
                if not line.strip().startswith("```")
            )

        # 修复常见 JSON 错误
        text = re.sub(r",\s*}", "}", text)
        text = re.sub(r",\s*]", "]", text)

        # 尝试找到 JSON 对象
        match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
        if match:
            text = match.group(0)

        try:
            data = json.loads(text)
            success = data.get("success")
            critique = data.get("critique", "")

            if not isinstance(success, bool):
                # 尝试从字符串解析
                if str(success).lower() in ("true", "yes", "1"):
                    success = True
                elif str(success).lower() in ("false", "no", "0"):
                    success = False
                else:
                    return None

            return success, str(critique)
        except (json.JSONDecodeError, AttributeError):
            return None
