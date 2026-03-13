# Minecraft Agent 项目评估报告（2026-03-10 测试）

基于日志 `logs/run_2026-03-10_22-27-33.log` 与代码分析。

---

## 一、本次测试概览

### 1.1 测试范围

- **时长**：约 45 分钟（22:27–23:12）
- **任务类型**：砍木头、制作木斧、扔木斧、跟随玩家、采集小麦种子、切换旁观模式等
- **流程**：意图识别 → 复杂度判定 → 上下文构建（技能 + RAG + 记忆）→ 代码生成与执行 → Critic 校验 → 成功后技能抽象存储

### 1.2 成功之处

| 项目 | 说明 |
|------|------|
| **ReAct 闭环** | 执行失败时能把 error / chat_log 反馈给 LLM，多轮重试（如「帮我砍5个木头」2 次、「制作一把木斧」4 次后成功） |
| **技能沉淀** | 任务通过 Critic 后自动抽象并写入技能库（如 collectLogs、craftWoodenTool、moveToPlayer），技能数从 0 增长 |
| **5 分钟任务超时** | 单次执行上限 300s，长任务不再被 60s 提前掐断 |
| **【MC】日志** | 游戏内聊天与 Node 踢出/错误（[MC-KICK]、[MC-ERROR]、[MC-END]）在终端和日志中可区分，便于排查 |
| **意图/复杂度关键词** | 「帮我砍」「制作」「来身边」等走 task_execution；多数任务被判 simple，直接走单任务路径 |
| **三路 context** | 技能检索、RAG、记忆在 `_build_context` 中并发拉取，合并后作为 `Context` 传入代码生成 prompt |

### 1.3 缺失与欠佳之处

| 问题 | 现象 / 原因 |
|------|----------------|
| **① 大模型调用耗时大** | 意图识别、复杂度、RAG 分类、代码生成、Critic、技能抽象均调 LLM；单次任务多次 20–60s 级响应，整轮延迟高 |
| **② RAG 分类串行且耗时长** | `retriever.search()` 内部先 `classifier.classify(query)`（约 5s），再向量检索；与技能检索虽并发，但 RAG 自身含一次 LLM |
| **③ 无「先 RAG 再技能再模板」管线** | 当前是「技能 + RAG + 记忆」一起拉取后全部塞进 context，未实现：先 RAG → 再按知识匹配技能 → 有技能则强约束「按模板改写」 |
| **④ 复杂任务拆解未充分使用** | 复杂度多为 `simple`，几乎未走 `_run_hierarchical`；「制作木斧」等多步任务未拆成子任务序列，而是一段长代码多次重试 |
| **⑤ 子任务未作为技能学习** | 即便走 hierarchical，子任务完成后只更新 game_state，没有把每个子任务的成功轨迹抽象成独立技能入库 |
| **⑥ 合成/原语知识不足** | 「制作一把木斧」多次报错「合成 stick 需要工作台」；模型未稳定遵循「先造工作台再合成」等顺序，说明 RAG 或 prompt 中的合成知识未被充分约束 |
| **⑦ 生成代码与运行环境不一致** | 如 `bot.drop is not a function`、`pos.floored is not a function`、toss 找错物品（oak_planks）；原语/API 与 prompt 不一致或模型幻觉 |
| **⑧ Critic / 技能抽象偶发超时** | 如 22:40:43、22:42:13、23:11:37 出现「LLM 调用超时（90s）」，影响当轮判定与后续重试 |
| **⑨ 部分任务 4 次仍失败** | 「采集10个小麦种子」因 API 使用错误（equip、floored 等）4 次未过；「把你的木斧扔给我」因 toss/drop 用法 4 次未过 |

---

## 二、任务运行逻辑（当前实现）

### 2.1 整体流程（代码依据）

