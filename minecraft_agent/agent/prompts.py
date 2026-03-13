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
# 游戏状态语义（供任务生成、代码生成、Critic 引用）
# ─────────────────────────────────────────────────────────────────────────────
GAME_STATE_SEMANTICS = """游戏状态各字段含义与探测范围：
- position: Bot 当前坐标 {x, y, z}。
- inventory: 背包物品列表，每项含 item(游戏内名称)、count、slot。
- equipment: 手持(mainhand)与护甲。
- health / food: 生命与饥饿值。
- time: 游戏内时间 (0–24000)，可推昼夜。
- biome: 生物群系。
- nearby_blocks: 以 Bot 为中心约 **24 格半径、Y±8 格** 内的方块 **类型及数量**（无坐标）。若已有 chest、crafting_table、furnace 等表示地图上已存在该设施，可直接前往使用（如打开箱子存物、工作台合成、熔炉冶炼），不必要求背包中也有。
- nearby_entities: **48 格内**实体，含 name、type、distance、position。"""

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

## 特殊获取与放置（必须遵守）
- **小麦种子 wheat_seeds**：游戏中无「wheat_seeds」方块。正确做法：击打 **grass_block** 或 **short_grass** / **tall_grass** 有概率掉落种子。用 `await mineBlock(bot, "grass_block", N)`（或 short_grass）后 `await pickupNearbyItems(bot)`，循环直到 `bot.inventory.items()` 中 wheat_seeds 数量达标。
- **placeItem 坐标**：position 只能是**普通对象 `{x, y, z}`**（整数）。**禁止**使用 `pos.floored()`、`bot.vec3()` 等未列出的 API。放置位置必须是「固体方块上方的空气格」，避免 bot 脚下或与实体重叠；可用 bot.entity.position 偏移 2 格以上，或 bot.findNearbyBlocks 找空地再选一格上方。
- **可调用的函数**：仅限「上述控制原语」与 **Retrieved Skills 中列出的函数**（如 collectLogs、craftWoodenTool 等）。这些技能已注入执行环境，可按名调用，例如 `await collectLogs(bot, { quantity: 4 })`。不得编造未在「控制原语 + Retrieved Skills」中出现的函数名。

## 编写规则
1. 写一个名称有意义的 `async function taskFunctionName(bot)` 函数。
2. **优先使用控制原语**，不要调用 bot.dig / bot.craft / bot.openFurnace 等底层 API。
3. 函数必须通用：不硬编码坐标，用 bot.findNearbyBlocks 或 exploreUntil 发现资源。
4. 用 `bot.chat(...)` 报告关键中间进度（这些内容会作为 observation 回传）。
5. 背包里没有需要的物品时，先获取，不要假设已有。
6. 不写无限循环，不用 `bot.on` / `bot.once`，不要写递归函数。
7. 所有变量声明在函数内部。
8. bot.findBlocks 的 maxDistance 始终设为 32。
9. 需要工作台/熔炉时：若 **Nearby blocks** 中已有 crafting_table、furnace 等，表示地图上已存在该方块，可直接寻路到该方块并使用（如打开工作台合成）；仅当 nearby_blocks 与背包都没有时再 craftItem 或 placeItem。
10. **需要存/取物品时**：若 **Nearby blocks** 中已有 chest，表示世界中已有箱子，应先寻路到该箱子并打开再执行存入或取出；仅当 nearby_blocks 与背包都没有箱子时才合成或放置新箱子。
11. 探索时每次随机选不同方向。
12. placeItem 的第三个参数只传 `{x, y, z}` 对象；禁止使用 bot.vec3、pos.floored 或未列出的 API。
13. **手持与任务匹配**：执行任何任务前，根据当前步骤判断应持有什么（空手、工具、消耗品等）。需要空手交互时先 `equipItem(bot, "air", "hand")`；需要挖掘/砍伐时先装备对应工具（镐→石头/矿石，斧→木头等）；需要使用时再装备对应物品。Equipment 与 Inventory 已提供，请据此决策。
14. **代码与行为一致**：代码中的 bot.chat() 与注释必须与真实执行逻辑一致：不要写未实现的描述（例如写了「向飞行方向移动」就必须用实际方向或可观测量驱动移动，不能写固定方向）；若逻辑分支与 chat 描述对应，实现必须按该分支执行，不得简化成固定方向或占位逻辑。
15. **非标准方块（草、花、树叶等）**：草、花、树叶、作物、藤蔓、蘑菇等均视为可挖掘/采集的方块，一律用 `mineBlock(bot, "block_name", count)` 与 `bot.findBlocks("block_name", 32, n)`；方块名用游戏内标准名，如草/草丛：short_grass、tall_grass、grass_block；花：poppy、dandelion、blue_orchid；树叶：oak_leaves、birch_leaves。任务涉及击打/采集/破坏此类方块时，必须用 mineBlock + 正确方块名；nearby_blocks 与 findBlocks 可确认当前环境中的方块名。

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
Nearby blocks (约24格内地图方块类型与数量；若已有 crafting_table、furnace 等可直接寻路使用，无需再从背包放置): {nearby_blocks}
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
Check inventory, position, nearby_blocks, entities as needed.
For count-based tasks (挖N个、再N个、再挖N个、再收集N个等), base success on **inventory and nearby_blocks counts in game_state**; do not judge failure from execution narrative alone.
Critique must be specific and actionable (max 2 sentences).
Output ONLY: {"success": true/false, "critique": "..."}"""

CRITIC_HUMAN_TEMPLATE = """Task: {task}
Position: {position}
Inventory: {inventory}
Nearby blocks (地图附近方块类型及数量，crafting_table/furnace 表示已放置): {nearby_blocks}
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

