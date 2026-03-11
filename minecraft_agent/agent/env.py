"""
MineflayerEnv v3 — 新增 execute_code() 方法

新增接口：
  execute_code(code, timeout_ms) → {success, output, error, game_state}
    对应 /execute_code 端点，执行 LLM 生成的 JS async function

保留接口（兼容）：
  send_action(action_type, params) → /step（JSON action，保留用于简单操作）
  observe() → /observe
  start() / stop()
"""

import asyncio
import subprocess
import os
import threading
from typing import Optional

import aiohttp
from loguru import logger


def _forward_node_output(pipe, prefix: str = "Node"):
    """在后台线程中读取 Node 子进程 stdout，将踢出/错误等高亮写入终端和日志。"""
    try:
        for line in iter(pipe.readline, ""):
            line = (line or "").rstrip()
            if not line:
                continue
            if "[MC-KICK]" in line or "[MC-ERROR]" in line or "[MC-END]" in line:
                logger.warning(f"[{prefix}] {line}")
            else:
                logger.debug(f"[{prefix}] {line}")
    except Exception as e:
        logger.debug(f"[{prefix}] stdout 读取结束: {e}")


class MineflayerEnv:
    def __init__(
        self,
        mc_host: str = "localhost",
        mc_port: int = 25565,
        username: str = "Agent",
        server_port: int = 3000,
        mineflayer_dir: Optional[str] = None,
        wait_ticks: int = 5,
        request_timeout: int = 60,
        auto_start_server: bool = True,
    ):
        self.mc_host = mc_host
        self.mc_port = mc_port
        self.username = username
        self.server_port = server_port
        self.wait_ticks = wait_ticks
        self.request_timeout = request_timeout
        self.auto_start_server = auto_start_server
        self.server_url = f"http://localhost:{server_port}"

        if mineflayer_dir is None:
            base = os.path.dirname(os.path.abspath(__file__))
            mineflayer_dir = os.path.join(base, "..", "mineflayer")
        self.mineflayer_dir = os.path.abspath(mineflayer_dir)

        self._session: Optional[aiohttp.ClientSession] = None
        self._node_process = None
        self._started = False

    # ── 生命周期 ─────────────────────────────────────────────────────────────

    async def start(self, reset: str = "soft") -> dict:
        if self._session:
            await self._session.close()
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.request_timeout)
        )

        if self.auto_start_server and not await self._ping_server():
            await self._start_node_server()

        payload = dict(
            host=self.mc_host, port=self.mc_port,
            username=self.username, waitTicks=self.wait_ticks, reset=reset,
        )
        try:
            async with self._session.post(
                f"{self.server_url}/start", json=payload,
                timeout=aiohttp.ClientTimeout(total=45),
            ) as resp:
                data = await resp.json()
                if resp.status != 200:
                    raise RuntimeError(f"Bot 启动失败: {data}")
                self._started = True
                return data.get("game_state", {})
        except aiohttp.ServerDisconnectedError as e:
            raise RuntimeError(
                f"Node 服务在连接 MC ({self.mc_host}:{self.mc_port}) 时断开。"
                " 请确认：1) 游戏内已「对局域网开放」；2) Windows 防火墙放行 25565；"
                f" 3) .env 中 MC_HOST 为 WSL 可见的 Windows IP。原始错误: {e}"
            ) from e
        except aiohttp.ClientError as e:
            raise RuntimeError(
                f"请求 Node /start 失败 (MC={self.mc_host}:{self.mc_port}): {e}"
            ) from e

    async def stop(self):
        if self._started:
            try:
                async with self._session.post(f"{self.server_url}/stop") as _:
                    pass
            except Exception:
                pass
        if self._session:
            await self._session.close()
        if self._node_process:
            self._node_process.terminate()
        self._started = False

    # ── ★ 核心新接口：执行 JS 代码 ──────────────────────────────────────────

    async def execute_code(
        self,
        code: str,
        timeout_ms: int = 60000,
    ) -> dict:
        """
        执行 LLM 生成的 async JS function。

        Returns:
            {
              "success":    bool,
              "output":     str,    # bot.chat() + console.log() 输出
              "error":      str,    # 执行错误信息（无错则为 None）
              "game_state": dict,   # 执行后的游戏状态
            }
        """
        if not self._started:
            return {"success": False, "output": "", "error": "Bot not started", "game_state": {}}

        logger.log("FLOW", f"Env.execute_code(timeout_ms={timeout_ms}) → POST /execute_code")
        http_timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000 + 10)
        try:
            async with self._session.post(
                f"{self.server_url}/execute_code",
                json={"code": code, "timeout_ms": timeout_ms},
                timeout=http_timeout,
            ) as resp:
                data = await resp.json()
                logger.debug(
                    f"[Env] execute_code success={data.get('success')} "
                    f"output={data.get('output','')[:60]}"
                )
                return data
        except asyncio.TimeoutError:
            return {"success": False, "output": "",
                    "error": f"HTTP timeout after {timeout_ms+10000}ms", "game_state": {}}
        except Exception as e:
            return {"success": False, "output": "", "error": str(e), "game_state": {}}

    # ── 保留：JSON action（兼容旧代码 / 简单单步操作）──────────────────────

    async def send_action(
        self,
        action_type: str,
        action_params: dict,
        display_message: str = "",
        timeout: float = None,
    ) -> tuple[str, dict]:
        if not self._started:
            return "[Bot not started]", {}

        timeout = timeout or self.request_timeout
        try:
            async with self._session.post(
                f"{self.server_url}/step",
                json={"action_type": action_type,
                      "action_params": action_params,
                      "display_message": display_message},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                data = await resp.json()
                obs  = data.get("observation", "")
                if not data.get("success", True):
                    obs = f"[失败] {obs}"
                return obs, data.get("game_state", {})
        except Exception as e:
            return f"[错误] {e}", {}

    async def observe(self) -> dict:
        if not self._started:
            return {}
        try:
            async with self._session.post(
                f"{self.server_url}/observe",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                return await resp.json()
        except Exception:
            return {}

    # ── Node.js 进程管理 ─────────────────────────────────────────────────────

    async def _start_node_server(self):
        index_js = os.path.join(self.mineflayer_dir, "index.js")
        if not os.path.exists(index_js):
            raise FileNotFoundError(f"找不到 {index_js}")

        logger.info(f"[Env] 启动 mineflayer server: node {index_js} {self.server_port}")
        self._node_process = subprocess.Popen(
            ["node", index_js, str(self.server_port)],
            cwd=self.mineflayer_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        t = threading.Thread(
            target=_forward_node_output,
            args=(self._node_process.stdout, "Node"),
            daemon=True,
        )
        t.start()

        for _ in range(20):
            await asyncio.sleep(0.5)
            if await self._ping_server():
                logger.info("[Env] ✅ mineflayer server 启动成功")
                return

        raise RuntimeError("mineflayer server 10秒内未就绪")

    async def _ping_server(self) -> bool:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{self.server_url}/status",
                                  timeout=aiohttp.ClientTimeout(total=2)) as r:
                    return r.status == 200
        except Exception:
            return False

    def _is_node_process_alive(self) -> bool:
        """Node 子进程是否仍在运行（参考 Voyager 的 is_running）。"""
        if self._node_process is None:
            return False
        return self._node_process.poll() is None

    async def _restart_node_server(self) -> None:
        """Node 进程已退出时重启（参考 Voyager：Mineflayer process has exited, restarting）。"""
        if self._node_process is not None:
            try:
                self._node_process.terminate()
                self._node_process.wait(timeout=5)
            except Exception:
                pass
            self._node_process = None
        await asyncio.sleep(1)
        await self._start_node_server()

    async def ensure_bot_connected(self) -> bool:
        """
        参考 Voyager：被踢或 Node 崩了后自动恢复。
        - 若 Node 进程已退出则重启 Node，再 POST /start 让 bot 重新进服；
        - 若 Node 仍在但 bot 被踢（ping 通但需重连），则只 POST /start。
        返回 True 表示已重新连接，False 表示无法恢复。

        说明：挖掘时被僵尸/玩家打断不会直接导致「服务器踢人」，但可能让
        寻路/采集抛错；若未捕获会导致 Node 进程崩溃，表现像被踢。本逻辑
        在检测到 Node 挂掉或连接失败时会自动重启并重连。
        """
        # 先看 HTTP 服务是否可达
        if await self._ping_server():
            if self._started:
                return True
            # 服务在但 _started 被清空（或首次），执行一次 start 即可
            try:
                await self.start(reset="soft")
                logger.info("[Env] Bot 已重新连接（/start）")
                return True
            except Exception as e:
                logger.warning(f"[Env] ensure_bot_connected start 失败: {e}")
                return False
        # 服务不可达：若我们管理的 Node 进程已退出，则重启
        if self.auto_start_server and self._node_process is not None and not self._is_node_process_alive():
            logger.warning("[Env] Node 进程已退出，退出原因请查看上方/日志中的 [MC-KICK]/[MC-ERROR]/[MC-END]，正在重启…")
            try:
                await self._restart_node_server()
                await self.start(reset="soft")
                self._started = True
                logger.info("[Env] Node 已重启，Bot 已重新连接")
                return True
            except Exception as e:
                logger.error(f"[Env] 重启 Node 或重连失败: {e}")
                return False
        # 服务不可达且无法重启（如未托管进程）
        return False

    @staticmethod
    def _simulate_action(action_type, params, display):
        return f"[SIMULATE] {action_type}", {"position": {"x":0,"y":64,"z":0}, "inventory": []}


# ── 全局单例 ─────────────────────────────────────────────────────────────────

_env_instance: Optional[MineflayerEnv] = None

def get_env() -> MineflayerEnv:
    global _env_instance
    if _env_instance is None:
        _env_instance = MineflayerEnv()
    return _env_instance
