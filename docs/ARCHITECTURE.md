# Minecraft AI Agent 架构与运行逻辑

本文档详细描述项目的整体架构、数据流、理想运行逻辑及各模块职责。

---

## 一、系统架构总览

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                            Minecraft AI Agent 系统                                │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│   ┌──────────────────────┐         WebSocket          ┌──────────────────────┐  │
│   │   Python Agent       │    ws://localhost:8765      │   Fabric Mod         │  │
│   │   (minecraft_agent/) │  ◄──────────────────────►  │   (mc_fabric_mod/)   │  │
│   └──────────┬───────────┘                             └──────────┬───────────┘  │
│              │                                                    │              │
│              │  main.py                                            │              │
│              │  - 消息路由                                          │  ServerEventHandler
│              │  - 意图识别 → run/chat                               │  - 玩家聊天 → game_state
│              │  - game_state_update → 自主探索                      │  - 每60s push game_state
│              │                                                    │              │
│   ┌──────────▼───────────┐                             ┌──────────▼───────────┐  │
│   │  ReactAgent         │                             │  ActionExecutor      │  │
│   │  - Plan-then-Execute│   action + observation      │  - move_to/mine_block│  │
│   │  - 技能库检索        │  ◄────────────────────────► │    → ClientInputSim  │  │
│   │  - 技能学习          │                             │  - 其余 → 即时执行   │  │
│   └─────────────────────┘                             └──────────────────────┘  │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## 二、消息协议（Mod ↔ Python）

| 方向 | type | 说明 |
|------|------|------|
| Mod → Python | `player_chat` | 玩家在聊天框发言，附带完整 game_state |
| Mod → Python | `game_state_update` | 每 60 秒周期性推送，触发自主探索 |
| Mod → Python | `observation` | 行动执行完成，附带 success、observation、game_state_update |
| Python → Mod | `action` | 执行游戏行动（request_id、action_type、action_params） |
| Python → Mod | `final_response` | 最终回复，显示给玩家 |

---

## 三、理想运行逻辑

### 3.1 玩家发言流程（player_chat）

```
玩家在聊天框输入「帮我砍 5 个木头」
        │
        ▼
Mod: ServerEventHandler 捕获 CHAT_MESSAGE
        │ 构建 game_state（position、inventory、nearby_resources、horizon_scan 等）
        │ 发送 { type: "player_chat", player_message, game_state }
        ▼
Python: main.handle_connection 收到消息
        │ asyncio.create_task(_process_player_message)
        │ explorer.notify_player_active()  # 玩家活跃，自主探索让步
        ▼
Python: agent.classify_intent(player_message)
        │ LLM 判断 → task_execution / knowledge_qa / chat
        ▼
┌───────┴───────┐
│ task_execution│
└───────┬───────┘
        │
        ▼
agent.run(game_state, player_message)
        │
        ├─ Phase 0: skill_lib.search_skill() → 检索参数化技能
        │   ├─ 命中且相关度 ≥ 0.70 → execute_skill_template() 直接执行
        │   └─ 未命中 → Phase 1
        │
        ├─ Phase 1: _generate_action_plan() → V3 一次性生成 JSON 计划
        │   ├─ 成功 → Phase 2
        │   └─ 失败 → _run_step_by_step() 逐步 ReAct
        │
        ├─ Phase 2: _execute_plan() 逐步执行
        │   │ 每步: _execute_action() → connection_manager.send_action()
        │   │       → Mod 执行 → observation 回传 → game_state.update()
        │   └─ 失败 → _replan() 局部重规划
        │
        └─ 成功 → _post_process_success() 后台抽象技能入库
        │
        ▼
connection_manager.send_final_response(display_message)
        │
        ▼
Mod 广播给玩家：「砍好啦！共砍了 5 个橡木原木～」
```

### 3.2 闲聊/知识问答流程（chat / knowledge_qa）

```
玩家输入「矿洞里绿色的生物是什么」
        │
        ▼
classify_intent → knowledge_qa 或 chat
        │
        ▼
agent.chat(game_state, player_message)
        │
        ├─ asyncio.gather(rag.search(), memory.get_relevant_context())  # 并行
        ├─ personality.get_chat_system_prompt(player_name)
        └─ llm.classify() → 回复
        │
        ▼
send_final_response → 玩家看到回复
```

### 3.3 自主探索流程（game_state_update）

```
Mod: ServerTickEvents 每 1200 tick（60秒）触发
        │ 构建 game_state（同 player_chat）
        │ 发送 { type: "game_state_update", game_state }
        ▼
Python: explorer.on_game_state_update(game_state)
        │
        ├─ 检查：_is_running? 冷却期? 夜晚危险?
        ├─ 通过 → _propose_next_task()
        │   │ LLM 根据 nearby_resources、已知技能、背包 → 提出任务
        │   └─ 例：「收集 3 个橡木原木」
        │
        └─ _run_exploration(task, game_state)
            │ agent.run(game_state, task)  # 同玩家任务流程
            └─ 成功 → _post_process_success 自动完成技能学习
```

### 3.4 行动执行流程（move_to / mine_block）

