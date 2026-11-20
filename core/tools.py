"""暴露给**主 Agent**的调度工具。

两个工具：

* ``pmind_wake``：临时提醒（一次性、可多个、不循环）——「两分钟后回复我」「到点提醒我」；
* ``pmind_schedule``：她自己安排三类节奏里的任意一类
  - ``kind=dialogue``：对话节奏（恒 1，覆盖）
  - ``kind=hobby``：爱好节奏（恒 1，覆盖；创建前先写记忆，符合"先记忆后创建"）
  - ``kind=temp``：同 ``pmind_wake``

注意：对话/临时周期的"主动写记忆"由她自己决定——需要留记忆时，直接调用
``memorize_long_term_memory`` 工具即可（它本来就在可用工具里）。
"""

from __future__ import annotations

import logging
from dataclasses import field
from typing import Any

from pydantic.dataclasses import dataclass

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from . import jobs as jobs_mod
from . import timeutil

LOGGER_NAME = "astrbot"
WAKE_TOOL_NAME = "pmind_wake"
SCHEDULE_TOOL_NAME = "pmind_schedule"

_KIND_DESC = "目标节奏：dialogue=对话节奏（只会有一个，覆盖旧的）；hobby=爱好节奏（只会有一个，覆盖旧的）；temp=临时提醒（可多个、一次性）"


@dataclass
class ProactiveWakeTool(FunctionTool[AstrAgentContext]):
    """让机器人给自己加一条临时提醒。"""

    __pydantic_config__ = {"arbitrary_types_allowed": True}

    plugin: Any = None
    name: str = WAKE_TOOL_NAME
    description: str = (
        "加一条一次性临时提醒：到点你会被唤醒，处理 note 里写的事情，然后结束（不会循环）。"
        "用于『X 分钟后回复我』『到点提醒我』这类；到点后自己决定说不说话、要不要写记忆。"
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "after": {
                    "type": "string",
                    "description": "多久之后醒来，例如 '2m'、'90s'、'1h'。与 at 二选一。",
                },
                "at": {
                    "type": "string",
                    "description": "绝对时间点，ISO8601，例如 2026-11-18T09:30:00+08:00。与 after 二选一。",
                },
                "note": {
                    "type": "string",
                    "description": "写给自己看的备注：到点要做什么。越具体越好。",
                },
            },
            "required": ["note"],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        after: str = "",
        at: str = "",
        note: str = "",
        **_: Any,
    ) -> ToolExecResult:
        return await _schedule_via_plugin(
            self.plugin,
            context,
            kind=jobs_mod.KIND_TEMP,
            after=after,
            at=at,
            note=note,
            intent="",
            summary="",
        )


@dataclass
class ProactiveScheduleTool(FunctionTool[AstrAgentContext]):
    """让机器人自己安排对话/爱好/临时三类节奏。"""

    __pydantic_config__ = {"arbitrary_types_allowed": True}

    plugin: Any = None
    name: str = SCHEDULE_TOOL_NAME
    description: str = (
        "安排你自己的节奏作业（三类）：dialogue=对话节奏、hobby=爱好节奏、temp=临时提醒。"
        "对话/爱好各自只会有一个，设置即覆盖旧时间；临时提醒可多个且只执行一次。"
        "创建爱好节奏时，可用 intent 写清『下一轮要做什么』，并提供 summary（本轮总结）——"
        "会先把这段总结写进记忆，再创建爱好作业。"
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["dialogue", "hobby", "temp"],
                    "description": _KIND_DESC,
                },
                "after": {
                    "type": "string",
                    "description": "多久之后，例如 '2m'、'3h'、'1d'。与 at 二选一。",
                },
                "at": {
                    "type": "string",
                    "description": "绝对时间，ISO8601（带时区）。与 after 二选一。",
                },
                "note": {
                    "type": "string",
                    "description": "kind=temp 时的备注：到点要做什么。",
                },
                "intent": {
                    "type": "string",
                    "description": "kind=hobby 时的下轮计划：下次醒来打算做什么（会存进作业与提示词）。",
                },
                "summary": {
                    "type": "string",
                    "description": "可选：本轮总结（自然语言）。给了就先写入记忆，再创建作业。",
                },
            },
            "required": ["kind"],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        kind: str = "",
        after: str = "",
        at: str = "",
        note: str = "",
        intent: str = "",
        summary: str = "",
        **_: Any,
    ) -> ToolExecResult:
        normalized = str(kind or "").strip().lower()
        if normalized not in jobs_mod.KINDS:
            return "error: kind 只能是 dialogue / hobby / temp"
        return await _schedule_via_plugin(
            self.plugin,
            context,
            kind=normalized,
            after=after,
            at=at,
            note=note,
            intent=intent,
            summary=summary,
        )


