"""
技能库 v2（Refactored）

核心变更：
1. 技能只在 Critic 验证成功后才存储 —— 保证质量
2. 技能格式加入 preconditions / postconditions / reliability_score
3. 技能检索返回置信度分数，低分技能不使用
4. 加入技能可靠性追踪（成功/失败次数），自动降级失效技能

技能存储格式（改进后）：
{
  "name": "chop_wood",
  "description": "在指定范围内找到并采集指定数量的木头",
  "parameters": {"block_type": "oak_log", "count": 5, "radius": 24},
  "preconditions": ["有斧头或手（工具已装备）"],
  "steps": [
    {"action": "find_resource", "params": {"type": "{{block_type}}", "radius": "{{radius}}"}, "description": "找到最近的木头"},
    {"action": "move_to", "params": {"region_center": "{{first_result}}", "radius": 2}, "description": "走到木头旁边"},
    {"action": "mine_block", "params": {"x": "{{x}}", "y": "{{y}}", "z": "{{z}}"}, "description": "挖掘木头"}
  ],
  "postconditions": ["背包中有 {{count}} 个 {{block_type}}"],
  "skill_type": "simple",
  "reliability_score": 0.85,   # 0.0~1.0
  "success_count": 17,
  "fail_count": 3
}
"""

import json
import re
from loguru import logger

from .llm_router import LLMRouter
from .prompts import SKILL_ABSTRACT_SYSTEM_PROMPT

# ChromaDB（可选，不影响核心逻辑）
try:
    import chromadb
    from chromadb.utils import embedding_functions
    CHROMA_AVAILABLE = True
except ImportError:
    CHROMA_AVAILABLE = False
    logger.warning("[SkillLib] ChromaDB 未安装，使用内存技能库")


MIN_RELIABILITY_SCORE = 0.4   # 低于此分数的技能不会被推荐使用


