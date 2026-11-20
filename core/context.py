"""提示词与上下文构造（单上下文：人格一份、流水一份）。

三种周期共用一套 system 规则，只是 user 块不同：

* ``dialogue``：状态 + 最近流水 + 工具目录
* ``hobby``   ：状态 + 爱好记忆 + 近期流水 + 工具目录（小目录）
* ``temp``    ：状态 + 近期流水 + 待办备注 + 工具目录

同时负责"不复读"的注入策略：旧主动消息、旧判断措辞都不会再回放。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from . import jobs as jobs_mod
from . import memorydb, timeutil

LOGGER_NAME = "astrbot"

KIND_RULES = {
    jobs_mod.KIND_DIALOGUE: "现在是【对话节奏】的到点：你自己定了这个时刻回来看一眼。",
    jobs_mod.KIND_HOBBY: "现在是【爱好节奏】的到点：这一段是你自己的时间，去做点你感兴趣的事。",
    jobs_mod.KIND_TEMP: "现在是【临时提醒】到点：之前你给自己留了下面这件事要办。",
}

TRIGGER_LABELS = {
    "user_message": "用户刚说完话（等同于对话节奏）",
    "dialogue_cron": "对话节奏到点",
    "hobby_cron": "爱好节奏到点",
    "temp_cron": "临时提醒到点",
    "manual": "手动触发",
    "page": "面板触发",
}

BASE_RULES = """你是同一个角色的「主动心智」部分。此刻没有人正在跟你对话，你要独立决定这一轮做什么、要不要开口。

