# Minecraft Agent 性能优化综合方案

**基于代码架构分析 + 运行日志诊断的深度优化报告**

> 项目：chunblam/Minecraft_agent ｜ 分析日期：2026-03-11 ｜ 日志：run_2026-03-10_22-27-33

---

## 一、问题诊断：从日志数据出发

通过阅读完整运行日志与所有核心模块源码（`llm_router.py`、`react_agent.py`、`retriever.py`、`critic.py`、`plan_executor.py`、`prompts.py` 等），可以将问题精确定位到以下两个层次：

### 1.1 LLM 调用时序分析（实测数据）

| 调用环节 | 实测延迟 | 频率 | 症结 |
|---|---|---|---|
| RAG 查询分类 | ~5s | 每次任务 | 每次都走 LLM，缓存未命中时必等 |
| 意图分类 | ~4.7s | 每次任务 | 独立 LLM 请求，可合并 |
| 复杂度分析 | ~1s | 每次任务 | 独立 LLM 请求，可合并 |
| 代码生成（首次） | **21s** | 每次任务 | SiliconFlow 高峰期排队 |
| 代码生成（重试） | **56s** | 每次失败 | 越重试越慢，无差异重试策略 |
| Critic 评估 | 4–90s | 每次尝试 | 超时直接返回空，浪费一次机会 |
| **总冷启动（简单任务）** | **30s+** | 每次 | 串行叠加，用户体验差 |

> ⚠️ **核心症结：所有调用走同一个入口，同一个模型**
>
> `llm_router.py` 中 `think()`、`think_fast()`、`classify()` 三个方法实际上调用的是完全相同的 `deepseek-ai/DeepSeek-V3` 模型，没有任何差异化。意图分类这种只需输出一个词的轻量任务，与需要生成完整 JS 函数的代码生成任务，消耗着同等量级的资源和等待时间。SiliconFlow 在高峰期会对大模型请求做排队，导致同一个任务的不同重试可以出现 21s → 56s 的指数级劣化。

---

### 1.2 架构层面的串行调用链

从 `react_agent.py` 的 `run()` 方法可以还原出完整的执行调用链：

```
# 当前实际执行顺序（全串行，无法并发）
用户输入
  → [LLM-1] _classify_intent()       # ~4.7s  意图识别
  → [LLM-2] _classify_complexity()    # ~1.0s  复杂度分析
  → [并发] _build_context()
       ├─ [LLM-3] RAG QueryClassifier  # ~5.0s  语义分类
       ├─ [Vec]   ChromaDB 向量检索    # <0.1s
       └─ [Mem]   记忆检索             # <0.1s
  → [LLM-4] 代码生成                  # ~21–56s
  → [Exec]  Node.js execute_code      # ~10–20s
  → [LLM-5] Critic 评估               # ~4–90s
  ─────────────────────────────────────────────
  总计：简单任务冷启动 ≥ 30s，重试叠加 ≥ 80s
```

---

### 1.3 任务可行性盲区（无效重试浪费）

日志 `23:07–23:12` 段落完整记录了一次典型的失效场景：玩家要求切换到旁观者模式，但 Bot 没有 OP 权限。Agent 对此没有任何预检机制，照常走完完整的 CodeLoop：3 次代码生成 + 3 次 Critic 评估，累计浪费约 2 分钟 API 调用时间，最终仍然失败。

> 💡 **根因总结**
>
> （1）所有 LLM 调用使用同一个大模型，轻量任务没有快速路径。（2）意图分类和复杂度分析是两次独立的串行 LLM 调用，可合并为一次。（3）RAG 分类在缓存未命中时必须等待完整的大模型响应。（4）没有任务可行性预检，无法拦截不可能成功的任务。（5）代码生成重试策略无差异化，模型经常生成几乎相同的代码。

---

## 二、综合优化方案（8 项，按优先级排列）

