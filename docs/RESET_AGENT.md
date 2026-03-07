# Agent 重置指南

在架构变更、数据格式更新或想清空 Agent 状态时，使用 `reset_agent.py` 进行重置。

## 快速操作

```bash
cd minecraft_agent

# 推荐：仅重置 Agent 状态（技能库 + 长期记忆），保留知识库
python reset_agent.py

# 完全重置（删除整个 ChromaDB）
python reset_agent.py --full
# 然后重新加载知识库：
python load_knowledge_base.py
```

## 选项说明

| 命令 | 效果 |
|------|------|
| `python reset_agent.py` | 清空技能库 + 长期记忆，**保留**知识库 embeddings |
| `python reset_agent.py --full` | 删除整个 `data/chroma_db`，需重新运行 `load_knowledge_base.py` |
| `python reset_agent.py --skills` | 仅清空技能库 |
| `python reset_agent.py --memory` | 仅清空长期记忆 |

## 数据存储位置

- **ChromaDB**：`./data/chroma_db`（可通过 `.env` 的 `CHROMA_DB_PATH` 修改）
  - `mc_skills`：技能库
  - `mc_long_term_memory`：长期记忆
  - `mc_base`、`mc_mob_*` 等：知识库 embeddings
- **知识库源文件**：`./data/knowledge_base/`（不会被重置脚本删除）

## 何时使用

- 修改了技能 schema（如 parameterized 格式）→ 运行 `reset_agent.py` 或 `--skills`
- 修改了记忆结构 → 运行 `reset_agent.py` 或 `--memory`
- 修改了知识库加载逻辑或 collection 结构 → 运行 `reset_agent.py --full`，再执行 `load_knowledge_base.py`
