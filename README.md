# Minecraft AI Agent

基于 LLM 的 Minecraft 游戏助手，参考 [Voyager](https://voyager.minedojo.org/) 设计。使用 **Mineflayer（Node.js）** 作为底层控制，Bot 以独立玩家身份加入游戏，**无需安装 Fabric Mod**。支持自然语言指令、ReAct 式代码生成与重试、Critic 自验证、技能库沉淀与 RAG 知识检索。

---

## 架构概览

```
┌─────────────────────┐         HTTP          ┌─────────────────────┐
│  Python Agent       │  ◄─────────────────►  │  Mineflayer         │
│  VoyagerAgent       │   localhost:3000      │  Node.js Server     │
│  Critic / Planner   │   execute_code 等     │  pathfinder / 原语  │
│  技能库 / RAG / 记忆 │                       │  Bot 加入游戏        │
└─────────────────────┘                       └─────────────────────┘
        │                                                │
        │  意图识别 → 复杂度 → 三路 context → 代码生成     │
        │  → 执行 → Critic 验证 → 成功则技能入库           │
        ▼                                                ▼
   LLM (DeepSeek / OpenAI 等)                    Minecraft 服务器
```

- **Python 侧**：意图分类、任务复杂度判定、技能检索 + RAG + 记忆三路并发构建上下文，生成 JS 代码，经 Critic 验证成功后抽象为技能写入 ChromaDB。
- **Node 侧**：Mineflayer 提供寻路、挖掘、合成、熔炼等原语；Bot 断线/崩溃时 Python 可触发重启 Node 并重连（`ensure_bot_connected`）。

---

## 快速开始

### 环境要求

- **Python** ≥ 3.9  
- **Node.js** ≥ 18  
- **Minecraft Java Edition**（单人世界对局域网开放，或专用服务器）

### 1. 克隆并进入项目

```bash
git clone https://github.com/你的用户名/Minecraft_agent.git
cd Minecraft_agent/minecraft_agent
```

### 2. 安装 Node 依赖

```bash
cd mineflayer
npm install
cd ..
```

### 3. 安装 Python 依赖并配置环境变量

```bash
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env：填写 DEEPSEEK_API_KEY（或硅基流动等）、MC_HOST、MC_PORT 等
```

### 4. 启动 Minecraft

- **单人**：进入世界 → 对局域网开放 → 记下端口（如 `25565` 或 `12345`）。
- **专用服**：在 `server.properties` 中设置 `online-mode=false`（Mineflayer 使用离线账号）。

### 5. 启动 Agent

```bash
# 从 minecraft_agent 目录执行；自动启动 mineflayer 并连接 MC
MC_PORT=25565 python main.py
```

启动后 Bot 会加入游戏。在聊天中对 Bot 说话（或 `/msg Agent 你好`）即可下发任务，例如：「帮我砍 5 个木头」「制作一把木斧」「来到我身边」。

---

## 环境变量（.env）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MC_HOST` | `localhost` | Minecraft 服务器地址（WSL 下连 Windows 可用宿主机 IP） |
| `MC_PORT` | `25565` | 端口（局域网开放时改为实际端口） |
| `MC_USERNAME` | `Agent` | Bot 游戏内名称 |
| `MINEFLAYER_PORT` | `3000` | mineflayer HTTP 服务端口 |
| `DEEPSEEK_API_KEY` | - | DeepSeek / 硅基流动等 API Key |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | API 地址（硅基流动可改为 `https://api.siliconflow.cn/v1`） |
| `DEEPSEEK_V3_MODEL` | `deepseek-chat` | 模型名 |
| `DEEPSEEK_LLM_TIMEOUT` | `90` | 单次 LLM 调用超时（秒） |
| `CHROMA_DB_PATH` | `./data/chroma_db` | 技能库 / RAG 向量库路径 |
| `KNOWLEDGE_BASE_PATH` | `./data/knowledge_base` | RAG 知识库目录 |
| `MAX_TASK_RETRIES` | `4` | 单任务最大重试次数 |
| `MAX_REACT_STEPS` | `12` | 单次运行最大 ReAct 步数 |
| `AUTONOMOUS_IDLE_SECONDS` | `60` | 空闲多少秒后触发自主探索（聊天输入「autonomous explore」开启） |

更多项见 `minecraft_agent/.env.example`。

---

## 项目结构

```
Minecraft_agent/
├── README.md
├── .gitignore
├── docs/                          # 文档
│   ├── EVALUATION_2026-03-10.md   # 项目评估与改进建议
│   ├── PUSH_TO_GITHUB.md          # 推送到 GitHub 指引
│   ├── framework_and_test_guide.md
│   ├── skill_library_quality_evaluation.md
│   └── ...
└── minecraft_agent/
    ├── main.py                    # 入口：连接 MC、聊天循环、意图路由、重连
    ├── requirements.txt
    ├── .env.example
    ├── agent/
    │   ├── react_agent.py         # VoyagerAgent：意图/复杂度、_run_task、ReAct、技能入库
    │   ├── critic.py              # CriticAgent：任务成功自验证
    │   ├── planner.py             # TaskPlanner：层级任务分解
    │   ├── plan_executor.py       # 层级计划执行
    │   ├── skill_library.py       # 技能检索与存储（ChromaDB）
    │   ├── llm_router.py          # LLM 调用路由（DeepSeek 等）
    │   ├── env.py                 # MineflayerEnv、ensure_bot_connected、Node 重启
    │   ├── prompts.py             # 系统/用户 prompt 模板
    │   ├── memory.py              # 短期/长期记忆
    │   ├── personality.py         # 情绪与关系
    │   ├── autonomous_explorer.py # 自主探索逻辑
    │   └── ...
    ├── mineflayer/
    │   ├── index.js               # HTTP 服务、execute_code、原语暴露
    │   └── lib/primitives.js      # 挖掘、合成、熔炼、移动等
    ├── rag/                       # RAG 检索（可选）
    │   ├── retriever.py
    │   └── md_loader.py
    ├── data/
    │   ├── skill_db/              # 技能库持久化（ChromaDB，gitignore）
    │   ├── chroma_db/             # RAG 向量库（gitignore）
    │   ├── knowledge_base/       # 知识库 Markdown（gitignore）
    │   └── skill_db_export/      # 导出技能示例（可提交）
    ├── scripts/
    │   ├── export_skills.py      # 导出技能库
    │   └── remove_skill.py       # 按 name 删除技能
    └── logs/                      # 运行日志（gitignore）
```

---

## 核心能力

- **自然语言任务**：砍树、挖矿、合成、熔炼、移动、与实体互动；「来我身边/跟着我」等会识别为任务执行而非闲聊。
- **ReAct 闭环**：执行失败时将 `error` / `chat_log` 反馈给 LLM，多轮重试（默认最多 4 次），单次代码执行超时 5 分钟。
- **Critic 自验证**：任务完成后由 Critic 判定是否成功，通过则把本次代码抽象为技能写入技能库。
- **三路上下文**：技能检索、RAG 检索、记忆检索并发拉取，合并后传入代码生成 prompt。
- **断线重连**：observe 失败或连接异常时调用 `ensure_bot_connected()`，尝试重启 Node 并重连 Bot；日志中 [MC-KICK]/[MC-ERROR]/[MC-END] 便于排查。

---

## 已知问题与限制（现有问题）

以下为当前版本的已知问题，便于贡献者与使用者排错、排期改进。

### 1. 性能与延迟

- **LLM 调用链路过长**：单次任务会多次调用大模型（意图、复杂度、RAG 分类、代码生成、Critic、技能抽象），单轮延迟较高（数十秒到数分钟）。
- **RAG 内部串行**：`retriever.search()` 先做一次 LLM 分类再向量检索，RAG 路径本身耗时明显。
- **Critic / 技能抽象偶发超时**：90s 超时会导致当轮判定失败或重试异常。

### 2. 流程与设计缺口

- **无「先 RAG → 再技能 → 再模板」管线**：当前是技能 + RAG + 记忆一起拉取后全部塞入 context，没有「先 RAG 再按知识匹配技能、有技能则强约束按模板改写」的单独分支。
- **有技能时仍完整生成代码**：未实现「技能模板 + 参数填空」的轻量路径，检索到技能后仍是完整代码生成。
- **复杂任务拆解使用不足**：多数任务被判为 `simple`，很少走层级分解（`_run_hierarchical`）；多步任务（如「制作木斧」）多是一段长代码多次重试而非子任务序列。
- **子任务未技能化**：即便走层级执行，子任务完成后只更新 game_state，没有把子任务成功轨迹抽象成独立技能入库。

### 3. 合成与原语一致性

- **合成顺序依赖不稳固**：如「制作木斧」多次出现「合成 stick 需要工作台」等错误；RAG 或 prompt 对「先造工作台再合成」的约束不够强。
- **生成代码与运行环境不一致**：出现过 `bot.drop is not a function`、`pos.floored is not a function`、toss 用错物品等；原语/API 与 prompt 文档需进一步对齐，减少模型幻觉。

### 4. 技能库质量

- **同名覆盖**：同一技能名多次成功会覆盖，只保留最后一次 code/description，可能产生语义漂移。
- **异名同效**：不同名称但逻辑相似的技能会同时存在，检索时增加噪音。
- **Critic 通过但效果不佳**：个别任务 Critic 判成功但实际未达预期，错误技能被写入后可能持续被检索到。建议配合 `scripts/export_skills.py` 与 `scripts/remove_skill.py` 做人工复核与清理。

### 5. 其他

- **部分任务 4 次重试仍失败**：如「采集 10 个小麦种子」「把你的木斧扔给我」等，因 API 用法错误（equip、floored、toss/drop）多次未过。
- **系统/命令消息过滤**：以 `/` 开头的消息及传送类系统提示已过滤，不进入 Agent；用户执行命令后短时间内的下一条消息视为命令反馈并跳过，若误伤可调 `FEEDBACK_SKIP_SEC`。

更细的流程分析、时间分布与改进建议见 [docs/EVALUATION_2026-03-10.md](docs/EVALUATION_2026-03-10.md)；技能库质量评估见 [docs/skill_library_quality_evaluation.md](docs/skill_library_quality_evaluation.md)。

---

## 文档索引

| 文档 | 说明 |
|------|------|
| [EVALUATION_2026-03-10.md](docs/EVALUATION_2026-03-10.md) | 项目评估、任务流程、耗时分析、改进建议 |
| [PUSH_TO_GITHUB.md](docs/PUSH_TO_GITHUB.md) | 推送到 GitHub 的步骤与敏感信息自检 |
| [framework_and_test_guide.md](docs/framework_and_test_guide.md) | 框架分析与多场景测试指南 |
| [skill_library_quality_evaluation.md](docs/skill_library_quality_evaluation.md) | 技能库重复/质量评估方案 |
| [minecraft_agent_guide.md](docs/minecraft_agent_guide.md) | 调试、技能学习与升级方案 |
| [upgrade_v3_*.md](docs/) | 分层执行与代码架构评估 |

---

## 常见问题

**Q: Bot 加入游戏后立刻断开？**  
A: 将服务器 `server.properties` 中 `online-mode` 设为 `false`。

**Q: craft_item 报错说没有工作台？**  
A: 先让 Agent 执行放置工作台（如「放一个工作台」），或在地图中预先放一个；当前合成知识与顺序约束仍在改进中。

**Q: 如何开启自主探索？**  
A: 在游戏聊天中输入 `autonomous explore`，Agent 会在空闲约 60 秒后自动执行探索逻辑；可通过 `AUTONOMOUS_IDLE_SECONDS` 调整。

**Q: 如何导出或删除已学技能？**  
A: 使用 `python scripts/export_skills.py` 导出；使用 `scripts/remove_skill.py` 按 name 删除。详见 [skill_library_quality_evaluation.md](docs/skill_library_quality_evaluation.md)。

**Q: RAG 未生效？**  
A: 需安装并配置 `rag` 模块与 `KNOWLEDGE_BASE_PATH`、`CHROMA_DB_PATH`；若 RAG 不可用，主流程会降级运行并打 warning。

---

## 许可证

MIT