| # | 方案 | 预期加速 | 精度提升 | 实施难度 | 优先级 |
|---|---|---|---|---|---|
| 1 | 分层模型路由（快/慢分离） | 节省 10–15s/任务 | ★★★ | ⭐⭐（中） | 🔴 最高 |
| 2 | 合并串行分类调用 | 节省 6–10s/任务 | ★★ | ⭐（简单） | 🔴 最高 |
| 3 | 任务可行性预检（规则层） | 消除无效重试 | ★★★★ | ⭐（简单） | 🔴 最高 |
| 4 | RAG 分类结果本地化（规则+缓存） | 节省 4–5s/任务 | ★★ | ⭐（简单） | 🟡 高 |
| 5 | Prompt Caching（静态上下文） | 代码生成快 30–50% | ★ | ⭐⭐（中） | 🟡 高 |
| 6 | 差异化重试 Prompt | 减少重试次数 ~50% | ★★★★ | ⭐（简单） | 🟡 高 |
| 7 | Critic 轻量规则化 | 节省 4–16s/次 | ★★★ | ⭐⭐（中） | 🟢 中 |
| 8 | 流式代码生成（streaming） | TTFB 从 20s→2s | — | ⭐⭐（中） | 🟢 中 |
| **合计** | **综合实施后预期效果** | **简单任务：30s+ → 5–8s** | **复杂任务首次成功率 ↑70%** | | |

---

### 方案一：分层模型路由（最高优先级）

现有 `LLMRouter` 的三个方法（`think` / `think_fast` / `classify`）实质等价。改造方向：按任务类型路由到不同模型，用小模型处理轻量决策，大模型只用于代码生成。

```python
# agent/llm_router.py — 改造方案

class LLMRouter:
    def __init__(self) -> None:
        # 快速分类模型：Qwen2.5-7B，SiliconFlow上延迟0.5-2s
        self.fast_client = AsyncOpenAI(
            api_key=os.getenv('SILICONFLOW_API_KEY'),
            base_url='https://api.siliconflow.cn/v1'
        )
        self.fast_model = 'Qwen/Qwen2.5-7B-Instruct'  # 轻量任务

        # 代码生成模型：DeepSeek官方API（稳定低延迟）
        self.code_client = AsyncOpenAI(
            api_key=os.getenv('DEEPSEEK_OFFICIAL_API_KEY'),
            base_url='https://api.deepseek.com'
        )
        self.code_model = 'deepseek-chat'  # 代码生成专用

    async def classify(self, system, user, temperature=0.3) -> str:
        # 分类任务 → 小模型，~0.5–2s
        return await self._call(self.fast_client, self.fast_model, system, user, temperature)

    async def think_fast(self, system, user, temperature=0.7) -> str:
        # 推理/规划 → 大模型（DeepSeek官方）
        return await self._call(self.code_client, self.code_model, system, user, temperature)

    async def think(self, system, user, temperature=0.3) -> str:
        # 代码生成 → 同大模型
        return await self._call(self.code_client, self.code_model, system, user, temperature)
```

这一改动将意图分类、复杂度分析、Critic 评估、RAG 分类全部切换到小模型，单次延迟从 4–5s 降至 0.5–1.5s，且 SiliconFlow 小模型排队概率极低。代码生成切换到 DeepSeek 官方 API，P95 延迟在 10s 以内。

---

### 方案二：合并串行分类调用（最高优先级）

`react_agent.py` 中 `_classify_intent()` 和 `_classify_complexity()` 是两次独立的 LLM 请求，合并为一次可节省 5–7s，且逻辑更内聚。

```python
# agent/react_agent.py — 合并 intent + complexity

UNIFIED_CLASSIFY_PROMPT = '''Analyze the Minecraft player message.
Output JSON only:
{
  "intent": "task_execution|knowledge_qa|chat",
  "complexity": "simple|complex",
  "feasible": true/false,
  "reason": "if not feasible, explain why (op required, impossible, etc.)"
}
complex = requires 3+ dependent steps (smelt+craft+build)
infeasible = requires OP/admin, or physically impossible for bot'''

async def _classify(self, message: str, game_state: dict) -> dict:
    state_hint = f'isOp={game_state.get("isOp", False)}'
    raw = await self.llm.classify(
        system_prompt=UNIFIED_CLASSIFY_PROMPT,
        user_prompt=f'Message: {message}\nState: {state_hint}',
        temperature=0.1
    )
    try:
        return json.loads(raw.strip())
    except Exception:
        return {'intent': 'task_execution', 'complexity': 'simple', 'feasible': True}

# 在 run() 中替换两次调用：
# 原来：intent = await self._classify_intent(msg)
#       complexity = await self._classify_complexity(msg, gs)
# 改为：
classify_result = await self._classify(player_message, game_state)
intent     = classify_result['intent']
complexity = classify_result['complexity']
feasible   = classify_result.get('feasible', True)
if not feasible:
    reason = classify_result.get('reason', '无法执行此操作')
    return {'display_message': f'⚠️ {reason}', 'action_type': 'chat'}
```

