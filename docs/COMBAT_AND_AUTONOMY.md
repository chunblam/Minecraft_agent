# 战斗与自主探索扩展指南

本文档说明如何让 Agent 具备自主探索、学习技能和战斗能力，以支持你设想的场景：
- 发现钻石矿 → 自行探索用途 → 决定是否采集 → 从技能库找技能 → 学习并记忆
- 发现僵尸 → 学会战斗 → 使用武器 → 僵尸靠近时攻击

---

## 一、当前动作执行方式

| 执行方式 | 说明 |
|----------|------|
| **客户端输入模拟** | move_to / mine_block 通过 Fabric 网络包下发到客户端，模拟 WASD + 鼠标点击 |
| **服务端回退** | 客户端无 Mod 时，使用 PlayerTaskRunner 直接操控玩家 |
| **移动速度** | 4.317 格/秒（原版行走） |
| **挖掘** | 使用 `updateBlockBreakingProgress` 或 `breakBlock`，按硬度+工具计算 |

---

## 二、战斗相关扩展（待实现）

### 2.1 Java 端需新增的动作

| 动作 | 说明 | 参数 | 实现要点 |
|------|------|------|----------|
| `attack_entity` | 攻击指定实体 | `entity_uuid` 或 `entity_type` + 最近目标 | `player.attack(target)`，攻击冷却约 1 秒（20 tick） |
| `equip_weapon` | 装备武器 | `slot` | 切换到主手/副手 |
| `look_at_entity` | 面向实体 | 同上 | 用于攻击前瞄准 |

### 2.2 攻击冷却（与真实玩家一致）

- Minecraft 攻击冷却：1.6 秒（32 tick）满冷却，攻击后重置
- 可用 `player.getAttackCooldownProgress(0)` 判断是否可攻击

### 2.3 扩展实现建议

- **客户端**：在 `ClientInputSimulator` 中新增 `attack_entity` 类型，模拟鼠标左键攻击
- **服务端回退**：在 `PlayerTaskRunner` 中新增 `AttackEntityTask`，每 tick 检查攻击冷却后 `player.attack(target)`

---

## 三、自主探索与学习流程（Python 端）

### 3.1 感知层

- `game_state.nearby_resources`：矿石、原木等
- `game_state.nearby_entities`：实体类型及坐标
- 需扩展：**敌对生物**（僵尸、骷髅等）的单独标记或分类

### 3.2 决策层（LLM 或规则）

1. **发现钻石矿**：检索知识库「钻石用途」→ 决定是否采集 → 检索技能库「挖N个某矿石」
2. **发现僵尸**：检索知识库「僵尸威胁」→ 决定战斗/逃跑 → 检索技能库「战斗」或生成新技能

### 3.3 技能库扩展

- 新增「战斗」类参数化技能，例如：
  - 技能：`attack_entity`（攻击最近敌对生物）
  - 流程：`find_entity(zombie)` → `for_each: move_to → attack_entity`

### 3.4 记忆与学习

- 成功战斗轨迹 → `abstract_from_trajectory` 抽象为「战斗」技能
- 长期记忆：记录「钻石有用」「僵尸危险」等

---

## 四、实现优先级建议

1. **Phase 1**：Java 端 `attack_entity` + `AttackEntityTask`（tick 级，含攻击冷却）
2. **Phase 2**：Python 端 `action` 列表增加 `attack_entity`，`game_state` 增加 `hostile_entities`
3. **Phase 3**：战斗类参数化技能 + 自主决策（发现僵尸 → 选择战斗）

---

## 五、参考：原版攻击时间

- 攻击冷却：1.6 秒（100% 恢复）
- 攻击范围：约 3 格（生存模式）
- 挥剑动画：约 0.6 秒（与攻击冷却部分重叠）
