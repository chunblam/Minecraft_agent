"""
WebSocket 连接管理器

管理 Python Agent 与 Minecraft Fabric Mod 之间的双向 WebSocket 通信。

关键改进（v2）：
  send_action() 现在返回 (observation: str, game_state_update: dict) 二元组。
  Java 侧在每次执行行动后会附带一份背包/位置的最新快照，
  react_agent._execute_action() 通过 game_state.update(state_update) 实时刷新游戏状态，
  使下一步 LLM 推理能感知到行动的实际效果（挖到了什么、背包有什么变化）。

核心异步架构说明（防死锁设计）：
  main.py 的 handle_connection() 使用 asyncio.create_task() 启动 agent.run()，
  WebSocket 消息循环保持运行，能继续接收 observation 消息并 resolve Future，
  从而解除 send_action() 内部的 asyncio.wait_for() 阻塞。
"""

import asyncio
import json
import uuid
from typing import Optional, NamedTuple

from loguru import logger


class ActionResult(NamedTuple):
    """行动结果：观察字符串 + 游戏状态更新快照"""
    observation: str
    game_state_update: dict


class ConnectionManager:
    """
    单例：管理 Minecraft Mod 的 WebSocket 连接与请求-响应生命周期。

    所有操作均在同一 asyncio 事件循环中运行，无需加锁。
    """

    _instance: Optional["ConnectionManager"] = None

    def __init__(self) -> None:
        self._websocket = None
        # 待处理请求：{ request_id: asyncio.Future[ActionResult] }
        self._pending: dict[str, asyncio.Future] = {}

    @classmethod
    def get_instance(cls) -> "ConnectionManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── 连接管理 ───────────────────────────────────────────────────────────────

    def set_connection(self, websocket) -> None:
        self._websocket = websocket
        logger.info("Minecraft Mod 已连接，WebSocket 连接已注册")

    def clear_connection(self) -> None:
        self._websocket = None
        cancelled = 0
        for future in self._pending.values():
            if not future.done():
                future.cancel()
                cancelled += 1
        self._pending.clear()
        if cancelled:
            logger.warning(f"连接断开，已取消 {cancelled} 个待处理行动请求")
        logger.info("Minecraft Mod 连接已断开")

    def is_connected(self) -> bool:
        return self._websocket is not None

    # ── 行动发送与等待 ─────────────────────────────────────────────────────────

    async def send_action(
        self,
        action_type: str,
        action_params: dict,
        display_message: str = "",
        timeout: float = 30.0,
    ) -> tuple[str, dict]:
        """
        向 Mod 发送行动指令，等待执行完成后返回 (observation, game_state_update)。

        observation        : 行动结果的文字描述，用于 ReAct 下一步推理
        game_state_update  : Mod 执行后返回的背包/位置快照，合并到 game_state 可刷新状态

        若 Mod 未连接，返回模拟结果（便于纯 Python 端调试）。
        """
        if not self.is_connected():
            logger.warning(f"Mod 未连接，返回模拟结果 [{action_type}]")
            return self._simulate_action(action_type, action_params, display_message)

        request_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[request_id] = future

        message = {
            "type": "action",
            "request_id": request_id,
            "action_type": action_type,
            "action_params": action_params,
            "display_message": display_message,
        }

        try:
            await self._websocket.send(json.dumps(message, ensure_ascii=False))
            logger.debug(f"[→ Mod] action={action_type} req={request_id[:8]}...")

            result: ActionResult = await asyncio.wait_for(future, timeout=timeout)
            logger.debug(f"[← Mod] obs={result.observation[:60]}")
            return result.observation, result.game_state_update

        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            logger.error(f"行动 [{action_type}] 等待超时 ({timeout}s)")
            return f"行动 [{action_type}] 超时，未收到 Minecraft 回执", {}

        except asyncio.CancelledError:
            self._pending.pop(request_id, None)
            logger.warning(f"行动 [{action_type}] 已取消（连接断开？）")
            return f"行动 [{action_type}] 被取消", {}

        except Exception as e:
            self._pending.pop(request_id, None)
            logger.error(f"发送行动失败: {e}")
            return f"行动执行出错: {str(e)}", {}

    def resolve_observation(
        self,
        request_id: str,
        observation: str,
        game_state_update: dict | None = None,
    ) -> None:
        """
        收到 Mod 的 observation 消息时，将结果写入对应 Future。
        由 main.py 在接收到 type=observation 消息时调用。

        Args:
            request_id:         对应行动的请求 ID
            observation:        Mod 返回的观察字符串
            game_state_update:  Mod 执行后的游戏状态快照（可为 None）
        """
        future = self._pending.pop(request_id, None)
        if future is None:
            logger.warning(f"收到未知 request_id 的 observation: {request_id}")
            return
        if future.done():
            logger.warning(f"Future 已完成（超时后迟到的 observation）: {request_id[:8]}")
            return
        future.set_result(ActionResult(
            observation=observation,
            game_state_update=game_state_update or {},
        ))
        logger.debug(f"observation 已匹配: req={request_id[:8]}...")

    # ── 最终响应 ───────────────────────────────────────────────────────────────

    async def send_final_response(self, action_type: str, display_message: str) -> None:
        """
        ReAct 完成后发送最终回复给 Mod（无需等待 observation）。
        Mod 收到后调用 broadcastAgentMessage() 显示给玩家。
        """
        if not self.is_connected() or not display_message:
            return
        message = {
            "type": "final_response",
            "action_type": action_type,
            "display_message": display_message,
        }
        try:
            await self._websocket.send(json.dumps(message, ensure_ascii=False))
            logger.info(f"[→ Mod] final_response: {display_message[:80]}")
        except Exception as e:
            logger.error(f"发送最终响应失败: {e}")

    # ── 离线模拟（调试用）──────────────────────────────────────────────────────

    @staticmethod
    def _simulate_action(
        action_type: str, params: dict, display_message: str
    ) -> tuple[str, dict]:
        """Mod 未连接时的本地模拟，返回 (observation, empty_state_update)"""
        msg = display_message or params.get("message", "")
        simulations = {
            "chat":           f"[模拟] 向玩家发送消息: {msg}",
            "move_to":        f"[模拟] 移动到 ({params.get('x', 0)}, {params.get('y', 64)}, {params.get('z', 0)})",
            "mine_block":     f"[模拟] 挖掘方块 ({params.get('x', 0)}, {params.get('y', 64)}, {params.get('z', 0)})",
            "place_block":    f"[模拟] 放置 {params.get('block', 'stone')} 在 ({params.get('x', 0)}, {params.get('y', 64)}, {params.get('z', 0)})",
            "craft_item":     f"[模拟] 合成 {params.get('item', '?')} x{params.get('count', 1)}",
            "enchant_item":   f"[模拟] 对主手物品附魔 {params.get('enchantment', '?')} {params.get('level', 1)}",
            "interact_entity":f"[模拟] 与 {params.get('entity_type', '?')} 互动: {params.get('action', 'find')}",
            "get_inventory":  "[模拟] 获取背包信息",
            "follow_player":  f"[模拟] 跟随玩家: {params.get('player', '最近玩家')}",
            "stop":           "[模拟] 已停止行动",
            "look_at":        "[模拟] 转向目标",
            "find_resource":  f"[模拟] 搜索 {params.get('type', '?')} 半径{params.get('radius', 24)} → 找到 2 处：(10,64,5) (12,65,-3)",
            "scan_area":      "[模拟] 扫描区域完成",
            "finish":         "[模拟] 任务完成",
        }
        return simulations.get(action_type, f"[模拟] 执行 {action_type}"), {}


# ── 全局单例 ──────────────────────────────────────────────────────────────────
connection_manager = ConnectionManager.get_instance()