---

### 方案三：任务可行性预检（最高优先级）

在进入 CodeLoop 之前加一层轻量规则检查，拦截已知不可能成功的任务类别，避免 4 次无效的 LLM 调用 + 执行循环。

```python
# agent/feasibility.py — 新增模块

import re
from dataclasses import dataclass

@dataclass
class FeasibilityResult:
    feasible: bool
    reason: str = ''

# 规则表：pattern → checker(game_state) → infeasible_reason
_RULES = [
    # OP 权限类
    (r'(spectator|creative|survival|adventure)\s*(mode|模式)',
     lambda gs: not gs.get('isOp', False),
     '切换游戏模式需要 OP 权限'),
    (r'(ban|kick|op|deop)\s+\w+',
     lambda gs: not gs.get('isOp', False),
     '管理员命令需要 OP 权限'),
    (r'(give|gamemode)\s+\w+',
     lambda gs: not gs.get('isOp', False),
     '此命令需要 OP 权限'),
    # 资源不足类（可扩展）
    (r'(smelt|冶炼).*(without|没有).*(fuel|fuel|木炭|coal)',
     lambda gs: True,
     '冶炼需要燃料，请先准备木炭或煤'),
]

def check_feasibility(task: str, game_state: dict) -> FeasibilityResult:
    task_lower = task.lower()
    for pattern, should_block, reason in _RULES:
        if re.search(pattern, task_lower, re.IGNORECASE):
            if should_block(game_state):
                return FeasibilityResult(feasible=False, reason=reason)
    return FeasibilityResult(feasible=True)

# react_agent.py _run_task() 开头加入：
from .feasibility import check_feasibility

async def _run_task(self, task, game_state, critique='', context=''):
    # ★ 新增：可行性预检
    feasibility = check_feasibility(task, game_state)
    if not feasibility.feasible:
        logger.warning(f'[Feasibility] 任务不可行: {feasibility.reason}')
        return {'display_message': f'⚠️ {feasibility.reason}', ...}
    # 后续原有逻辑...
```

---

### 方案四：RAG 分类本地化（高优先级）

`retriever.py` 中的 `QueryClassifier` 已实现了 LRU 缓存，但缓存 key 是纯文本精确匹配。对于「帮我砍木头」和「帮我砍5个木头」，会产生两次独立的 LLM 调用。建议在 LLM 分类前加一层本地规则快速路径：

```python
# rag/retriever.py QueryClassifier.classify() 方法改造

# 本地关键词快速路由（毫秒级，无 LLM 调用）
_LOCAL_RULES = {
    'mc_base': ['砍', '挖', '采', '木头', '石头', 'log', 'stone', 'mine',
                'craft', '合成', '制作', 'move', '移动', '跑', '走'],
    'mc_brewing': ['药水', 'potion', '酿造', 'brewing'],
    'mc_enchanting': ['附魔', 'enchant', '经验'],
    'mc_trading': ['交易', 'trade', '村民', 'villager'],
    'mc_combat': ['战斗', 'fight', 'kill', '击杀', '怪物', 'mob'],
}

def _local_classify(self, query: str) -> list[str] | None:
    q = query.lower()
    for col, kws in _LOCAL_RULES.items():
        if any(kw in q for kw in kws):
            return [col]
    return None  # 无法本地分类，走 LLM

async def classify(self, query: str, max_collections: int = 3) -> list[str]:
    cache_key = query.strip().lower()
    if cache_key in self._cache:
        return self._cache[cache_key]

    # ★ 新增：本地规则快速路径
    local_result = self._local_classify(query)
    if local_result:
        self._cache[cache_key] = local_result
        return local_result

    # 兜底走 LLM 分类（保留原有逻辑）
    result = await self._llm_classify(query, max_collections)
    self._cache[cache_key] = result
    return result
```

