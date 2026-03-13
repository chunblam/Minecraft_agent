# 测试日志分析（2026-03-11 两场）

## 一、日志一（run_2026-03-11_23-49-38.log）— 小麦种子

### 现象

- 用户：「采集10个小麦种子」
- 统一分类、RAG（mc_base）、技能检索正常；首次用 stream_think 生成代码。
- 生成代码逻辑：`collectWheatSeeds`，检查背包 `wheat_seeds` 数量，后续逻辑日志未完整打出，但执行后无成功记录。

### 根因

**小麦种子在游戏里不是「方块」，而是击打草类方块后的掉落物。**

- 游戏中**没有名为 wheat_seeds 的方块**可被 `mineBlock(bot, "wheat_seeds", N)` 挖掘。
- 正确获取方式：击打 **草方块（grass_block）** 或 **高草（short_grass / tall_grass）**，有概率掉落小麦种子，再用 `pickupNearbyItems(bot)` 捡起；循环直到背包中 `wheat_seeds` 数量达标。

若模型或 RAG 没有这条知识，就会按「挖某种方块」来写，可能去查不存在的方块或只检查背包，导致无法真正采集。

### 建议

1. **在代码生成 System Prompt 中固定写一条「特殊获取」规则**（推荐）  
   - 明确写：小麦种子（wheat_seeds）来自击打 **grass_block** 或 **short_grass** / **tall_grass**，用 `mineBlock` 击打这些草类后 `pickupNearbyItems`，循环直到背包种子数量足够。  
   - 这样无需依赖 RAG 命中，每次生成都会看到。

2. **RAG 补充（可选）**  
   - 在知识库（如 base 或新建「获取方式」）中加一篇：小麦种子、甜菜种子等「由草/作物掉落」的获取方式，便于扩展类似任务（如甜菜种子）。

---

## 二、日志二（run_2026-03-11_23-56-09.log）— 制作箱子与放置

### 现象概览

| 阶段 | 现象 |
|------|------|
| 任务分解 | 「制作一个箱子来存放物品」→ complex，分解为 Gather wood、Craft chest，合理。 |
| 子任务 1 | Place crafting_table + 合成木板；首次 `blockUpdate` 超时，重试后执行成功但 Critic 判木板不足。 |
| 子任务 2/续做 | 用户说「接着完成」→ 放置箱子；多次失败：`blockUpdate` 超时、`pos.floored is not a function`、`bot.vec3 is not a function`、再次 `blockUpdate` 超时。 |
| 其他 | 「你走到我的身边」被误判为 chat；用户澄清后再次发「游戏里操控移动到我身边」才走 task；「做10次蹲起」同样先判 chat，澄清后才执行。 |

### 根因分析

1. **blockUpdate 超时（放置工作台/箱子）**  
   - mineflayer 的 `placeBlock` 会等待 `blockUpdate:目标坐标` 事件，超时 5s。  
   - 可能原因：选点恰为 bot 脚下或与实体重叠、朝向/距离导致服务端未确认放置、或选点不可放置（非固体表面上方）。  
   - 即：**放置逻辑不够稳**——未保证「可放置的固体表面上方空格」或选点策略单一（如固定 offset(1,0,0)）。

2. **pos.floored / bot.vec3 不存在**  
   - 生成代码里出现了 `pos.floored()`、`bot.vec3()`。  
   - 在注入的上下文中，**只有「控制原语 + 少量工具方法」**；坐标应使用**普通对象 `{x, y, z}`** 传给 `placeItem(bot, name, {x,y,z})`。  
   - 模型「幻觉」了 Vec3/floored 等未在 prompt 中提供的 API，导致运行时报错。

3. **collectLogs is not defined**  
   - 生成的代码调用了未定义的 `collectLogs`，说明模型在复杂子任务中「编造」了不存在的函数名，应只使用原语与已列出的 bot 方法。

4. **RAG 是否起作用**  
   - RAG 分类到了 mc_base、mc_trading，说明检索路径有触发。  
   - 但「如何安全放置方块」「placeItem 的坐标格式」「箱子合成配方」若不在检索到的片段里，或片段太短，模型仍会依赖自身知识，容易产生 vec3/floored 和错误选点。  
   - 因此：**仅靠 RAG 不够**，需要在 **System Prompt 里明确**：坐标格式、禁止未列出 API、放置选点原则。

