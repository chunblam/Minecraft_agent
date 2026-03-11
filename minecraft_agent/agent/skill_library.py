"""
SkillLibrary v3 — Voyager 代码技能版

技能存储格式（改为 JS 函数代码，与 Voyager SkillManager 对等）：
{
  "name": "chop_wood",
  "description": "在附近寻找并采集指定数量的木头",
  "code": "async function chop_wood(bot, params = {}) { ... }",
  "parameters": {"block_type": "oak_log", "count": 5},
  "preconditions": ["附近有树木"],
  "reliability_score": 0.9,
  "success_count": 12,
  "fail_count": 1
}

与 Voyager 的区别：
  - ChromaDB 向量检索（与原版相同）
  - 额外保存 reliability_score（Voyager 无此机制）
  - 技能必须通过 Critic 验证后才存储（Voyager 无此过滤）
  - 检索时返回 code 字符串，直接注入 LLM 的 Context
"""

import json
import re
from loguru import logger

from .llm_router import LLMRouter
from .prompts import SKILL_CODE_SYSTEM_PROMPT, SKILL_CODE_HUMAN_TEMPLATE

try:
    import chromadb
    from chromadb.utils import embedding_functions
    CHROMA_AVAILABLE = True
except ImportError:
    CHROMA_AVAILABLE = False
    logger.warning("[SkillLib] ChromaDB 未安装，使用内存技能库")

MIN_RELIABILITY = 0.4