---

### 方案五：Prompt Caching（静态上下文缓存）

代码生成的 `CODE_GENERATION_SYSTEM_PROMPT` 包含大量静态的 Mineflayer API 文档，每次调用都重新发送，既消耗 token 计费又影响速度。如果切换到 Anthropic Claude API（Haiku/Sonnet），可以使用 `cache_control` 标记静态部分，首次缓存后后续调用速度提升 30–50%，且成本降低 90%。

```python
# 使用 Anthropic SDK 的 Prompt Caching 示例
# (适配 llm_router.py，添加 Claude 客户端支持)

from anthropic import AsyncAnthropic

self.claude = AsyncAnthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))

async def think_with_cache(self, system_prompt: str, user_prompt: str) -> str:
    response = await self.claude.messages.create(
        model='claude-haiku-4-5-20251001',  # Haiku: 极快 + 低成本
        max_tokens=4096,
        system=[
            {
                'type': 'text',
                'text': system_prompt,      # 静态 API 文档等
                'cache_control': {'type': 'ephemeral'}  # ★ 缓存这部分
            }
        ],
        messages=[
            {'role': 'user', 'content': user_prompt}  # 动态：任务+错误
        ]
    )
    return response.content[0].text

# 对代码生成场景效果最显著：
# CODE_GENERATION_SYSTEM_PROMPT 约 800 token（每次缓存后免费复用）
# 实际计费 token 减少 ~85%，速度提升 ~40%
```

---

### 方案六：差异化重试 Prompt（高优先级）

日志显示第 2 次重试的代码与第 1 次高度相似（`setSpectatorMode` 函数几乎完全一样）。这是因为重试时只是把 error message 附加到 prompt，模型没有足够的约束去尝试不同方法。建议在 `_build_code_prompt()` 中针对重试轮次做差异化处理：

```python
# agent/react_agent.py — _build_code_prompt() 改造

def _build_code_prompt(self, task, game_state, context,
                       last_code, last_error, last_output,
                       critique, attempt=1) -> str:

    # 首次：正常生成
    if attempt == 1 or last_error == 'N/A':
        retry_instruction = ''

    # 第2次：强制换思路
    elif attempt == 2:
        retry_instruction = f'''
⚠️ RETRY #{attempt}: Previous approach failed with: {last_error[:200]}
REQUIRED: Use a COMPLETELY DIFFERENT strategy. Do NOT repeat the same approach.
If pathfinding failed → try moving closer first, then mine.
If item not found → explore in different direction before retrying.'''

    # 第3次+：更激进的容错
    else:
        retry_instruction = f'''
⚠️ RETRY #{attempt}: Two previous approaches failed.
REQUIRED: Use the most defensive/simple approach possible.
- Add explicit distance checks before mineBlock
- Add inventory checks before crafting
- Use exploreUntil() if resource not found within 32 blocks
- Add try/catch around each major operation'''

    return CODE_GENERATION_HUMAN_TEMPLATE.format(
        ...,
        critique=f'{critique}\n{retry_instruction}'.strip(),
    )
```

---

### 方案七：Critic 轻量规则化（中优先级）

Critic 是整个链路中延迟最不稳定的环节（4s–90s，甚至超时）。对于大量任务，成功与否其实可以通过规则直接判断，无需动用 LLM：