【硬性纪律】
- 保持上面的人格、语气、关系，不要变成助手口吻；不要解释你在做什么，也不要提到"决策""JSON""模块"。
- 只依据下面给出的流水、记忆和工具结果，不要编造事实。
- 不复读：不要再用你上一轮判断的原话或句式；同一件事不要反复催；已经翻篇/作罢的旧话题不要再拿到桌上。
- 允许沉默：没什么可说、说也不合适时，message 给 null，只把记忆和时间处理好。
- 每次用新的、贴合此刻的说法；旧事若要提，只给结论，不再索要回答。
- 输出必须是一个 JSON 对象：不要代码块、不要注释、JSON 之外不要任何文字。"""

OUTPUT_SCHEMA = """只输出 JSON：
{
  "act": true 或 false,                  // 这一轮是否真的要做事/开口
  "reason": "一句话理由（第一人称，不复述上一轮）",
  "thought": "内心想法，1-3 句",
  "steps": [{"tool": "工具名", "args": {}}],   // 不需要就 []
  "message": "要说的话；不发消息给 null",
  "memory": "本轮总结（自然语言，写进记忆库；爱好轮还必须写清下一轮打算做什么）",
  "dialogue_next_at": "ISO8601 或 +90m 或 null",
  "hobby_next_at": "ISO8601 或 +3h 或 null"
}"""

# 对话/临时周期：记忆由她通过工具自己决定，不在 JSON 里写
OUTPUT_SCHEMA_NO_MEMORY = """只输出 JSON：
{
  "act": true 或 false,                  // 这一轮是否真的要做事/开口
  "reason": "一句话理由（第一人称，不复述上一轮）",
  "thought": "内心想法，1-3 句",
  "steps": [{"tool": "工具名", "args": {}}],   // 不需要就 []
  "message": "要说的话；不发消息给 null",
  "dialogue_next_at": "ISO8601 或 +90m 或 null",
  "hobby_next_at": "ISO8601 或 +3h 或 null"
}"""

MEMORY_BY_YOURSELF = (
    "记忆由你自己决定：想让这件事留在记忆里，就在 steps 里调用"
    " memorize_long_term_memory（memory=你的自然语言总结）；不想留就不调。"
)


def build_system(persona_prompt: str, settings) -> str:
    """人格 + 规则（每次周期只拼一次）。"""
    blocks = [persona_prompt.strip() or "你是一个有自己节奏的聊天搭子。", BASE_RULES]
    extra = settings.persona_extra_instruction.strip()
    if extra:
        blocks.append(f"补充要求：\n{extra}")
    return "\n\n".join(blocks)


def _minutes_since(stamp_iso: Any, now: datetime) -> str:
    stamp = timeutil.from_iso(stamp_iso) if stamp_iso else None
    if stamp is None:
        return "未知"
    delta = (now - stamp).total_seconds()
    if delta < 0:
        return "刚刚"
    if delta < 90:
        return f"{int(delta)} 秒"
    if delta < 5400:
        return f"{int(delta // 60)} 分钟"
    if delta < 172800:
        return f"{delta / 3600:.1f} 小时"
    return f"{delta / 86400:.1f} 天"


def build_state_block(
    *,
    settings,
    session: dict[str, Any],
    kind: str,
    trigger: str,
    now: datetime,
    tz_name: str,
    flow_count: int = 0,
    memory_count: int = 0,
    temp_note: str = "",
) -> str:
    """状态块：只讲"现在的处境"，不回放旧话。"""
    jobs = jobs_mod.ensure_jobs(session)
    cap = settings.max_consecutive_wakes
    cap_text = "无限" if cap == -1 else str(cap)
    lines = [
        f"当前时间：{timeutil.human(now)}（{tz_name}）",
        f"触发：{TRIGGER_LABELS.get(trigger, trigger)}",
        f"周期类型：{jobs_mod.KIND_LABELS.get(kind, kind)}",
    ]

    # 上次主动说话：只在足够近的时候回放，避免旧话反复锚定
    last_sent_at = timeutil.from_iso(session.get("last_sent_at"))
    if last_sent_at is not None:
        age_hours = (now - last_sent_at).total_seconds() / 3600
        if age_hours <= settings.recent_sent_hours and session.get("last_sent_message"):
            lines.append(
                f"我 {_minutes_since(session.get('last_sent_at'), now)} 前主动说过："
                f"{str(session.get('last_sent_message'))[:200]}"
            )
        else:
            lines.append(f"我上次主动说话：{_minutes_since(session.get('last_sent_at'), now)}前（内容不再回放）")
    else:
        lines.append("我上次主动说话：无")

    # 上次只留结论，不留措辞（防止逐轮复读）
    last_gate = session.get("last_gate") or {}
    if isinstance(last_gate, dict) and last_gate.get("act") is not None:
        lines.append(f"上次我的判断：{'行动' if last_gate.get('act') else '不打扰'}")
    else:
        lines.append("上次我的判断：无")

    lines.extend(
        [
            f"距用户上次说话：{_minutes_since(session.get('last_user_message_at'), now)}",
            f"连续触发次数：{int(session.get('silent_wake_count') or 0)} / 上限 {cap_text}",
            f"本次流水：{flow_count} 条"
            + (f"；爱好记忆：{memory_count} 条" if kind == jobs_mod.KIND_HOBBY else ""),
            "作业安排：",
            jobs_mod.summarize_for_prompt(jobs, now),
        ]
    )
    if temp_note:
        lines.append(f"这次的临时备注：{temp_note[:400]}")
    return "\n".join(lines)


def build_dialogue_user(state_block: str, flow_text: str, catalog_text: str) -> str:
    return (
        f"【状态】\n{state_block}\n\n"
        f"【最近对话流水】\n{flow_text}\n\n"
        f"【可用工具】\n{catalog_text}\n\n"
        "【本轮任务】\n"
        "1) 这段对话现在需要你主动做点什么吗？还是保持安静更好？\n"
        "2) 需要的话，按顺序调用工具，并想好要对用户说的话。\n"
        "3) 如果你开口了（message 非 null），必须给出 dialogue_next_at：你下一次想在什么时候回来看一眼。\n"
        f"4) {MEMORY_BY_YOURSELF}\n\n"
        f"{OUTPUT_SCHEMA_NO_MEMORY}"
    )


def build_hobby_user(
    state_block: str,
    memories_text: str,
    flow_text: str,
    catalog_text: str,
) -> str:
    return (
        f"【状态】\n{state_block}\n\n"
        f"【我的爱好记忆（长期，最近的在前）】\n{memories_text}\n\n"
        f"【近期对话流水】\n{flow_text}\n\n"
        f"【可用工具】\n{catalog_text}\n\n"
        "【本轮任务】\n"
        "这是你自己的时间：从上面的爱好记忆里挑一件此刻想做的事，或者顺着最近的兴趣继续挖。\n"
        "1) 决定这一轮具体做什么（可调工具；也可以在记忆里只有念头时先做点功课）。\n"
        "2) 决定要不要给用户发消息：有值得分享的、且不打扰，就发；否则 message 给 null。\n"
        "3) memory 必须写两件事：这一轮你做了什么/看到什么，以及**下一轮你打算做什么**（下一轮只有这条记忆可依赖）。\n"
        "4) 给出 hobby_next_at：下一轮什么时候继续。\n\n"
        f"{OUTPUT_SCHEMA}"
    )


def build_temp_user(state_block: str, flow_text: str, catalog_text: str, note: str) -> str:
    return (
        f"【状态】\n{state_block}\n\n"
        f"【最近对话流水】\n{flow_text}\n\n"
        f"【可用工具】\n{catalog_text}\n\n"
        "【本轮任务】\n"
        f"到点了，办你之前给自己留的这件事：{note or '（没有备注）'}\n"
        "办完之后决定：要不要开口（message 或 null）。\n"
        f"{MEMORY_BY_YOURSELF}\n"
        "临时提醒只有这一次，不需要再安排下一次。\n\n"
        f"{OUTPUT_SCHEMA_NO_MEMORY}"
    )


def build_hobby_memory_block(items: list[memorydb.HobbyMemory], max_chars: int = 6000) -> str:
    return memorydb.render_memories(items, max_chars=max_chars)


def hobby_window_hours(settings) -> float:
    """爱好轮流水的回溯窗口（默认跟随 flow_max_age_hours）。"""
    return float(settings.flow_max_age_hours or 24)


def now_minus(hours: float) -> datetime:
    return timeutil.now() - timedelta(hours=hours)
