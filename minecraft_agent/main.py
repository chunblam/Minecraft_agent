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
AUTONOMOUS_IDLE_S=int(os.getenv("AUTONOMOUS_IDLE_SECONDS","60"))

def _is_system_chat_message(msg: str) -> bool:
    """过滤掉系统/控制台类消息：/ 开头为执行命令，不进入后续流程；传送等系统提示也过滤。"""
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
    game_state=await env.start(reset="soft")
    logger.info(f"✅ Bot 已加入，位置: {game_state.get('position')}")
    agent=VoyagerAgent(llm=llm,memory=memory,skill_lib=skill_lib,personality=personality,retriever=retriever)
    processed_msgs=set(); last_msg_time=time.time(); last_chat_count=0
    autonomous_enabled=False  # 默认关闭；聊天输入「autonomous explore」后启用
    # 用户执行 / 命令后，服务器返回的反馈会进聊天框且内容千变万化；用「同一用户、短时间内的下一条」视为反馈并跳过
    pending_feedback_skip=None  # (username, timestamp) 或 None
    FEEDBACK_SKIP_SEC=5
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
                if _is_system_chat_message(m):
                    logger.log("MC_PLAYER", f"【MC】<{u}> {m[:80]}{'...' if len(m)>80 else ''} (已忽略-系统消息)")
                    key = f"{u}:{m}:{int(entry.get('time',0)/5000)}"
                    processed_msgs.add(key)
                    if m.startswith("/"):
                        pending_feedback_skip = (u, time.time())
                    continue
                key=f"{u}:{m}:{int(entry.get('time',0)/5000)}"
                if key in processed_msgs: continue
                # 同一用户刚执行过 / 命令时，其下一条消息视为命令反馈，不交给 agent（反馈内容千变万化无法穷举）
                if pending_feedback_skip is not None:
                    u_prev, t_prev = pending_feedback_skip
                    if u == u_prev and (time.time() - t_prev) <= FEEDBACK_SKIP_SEC:
                        logger.log("MC_PLAYER", f"【MC】<{u}> {m[:80]}{'...' if len(m)>80 else ''} (已忽略-命令反馈)")
                        processed_msgs.add(key)
                        pending_feedback_skip = None
                        continue
                pending_feedback_skip = None
                processed_msgs.add(key)
                if len(processed_msgs)>500: processed_msgs.clear()
                logger.log("MC_PLAYER", f"【MC】<{u}> {m}")
                last_msg_time=time.time(); game_state["player_name"]=u
                if m.strip().lower()=="autonomous explore":
                    autonomous_enabled=True
                    logger.info("[Main] 已通过聊天开启自主探索")
                    try: await env.send_action("chat",{"message":"已开启自主探索，空闲一段时间后我会自己行动。"})
                    except Exception: pass
                    continue
                logger.log("FLOW", f"Main → agent.run(message={m[:40]!r})")
                try:
                    result=await agent.run(game_state,m)
                    reply=result.get("display_message","")
                    logger.log("FLOW", f"Main ← agent.run → display_message={str(reply)[:50]!r}")
                    if reply:
                        logger.log("MC_AGENT", f"【MC】<{MC_USERNAME}> {reply}")
                        await env.send_action("chat",{"message":reply})
                except Exception as e:
                    logger.error(f"Agent异常: {e}",exc_info=True)
                    err_lower = str(e).lower()
                    if "connect" in err_lower or "refused" in err_lower or "timeout" in err_lower:
                        try:
                            if await env.ensure_bot_connected():
                                logger.info("[Main] 连接已恢复（Agent 请求失败后重连）")
                        except Exception as re:
                            logger.debug(f"ensure_bot_connected: {re}")
                    try:
                        await env.send_action("chat",{"message":"出了点问题，稍等..."})
                    except Exception:
                        pass
            if autonomous_enabled and AUTONOMOUS_IDLE_S>0 and time.time()-last_msg_time>AUTONOMOUS_IDLE_S:
                last_msg_time=time.time()
                logger.log("FLOW", "Main → run_autonomous (idle timeout)")
                try: await agent.run_autonomous(game_state)
                except Exception as e: logger.debug(f"自主模式异常: {e}")
    except KeyboardInterrupt:
        logger.info("退出...")
    finally:
        try: await env.stop()
        except Exception: pass

if __name__=="__main__":
    asyncio.run(main())