```python
# agent/critic.py — 在 LLM 调用前加规则快速判断

def _rule_check(self, task: str, game_state: dict, output: str) -> tuple[bool, str] | None:
    '''快速规则判断，返回 None 表示需要 LLM 判断'''
    task_lower = task.lower()
    inv = {item['name']: item['count']
           for item in game_state.get('inventory', [])}

    # 检测「获取N个X」类任务
    m = re.search(r'(get|mine|chop|collect|获取|砍|挖|采集).*?(\d+).*?([a-z_\u4e00-\u9fff]+)', task_lower)
    if m:
        count, item = int(m.group(2)), m.group(3)
        # 尝试匹配背包中的物品
        matching = sum(v for k, v in inv.items() if item.replace('个','') in k)
        if matching >= count:
            return True, f'已确认背包中有 {matching} 个 {item}'

    # 检测执行输出中的成功标记
    if 'Successfull' in output or 'success' in output.lower():
        if 'error' not in output.lower() and 'fail' not in output.lower():
            return True, '执行输出显示成功'

    # 检测执行输出中的明确失败
    if 'requires operator' in output.lower() or 'permission' in output.lower():
        return False, '权限不足，无法完成'

    return None  # 无法规则判断，走 LLM

async def check_task_success(self, task, game_state, last_observation, max_retries=3):
    # ★ 先走规则判断
    rule_result = self._rule_check(task, game_state, last_observation)
    if rule_result is not None:
        success, critique = rule_result
        logger.info(f'[Critic:Rule] {task[:40]} → {success}')
        return success, critique
    # 规则无法判断，降级到 LLM（使用小模型）
    ...
```

---

### 方案八：代码生成流式输出（中优先级）

代码生成平均需要 20s+，但用户在整个等待期间没有任何反馈。启用 streaming 后，第一个 token 通常在 1–2s 内到达，可以实时在 MC 聊天栏显示生成进度，大幅改善主观体验。

```python
# agent/llm_router.py — 新增 stream_think() 方法

async def stream_think(self, system_prompt, user_prompt,
                       temperature=0.3, on_chunk=None) -> str:
    '''流式代码生成，on_chunk 回调可实时反馈进度'''
    full_text = ''
    async with await self.code_client.chat.completions.create(
        model=self.code_model,
        messages=[
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        temperature=temperature,
        stream=True,
    ) as stream:
        async for chunk in stream:
            delta = chunk.choices[0].delta.content or ''
            full_text += delta
            if on_chunk:
                await on_chunk(delta)  # 回调：可发送进度给 MC 聊天
    return full_text

# react_agent.py 中使用示例：
chars_received = 0
async def progress_cb(delta):
    nonlocal chars_received
    chars_received += len(delta)
    if chars_received % 200 == 0:  # 每200字符报告一次
        await env.chat(f'⏳ 正在生成代码... ({chars_received} chars)')

raw = await self.llm.stream_think(
    system_prompt=CODE_GENERATION_SYSTEM_PROMPT,
    user_prompt=prompt,
    temperature=0.3,
    on_chunk=progress_cb,
)
```

---

## 三、实施路线图

### 第一阶段（1–2天）：立竿见影的高优改造

以下改动无需重构架构，改动范围小，风险低，效果最显著：

1. 注册 DeepSeek 官方 API（`api.deepseek.com`），替换代码生成调用端点
2. 在 SiliconFlow 上开通 `Qwen2.5-7B-Instruct`，用于分类和 Critic
3. `llm_router.py`：添加双客户端，`classify()` 走小模型，`think()` 走大模型
4. `react_agent.py`：合并 `_classify_intent()` + `_classify_complexity()` 为单次调用
5. 新建 `feasibility.py`，在 `_run_task()` 入口处加可行性预检
6. `rag/retriever.py`：在 LLM 分类前加本地关键词规则快速路径

### 第二阶段（3–5天）：精度与体验提升

1. `react_agent.py`：`_build_code_prompt()` 按 `attempt` 轮次差异化重试 prompt
2. `critic.py`：加规则快速判断层，减少 Critic 的 LLM 调用频率
3. `llm_router.py`：添加 `stream_think()` 流式接口，在 MC 聊天栏实时显示进度
4. 环境变量配置：将所有 API key 和模型名称迁移到 `.env` 文件，方便切换

### 第三阶段（可选，长期优化）

1. 评估切换到 Anthropic Claude API（`claude-haiku-4-5-20251001` 做 Critic，`claude-sonnet-4-6` 做代码生成）
2. 实现 Prompt Caching（需 Claude API 或支持 prefix caching 的提供商）
3. `plan_executor.py` 已有 TEMPLATE/PLAN/REACT 三级路由，优先激活 TEMPLATE 模式减少代码生成调用
4. 技能库预热：启动时加载高频任务的缓存代码，直接执行不走 LLM

