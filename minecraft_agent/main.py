"""
main.py — Minecraft Agent 主入口（mineflayer 版）

架构变更：
  原来：Python WebSocket Server ← Java Mod Client
  现在：Python Agent → HTTP → Node.js mineflayer → Minecraft

不再需要 Fabric Mod！Bot 作为独立玩家加入游戏。

启动方式：
  python main.py

配置（环境变量）：
  MC_HOST       Minecraft 服务器地址（默认 localhost）
  MC_PORT       Minecraft 端口（默认 25565）
  MC_USERNAME   Bot 用户名（默认 Agent）
  MINEFLAYER_PORT  mineflayer HTTP server 端口（默认 3000）
  LLM_API_KEY   OpenAI/Claude API Key
  LLM_BASE_URL  API Base URL（可选）
  LLM_MODEL     模型名称（默认 gpt-4o-mini）
"""

import asyncio
import os
import json
from loguru import logger

# 导入重构后的 agent 模块
from agent.env import MineflayerEnv, get_env
from agent.react_agent import VoyagerAgent
from agent.llm_router import LLMRouter
from agent.memory import MemoryManager
from agent.skill_library import SkillLibrary
from agent.personality import PersonalitySystem

# 配置
MC_HOST = os.getenv("MC_HOST", "localhost")
MC_PORT = int(os.getenv("MC_PORT", "25565"))
MC_USERNAME = os.getenv("MC_USERNAME", "Agent")
MINEFLAYER_PORT = int(os.getenv("MINEFLAYER_PORT", "3000"))
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")


async def main():
    logger.info("=" * 60)
    logger.info("  Minecraft Agent (mineflayer 版) 启动")
    logger.info("=" * 60)

    # ── 初始化各模块 ────────────────────────────────────────────────────────
    llm = LLMRouter(
        fast_model=LLM_MODEL,
        api_key=os.getenv("LLM_API_KEY"),
        base_url=os.getenv("LLM_BASE_URL"),
    )
    memory = MemoryManager()
    skill_lib = SkillLibrary(llm=llm, persist_dir="./skill_db")
    personality = PersonalitySystem()

    # ── 初始化 mineflayer 环境 ───────────────────────────────────────────────
    env = MineflayerEnv(
        mc_host=MC_HOST,
        mc_port=MC_PORT,
        username=MC_USERNAME,
        server_port=MINEFLAYER_PORT,
        auto_start_server=True,
    )

    # 将 env 注册为全局单例（让 react_agent 中的 get_env 能访问）
    import agent.env as env_module
    env_module._env_instance = env

    # ── 启动 Bot ─────────────────────────────────────────────────────────────
    logger.info(f"连接 Minecraft {MC_HOST}:{MC_PORT}...")
    initial_state = await env.start(reset="soft")
    logger.info(f"✅ Bot 已加入游戏: {initial_state.get('position')}")

    # ── 初始化 Agent ─────────────────────────────────────────────────────────
    agent = VoyagerAgent(
        llm=llm,
        memory=memory,
        skill_lib=skill_lib,
        personality=personality,
    )

    # ── 主循环：监听 bot 接收到的聊天消息 ────────────────────────────────────
    logger.info("Agent 就绪，等待玩家发送消息...")
    logger.info("在游戏中对 Bot 说话（或 /msg Agent 你好），Agent 会响应")

    game_state = initial_state
    last_chat_count = 0

    while True:
        try:
            # 轮询游戏状态（含聊天记录）
            current_state = await env.observe()
            if not current_state:
                await asyncio.sleep(1)
                continue

            game_state.update(current_state)
            chat_log = current_state.get("chat_log", [])

            # 处理新消息
            new_messages = chat_log[last_chat_count:]
            last_chat_count = len(chat_log)

            for msg in new_messages:
                username = msg.get("username", "")
                text = msg.get("message", "").strip()

                # 忽略 bot 自己的消息
                if username == MC_USERNAME or username == "Agent":
                    continue

                if not text:
                    continue

                logger.info(f"[Chat] <{username}> {text}")
                game_state["player_name"] = username

                # 运行 Agent
                result = await agent.run(game_state, text)

                # 发送回复
                reply = result.get("display_message", "")
                if reply:
                    # 截断过长消息（Minecraft 聊天限制 256 字符）
                    for chunk in _split_message(reply, 240):
                        await env.send_action("chat", {"message": chunk})
                        await asyncio.sleep(0.3)

            await asyncio.sleep(0.5)  # 轮询间隔 0.5s

        except KeyboardInterrupt:
            logger.info("用户中断，退出...")
            break
        except Exception as e:
            logger.error(f"主循环异常: {e}", exc_info=True)
            await asyncio.sleep(2)

    await env.stop()
    logger.info("Agent 已停止")


def _split_message(text: str, max_len: int) -> list[str]:
    """将长消息分成多个 chunk"""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        # 找最近的句号/逗号断点
        cut = max_len
        for sep in ["。", "，", "！", "？", ".", ",", " "]:
            idx = text.rfind(sep, 0, max_len)
            if idx > max_len // 2:
                cut = idx + 1
                break
        chunks.append(text[:cut])
        text = text[cut:]
    return chunks


if __name__ == "__main__":
    asyncio.run(main())
