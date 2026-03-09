"""
技能模板执行器（混合方案核心）

将参数化技能（parameterized）解析为具体动作序列并执行，
实现「任意地点复用」：运行时从 find_resource 获取坐标，无需 LLM 每步规划。

支持的 procedure 结构：
  - find_resource + store_as: 解析 observation 中的坐标列表
  - for_each (over targets, limit N): 对每个目标执行 move_to + mine_block
"""

import re
from loguru import logger

# 从 find_resource / scan_area 的 observation 中解析坐标的正则
# 格式示例: "找到 3 处 [diamond]：(12,-58,-23) [minecraft:xxx]  (-5,-61,8) ..."
COORD_PATTERN = re.compile(r"\((-?\d+),(-?\d+),(-?\d+)\)")

# 任务描述到 find_resource type 的映射（中文/常见说法）
TASK_TO_RESOURCE_TYPE = {
    "煤": "coal", "煤矿": "coal", "coal": "coal",
    "铁": "iron", "铁矿": "iron", "iron": "iron",
    "金": "gold", "金矿": "gold", "gold": "gold",
    "钻石": "diamond", "diamond": "diamond",
    "铜": "copper", "铜矿": "copper", "copper": "copper",
    "青金石": "lapis", "lapis": "lapis",
    "红石": "redstone", "redstone": "redstone",
    "绿宝石": "emerald", "emerald": "emerald",
    "橡木": "oak_log", "橡树": "oak_log", "木头": "tree", "原木": "log",
    "桦木": "birch_log", "云杉": "spruce_log", "丛林": "jungle_log",
    "沙子": "sand", "sand": "sand",
    "沙砾": "gravel", "gravel": "gravel",
}


def parse_coords_from_observation(observation: str) -> list[tuple[int, int, int]]:
    """
    从 find_resource / scan_area 的 observation 字符串中解析坐标列表。

    Returns:
        [(x, y, z), ...] 按出现顺序
    """
    coords = []
    for m in COORD_PATTERN.finditer(observation):
        x, y, z = int(m.group(1)), int(m.group(2)), int(m.group(3))
        coords.append((x, y, z))
    return coords


def extract_params_from_task(task: str, skill: dict) -> dict:
    """
    从任务描述中提取参数（block_type, count 等）。
    使用简单规则 + 关键词映射，避免额外 LLM 调用。
    """
    params = {}
    task_lower = task.lower().strip()

    # 从 skill 的 params_schema 获取默认值
    schema = skill.get("params_schema", {})
    if "count" in schema:
        params["count"] = schema["count"].get("default", 5)
    if "radius" in schema:
        r = schema["radius"]
        if isinstance(r, dict) and "value" in r:
            params["radius"] = r["value"]
        else:
            params["radius"] = 24

    # 提取数量：挖5个、砍20棵、收集10个、要3个
    count_match = re.search(r"(\d+)\s*[个棵块堆]", task)
    if count_match:
        params["count"] = int(count_match.group(1))

    # 提取资源类型
    block_type = None
    for keyword, res_type in TASK_TO_RESOURCE_TYPE.items():
        if keyword in task or keyword in task_lower:
            block_type = res_type
            break
    if block_type:
        params["block_type"] = block_type
    else:
        # 兜底：常见默认
        if "挖" in task or "矿" in task:
            params["block_type"] = "coal"
        elif "砍" in task or "树" in task or "木" in task:
            params["block_type"] = "oak_log"
        elif "沙" in task:
            params["block_type"] = "sand"

    return params


def can_execute_as_template(skill: dict) -> bool:
    """判断技能是否为可执行的参数化模板"""
    return (
        skill.get("template_type") == "parameterized"
        or skill.get("skill_type") == "parameterized"
    ) and "procedure" in skill


async def execute_skill_template(
    skill: dict,
    task: str,
    execute_action_fn,
) -> tuple[bool, list[dict], str]:
    """
    执行参数化技能模板。

    Args:
        skill: 参数化技能 JSON（含 procedure）
        task: 用户任务描述（用于解析 block_type, count）
        execute_action_fn: async (action, params) -> observation
           调用方负责在内部更新 game_state（如 agent._execute_action）

    Returns:
        (success, trajectory, final_message)
    """
    if not can_execute_as_template(skill):
        return False, [], ""

    procedure = skill.get("procedure", [])
    if not procedure:
        return False, [], ""

    params = extract_params_from_task(task, skill)
    block_type = params.get("block_type")
    count = params.get("count", 5)
    radius = params.get("radius", 24)

    if not block_type:
        logger.warning("参数化技能：无法从任务中解析 block_type")
        return False, [], ""

    trajectory: list[dict] = []
    targets: list[tuple[int, int, int]] = []
    ctx = {"targets": targets, "current_index": 0}

    for i, step in enumerate(procedure):
        action = step.get("action", "")
        step_params = step.get("params", {})
        store_as = step.get("store_as")
        do_steps = step.get("do", [])
        over = step.get("over")
        limit = step.get("limit", "{{count}}")

        # 解析模板变量
        def resolve(v):
            if isinstance(v, str):
                v = v.replace("{{block_type}}", str(block_type))
                v = v.replace("{{radius}}", str(radius))
                v = v.replace("{{count}}", str(count))
            return v

        # find_resource
        if action == "find_resource":
            resolved = {k: resolve(v) for k, v in step_params.items()}
            obs = await execute_action_fn("find_resource", resolved)
            trajectory.append({
                "step": len(trajectory) + 1,
                "thought": f"[Template] 搜索 {block_type}",
                "action": "find_resource",
                "action_params": resolved,
                "observation": obs,
            })
            if "未找到" in obs or "未发现" in obs:
                return False, trajectory, obs
            coords = parse_coords_from_observation(obs)
            if store_as:
                ctx[store_as] = coords
                targets = coords

        # for_each
        elif action == "for_each":
            limit_val = count
            if isinstance(limit, str):
                limit_val = int(resolve(limit)) if limit else count
            items = ctx.get(over, [])[:limit_val]
            for idx, item in enumerate(items):
                x, y, z = item[0], item[1], item[2]
                for sub in do_steps:
                    sub_action = sub.get("action", "")
                    if sub_action == "move_to":
                        obs = await execute_action_fn(
                            "move_to", {"region_center": [x, y, z], "radius": 2}
                        )
                        trajectory.append({
                            "step": len(trajectory) + 1,
                            "thought": f"[Template] 移动到 ({x},{y},{z}) 附近",
                            "action": "move_to",
                            "action_params": {"region_center": [x, y, z], "radius": 2},
                            "observation": obs,
                        })
                    elif sub_action == "mine_block":
                        obs = await execute_action_fn(
                            "mine_block", {"x": x, "y": y, "z": z}
                        )
                        trajectory.append({
                            "step": len(trajectory) + 1,
                            "thought": f"[Template] 挖掘 ({x},{y},{z})",
                            "action": "mine_block",
                            "action_params": {"x": x, "y": y, "z": z},
                            "observation": obs,
                        })
                        if "失败" in obs or "无法" in obs:
                            return False, trajectory, obs

    success_msg = f"已按模板完成，采集 {min(len(targets), count)} 个目标"
    return True, trajectory, success_msg
