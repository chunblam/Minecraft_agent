"""
Minecraft AI Agent - WebSocket 服务器入口

监听 ws://localhost:8765，等待 Minecraft Fabric Mod 连接。

消息协议（Mod ↔ Python）：
─────────────────────────────────────────────────────────────────────
  Mod → Python  {type: "player_chat", player_message: "...", game_state: {...}}
  Python → Mod  {type: "action", request_id: "uuid", action_type: "...", action_params: {...}, display_message: "..."}
  Mod → Python  {type: "observation", request_id: "uuid", success: true, observation: "..."}
  Python → Mod  {type: "final_response", action_type: "chat", display_message: "..."}
─────────────────────────────────────────────────────────────────────

关键架构：asyncio.create_task()
  收到 player_chat 时，不直接 await agent.run()，而是启动为独立协程任务。
  这样 WebSocket 消息循环可以继续接收 observation 消息，
  observation 再通过 connection_manager.resolve_observation() 解除
  _execute_action() 内部的 asyncio.wait_for() 阻塞，形成完整的往返链路。
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import websockets
from dotenv import load_dotenv
from loguru import logger
from websockets.server import WebSocketServerProtocol

from agent.connection_manager import connection_manager
from agent.react_agent import ReactAgent

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# 日志配置：终端 + 文件双输出
#
# 文件策略：
#   logs/agent_YYYY-MM-DD.log     - 按天轮转，保留最近 14 天
#   logs/agent_latest.log         - 始终指向本次运行的实时日志（追加模式）
#
# 日志级别：
#   终端  → INFO 及以上（清晰可读）
#   文件  → DEBUG 及以上（完整记录，包含 LLM 原始响应等调试信息）
# ─────────────────────────────────────────────────────────────────────────────

_LOG_DIR = Path(__file__).parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)

# 移除 loguru 默认的终端 handler（后面重新添加，格式更友好）
logger.remove()

# ① 终端输出：INFO+，带颜色，简洁格式
logger.add(
    sys.stderr,
    level="INFO",
    colorize=True,
    format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | "
           "<cyan>{name}</cyan>:<cyan>{line}</cyan> | <level>{message}</level>",
)

# ② 文件输出（按日期轮转）：DEBUG+，完整格式，UTF-8
logger.add(
    str(_LOG_DIR / "agent_{time:YYYY-MM-DD}.log"),
    level="DEBUG",
    rotation="00:00",        # 每天零点新建文件
    retention="14 days",     # 保留 14 天
    compression="zip",       # 旧文件自动压缩
    encoding="utf-8",
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {name}:{line} | {message}",
    enqueue=True,            # 异步写入，不阻塞主线程
)

# ③ latest.log：当前运行完整日志（每次启动覆盖，方便随时 tail -f 查看）
logger.add(
    str(_LOG_DIR / "agent_latest.log"),
    level="DEBUG",
    mode="w",                # 每次启动清空，保留本次完整记录
    encoding="utf-8",
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {name}:{line} | {message}",
    enqueue=True,
)

logger.info(f"日志文件目录: {_LOG_DIR.resolve()}")
logger.info(f"实时日志: {_LOG_DIR / 'agent_latest.log'}")

WS_HOST = os.getenv("WS_HOST", "localhost")
WS_PORT = int(os.getenv("WS_PORT", "8765"))

# 全局 Agent 实例（单例，保持记忆跨会话连续）
agent = ReactAgent()


# ─────────────────────────────────────────────────────────────────────────────
# 连接处理主循环
# ─────────────────────────────────────────────────────────────────────────────

async def handle_connection(websocket: WebSocketServerProtocol) -> None:
    """
    处理单个 WebSocket 连接的消息循环。

    架构要点：
      - player_chat  → asyncio.create_task() 异步处理（不阻塞消息循环）
      - observation  → 同步调用 resolve_observation()（立即解除 Future 阻塞）
    """
    client_addr = websocket.remote_address
    logger.info(f"Minecraft Mod 已连接: {client_addr}")

    # 向全局连接管理器注册当前连接
    connection_manager.set_connection(websocket)

    try:
        async for raw_message in websocket:
            try:
                data = json.loads(raw_message)
            except json.JSONDecodeError as e:
                logger.warning(f"JSON 解析失败: {e}")
                continue

            msg_type = data.get("type", "player_chat")

            if msg_type == "player_chat":
                # ── 玩家发言：启动为独立协程，让消息循环继续接收 observation ──
                game_state = data.get("game_state", {})
                player_message = data.get("player_message", "").strip()

                if not player_message:
                    logger.debug("收到空 player_message，跳过")
                    continue

                logger.info(f"[玩家] {game_state.get('player_name', '?')}: {player_message}")

                # create_task 关键点：允许 asyncio 在 agent.run() 等待期间
                # 继续处理下方的 observation 消息
                asyncio.create_task(
                    _process_player_message(game_state, player_message)
                )

            elif msg_type == "observation":
                # ── Mod 回传行动结果：立即解锁对应的 send_action Future ──
                request_id = data.get("request_id", "")
                observation = data.get("observation", "")
                success = data.get("success", True)
                # v2：附带游戏状态更新快照（背包、位置等），传给 connection_manager
                game_state_update = data.get("game_state_update", {})

                if not success:
                    observation = f"[行动失败] {observation}"

                logger.debug(f"[← observation] req={request_id[:8]}... obs={observation[:60]}")
                connection_manager.resolve_observation(request_id, observation, game_state_update)

            else:
                logger.warning(f"未知消息类型: {msg_type}")

    except websockets.exceptions.ConnectionClosedOK:
        logger.info(f"Mod 连接正常关闭: {client_addr}")
    except websockets.exceptions.ConnectionClosedError as e:
        logger.warning(f"Mod 连接异常关闭: {client_addr} | {e}")
    except Exception as e:
        logger.error(f"连接处理异常: {e}", exc_info=True)
    finally:
        # 清理连接状态，取消所有待处理的行动请求
        connection_manager.clear_connection()


# ─────────────────────────────────────────────────────────────────────────────
# 玩家消息路由（LLM 意图识别分流）
# ─────────────────────────────────────────────────────────────────────────────


async def _process_player_message(game_state: dict, player_message: str) -> None:
    """
    消息分流处理器：先用 LLM 识别意图，再路由到对应处理逻辑。

    路由规则：
      task_execution → 任务模式（ReAct 循环 / 层级执行，需 Java 端操控角色）
      chat / knowledge_qa → 闲聊模式（V3 直接回复，2-5秒）
    """
    try:
        intent = await agent.classify_intent(player_message)
        logger.info(f"[路由] 意图={intent} | 消息: {player_message[:50]}")

        if intent == "task_execution":
            result = await agent.run(game_state, player_message)
        else:
            result = await agent.chat(game_state, player_message)

        display_message = result.get("display_message", "")
        action_type = result.get("action_type", "chat")

        if display_message:
            await connection_manager.send_final_response(action_type, display_message)

    except Exception as e:
        logger.error(f"处理玩家消息时发生异常: {e}", exc_info=True)
        await connection_manager.send_final_response(
            "chat", "呜...晨曦出了点小问题，主人稍等一下再试试？(>_<)"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 服务器启动
# ─────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    """启动 WebSocket 服务器"""
    logger.info("=" * 50)
    logger.info("  Minecraft AI Agent 服务器启动")
    logger.info(f"  监听地址: ws://{WS_HOST}:{WS_PORT}")
    logger.info("=" * 50)

    async with websockets.serve(
        handle_connection,
        WS_HOST,
        WS_PORT,
        ping_interval=30,
        ping_timeout=10,
        max_size=2 ** 20,
    ):
        logger.success("服务器就绪，等待 Minecraft Mod 连接...")
        await asyncio.Future()  # 永久运行


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("服务器已手动停止")
