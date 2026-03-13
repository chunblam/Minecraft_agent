# Minecraft Agent 调试、技能学习与升级方案完全指南

---

## 一、架构全景分析

### 当前代码框架（chunblam/Minecraft_agent v2）

```
玩家/LLM
   │
   ▼
main.py  ──→  VoyagerAgent (react_agent.py)
                │
                ├── LLMRouter          ← 分层调用 fast/slow 模型
                ├── TaskPlanner        ← 复杂任务分解成子任务列表
                ├── CriticAgent        ← 执行后自验证是否成功
                ├── MemoryManager      ← 短期/长期记忆三层系统
                └── SkillLibrary       ← ChromaDB 向量检索技能库
                         │
                         ▼
              MineflayerEnv (env.py)
                HTTP POST ↕
              Node.js mineflayer/index.js
                         │
                         ▼
              Minecraft Server (原版/Paper)
```

**关键变化（相比原始 Voyager）：**
- ❌ 废弃 Java Fabric Mod → 用 mineflayer Bot 替代
- ✅ Python→HTTP→Node.js→Minecraft，无需 Mod 安装
- ✅ 保留 Voyager 三大核心：Curriculum / Critic / SkillManager
- ✅ 新增：PersonalitySystem（人格）、SkillExecutor（模板执行）

---

## 二、一步一步运行与调试

### Step 0：环境准备

```bash
# 系统要求
# - Python >= 3.9
# - Node.js >= 18
# - Minecraft Java Edition（任意版本，开放 LAN 即可）

# 克隆仓库
git clone https://github.com/chunblam/Minecraft_agent
cd Minecraft_agent

# Python 依赖
pip install -r requirements.txt
# 核心依赖：loguru, aiohttp, chromadb, openai

# Node.js 依赖
cd minecraft_agent/mineflayer
npm install
cd ../..
```

### Step 1：启动 Minecraft 服务器

**方式 A：本机单人游戏（最简单）**
1. 打开 Minecraft Java Edition
2. 新建/加载世界 → 选创造模式（便于调试）
3. 按 `Esc` → 「对局域网开放」→ 记下端口号（如 54321）
4. 游戏模式选「创造」，作弊选「允许」

**方式 B：专用服务器（推荐用于持续学习）**
```bash
# 下载 PaperMC（性能更好）
java -jar paper-1.20.4.jar --nogui
# server.properties 设置：
# online-mode=false    ← 允许离线账户（mineflayer 不需要正版）
# gamemode=creative
```

### Step 2：配置环境变量

```bash
# 创建 .env 文件
cat > .env << 'EOF'
MC_HOST=localhost
MC_PORT=25565          # 或局域网游戏的端口
MC_USERNAME=AgentBot
MINEFLAYER_PORT=3000
LLM_API_KEY=sk-xxxx    # OpenAI / Claude API Key
LLM_BASE_URL=          # 可选，用于代理或 Claude
LLM_MODEL=gpt-4o-mini  # 推荐先用 mini 省钱调试
EOF
```

### Step 3：手动测试 mineflayer 连接（先不启动 Agent）

```bash
# 单独启动 mineflayer HTTP 服务
cd minecraft_agent/mineflayer
node index.js 3000

# 新开终端，测试 Bot 能否入游戏
curl -X POST http://localhost:3000/start \
  -H "Content-Type: application/json" \
  -d '{"host":"localhost","port":25565,"username":"AgentBot","reset":"soft"}'

# 预期输出：{"status":"ok","observation":{...position, health...}}
# 如果成功，你会在游戏中看到 AgentBot 加入
```

### Step 4：测试单条 Action

```bash
# 测试移动
curl -X POST http://localhost:3000/step \
  -H "Content-Type: application/json" \
  -d '{"action_type":"move_to","params":{"x":100,"y":64,"z":100}}'

# 测试挖掘（先站在一棵树旁边）
curl -X POST http://localhost:3000/step \
  -H "Content-Type: application/json" \
  -d '{"action_type":"collect_block","params":{"block_type":"oak_log","count":3}}'

# 查看当前状态
curl -X POST http://localhost:3000/observe
```

### Step 5：测试 Python Agent 核心模块

