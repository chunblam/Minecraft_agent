import asyncio, os, time
from datetime import datetime
from dotenv import load_dotenv
# 从 main.py 所在目录加载 .env，避免因工作目录不同读不到配置（WSL 下 MC 在 Windows 时需 MC_HOST=Windows IP）
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)
from loguru import logger

# 本次运行日志写入文件（精确到日期时间，从启动到中断的完整记录）
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_LOGS_DIR = os.path.join(_BASE_DIR, "logs")
os.makedirs(_LOGS_DIR, exist_ok=True)
_run_log = os.path.join(_LOGS_DIR, datetime.now().strftime("run_%Y-%m-%d_%H-%M-%S.log"))
logger.add(_run_log, level="DEBUG", format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}")
# 关键流程日志级别（终端显示为青色，便于区分）
logger.level("FLOW", no=25, color="<cyan>")
# 游戏内聊天：玩家与 Agent 区分颜色，便于 debug
logger.level("MC_PLAYER", no=24, color="<green>")
logger.level("MC_AGENT", no=24, color="<magenta>")
from agent.env import MineflayerEnv
import agent.env as env_module
from agent.react_agent import VoyagerAgent
from agent.llm_router import LLMRouter
from agent.memory import MemoryManager
from agent.skill_library import SkillLibrary
from agent.personality import PersonalitySystem

MC_HOST=os.getenv("MC_HOST","localhost"); MC_PORT=int(os.getenv("MC_PORT","25565"))
MC_USERNAME=os.getenv("MC_USERNAME","Agent"); MINEFLAYER_PORT=int(os.getenv("MINEFLAYER_PORT","3000"))
AUTONOMOUS_FIRST_DELAY_S=int(os.getenv("AUTONOMOUS_FIRST_DELAY_SECONDS","15"))
AUTONOMOUS_IDLE_S=int(os.getenv("AUTONOMOUS_IDLE_SECONDS","15"))

def _is_system_chat_message(msg: str) -> bool:
    """过滤掉系统/控制台类消息：/ 开头为执行命令、命令执行后的服务器反馈等，不当作任务。"""
    if not msg or len(msg) > 200:
        return True
    m = msg.strip()
    if m.startswith("/"):
        return True
    m_lower = m.lower()
    if "teleported" in m_lower and " to " in m_lower:
        return True
    if "已将" in m_lower and "传送" in m_lower:
        return True
    # 命令执行后服务器反馈（如 /gamemode 后的 "Set own game mode to Survival Mode]"）
    if "game mode" in m_lower or "own game mode" in m_lower:
        return True
    if "mode set to" in m_lower or "set to " in m_lower and ("mode" in m_lower or "gamemode" in m_lower):
        return True
    if m_lower.startswith("unknown command") or m_lower.startswith("[") or m.rstrip().endswith("]"):
        if any(x in m_lower for x in ("command", "mode", "set", "game", "server")):
            return True
    return False

