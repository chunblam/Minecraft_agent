"""
LLM 分层调用路由模块（v3 —— 全 V3 快速推理）

模型分配策略（与 Voyager 论文一致，GPT-4 非推理模型即可完成任务）：
  - 全部使用 DeepSeek-V3（deepseek-chat）：
    · think() / think_fast() : 任务分解、ReAct 推理、行动计划生成（temperature=0.7）
    · classify()             : 意图识别、条件检查（temperature=0.3）
    · 技能抽象、闲聊回复等

性能：V3 每次调用约 2-10 秒，无 R1 长推理等待。
"""

import asyncio
import os
import time
from openai import AsyncOpenAI
from loguru import logger

# LLM 单次调用超时（秒），避免 API 无响应时长时间“卡住”
LLM_CALL_TIMEOUT = float(os.getenv("DEEPSEEK_LLM_TIMEOUT", "90"))


class LLMRouter:
    """
    LLM 调用路由器（v3 —— 全 V3）。

    三个入口均使用 V3：
      think()      → 与 think_fast 相同（兼容旧调用）
      think_fast() → 任务分解、ReAct 推理、行动规划
      classify()   → 意图识别、技能抽象、闲聊回复
    """

    def __init__(self) -> None:
        api_key = os.getenv("DEEPSEEK_API_KEY", "")
        base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

        if not api_key:
            logger.warning("DEEPSEEK_API_KEY 未设置，LLM 调用将失败")

        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.v3_model = os.getenv("DEEPSEEK_V3_MODEL", "deepseek-chat")

    async def think(self, system_prompt: str, user_prompt: str, temperature: float = 0.7) -> str:
        """
        与 think_fast 相同，统一使用 V3 快速推理（原 R1 已弃用）。
        """
        return await self.think_fast(system_prompt, user_prompt, temperature)

    async def think_fast(self, system_prompt: str, user_prompt: str, temperature: float = 0.7) -> str:
        """
        调用 DeepSeek-V3 进行快速推理（ReAct 步骤、技能抽象、行动规划等）。
        响应速度 2-5 秒，适合需要反复调用的场景。
        """
        return await self._call(self.v3_model, system_prompt, user_prompt, temperature)

    async def classify(self, system_prompt: str, user_prompt: str, temperature: float = 0.3) -> str:
        """
        调用 DeepSeek-V3 进行快速分类或简单判断。
        低温输出更确定，用于意图识别、条件检查等。
        """
        return await self._call(self.v3_model, system_prompt, user_prompt, temperature)

    async def _call(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
    ) -> str:
        """底层 API 调用，带超时、耗时统计和错误处理。超时或异常时返回空字符串，避免长时间卡住。"""
        t0 = time.monotonic()
        logger.info(f"[LLM] 请求中（超时 {int(LLM_CALL_TIMEOUT)}s）: {model}")
        try:
            response = await asyncio.wait_for(
                self.client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=temperature,
                    stream=False,
                ),
                timeout=LLM_CALL_TIMEOUT,
            )
            content = response.choices[0].message.content or ""
            elapsed = time.monotonic() - t0
            logger.debug(f"模型响应 [{elapsed:.1f}s]（前200字）: {content[:200]}")
            return content
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - t0
            logger.warning(f"[LLM] 调用超时（{elapsed:.1f}s > {LLM_CALL_TIMEOUT}s），返回空结果，请检查网络或 API")
            return ""
        except Exception as e:
            logger.error(f"LLM 调用失败 (model={model}): {e}", exc_info=True)
            return ""
