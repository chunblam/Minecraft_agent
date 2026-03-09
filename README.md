# Minecraft AI Agent

基于 LLM 的 Minecraft 游戏助手，参考 [Voyager](https://voyager.minedojo.org/) 论文设计，通过 WebSocket 连接 Fabric Mod，实现任务规划、技能学习与玩家操控。

## 架构概览

```
┌─────────────────┐     WebSocket      ┌─────────────────┐
│  Python Agent   │ ◄──────────────►  │  Fabric Mod     │
│  (规划/技能/RAG) │   ws://localhost  │  (客户端+服务端) │
└─────────────────┘       :8765       └─────────────────┘
        │                                    │
        │ 意图识别 → 任务执行/闲聊             │ 模拟 WASD + 鼠标
        │ Plan-then-Execute                   │ 操控主角
        │ 技能库 + 知识库                     │
        ▼                                    ▼
   DeepSeek-V3                           Minecraft 玩家
```

## 快速开始

### 1. Python 端

```bash
cd minecraft_agent
cp .env.example .env   # 填写 DEEPSEEK_API_KEY 等
pip install -r requirements.txt
python load_knowledge_base.py   # 首次需加载知识库
python load_demonstrations.py   # 可选：预置演示技能（模仿 MineDojo）
python main.py                  # 启动 WebSocket 服务
```

### 2. Minecraft Mod

- 编译 `mc_fabric_mod`（需 Gradle）
- 安装 Fabric Loader + Fabric API（MC 1.21.1）
- 将 Mod 放入 `mods` 目录
- 启动游戏，Mod 自动连接 `ws://localhost:8765`

### 3. 游戏内

- 在聊天框输入任务，如「帮我砍 5 个木头」
- Agent 会规划并操控你的角色执行

## 项目结构

| 目录/文件 | 说明 |
|-----------|------|
| `minecraft_agent/` | Python Agent（ReAct、技能库、RAG、记忆） |
| `mc_fabric_mod/` | Fabric Mod（客户端输入模拟 + 服务端行动执行） |
| `docs/` | 扩展指南（战斗、重置等） |

## 核心能力

- **任务执行**：砍树、挖矿、合成、附魔、与实体互动
- **技能学习**：成功轨迹抽象为参数化技能，可复用
- **自主探索**：空闲时根据 game_state 自动提议并执行任务（Voyager 风格）
- **知识问答**：RAG 检索知识库，回答 Minecraft 相关问题
- **输入模拟**：客户端模拟 WASD + 鼠标，实现 Voyager 式操控

## 环境变量

| 变量 | 说明 |
|------|------|
| `DEEPSEEK_API_KEY` | API Key（硅基流动） |
| `DEEPSEEK_V3_MODEL` | 统一模型，如 `deepseek-ai/DeepSeek-V3` |
| `WS_PORT` | WebSocket 端口，默认 8765 |
| `MAX_REACT_STEPS` | ReAct 最大步数，默认 10 |

详见 `.env.example`。

## 文档

| 文档 | 说明 |
|------|------|
| [架构与运行逻辑](docs/ARCHITECTURE.md) | 系统架构、消息协议、理想运行流程、模块职责 |
| [项目评估](docs/PROJECT_EVALUATION.md) | 架构评分、技术栈、待改进项、后续方向 |
| [战斗与自主探索扩展](docs/COMBAT_AND_AUTONOMY.md) | 战斗动作、自主探索实现与扩展 |
| [Agent 重置指南](docs/RESET_AGENT.md) | 技能库、记忆、知识库重置方法 |
| [Voyager 差异与演示技能](docs/VOYAGER_AND_DEMO.md) | 与 MineDojo 的差异、演示加载、改进方向 |

## 本地开发与推送

### 更新本地仓库并推送到 GitHub

```bash
# 1. 查看当前状态
git status

# 2. 添加所有更改（或指定文件）
git add .

# 3. 提交
git commit -m "docs: 新增架构文档，更新项目评估与 README"

# 4. 推送到远程（首次需配置远程仓库与认证）
git push origin main
```

**认证说明**：若 `git push` 失败，需配置 GitHub 凭据：

- **HTTPS**：使用 [Personal Access Token](https://github.com/settings/tokens) 替代密码
- **SSH**：配置 SSH 密钥并改用 `git@github.com:用户名/Minecraft_agent.git`

## 许可证

MIT