async def main():
    logger.info("Minecraft Agent v3 (Voyager JS Code Generation + RAG) 启动")
    logger.info(f"连接 MC: {MC_HOST}:{MC_PORT} (Bot 名: {MC_USERNAME})")
    llm=LLMRouter(); memory=MemoryManager()
    skill_lib=SkillLibrary(llm=llm,persist_dir="./data/skill_db"); personality=PersonalitySystem()
    retriever=None
    try:
        from rag.retriever import RAGRetriever
        retriever=RAGRetriever(); logger.info("✅ RAG 就绪")
    except Exception as e:
        logger.warning(f"RAG 不可用: {e}")
    env=MineflayerEnv(mc_host=MC_HOST,mc_port=MC_PORT,username=MC_USERNAME,server_port=MINEFLAYER_PORT,auto_start_server=True)
    env_module._env_instance=env
    logger.info("正在连接 MC 服务器（最多等待约 60s）…")
    try:
        game_state=await env.start(reset="soft")
    except (RuntimeError, asyncio.TimeoutError, TimeoutError) as e:
        logger.error(f"连接 MC 失败: {e}")
        raise
    logger.info(f"✅ Bot 已加入，位置: {game_state.get('position')}")
    agent=VoyagerAgent(llm=llm,memory=memory,skill_lib=skill_lib,personality=personality,retriever=retriever)
    # 启动时自动将当前技能库导出到 data/skill_db_export，便于快速查验（可通过 EXPORT_SKILLS_ON_START=0 关闭）
    if os.getenv("EXPORT_SKILLS_ON_START", "1").lower() in ("1", "true", "yes"):
        try:
            from scripts.export_skills import export_skills_to_path
            _export_dir = os.path.join(_BASE_DIR, "data", "skill_db_export")
            _n = export_skills_to_path(skill_lib, _export_dir, quiet=True)
            logger.info(f"已导出 {_n} 条技能到 {_export_dir}")
        except Exception as _e:
            logger.debug(f"启动时导出技能跳过: {_e}")
    async def _on_code_gen_progress(msg: str):
        try: await env.send_action("chat", {"message": msg})
        except Exception: pass
    agent.on_code_gen_progress = _on_code_gen_progress
    processed_msgs=set(); last_msg_time=time.time(); last_chat_count=0
    autonomous_enabled=False  # 默认关闭；聊天输入「autonomous」启用，「autonomous stop」关闭
    autonomous_enabled_at=None  # 用户输入 autonomous 时设为 time.time()，用于首次 15s 延迟
    last_autonomous_finish_ref={"t": None}  # 上次自主任务完成时间，用于任务间隔 15s
    current_task_ref={"task": None}  # 当前运行的 agent 任务，用于 stop 中断
    # 用户执行 / 命令后，服务器返回的反馈会进聊天框；5s 内所有聊天内容均不当作任务描述
    last_slash_time=None  # 最近一次 / 命令时间；5s 内任意聊天都忽略
    FEEDBACK_SKIP_SEC=int(os.getenv("FEEDBACK_SKIP_SEC", "5"))
    logger.info(f"运行日志已写入: {_run_log}")
    try:
        while True:
            await asyncio.sleep(1)
            try:
                s=await env.observe()
                if s: game_state.update(s)
            except Exception as e:
                logger.warning(f"observe失败: {e}")
                logger.info("[Main] 若 Agent 已退出游戏，请查看上方/日志中的 [MC-KICK]/[MC-ERROR]/[MC-END] 判别原因，正在尝试重连…")
                # 参考 Voyager：连接/Node 挂掉时尝试重启 Node 并重连 bot
                try:
                    if await env.ensure_bot_connected():
                        logger.info("[Main] 已恢复连接，继续运行")
                except Exception as re:
                    logger.debug(f"ensure_bot_connected: {re}")
                await asyncio.sleep(3)
                continue
            chat_log=game_state.get("chat_log",[]); bot_name=MC_USERNAME.lower()
            new_msgs=chat_log[last_chat_count:]; last_chat_count=len(chat_log)
            for entry in new_msgs:
                u=entry.get("username",""); m=entry.get("message","").strip()
                if u.lower()==bot_name or not m: continue
                # 以 / 开头的消息一律视为 MC 命令，不当作任务，并开启命令反馈忽略窗口
                if m.startswith("/"):
                    key = f"{u}:{m}:{int(entry.get('time',0)/5000)}"
                    processed_msgs.add(key)
                    last_slash_time = time.time()
                    logger.log("MC_PLAYER", f"【MC】<{u}> {m[:80]}{'...' if len(m)>80 else ''} (已忽略-玩家命令)")
                    continue
                if _is_system_chat_message(m):
                    logger.log("MC_PLAYER", f"【MC】<{u}> {m[:80]}{'...' if len(m)>80 else ''} (已忽略-系统消息)")
                    key = f"{u}:{m}:{int(entry.get('time',0)/5000)}"
                    processed_msgs.add(key)
                    # 命令执行后的反馈也开启反馈窗口，避免紧随其后的系统多行当作任务
                    if "game mode" in m.lower() or ("set " in m.lower() and "mode" in m.lower()) or (m.rstrip().endswith("]") and ("mode" in m.lower() or "command" in m.lower())):
                        last_slash_time = time.time()
                    continue
                key=f"{u}:{m}:{int(entry.get('time',0)/5000)}"
                if key in processed_msgs: continue
                # / 命令后 5s 内所有聊天都不当作任务描述
                if last_slash_time is not None and (time.time() - last_slash_time) <= FEEDBACK_SKIP_SEC:
                    logger.log("MC_PLAYER", f"【MC】<{u}> {m[:80]}{'...' if len(m)>80 else ''} (已忽略-命令反馈)")
                    processed_msgs.add(key)
                    continue
                if last_slash_time is not None:
                    last_slash_time = None
                processed_msgs.add(key)
                if len(processed_msgs)>500: processed_msgs.clear()
                logger.log("MC_PLAYER", f"【MC】<{u}> {m}")
                last_msg_time=time.time(); game_state["player_name"]=u
                ml=m.strip().lower()
                if ml=="autonomous stop":
                    autonomous_enabled=False
                    autonomous_enabled_at=None
                    last_autonomous_finish_ref["t"]=None
                    logger.info("[Main] 已通过聊天关闭自主探索")
                    t=current_task_ref["task"]
                    if t and not t.done():
                        t.cancel()
                        logger.info("[Main] 已中断当前任务")
                    try: await env.send_action("chat",{"message":"已关闭自主探索，等待你的指令。"})
                    except Exception: pass
                    continue
                if ml=="autonomous":
                    autonomous_enabled=True
                    autonomous_enabled_at=time.time()
                    last_autonomous_finish_ref["t"]=None
                    logger.info("[Main] 已通过聊天开启自主探索")
                    try: await env.send_action("chat",{"message":"已开启自主探索，空闲一段时间后我会自己行动。"})
                    except Exception: pass
                    continue
                if ml=="stop":
                    t=current_task_ref["task"]
                    if t and not t.done():
                        t.cancel()
                        logger.info("[Main] 已按 stop 中断当前任务")
                        try: await env.send_action("chat",{"message":"已停止。"})
                        except Exception: pass
                    else:
                        logger.log("FLOW", "[Main] 当前无运行中任务")
                    continue
                # 普通任务消息：若无运行中任务则启动，否则拒绝
                t=current_task_ref["task"]
                if t and not t.done():
                    try: await env.send_action("chat",{"message":"当前有任务执行中，请稍候或输入 stop 中断。"})
                    except Exception: pass
                    continue
                def _done_cb(t):
                    current_task_ref["task"]=None
                    if t.cancelled():
                        return
                    try:
                        r=t.result()
                        reply=(r or {}).get("display_message","")
                        if reply:
                            logger.log("FLOW", f"Main ← agent.run → display_message={str(reply)[:50]!r}")
                            logger.log("MC_AGENT", f"【MC】<{MC_USERNAME}> {reply}")
                            asyncio.create_task(_send_chat(reply))
                    except asyncio.CancelledError:
                        pass
                    except Exception as e:
                        logger.error(f"Agent 任务异常: {e}", exc_info=True)
                        asyncio.create_task(_send_chat("出了点问题，稍等..."))
                async def _send_chat(msg):
                    try: await env.send_action("chat",{"message":msg})
                    except Exception: pass
                try:
                    s=await env.observe()
                    if s: game_state.update(s)
                except Exception as e:
                    logger.debug(f"用户任务前 observe 失败: {e}")
                logger.log("FLOW", f"Main → agent.run(message={m[:40]!r})")
                current_task_ref["task"]=asyncio.create_task(agent.run(game_state,m))
                current_task_ref["task"].add_done_callback(_done_cb)
            # 自主探索：首次 15s 后或上次任务完成后 15s 且无运行中任务时触发
            t=current_task_ref["task"]
            if autonomous_enabled and (not t or t.done()):
                now=time.time()
                if t and t.done():
                    last_autonomous_finish_ref["t"]=now
                    if current_task_ref["task"] is t:
                        current_task_ref["task"]=None
                if last_autonomous_finish_ref["t"] is None:
                    # 尚未完成过任何自主任务：用开启时间判断
                    start=autonomous_enabled_at
                    wait_sec=AUTONOMOUS_FIRST_DELAY_S
                else:
                    start=last_autonomous_finish_ref["t"]
                    wait_sec=AUTONOMOUS_IDLE_S
                if start is not None and wait_sec>0 and (now-start)>=wait_sec:
                    try:
                        s=await env.observe()
                        if s: game_state.update(s)
                    except Exception as e:
                        logger.debug(f"run_autonomous 前 observe 失败: {e}")
                    logger.log("FLOW", "Main → run_autonomous")
                    def _done_auto(completed_task):
                        if current_task_ref["task"] is completed_task:
                            current_task_ref["task"]=None
                        if completed_task.cancelled():
                            return
                        last_autonomous_finish_ref["t"]=time.time()
                        try:
                            r=completed_task.result()
                            reply=(r or {}).get("display_message","")
                            if reply:
                                logger.log("MC_AGENT", f"【MC】<{MC_USERNAME}> {reply}")
                                asyncio.create_task(_send_chat_auto(reply))
                        except (asyncio.CancelledError, Exception):
                            pass
                    async def _send_chat_auto(msg):
                        try: await env.send_action("chat",{"message":msg})
                        except Exception: pass
                    current_task_ref["task"]=asyncio.create_task(agent.run_autonomous(game_state))
                    current_task_ref["task"].add_done_callback(_done_auto)
    except KeyboardInterrupt:
        logger.info("退出...")
    finally:
        try: await env.stop()
        except Exception: pass

if __name__=="__main__":
    asyncio.run(main())
