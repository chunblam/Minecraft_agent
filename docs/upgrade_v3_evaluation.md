# 分层执行升级（v3）方案评估与测试效果预估

本文档对「分层路由 + Node.js 批量执行 + RAG 接入」升级方案做总体评估，并从多种可能情况分类讨论**任务拆解、任务执行、技能使用**的预期效果，并与原版 Voyager 论文实现对比。

---

## 一、方案总览与架构评估

### 1.1 核心改动是否到位

| 设计目标 | 实现方式 | 评估 |
|----------|----------|------|
| 三档路由 | `PlanExecutor.route_execution_mode()` → TEMPLATE / PLAN / REACT | ✅ 关键词 + 模板技能匹配，逻辑清晰 |
| 模板优先（0 LLM） | 命中参数化技能 → `execute_skill_template()` → HTTP 单步 | ✅ 与现有 skill_executor 一致 |
| 计划批量执行（1 LLM） | `_generate_plan()` → `_batch_execute()` → POST `/execute_plan` | ✅ 计划在 Node 内连续执行，无 Python 往返 |
| 失败降级 ReAct | 计划失败或强制 REACT 关键词 → `react_step_fn` | ✅ 与原有 ReAct 循环衔接 |
| RAG 注入 | `_build_context()` 三路并发（技能 + RAG + 记忆），planner.decompose(retriever=) | ✅ 分解与执行均可带知识库 |
| 自主探索 | `run_autonomous()` + AUTONOMOUS_* prompt，空闲 N 秒触发 | ✅ 可选，不干扰主流程 |

### 1.2 潜在风险与依赖

- **Node 端**：依赖 `/execute_plan`、`/execute_script` 与 `evaluatePlanCondition` 已正确插入 `index.js`，且 `executeAction` 支持 `collect_block`、`equip_item`、`eat_food`，否则计划中的这些步骤会报错。
- **计划 JSON**：LLM 输出必须可被 `_parse_plan_json` 解析；若经常带 markdown 或多余文字，需依赖正则/清洗，仍有解析失败概率。
- **ReAct 与 env**：ReAct 兜底使用 `env.step(action_type, action_params)`，需保证 `MineflayerEnv.step()` 与 `send_action` 行为一致（已做兼容）。
- **记忆接口**：`memory.get_relevant_context(task)` 与现有 `MemoryManager.get_relevant_context` 一致，无需改。

---

## 二、按场景分类的预期效果

### 2.1 简单、步骤可预测（如「挖 5 个木头」「合成工作台」）

- **路由**：多数走 **PLAN**（无匹配模板时）或 **TEMPLATE**（若有现成参数化技能如「采集 N 个 block_type」）。
- **任务拆解**：简单任务不会进层级分解，直接进 `_run_task`；拆解质量不敏感。
- **执行**：
  - **TEMPLATE**：0 次额外 LLM，直接按 procedure 执行，延迟最低，**预期成功率高**（只要技能库里有对应模板且参数可解析）。
  - **PLAN**：1 次 LLM 生成 5–10 步计划，Node 批量执行；若 LLM 给出的动作类型与参数正确（如 `get_inventory` → `collect_block` → `craft_item` → `finish`），**流畅度明显优于原 ReAct 多步**；若某步参数错误（如坐标瞎写、物品名错误），该步失败后触发 **replan**，最多 2 次，再失败则降级 ReAct。
- **技能使用**：模板命中时直接复用；未命中时计划中不会显式「调用技能」，但 context 里带技能描述，LLM 可能模仿步骤顺序。
- **整体**：在「动作集与 prompt 描述一致、RAG 有基础合成/采集知识」的前提下，**成功率和流畅度都会优于升级前纯 ReAct**；反之若 LLM 常生成错误 action_type 或参数，replan 与 ReAct 兜底会拉高延迟。

### 2.2 复杂、多子任务（如「造一把铁镐」「建一个简易农场」）

- **路由**：先被判为 **complex**，走 `_run_hierarchical`；子任务再各自走 TEMPLATE / PLAN / REACT。
- **任务拆解**：由 `TaskPlanner.decompose(task, game_state, retriever=self.retriever)` 完成；**RAG 注入**可使子任务更符合 MC 设定（如合成链、材料顺序）。若 RAG 质量高，分解更合理；若 LLM 本身分解就乱，RAG 只能部分纠正。
- **执行**：每个子任务独立执行，成功则更新 `game_state` 再执行下一子任务；某一子任务多次失败会终止后续（除非 `can_skip_if`）。**预期**：子任务粒度合适时，整体完成率中等偏上；若某一步依赖上一步的精确状态（如「放工作台」的坐标），而 state 更新滞后，可能出错，需依赖 Critic 与重试。
- **技能使用**：层级成功后会把「任务 + 子任务摘要 + 轨迹」存成技能；后续类似复杂任务可检索到「造铁镐」等描述，辅助分解与计划生成，**长期会越用越顺**。

### 2.3 高不确定性（如「遇到苦力怕就跑」「探索并找钻石」）

- **路由**：含「战斗」「攻击」「逃跑」「探索未知」等关键词会强制 **REACT**。
- **任务拆解**：若整句被判 complex 会先分解，但子任务仍可能走 ReAct；若判 simple 则直接 ReAct。
- **执行**：与升级前一致，每步 LLM → 执行 → 观察，**适应性最强**，但延迟和调用次数最高。**预期**：行为与旧版 ReAct 相当，依赖模型单步决策质量。
- **技能使用**：多为「参考 context 里的技能描述」做模仿，无模板命中；探索类技能难以参数化，复用有限。

