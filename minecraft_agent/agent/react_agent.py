"""
VoyagerAgent v3 — JS 代码生成执行版（Voyager 架构 + RAG + 记忆）

核心执行流程（_run_task 内部，等同 Voyager rollout）：
  for attempt in range(MAX_RETRIES):
      context  = 技能库JS代码 + RAG知识 + 记忆  (三路并发)
      raw_resp = LLM(system=CODE_GENERATION_PROMPT, user=状态+任务+上次错误)
      code     = 从 raw_resp 提取 ```javascript ... ``` 块
      result   = POST /execute_code → Node.js 执行，返回 output+error+game_state
      ok, crit = Critic(task, game_state, output)
      if ok: 存技能(后台) → return 成功
      else:  critique = crit → 下次带入重新生成

ReAct 对应关系：
  Reason = LLM 输出的 Explain + Plan 文字
  Act    = 生成 async JS function → /execute_code 连续执行
  Observe= output(bot.chat 日志) + error + 新 game_state
  比 JSON 单步 ReAct 少 60-80% LLM 调用，执行连贯无 Python 往返
"""

import asyncio, json, re, os
from loguru import logger
from .llm_router import LLMRouter
from .memory import MemoryManager
from .skill_library import SkillLibrary
from .critic import CriticAgent
from .personality import PersonalitySystem
from .planner import TaskPlanner
from .env import get_env
from .prompts import (
    CODE_GENERATION_SYSTEM_PROMPT, CODE_GENERATION_HUMAN_TEMPLATE,
    UNIFIED_CLASSIFY_PROMPT,
    AUTONOMOUS_SYSTEM_PROMPT, AUTONOMOUS_HUMAN_TEMPLATE,
)

MAX_RETRIES     = int(os.getenv("MAX_TASK_RETRIES", "4"))
CODE_TIMEOUT_MS = int(os.getenv("CODE_TIMEOUT_MS",  "300000"))  # 单次任务执行上限 5 分钟


