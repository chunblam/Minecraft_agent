"""
技能库模块（v3 —— 混合方案：支持参数化可执行技能）

技能分三种类型：
  - simple       : 单一 ReAct 循环完成的技能（原有格式，步骤提示）
  - hierarchical : 多子任务层级任务完成的技能（原有格式）
  - parameterized: 参数化可执行技能（新增，运行时解析参数，任意地点复用）

存储：ChromaDB mc_skills collection（向量化存储，支持语义检索）
检索：输入任务描述，返回最相关的已有技能
学习：从成功的执行轨迹抽象为 simple/hierarchical 或 parameterized 模板
"""

import os
import uuid
import json
from datetime import datetime
import chromadb
from loguru import logger

from .llm_router import LLMRouter


SKILLS_COLLECTION = "mc_skills"

# ── Prompts ───────────────────────────────────────────────────────────────────

SKILL_ABSTRACT_SYSTEM_PROMPT = """你是 Minecraft AI Agent 的技能提炼专家。
从一段单步任务的执行轨迹中提炼可复用技能，输出标准 JSON。

{
  "skill_name": "技能名称（简短、描述性）",
  "skill_type": "simple",
  "description": "技能适用场景的自然语言描述",
  "prerequisites": ["前提条件"],
  "steps": ["步骤1", "步骤2", "步骤3"],
  "skip_conditions": ["已有X时可跳过步骤Y"],
  "tips": ["注意事项"],
  "applicable_scenarios": ["场景关键词1", "场景关键词2"]
}

只输出 JSON。"""

HIERARCHICAL_SKILL_ABSTRACT_PROMPT = """你是 Minecraft AI Agent 的技能提炼专家。
从一个层级任务的执行过程中提炼可复用的复合技能，输出标准 JSON。

{
  "skill_name": "技能名称",
  "skill_type": "hierarchical",
  "description": "适用场景描述",
  "prerequisites": ["前提条件"],
  "subtasks": [
    {
      "name": "子任务名",
      "description": "做什么",
      "can_skip_if": "满足什么条件时跳过"
    }
  ],
  "tips": ["整体注意事项"],
  "applicable_scenarios": ["场景关键词"]
}

只输出 JSON。"""

# 参数化技能抽象 Prompt（用于「挖N个X」「砍N棵Y」等可复用模式）
PARAMETERIZED_SKILL_ABSTRACT_PROMPT = """你是 Minecraft AI Agent 的技能提炼专家。
从执行轨迹中判断是否为「采集类」重复模式（如：找资源→移动→挖掘，循环多次）。
若是，输出参数化技能 JSON，可在任意地点复用；否则输出 null。

参数化技能格式（仅当轨迹符合「搜索→移动→挖掘」循环时使用）：
{
  "skill_name": "技能名称",
  "skill_type": "parameterized",
  "template_type": "parameterized",
  "description": "适用场景描述",
  "params_schema": {
    "block_type": {"source": "task", "hint": "资源类型：coal/iron/diamond/oak_log/sand/gravel 等"},
    "count": {"source": "task", "default": 5},
    "radius": {"source": "fixed", "value": 24}
  },
  "procedure": [
    {"action": "find_resource", "params": {"type": "{{block_type}}", "radius": "{{radius}}"}, "store_as": "targets"},
    {"action": "for_each", "over": "targets", "limit": "{{count}}",
      "do": [{"action": "move_to"}, {"action": "mine_block"}]}
  ],
  "applicable_scenarios": ["挖矿", "砍树", "采集"]
}

若轨迹不是采集循环模式（如合成、附魔、建造），只输出：null
只输出 JSON 或 null，不要其他文字。"""


