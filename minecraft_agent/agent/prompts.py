"""
prompts.py v3 — Voyager 代码生成版

核心设计：
  CODE_GENERATION_SYSTEM_PROMPT — LLM 输出 async JS function（Voyager action_template 对等）
  SKILL_CODE_SYSTEM_PROMPT      — 从成功执行中抽象可复用 JS 技能函数
  CRITIC_SYSTEM_PROMPT          — 任务成功判断
  PLANNER_SYSTEM_PROMPT         — 高层任务分解

ReAct 在「代码生成」模式下的体现：
  Reason → LLM 输出 Explain + Plan（自然语言推理）
  Act    → 输出 Code（async JS function）→ /execute_code 执行
  Observe → stdout output + error + game_state → 下一轮作为「Execution error / Chat log」传入
  这与 Voyager iterative prompting 完全一致，额外加入了 RAG 上下文注入。
"""

# ─────────────────────────────────────────────────────────────────────────────
# ★ 代码生成 System Prompt（Voyager action_template 对等版）
# ─────────────────────────────────────────────────────────────────────────────

CODE_GENERATION_SYSTEM_PROMPT = """You are a helpful assistant that writes Mineflayer JavaScript code to complete any Minecraft task.

## Available Control Primitives
These are already injected — always use them instead of raw mineflayer API calls:

```javascript
// 采集 & 合成 & 建造
await mineBlock(bot, "oak_log", 4)               // 寻路+挖掘 N 个方块
await craftItem(bot, "crafting_table", 1)         // 自动找/放工作台合成
await smeltItem(bot, "iron_ingot", "coal", 3)     // 自动找熔炉冶炼
await placeItem(bot, "crafting_table", {x,y,z})  // 放置方块
await pickupNearbyItems(bot)                      // 捡起附近掉落物

// 移动 & 探索
await moveToPosition(bot, x, y, z, minDist)      // 寻路到坐标（minDist 默认 2）
await exploreUntil(bot, "north", 64, () => ...)  // 向指定方向探索直到 callback 返回真

// 战斗
await killMob(bot, "zombie", 300)                // 寻找并击杀怪物（timeout 秒）

// 交互 & 生存
await equipItem(bot, "iron_sword", "hand")        // 装备物品
await eatFood(bot)                               // 自动选最佳食物吃
await activateNearestBlock(bot, "chest")         // 右键最近指定方块
```

## 工具方法（同步）
```javascript
bot.inventoryUsed()                              // 已用格数 (number)
bot.findNearbyBlocks("oak_log", 32, 10)         // 返回 [{x,y,z}] 坐标数组
bot.scanNearby(24)                               // 返回 {blockName: count}
bot.inventory.items()                            // 返回背包物品 [{name, count}]
```

## 编写规则
1. 写一个名称有意义的 `async function taskFunctionName(bot)` 函数。
2. **优先使用控制原语**，不要调用 bot.dig / bot.craft / bot.openFurnace 等底层 API。
3. 函数必须通用：不硬编码坐标，用 bot.findNearbyBlocks 或 exploreUntil 发现资源。
4. 用 `bot.chat(...)` 报告关键中间进度（这些内容会作为 observation 回传）。
5. 背包里没有需要的物品时，先获取，不要假设已有。
6. 不写无限循环，不用 `bot.on` / `bot.once`，不要写递归函数。
7. 所有变量声明在函数内部。
8. bot.findBlocks 的 maxDistance 始终设为 32。
9. 需要工作台时：先检查背包 → 没有则 craftItem("crafting_table") → placeItem 放置。
10. 探索时每次随机选不同方向。

## 响应格式（严格遵守，缺一不可）

Explain: （仅在有上一轮错误时）分析 Execution error 和 Chat log，说明为什么上次没成功。
Plan:
1. ...
2. ...
Code:
```javascript
async function taskName(bot) {
    // 实现
}
```"""