class VoyagerAgent:
    def __init__(self, llm: LLMRouter, memory: MemoryManager,
                 skill_lib: SkillLibrary, personality: PersonalitySystem = None,
                 retriever=None):
        self.llm         = llm
        self.memory      = memory
        self.skill_lib   = skill_lib
        self.personality = personality or PersonalitySystem()
        self.retriever   = retriever
        self.critic      = CriticAgent(llm)
        self.planner     = TaskPlanner(llm)
        self.on_code_gen_progress = None  # 可选：流式代码生成时首 chunk 回调（如发 MC 聊天「正在生成代码…」）

    # ── 公共入口 ──────────────────────────────────────────────────────────────

    async def run(self, game_state: dict, player_message: str) -> dict:
        await self.memory.add_event("user", player_message,
                                    {"player": game_state.get("player_name", "Player")})
        cls = await self._classify(player_message, game_state)
        intent = cls.get("intent", "task_execution")
        if intent != "task_execution":
            logger.log("FLOW", f"Agent.run → intent={intent!r} path=chat")
            return await self._chat_reply(game_state, player_message)
        if cls.get("feasible") is False:
            reason = cls.get("reason", "该操作不可行")
            msg = f"无法执行：{reason}"
            logger.log("FLOW", f"Agent.run → feasible=False reason={reason!r}")
            await self.memory.add_event("agent", msg, {})
            return {"action_type": "chat", "display_message": msg, "extra_data": {}}
        complexity = cls.get("complexity", "simple")
        path = "hierarchical" if complexity == "complex" else "task"
        logger.log("FLOW", f"Agent.run → intent=task_execution complexity={complexity!r} path={path!r}")
        result = (await self._run_hierarchical(game_state, player_message)
                  if complexity == "complex"
                  else await self._run_task(player_message, game_state))
        await self.memory.add_event("agent", result.get("display_message", ""), {})
        return result

    async def run_autonomous(self, game_state: dict) -> dict:
        """空闲时根据游戏状态自主决策并执行"""
        recent  = self.memory.short_term.to_context_string()[-400:]
        trimmed = {k: v for k, v in game_state.items()
                   if k in {"position","health","food","inventory",
                             "nearby_entities","nearby_blocks","time","biome"}}
        raw = await self.llm.think_fast(
            system_prompt=AUTONOMOUS_SYSTEM_PROMPT,
            user_prompt=AUTONOMOUS_HUMAN_TEMPLATE.format(
                game_state=json.dumps(trimmed, ensure_ascii=False),
                recent_memory=recent),
            temperature=0.6)
        if not raw:
            return {"action_type": "chat", "display_message": "", "extra_data": {}}
        try:
            data = json.loads(self._strip_json(raw))
            task = data.get("task", "")
        except Exception:
            return {"action_type": "chat", "display_message": "", "extra_data": {}}
        if not task:
            return {"action_type": "chat", "display_message": "", "extra_data": {}}
        logger.info(f"[Autonomous] {task}")
        result = await self._run_task(task, game_state)
        return {"action_type": "chat", "display_message": f"[自主] {task}",
                "extra_data": {"mode": "autonomous", **result.get("extra_data", {})}}

    # ── ★ 核心：代码生成 + 执行循环 ──────────────────────────────────────────

    async def _run_task(self, task: str, game_state: dict,
                        critique: str = "", context: str = "") -> dict:
        # 技能库快速匹配：同质不同参时优先用已有技能代码并尝试参数替换执行
        fast_result = await self._try_skill_fast_path(task, game_state)
        if fast_result is not None:
            return fast_result

        logger.log("FLOW", f"_run_task → _build_context(task={task[:35]!r})")
        if not context:
            context = await self._build_context(task, game_state)

        env         = get_env()
        last_code   = "N/A"
        last_error  = "N/A"
        last_output = ""
        best_code   = ""

        for attempt in range(1, MAX_RETRIES + 1):
            logger.info(f"[CodeLoop] '{task[:50]}' attempt {attempt}/{MAX_RETRIES}")

            # 1. LLM 生成 async JS function（首次用流式以便 MC 聊天栏可提示「正在生成代码…」）
            prompt = self._build_code_prompt(
                task, game_state, context,
                last_code, last_error, last_output, critique, attempt=attempt)
            if attempt == 1 and getattr(self, "on_code_gen_progress", None):
                first_chunk = [True]
                async def _on_chunk(delta: str):
                    if first_chunk[0] and delta and self.on_code_gen_progress:
                        first_chunk[0] = False
                        try:
                            await self.on_code_gen_progress("正在生成代码…")
                        except Exception as e:
                            logger.debug(f"on_code_gen_progress: {e}")
                raw = await self.llm.stream_think(
                    system_prompt=CODE_GENERATION_SYSTEM_PROMPT,
                    user_prompt=prompt, temperature=0.3, on_chunk=_on_chunk)
            else:
                raw = await self.llm.think(
                    system_prompt=CODE_GENERATION_SYSTEM_PROMPT,
                    user_prompt=prompt, temperature=0.3)
            if not raw:
                logger.warning("[CodeLoop] LLM 无输出")
                continue

            code = self._extract_code(raw)
            if not code:
                logger.warning(f"[CodeLoop] 无法提取代码: {raw[:80]}")
                last_error = "No valid JavaScript code block found in LLM response"
                continue

            best_code = code
            logger.debug(f"[CodeLoop] 代码片段:\n{code[:200]}")

            # 2. 执行代码 → /execute_code（Node.js 内连续执行，无 Python 往返）
            logger.log("FLOW", f"_run_task → Env.execute_code(timeout_ms={CODE_TIMEOUT_MS})")
            try:
                exec_r = await env.execute_code(code=code, timeout_ms=CODE_TIMEOUT_MS)
            except Exception as e:
                last_error = str(e); last_output = ""
                logger.error(f"[CodeLoop] execute_code 失败: {e}")
                continue

            last_output  = exec_r.get("output", "")
            success_exec = exec_r.get("success", False)
            last_error   = exec_r.get("error") or "N/A"
            if exec_r.get("game_state"):
                game_state.update(exec_r["game_state"])
            logger.info(f"[CodeLoop] {'✅' if success_exec else '❌'} | {last_output[:80]}")

            if not success_exec:
                logger.warning(f"[CodeLoop] error={last_error[:80]}")
                last_code = code
                continue

            # 3. Critic 验证
            logger.log("FLOW", "_run_task → Critic.check_task_success")
            ok, new_crit = await self._critic_check(task, game_state, last_output)
            logger.info(f"[Critic] success={ok} | {new_crit[:60]}")
            if ok:
                logger.success("[Flow] 任务通过 Critic，后台存储技能")
                asyncio.create_task(self._store_skill(task, best_code, last_output))
                return {
                    "action_type":    "chat",
                    "display_message": f"✅ {task[:40]} 完成（{attempt} 次尝试）",
                    "extra_data": {"success": True, "attempts": attempt,
                                   "code": best_code, "output": last_output},
                }
            critique  = new_crit
            last_code = code

        return {
            "action_type":    "chat",
            "display_message": f"尝试了 {MAX_RETRIES} 次，未能完成「{task}」。{critique}",
            "extra_data":     {"success": False, "critique": critique, "code": best_code},
        }

    # ── 层级任务（复杂指令分解执行）─────────────────────────────────────────

    async def _run_hierarchical(self, game_state: dict, task: str) -> dict:
        logger.log("FLOW", "Planner.decompose(task=...) + _build_context")
        subtasks, _ = await asyncio.gather(
            self.planner.decompose(task, game_state, retriever=self.retriever),
            self._build_context(task, game_state))
        logger.info(f"[Hier] {len(subtasks)} 子任务: {[s.name for s in subtasks]}")
        completed, failed = [], []
        for st in subtasks:
            if await self.planner.check_if_satisfied(st, game_state):
                st.status = "skipped"; completed.append(st.name); continue
            st.status = "running"
            ctx    = await self._build_context(st.description, game_state)
            result = await self._run_task(st.description, game_state, context=ctx)
            if result.get("extra_data", {}).get("success"):
                st.status = "done"; completed.append(st.name)
                try:
                    fresh = await get_env().observe()
                    if fresh: game_state.update(fresh)
                except Exception:
                    pass
            else:
                st.status = "failed"; failed.append(st.name)
                if not st.can_skip_if: break
        success = len(completed) >= len(subtasks) * 0.7
        msg = (f"完成 {len(completed)}/{len(subtasks)} 子任务"
               + (f"（{','.join(completed)}）" if completed else "")
               + (f"，失败：{','.join(failed)}" if failed else ""))
        return {"action_type": "chat", "display_message": msg, "extra_data": {"success": success}}

    # ── Context（三路并发：技能JS代码 + RAG知识 + 记忆）──────────────────────

    async def _build_context(self, task: str, game_state: dict) -> str:
        logger.log("FLOW", "_build_context → skill+RAG+memory 三路并发")
        s, r, m = await asyncio.gather(
            self._fetch_skill_programs(task),
            self._fetch_rag_context(task),
            self.memory.get_relevant_context(task),
            return_exceptions=True)
        parts = []
        if isinstance(s, str) and s.strip(): parts.append(s)
        if isinstance(r, str) and r.strip(): parts.append(r)
        if isinstance(m, str) and m.strip(): parts.append(f"## Past Experience:\n{m}")
        return "\n\n".join(parts)

    async def _fetch_skill_programs(self, task: str) -> str:
        try: return await self.skill_lib.get_programs_string(task, top_k=3)
        except Exception as e: logger.debug(f"技能检索失败: {e}"); return ""

    async def _fetch_rag_context(self, task: str) -> str:
        if not self.retriever: return ""
        try:
            docs = await self.retriever.search(task, top_k=4)
            if not docs: return ""
            lines = ["## Relevant Minecraft Knowledge:"]
            for d in docs:
                if d.get("score", 0) > 0.3:
                    lines.append(f"// {d.get('title','')}  {d.get('content','')[:250]}")
            return "\n".join(lines) if len(lines) > 1 else ""
        except Exception as e: logger.debug(f"RAG失败: {e}"); return ""

    # ── Critic & 技能存储 ─────────────────────────────────────────────────────

    async def _critic_check(self, task, game_state, last_output) -> tuple[bool, str]:
        try: return await self.critic.check_task_success(
            task=task, game_state=game_state, last_observation=last_output)
        except Exception as e: return False, str(e)

    async def _store_skill(self, task: str, code: str, output: str):
        try:
            sk = await self.skill_lib.abstract_from_code(
                task=task, code=code, output=output, verified_success=True)
            if sk: logger.info(f"[Skill] ✅ 存储: {sk.get('name')}")
        except Exception as e: logger.warning(f"[Skill] 存储失败: {e}")

    # ── Prompt 构建 ───────────────────────────────────────────────────────────

    def _build_code_prompt(self, task, game_state, context, last_code,
                           execution_error, chat_log, critique, attempt: int = 1) -> str:
        pos = game_state.get("position", {})
        inv = game_state.get("inventory", [])
        eq  = game_state.get("equipment", {})
        nb  = game_state.get("nearby_blocks", {})
        ne  = game_state.get("nearby_entities", [])
        tod = game_state.get("time", 0)
        tod_str = ("midnight" if tod < 1000 else "day" if tod < 13000
                   else "sunset" if tod < 14000 else "night")
        base_critique = critique or "N/A"
        if attempt >= 3:
            base_critique += "\n[重试要求] 请更保守：增加距离/背包检查、使用 exploreUntil 等容错手段，避免重复上次失败做法。"
        elif attempt >= 2:
            base_critique += "\n[重试要求] 请换一种思路，不要重复上次做法。"
        body = CODE_GENERATION_HUMAN_TEMPLATE.format(
            last_code=last_code, execution_error=execution_error,
            chat_log=(chat_log or "N/A")[:300],
            biome=game_state.get("biome", "unknown"), time_of_day=tod_str,
            nearby_blocks=", ".join(f"{k}:{v}" for k, v in list(nb.items())[:15]) or "none",
            nearby_entities=", ".join(
                f"{e.get('name','?')}({e.get('distance','?')}m)" for e in ne[:8]) or "none",
            health=game_state.get("health", 20), food=game_state.get("food", 20),
            pos_x=round(pos.get("x", 0), 1) if isinstance(pos, dict) else 0,
            pos_y=round(pos.get("y", 64), 1) if isinstance(pos, dict) else 64,
            pos_z=round(pos.get("z", 0), 1) if isinstance(pos, dict) else 0,
            equipment=eq.get("mainhand") or "nothing",
            inventory=", ".join(
                f"{i.get('item','?')}x{i.get('count',1)}" for i in inv[:20]) or "empty",
            inv_used=sum(1 for i in inv if i),
            task=task, context=context or "N/A", critique=base_critique)
        return body

    @staticmethod
    def _extract_code(raw: str) -> str | None:
        """从 LLM 输出提取 JS 代码块（对应 Voyager _process_ai_message）"""
        m = re.search(r"```(?:javascript|js)\s*(.*?)```", raw, re.DOTALL)
        if m: return m.group(1).strip()
        m = re.search(r"```\s*(async function.*?)```", raw, re.DOTALL)
        if m: return m.group(1).strip()
        m = re.search(r"(async\s+function\s+\w+\s*\([^)]*\)\s*\{.*\})", raw, re.DOTALL)
        if m: return m.group(1).strip()
        return None

    # ── 闲聊 & 分类 ──────────────────────────────────────────────────────────

    async def _classify(self, message: str, game_state: dict) -> dict:
        """一次调用得到 intent、complexity、feasible、reason。"""
        inv = game_state.get("inventory", [])
        inv_summary = []
        if isinstance(inv, list):
            for i in inv[:24]:
                name = (i.get("item") or i.get("name") or "?").strip()
                cnt = i.get("count", 1)
                if name and name != "?":
                    inv_summary.append(f"{name}x{cnt}")
        gs = {
            "position": game_state.get("position"),
            "health": game_state.get("health"),
            "inventory_count": len(inv),
            "inventory": inv_summary if inv_summary else "空或未提供",
        }
        user = f"玩家消息：{message}\n当前状态摘要：{json.dumps(gs, ensure_ascii=False)}"
        raw = await self.llm.classify(
            system_prompt=UNIFIED_CLASSIFY_PROMPT,
            user_prompt=user,
            temperature=0.2,
        )
        default = {"intent": "task_execution", "complexity": "simple", "feasible": True, "reason": ""}
        if not raw or not raw.strip():
            return default
        try:
            text = self._strip_json(raw)
            data = json.loads(text)
            intent = (data.get("intent") or "task_execution").strip().lower()
            if "task" in intent: intent = "task_execution"
            elif "knowledge" in intent or "qa" in intent: intent = "knowledge_qa"
            else: intent = "chat"
            complexity = (data.get("complexity") or "simple").strip().lower()
            complexity = "complex" if "complex" in complexity else "simple"
            feasible = data.get("feasible")
            if not isinstance(feasible, bool):
                feasible = str(feasible).strip().lower() not in ("false", "no", "0")
            return {
                "intent": intent,
                "complexity": complexity,
                "feasible": feasible,
                "reason": (data.get("reason") or "").strip(),
            }
        except Exception as e:
            logger.debug(f"_classify parse error: {e}, raw={raw[:150]}")
            return default

    async def _try_skill_fast_path(self, task: str, game_state: dict) -> dict | None:
        """任务与技能库快速匹配：同质不同参时用技能代码+参数替换执行，成功则返回结果，否则返回 None 走 CodeLoop。"""
        try:
            results = await self.skill_lib.search_skills(task, top_k=1)
            if not results:
                return None
            item = results[0]
            score = item.get("score") if isinstance(item, dict) else getattr(item, "score", 0)
            skill = item.get("skill", item) if isinstance(item, dict) else item
            if isinstance(skill, dict):
                code = skill.get("code", "")
            else:
                code = getattr(skill, "code", "") or ""
            if not code or not code.strip():
                return None
            # 相似度阈值：高置信时才直接复用
            if score < 0.88:
                return None
            adapted = self._adapt_skill_params(task, code)
            if not adapted:
                return None
            env = get_env()
            try:
                exec_r = await env.execute_code(code=adapted, timeout_ms=CODE_TIMEOUT_MS)
            except Exception as e:
                logger.debug(f"[SkillFastPath] execute 异常: {e}，回退 CodeLoop")
                return None
            if not exec_r.get("success", False):
                logger.debug(f"[SkillFastPath] 执行未成功，回退 CodeLoop")
                return None
            last_output = exec_r.get("output", "")
            if exec_r.get("game_state"):
                game_state.update(exec_r["game_state"])
            ok, crit = await self._critic_check(task, game_state, last_output)
            if not ok:
                logger.debug(f"[SkillFastPath] Critic 未通过: {crit[:50]}，回退 CodeLoop")
                return None
            asyncio.create_task(self._store_skill(task, adapted, last_output))
            return {
                "action_type": "chat",
                "display_message": f"✅ {task[:40]} 完成（技能库匹配）",
                "extra_data": {"success": True, "skill_fast_path": True, "output": last_output},
            }
        except Exception as e:
            logger.debug(f"[SkillFastPath] 异常: {e}")
            return None

    @staticmethod
    def _adapt_skill_params(task: str, code: str) -> str | None:
        """从任务描述提取数量等参数，替换到技能代码中（如 mineBlock 的 count）。无法安全替换时返回 None。"""
        # 提取「N 个 / N个」中的数字
        num_match = re.search(r"(\d+)\s*个", task)
        if not num_match:
            return code
        want_count = int(num_match.group(1))
        # 在常见原语中找第一个数字参数并替换（仅替换明显为数量的参数，避免改坐标）
        # mineBlock(bot, "xxx", N) / craftItem(bot, "xxx", N)
        def replace_count(m):
            pre, num = m.group(1), m.group(2)
            n = int(num)
            if n <= 0 or n > 64:
                return m.group(0)
            return f"{pre}{want_count}"
        code_new = re.sub(
            r"(mineBlock\(bot,\s*[^,]+,\s*)(\d+)(\s*\))",
            replace_count,
            code,
            count=1,
        )
        if code_new == code:
            code_new = re.sub(
                r"(craftItem\(bot,\s*[^,]+,\s*)(\d+)(\s*\))",
                replace_count,
                code,
                count=1,
            )
        return code_new if code_new else code

    async def _chat_reply(self, game_state: dict, message: str) -> dict:
        player_name = game_state.get("player_name", "Player")
        self.personality.record_interaction(player_name, "casual_chat")
        rag_hint = ""
        if self.retriever:
            try:
                docs = await self.retriever.search(message, top_k=2)
                if docs:
                    rag_hint = "\n知识参考：" + "|".join(
                        f"{d.get('title','')}: {d.get('content','')[:100]}"
                        for d in docs if d.get("score", 0) > 0.4)
            except Exception:
                pass
        system = self.personality.get_chat_system_prompt(player_name) + rag_hint
        reply  = await self.llm.think_fast(
            system_prompt=system, user_prompt=message, temperature=0.8)
        return {"action_type": "chat", "display_message": reply or "嗯嗯！", "extra_data": {}}

    @staticmethod
    def _strip_json(raw: str) -> str:
        t = raw.strip()
        if "```" in t: t = re.sub(r"```[a-z]*\n?", "", t).replace("```", "")
        t = re.sub(r"[\x00-\x1f\x7f]", " ", t)
        m = re.search(r"\{.*\}", t, re.DOTALL)
        return m.group(0) if m else t
