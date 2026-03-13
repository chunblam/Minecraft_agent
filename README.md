# Minecraft AI Agent

基于 LLM 的 Minecraft 游戏助手，参考 [Voyager](https://voyager.minedojo.org/) 设计。使用 **Mineflayer（Node.js）** 作为底层控制，Bot 以独立玩家身份加入游戏，**无需安装 Fabric Mod**。支持自然语言指令、ReAct 式代码生成与重试、Critic 自验证、技能库沉淀、RAG 知识检索、**游戏状态实时刷新**与**世界状态记忆**（如「附近已有箱子/工作台」）。

---

## 架构概览

```
┌─────────────────────┐         HTTP          ┌─────────────────────┐
│  Python Agent       │  ◄─────────────────►  │  Mineflayer         │
│  VoyagerAgent       │   localhost:3000      │  Node.js Server     │
│  Critic / Planner   │   execute_code        │  pathfinder / 原语  │
│  技能库 / RAG / 记忆 │   observe（状态刷新） │  buildObservation   │
└─────────────────────┘                       └─────────────────────┘
        │                                                │
        │  用前刷新 game_state → 统一分类 → 技能快匹配   │
        │  → 三路 context（技能+RAG+记忆）→ 代码生成     │
        │  → 执行 → game_state 更新 → Critic → 世界状态记忆/技能入库
        ▼                                                ▼
   双模型路由（小模型 25s 超时 / 大模型流式 90s）    Minecraft 服务器
```

- **Python 侧**：**双模型路由**——小模型（硅基流动 Qwen2.5-7B，`CLASSIFY_TIMEOUT=25`）做意图/复杂度/可行性统一分类、Critic、技能抽象、RAG 分类；大模型（DeepSeek deepseek-chat）做代码生成（流式，`STREAM_THINK_TIMEOUT=90`）、任务分解、闲聊。**游戏状态**在每次关键决策前通过 `_refresh_game_state()` 或 main 的 `observe()` 拉取最新（背包、附近方块、位置等）；执行后由 `execute_code` 返回的 `game_state` 写回。**记忆**除对话外，会写入任务开始/结束与**世界状态事实**（如「附近已放置箱子/工作台/熔炉」），供后续「存东西进箱子」等任务检索。**技能库快速匹配**：相似度 ≥ 0.88 时可直接参数替换执行；否则三路上下文并发构建后代码生成，首次走流式并在 MC 聊天栏提示「正在生成代码…」。Critic 通过后写入世界状态记忆并抽象技能入库。
- **Node 侧**：Mineflayer 提供寻路、挖掘、合成、熔炼、装备、空手（`equipItem("air")`）等原语；`/observe` 与 `execute_code` 返回的 `buildObservation` 含 `nearby_blocks`（约 24 格半径）。Bot 断线时 Python 可 `ensure_bot_connected` 重启 Node 并重连。

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
| `CLASSIFY_TIMEOUT` | `25` | 小模型分类/意图/复杂度/Critic 单次超时（秒） |
| `DEEPSEEK_LLM_TIMEOUT` | `90` | 大模型单次非流式调用超时（秒） |
| `STREAM_THINK_TIMEOUT` | `90` | 流式代码生成整段（请求+消费）超时（秒） |
| `CODE_TIMEOUT_MS` | `300000` | 单次代码执行超时（毫秒） |
| `MAX_TASK_RETRIES` | `4` | 单任务最大重试次数 |
| **RAG / 知识库（可选）** | | |
| `CHROMA_DB_PATH` | `./data/chroma_db` | RAG 向量库路径 |
| `KNOWLEDGE_BASE_PATH` | `./data/knowledge_base` | 知识库 Markdown 目录 |
| `RAG_CLASSIFY_MODEL` | `Qwen/Qwen2.5-7B-Instruct` | RAG 查询分类模型 |
| **行为 / 聊天 / 自主** | | |
| `FEEDBACK_SKIP_SEC` | `5` | `/` 命令后同用户多少秒内的下一条消息视为命令反馈而忽略 |
| `AUTONOMOUS_FIRST_DELAY_SECONDS` | `15` | 开启自主探索后，首次触发任务前的等待秒数 |
| `AUTONOMOUS_IDLE_SECONDS` | `15` | 上次自主任务完成后，到下一次任务前的空闲秒数（聊天输入「autonomous」开启） |

更多项见 `minecraft_agent/.env.example`。

---

## 项目结构

```
Minecraft_agent/
├── README.md
├── .gitignore
└── minecraft_agent/
    ├── main.py                    # 入口：连接 MC、聊天循环、observe 刷新、自主探索、重连
    ├── requirements.txt
    ├── .env.example / .env
    ├── agent/
    │   ├── react_agent.py         # VoyagerAgent：_refresh_game_state、统一分类、技能快匹配、_run_task、流式代码生成、世界状态记忆、技能入库
    │   ├── critic.py              # CriticAgent：规则快速判断 + LLM 任务成功自验证
    │   ├── planner.py             # TaskPlanner：层级任务分解（complex 时）
    │   ├── skill_library.py       # 技能检索与存储（ChromaDB）
    │   ├── llm_router.py          # 双模型路由、classify(25s)、stream_think(90s)
    │   ├── env.py                 # MineflayerEnv、execute_code、observe、ensure_bot_connected
    │   ├── prompts.py             # 代码生成 / 分类 / Critic / 自主任务生成 prompt（含 nearby_blocks、chest 优先规则）
    │   ├── memory.py              # 短期滑动窗口 + 长期 ChromaDB；对话、任务、世界状态
    │   ├── trajectory_logger.py   # 任务轨迹落盘（含 prompt/代码/ game_state）
    │   └── personality.py         # 情绪与关系（可选）
    ├── mineflayer/
    │   ├── index.js               # HTTP：/start、/execute_code、/observe、buildObservation
    │   └── lib/primitives.js      # 挖掘、合成、熔炼、移动、equipItem（含 air 空手）
    ├── rag/                       # RAG 检索（可选）
    │   ├── retriever.py
    │   └── md_loader.py
    ├── data/
    │   ├── skill_db/              # 技能库持久化（ChromaDB）
    │   ├── chroma_db/             # RAG + 长期记忆向量库
    │   ├── knowledge_base/        # 知识库 Markdown（含 guide）
    │   ├── autonomous_curriculum.json  # 自主探索已完成/失败任务
    │   ├── task_trajectories/     # 按任务与 attempt 保存的轨迹（code、prompt、game_state）
    │   └── skill_db_export/       # 导出技能 .js + .json
    ├── scripts/
    │   ├── export_skills.py       # 导出技能库
    │   ├── remove_skill.py        # 按 name 删除技能
    │   └── validate_and_remove_invalid_skills.py
    └── logs/                      # 运行日志
```

---

## Agent 行为与多场景评估

本节从**多种入口与场景**对 Agent 行为做综合评估，便于理解何时走哪条路径、状态如何更新、记忆与技能如何参与。

### 入口与分流

| 场景 | 触发条件 | 行为概要 |
|------|----------|----------|
| **用户自然语言任务** | 主循环检测到玩家聊天（非 `/`、非系统消息、非命令反馈窗口内） | 先 `observe()` 刷新 game_state → `agent.run(game_state, msg)` → 记忆写入 user → **用前刷新** game_state → 统一分类 |
| **自主探索** | 聊天输入 `autonomous` 开启后，空闲 15s（首次 15s / 上次任务完成后 15s）且无运行中任务 | `observe()` 刷新 → `run_autonomous(game_state)` → 内部再刷新 → 任务生成器按 game_state + 技能库 + 记忆 + 课程生成下一任务 → `_run_task` |
| **中断** | 聊天输入 `stop` 或 `autonomous stop` | 取消当前任务或关闭自主探索 |

### 分类与路径

- **统一分类**（小模型，25s 超时）：一次调用得到 `intent`（task_execution / chat / knowledge_qa）、`complexity`（simple / complex）、`feasible`。不可行则直接返回提示，不进入执行。
- **task_execution + simple**：单任务路径 `_run_task(player_message, game_state)`。
- **task_execution + complex**：层级路径 `_run_hierarchical`：先刷新 game_state → Planner 分解子任务 → 依次执行子任务，每完成一子任务用 `observe()` 更新 game_state，再执行下一子任务。

### 单任务内部（_run_task）

1. **用前刷新**：`_refresh_game_state(game_state)`（节流 1s）。
2. **技能快速匹配**：检索技能库 top-1，相似度 ≥ 0.88 则参数替换后直接 `execute_code`，通过 Critic 即返回，不调代码生成 LLM。
3. **CodeLoop**：三路 context（技能 + RAG + 记忆）并发 → 构建 code prompt（含**当前** game_state：position、inventory、nearby_blocks、equipment 等）→ 首次流式 `stream_think`（90s 总超时），重试用非流式 `think` → 提取 JS 代码 → `execute_code`（注入检索到的技能函数）→ 用返回的 `game_state` 更新本地 → Critic 判定 → 通过则写入**世界状态记忆**（附近 chest/crafting_table/furnace）并后台存储技能，失败则带入 critique 重试（最多 4 次）。

### 游戏状态与记忆

- **实时性**：用户任务前 main 做一次 `observe()`；`run()` / `run_autonomous()` / `_run_task()` / `_run_hierarchical()` 入口处会 `_refresh_game_state()`；每次 `execute_code` 返回也会用其 `game_state` 更新。因此代码生成与 Critic 使用的都是**近期**状态（背包、附近方块、手持等）。
- **记忆内容**：短期记忆包含对话（user/agent）、**任务开始/结束**（system）、**世界状态**（system，如「附近已放置箱子，可用于存放或取出」）；满时压缩到长期 ChromaDB。检索时与「相关历史记忆」一起注入代码生成与任务生成，避免重复造箱子等。

### 超时与重试

- **classify**：25s（`CLASSIFY_TIMEOUT`），超时返回空，走默认 task_execution/simple。
- **stream_think**：整段请求+流式消费 90s（`STREAM_THINK_TIMEOUT`），超时返回已接收内容。
- **单任务重试**：最多 4 次（`MAX_TASK_RETRIES`），每次带入上一轮 execution error 与 Critic 的 critique；第 2 次要求换思路，第 3 次要求更保守。

### 评估小结

- **强项**：双模型分工明确；技能快匹配减少重复生成；游戏状态用前刷新 + 世界状态记忆减少「已有箱子仍去造」类错误；流式代码生成改善等待体验；Critic + 规则层保证只有验证通过才入库。
- **可改进点**：层级子任务未单独技能化；合成顺序与 API 一致性仍依赖 prompt 与原语文档；技能库同名覆盖与异名同效需人工复核与脚本清理。

---

## 核心能力

- **自然语言任务**：砍树、挖矿、合成、熔炼、移动、与实体互动、存/取箱子；「来我身边/跟着我」等会识别为任务执行而非闲聊。
- **游戏状态实时更新**：用户任务前、自主任务前、每次 `_run_task`/层级分解前及 `execute_code` 返回后都会更新 game_state（背包、附近方块、位置等）；代码生成与 Critic 始终基于近期状态。附近已有 chest/crafting_table/furnace 时，prompt 明确要求优先使用而非再合成/放置。
- **世界状态记忆**：任务成功后写入「附近已放置箱子/工作台/熔炉」等 system 事件，与对话、任务开始/结束一起参与检索，后续如「把东西放进箱子」会优先利用已有箱子。
- **双模型路由**：分类、Critic、技能抽象、RAG 分类走小模型（25s 超时）；代码生成、任务分解、闲聊走大模型（流式 90s 总超时）。
- **统一分类与可行性**：一次 LLM 调用得到 intent / complexity / feasible；不可行时直接提示，不进入执行。
- **技能库快速匹配**：任务与技能库 top-1 相似度 ≥ 0.88 时，参数替换后直接执行并走 Critic，不调代码生成 LLM；否则走完整 CodeLoop。
- **流式代码生成**：首次代码生成走流式，可在 MC 聊天栏显示「正在生成代码…」；整段流式请求受 `STREAM_THINK_TIMEOUT` 限制。
- **ReAct 闭环**：执行失败时将 error / chat_log 反馈给 LLM，最多 4 次重试，第 2 次换思路、第 3 次更保守。
- **Critic 自验证**：规则层（如「N 个 X」对照背包、nearby_blocks）+ LLM 判定；通过则写世界状态记忆并抽象技能入库。
- **三路上下文**：技能、RAG、记忆（含近期对话与状态）并发拉取，合并后传入代码生成 prompt。
- **/ 命令与反馈过滤**：以 `/` 开头的消息不交给 Agent；命令后 `FEEDBACK_SKIP_SEC`（默认 5s）内同用户下一条视为命令反馈并跳过。
- **自主探索**：聊天输入 `autonomous` 开启、`autonomous stop` 关闭；空闲 15s 后按游戏状态与课程生成下一任务并执行，任务间隔 15s。
- **断线重连**：observe 失败或连接异常时调用 `ensure_bot_connected()`，尝试重启 Node 并重连 Bot；日志中 [MC-KICK]/[MC-ERROR]/[MC-END] 便于排查。

---

## 已知问题与限制

以下为当前版本的已知问题，便于贡献者与使用者排错、排期改进。

### 1. 性能与延迟

- **代码生成仍为最大耗时**：技能快速匹配与分类 25s 超时已减轻小模型侧卡顿；首次代码生成仍约十秒级，流式可改善主观等待；流式整段 90s 超时避免无限挂起。
- **RAG 内部**：`retriever.search()` 先做 LLM 分类再向量检索，RAG 路径有额外耗时。
- **Critic / 技能抽象**：使用小模型 25s 超时，偶发超时会导致当轮判定失败或重试。

### 2. 流程与设计

- **复杂任务**：complex 时走层级分解；多数任务仍判 simple，多步任务有时为一段长代码多次重试。
- **子任务未技能化**：层级子任务完成后只更新 game_state，未把子任务成功轨迹单独抽象成技能入库。

### 3. 合成与原语一致性

- **合成顺序**：如「制作木斧」仍可能出现「合成 stick 需要工作台」等顺序问题；prompt 已强调 nearby_blocks 有工作台时直接使用。
- **API 一致性**：原语与 prompt 已对齐 `equipItem("air")` 空手、placeItem 坐标等；若出现未列出的 API（如 `pos.floored`）需在 prompt 中明确禁止。

### 4. 技能库质量

- **同名覆盖**：同一技能名多次成功会覆盖，只保留最后一次。
- **异名同效**：逻辑相似的技能可能异名存在，检索时增加噪音。建议用 `scripts/export_skills.py` 与 `scripts/remove_skill.py`、`validate_and_remove_invalid_skills.py` 做人工复核与清理。
- **Critic 通过但效果不佳**：个别任务判成功但实际未达预期，可结合轨迹日志排查。

### 5. 其他

- **DeepSeek 上下文缓存**：请求顺序为 [system, user]，DeepSeek 官方上下文缓存对 system 段可命中以降本加速。

---

## 常见问题

**Q: Bot 加入游戏后立刻断开？**  
A: 将服务器 `server.properties` 中 `online-mode` 设为 `false`。

**Q: craft_item 报错说没有工作台？**  
A: 先让 Agent 执行放置工作台（如「放一个工作台」），或在地图中预先放一个；当前合成知识与顺序约束仍在改进中。

**Q: 如何开启自主探索？**  
A: 在游戏聊天中输入 `autonomous` 开启；输入 `autonomous stop` 关闭。开启后首次约 15 秒、之后每次任务完成间隔 15 秒会自动生成并执行下一任务；可通过 `AUTONOMOUS_FIRST_DELAY_SECONDS`、`AUTONOMOUS_IDLE_SECONDS` 调整。

**Q: 如何导出或删除已学技能？**  
A: 使用 `python scripts/export_skills.py` 导出为 .js + .json；使用 `python scripts/remove_skill.py --list` 列名称、`python scripts/remove_skill.py "技能名"` 删除；可用 `scripts/validate_and_remove_invalid_skills.py` 校验并清理无效技能。

**Q: 不配置 SILICONFLOW_API_KEY 可以吗？**  
A: 可以。未配置时 classify（意图/复杂度/Critic/技能抽象）会回退到大模型（DEEPSEEK_API_KEY），功能正常，仅延迟与成本略高。

**Q: RAG 未生效？**  
A: 需安装并配置 `rag` 模块与 `KNOWLEDGE_BASE_PATH`、`CHROMA_DB_PATH`；若 RAG 不可用，主流程会降级运行并打 warning。

**Q: 以 `/` 开头的消息会被处理吗？**  
A: 不会。以 `/` 开头的消息视为系统/命令而忽略；同一用户执行命令后 `FEEDBACK_SKIP_SEC`（默认 5 秒）内的下一条消息也会被当作命令反馈而跳过。

**Q: 为什么有时让「把东西放进箱子」还会去造新箱子？**  
A: 已通过「用前刷新 game_state」与「世界状态记忆」改进：每次任务前会拉取最新 nearby_blocks，任务成功后会写入「附近已有箱子/工作台/熔炉」；代码生成规则也要求若 nearby_blocks 已有 chest 则优先寻路打开再存物。若 Bot 被传送离箱子过远，nearby_blocks 可能不包含该箱子，此时会依赖记忆中的「已放置箱子」提示；可确认日志中是否有 `[State] 已刷新 game_state` 与记忆中的世界状态条目。

---

## 许可证

MIT
