# Minecraft AI Agent 项目评估

## 一、整体架构评分：★★★★☆

| 维度 | 评分 | 说明 |
|------|------|------|
| **模块划分** | ★★★★★ | Python / Java 职责清晰，WebSocket 协议稳定 |
| **可扩展性** | ★★★★☆ | 技能库、动作类型易扩展；战斗等需新增 action |
| **可维护性** | ★★★★☆ | 注释已统一，R1 残留已清理 |
| **与 Voyager 对齐** | ★★★★☆ | 技能库、Plan-then-Execute、输入模拟已实现 |

## 二、已完成的优化（本次清理）

1. **模型统一**：R1 已弃用，全部使用 V3
2. **注释更新**：react_agent、llm_router、planner、planner 中 R1/agent_position 描述已修正
3. **配置精简**：.env.example 移除 R1 配置，API Key 改为占位符
4. **Java 注释**：ActionExecutor、ServerEventHandler、PlayerContext、WorldScanner 已更新
5. **文档同步**：COMBAT_AND_AUTONOMY.md 反映客户端输入模拟架构

## 三、技术栈概览

| 层级 | 技术 | 用途 |
|------|------|------|
| LLM | DeepSeek-V3 | 意图识别、任务分解、ReAct、技能抽象 |
| 向量库 | ChromaDB | 技能检索、知识库 RAG |
| Embedding | BAAI/bge-m3 | 文档与查询向量化 |
| 通信 | WebSocket | Python ↔ Mod 双向消息 |
| 游戏 | Fabric 1.21.1 | 客户端输入模拟 + 服务端行动执行 |

## 四、待改进项

| 优先级 | 项目 | 说明 |
|--------|------|------|
| 高 | 战斗动作 | attack_entity 未实现，需扩展 ClientInputSimulator |
| 高 | 复杂任务成功率 | 制作床等多步任务易因 JSON 解析、步数限制失败 |
| 中 | 技能模板命中 | 语义相近任务（如「再去搞一点」）已放宽阈值，可继续观察 |
| 中 | 长距离移动超时 | move_to 192 格等可能超时，需调整或分步 |
| 低 | 背包/合成 UI 模拟 | 当前 craft_item 为直接合成，未模拟打开工作台 |

## 五、代码质量

- **Python**：类型注解较完整，async/await 使用规范
- **Java**：Fabric 网络包、tick 级任务结构清晰
- **日志**：loguru + 按日轮转，便于排查

## 六、建议的后续方向

1. 实现 `attack_entity`，支持战斗类技能
2. 增强 JSON 解析鲁棒性（已做尾逗号修复，可考虑 json_repair 等）
3. 增加单元测试覆盖核心流程
4. 考虑支持多玩家（当前为单玩家操控）