```python
# test_agent.py —— 分模块逐步调试

import asyncio
import os
from dotenv import load_dotenv
load_dotenv()

from minecraft_agent.agent.env import MineflayerEnv
from minecraft_agent.agent.llm_router import LLMRouter
from minecraft_agent.agent.skill_library import SkillLibrary

async def test_env_only():
    """测试环境连接"""
    env = MineflayerEnv(
        mc_host="localhost",
        mc_port=25565,
        username="AgentBot",
        server_port=3000,
        auto_start_server=True,   # 自动启动 node 进程
    )
    state = await env.start()
    print("连接成功！位置:", state.get("position"))
    
    obs, new_state = await env.step("collect_block", {"block_type": "oak_log", "count": 1})
    print("执行结果:", obs)
    print("背包:", new_state.get("inventory"))
    
    await env.stop()

asyncio.run(test_env_only())
```

### Step 6：启动完整 Agent

```bash
# 正式启动
cd minecraft_agent
python main.py

# 然后在 Minecraft 游戏聊天中发消息给 Bot：
# "AgentBot 帮我砍5棵树"
# "AgentBot 制作一张工作台"
# "AgentBot 去 x=200 z=300 探索"
```

### 常见问题排查

| 错误 | 原因 | 解决 |
|------|------|------|
| `无法连接 mineflayer 服务器` | Node.js 进程未启动 | 检查 `node_modules` 是否安装，手动 `node index.js` |
| `Bot 被踢出：AuthError` | 服务器要求正版验证 | `server.properties` 设 `online-mode=false` |
| `LLM API Error 401` | API Key 错误 | 检查 `.env` 的 `LLM_API_KEY` |
| `ChromaDB 启动失败` | 缺少 chromadb 依赖 | `pip install chromadb` |
| `find_block 总返回未找到` | 当前区块未加载 | 在游戏中先走到目标区域附近 |

---

## 三、技能学习的三种方式

### 方式 A：让 Agent 自动学习（Voyager 原版方式）

Bot 执行任务成功后，`SkillLibrary.extract_from_trajectory()` 自动将轨迹抽象成技能：

```python
# react_agent.py 内部已实现，成功后自动触发
# 技能持久化到 skills/skill_library/ 目录（ChromaDB）

# 查看已学到的技能
from minecraft_agent.agent.skill_library import SkillLibrary
sl = SkillLibrary(persist_dir="skills/skill_library")
print(f"已存储 {sl.skill_count} 个技能")

# 检索相关技能
results = asyncio.run(sl.search_skill("mine wood", top_k=3))
for r in results:
    print(r["skill"]["name"], "->", r["skill"]["description"])
```

### 方式 B：手动注入预设技能（直接教）

```python
# inject_skills.py —— 直接向技能库注入已知最优技能

import asyncio
from minecraft_agent.agent.skill_library import SkillLibrary
from minecraft_agent.agent.llm_router import LLMRouter

llm = LLMRouter(fast_model="gpt-4o-mini", api_key="YOUR_KEY")
sl = SkillLibrary(llm=llm, persist_dir="skills/skill_library")

# 定义一个"砍树+制作工作台"技能
craft_table_skill = {
    "name": "craft_crafting_table",
    "description": "砍木头→制木板→制工作台的完整流程",
    "parameters": {"log_type": "oak_log"},
    "preconditions": ["附近有树木", "背包空间足够"],
    "steps": [
        {"action": "collect_block", "params": {"block_type": "{{log_type}}", "count": 4},
         "description": "砍4块木头"},
        {"action": "craft", "params": {"recipe": "planks", "count": 16},
         "description": "制作16块木板"},
        {"action": "craft", "params": {"recipe": "crafting_table", "count": 1},
         "description": "制作工作台"},
    ],
    "postconditions": ["背包有 crafting_table"],
    "reliability_score": 0.9,
}

asyncio.run(sl.add_skill(craft_table_skill))
print("✅ 技能已注入")
```

### 方式 C：你亲自演示→Agent 学习（最接近 MineJoDo 思路）

这是本方案的核心升级方向，详见第四节。

---

## 四、MineJoDo 风格的「人类演示学习」升级方案

