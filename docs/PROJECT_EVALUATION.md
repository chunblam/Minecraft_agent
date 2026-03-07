# Minecraft AI Agent 项目评估

本文档基于对全项目代码的详细分析，评估整体架构、理想运行逻辑及改进空间。

---

## 一、整体架构评分：★★★★☆

| 维度 | 评分 | 说明 |
|------|------|------|
| **模块划分** | ★★★★★ | Python / Java 职责清晰，WebSocket 协议稳定 |
| **可扩展性** | ★★★★☆ | 技能库、动作类型易扩展；战斗等需新增 action |
| **可维护性** | ★★★★☆ | 注释已统一，R1 残留已清理 |
| **与 Voyager 对齐** | ★★★★☆ | 技能库、Plan-then-Execute、输入模拟已实现 |

---

## 二、理想运行逻辑概览

| 流程 | 触发 | 核心路径 |
|------|------|----------|
| **任务执行** | 玩家聊天（player_chat） | classify_intent → run → 技能检索/Plan-then-Execute → action → Mod 执行 → observation |
| **闲聊/知识** | 玩家聊天 | classify_intent → chat → RAG + 记忆并行检索 → LLM 回复 |
| **自主探索** | game_state_update（每 60s） | 冷却/夜晚检查 → _propose_next_task → run |
| **行动执行** | action 消息 | move_to/mine_block → ClientInputSimulator（客户端）或 PlayerTaskRunner（服务端） |

详见 [架构与运行逻辑](ARCHITECTURE.md)。

---

## 三、已完成的优化（历史清理）

1. **模型统一**：R1 已弃用，全部使用 V3
2. **注释更新**：react_agent、llm_router、planner 中 R1/agent_position 描述已修正
3. **配置精简**：.env.example 移除 R1 配置，API Key 改为占位符
4. **Java 注释**：ActionExecutor、ServerEventHandler、PlayerContext、WorldScanner 已更新
5. **文档同步**：COMBAT_AND_AUTONOMY.md 反映客户端输入模拟架构

---

## 四、技术栈概览

| 层级 | 技术 | 用途 |
|------|------|------|
| LLM | DeepSeek-V3 | 意图识别、任务分解、ReAct、技能抽象 |
| 向量库 | ChromaDB | 技能检索、知识库 RAG |
| Embedding | BAAI/bge-m3 | 文档与查询向量化（含 LRU 缓存） |
| 通信 | WebSocket | Python ↔ Mod 双向消息 |
| 游戏 | Fabric 1.21.1 | 客户端输入模拟 + 服务端行动执行 |

---

## 五、待改进项

| 优先级 | 项目 | 说明 |
|--------|------|------|
| 高 | 战斗动作 | attack_entity 未实现，需扩展 ClientInputSimulator |
| 高 | 复杂任务成功率 | 制作床等多步任务易因 JSON 解析、步数限制失败 |
| 中 | 技能模板命中 | 语义相近任务（如「再去搞一点」）已放宽阈值，可继续观察 |
| 中 | 长距离移动超时 | move_to 192 格等可能超时，需调整或分步 |
| 低 | 背包/合成 UI 模拟 | 当前 craft_item 为直接合成，未模拟打开工作台 |

---

## 六、代码质量

- **Python**：类型注解较完整，async/await 使用规范
- **Java**：Fabric 网络包、tick 级任务结构清晰
- **日志**：loguru + 按日轮转，便于排查

---

## 七、建议的后续方向

1. 实现 `attack_entity`，支持战斗类技能
2. 增强 JSON 解析鲁棒性（已做尾逗号修复，可考虑 json_repair 等）
3. 增加单元测试覆盖核心流程
4. 考虑支持多玩家（当前为单玩家操控）
