# 技能组合复用方案（Voyager 对齐）

## 一、目标

在「检索到的技能注入执行环境」后，支持 **自行组合技能**：生成的任务函数中可以按名调用多个检索到的技能，形成组合式行为（与 Voyager 论文中 “skills are compositional” 一致）。

## 二、前提

- 执行时 Node 端已将本次检索到的技能 **code 列表** 与用户代码拼在同一作用域执行，因此任务函数内可调用 `await collectLogs(bot, { quantity: 4 })`、`await craftItem(bot, "crafting_table", 1)` 等。
- 仅能调用的函数为：**控制原语**（mineBlock、craftItem、placeItem 等）+ **本节 Retrieved Skills 中出现的函数**。未检索到的函数名仍禁止编造。

## 三、组合方式（详细）

### 3.1 顺序组合

在同一任务函数中依次调用多个技能，前一个的输出（如背包状态）自然作为后一个的输入：

```javascript
async function makeChestAndStore(bot) {
    await collectLogs(bot, { logType: "oak_log", quantity: 8 });  // 技能 1
    await craftItem(bot, "crafting_table", 1);                     // 原语
    // 放置工作台、合成箱子等...
    await craftItem(bot, "chest", 1);                              // 原语
    bot.chat("Chest crafted.");
}
```

- **谁决定顺序**：LLM 根据任务描述（如「制作箱子并存东西」）决定先采木、再工作台、再箱子。
- **数据流**：技能与原语共享 `bot`（背包、位置等），无需显式传参；LLM 只需按逻辑顺序写多次 `await`。

### 3.2 条件组合

根据当前状态选择调用不同技能或原语：

```javascript
async function gatherWoodOrStone(bot) {
    const hasAxe = bot.inventory.items().some(i => i.name.includes("axe"));
    if (hasAxe) {
        await collectLogs(bot, { quantity: 4 });
    } else {
        await mineBlock(bot, "cobblestone", 4);
    }
}
```

- **谁决定分支**：LLM 根据 prompt 中的 game_state（如装备、背包）和任务要求生成条件逻辑。
- **技能接口**：技能均以 `(bot, params)` 为签名，与 primitives 一致，便于在条件分支中调用。

### 3.3 循环内组合

在 `exploreUntil` 或有限循环中多次调用同一/不同技能：

```javascript
async function collectUntilEnough(bot) {
    let total = 0;
    while (total < 10) {
        await collectLogs(bot, { quantity: 4 });
        total = bot.inventory.items().filter(i => i.name === "oak_log").reduce((s, i) => s + i.count, 0);
        if (total >= 10) break;
        await exploreUntil(bot, "north", 32, () => false);
    }
}
```

- **注意**：Prompt 已约束「不写无限循环」，此处为有限循环（有 break 条件），符合规范。
- **组合含义**：同一技能 + 原语 exploreUntil 组合，实现「不够就采、采完再探索」的流程。

### 3.4 与「快速路径」的关系

- **快速路径**（`_try_skill_fast_path`）：当任务与**单条**技能高度匹配（相似度 ≥ 0.88）且可参数替换时，直接执行该技能代码，不经过 LLM 生成。此时不存在「多技能组合」。
- **CodeLoop 生成**：当需要多步或组合时，走 CodeLoop；LLM 生成的代码中可以**同时调用多个检索到的技能 + 原语**，即本方案所描述的「自行组合」。
- **检索数量**：当前 `top_k=3` 技能注入；若任务明显需要多种能力（如采木 + 合成 + 放置），可考虑将 `top_k` 提高到 5，以便更多技能进入 context 与注入列表，供 LLM 组合调用。

## 四、约束与最佳实践

| 项 | 说明 |
|----|------|
| **仅调用已注入技能** | 生成代码只能调用「本次 Retrieved Skills」中出现的函数名；不得编造未检索到的技能名。 |
| **参数格式** | 技能若接受 `params` 对象（如 `collectLogs(bot, { quantity: 4 })`），需与技能定义一致；Prompt 中应保留「参考 Retrieved Skills 的签名与描述」。 |
| **错误处理** | 技能内部已包含 try/catch 或 chat 提示时，组合代码可依赖其行为；若需更细粒度控制，可在组合层再包一层 try/catch。 |
| **可解释性** | 每个技能为独立函数，组合后的任务函数仍是一段完整代码，便于调试与后续抽取为新技能。 |

## 五、小结

- **可以自行组合**：在检索技能注入执行环境的前提下，LLM 生成的任务函数中可以**按名多次调用**不同技能与原语，实现顺序、条件、循环等组合。
- **无需额外实现**：组合完全由 LLM 在生成阶段完成，执行层只需保证「注入技能代码 + 用户代码同作用域」即可。
- **后续可选增强**：若希望更稳定地触发组合，可在 Prompt 中增加 1～2 条示例（如「若任务包含采集与合成，可先调用 collectLogs 再 craftItem」）；或对「多子目标」任务适当提高 `top_k` 以注入更多技能供选择。