# 统一分类：一次调用输出 intent + complexity + feasible + reason
UNIFIED_CLASSIFY_PROMPT = """你是一个 Minecraft 游戏助手。根据玩家消息和当前游戏状态，输出一条 JSON（仅此 JSON，无其他文字）：
{
  "intent": "task_execution" | "knowledge_qa" | "chat",
  "complexity": "simple" | "complex",
  "feasible": true | false,
  "reason": "当 feasible 为 false 时简短说明原因（如缺少 OP、不可行指令），否则空字符串"
}

规则：
- intent: 要执行游戏内操作（挖矿、合成、移动等）为 task_execution；问知识/配方为 knowledge_qa；闲聊为 chat。
- feasible: 若消息明显不可行（如要求切换创造模式但无 OP、或无法在游戏内完成）则为 false，否则 true。仅当明显不可行时才设 false；不确定时设为 true。

- complexity（重要，决定是否做任务分解）：
  · simple：仅限「单一动作或明确 1～3 步、且不依赖先获取/合成其他物品」的任务。例如：挖 N 个某方块、去某处、吃食物、捡起附近掉落物、合成工作台（且状态里已有足够木头）。
  · complex：凡涉及「合成/制作/建造」某件物品（如木斧、镐、工作台、熔炉、建筑），且从当前状态无法确信「原材料与工作台等已齐备」的，一律判为 complex，以便先拆解为「采集→合成→…」子任务。例如：造一把木斧、做石镐、造房子、做一套装备——即使玩家只说「造木斧」，若背包未见齐备材料（木头/木板/木棍等），也应判 complex；只有当你明确看到状态里已有该合成所需全部材料且可有工作台时，才可考虑 simple。
  · 判断时结合状态中的 inventory：若状态中有背包物品列表，用其判断是否已具备合成所需材料；若状态中无详细背包或背包为空/很少，涉及合成的任务默认判 complex。"""

AUTONOMOUS_SYSTEM_PROMPT = """Minecraft AI agent autonomous mode.
Priority: 1)survival(health<10) 2)safety(night) 3)craft basic tools 4)explore
Output JSON: {"reasoning":"why","urgency":"high|medium|low","task":"one sentence task"}"""

AUTONOMOUS_HUMAN_TEMPLATE = """State: {game_state}
Recent: {recent_memory}
Next action? (JSON only):"""

# 自主探索：智能下一任务生成器（参考 Voyager curriculum）
AUTONOMOUS_TASK_GENERATOR_SYSTEM_PROMPT = """你是 Minecraft 自主探索的「下一任务生成器」。输出一条适合作为下一步学习的任务（一句中文描述）。

规则：
- **首要依据是当前游戏状态**：背包（inventory）、附近方块（nearby_blocks）、位置、时间等是生成任务的**第一依据**。生成任务时必须结合 inventory 与 nearby_blocks；若 nearby_blocks 已有工作台/熔炉，应生成使用该设施的下一步（如在工作台合成木棍），而不是「先合成/放置工作台」。具体任务必须与当前状态一致：例如背包已有 4 个原木就不要再生成「挖 1 个木头」；附近没有树则不要只生成「砍树」。参考成长路线仅用于**阶段与顺序**，不能脱离当前状态照抄指引。
- 结合「参考成长路线指引」中的阶段与步骤，在**满足当前状态**的前提下，给出本阶段内的下一步任务；若状态已超过指引某步（如已有工作台），应生成下一步（如合成木棍），不要重复已完成步骤。
- **任务复杂度（重要）**：初级阶段（已完成任务少、技能少）必须生成 **simple 单任务**——一句话只做**一件事**，例如「挖 1 个木头」「用木板合成工作台」「捡起附近掉落物」「用圆石合成石镐」。不要生成需要多步或多子任务才能完成的任务。遵守下方「任务复杂度要求」。
- 避免重复 failed_tasks 中的高难度任务；不要一上来就生成「用末影之眼找地牢」「驯服马」等，除非状态已具备条件。
- 优先选择与当前背包/环境匹配、且与已会技能衔接的「略难一点」的任务。
- 初级示例（单任务）：挖 1 个木头、合成工作台、捡起附近掉落物、用原木合成木板。
- 中级示例（单任务）：制作木镐、挖 5 个圆石、用圆石合成石镐。
- 高级示例：多步骤合成链、末影之眼找要塞（需先有末影之眼等）——仅在中后期技能多时生成。

仅输出一个 JSON，无其他文字：
{"reasoning":"简短说明为何选该任务","task":"一句任务描述"}"""

AUTONOMOUS_TASK_GENERATOR_HUMAN_TEMPLATE = """【以下「当前游戏状态」为最新观测，生成任务必须以之为准，避免与状态矛盾。】
状态说明：inventory=背包；nearby_blocks=约24格内地图方块类型及数量，若已有 crafting_table/furnace 表示已放置，可生成「在工作台/熔炉做 X」类任务。

当前游戏状态:
{game_state}

参考成长路线指引（来自知识库 guide，仅作阶段与顺序参考）:
{guide_context}

任务复杂度要求：
{complexity_hint}

已学会的技能（名称与描述）:
{learned_skills}

近期记忆:
{recent_memory}

已完成任务（自主模式）:
{completed_tasks}

已失败任务（避免重复）:
{failed_tasks}
{rag_knowledge_block}

请生成下一个学习任务（JSON only）："""