```
run(game_state, player_message)
  │
  ├─ 1. _classify_intent(msg)          → LLM 或关键词 → "task_execution" | "chat"
  ├─ 2. [若 task_execution] _classify_complexity(msg, game_state) → LLM 或关键词 → "simple" | "complex"
  ├─ 3. [若 simple] _run_task(task, game_state)
  │       │
  │       ├─ 3.1 _build_context(task, game_state)   ← ★ 三路并发
  │       │         asyncio.gather(
  │       │           _fetch_skill_programs(task),  → skill_lib.get_programs_string(query, top_k=3)
  │       │           _fetch_rag_context(task),    → retriever.search(task, top_k=4)  [内部先 classify 再向量检索]
  │       │           memory.get_relevant_context(task)
  │       │         )
  │       │         → context = "技能代码块" + "RAG 知识片段" + "Past Experience"
  │       │
  │       ├─ 3.2 for attempt in 1..4:
  │       │         prompt = _build_code_prompt(task, game_state, context, last_code, last_error, last_output, critique)
  │       │         raw = llm.think(CODE_GENERATION_SYSTEM_PROMPT, prompt)   ← context 在这里传入
  │       │         code = _extract_code(raw)
  │       │         exec_r = env.execute_code(code, timeout_ms=300000)
  │       │         [若失败] last_error, last_code 带入下一轮
  │       │         [若成功] Critic.check_task_success(...) → 通过则 _store_skill（后台）并 return
  │       │
  │       └─ 3.3 返回 display_message / extra_data
  │
  └─ [若 complex] _run_hierarchical(...)  → planner.decompose + 对每个子任务 _run_task（当前测试中几乎未触发）
```

- **结论 1（是否并行）**：**技能检索、RAG 检索、记忆检索** 在 `_build_context` 里是 **并行** 的（`asyncio.gather`）。但 **RAG 检索内部** 先有一次 **LLM 分类**（`classifier.classify`），再向量检索，因此「RAG 路径」总耗时 ≈ 分类耗时 + 检索耗时。
- **结论 2（RAG 是否参与生成代码）**：**有**。`_fetch_rag_context` 得到的内容会放入 `context`，`_build_code_prompt` 里把 `context` 填入 `CODE_GENERATION_HUMAN_TEMPLATE` 的 `Context: {context}`，因此 RAG 知识会随「技能代码 + 记忆」一起传给大模型用于生成代码。

### 2.2 与你理想流程的对比

| 理想（你描述） | 当前实现 |
|----------------|----------|
| 先对任务做 RAG 检索，得到相关知识 | RAG 与技能、记忆 **并发** 拉取，无「先 RAG 再其他」的严格顺序 |
| 用 RAG + 任务描述去匹配技能库，若有则用模板 | 技能库用 query 做语义检索（ChromaDB），返回 top_k 段代码；**没有**「先 RAG 再以 RAG 约束技能匹配」，也**没有**「有技能则强制按模板改写」的单独分支 |
| 有技能：模板 + 任务 → 快速生成代码 | 有技能时，技能代码作为 context 一部分给 LLM，**没有**「只改参数/填空」的轻量生成路径，仍是完整代码生成 |
| 无技能：RAG + 任务 → 复杂度判别 → 拆解 → 生成代码 | 复杂度在 **进入 _run_task 之前** 用 LLM 判一次；**没有**「无技能时才做复杂度+拆解」的分支；complex 时走 hierarchical，但本次测试多为 simple，几乎未拆解 |
| 拆解后的子任务也当作小技能学习 | **没有**。子任务完成后只更新 game_state，不调用 _store_skill 对子任务抽象存储 |

---

## 三、时间都花在哪（结合日志）

- **意图**：约 1.4–4.5s/次（classify）
- **复杂度**：约 1.0–4.1s/次（classify）
- **_build_context**：三路并发，整体约 5–85s；其中 **RAG 的 classify** 单次约 5s（如 22:28:44→22:28:49），技能检索若 Chroma 无或很少则很快；有时 RAG 分类到多 collection 或检索慢会拉长（如 22:32:25→22:33:50 约 85s）
- **代码生成**：约 18–56s/次，重试时带 error 的更长（28–49s）
- **Critic**：约 2–53s/次，偶发 90s 超时
- **技能抽象**：约 14–64s/次，后台执行但仍占 LLM

