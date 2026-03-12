"""
任务轨迹落盘：为后续 SFT/RL 提供结构化数据。

目录：data/task_trajectories/YYYY-MM-DD/
文件名：HH-mm-ss__<task_slug>__attempt_<n>__<success|failed>.json
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path

from loguru import logger

# 根目录：minecraft_agent/data/task_trajectories（与 cwd 无关）
_AGENT_DIR = Path(__file__).resolve().parent
TRAJECTORIES_ROOT = str(_AGENT_DIR.parent / "data" / "task_trajectories")


def _task_slug(task: str, max_len: int = 30) -> str:
    """任务描述转文件名安全片段，截断并替换非法字符。"""
    s = (task or "").strip()[:max_len]
    s = re.sub(r'[/\\:*?"<>|\n\r]', "_", s)
    s = s.replace(" ", "_")
    return s or "task"


def _skill_names_from_codes(skill_codes: list[str]) -> list[str]:
    """从技能 code 列表中提取函数名（async function name）。"""
    names = []
    for code in skill_codes or []:
        m = re.search(r"async\s+function\s+(\w+)\s*\(", code)
        if m:
            names.append(m.group(1))
    return names


def save_task_trajectory(
    task: str,
    attempt: int,
    code: str,
    execution_success: bool,
    execution_error: str | None,
    critic_ok: bool,
    critic_message: str,
    output: str,
    game_state_after: dict,
    skill_codes: list[str],
    run_id: str | None = None,
    game_state_before: dict | None = None,
    base_dir: str | None = None,
    llm_prompt_user: str | None = None,
    llm_response_raw: str | None = None,
    rag_context: str | None = None,
) -> str | None:
    """
    将单次任务尝试写入 data/task_trajectories/YYYY-MM-DD/ 下：
    - 单文件 JSON（含完整 payload，便于程序消费）
    - Voyager 式子目录：code.js, description.txt, execution_output.txt,
      llm_prompt_user.txt, llm_response_raw.txt, rag_context.txt（便于人工核对 RAG/重试内容）

    Returns:
        写入的 JSON 文件路径，失败返回 None。
    """
    base = base_dir or TRAJECTORIES_ROOT
    now = datetime.now()
    date_dir = now.strftime("%Y-%m-%d")
    slug = _task_slug(task)
    outcome = "success" if (execution_success and critic_ok) else "failed"
    base_name = f"{now.strftime('%H-%M-%S')}__{slug}__attempt_{attempt}__{outcome}"
    filename = base_name + ".json"
    dir_path = Path(base) / date_dir
    try:
        dir_path.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.warning(f"[Trajectory] 创建目录失败 {dir_path}: {e}")
        return None

    payload = {
        "run_id": run_id or now.strftime("%Y-%m-%d_%H-%M-%S"),
        "task": task,
        "attempt": attempt,
        "code": code,
        "execution_success": execution_success,
        "execution_error": execution_error,
        "critic_ok": critic_ok,
        "critic_message": critic_message,
        "output": (output or "")[:5000],
        "game_state_before": game_state_before,
        "game_state_after": game_state_after,
        "timestamp": now.isoformat(),
        "skill_codes_injected": _skill_names_from_codes(skill_codes),
    }
    if llm_prompt_user is not None:
        payload["llm_prompt_user"] = (llm_prompt_user or "")[:8000]
    if llm_response_raw is not None:
        payload["llm_response_raw"] = (llm_response_raw or "")[:8000]
    if rag_context is not None:
        payload["rag_context"] = (rag_context or "")[:4000]

    file_path = dir_path / filename
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logger.debug(f"[Trajectory] 已写入 {file_path}")
    except Exception as e:
        logger.warning(f"[Trajectory] 写入失败 {file_path}: {e}")
        return None

    # Voyager 式子目录：code / description / execution_output / llm_* / rag_context
    subdir = dir_path / base_name
    try:
        subdir.mkdir(parents=True, exist_ok=True)
        (subdir / "code.js").write_text(code or "", encoding="utf-8")
        (subdir / "description.txt").write_text(
            f"task: {task}\nattempt: {attempt}\nsuccess: {outcome == 'success'}\ncritic_ok: {critic_ok}\n",
            encoding="utf-8",
        )
        (subdir / "execution_output.txt").write_text(output or "", encoding="utf-8")
        if llm_prompt_user is not None:
            (subdir / "llm_prompt_user.txt").write_text(llm_prompt_user or "", encoding="utf-8")
        if llm_response_raw is not None:
            (subdir / "llm_response_raw.txt").write_text(llm_response_raw or "", encoding="utf-8")
        if rag_context is not None:
            (subdir / "rag_context.txt").write_text(rag_context or "", encoding="utf-8")
    except Exception as e:
        logger.warning(f"[Trajectory] Voyager 子目录写入失败 {subdir}: {e}")

    return str(file_path)