### 2.4 知识问答与闲聊

- **意图**：`_classify_intent` 非 task_execution 时走 `chat()`。
- **RAG**：闲聊分支会拉 RAG（`retriever.search`），将知识片段拼进 personality prompt，**回答 MC 机制/配方类问题会更有依据**。
- **预期**：若知识库覆盖到位，问答质量提升；否则与升级前相近。

### 2.5 自主探索（无玩家指令，空闲 N 秒后触发）

- **逻辑**：`run_autonomous()` 用当前状态 + 短期记忆生成「1–3 步计划」JSON，再走 `_execute_plan()`。
- **预期**：若模型能稳定输出合法 plan（如「先 get_inventory，再 collect_block 木头」），会看到 Bot 自主采集/合成；若输出常解析失败或步骤不合理，可能多数为空或只执行 1 步即停。**适合作为锦上添花，不宜强依赖。**

---

## 三、与原版 Voyager 论文对比

| 维度 | 原版 Voyager（论文/仓库） | 本项目升级后 v3 |
|------|---------------------------|------------------|
| **动作空间** | 自由 JavaScript 代码（含 async function(bot)），在 Node 中 eval 执行 | 结构化 JSON 动作序列；执行器固定 switch(action_type)，无 eval |
| **单步 vs 批量** | 每次 LLM 可输出一整段脚本（多步逻辑），一次 eval 执行到底 | TEMPLATE：多步但无 LLM；PLAN：一次 LLM 输出多步 JSON，Node 批量执行；REACT：每步 1 次 LLM |
| **技能形态** | 技能 = 可被注入的 .js 代码，新代码可调用已有函数 | 技能 = JSON 描述 + 可选参数化 procedure；复用靠 context 注入与模板展开，无「代码级调用」 |
| **任务分解** | Curriculum 自动提议任务 + 人工/脚本可做 decompose | TaskPlanner.decompose + RAG；复杂任务先分解再逐子任务执行 |
| **知识来源** | 主要依赖 GPT-4 训练知识 + 环境反馈 | 训练知识 + **RAG 知识库** + 技能库 + 记忆，知识更可控 |
| **流畅度** | 单段脚本内无 Python 往返，观感最连贯 | PLAN 模式接近：一次计划在 Node 内执行，流畅；TEMPLATE 同样无额外 LLM；REACT 仍为逐步 |
| **精准度与安全** | eval 有安全与稳定性风险，生成代码易出错 | 无 eval，动作集固定，**精准度与可控性更好** |
| **适应性** | 段内可写 if/else、循环；段间依赖下一轮 LLM | 计划失败可 replan 或降级 ReAct；ReAct 每步可改策略，**适应性好** |

**小结**：  
- 在「流畅度 + 精准度 + 安全」的折中上，v3 更偏向**可控与安全**，用 PLAN 批量执行逼近原版「一段脚本多步」的流畅感，用 ReAct 兜底保证高不确定性场景的适应性。  
- 在「技能组合的表达力」上，原版仍是「代码即技能、任意组合」更强；v3 则靠**模板 + 计划中的多步序列 + context 中的技能描述**来近似，长期通过技能库积累可缩小差距。

---

## 四、实际测试时建议关注的指标

1. **路由分布**：日志中 TEMPLATE / PLAN / REACT 的比例；PLAN 占比高且成功率高时，说明多数任务被正确识别为「可计划」且 LLM 计划质量尚可。
2. **计划解析率**：`_parse_plan_json` 成功解析的比例；若经常失败，需要加强 prompt 或后处理。
3. **批量执行**：`/execute_plan` 的 success_count / fail_count；第一步就失败 vs 中间某步失败的比例，便于区分「计划不合理」与「状态/坐标等执行条件」问题。
4. **ReAct 兜底**：计划失败后降级 ReAct 的成功率；若经常降级且仍失败，说明单步决策或 state 传递仍有问题。
5. **RAG 影响**：对比开启/关闭 RAG 时，任务分解与计划步骤是否更符合 MC 常识（如合成顺序、材料名）。
6. **技能沉淀**：运行一段时间后，技能库中参数化模板与描述是否被新任务命中，以及命中后 TEMPLATE 执行是否稳定。

---

## 五、结论

- **方案本身**与文档中的「综合更好形式」一致：高层结构化计划 + 低层固定 JSON 执行 + 关键节点 ReAct 兜底，并接入 RAG 与记忆，**架构合理、可维护性好**。
- **预期效果**：  
  - 简单/可预测任务：**明显优于**升级前（更少 LLM 调用、更连贯）；  
  - 复杂多子任务：**中等偏上**，依赖分解质量与 state 更新；  
  - 高不确定性任务：与升级前 ReAct **相当**；  
  - 知识问答与自主探索：**有提升空间**，取决于 RAG 与 prompt 调优。  
- **与原版 Voyager**：在表达力与「代码级技能组合」上仍不如原版，在**安全性、精准度与可维护性**上更好，适合作为生产可用的折中方案。实际效果需在真实 MC 环境与多种指令下跑一轮，再结合上述指标做迭代。