class SkillLibrary:
    """
    技能存储与检索。

    重要规则：
      - 只有 CriticAgent 验证成功的轨迹才可以存储技能
      - 技能使用后根据结果更新 reliability_score
    """

    def __init__(self, llm: LLMRouter, persist_dir: str = "./skill_db"):
        self.llm = llm
        self.persist_dir = persist_dir
        self._collection = None
        self._memory_store: dict[str, dict] = {}  # 内存回退

        if CHROMA_AVAILABLE:
            try:
                self._init_chroma(persist_dir)
            except Exception as e:
                logger.warning(f"[SkillLib] ChromaDB 初始化失败: {e}，使用内存存储")

    # ── 技能抽象（仅在成功后调用）────────────────────────────────────────────

    async def abstract_from_trajectory(
        self,
        task: str,
        trajectory: list[dict],
        verified_success: bool = False,
    ) -> dict | None:
        """
        从执行轨迹中抽象技能。

        Args:
            task:              任务描述
            trajectory:        步骤轨迹列表
            verified_success:  必须为 True（Critic 验证后）才会存储

        ⚠️ 如果 verified_success=False，只抽象但不存储。
        """
        if not trajectory:
            return None

        # 格式化轨迹
        traj_text = self._format_trajectory(trajectory[:15])
        user_prompt = f"Task: {task}\n\nExecution trajectory:\n{traj_text}\n\nExtract a reusable skill:"

        raw = await self.llm.think_fast(
            system_prompt=SKILL_ABSTRACT_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.3,
        )

        if not raw:
            return None

        skill = self._parse_skill_json(raw)
        if not skill:
            logger.warning(f"[SkillLib] 技能解析失败: {raw[:100]}")
            return None

        skill.setdefault("skill_type", "simple")
        skill["reliability_score"] = 1.0  # 初始分数（经验证的成功）
        skill["success_count"] = 1
        skill["fail_count"] = 0

        if verified_success:
            await self._store_skill(skill)
            logger.info(f"[SkillLib] ✅ 技能已存储: {skill.get('name', '?')}")
        else:
            logger.debug(f"[SkillLib] 技能抽象完成但未存储（未经 Critic 验证）")

        return skill

    # ── 技能检索 ─────────────────────────────────────────────────────────────

    async def search_skill(
        self, query: str, top_k: int = 3, min_reliability: float = MIN_RELIABILITY_SCORE
    ) -> list[dict]:
        """
        检索相关技能，过滤掉低可靠性的技能。

        Returns:
            list of {"skill": dict, "score": float}，按相关度降序
        """
        if self._collection is not None:
            return await self._chroma_search(query, top_k, min_reliability)
        else:
            return self._memory_search(query, top_k, min_reliability)

    # ── 可靠性更新 ────────────────────────────────────────────────────────────

    async def update_reliability(self, skill_name: str, success: bool) -> None:
        """
        技能使用后根据结果更新可靠性分数。
        使用指数移动平均：score = 0.8 * score + 0.2 * outcome
        """
        skill = await self._get_skill(skill_name)
        if not skill:
            return

        if success:
            skill["success_count"] = skill.get("success_count", 0) + 1
        else:
            skill["fail_count"] = skill.get("fail_count", 0) + 1

        total = skill["success_count"] + skill["fail_count"]
        raw_rate = skill["success_count"] / total if total > 0 else 0.5
        # 指数移动平均，保留历史权重
        old_score = skill.get("reliability_score", 0.5)
        skill["reliability_score"] = 0.7 * old_score + 0.3 * raw_rate

        await self._update_skill(skill)
        logger.debug(
            f"[SkillLib] 更新技能 '{skill_name}' 可靠性: {skill['reliability_score']:.2f} "
            f"(成功={skill['success_count']}, 失败={skill['fail_count']})"
        )

    # ── 存储实现 ─────────────────────────────────────────────────────────────

    def _init_chroma(self, persist_dir: str):
        client = chromadb.PersistentClient(path=persist_dir)
        ef = embedding_functions.DefaultEmbeddingFunction()
        self._collection = client.get_or_create_collection(
            name="skills_v2",
            embedding_function=ef,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(f"[SkillLib] ChromaDB 已初始化，当前技能数: {self._collection.count()}")

    async def _store_skill(self, skill: dict) -> None:
        name = skill.get("name", "unnamed")
        doc = json.dumps(skill, ensure_ascii=False)

        if self._collection is not None:
            try:
                # 用 name 作为 ID，同名技能覆盖
                self._collection.upsert(
                    ids=[name],
                    documents=[doc],
                    metadatas=[{
                        "skill_type": skill.get("skill_type", "simple"),
                        "reliability_score": skill.get("reliability_score", 1.0),
                    }],
                )
            except Exception as e:
                logger.warning(f"[SkillLib] ChromaDB 写入失败: {e}，回退内存")
                self._memory_store[name] = skill
        else:
            self._memory_store[name] = skill

    async def _get_skill(self, skill_name: str) -> dict | None:
        if self._collection is not None:
            try:
                result = self._collection.get(ids=[skill_name])
                if result["documents"]:
                    return json.loads(result["documents"][0])
            except Exception:
                pass
        return self._memory_store.get(skill_name)

    async def _update_skill(self, skill: dict) -> None:
        await self._store_skill(skill)

    async def _chroma_search(
        self, query: str, top_k: int, min_reliability: float
    ) -> list[dict]:
        if self._collection.count() == 0:
            return []
        try:
            results = self._collection.query(
                query_texts=[query],
                n_results=min(top_k * 2, self._collection.count()),
            )
            output = []
            for doc, meta, dist in zip(
                results["documents"][0],
                results["metadatas"][0],
                results["distances"][0],
            ):
                reliability = meta.get("reliability_score", 1.0)
                if reliability < min_reliability:
                    continue
                try:
                    skill = json.loads(doc)
                    score = max(0.0, 1.0 - dist)
                    output.append({"skill": skill, "score": score})
                except json.JSONDecodeError:
                    continue
            output.sort(key=lambda x: x["score"], reverse=True)
            return output[:top_k]
        except Exception as e:
            logger.warning(f"[SkillLib] ChromaDB 查询失败: {e}")
            return []

    def _memory_search(
        self, query: str, top_k: int, min_reliability: float
    ) -> list[dict]:
        """内存模式：简单关键词匹配"""
        query_lower = query.lower()
        results = []
        for skill in self._memory_store.values():
            if skill.get("reliability_score", 1.0) < min_reliability:
                continue
            name = skill.get("name", "").lower()
            desc = skill.get("description", "").lower()
            score = 0.0
            for word in query_lower.split():
                if word in name:
                    score += 0.5
                if word in desc:
                    score += 0.3
            if score > 0:
                results.append({"skill": skill, "score": score})
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]

    # ── 工具方法 ─────────────────────────────────────────────────────────────

    @staticmethod
    def _format_trajectory(trajectory: list[dict]) -> str:
        lines = []
        for step in trajectory:
            lines.append(
                f"Step {step.get('step', '?')}: {step.get('action', '?')} "
                f"| params={step.get('action_params', {})} "
                f"| obs={step.get('observation', '')[:100]}"
            )
        return "\n".join(lines)

    @staticmethod
    def _parse_skill_json(raw: str) -> dict | None:
        text = raw.strip()
        if "```" in text:
            lines = text.split("\n")
            text = "\n".join(l for l in lines if not l.strip().startswith("```"))
        text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
        text = re.sub(r",\s*}", "}", text)
        text = re.sub(r",\s*]", "]", text)

        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            text = match.group(0)

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    @property
    def skill_count(self) -> int:
        if self._collection is not None:
            return self._collection.count()
        return len(self._memory_store)