### 背景：MineJoDo 做了什么？

MineJoDo（Wang et al., 2024）的核心思想：
> 让人类玩家在 Minecraft 中演示操作，系统录制轨迹，用 LLM 将轨迹转化为可泛化的技能程序，Agent 再调用这些技能完成复杂任务。

与原始 Voyager 的关键区别：
- Voyager：Agent 自己探索，试错学习
- MineJoDo：**人类示范** → 轨迹录制 → LLM 抽象 → 技能入库

### 实施方案：在现有框架上添加「演示录制系统」

#### 组件 1：DemoRecorder —— 录制你的操作轨迹

```python
# minecraft_agent/agent/demo_recorder.py

import json
import time
import asyncio
from pathlib import Path
from loguru import logger

class DemoRecorder:
    """
    录制人类玩家在 Minecraft 中的操作，生成轨迹文件。
    
    工作原理：
    1. 以固定频率（默认2秒）轮询 /observe 获取游戏状态
    2. 检测状态变化（位置、背包、周围方块）
    3. 将变化记录为轨迹步骤
    4. 最终输出 demo_trajectory.json
    
    使用方式：
    - 打开 demo_recorder.py 
    - 在游戏里正常操作（砍树、合成、建造...）
    - 停止录制后，轨迹自动保存并触发技能抽象
    """
    
    def __init__(self, env, skill_lib, demo_name: str, sample_interval: float = 2.0):
        self.env = env
        self.skill_lib = skill_lib
        self.demo_name = demo_name
        self.sample_interval = sample_interval
        self.trajectory = []
        self.recording = False
        self._prev_state = None
    
    async def start_recording(self):
        """开始录制"""
        self.recording = True
        self.trajectory = []
        logger.info(f"🎬 开始录制演示：{self.demo_name}")
        logger.info("现在请在 Minecraft 中执行你想教给 Agent 的操作...")
        
        step = 0
        while self.recording:
            try:
                state = await self.env.observe()
                if state:
                    entry = self._detect_changes(state, step)
                    if entry:
                        self.trajectory.append(entry)
                        logger.info(f"  [{step}] 检测到变化: {entry.get('inferred_action', '?')}")
                    self._prev_state = state
                    step += 1
            except Exception as e:
                logger.warning(f"录制采样失败: {e}")
            
            await asyncio.sleep(self.sample_interval)
    
    def stop_recording(self) -> list:
        """停止录制，返回轨迹"""
        self.recording = False
        logger.info(f"⏹ 录制结束，共 {len(self.trajectory)} 步")
        
        # 保存到文件
        demo_dir = Path("demos")
        demo_dir.mkdir(exist_ok=True)
        demo_path = demo_dir / f"{self.demo_name}_{int(time.time())}.json"
        with open(demo_path, "w", encoding="utf-8") as f:
            json.dump(self.trajectory, f, ensure_ascii=False, indent=2)
        logger.info(f"✅ 演示轨迹已保存到 {demo_path}")
        
        return self.trajectory
    
    def _detect_changes(self, curr: dict, step: int) -> dict | None:
        """对比前后状态，推断发生了什么动作"""
        if self._prev_state is None:
            return {"step": step, "state": curr, "inferred_action": "initial_state",
                    "action_params": {}, "observation": "初始状态"}
        
        prev = self._prev_state
        changes = {}
        inferred = "observe"
        
        # 检测位置变化
        prev_pos = prev.get("position", {})
        curr_pos = curr.get("position", {})
        dist = (
            (curr_pos.get("x",0) - prev_pos.get("x",0))**2 +
            (curr_pos.get("z",0) - prev_pos.get("z",0))**2
        ) ** 0.5
        if dist > 2:
            inferred = "move_to"
            changes["distance"] = round(dist, 1)
            changes["to"] = curr_pos
        
        # 检测背包变化（物品增加）
        prev_inv = {i["item"]: i["count"] for i in prev.get("inventory", [])}
        curr_inv = {i["item"]: i["count"] for i in curr.get("inventory", [])}
        gained = {}
        lost = {}
        for item, count in curr_inv.items():
            prev_count = prev_inv.get(item, 0)
            if count > prev_count:
                gained[item] = count - prev_count
        for item, count in prev_inv.items():
            curr_count = curr_inv.get(item, 0)
            if curr_count < count:
                lost[item] = count - curr_count
        
        if gained:
            if any("log" in k or "ore" in k or "stone" in k for k in gained):
                inferred = "mine_block"
            elif any(k in curr_inv and k not in prev_inv for k in gained):
                inferred = "craft"
            else:
                inferred = "collect"
            changes["gained"] = gained
        if lost:
            if any("log" in k or "planks" in k for k in lost) and gained:
                inferred = "craft"
            changes["lost"] = lost
        
        # 没有明显变化则跳过
        if inferred == "observe" and not changes:
            return None
        
        return {
            "step": step,
            "inferred_action": inferred,
            "action_params": changes,
            "state_snapshot": {
                "position": curr_pos,
                "inventory_gained": gained,
                "inventory_lost": lost,
                "health": curr.get("health"),
            },
            "observation": f"[演示] {inferred}: {changes}",
        }
    
    async def learn_from_trajectory(self, task_description: str) -> dict | None:
        """将录制的轨迹提交给 SkillLibrary 抽象成技能"""
        if not self.trajectory:
            logger.warning("轨迹为空，无法学习")
            return None
        
        logger.info(f"🧠 正在从演示中抽象技能: {task_description}")
        skill = await self.skill_lib.extract_from_trajectory(
            task=task_description,
            trajectory=self.trajectory,
            success=True,
        )
        if skill:
            logger.info(f"✅ 成功学习技能: {skill.get('name')}")
        return skill
```