整体上，**意图 + 复杂度 + context（含 RAG 分类）+ 代码生成 + Critic** 是单任务主链路，多次 20–60s 级调用叠加，导致「大模型占用时间太久」的体感。

---

## 四、RAG 知识库是否被参考并传入大模型

- **有被参考并传入**。  
  - `_fetch_rag_context` 调用 `retriever.search(task, top_k=4)`，返回的文档列表被格式化为 `## Relevant Minecraft Knowledge:` + 若干 `// title content`。  
  - 该字符串与技能、记忆一起拼成 `context`，在 `_build_code_prompt` 中通过 `CODE_GENERATION_HUMAN_TEMPLATE` 的 `Context: {context}` 传给 LLM。  
- **效果受限的可能原因**：  
  - RAG 分类或检索到的片段与「合成顺序、工作台前置」等强约束不够精准；  
  - prompt 中未显式强调「必须严格按 RAG 中的合成/原语说明编写」；  
  - 模型仍依赖自身知识，未严格遵循检索到的短句。

---

## 五、改进建议（与你的疑惑对应）

1. **减少 LLM 调用与耗时**  
   - 意图/复杂度：更多关键词规则，或用小模型/本地分类器，减少 DeepSeek 调用。  
   - RAG：可选「先向量检索（不分类）」或「固定 collection」路径，减少每次 search 的 classify。  
   - 有技能时：设计「技能模板 + 参数填空」的轻量生成或直接执行，避免每次都完整代码生成。

2. **更贴近「先 RAG → 再技能 → 再生成」**  
   - 先只调 RAG，得到「相关知识」；  
   - 用「任务 + RAG 摘要」检索技能，若有高置信匹配则走「模板 + 参数」或直接复用；  
   - 无技能时再：复杂度判定 → 若 complex 则拆解 → 用 RAG + 任务（+ 子任务描述）生成代码。

3. **复杂任务拆解与子任务技能化**  
   - 对「制作木斧」「建房子」等多步任务，通过 prompt 或规则更倾向判为 complex，走 `_run_hierarchical`。  
   - 每个子任务完成后，除更新 game_state 外，**对子任务轨迹调用 _store_skill（或仅对「可复用」子任务存储）**，把子任务当作小技能入库，便于后续「有技能则快速用模板」。

4. **合成/原语与 prompt 一致性**  
   - 在 CODE_GENERATION_SYSTEM_PROMPT 或 RAG 中明确写出「合成 stick/工作台等必须先有工作台」「仅可使用 primitives 列表中的 API」；  
   - 运行环境暴露的 API（如 toss 参数、是否有 drop）与文档/prompt 对齐，减少 `bot.drop is not a function`、`pos.floored` 等幻觉。

5. **Critic / 技能抽象超时**  
   - 适当提高 Critic 与技能抽象的 timeout，或拆成更小步、减少单次输入长度；  
   - 超时时的降级策略（如默认 success=false 并带简短 critique）避免整轮卡死。

---

## 六、小结

- **成功点**：ReAct 闭环、技能沉淀、5 分钟超时、【MC】日志、三路 context 并行、RAG 内容确实传入代码生成。  
- **主要缺口**：大模型调用链路过长且串行环节多（含 RAG 内 classify）、没有「先 RAG 再技能再模板」的管线、复杂任务拆解与子任务技能化不足、合成/原语一致性与超时处理需加强。  
- **你提的「有模板应更快」**：当前即使用到技能，仍是「context 里塞技能代码 + 完整生成」，没有单独的「模板填空」快速路径，这是后续可做的重要优化。

以上为本次测试与项目逻辑的评估与改进方向，可直接用于迭代设计与排期。