```
Python: action_type="move_to", action_params={x,y,z}
        │
        ▼
connection_manager.send_action() → 发送 action 消息，等待 Future
        │
        ▼
Mod: AgentWebSocketClient 收到 action
        │ server.execute() → ActionExecutor.executeWithState()
        │
        ├─ move_to / mine_block:
        │   ├─ AgentNetworking.sendInputToClient() 检查客户端是否支持
        │   │   ├─ 支持 → 发送 AgentInputPayload 到客户端
        │   │   │         ClientInputSimulator 接收
        │   │   │         - move_to: SimulatedInput 模拟 W 键 + 自动跳跃 + 平滑视角
        │   │   │         - mine_block: updateBlockBreakingProgress 模拟挖掘
        │   │   │         完成 → AgentActionCompletePayload 回传
        │   │   └─ 不支持 → PlayerTaskRunner 服务端直接操控（setPosition/breakBlock）
        │   │
        │   └─ 完成 → 发送 observation 消息（含 game_state_update）
        │
        └─ 其余行动: execute() 即时执行
            craft_item / enchant_item / find_resource / scan_area 等
        │
        ▼
Python: resolve_observation() 解除 Future
        │ game_state.update(state_update)
        ▼
下一步 LLM 推理能感知最新状态
```

---

## 四、模块职责

### 4.1 Python 端

| 模块 | 文件 | 职责 |
|------|------|------|
| 入口 | main.py | WebSocket 服务、消息路由、player_chat / game_state_update / observation 分发 |
| 核心 | react_agent.py | 意图识别、Plan-then-Execute、ReAct 回退、技能学习、上下文构建 |
| 规划 | planner.py | 任务分解（decompose）、子任务跳过条件检查 |
| 技能 | skill_library.py | 技能检索、抽象（simple/hierarchical/parameterized）、ChromaDB 存储 |
| 执行 | skill_executor.py | 参数化技能模板解析、find_resource + for_each 流程执行 |
| 连接 | connection_manager.py | 单例、send_action / resolve_observation、Future 等待 |
| 记忆 | memory.py | 短期滑动窗口、长期 ChromaDB、相关上下文检索 |
| 人格 | personality.py | 情绪状态、好感度、聊天回复风格 |
| 探索 | autonomous_explorer.py | Voyager-style 自主探索、课程提议、冷却机制 |
| LLM | llm_router.py | 统一 V3 调用（think_fast、classify） |
| RAG | rag/retriever.py | EmbeddingClient（LRU 缓存）、QueryClassifier、多 collection 检索 |

### 4.2 Java 端

| 模块 | 文件 | 职责 |
|------|------|------|
| 入口 | MinecraftAgentMod.java | 初始化、WebSocket 连接、定时 game_state_update 推送 |
| 客户端 | MinecraftAgentModClient.java | ClientInputSimulator 注册 |
| 网络 | AgentWebSocketClient.java | Mod → Python 连接、消息接收、action 分发 |
| 网络 | AgentNetworking.java | Payload 注册、sendInputToClient、PendingCompletion 回调 |
| 网络 | AgentInputPayload / AgentActionCompletePayload | 自定义网络包定义 |
| 执行 | ActionExecutor.java | 行动执行入口、move_to/mine_block 异步、其余即时 |
| 执行 | PlayerTaskRunner.java | 服务端回退：MoveToTask、MineBlockTask（tick 级） |
| 输入 | ClientInputSimulator.java | 客户端：接收 AgentInputPayload、模拟 WASD+跳跃+挖掘 |
| 输入 | SimulatedInput.java | 自定义 Input，setForward/setJump/stop |
| 事件 | ServerEventHandler.java | 玩家聊天、buildGameState |
| 工具 | WorldScanner.java | horizon_scan 8 方向扫描 |

---

## 五、game_state 字段说明

| 字段 | 说明 |
|------|------|
| player_name | 玩家名 |
| health / hunger | 血量 / 饥饿值 |
| xp_level | 经验等级 |
| dimension | 当前维度 |
| time | 游戏时间（0–24000） |
| position | 玩家坐标 {x,y,z} |
| inventory | 背包 41 格非空物品 |
| nearby_blocks | 7×7×5 周围方块（规划时已裁剪） |
| nearby_resources | 24 格内资源（ores/logs/water/crafting 等） |
| nearby_entities | 20 格内生物 |
| environment | 深度、光照、生物群系 |
| horizon_scan | 8 方向 × 48/96/192 格地形感知 |

---

## 六、技能类型与复用

| 类型 | 说明 | 复用方式 |
|------|------|----------|
| simple | 单步轨迹抽象 | 步骤提示，供 LLM 参考 |
| hierarchical | 多子任务 | 子任务列表 + 跳过条件 |
| parameterized | 参数化可执行 | 运行时解析 block_type、count，find_resource → for_each 执行 |

---

## 七、参考

- [Voyager 论文](https://voyager.minedojo.org/)
- [战斗与自主探索扩展](COMBAT_AND_AUTONOMY.md)
- [Agent 重置指南](RESET_AGENT.md)
- [项目评估](PROJECT_EVALUATION.md)