class SkillLibrary:
    """
    代码技能库。存储 JS 函数，检索时返回代码字符串供 LLM 使用。
    """

    def __init__(self, llm: LLMRouter, persist_dir: str = "./data/skill_db"):
        self.llm = llm
        self.persist_dir = persist_dir
        self._collection = None
        self._memory_store: dict[str, dict] = {}

        if CHROMA_AVAILABLE:
            try:
                self._init_chroma(persist_dir)
            except Exception as e:
                logger.warning(f"[SkillLib] ChromaDB 初始化失败: {e}")

    # ── 技能抽象（成功执行后调用）────────────────────────────────────────────

    async def abstract_from_code(
        self,
        task: str,
        code: str,
        output: str,
        verified_success: bool = False,
    ) -> dict | None:
        """
        从成功执行的 JS 代码中抽象可复用技能。

        Args:
            task:             任务描述
            code:             执行成功的 JS 函数代码
            output:           执行输出（bot.chat 内容）
            verified_success: True = Critic 验证通过，才存储
        """
        if not code or not code.strip():
            return None

        logger.log("FLOW", f"SkillLib.abstract_from_code(task={task[:35]!r}, verified_success={verified_success})")
        user_prompt = SKILL_CODE_HUMAN_TEMPLATE.format(
            task=task,
            code=code,
            output=output[:500],
        )

        raw = await self.llm.classify(
            system_prompt=SKILL_CODE_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.3,
        )
        if not raw:
            return None

        skill = self._parse_skill_json(raw)
        if not skill or not skill.get("code"):
            logger.warning(f"[SkillLib] 技能解析失败: {raw[:100]}")
            return None

        skill["reliability_score"] = 1.0
        skill["success_count"]     = 1
        skill["fail_count"]        = 0

        if verified_success:
            await self._store_skill(skill)
            logger.info(f"[SkillLib] ✅ 技能已存储: {skill.get('name')}")
        return skill

    # ── 技能检索（返回代码字符串，直接注入 LLM）─────────────────────────────

    async def search_skills(
        self, query: str, top_k: int = 5
    ) -> list[dict]:
        """
        检索相关技能。返回 [{"skill": dict, "score": float}]
        """
        if self._collection is not None:
            return await self._chroma_search(query, top_k)
        return self._memory_search(query, top_k)

    async def get_programs_string(self, query: str, top_k: int = 5) -> str:
        """
        返回格式化的技能代码字符串，直接注入到 CODE_GENERATION_HUMAN_TEMPLATE 的 context。
        与 Voyager SkillManager.programs 属性对等。
        """
        logger.log("FLOW", f"SkillLib.get_programs_string(query={query[:40]!r}, top_k={top_k})")
        results = await self.search_skills(query, top_k)
        if not results:
            return ""

        lines = ["## Retrieved Skills (reuse these if applicable):\n"]
        for r in results:
            sk = r["skill"]
            lines.append(f"// {sk.get('description', '')}")
            lines.append(sk.get("code", ""))
            lines.append("")
        return "\n".join(lines)

    # ── ChromaDB ─────────────────────────────────────────────────────────────

    def _init_chroma(self, persist_dir: str):
        client = chromadb.PersistentClient(path=persist_dir)
        ef = embedding_functions.DefaultEmbeddingFunction()
        self._collection = client.get_or_create_collection(
            name="skills_v3",
            embedding_function=ef,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(f"[SkillLib] ChromaDB 就绪，技能数: {self._collection.count()}")

    async def _store_skill(self, skill: dict):
        name = skill.get("name", "unnamed")
        doc  = json.dumps(skill, ensure_ascii=False)

        if self._collection is not None:
            try:
                # 用 description + code 作为 embedding 文本，让语义检索更准
                embed_text = f"{skill.get('description', '')} {skill.get('code', '')[:500]}"
                self._collection.upsert(
                    ids=[name],
                    documents=[embed_text],
                    metadatas=[{
                        "name": name,
                        "description": skill.get("description", ""),
                        "json": doc,
                        "reliability_score": skill.get("reliability_score", 1.0),
                    }],
                )
                return
            except Exception as e:
                logger.warning(f"[SkillLib] ChromaDB 写入失败: {e}")
        self._memory_store[name] = skill

    async def _get_skill(self, name: str) -> dict | None:
        if self._collection is not None:
            try:
                r = self._collection.get(ids=[name])
                if r["metadatas"]:
                    meta = r["metadatas"][0]
                    return json.loads(meta.get("json", "{}"))
            except Exception:
                pass
        return self._memory_store.get(name)

    async def _chroma_search(self, query: str, top_k: int) -> list[dict]:
        if self._collection.count() == 0:
            return []
        try:
            results = self._collection.query(
                query_texts=[query],
                n_results=min(top_k, self._collection.count()),
            )
            output = []
            for meta, dist in zip(results["metadatas"][0], results["distances"][0]):
                reliability = meta.get("reliability_score", 1.0)
                if reliability < MIN_RELIABILITY:
                    continue
                try:
                    skill = json.loads(meta.get("json", "{}"))
                    score = max(0.0, 1.0 - dist)
                    output.append({"skill": skill, "score": score})
                except Exception:
                    continue
            return sorted(output, key=lambda x: x["score"], reverse=True)
        except Exception as e:
            logger.warning(f"[SkillLib] ChromaDB 查询失败: {e}")
            return []

    def _memory_search(self, query: str, top_k: int) -> list[dict]:
        query_lower = query.lower()
        results = []
        for skill in self._memory_store.values():
            if skill.get("reliability_score", 1.0) < MIN_RELIABILITY:
                continue
            score = sum(
                0.5 if w in skill.get("name", "").lower() else
                0.3 if w in skill.get("description", "").lower() else 0
                for w in query_lower.split()
            )
            if score > 0:
                results.append({"skill": skill, "score": score})
        return sorted(results, key=lambda x: x["score"], reverse=True)[:top_k]

    @staticmethod
    def _parse_skill_json(raw: str) -> dict | None:
        text = raw.strip()
        if "```" in text:
            lines = text.split("\n")
            text  = "\n".join(l for l in lines if not l.strip().startswith("```"))
        text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
        text = re.sub(r",\s*}", "}", text)
        text = re.sub(r",\s*]",  "]", text)
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            text = match.group(0)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    def delete_skill(self, name: str) -> bool:
        """
        按名称删除一条技能。用于人工复核后剔除「Critic 通过但实际效果不佳」的技能。
        返回 True 表示已删除，False 表示未找到或删除失败。
        """
        if not name or not name.strip():
            return False
        name = name.strip()
        if self._collection is not None:
            try:
                self._collection.delete(ids=[name])
                logger.info(f"[SkillLib] 已删除技能: {name}")
                return True
            except Exception as e:
                logger.warning(f"[SkillLib] 删除技能失败 {name}: {e}")
                return False
        if name in self._memory_store:
            del self._memory_store[name]
            logger.info(f"[SkillLib] 已从内存删除技能: {name}")
            return True
        return False

    def list_all_skills(self) -> list[dict]:
        """
        列出当前技能库中全部技能（用于导出、人工复核）。返回 [{"name", "description", "code", ...}, ...]。
        """
        out = []
        if self._collection is not None:
            try:
                if self._collection.count() == 0:
                    return []
                data = self._collection.get(include=["metadatas"])
                for meta in (data.get("metadatas") or []):
                    if not meta:
                        continue
                    try:
                        sk = json.loads(meta.get("json", "{}"))
                        if sk.get("name") or sk.get("code"):
                            out.append(sk)
                    except Exception:
                        continue
            except Exception as e:
                logger.warning(f"[SkillLib] list_all 失败: {e}")
            return out
        return list(self._memory_store.values())

    @property
    def skill_count(self) -> int:
        if self._collection is not None:
            return self._collection.count()
        return len(self._memory_store)
