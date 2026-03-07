"""
agent 包 - Minecraft AI Agent 核心模块（v2）

包含：
- ReactAgent        : ReAct 推理循环（简单任务）+ 层级任务执行（复杂任务）
- TaskPlanner       : 层级任务分解器（任务→子任务列表）
- SubTask           : 子任务数据结构
- MemoryManager     : 三层记忆系统
- SkillLibrary      : 技能自动抽象与检索（支持 simple / hierarchical 两种类型）
- PersonalitySystem : 情绪与关系系统
- LLMRouter         : 分层 LLM 调用路由
- ConnectionManager : WebSocket 连接管理（请求-响应异步匹配）
"""

from .react_agent import ReactAgent
from .planner import TaskPlanner, SubTask
from .memory import ShortTermMemory, LongTermMemory, MemoryManager
from .skill_library import SkillLibrary
from .personality import PersonalitySystem
from .llm_router import LLMRouter
from .connection_manager import connection_manager, ConnectionManager

__all__ = [
    "ReactAgent",
    "TaskPlanner",
    "SubTask",
    "ShortTermMemory",
    "LongTermMemory",
    "MemoryManager",
    "SkillLibrary",
    "PersonalitySystem",
    "LLMRouter",
    "connection_manager",
    "ConnectionManager",
]