class SkillLibrary:
    """
    技能库管理器。

    - 存储：向量化后存入 ChromaDB mc_skills collection
    - 检索：语义相似度匹配，返回最相关技能的步骤提示
    - 学习：
        abstract_from_trajectory()       - 从单步任务轨迹学习简单技能
        abstract_hierarchical_skill()    - 从层级任务学习复合技能
    """

    def __init__(self) -> None:
        chroma_path = os.getenv("CHROMA_DB_PATH", "./data/chroma_db")
        if not os.path.isabs(chroma_path):
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            chroma_path = os.path.join(base_dir, chroma_path.lstrip("./"))

        self.chroma_client = chromadb.PersistentClient(path=chroma_path)
        self._collection = self.chroma_client.get_or_create_collection(
            name=SKILLS_COLLECTION,
            metadata={"description": "Minecraft Agent 技能库"},
        )
        self.llm = LLMRouter()

        from rag.retriever import EmbeddingClient
        self._embedder = EmbeddingClient()

        logger.info(f"技能库初始化完成，当前技能数: {self._collection.count()}")

    # ── 检索 ───────────────────────────────────────────────────────────────────

    async def search_skill(self, task_description: str, top_k: int = 3) -> list[dict]:
        """
        根据任务描述检索最相关的已有技能。

        Returns:
            技能列表，每项包含 skill（原始 JSON）、skill_name、relevance_score
        """
        if self._collection.count() == 0:
            return []

        try:
            embedding = await self._embedder.embed(task_description)
            results = self._collection.query(
                query_embeddings=[embedding],
                n_results=min(top_k, self._collection.count()),
                include=["documents", "metadatas", "distances"],
            )

            skills = []
            for doc, meta, dist in zip(
                results.get("documents", [[]])[0],
                results.get("metadatas", [[]])[0],
                results.get("distances", [[]])[0],
            ):
                try:
                    skill_json = json.loads(doc)
                except json.JSONDecodeError:
                    skill_json = {"raw": doc}

                skills.append({
                    "skill": skill_json,
                    "skill_name": meta.get("skill_name", "未知技能"),
                    "skill_type": meta.get("skill_type", "simple"),
                    "relevance_score": round(1 - dist, 4),
                    "created_at": meta.get("created_at", ""),
                })

            logger.debug(f"技能检索完成，找到 {len(skills)} 个相关技能")
            return skills

        except Exception as e:
            logger.error(f"技能检索失败: {e}", exc_info=True)
            return []

    # ── 技能学习：简单任务 ─────────────────────────────────────────────────────

    async def abstract_from_trajectory(
        self, task: str, trajectory: list[dict]
    ) -> dict | None:
        """
        从单步任务的执行轨迹中抽象技能。
        优先尝试参数化格式（采集类循环模式），失败则回退到简单步骤格式。

        Args:
            task:       任务描述
            trajectory: 执行轨迹（每项含 thought/action/observation）

        Returns:
            技能字典（已存入 ChromaDB），失败返回 None
        """
        if not trajectory:
            return None

        trajectory_text = self._format_trajectory(trajectory)
        user_prompt = f"任务目标：{task}\n\n执行轨迹：\n{trajectory_text}"

        # 1. 尝试参数化格式（采集类：find_resource + move_to + mine_block 循环）
        logger.info(f"开始抽象技能（优先参数化）: {task[:50]}")
        raw_param = await self.llm.think_fast(
            system_prompt=PARAMETERIZED_SKILL_ABSTRACT_PROMPT,
            user_prompt=user_prompt,
            temperature=0.2,
        )
        if raw_param and "null" not in raw_param.strip().lower():
            skill_json = self._parse_skill_json(raw_param)
            if skill_json and skill_json.get("procedure"):
                skill_json["skill_type"] = "parameterized"
                skill_json["template_type"] = "parameterized"
                await self._store_skill(skill_json)
                return skill_json

        # 2. 回退：简单步骤格式
        raw_output = await self.llm.think_fast(
            system_prompt=SKILL_ABSTRACT_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.3,
        )
        if not raw_output:
            return None

        skill_json = self._parse_skill_json(raw_output)
        if skill_json:
            skill_json.setdefault("skill_type", "simple")
            await self._store_skill(skill_json)
        return skill_json

    # ── 技能学习：层级任务 ─────────────────────────────────────────────────────

    async def abstract_hierarchical_skill(
        self,
        task: str,
        subtasks: list,           # list[SubTask]（避免循环导入，用 list）
        all_trajectories: list[dict],
    ) -> dict | None:
        """
        从层级任务的完整执行过程中抽象复合技能。

        Args:
            task:             顶级任务描述
            subtasks:         SubTask 对象列表（含 name/description/status/can_skip_if）
            all_trajectories: 所有子任务的合并轨迹

        Returns:
            技能字典（已存入 ChromaDB），失败返回 None
        """
        subtask_summary = "\n".join(
            f"- [{s.status}] {s.name}: {s.description}"
            + (f"（可跳过：{s.can_skip_if}）" if s.can_skip_if else "")
            for s in subtasks
        )
        # 限制轨迹长度避免 token 溢出
        trajectory_text = self._format_trajectory(all_trajectories[:20])

        user_prompt = (
            f"复杂任务：{task}\n\n"
            f"子任务执行记录：\n{subtask_summary}\n\n"
            f"关键执行轨迹：\n{trajectory_text}"
        )

        logger.info(f"开始抽象层级技能: {task[:50]}")

        raw_output = await self.llm.think_fast(
            system_prompt=HIERARCHICAL_SKILL_ABSTRACT_PROMPT,
            user_prompt=user_prompt,
            temperature=0.3,
        )

        if not raw_output:
            return None

        skill_json = self._parse_skill_json(raw_output)
        if skill_json:
            skill_json["skill_type"] = "hierarchical"
            await self._store_skill(skill_json)
        return skill_json

    # ── 辅助 ───────────────────────────────────────────────────────────────────

    async def _store_skill(self, skill_json: dict) -> None:
        """向量化后存入 ChromaDB"""
        try:
            embed_text = (
                f"{skill_json.get('skill_name', '')} "
                f"{skill_json.get('description', '')} "
                f"{' '.join(skill_json.get('applicable_scenarios', []))}"
            )
            embedding = await self._embedder.embed(embed_text)
            skill_id = str(uuid.uuid4())

            self._collection.add(
                ids=[skill_id],
                embeddings=[embedding],
                documents=[json.dumps(skill_json, ensure_ascii=False)],
                metadatas=[{
                    "skill_name": skill_json.get("skill_name", ""),
                    "skill_type": skill_json.get("skill_type", "simple"),
                    "created_at": datetime.now().isoformat(),
                }],
            )
            logger.success(
                f"技能已存储 [{skill_json.get('skill_type', 'simple')}]: "
                f"{skill_json.get('skill_name', '?')} (id={skill_id})"
            )
        except Exception as e:
            logger.error(f"技能存储失败: {e}", exc_info=True)

    def get_all_skill_names(self) -> list[str]:
        """获取所有技能名称（用于展示）"""
        try:
            results = self._collection.get(include=["metadatas"])
            return [m.get("skill_name", "") for m in (results.get("metadatas") or [])]
        except Exception as e:
            logger.error(f"获取技能名称失败: {e}")
            return []

    @staticmethod
    def _format_trajectory(trajectory: list[dict]) -> str:
        lines = []
        for i, step in enumerate(trajectory, 1):
            lines.append(f"步骤 {i}:")
            if "thought" in step:
                lines.append(f"  思考: {step['thought'][:80]}")
            if "action" in step:
                lines.append(f"  行动: {step['action']}")
            if "observation" in step:
                lines.append(f"  观察: {step['observation'][:80]}")
        return "\n".join(lines)

    @staticmethod
    def _parse_skill_json(raw: str) -> dict | None:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            cleaned = "\n".join(
                lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
            )
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.error(f"技能 JSON 解析失败: {e}\n原文: {raw[:200]}")
            return None