---

## 四、环境变量配置参考

```bash
# .env — 推荐配置（第一阶段实施后）

# SiliconFlow（轻量任务：分类/Critic）
SILICONFLOW_API_KEY=sk-xxx
SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1
RAG_CLASSIFY_MODEL=Qwen/Qwen2.5-7B-Instruct

# DeepSeek 官方（代码生成主力）
DEEPSEEK_OFFICIAL_API_KEY=sk-xxx
DEEPSEEK_OFFICIAL_BASE_URL=https://api.deepseek.com
DEEPSEEK_V3_MODEL=deepseek-chat

# （可选）Anthropic Claude（最稳定，Prompt Caching 支持）
ANTHROPIC_API_KEY=sk-ant-xxx

# Agent 参数
MAX_TASK_RETRIES=3          # 从4改为3，配合差异化重试减少浪费
CODE_TIMEOUT_MS=180000      # 从5分钟改为3分钟
DEEPSEEK_LLM_TIMEOUT=45    # 从90s改为45s，更快失败重试
```

---

## 五、API 提供商对比

| API 提供商 | P50 延迟 | 稳定性 | 成本 | 推荐场景 |
|---|---|---|---|---|
| SiliconFlow（现在） | 5–20s | ★★☆（高峰差） | 低 | 大量测试、非实时场景 |
| DeepSeek 官方 | 3–8s | ★★★ | 中 | 代码生成主力（推荐） |
| Qwen2.5-7B（SF） | **0.5–2s** | ★★★★ | 极低 | 分类/Critic 轻量任务 |
| Claude 3.5 Haiku | 1–3s | ★★★★★ | 中低 | Critic + 分类（极稳定） |
| Claude 3.5 Sonnet | 3–6s | ★★★★★ | 中高 | 代码生成质量最佳 |

---

## 六、常见问题解答

**Q：换 DeepSeek 官方 API 一定会变快吗？**

基本确定。SiliconFlow 是国内聚合平台，在高峰期会对 DeepSeek-V3 请求做负载排队，日志中出现的 56s 和 90s 超时正是这种排队导致的。DeepSeek 官方 API 对自家模型有更高优先级，P95 延迟通常在 10s 以内。代价是价格略高（约 SiliconFlow 的 1.5–2 倍），但从项目体验来说完全值得。

**Q：Qwen2.5-7B 能否胜任分类和 Critic 任务？**

完全可以。意图分类（输出 `task_execution/chat` 等几个词）、复杂度分析（`simple/complex`）、以及 Critic 的 JSON 格式输出（`{"success": true/false}`）都是结构极其简单的任务，7B 模型的准确率不低于大模型，但延迟只有 0.5–1.5s，且几乎不会被排队。

**Q：`plan_executor.py` 已经实现了三级路由，为什么没有效果？**

查看 `react_agent.py` 的 `run()` 方法，`_run_task()` 依然走旧的 CodeLoop 路径，`plan_executor.py` 的 `PlanExecutor` 类尚未在主流程中启用（`react_agent.py` 没有 import 和调用它）。建议第三阶段将 `PlanExecutor` 接入 `_run_task()`，特别是 TEMPLATE 模式，让高频任务直接复用技能库代码而不走 LLM。

**Q：任务拆解（TaskPlanner）不准怎么解决？**

`planner.py` 的 `PLANNER_SYSTEM_PROMPT` 已经相当完善，主要问题是 LLM 对 Minecraft 游戏逻辑理解不够深。建议在 RAG 知识库中增加「任务分解示例」类文档（Few-Shot Examples），例如「制作铁剑」→「1. 采集铁矿 2. 冶炼铁锭 3. 合成铁剑」的标准拆解样例。这样 RAG 会在分解前注入相关示例，显著提升拆解准确率。

---

*本报告基于项目源码深度分析生成 ｜ chunblam/Minecraft_agent ｜ 2026-03-11*