5. **任务执行灵活度**  
   - 与模型能力有一定关系（能否从错误信息反推正确 API、选点策略），但更直接的是**缺少明确文档**：  
   - 放置时只能使用 `{x, y, z}`；选点应为「固体方块上方空气」、避免脚下/实体重叠；不要使用 bot.vec3、pos.floored、自定义未定义函数。

---

## 三、与既有问题的关系

| 既有问题（README/评估） | 本次日志体现 |
|------------------------|--------------|
| 合成顺序/工作台依赖 | 子任务中木板数量、工作台放置与合成顺序仍有混乱（Critic 报木板不足）。 |
| 生成代码与运行环境不一致 | **明显**：pos.floored、bot.vec3、collectLogs 未定义；placeItem 的坐标格式未在 prompt 写清。 |
| 技能库/Critic | 未在本两场日志中重点暴露。 |
| 意图误判（闲聊 vs 任务） | 「你走到我的身边」「做10次蹲起」首次判成 chat，需用户澄清才执行。 |

---

## 四、建议措施汇总

### 1. 代码生成 Prompt 补充（必做）

在 `CODE_GENERATION_SYSTEM_PROMPT` 中：

- **特殊获取**  
  - 写明：小麦种子（wheat_seeds）需通过击打 **grass_block** 或 **short_grass** / **tall_grass**，再用 `pickupNearbyItems` 捡起，循环直到数量足够；不要对「wheat_seeds」使用 mineBlock（无此方块）。

- **放置与坐标**  
  - 写明：`placeItem(bot, name, position)` 的 position **只能是普通对象 `{x, y, z}`**（整数），**禁止**使用 `pos.floored()`、`bot.vec3()` 等未列出的 API。  
  - 写明：放置位置必须是「可放置的固体方块上方的空气格」，避免 bot 脚下或与实体重叠；可用 `bot.findNearbyBlocks` 找空地或参考 bot.entity.position 偏移 2 格以上再取放置点。

- **禁止编造 API**  
  - 写明：只使用上述控制原语与工具方法，不要定义或调用未在列表中出现的函数（如 collectLogs）。

### 2. RAG 知识库补充（已做）

- 已在 `data/knowledge_base/base/obtain_and_place.md` 新增文档，内容包括：  
  - 小麦种子（wheat_seeds）来自击打 grass_block / short_grass / tall_grass，以及推荐流程（mineBlock + pickupNearbyItems 循环）；  
  - placeItem 坐标格式（仅 `{x,y,z}`）、放置位置要求与选点建议；  
  - 工作台与箱子的合成与放置流程简述。  
- 该文件位于 base 目录，会被路由到 **mc_base**。**需重新运行 `python load_knowledge_base.py`** 后，新文档才会被切片并写入 ChromaDB，供 RAG 检索。

### 3. 原语/Node 侧（可选）

- **placeItem 鲁棒性**：若 Node 侧 `placeItem` 在收到 `{x,y,z}` 后内部会转为 Vec3 再调 placeBlock，可确认 refBlock 与目标格是否有效，避免无效位置触发 blockUpdate 长时间不触发。  
- **mineBlock 与草**：确认 `grass_block`、`short_grass`、`tall_grass` 在 mcData.blocksByName 中存在且可被 collectBlock 挖掘；若当前 mineBlock 只支持「方块名」，可考虑为「采集小麦种子」单独加一条原语或文档说明「先挖草再捡」的流程。

### 4. 意图分类（可选）

- 「你走到我的身边」「做10次蹲起的动作」等偏口语的移动/动作指令，可考虑在统一分类的 few-shot 或规则中略微向 task_execution 倾斜，或保留现状依赖用户澄清。

---

## 五、结论

- **日志一**：小麦种子失败主要因为**缺少「种子来自击打草」的明确知识**；建议在 **Prompt 中写死** 这条，RAG 可再补一篇扩展类似作物。  
- **日志二**：放置卡在 **blockUpdate 超时** 和 **坐标/API 误用**（floored、vec3、未定义函数）；建议在 **Prompt 中写清** placeItem 的坐标格式与禁止 API，并简要说明放置选点原则；RAG 可补充「基本操作」文档。  
- **是否用 RAG**：两条都适合用 RAG 做**补充**（更多获取方式、更细放置说明），但**关键约束与 API 使用必须写在 System Prompt**，否则模型容易幻觉且行为不稳定。
