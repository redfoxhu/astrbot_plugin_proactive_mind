"""三类唤醒作业的纯逻辑：默认结构、id、时间钳制、上限。

- dialogue：恒 1 个，只在"本轮说了话"后更新（必须更新），有兜底间隔；
- hobby：恒 1 个，循环创建；创建前先写记忆（本轮总结 + 下轮计划）；
- temp：0..N 个，一次性，触发后不重建。

本模块不碰 asyncio / AstrBot，便于单测。
"""

from __future__ import annotations

import copy
import uuid
from datetime import datetime
from typing import Any

KIND_DIALOGUE = "dialogue"
KIND_HOBBY = "hobby"
KIND_TEMP = "temp"
KINDS = (KIND_DIALOGUE, KIND_HOBBY, KIND_TEMP)

KIND_LABELS = {
    KIND_DIALOGUE: "对话",
    KIND_HOBBY: "爱好",
    KIND_TEMP: "临时",
}


def default_jobs() -> dict[str, Any]:
    """会话里 jobs 字段的默认结构。"""
    return {
        "dialogue": {
            "next_at": None,
            "updated_at": None,
            "last_spoke_at": None,
            "cancelled": False,
        },
        "hobby": {
            "next_at": None,
            "intent": None,
            "intent_at": None,
            "memory_id": None,
            # runs = 真正到期执行过的轮数（手动/面板触发不计数）
            "runs": 0,
            # scheduled = 排过程次数（供排查，不影响面板文案）
            "scheduled": 0,
            "cancelled": False,
        },
        "temp": [],
    }


def ensure_jobs(session: dict[str, Any]) -> dict[str, Any]:
    """补齐/修正 jobs 结构（幂等）。"""
    jobs = session.get("jobs")
    if not isinstance(jobs, dict):
        jobs = default_jobs()
        session["jobs"] = jobs
    for key, value in default_jobs().items():
        if key not in jobs:
            jobs[key] = copy.deepcopy(value)
        elif isinstance(value, dict) and not isinstance(jobs[key], dict):
            jobs[key] = copy.deepcopy(value)
        elif isinstance(value, list) and not isinstance(jobs[key], list):
            jobs[key] = copy.deepcopy(value)
    if not isinstance(jobs.get("temp"), list):
        jobs["temp"] = []
    return jobs


def new_temp_id() -> str:
    """临时作业 id（短、可读）。"""
    return "t-" + uuid.uuid4().hex[:6]


def find_temp(jobs: dict[str, Any], temp_id: str) -> dict[str, Any] | None:
    """按 id 找临时作业。"""
    for item in jobs.get("temp") or []:
        if isinstance(item, dict) and item.get("id") == temp_id:
            return item
    return None


def sort_temp(jobs: dict[str, Any]) -> list[dict[str, Any]]:
    """按时间排序的临时作业（缺时间的排最后）。"""
    items = [item for item in (jobs.get("temp") or []) if isinstance(item, dict)]
    return sorted(items, key=lambda item: str(item.get("next_at") or "9999"))


def clamp_interval(
    when: datetime | None,
    *,
    now: datetime,
    min_minutes: int,
    max_minutes: int,
    default_minutes: int,
) -> datetime:
    """把模型给的时间点钳制进 [now+min, now+max]，缺失用默认间隔。"""
    from . import timeutil

    return timeutil.clamp_wake(
        when,
        reference=now,
        min_minutes=min_minutes,
        max_minutes=max_minutes,
        default_minutes=default_minutes,
    )


def remaining_seconds(next_at: Any, now: datetime) -> int | None:
    """距离下次触发的秒数（None 表示没排）。"""
    from . import timeutil

    target = timeutil.from_iso(next_at) if next_at else None
    if target is None:
        return None
    return max(0, int((target - now).total_seconds()))


def summarize_for_prompt(jobs: dict[str, Any], now: datetime) -> str:
    """把三类作业渲染进提示词的状态块。"""
    from . import timeutil

    if not isinstance(jobs, dict):
        jobs = default_jobs()
    lines: list[str] = []

    dialogue = jobs.get("dialogue") or {}
    dialogue_at = timeutil.from_iso(dialogue.get("next_at")) if dialogue.get("next_at") else None
    if dialogue.get("cancelled"):
        lines.append("对话节奏：已取消（不再自动安排对话唤醒）")
    elif dialogue_at:
        lines.append(f"对话节奏：下次 {timeutil.human(dialogue_at)}")
    else:
        lines.append("对话节奏：未安排")

    hobby = jobs.get("hobby") or {}
    hobby_at = timeutil.from_iso(hobby.get("next_at")) if hobby.get("next_at") else None
    if hobby.get("cancelled"):
        lines.append("爱好节奏：已取消")
    elif hobby_at:
        lines.append(f"爱好节奏：下次 {timeutil.human(hobby_at)}（已执行 {int(hobby.get('runs') or 0)} 轮）")
    else:
        lines.append("爱好节奏：未安排")

    temps = sort_temp(jobs)
    if temps:
        preview = "、".join(
            f"{timeutil.human(timeutil.from_iso(item.get('next_at')))}"
            for item in temps[:3]
        )
        lines.append(f"临时提醒：{len(temps)} 个（{preview}{'…' if len(temps) > 3 else ''}）")
    else:
        lines.append("临时提醒：无")
    return "\n".join(lines)