#### 组件 2：DemoCLI —— 交互式演示录制入口

```python
# demo_teach.py —— 运行这个脚本来教 Agent

import asyncio
import os
from dotenv import load_dotenv
load_dotenv()

from minecraft_agent.agent.env import MineflayerEnv
from minecraft_agent.agent.llm_router import LLMRouter
from minecraft_agent.agent.skill_library import SkillLibrary
from minecraft_agent.agent.demo_recorder import DemoRecorder

async def main():
    # 初始化
    env = MineflayerEnv(mc_host="localhost", mc_port=25565, username="AgentBot")
    await env.start()
    
    llm = LLMRouter(fast_model="gpt-4o-mini", api_key=os.getenv("LLM_API_KEY"))
    skill_lib = SkillLibrary(llm=llm, persist_dir="skills/skill_library")
    
    print("\n=== Minecraft Agent 演示教学系统 ===")
    print("命令: record <技能名> | stop | list | test <技能名> | quit\n")
    
    recorder = None
    record_task = None
    
    while True:
        cmd = input(">>> ").strip()
        
        if cmd.startswith("record "):
            skill_name = cmd[7:].strip()
            task_desc = input(f"请描述这个技能的目标（如'砍3棵橡树并制作工作台'）: ").strip()
            
            recorder = DemoRecorder(env, skill_lib, skill_name)
            record_task = asyncio.create_task(recorder.start_recording())
            print(f"✅ 开始录制 [{skill_name}]，请在游戏中演示操作！")
            print("完成后输入 'stop' 结束录制")
        
        elif cmd == "stop" and recorder:
            recorder.stop_recording()
            if record_task:
                record_task.cancel()
            
            task_desc = input("请用一句话描述你刚才演示的任务: ").strip()
            skill = await recorder.learn_from_trajectory(task_desc)
            if skill:
                print(f"\n✅ 技能学习成功！")
                print(f"  名称: {skill.get('name')}")
                print(f"  描述: {skill.get('description')}")
                print(f"  步骤数: {len(skill.get('steps', []))}")
            recorder = None
        
        elif cmd == "list":
            print(f"\n已学习的技能（共 {skill_lib.skill_count} 个）:")
            results = await skill_lib.search_skill("", top_k=20)
            for r in results:
                sk = r["skill"]
                print(f"  - {sk['name']}: {sk.get('description', '')[:60]}")
        
        elif cmd.startswith("test "):
            skill_name = cmd[5:].strip()
            print(f"[测试] 让 Agent 执行技能: {skill_name}")
            # 调用 VoyagerAgent 执行
        
        elif cmd == "quit":
            break
    
    await env.stop()

asyncio.run(main())
```

