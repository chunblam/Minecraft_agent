"""
MineflayerEnv — Python 端环境接口

替换原来的 WebSocket ConnectionManager，改用 HTTP 与 mineflayer 服务器通信。

架构：
  Python Agent ←─── HTTP ───► Node.js mineflayer server ───► Minecraft

优势（vs 原 Java Fabric Mod WebSocket）：
  1. 同步请求-响应，无需手动匹配 request_id
  2. 内置超时处理，不会死锁
  3. mineflayer 自带 pathfinder，move_to 真正可靠
  4. 直接访问所有 Minecraft API，无需 Java 中间层

用法：
    env = MineflayerEnv(host="localhost", port=25565, server_port=3000)
    await env.start()
    obs, state = await env.step("move_to", {"direction": "north", "distance": 32})
    state = await env.observe()
    await env.stop()
"""

import asyncio
import json
import subprocess
import os
import time
from typing import Optional

import aiohttp
from loguru import logger


class MineflayerEnv:
    """
    与 mineflayer Node.js 服务器通信的 Python 端环境。

    完全替换 ConnectionManager（WebSocket）。
    所有接口保持与原 connection_manager.send_action() 兼容。
    """

    def __init__(
        self,
        mc_host: str = "localhost",
        mc_port: int = 25565,
        username: str = "Agent",
        server_port: int = 3000,             # mineflayer HTTP server port
        mineflayer_dir: Optional[str] = None, # mineflayer/index.js 所在目录
        wait_ticks: int = 5,
        request_timeout: int = 60,           # 单次 action 超时（秒）
        auto_start_server: bool = True,       # 自动启动 mineflayer 进程
    ):
        self.mc_host = mc_host
        self.mc_port = mc_port
        self.username = username
        self.server_port = server_port
        self.wait_ticks = wait_ticks
        self.request_timeout = request_timeout
        self.auto_start_server = auto_start_server

        # mineflayer server 地址
        self.server_url = f"http://localhost:{server_port}"

        # mineflayer 目录：agent/../mineflayer = minecraft_agent/mineflayer
        if mineflayer_dir is None:
            base = os.path.dirname(os.path.abspath(__file__))
            mineflayer_dir = os.path.join(base, "..", "mineflayer")
        self.mineflayer_dir = os.path.abspath(mineflayer_dir)

        self._session: Optional[aiohttp.ClientSession] = None
        self._node_process: Optional[subprocess.Popen] = None
        self._started = False

    # ── 生命周期 ─────────────────────────────────────────────────────────────

    async def start(self, reset: str = "soft") -> dict:
        """
        启动 mineflayer 服务器进程（如果需要），并让 bot 加入 Minecraft。

        Args:
            reset: "hard" 清空背包并重生；"soft" 直接加入

        Returns:
            初始游戏状态 dict
        """
        # 启动 Node.js 进程
        if self.auto_start_server:
            await self._ensure_node_server()

        # 创建 aiohttp session
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self._session = aiohttp.ClientSession(timeout=timeout)

        # 发送 /start 请求
        payload = {
            "host": self.mc_host,
            "port": self.mc_port,
            "username": self.username,
            "waitTicks": self.wait_ticks,
            "reset": reset,
        }
        logger.info(f"[Env] 连接 Minecraft {self.mc_host}:{self.mc_port} as '{self.username}'")

        for attempt in range(3):
            try:
                async with self._session.post(
                    f"{self.server_url}/start",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=40),
                ) as resp:
                    data = await resp.json()
                    if resp.status == 200:
                        self._started = True
                        state = data.get("observation", {})
                        logger.info(f"[Env] ✅ Bot 已加入游戏，位置: {state.get('position')}")
                        return state
                    else:
                        logger.warning(f"[Env] /start 失败: {data}")
            except Exception as e:
                logger.warning(f"[Env] /start 尝试 {attempt+1}/3 失败: {e}")
                await asyncio.sleep(2)

        raise RuntimeError("无法连接 mineflayer 服务器，请检查 Minecraft 是否开放 LAN")

    async def stop(self) -> None:
        """停止 bot 并关闭连接"""
        if self._session and not self._session.closed:
            try:
                async with self._session.post(f"{self.server_url}/stop") as resp:
                    await resp.json()
            except Exception:
                pass
            await self._session.close()
            self._session = None

        if self._node_process and self._node_process.poll() is None:
            self._node_process.terminate()
            self._node_process = None

        self._started = False
        logger.info("[Env] 已停止")

    # ── 核心接口：与原 ConnectionManager 完全兼容 ─────────────────────────────

    async def send_action(
        self,
        action_type: str,
        action_params: dict,
        display_message: str = "",
        timeout: float = None,
    ) -> tuple[str, dict]:
        """
        执行一个 action，返回 (observation: str, game_state_update: dict)。

        接口与原 ConnectionManager.send_action() 完全一致，
        可直接替换 react_agent.py 中的调用。
        """
        if not self._started:
            logger.warning("[Env] Bot 未启动，返回模拟结果")
            return self._simulate_action(action_type, action_params, display_message)

        timeout = timeout or self.request_timeout
        payload = {
            "action_type": action_type,
            "action_params": action_params,
            "display_message": display_message,
        }

        try:
            async with self._session.post(
                f"{self.server_url}/step",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    return f"[HTTP {resp.status}] {text}", {}

                data = await resp.json()
                observation = data.get("observation", "")
                game_state = data.get("game_state", {})
                success = data.get("success", True)

                if not success:
                    observation = f"[失败] {observation}"

                logger.debug(f"[Env] {action_type} → {observation[:60]}")
                return observation, game_state

        except asyncio.TimeoutError:
            msg = f"[超时] {action_type} 执行超过 {timeout}s"
            logger.warning(msg)
            return msg, {}
        except Exception as e:
            msg = f"[错误] {action_type}: {e}"
            logger.error(msg)
            return msg, {}

    async def observe(self) -> dict:
        """只读取当前游戏状态，不执行任何动作"""
        if not self._started:
            return {}
        try:
            async with self._session.post(f"{self.server_url}/observe") as resp:
                return await resp.json()
        except Exception as e:
            logger.warning(f"[Env] observe 失败: {e}")
            return {}

    async def is_connected(self) -> bool:
        """检查 bot 是否在线"""
        try:
            async with self._session.get(
                f"{self.server_url}/status",
                timeout=aiohttp.ClientTimeout(total=3),
            ) as resp:
                data = await resp.json()
                return data.get("connected", False)
        except Exception:
            return False

    # ── Node.js 进程管理 ──────────────────────────────────────────────────────

    async def _ensure_node_server(self) -> None:
        """确保 mineflayer Node.js 服务器正在运行"""

        # 检查是否已经运行
        if await self._ping_server():
            logger.info(f"[Env] mineflayer server 已在 port {self.server_port} 运行")
            return

        # 检查目录是否存在
        index_js = os.path.join(self.mineflayer_dir, "index.js")
        if not os.path.exists(index_js):
            raise FileNotFoundError(
                f"找不到 {index_js}，请确认 mineflayer/ 目录结构正确"
            )

        node_modules = os.path.join(self.mineflayer_dir, "node_modules")
        if not os.path.exists(node_modules):
            logger.info("[Env] 正在安装 npm 依赖...")
            result = subprocess.run(
                ["npm", "install"],
                cwd=self.mineflayer_dir,
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(f"npm install 失败:\n{result.stderr}")
            logger.info("[Env] npm install 完成")

        # 启动 Node.js 进程
        logger.info(f"[Env] 启动 mineflayer server (port={self.server_port})")
        self._node_process = subprocess.Popen(
            ["node", "index.js", str(self.server_port)],
            cwd=self.mineflayer_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        # 等待服务器就绪
        for _ in range(20):
            await asyncio.sleep(0.5)
            if self._node_process.poll() is not None:
                out, _ = self._node_process.communicate()
                raise RuntimeError(f"mineflayer server 启动失败:\n{out.decode()}")
            if await self._ping_server():
                logger.info("[Env] ✅ mineflayer server 启动成功")
                return

        raise RuntimeError(f"mineflayer server 在 10 秒内未就绪 (port={self.server_port})")

    async def _ping_server(self) -> bool:
        """测试 mineflayer HTTP server 是否可用"""
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    f"{self.server_url}/status",
                    timeout=aiohttp.ClientTimeout(total=2),
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False

    # ── 调试用模拟 ────────────────────────────────────────────────────────────

    @staticmethod
    def _simulate_action(action_type: str, params: dict, display: str) -> tuple[str, dict]:
        """Mod 未连接时的模拟响应（开发调试用）"""
        obs = f"[SIMULATE] {action_type} 执行成功"
        state = {
            "position": {"x": 0.0, "y": 64.0, "z": 0.0},
            "inventory": [],
            "health": 20.0,
            "hunger": 20.0,
        }
        return obs, state


# ── 全局单例（兼容现有代码中的 from .connection_manager import connection_manager）──
_env_instance: Optional[MineflayerEnv] = None


def get_env() -> MineflayerEnv:
    global _env_instance
    if _env_instance is None:
        _env_instance = MineflayerEnv()
    return _env_instance


# 兼容旧代码的别名
class _ConnectionManagerCompat:
    """让旧代码 `from .connection_manager import connection_manager` 不报错"""

    async def send_action(self, action_type, action_params, display_message="", timeout=60.0):
        return await get_env().send_action(action_type, action_params, display_message, timeout)

    def is_connected(self):
        return get_env()._started

    def resolve_observation(self, *args, **kwargs):
        pass  # mineflayer 版不需要手动解析

    def set_connection(self, *args, **kwargs):
        pass

    def clear_connection(self):
        pass


connection_manager = _ConnectionManagerCompat()
