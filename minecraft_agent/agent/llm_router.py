"""
LLM 分层模型路由（优化版：小模型 + 大模型分离）

- classify()     → 小模型（硅基流动 Qwen2.5-7B）：意图/复杂度/可行性、RAG 分类、Critic、技能抽象
- think/think_fast() → 大模型（DeepSeek 官方 deepseek-chat）：代码生成、任务分解、闲聊
"""

import asyncio
import os
import time
from openai import AsyncOpenAI
from loguru import logger

LLM_CALL_TIMEOUT = float(os.getenv("DEEPSEEK_LLM_TIMEOUT", "90"))


class LLMRouter:
    """
    双模型路由：小模型做分类与轻量判断，大模型做代码生成与推理。
    若未配置小模型 KEY，classify 回退到大模型。
    """

    def __init__(self) -> None:
        # 小模型（硅基流动）：分类、Critic、RAG 分类、技能抽象
        fast_key = os.getenv("SILICONFLOW_API_KEY", "") or os.getenv("DEEPSEEK_API_KEY", "")
        fast_base = os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")
        self.fast_model = os.getenv("FAST_MODEL", "Qwen/Qwen2.5-7B-Instruct")
        if fast_key:
            self.fast_client = AsyncOpenAI(api_key=fast_key, base_url=fast_base)
        else:
            self.fast_client = None
            logger.warning("SILICONFLOW_API_KEY 未设置，classify 将回退到大模型")

        # 大模型（DeepSeek 官方）：代码生成、任务分解、闲聊
        code_key = os.getenv("DEEPSEEK_API_KEY", "")
        code_base = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        if not code_key:
            logger.warning("DEEPSEEK_API_KEY 未设置，大模型调用将失败")
        self.code_client = AsyncOpenAI(api_key=code_key or "dummy", base_url=code_base)
        self.code_model = os.getenv("DEEPSEEK_V3_MODEL", "deepseek-chat")

    def _client_for_classify(self):
        """分类用：有小模型则用小模型，否则用大模型."""
        if self.fast_client is not None:
            return self.fast_client, self.fast_model
        return self.code_client, self.code_model

    async def think(self, system_prompt: str, user_prompt: str, temperature: float = 0.7) -> str:
        """代码生成 / 推理 → 大模型"""
        return await self.think_fast(system_prompt, user_prompt, temperature)

    async def think_fast(self, system_prompt: str, user_prompt: str, temperature: float = 0.7) -> str:
        """任务分解、ReAct、闲聊、自主决策 → 大模型"""
        return await self._call(
            self.code_client, self.code_model,
            system_prompt, user_prompt, temperature,
        )

    async def classify(self, system_prompt: str, user_prompt: str, temperature: float = 0.3) -> str:
        """意图、复杂度、可行性、Critic、RAG 分类、技能抽象 → 小模型（或回退大模型）"""
        client, model = self._client_for_classify()
        return await self._call(client, model, system_prompt, user_prompt, temperature)

    async def _call(
        self,
        client: AsyncOpenAI,
        model: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
    ) -> str:
        """底层 API 调用，带超时与错误处理。消息顺序固定为 [system, user]，便于 DeepSeek 上下文硬盘缓存对 system 前缀命中。"""
        t0 = time.monotonic()
        logger.info(f"[LLM] 请求中（超时 {int(LLM_CALL_TIMEOUT)}s）: {model}")
        try:
            response = await asyncio.wait_for(
                client.chat.completions.create(
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
            # DeepSeek 上下文硬盘缓存：命中部分成本更低，可选打日志
            usage = getattr(response, "usage", None)
            if usage is not None:
                hit = getattr(usage, "prompt_cache_hit_tokens", None)
                miss = getattr(usage, "prompt_cache_miss_tokens", None)
                if hit is not None or miss is not None:
                    logger.debug(f"[LLM] 缓存命中 tokens={hit or 0}, 未命中={miss or 0}")
            return content
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - t0
            logger.warning(f"[LLM] 调用超时（{elapsed:.1f}s > {LLM_CALL_TIMEOUT}s），返回空结果，请检查网络或 API")
            return ""
        except Exception as e:
            logger.error(f"LLM 调用失败 (model={model}): {e}", exc_info=True)
            return ""

    async def stream_think(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.3,
        on_chunk=None,
    ) -> str:
        """流式代码生成，on_chunk(delta) 可实时反馈（如发 MC 聊天）。"""
        full_text = ""
        t0 = time.monotonic()
        logger.info(f"[LLM] stream 请求中: {self.code_model}")
        try:
            stream = await asyncio.wait_for(
                self.code_client.chat.completions.create(
                    model=self.code_model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=temperature,
                    stream=True,
                ),
                timeout=LLM_CALL_TIMEOUT,
            )
            async for chunk in stream:
                delta = (chunk.choices[0].delta.content or "") if chunk.choices else ""
                full_text += delta
                if on_chunk and delta:
                    await on_chunk(delta)
            elapsed = time.monotonic() - t0
            logger.debug(f"[LLM] stream 完成 [{elapsed:.1f}s]")
            return full_text
        except asyncio.TimeoutError:
            logger.warning("[LLM] stream 超时，返回已接收内容")
            return full_text
        except Exception as e:
            logger.error(f"LLM stream 失败: {e}", exc_info=True)
            return full_text
