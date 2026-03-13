# Minecraft Agent 项目深度分析与 Multi-Agent 升级方案

> 基于 `chunblam/Minecraft_agent` 仓库 `minecraft_agent/` 目录的完整技术报告

---

## 目录

1. [项目架构全景](#1-项目架构全景)
2. [核心模块逐层解析](#2-核心模块逐层解析)
3. [运行逻辑流程图](#3-运行逻辑流程图)
4. [多场景运行示例](#4-多场景运行示例)
5. [现有问题诊断与优化方案](#5-现有问题诊断与优化方案)
6. [Multi-Agent 升级方案](#6-multi-agent-升级方案)

---

## 1. 项目架构全景

### 1.1 整体分层

```
┌─────────────────────────────────────────────────────────────┐
│                      用户层 (Minecraft Chat)                  │
│              玩家在游戏内聊天发出自然语言指令                    │
└─────────────────────────┬───────────────────────────────────┘
                          │ chat event
┌─────────────────────────▼───────────────────────────────────┐
│                     Python Agent 层                           │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │  main.py     │  │ react_agent  │  │ planner /        │  │
│  │ 入口/路由器   │→│ VoyagerAgent │  │ plan_executor    │  │
│  └──────────────┘  └──────┬───────┘  └──────────────────┘  │
│                            │                                  │
│  ┌──────────────┐  ┌──────▼───────┐  ┌──────────────────┐  │
│  │  critic.py   │  │  prompts.py  │  │  llm_router.py   │  │
│  │  自验证判定   │  │  提示词模板   │  │  LLM调用封装     │  │
│  └──────────────┘  └──────────────┘  └──────────────────┘  │
│                                                               │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │skill_library │  │  memory.py   │  │  rag/retriever   │  │
│  │  技能检索存储  │  │  短/长期记忆  │  │  RAG知识检索     │  │
│  └──────────────┘  └──────────────┘  └──────────────────┘  │
└─────────────────────────┬───────────────────────────────────┘
                          │ HTTP REST (localhost:3000)
┌─────────────────────────▼───────────────────────────────────┐
│                    Node.js Mineflayer 层                      │
│  ┌──────────────────────┐  ┌──────────────────────────────┐ │
│  │  index.js            │  │  lib/primitives.js           │ │
│  │  HTTP Server         │  │  mine/craft/smelt/move/      │ │
│  │  execute_code 接口    │  │  pathfinder 等底层原语        │ │
│  └──────────────────────┘  └──────────────────────────────┘ │
└─────────────────────────┬───────────────────────────────────┘
                          │ Minecraft Protocol
┌─────────────────────────▼───────────────────────────────────┐
│                    Minecraft Java Server                       │
└─────────────────────────────────────────────────────────────┘
```

### 1.2 文件职责速查表

| 文件 | 层级 | 核心职责 |
|------|------|---------|
| `main.py` | 入口 | 启动 Mineflayer、监听聊天、意图路由、断线重连 |
| `agent/react_agent.py` | 核心 | 意图分类、复杂度判断、ReAct循环、技能入库调度 |
| `agent/critic.py` | 评判 | 任务完成后调用 LLM 判定是否真正成功 |
| `agent/planner.py` | 规划 | 层级任务分解（复杂任务 → 子任务列表） |
| `agent/plan_executor.py` | 执行 | 按子任务序列顺序调用 VoyagerAgent |
| `agent/skill_library.py` | 记忆 | ChromaDB 向量存储，技能检索与写入 |
| `agent/llm_router.py` | LLM | 统一封装 DeepSeek / OpenAI 调用，超时管理 |
| `agent/env.py` | 环境 | HTTP 调用 Mineflayer，Bot 状态监控与重连 |
| `agent/prompts.py` | 提示 | 所有 system/user prompt 模板集中管理 |
| `agent/memory.py` | 记忆 | 短期（当前会话）/ 长期（跨会话）记忆存取 |
| `agent/personality.py` | 个性 | 情绪值、好感度建模，影响回复语气 |
| `agent/autonomous_explorer.py` | 自主 | 空闲探索逻辑，主动发现新技能 |
| `rag/retriever.py` | RAG | LLM分类 + 向量检索 Minecraft 知识库 |
| `mineflayer/index.js` | Node | HTTP 服务器，暴露 `execute_code` 等接口 |
| `mineflayer/lib/primitives.js` | Node | 挖掘/合成/熔炼/移动/实体交互等原语 |

---

## 2. 核心模块逐层解析

### 2.1 main.py — 入口与消息路由

```
启动流程：
  1. 加载 .env 配置
  2. 启动 Node.js Mineflayer 子进程
  3. 调用 ensure_bot_connected() 等待 Bot 入服
  4. 注册 on_chat 事件回调
  5. 进入聊天监听循环

消息处理逻辑（伪代码）：
  on_chat(sender, message):
    if message.startswith("/"):     # 跳过命令
        return
    if is_system_message(message):  # 跳过传送提示等
        return
    if sender == bot_name:          # 忽略自己
        return
    if in_feedback_cooldown():      # 命令反馈冷却期
        return
    
    # 路由给 VoyagerAgent
    asyncio.create_task(agent.handle_message(sender, message))
```

**关键设计：** `main.py` 只负责事件分发，不含业务逻辑，职责清晰。

---

### 2.2 react_agent.py — VoyagerAgent 核心

这是整个系统最核心的模块，包含以下子流程：

#### 步骤 A：意图识别

```
用户消息
    ↓
classify_intent(message)  [LLM调用 #1]
    ↓
返回：TASK / CHAT / QUERY / AUTONOMOUS
    ↓
CHAT → 直接回复（闲聊路径）
TASK → 进入任务执行路径
QUERY → 查询知识/状态路径
```

#### 步骤 B：任务复杂度判断

```
TASK消息
    ↓
classify_complexity(task)  [LLM调用 #2]
    ↓
返回：SIMPLE / COMPLEX
    ↓
SIMPLE → _run_task()
COMPLEX → _run_hierarchical() → TaskPlanner分解 → PlanExecutor执行
```

#### 步骤 C：_run_task() 的 ReAct 循环

```
_run_task(task, context):
    for step in range(MAX_REACT_STEPS):         # 最多12步
        
        # 三路并发上下文构建
        skills   = skill_library.search(task)   # 向量检索技能
        rag_docs = rag.retrieve(task)            # RAG知识检索
        memories = memory.retrieve(task)         # 历史记忆检索
        
        # 合并上下文，调用LLM生成JS代码  [LLM调用 #3]
        code = llm.generate_code(task, skills, rag_docs, memories, error_history)
        
        # 发送到 Mineflayer 执行
        result = env.execute_code(code)
        
        if result.success:
            break
        else:
            error_history.append(result.error)  # 错误反馈进下轮
    
    # 执行完毕（成功或超出重试）→ Critic 验证
    success = critic.evaluate(task, result)     # [LLM调用 #4]
    
    if success:
        # 技能抽象入库  [LLM调用 #5]
        skill = llm.abstract_skill(task, code)
        skill_library.store(skill)
    
    return success
```

---

### 2.3 critic.py — 自验证

Critic 接收任务描述、执行代码、执行结果（chat_log + game_state），调用 LLM 判断是否达成目标。

关键点：Critic 是独立的 LLM 调用，使用单独的 prompt，防止生成者给自己打高分。

---

### 2.4 skill_library.py — 技能库

技能的数据结构：

```python
Skill = {
    "name": "mine_wood",           # 技能名（唯一键，重名覆盖）
    "description": "砍5个橡木原木",  # 语义描述（用于向量检索）
    "code": "async function ...",   # 可复用的JS代码
    "usage_count": 3,               # 使用次数
    "created_at": "2026-01-01"
}
```

存储后端：**ChromaDB**（向量数据库），每次检索时将任务描述向量化后做余弦相似度匹配，返回 top-k 相关技能。

---

### 2.5 mineflayer/index.js — Node.js 服务

```javascript
// 核心接口
POST /execute_code
  body: { code: "async function(bot){ ... }" }
  → eval/new Function 动态执行
  → 返回 { success, output, error, chat_log }

GET /observe
  → 返回 bot 当前状态（位置、背包、血量等）

POST /restart
  → 重新创建 bot 实例并重连 MC 服务器
```

**安全说明：** `execute_code` 直接 eval 字符串代码，在本地受控环境下可接受，生产环境需隔离。

---

## 3. 运行逻辑流程图

### 完整任务流程（以"制作木斧"为例）

```
玩家聊天: "帮我制作一把木斧"
          │
          ▼
[main.py] on_chat() 过滤 → 分发给 VoyagerAgent
          │
          ▼
[react_agent] classify_intent() → TASK
          │
          ▼
[react_agent] classify_complexity() → COMPLEX（多步合成）
          │
          ▼
[planner] decompose_task() → 子任务列表：
          ├─ 1. 砍3个木头
          ├─ 2. 合成木板
          ├─ 3. 合成木棍
          ├─ 4. 放置工作台
          └─ 5. 合成木斧
          │
          ▼
[plan_executor] 顺序执行每个子任务
  ┌─ 子任务1: _run_task("砍3个木头")
  │   └─ ReAct循环 → 生成JS → Mineflayer执行 → Critic验证 → 成功入库
  ├─ 子任务2: _run_task("合成木板")
  │   └─ (检索到"mine_wood"技能) → 仅生成合成代码
  ├─ 子任务3-4-5: 同理
  └─ 所有子任务完成 → 返回总结
          │
          ▼
Bot 在游戏中完成操作，聊天回复："已为你制作好木斧！"
```

---

## 4. 多场景运行示例

### 场景一：简单任务 — "来到我身边"

```
输入: "来到我身边"

Step 1: classify_intent → TASK
Step 2: classify_complexity → SIMPLE
Step 3: _run_task()
  ├─ 技能检索: 找到 "follow_player" 技能（若存在）
  ├─ 生成JS代码:
  │   async function(bot) {
  │     const player = bot.players["玩家名"].entity;
  │     await bot.pathfinder.goto(
  │       new GoalNear(player.position.x, player.position.y, player.position.z, 2)
  │     );
  │   }
  ├─ Mineflayer执行: Bot 使用 pathfinder 寻路
  ├─ 执行结果: success=true, output="到达玩家附近"
  └─ Critic验证: 成功
  
总LLM调用: 4次（意图/复杂度/代码生成/Critic）
总耗时: ~15-30秒
```

---

### 场景二：中等任务 — "挖10个钻石矿"

```
输入: "帮我挖10个钻石矿"

Step 1: classify_intent → TASK
Step 2: classify_complexity → SIMPLE（单一资源采集）
Step 3: _run_task()
  ├─ 技能检索: 找到 "mine_diamond" (相似度0.85)
  ├─ RAG检索: 钻石分布Y=-58到-64，需要铁镐以上
  ├─ 生成代码:
  │   async function(bot) {
  │     // 检查是否有合适的镐
  │     if (!bot.inventory.findInventoryItem(mcData.itemsByName['iron_pickaxe'].id)) {
  │       throw new Error("需要铁镐");
  │     }
  │     // 找到最近的钻石矿
  │     const diamondOres = bot.findBlocks({
  │       matching: mcData.blocksByName['diamond_ore'].id,
  │       maxDistance: 32, count: 10
  │     });
  │     for (const pos of diamondOres) {
  │       await bot.dig(bot.blockAt(pos));
  │     }
  │   }
  ├─ 执行: Bot开始采矿
  ├─ 可能失败: "没有找到钻石矿"（区块未加载）
  ├─ 重试1: 代码改为先移动到Y=-60区域再搜索
  ├─ 执行成功
  └─ Critic验证: 背包中有10个钻石 → 成功

总LLM调用: 6次（+1次重试代码生成）
总耗时: ~2-5分钟（含实际采矿时间）
```

---

### 场景三：复杂任务 — "建造一个3x3的木屋"

```
输入: "建造一个3x3的木屋"

Step 1: classify_intent → TASK
Step 2: classify_complexity → COMPLEX
Step 3: TaskPlanner.decompose()
  ├─ 子任务1: 采集60个橡木原木
  ├─ 子任务2: 合成60个木板
  ├─ 子任务3: 确定建造位置（平地）
  ├─ 子任务4: 建造地基（9个木板）
  ├─ 子任务5: 建造四面墙（32个木板）
  └─ 子任务6: 建造屋顶（9个木板）

Step 4: PlanExecutor 顺序执行
  ├─ 子任务1执行（ReAct循环，可能3-4步）
  ├─ 子任务2-3执行
  ├─ 子任务4-6: 建造代码（setBlock/placeBlock循环）
  │   for(let x=0; x<3; x++)
  │     for(let z=0; z<3; z++)
  │       await bot.placeBlock(referenceBlock, vec3(x,0,z));
  └─ 全部完成

总LLM调用: ~15-20次
总耗时: ~10-20分钟
关键风险点: 
  - 子任务4-6的坐标计算容易出错（幻觉）
  - placeBlock API 参数格式不稳定
  - Critic可能因视角问题误判
```

---

### 场景四：失败恢复 — "制作一把木斧" 出现已知Bug

```
输入: "制作一把木斧"

第1次尝试:
  代码: await bot.craft(mcData.itemsByName['wooden_axe'].id, 1, null)
  错误: "需要工作台才能合成"
  
第2次重试（error注入prompt）:
  代码: 
    const craftingTable = bot.findBlock({ matching: mcData.blocksByName['crafting_table'].id });
    await bot.craft(recipe, 1, craftingTable);
  错误: "pos.floored is not a function" ← 已知的 API 幻觉 Bug

第3次重试:
  代码: 修正 pos 用法
    const pos = craftingTable.position;  // 直接用 .position 不调 .floored()
  执行成功
  
Critic验证: 背包中有wooden_axe → 成功
技能入库: "craft_wooden_axe" with corrected code

总重试: 3次（正好在MAX_TASK_RETRIES=4内）
```

---

## 5. 现有问题诊断与优化方案

### 问题 1：LLM 调用链路过长，延迟极高

**现象：** 单任务最多消耗 5 次串行 LLM 调用（意图→复杂度→代码生成→Critic→技能抽象），外加 RAG 内部又有一次 LLM 分类，总计 6 次以上，单轮延迟数十秒至数分钟。

**根本原因：**
```
串行调用链:
意图分类(~3s) → 复杂度(~3s) → RAG分类(~3s) → 代码生成(~10s) → Critic(~5s) → 技能抽象(~5s)
                                                    ↑ 重试N次
= 最坏情况: 3+3+3+(10×4)+5+5 = ~59秒
```

**优化方案 A：合并前期分类调用（立竿见影）**

将意图分类 + 复杂度判断合并为一次 LLM 调用，结构化输出：

```python
# 现有：2次调用
intent = classify_intent(message)      # 调用1
complexity = classify_complexity(task) # 调用2

# 优化后：1次调用，结构化JSON输出
result = llm.call(prompt="""
分析以下消息，返回JSON：
{"intent": "TASK|CHAT|QUERY", "complexity": "SIMPLE|COMPLEX", "task_summary": "..."}
消息: {message}
""")
# 节省: ~3秒 + 1次API费用
```

**优化方案 B：RAG 去除内部 LLM 分类**

直接用任务文本做向量检索，取消 RAG 内部的分类预处理：

```python
# 现有（2步）:
category = llm.classify_for_rag(task)  # LLM调用
docs = chroma.query(category_embedding)

# 优化后（1步）:
docs = chroma.query(task_embedding, n_results=5)  # 直接语义检索
# 节省: ~3秒 + 1次API费用
```

**优化方案 C：Critic 与技能抽象异步化**

Critic 验证和技能抽象不阻塞用户回复：

```python
async def _run_task(task, context):
    result = await execute_with_retry(task)
    
    # 立即回复用户
    await bot.chat(f"任务完成！")
    
    # 后台异步处理：Critic验证 + 技能入库（不阻塞）
    asyncio.create_task(self._post_task_processing(task, result))
```

---

### 问题 2：有技能时仍完整生成代码，未利用技能模板

**现象：** 检索到相似度 0.9 的技能后，仍然让 LLM 从零生成完整代码，浪费 tokens 且容易幻觉。

**优化方案：三级代码生成策略**

```python
async def generate_code(task, skills, rag_docs):
    if skills and skills[0].similarity > 0.95:
        # 一级：直接复用（参数填空）
        return fill_params(skills[0].code, task)  # 无需LLM
    
    elif skills and skills[0].similarity > 0.75:
        # 二级：模板改写（轻量LLM，仅改参数/条件）
        return await llm.adapt_skill(
            base_code=skills[0].code,
            task=task,
            prompt="仅修改参数，保留结构"
        )
    else:
        # 三级：完整生成（含RAG知识和记忆）
        return await llm.generate_full_code(task, rag_docs)
```

---

### 问题 3：子任务完成后未独立技能化

**现象：** 层级执行时，子任务成功只更新 game_state，子任务的代码轨迹不入库，导致第二次执行相同复杂任务仍需重头规划。

**优化方案：**

```python
# plan_executor.py 修改
async def execute_subtask(subtask, context):
    result = await voyager_agent._run_task(subtask, context)
    
    if result.success:
        # 新增：子任务成功后触发技能入库
        asyncio.create_task(
            skill_library.store_if_novel(subtask, result.code)
        )
        context.update_game_state(result.state)
    
    return result
```

---

### 问题 4：技能库质量劣化（同名覆盖 + 异名同效）

**现象：**
- 同名技能被新代码覆盖，可能覆盖掉更好的旧版本
- 不同名但语义等价的技能重复占用存储，检索时引入噪音

**优化方案：技能版本管理 + 去重合并**

```python
class SkillLibrary:
    def store(self, skill):
        # 1. 检查同名技能版本管理
        existing = self.get_by_name(skill.name)
        if existing:
            if skill.quality_score > existing.quality_score:
                self.archive(existing)  # 归档旧版，不删除
                self.update(skill)
            return
        
        # 2. 检查语义重复（similarity > 0.9）
        similar = self.search(skill.description, threshold=0.9)
        if similar:
            # 合并：保留代码更优的，丰富description
            self.merge(existing=similar[0], new_skill=skill)
            return
        
        # 3. 全新技能，直接写入
        self.insert(skill)
    
    def quality_score(self, skill) -> float:
        # 基于：成功执行次数、代码长度合理性、无已知错误模式
        return skill.usage_count * 0.5 + code_quality(skill.code) * 0.5
```

---

### 问题 5：API 幻觉导致运行错误

**高频错误清单（来自 README 已知问题）：**
- `pos.floored is not a function`
- `bot.drop is not a function`（应为 `bot.toss`）
- 合成顺序：未先放工作台就合成需工作台的物品

**优化方案：构建 API 正确性约束层**

```javascript
// mineflayer/lib/api_guard.js
// 运行时拦截常见错误模式

const DEPRECATED_METHODS = {
    'bot.drop': 'bot.toss',
    'pos.floored()': 'Math.floor(pos)',
};

// 在 execute_code 执行前做静态检查
function precheck(code) {
    for (const [wrong, correct] of Object.entries(DEPRECATED_METHODS)) {
        if (code.includes(wrong.split('(')[0])) {
            return { valid: false, hint: `使用 ${correct} 替代 ${wrong}` };
        }
    }
    return { valid: true };
}
```

同时在 `prompts.py` 的代码生成 prompt 中加入强约束：

```
【API规范（必须遵守）】
- 丢弃物品用 bot.toss(itemType, metadata, count)，不是 bot.drop
- 位置取整用 Math.floor(pos.x)，不是 pos.floored()  
- 合成需工作台的物品前，必须先确认附近有 crafting_table
- pathfinder.goto 的目标必须是 GoalNear/GoalBlock 实例
```

---

### 问题 6：复杂任务判定不足，大量任务走 SIMPLE 路径

**现象：** "制作木斧"这种明显需要多步的任务被判为 SIMPLE，导致一段超长代码多次重试，而非子任务分解。

**优化方案：增强复杂度判定 prompt**

```python
COMPLEXITY_PROMPT = """
判断以下任务是 SIMPLE 还是 COMPLEX。

COMPLEX的标准（满足任意一条）：
1. 需要3个以上不同操作步骤
2. 需要先制作中间产物（如先做木板才能做木斧）
3. 需要建造结构（放置多个方块）
4. 需要先移动到特定区域再执行操作

示例：
- "来到我身边" → SIMPLE（单一移动）
- "制作木斧" → COMPLEX（采木→木板→木棍→工作台→木斧，5步）
- "挖10个石头" → SIMPLE（单一采矿）
- "建一个房子" → COMPLEX（采材→合成→建造，多步）

任务: {task}
返回: SIMPLE 或 COMPLEX（只返回这两个词之一）
"""
```

---

## 6. Multi-Agent 升级方案

### 6.1 设计理念与参考工程

本升级方案综合以下研究与工程的核心思想：

- **VillagerAgent（ACL 2024）**：DAG 任务图管理，有向无环图表达子任务依赖，Agent Controller 动态分配，State Manager 全局状态同步
- **Voyager**（原始参考）：技能库、ReAct循环、Critic验证
- **AutoGen**：Agent 间消息传递协议，多 Agent 对话框架
- **当前项目优势**：Mineflayer 底层已稳定，技能库已有积累，RAG+记忆体系完整

---

### 6.2 Multi-Agent 总体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                         用户指令层                                │
│              "帮我建一座村庄（5栋房子+农田+围栏）"                 │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│                    GlobalController (新增)                        │
│  ┌─────────────────┐  ┌────────────────┐  ┌─────────────────┐  │
│  │  MasterPlanner  │  │  TaskDAGBuilder │  │  AgentRegistry  │  │
│  │  高层任务理解    │  │  DAG依赖图构建  │  │  Agent注册/状态  │  │
│  └────────┬────────┘  └───────┬────────┘  └─────────────────┘  │
│           └──────────────────┘                                   │
│                    TaskDAG (DAG任务图)                            │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐    │
│  │ Task A   │──▶│ Task B   │   │ Task C   │──▶│ Task D   │    │
│  │砍木头    │   │ 合成木板  │   │ 挖石头   │   │ 建房子   │    │
│  └──────────┘   └────┬─────┘   └──────────┘   └────▲─────┘    │
│                       └─────────────────────────────┘           │
└──────────────────────────┬──────────────────────────────────────┘
                           │ 任务分发
┌──────────────────────────▼──────────────────────────────────────┐
│                    Agent Pool (多个独立Bot)                       │
│  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐    │
│  │ WorkerAgent-1  │  │ WorkerAgent-2  │  │ WorkerAgent-3  │    │
│  │ 角色: 伐木工   │  │ 角色: 建筑工   │  │ 角色: 矿工     │    │
│  │ Bot: Agent1    │  │ Bot: Agent2    │  │ Bot: Agent3    │    │
│  │ 技能库(共享)   │  │ 技能库(共享)   │  │ 技能库(共享)   │    │
│  └───────┬────────┘  └───────┬────────┘  └───────┬────────┘    │
└──────────┼────────────────────┼────────────────────┼────────────┘
           │                    │                    │
           ▼                    ▼                    ▼
    Mineflayer-1          Mineflayer-2          Mineflayer-3
    (port 3000)           (port 3001)           (port 3002)
           │                    │                    │
           └──────────────┬─────┘                    │
                          └──────────────────────────┘
                                     │
                              Minecraft Server
```

---

### 6.3 新增核心模块设计

#### 模块 1：GlobalController（全局控制器）

```python
# agent/multi_agent/global_controller.py

class GlobalController:
    """
    负责：高层任务分解、DAG构建、Agent调度、全局状态管理
    对应 VillagerAgent 的 TaskDecomposer + AgentController + StateManager
    """
    
    def __init__(self, n_agents: int = 3):
        self.dag_builder = TaskDAGBuilder()
        self.agent_registry = AgentRegistry()
        self.state_manager = GlobalStateManager()
        self.agents = [WorkerAgent(i) for i in range(n_agents)]
        self.message_bus = MessageBus()  # Agent间通信
    
    async def execute_mission(self, mission: str):
        # Step 1: 构建 DAG
        dag = await self.dag_builder.build(mission, self.state_manager.get_world_state())
        
        # Step 2: 拓扑排序，找出可并行的任务集
        ready_tasks = dag.get_ready_tasks()  # 无前置依赖的任务
        
        # Step 3: 持续调度，直到所有任务完成
        while not dag.is_complete():
            # 找出空闲的 agent
            idle_agents = self.agent_registry.get_idle_agents()
            
            # 分配：一个空闲agent对应一个ready任务
            for agent, task in zip(idle_agents, ready_tasks):
                asyncio.create_task(
                    self._execute_task_on_agent(agent, task, dag)
                )
            
            # 等待任意一个任务完成
            await asyncio.sleep(1.0)
            ready_tasks = dag.get_ready_tasks()  # 重新计算
        
        return dag.get_results()
    
    async def _execute_task_on_agent(self, agent, task, dag):
        # 检查是否需要等待前置任务
        await dag.wait_for_prerequisites(task)
        
        # 执行（可能需要Agent间协调：如A砍木头后传给B合成）
        result = await agent.execute(task, self.state_manager)
        
        # 更新DAG状态
        dag.mark_complete(task, result)
        self.state_manager.update(result.state_changes)
        
        # 发布完成事件（其他依赖此任务的任务可解锁）
        await self.message_bus.publish(f"task.{task.id}.complete", result)
```

---

#### 模块 2：TaskDAGBuilder（DAG 任务图构建器）

```python
# agent/multi_agent/task_dag.py

class TaskNode:
    id: str
    description: str
    assigned_to: str | None      # agent名
    dependencies: list[str]      # 前置任务ID列表
    status: str                  # pending/running/done/failed
    result: Any | None
    resource_needs: dict         # {"wood": 10, "crafting_table": 1}
    resource_provides: dict      # {"planks": 20}

class TaskDAGBuilder:
    async def build(self, mission: str, world_state: dict) -> DAG:
        # 调用 LLM，一次生成完整的任务依赖图
        response = await llm.call(
            system=DAG_BUILD_SYSTEM_PROMPT,
            user=f"""
            任务: {mission}
            当前世界状态: {world_state}
            
            输出格式（JSON）:
            {{
                "tasks": [
                    {{
                        "id": "t1",
                        "description": "采集20个橡木",
                        "dependencies": [],
                        "resource_needs": {{}},
                        "resource_provides": {{"oak_log": 20}},
                        "estimated_agent_role": "lumberjack"
                    }},
                    {{
                        "id": "t2",
                        "description": "合成40个木板",
                        "dependencies": ["t1"],
                        "resource_needs": {{"oak_log": 10}},
                        "resource_provides": {{"oak_planks": 40}},
                        "estimated_agent_role": "crafter"
                    }}
                ]
            }}
            """
        )
        return DAG.from_json(response)
```

---

#### 模块 3：WorkerAgent（工作者 Agent）

```python
# agent/multi_agent/worker_agent.py

class WorkerAgent:
    """
    改造自现有 VoyagerAgent，扩展以下能力：
    1. 支持 agent_role（角色特化）
    2. 支持跨 agent 通信（请求资源/报告状态）
    3. 共享技能库和 RAG
    """
    
    def __init__(self, agent_id: int, role: str = "generalist"):
        self.id = f"Agent{agent_id}"
        self.role = role  # lumberjack / miner / builder / crafter
        self.env = MineflayerEnv(port=3000 + agent_id)  # 独立端口
        
        # 共享组件（所有agent共用同一个库）
        self.skill_library = SharedSkillLibrary()  # 单例，共享
        self.rag = SharedRAGRetriever()             # 单例，共享
        
        # 独立组件（每个agent各自维护）
        self.memory = AgentMemory(agent_id=self.id)
        self.react = ReActEngine(self.env, self.skill_library, self.rag)
    
    async def execute(self, task: TaskNode, state_manager: GlobalStateManager):
        # 检查物品依赖（前置任务是否真的提供了所需资源）
        missing = self._check_resources(task.resource_needs, state_manager)
        if missing:
            # 等待或请求其他 agent 转移资源
            await self._request_resources(missing, state_manager)
        
        # 执行（复用现有 _run_task 逻辑）
        result = await self.react.run_task(task.description)
        
        # 上报状态给全局
        await state_manager.report_completion(self.id, task, result)
        return result
    
    async def _request_resources(self, missing: dict, state_manager):
        """
        向消息总线发出资源请求，等待其他 agent 响应
        """
        request = ResourceRequest(
            requester=self.id,
            items=missing,
            location=await self.env.observe().position
        )
        await state_manager.message_bus.publish("resource.request", request)
        
        # 等待其他 agent 送来物品（或超时）
        await asyncio.wait_for(
            state_manager.message_bus.wait("resource.delivered", filter=self.id),
            timeout=120.0
        )
```

---

#### 模块 4：GlobalStateManager（全局状态管理器）

```python
# agent/multi_agent/state_manager.py

class GlobalStateManager:
    """
    维护全局共享状态，解决多 Agent 协调中的信息孤岛问题
    对应 VillagerAgent 的 State Manager
    """
    
    def __init__(self):
        self._lock = asyncio.Lock()
        self.world_state = {
            "resource_inventory": {},    # 各 agent 持有的资源汇总
            "placed_blocks": [],         # 已放置的方块
            "completed_tasks": [],       # 已完成任务
            "agent_positions": {},       # 各 agent 当前坐标
            "agent_status": {},          # idle/busy/error
        }
        self.message_bus = MessageBus()
    
    async def update(self, agent_id: str, changes: dict):
        async with self._lock:
            # 原子更新，避免并发冲突
            deep_merge(self.world_state, changes)
            self.world_state["agent_status"][agent_id] = changes.get("status", "idle")
    
    async def get_world_state(self) -> dict:
        # 从所有 Mineflayer Bot 拉取最新状态
        states = await asyncio.gather(*[
            agent.env.observe() for agent in self.agents
        ])
        return merge_observations(states)
```

---

#### 模块 5：MessageBus（Agent 间通信总线）

```python
# agent/multi_agent/message_bus.py

class MessageBus:
    """
    轻量级发布-订阅，用于 Agent 间协调
    不需要引入 Kafka/RabbitMQ，asyncio.Queue 即可
    """
    
    def __init__(self):
        self._subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)
    
    async def publish(self, topic: str, message: Any):
        for queue in self._subscribers.get(topic, []):
            await queue.put(message)
        
        # 也支持通配符订阅（如 "task.*"）
        for pattern, queues in self._subscribers.items():
            if fnmatch(topic, pattern):
                for queue in queues:
                    await queue.put(message)
    
    def subscribe(self, topic: str) -> asyncio.Queue:
        queue = asyncio.Queue()
        self._subscribers[topic].append(queue)
        return queue
    
    async def wait(self, topic: str, filter: str = None, timeout: float = 60):
        queue = self.subscribe(topic)
        msg = await asyncio.wait_for(queue.get(), timeout=timeout)
        return msg
```

---

### 6.4 Mineflayer 侧扩展

多 Bot 支持，每个 Agent 对应独立的 Mineflayer 实例：

```javascript
// mineflayer/server_manager.js（新增）

const AGENTS = [
    { port: 3000, username: "Agent1" },
    { port: 3001, username: "Agent2" },
    { port: 3002, username: "Agent3" },
];

// 为每个 agent 启动独立 HTTP 服务和 Bot 实例
AGENTS.forEach(({ port, username }) => {
    const app = createMineflayerApp({ username });
    app.listen(port, () => console.log(`${username} on :${port}`));
});

// 新增：agent间物品传递接口
app.post('/transfer_item', async (req, res) => {
    const { item, count, target_agent_port } = req.body;
    // 1. 当前 bot 移动到目标 bot 附近
    // 2. 执行 bot.toss(itemType, null, count)
    // 3. 目标 bot 自动捡起（mineflayer 监听 itemDrop 事件）
});
```

---

### 6.5 升级路线图（分三期）

#### 第一期：基础多 Agent（2-3周）

目标：让 2 个 Bot 能并行执行独立任务

```
工作项：
1. [ ] 重构 MineflayerEnv，支持 port 参数（多实例）
2. [ ] 新增 AgentRegistry：管理 agent 生命周期
3. [ ] 新增 GlobalController：简单版（无DAG，Round-robin分发）
4. [ ] 共享技能库：SkillLibrary 改为单例+线程锁
5. [ ] mineflayer/index.js 改为支持多实例启动

验证：两个Bot同时分别执行"挖木头"和"挖石头"，互不干扰
```

#### 第二期：DAG 任务依赖（3-4周）

目标：能处理有依赖关系的复杂任务

```
工作项：
1. [ ] 实现 TaskDAGBuilder（LLM生成DAG）
2. [ ] 实现 DAG 拓扑排序和就绪任务计算
3. [ ] 实现 GlobalStateManager（共享状态）
4. [ ] 实现 MessageBus（发布订阅）
5. [ ] WorkerAgent 支持等待前置依赖

验证："建一栋房子" → 自动分解为采木/合成/建造，两个Bot协作
```

#### 第三期：角色特化与资源协调（2-3周）

目标：Agent 有专职角色，能主动交换资源

```
工作项：
1. [ ] Agent 角色系统（lumberjack/miner/builder/crafter）
2. [ ] 角色特化的技能库过滤（miner优先检索挖矿技能）
3. [ ] 物品传递协议（Mineflayer toss/pickup）
4. [ ] 资源请求/响应协议（ResourceRequest消息）
5. [ ] 全局任务监控面板（可选，Web UI）

验证："建村庄（5栋房子+农田）" → 3个Bot分工协作，效率对比单Bot
```

---

### 6.6 关键架构决策

| 决策点 | 推荐方案 | 理由 |
|--------|---------|------|
| 技能库共享 | 单个 ChromaDB 实例，加读写锁 | 共享学习成果，避免重复学习 |
| 状态同步频率 | 每个任务步骤结束时更新 | 平衡一致性和性能 |
| Agent 间通信 | asyncio MessageBus（进程内） | 低延迟，无外部依赖 |
| 任务图构建 | 每次 LLM 生成（而非硬编码） | 泛化能力强，支持任意任务 |
| Bot 数量上限 | 建议 2-4 个 | 更多 Bot 对 MC 服务器性能压力大 |
| 失败处理 | Agent失败→重新分配给其他Agent | 容错性，参考 VillagerAgent |

---

### 6.7 与现有代码的兼容性

升级最大的优势：**现有单 Agent 路径完全保留**，多 Agent 是在其之上的扩展层：

```
用户输入
    │
    ├─ 简单任务（SIMPLE）→ 现有 VoyagerAgent（单Bot）
    │
    └─ 复杂任务（COMPLEX）→ GlobalController → 多 WorkerAgent 协作
                               │
                               └─ 每个 WorkerAgent 内部仍使用
                                  现有的 react_agent + critic + skill_library
```

改动最小的文件：`main.py`（仅需在入口增加多 Agent 初始化判断），现有所有 `agent/` 内文件保持不变或仅微小扩展。

---

## 总结

| 维度 | 现状 | 优化后（单Agent） | Multi-Agent 升级后 |
|------|------|----------------|-------------------|
| 单任务延迟 | 30-300s | 15-100s（合并调用） | 相同（单任务不变）|
| 并行任务 | 串行，1个 | 串行，1个 | 并行，2-4个 |
| 复杂任务完成率 | ~60% | ~75%（更好分解） | ~85%（分工协作） |
| 技能库质量 | 逐渐劣化 | 版本管理，可控 | 更快积累，共享学习 |
| 可扩展性 | 单Bot上限 | 单Bot上限 | 水平扩展，加Bot即可 |

> 本报告分析基于 README、项目结构、已知问题文档，以及 VillagerAgent（ACL 2024）等相关工程研究综合整理。