async def _schedule_via_plugin(
    plugin,
    context,
    *,
    kind: str,
    after: str,
    at: str,
    note: str,
    intent: str,
    summary: str,
) -> ToolExecResult:
    """工具共用的调度落地逻辑。"""
    log = getattr(plugin, "log", logging.getLogger(LOGGER_NAME))
    if plugin is None:
        return "error: 主动心智插件未就绪"
    if not plugin.settings.enabled:
        return "error: 主动心智当前已停用（basic.enabled=false）"
    if not plugin.settings.expose_wake_tool:
        return "error: 主动调度工具已被配置关闭"

    event = getattr(getattr(context, "context", None), "event", None)
    umo = str(getattr(event, "unified_msg_origin", "") or "")
    if not umo:
        return "error: 拿不到会话 ID，无法安排作业"

    tz = plugin.scheduler.tz
    now = timeutil.now(tz)
    when = None
    if str(after or "").strip():
        when = timeutil.parse_wake_time(str(after).strip(), reference=now, tz=tz)
    if when is None and str(at or "").strip():
        when = timeutil.parse_wake_time(str(at).strip(), reference=now, tz=tz)
    if when is None:
        return "error: 时间无法解析，请用 after='2m' 或 at='2026-11-18T09:30:00+08:00'"

    if kind == jobs_mod.KIND_HOBBY:
        # 规则：创建新的爱好作业之前先写记忆（用模型给的总结；没给就用 intent 兜底）
        text = (summary or intent or "").strip()
        if text and plugin.settings.write_memory:
            await plugin.write_memory_text(
                umo,
                text,
                topics=["proactive_mind", "trigger:hobby", plugin.settings.hobby_memory_tag],
                importance=plugin.settings.hobby_importance,
                reason="创建爱好节奏前的本轮总结",
            )
        result = await plugin.set_job_time(
            umo, jobs_mod.KIND_HOBBY, timeutil.iso(when), intent=intent or summary or None
        )
    elif kind == jobs_mod.KIND_DIALOGUE:
        result = await plugin.set_job_time(umo, jobs_mod.KIND_DIALOGUE, timeutil.iso(when))
    else:
        item = await plugin.schedule_explicit(
            umo, when, (note or "临时提醒").strip(), created_by="pmind_wake"
        )
        result = (
            {"ok": True, "next_at": item.get("next_at"), "human": timeutil.human(timeutil.from_iso(item.get("next_at")))}
            if item
            else {"ok": False, "error": "临时作业已达上限"}
        )

    if not result or not result.get("ok"):
        return f"error: 安排失败：{(result or {}).get('error') or 'unknown'}"
    log.info(f"[proactive_mind] 主 Agent 安排了 {kind} 作业：{umo} → {result.get('human')}")
    label = jobs_mod.KIND_LABELS.get(kind, kind)
    return (
        f"已安排{label}节奏：{result.get('human')}。"
        + ("（临时提醒只执行一次）" if kind == jobs_mod.KIND_TEMP else "")
        + "现在可以正常结束这一轮。"
    )