CODE_GENERATION_HUMAN_TEMPLATE = """Code from the last round: {last_code}
Execution error: {execution_error}
Chat log: {chat_log}
Biome: {biome}
Time: {time_of_day}
Nearby blocks: {nearby_blocks}
Nearby entities: {nearby_entities}
Health: {health} / 20
Hunger: {food} / 20
Position: x={pos_x}, y={pos_y}, z={pos_z}
Equipment: {equipment}
Inventory ({inv_used}/36): {inventory}
Task: {task}
Context: {context}
Critique: {critique}"""


# ─────────────────────────────────────────────────────────────────────────────
# ★ 技能代码抽象 Prompt（成功轨迹 → 可复用 JS 函数）
# ─────────────────────────────────────────────────────────────────────────────

SKILL_CODE_SYSTEM_PROMPT = """You are a Minecraft skill extractor. Given a successfully executed JS function and its task, produce a cleaner, reusable version as a skill.

Rules:
1. Make it generic — remove hardcoded values, add parameters.
2. Signature must be: async function skillName(bot, params = {})
3. Keep using the control primitives (mineBlock, craftItem, etc.).
4. Add bot.chat() at key progress points.
5. Function name should clearly describe the capability.

Output valid JSON only:
{
  "name": "function_name",
  "description": "one sentence what this skill does",
  "code": "async function function_name(bot, params = {}) { ... }",
  "parameters": {"param_name": "description and default"},
  "preconditions": ["what must be true before calling"]
}"""


SKILL_CODE_HUMAN_TEMPLATE = """Task: {task}

Successfully executed code:
{code}

Execution output:
{output}

Extract reusable skill (JSON only):"""


# ─────────────────────────────────────────────────────────────────────────────
# Critic
# ─────────────────────────────────────────────────────────────────────────────

CRITIC_SYSTEM_PROMPT = """You are a Minecraft task success evaluator.
Be STRICT: success=true only if fully met.
Check inventory, position, entities as needed.
Critique must be specific and actionable (max 2 sentences).
Output ONLY: {"success": true/false, "critique": "..."}"""

CRITIC_HUMAN_TEMPLATE = """Task: {task}
Position: {position}
Inventory: {inventory}
Nearby entities: {nearby_entities}
Health: {health}/20
Execution output: {last_output}
JSON only:"""


# ─────────────────────────────────────────────────────────────────────────────
# Planner
# ─────────────────────────────────────────────────────────────────────────────

PLANNER_SYSTEM_PROMPT = """You are a Minecraft task decomposer.
Subtask descriptions must be clear imperative instructions that a code-generating agent can act on directly.
Order by dependency: gather → smelt → craft → build.
2-6 subtasks max.
Output JSON array only:
[{"name":"short","description":"Direct instruction","success_criteria":"has N item","required_items":{},"can_skip_if":"has:item:N"}]"""

PLANNER_HUMAN_TEMPLATE = """Task: {task}
Inventory: {inventory}
Position: {position}
Biome: {biome}
{rag_context}
Decompose (JSON array only):"""


# ─────────────────────────────────────────────────────────────────────────────
# Intent / Complexity / Autonomous / Chat
# ─────────────────────────────────────────────────────────────────────────────

INTENT_SYSTEM_PROMPT = """Classify player message. One word only.
task_execution | knowledge_qa | chat"""

COMPLEXITY_SYSTEM_PROMPT = """Classify Minecraft task. One word only.
complex: multiple dependent steps | simple: 1-3 direct steps"""

AUTONOMOUS_SYSTEM_PROMPT = """Minecraft AI agent autonomous mode.
Priority: 1)survival(health<10) 2)safety(night) 3)craft basic tools 4)explore
Output JSON: {"reasoning":"why","urgency":"high|medium|low","task":"one sentence task"}"""

AUTONOMOUS_HUMAN_TEMPLATE = """State: {game_state}
Recent: {recent_memory}
Next action? (JSON only):"""
