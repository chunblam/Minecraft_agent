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
        │  统一分类 → 技能快速匹配(可选) → 三路 context   │
        │  → 代码生成(流式) → 执行 → Critic → 技能入库    │
        ▼                                                ▼
   双模型路由（小模型分类 / 大模型代码生成）        Minecraft 服务器
```

- **Python 侧**：**双模型路由**——小模型（硅基流动 Qwen2.5-7B）做意图/复杂度/可行性统一分类、Critic、技能抽象、RAG 分类；大模型（DeepSeek 官方 deepseek-chat）做代码生成、任务分解、闲聊。**统一分类**一次调用得到 intent / complexity / feasible，不可行则直接返回。**技能库快速匹配**：任务与技能库相似度 ≥ 0.88 时可直接参数替换执行，不调 LLM。三路上下文（技能 + RAG + 记忆）并发构建后传入代码生成；**首次代码生成走流式**，可在 MC 聊天栏提示「正在生成代码…」。Critic 通过后抽象为技能写入 ChromaDB。
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
# 编辑 .env：至少填写 DEEPSEEK_API_KEY（代码生成必选）；
# 可选 SILICONFLOW_API_KEY（不填则分类也走大模型）、MC_HOST、MC_PORT 等
```

### 4. 启动 Minecraft

- **单人**：进入世界 → 对局域网开放 → 记下端口（如 `25565` 或 `12345`）。
- **专用服**：在 `server.properties` 中设置 `online-mode=false`（Mineflayer 使用离线账号）。

### 5. 启动 Agent

```bash
# 从 minecraft_agent 目录执行；自动启动 mineflayer 并连接 MC
MC_PORT=25565 python main.py
```

启动后 Bot 会加入游戏。在聊天中对 Bot 说话（或 `/msg Agent 你好`）即可下发任务，例如：「帮我砍 5 个木头」「制作一把木斧」「来到我身边」。以 `/` 开头的消息会被视为系统/命令而忽略，不交给 Agent。

---

## 环境变量（.env）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MC_HOST` | `localhost` | Minecraft 服务器地址（WSL 下连 Windows 可用宿主机 IP） |
| `MC_PORT` | `25565` | 端口（局域网开放时改为实际端口） |
| `MC_USERNAME` | `Agent` | Bot 游戏内名称 |
| `MINEFLAYER_PORT` | `3000` | mineflayer HTTP 服务端口 |
| **小模型（分类 / Critic / 技能抽象）** | | |
| `SILICONFLOW_API_KEY` | - | 硅基流动 API Key；不填则 classify 回退到大模型 |
| `SILICONFLOW_BASE_URL` | `https://api.siliconflow.cn/v1` | 硅基流动 API 地址 |
| `FAST_MODEL` | `Qwen/Qwen2.5-7B-Instruct` | 小模型名 |
| **大模型（代码生成 / 任务分解 / 闲聊）** | | |
| `DEEPSEEK_API_KEY` | - | DeepSeek 官方 API Key（必填，用于代码生成） |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | DeepSeek API 地址 |
| `DEEPSEEK_V3_MODEL` | `deepseek-chat` | 大模型名（V3.2） |
| `DEEPSEEK_LLM_TIMEOUT` | `90` | 单次 LLM 调用超时（秒） |
| `CODE_TIMEOUT_MS` | `300000` | 单次代码执行超时（毫秒） |
| `MAX_TASK_RETRIES` | `4` | 单任务最大重试次数 |
| **RAG / 知识库（可选）** | | |
| `CHROMA_DB_PATH` | `./data/chroma_db` | RAG 向量库路径 |
| `KNOWLEDGE_BASE_PATH` | `./data/knowledge_base` | 知识库 Markdown 目录 |
| `RAG_CLASSIFY_MODEL` | `Qwen/Qwen2.5-7B-Instruct` | RAG 查询分类模型 |
| **行为 / 聊天** | | |
| `FEEDBACK_SKIP_SEC` | `2` | `/` 命令后同用户多少秒内的下一条消息视为命令反馈而忽略 |
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
│   ├── minecraft_agent_optimization.md  # 性能优化方案
│   ├── pre_test_checklist.md      # 测试前检查清单
│   ├── phase3_future.md           # 第三阶段与 DeepSeek 缓存说明
│   ├── framework_and_test_guide.md
│   ├── skill_library_quality_evaluation.md
│   ├── minecraft_agent_guide.md
│   └── ...
└── minecraft_agent/
    ├── main.py                    # 入口：连接 MC、聊天循环、重连、on_code_gen_progress 注入
    ├── requirements.txt
    ├── .env.example
    ├── agent/
    │   ├── react_agent.py         # VoyagerAgent：统一分类、技能快速匹配、_run_task、流式代码生成、技能入库
    │   ├── critic.py              # CriticAgent：规则快速判断 + LLM 任务成功自验证
    │   ├── planner.py             # TaskPlanner：层级任务分解
    │   ├── skill_library.py       # 技能检索与存储（ChromaDB）
    │   ├── llm_router.py          # 双模型路由、stream_think 流式接口
    │   ├── env.py                 # MineflayerEnv、execute_code、send_action、ensure_bot_connected
    │   ├── prompts.py             # 系统/用户 prompt（含 UNIFIED_CLASSIFY_PROMPT）
    │   ├── memory.py              # 短期/长期记忆
    │   ├── personality.py         # 情绪与关系
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
    │   ├── knowledge_base/        # 知识库 Markdown（gitignore）
    │   └── skill_db_export/       # 导出技能示例（可提交）
    ├── scripts/
    │   ├── export_skills.py       # 导出技能库为 .js + .json
    │   └── remove_skill.py        # 按 name 删除技能
    └── logs/                      # 运行日志（gitignore）