#### 组件 3：分级技能体系（基础→进阶→复杂任务）

```
基础技能（1-2步）           进阶技能（依赖基础）          复杂任务（组合进阶）
─────────────────          ──────────────────           ──────────────────
collect_oak_log       →    craft_crafting_table    →    setup_basic_base
mine_stone            →    smelt_iron_ore          →    build_iron_armor
find_animals          →    cook_food               →    survive_first_night
navigate_to_biome     →    brew_potion             →    explore_and_map
```

---

## 五、完整升级路线图

### Phase 1（当前可做）：稳定基础技能库

```bash
# 推荐先教 Agent 以下10个基础技能，通过 inject_skills.py 手动注入
1. collect_wood         —— 砍指定数量木头
2. craft_planks         —— 木头→木板
3. craft_crafting_table —— 制作工作台
4. mine_stone           —— 挖石头
5. craft_stone_tools    —— 石镐、石剑
6. find_cave            —— 寻找洞穴入口
7. mine_iron_ore        —— 挖铁矿
8. smelt_iron           —— 熔炉炼铁
9. craft_iron_tools     —— 铁镐、铁剑
10. build_shelter       —— 搭简易庇护所
```

### Phase 2（下一步）：接入人类演示录制

按照上文 DemoRecorder 实现：
1. 运行 `demo_teach.py`
2. 在游戏中演示操作（比如建一个地下室）
3. 系统自动抽象成可复用技能
4. Agent 下次遇到类似任务自动调用

### Phase 3（高阶）：层级规划 + 技能组合

```python
# 示例：给 Agent 一个复杂目标
# "在 100 blocks 以外建一个有床、储物箱和炉子的基地"

# VoyagerAgent 会自动：
# 1. TaskPlanner 分解成子任务
# 2. 从 SkillLibrary 检索每个子任务所需技能
# 3. 依次执行，失败时 CriticAgent 给出改进建议
# 4. 整个大任务成功后抽象成新的高层技能存库
```

### Phase 4（研究级）：视觉感知 + 多模态

对标 MineJoDo 完整版，需要额外实现：
- 截图 + Vision LLM 分析游戏画面
- 更精细的鼠标/键盘动作录制（需要 Game Capture）
- 动作模仿（Behavioral Cloning）而非纯状态推断

```python
# 可在 mineflayer/index.js 添加截图端点
# 使用 puppeteer 或 Xvfb + scrot 获取游戏截图
# 发送给 GPT-4V / Claude Vision 分析当前状态
```

---

## 六、快速验证清单

```
□ Step 1: Minecraft 已开启 LAN，记录端口
□ Step 2: node index.js 3000 正常启动
□ Step 3: curl /start 返回 Bot 位置信息  
□ Step 4: curl /step 能执行 collect_block
□ Step 5: python test_agent.py 无报错
□ Step 6: python main.py 启动，游戏里 AgentBot 出现
□ Step 7: 聊天发送任务，Agent 开始执行
□ Step 8: skills/skill_library/ 出现技能文件
□ Step 9: 重启后技能仍可被检索（ChromaDB 持久化）
□ Step 10: 运行 demo_teach.py，录制一个演示，成功学习
```

---

## 七、关键文件速查

| 文件 | 功能 | 调试重点 |
|------|------|----------|
| `main.py` | 启动入口 | 环境变量、模块初始化顺序 |
| `agent/env.py` | HTTP↔mineflayer | 连接超时、Bot 被踢出 |
| `mineflayer/index.js` | Bot 核心逻辑 | Action 执行失败、pathfinder 卡住 |
| `agent/react_agent.py` | 主循环 | LLM 输出格式解析错误 |
| `agent/skill_library.py` | 技能存取 | ChromaDB 初始化、向量检索 |
| `agent/planner.py` | 任务分解 | 子任务依赖顺序错误 |
| `agent/critic.py` | 成功验证 | 误判成功/失败 |
| `agent/prompts.py` | 所有 Prompt | LLM 输出格式调优 |

---

*参考论文：Voyager (Wang et al., 2023) · MineJoDo (同组后续工作)*
