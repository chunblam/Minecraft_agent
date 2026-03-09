# Minecraft AI Agent

基于 LLM 的 Minecraft 游戏助手，参考 [Voyager](https://voyager.minedojo.org/) 论文设计。**当前版本使用 Mineflayer（Node.js）作为底层控制**，Bot 以独立玩家身份加入游戏，无需安装 Fabric Mod。

## 架构概览

```
┌─────────────────┐       HTTP        ┌─────────────────┐
│  Python Agent   │ ───────────────►  │  Mineflayer     │
│  (VoyagerAgent  │   localhost:3000  │  Node.js Server │
│   Critic/规划)  │ ◄───────────────  │  (pathfinder等) │
└─────────────────┘                   └─────────────────┘
        │                                        │
        │ 意图识别 → 迭代重试 + Critic 验证        │ Bot 加入游戏
        │ 技能库（验证后存储）                     │ 寻路/挖掘/合成
        ▼                                        ▼
   LLM (OpenAI/DeepSeek 等)                 Minecraft 服务器
```

- **原架构（可选）**：Python WebSocket 服务 + Java Fabric Mod 操控主角，见 `mc_fabric_mod/`，当前仓库保留代码供参考。

## 快速开始（Mineflayer 版）

### 1. 安装 Node.js 依赖

```bash
cd minecraft_agent/mineflayer
npm install
```

需要 Node.js >= 18。

### 2. 安装 Python 依赖

```bash
cd minecraft_agent
cp .env.example .env   # 填写 LLM_API_KEY、LLM_BASE_URL 等
pip install -r requirements.txt
```

### 3. 启动 Minecraft

- **单人游戏（推荐调试）**：进入世界 → `Esc` → 对局域网开放，记下端口（如 `12345`）。
- **专用服务器**：端口默认 `25565`，需在 `server.properties` 中设置 `online-mode=false`（Mineflayer 使用离线账号）。

### 4. 启动 Agent

```bash
cd minecraft_agent
# 自动启动 mineflayer 进程并连接 MC
MC_PORT=12345 python main.py
```

若 Minecraft 对局域网开放端口为 `12345`，则上述命令即可。Agent 启动后 Bot 会加入游戏，在聊天中对 Bot 说话（或 `/msg Agent 你好`）即可触发回复与任务执行。

### 5. 可选：手动启动 mineflayer（调试用）

```bash
cd minecraft_agent/mineflayer && node index.js 3000
# 另开终端
cd minecraft_agent && MC_PORT=12345 python main.py
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MC_HOST` | `localhost` | Minecraft 服务器地址 |
| `MC_PORT` | `25565` | Minecraft 端口（局域网开放时改为实际端口） |
| `MC_USERNAME` | `Agent` | Bot 用户名 |
| `MINEFLAYER_PORT` | `3000` | mineflayer HTTP 服务端口 |
| `LLM_API_KEY` | - | OpenAI/DeepSeek/Claude API Key |
| `LLM_BASE_URL` | - | API Base URL（自定义时填写） |
| `LLM_MODEL` | `gpt-4o-mini` | 模型名称 |
| `MAX_TASK_RETRIES` | `4` | 任务最大重试次数（Voyager 风格） |
| `MAX_REACT_STEPS` | `12` | 单次执行最大 ReAct 步数 |

详见 `.env.example`。

## 项目结构

| 目录/文件 | 说明 |
|-----------|------|
| `minecraft_agent/` | Python Agent（VoyagerAgent、Critic、Planner、技能库、env） |
| `minecraft_agent/mineflayer/` | Node.js Mineflayer HTTP 服务（index.js、lib/primitives.js） |
| `mc_fabric_mod/` | 原 Fabric Mod（可选，当前版本不依赖） |
| `docs/` | 扩展指南 |

## 核心能力

- **任务执行**：砍树、挖矿、合成、熔炼、与实体互动；move_to 由 pathfinder 处理，无需自写寻路。
- **Voyager 机制**：迭代重试 + Critic 自我验证，失败时将 critique 注入下一轮 prompt。
- **技能学习**：仅在 Critic 验证成功后存储技能，带可靠性分数。
- **层级任务**：复杂任务分解为子任务，按依赖顺序执行并可跳过已满足子任务。

## Mineflayer vs Java Mod

| 功能 | Java Fabric Mod | Mineflayer |
|------|-----------------|------------|
| 路径规划 | 自实现 | 内置 pathfinder |
| 挖掘 | 自实现 | collectBlock 自动寻路挖掘 |
| 合成/熔炼 | 自实现 | bot.craft / openFurnace |
| 安装 | 需安装 Mod | 无需 Mod，Bot 独立进服 |
| 调试 | 重编译 Java | 改 JS 即可热重载 |

## 文档

| 文档 | 说明 |
|------|------|
| [架构与运行逻辑](docs/ARCHITECTURE.md) | 系统架构、消息协议、模块职责 |
| [项目评估](docs/PROJECT_EVALUATION.md) | 技术栈、待改进项 |
| [Agent 重置指南](docs/RESET_AGENT.md) | 技能库、记忆、知识库重置 |

## 常见问题

**Q: Bot 加入游戏后立刻断开？**  
A: 将服务器 `server.properties` 中 `online-mode` 设为 `false`。

**Q: craft_item 失败说没有工作台？**  
A: 让 agent 先执行 `place_block` 放置工作台，或在地图中预先放一个。

**Q: 仍想用原来的 Fabric Mod 操控主角？**  
A: 保留 `mc_fabric_mod` 代码可编译使用，但当前 main.py 与 agent 已改为 Mineflayer 版，若需恢复 WebSocket+Mod 需回退或分支实现。

## 许可证

MIT