```

---

## 核心能力

- **自然语言任务**：砍树、挖矿、合成、熔炼、移动、与实体互动；「来我身边/跟着我」等会识别为任务执行而非闲聊。
- **双模型路由**：分类、Critic、技能抽象、RAG 分类走小模型（低延迟、低成本）；代码生成、任务分解、闲聊走大模型。
- **统一分类与可行性**：一次 LLM 调用得到 intent / complexity / feasible；不可行（如需 OP）时直接提示，不进入执行。
- **技能库快速匹配**：任务与技能库 top-1 相似度 ≥ 0.88 时，对技能代码做数量等参数替换后直接执行，不调 LLM；否则走完整 CodeLoop。
- **流式代码生成**：首次代码生成使用流式接口，首 token 约 1–2s 内可在 MC 聊天栏显示「正在生成代码…」。
- **ReAct 闭环**：执行失败时将 error / chat_log 反馈给 LLM，多轮重试（默认最多 4 次），差异化重试 prompt（第 2 次换思路、第 3 次更保守）。
- **Critic 自验证**：规则层快速判断（如「获取 N 个 X」对照背包）+ LLM 判定；通过则把本次代码抽象为技能写入技能库。
- **三路上下文**：技能检索、RAG 检索、记忆检索并发拉取，合并后传入代码生成 prompt。
- **/ 命令与反馈过滤**：以 `/` 开头的消息不交给 Agent；用户执行命令后 `FEEDBACK_SKIP_SEC`（默认 2s）内同用户下一条视为命令反馈并跳过。
- **断线重连**：observe 失败或连接异常时调用 `ensure_bot_connected()`，尝试重启 Node 并重连 Bot；日志中 [MC-KICK]/[MC-ERROR]/[MC-END] 便于排查。

---

## 已知问题与限制

以下为当前版本的已知问题，便于贡献者与使用者排错、排期改进。

### 1. 性能与延迟

- **代码生成仍为最大耗时**：分层模型与技能快速匹配已减轻分类与重复任务延迟；首次冷启动代码生成仍约十秒级，流式可改善主观等待体验。
- **RAG 内部**：`retriever.search()` 先做 LLM 分类再向量检索，RAG 路径本身有耗时。
- **Critic / 技能抽象偶发超时**：90s 超时会导致当轮判定失败或重试异常。

### 2. 流程与设计

- **复杂任务拆解使用不足**：多数任务被判为 simple，多步任务（如「制作木斧」）常为一段长代码多次重试；已通过统一分类与 complexity 规则（结合背包）改进「造木斧」类判 complex。
- **子任务未技能化**：层级执行子任务完成后只更新 game_state，未把子任务成功轨迹抽象成独立技能入库。

### 3. 合成与原语一致性

- **合成顺序依赖不稳固**：如「制作木斧」多次出现「合成 stick 需要工作台」等错误；RAG 或 prompt 对「先造工作台再合成」的约束不够强。
- **生成代码与运行环境不一致**：出现过 `bot.drop is not a function`、`pos.floored is not a function`、toss 用错物品等；原语/API 与 prompt 文档需进一步对齐。

### 4. 技能库质量

- **同名覆盖**：同一技能名多次成功会覆盖，只保留最后一次 code/description。
- **异名同效**：不同名称但逻辑相似的技能会同时存在，检索时增加噪音。
- **Critic 通过但效果不佳**：个别任务 Critic 判成功但实际未达预期。建议配合 `scripts/export_skills.py` 与 `scripts/remove_skill.py` 做人工复核与清理。

### 5. 其他

- **部分任务多次重试仍失败**：因 API 用法错误（equip、floored、toss/drop）等。
- **DeepSeek 上下文缓存**：请求顺序为 [system, user]，DeepSeek 官方「上下文硬盘缓存」默认开启，system 段可缓存命中以降本加速；详见 [docs/phase3_future.md](docs/phase3_future.md)。

更细的流程分析、耗时与改进建议见 [docs/minecraft_agent_optimization.md](docs/minecraft_agent_optimization.md)、[docs/EVALUATION_2026-03-10.md](docs/EVALUATION_2026-03-10.md)；技能库质量评估见 [docs/skill_library_quality_evaluation.md](docs/skill_library_quality_evaluation.md)。

---

## 文档索引

| 文档 | 说明 |
|------|------|
| [minecraft_agent_optimization.md](docs/minecraft_agent_optimization.md) | 性能优化综合方案（双模型、合并分类、流式、Critic 规则等） |
| [pre_test_checklist.md](docs/pre_test_checklist.md) | 测试前检查清单与实施对照 |
| [phase3_future.md](docs/phase3_future.md) | 第三阶段说明、DeepSeek 上下文缓存 |
| [EVALUATION_2026-03-10.md](docs/EVALUATION_2026-03-10.md) | 项目评估、任务流程、耗时分析 |
| [framework_and_test_guide.md](docs/framework_and_test_guide.md) | 框架分析与多场景测试指南 |
| [skill_library_quality_evaluation.md](docs/skill_library_quality_evaluation.md) | 技能库重复/质量评估方案 |
| [minecraft_agent_guide.md](docs/minecraft_agent_guide.md) | 调试、技能学习与升级方案 |

---

## 常见问题

**Q: Bot 加入游戏后立刻断开？**  
A: 将服务器 `server.properties` 中 `online-mode` 设为 `false`。

**Q: craft_item 报错说没有工作台？**  
A: 先让 Agent 执行放置工作台（如「放一个工作台」），或在地图中预先放一个；当前合成知识与顺序约束仍在改进中。

**Q: 如何开启自主探索？**  
A: 在游戏聊天中输入 `autonomous explore`，Agent 会在空闲约 60 秒后自动执行探索逻辑；可通过 `AUTONOMOUS_IDLE_SECONDS` 调整。

**Q: 如何导出或删除已学技能？**  
A: 使用 `python scripts/export_skills.py` 导出为 .js + .json；使用 `python scripts/remove_skill.py --list` 列名称、`python scripts/remove_skill.py "技能名"` 删除。详见 [skill_library_quality_evaluation.md](docs/skill_library_quality_evaluation.md)。

**Q: 不配置 SILICONFLOW_API_KEY 可以吗？**  
A: 可以。未配置时 classify（意图/复杂度/Critic/技能抽象）会回退到大模型（DEEPSEEK_API_KEY），功能正常，仅延迟与成本略高。

**Q: RAG 未生效？**  
A: 需安装并配置 `rag` 模块与 `KNOWLEDGE_BASE_PATH`、`CHROMA_DB_PATH`；若 RAG 不可用，主流程会降级运行并打 warning。

**Q: 以 `/` 开头的消息会被处理吗？**  
A: 不会。以 `/` 开头的消息视为系统/命令而忽略；同一用户执行命令后 `FEEDBACK_SKIP_SEC`（默认 2 秒）内的下一条消息也会被当作命令反馈而跳过。

---

## 许可证

MIT
