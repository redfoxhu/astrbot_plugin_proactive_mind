"""三段式决策流水线：是否行动 → 调哪些工具做什么 → 说什么 + 下次何时。

一次 ``run()`` 的完整顺序：
1. 准备：人格、最近流水、状态、当前时间、可用工具；
2. gate：模型判断要不要主动行动；不需要则直接安排下次唤醒并静默结束；
3. plan：模型给出要调用的工具与参数（可以一串）；
4. 执行：串行调用工具，收集结果；
5. compose：模型给出要说的话（可为 null）与下次唤醒时间（已把"即将发送的消息"计入流水）；
6. 发送 → 落库（平台历史 / LivingMemory）→ 写记忆 → 创建唯一的唤醒作业；
7. 全过程写入 trace，供面板查看。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from . import delivery, echo, flow as flow_mod, llm, memory, timeutil, toolbox
from .persona import PersonaResolver

LOGGER_NAME = "astrbot"

TRIGGER_LABELS = {
    "cron": "预定唤醒到点",
    "user_message": "用户刚说完话",
    "manual": "手动触发",
    "page": "面板触发",
    "startup": "插件启动补跑",
}

SYSTEM_RULES = """你是同一个角色的「主动心智」部分：此刻没有人对你说话，你要独自决定接下来要不要主动做点什么。

规则：
- 保持上面的人格、语气、关系与说话习惯，不要变成助手口吻，不要解释你在做什么、不要提到"决策""JSON""模块"。
- 不要编造事实：只能依据对话流水、工具返回结果和你的记忆。不确定就别当成事实说。
- 允许主动：主动开口、回忆往事、自己找点乐子（比如看看视频、查点东西）、整理记忆。
- 允许沉默：刚聊完、对方可能在忙或在睡觉、没什么想说的，都可以决定这次不打扰，只安排下次醒来的时间。
- 你是一个会自己安排节奏的人：下次醒来的时间要合理（几分钟到几小时），不要一直高频窥屏。
- 输出必须是一个 JSON 对象：不要代码块、不要注释、不要 JSON 之外任何文字。"""


def _json_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "y", "是"}:
        return True
    if text in {"false", "0", "no", "n", "否"}:
        return False
    return default


def _json_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


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


class DecisionEngine:
    """主动心智的决策引擎（一个插件实例一个）。"""

    def __init__(
        self,
        *,
        context,
        settings,
        store,
        scheduler,
        data_dir,
        logger: logging.Logger | None = None,
    ) -> None:
        self.context = context
        self.settings = settings
        self.store = store
        self.scheduler = scheduler
        self.data_dir = data_dir
        self.log = logger or logging.getLogger(LOGGER_NAME)
        self.persona = PersonaResolver(context, self.log)
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # 公共入口
    # ------------------------------------------------------------------
    def lock_for(self, umo: str) -> asyncio.Lock:
        """取某会话的互斥锁。"""
        lock = self._locks.get(umo)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[umo] = lock
        return lock

    def tz(self):
        """当前使用的时区。"""
        return self.scheduler.tz if self.scheduler is not None else timeutil.resolve_tz(
            self.settings.timezone_name
        )

    async def run(
        self,
        umo: str,
        trigger: str,
        *,
        mode: str | None = None,
        bypass_limit: bool = False,
    ) -> dict[str, Any]:
        """跑一次完整决策，返回本次 trace。"""
        lock = self.lock_for(umo)
        if lock.locked():
            self.log.info(f"[proactive_mind] {umo} 正在决策中，跳过本次 {trigger}")
            return {"skipped": "busy", "umo": umo, "trigger": trigger}

        async with lock:
            trace = self.store.new_trace(umo, trigger)
            try:
                await self._run_locked(
                    umo, trigger, trace, mode=mode, bypass_limit=bypass_limit
                )
            except asyncio.CancelledError:
                trace["errors"].append("cancelled")
                raise
            except Exception as exc:  # noqa: BLE001 - 单次决策失败不影响作业链
                trace["errors"].append(f"fatal: {exc}")
                self.log.error(
                    f"[proactive_mind] 决策失败 {umo} ({trigger}): {exc}", exc_info=True
                )
                self._ensure_next_wake(umo, trace)
            finally:
                trace["finished_at"] = timeutil.now_iso(self.tz())
                self.store.add_trace(trace)
                self.store.set(
                    umo,
                    last_decision_at=trace["finished_at"],
                    last_trace_id=trace["trace_id"],
                )
                try:
                    await self.store.save()
                except Exception as exc:  # pragma: no cover - 磁盘异常
                    self.log.error(f"[proactive_mind] 状态落盘失败: {exc}")
            return trace

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    async def _run_locked(
        self,
        umo: str,
        trigger: str,
        trace: dict[str, Any],
        *,
        mode: str | None,
        bypass_limit: bool,
    ) -> None:
        settings = self.settings
        log = self.log
        tz = self.tz()
        now = timeutil.now(tz)
        session = self.store.session(umo)

        if not settings.enabled and not bypass_limit:
            trace["reason"] = "插件已关闭（basic.enabled=false）"
            self.store.clear_next_wake(umo, "disabled")
            return

        if trigger == "cron":
            session["last_wake_at"] = timeutil.iso(now)

        effective_mode = mode or (
            settings.user_message_mode if trigger == "user_message" else "decide"
        )

        persona_prompt, persona_id = await self.persona.resolve(umo)
        trace["persona_id"] = persona_id

        items, source, warnings = await flow_mod.read_flow(
            self.context, settings, umo, log
        )
        items = flow_mod.merge_proactive(items, session, settings)
        trace["flow_source"] = source
        trace["flow_count"] = len(items)
        trace["warnings"].extend(warnings)

        flow_text = flow_mod.format_flow(items, settings.flow_max_chars)
        system_prompt = self._system_prompt(persona_prompt)
        state_block = self._state_block(umo, session, trigger, now, len(items), source)

        # ---------------- 1) gate ----------------
        gate, raw, errors = await llm.chat_json(
            self.context,
            settings,
            umo,
            system_prompt=system_prompt,
            user_prompt=self._gate_prompt(state_block, flow_text, settings, trigger),
            log=log,
        )
        trace["errors"].extend(errors)
        if gate is None:
            trace["reason"] = "判断调用失败，按默认间隔安排下次唤醒"
            self._ensure_next_wake(umo, trace, bypass_limit=bypass_limit)
            return

        act = _json_bool(gate.get("act"), False)
        reason = _json_str(gate.get("reason")) or "（模型未给出理由）"
        trace["act"] = act
        trace["reason"] = reason
        session["last_gate"] = {"act": act, "reason": reason}

        if not act or effective_mode == "schedule_only":
            if not act:
                trace["reason_short"] = "不打扰"
            else:
                trace["reason_short"] = "只重排唤醒时间（schedule_only）"
            self._schedule(
                umo,
                gate.get("next_wake_at"),
                trace,
                now=now,
                sent_message=False,
                bypass_limit=bypass_limit,
            )
            return

        # ---------------- 2) plan ----------------
        catalog = toolbox.build_catalog(self.context, settings, log)
        plan, raw, errors = await llm.chat_json(
            self.context,
            settings,
            umo,
            system_prompt=system_prompt,
            user_prompt=self._plan_prompt(
                state_block, flow_text, reason, catalog, settings
            ),
            log=log,
        )
        trace["errors"].extend(errors)
        steps: list[dict[str, Any]] = []
        intent = ""
        if plan is None:
            trace["warnings"].append("计划调用失败，跳过工具直接说话")
        else:
            intent = _json_str(plan.get("intent"))
            raw_steps = plan.get("steps")
            if isinstance(raw_steps, list):
                for step in raw_steps:
                    if isinstance(step, dict) and _json_str(step.get("tool")):
                        steps.append(
                            {
                                "tool": _json_str(step.get("tool")),
                                "args": step.get("args") if isinstance(step.get("args"), dict) else {},
                            }
                        )
        trace["intent"] = intent

        # ---------------- 3) execute ----------------
        records: list[dict[str, Any]] = []
        if steps and settings.max_tool_calls > 0:
            event = toolbox.make_event(self.context, umo)
            records = await toolbox.execute_steps(
                self.context,
                settings,
                steps=steps,
                log=log,
                event=event,
            )
        trace["steps"] = [
            {
                "tool": record.get("tool"),
                "ok": record.get("ok"),
                "error": record.get("error"),
                "result_preview": str(record.get("result") or "")[:300],
            }
            for record in records
        ]

        # ---------------- 4) compose ----------------
        compose, raw, errors = await llm.chat_json(
            self.context,
            settings,
            umo,
            system_prompt=system_prompt,
            user_prompt=self._compose_prompt(
                state_block,
                flow_text,
                reason,
                intent,
                records,
                settings,
                trigger,
            ),
            log=log,
        )
        trace["errors"].extend(errors)

        thought = ""
        message = ""
        next_wake_raw: Any = None
        if compose is None:
            trace["warnings"].append("开口判断失败：只落库现有动作，不发送消息")
        else:
            thought = _json_str(compose.get("thought"))
            raw_message = compose.get("message")
            if raw_message is not None:
                message = delivery.trim_message(
                    _json_str(raw_message, ""), settings.max_message_chars
                )
            next_wake_raw = compose.get("next_wake_at")
        trace["thought"] = thought

        if not message and not settings.allow_empty_message:
            # 配置不允许"只做事不发消息"时，用 intent 兜底成一句话
            message = delivery.trim_message(intent or thought, settings.max_message_chars)
        if not message:
            trace["reason_short"] = "只做事，不说话"

        # ---------------- 5) 发送 ----------------
        send_result: dict[str, Any] = {"ok": False, "error": "empty_message"}
        if message:
            send_result = await delivery.send_text(
                self.context, umo=umo, text=message, log=log
            )
            trace["message"] = message
            trace["message_sent"] = bool(send_result.get("ok"))
            if not send_result.get("ok"):
                trace["errors"].append(f"send_failed: {send_result.get('error')}")
                message = ""
            else:
                session["last_sent_message"] = message
                session["last_sent_at"] = timeutil.iso(now)

        # ---------------- 6) 落库 ----------------
        if message:
            echo_result = await echo.echo_all(
                self.context,
                settings,
                umo=umo,
                text=message,
                session=session,
                log=log,
            )
            trace["echo"] = echo_result
            session["last_echo"] = {
                key: bool(value.get("ok"))
                for key, value in echo_result.items()
                if isinstance(value, dict)
            }
            for key, value in (echo_result or {}).items():
                if isinstance(value, dict) and not value.get("ok"):
                    trace["warnings"].append(
                        f"落库失败({key}): {value.get('error')}"
                    )

        # ---------------- 7) 先定下次唤醒（用于写进记忆） ----------------
        target = self._schedule(
            umo,
            next_wake_raw,
            trace,
            now=now,
            sent_message=bool(message),
            bypass_limit=bypass_limit,
        )

        # ---------------- 8) 记忆 ----------------
        memory_text = memory.build_memory_text(
            trigger=TRIGGER_LABELS.get(trigger, trigger),
            thought=thought,
            intent=intent,
            steps=trace["steps"],
            message=message or None,
            next_wake_human=timeutil.human(target) if target else "未安排（已停止主动唤醒）",
        )
        memory_result = await memory.write_memory(
            self.context,
            settings,
            umo=umo,
            content=memory_text,
            topics=["proactive_mind", f"trigger:{trigger}"],
            reason=f"主动行为：{reason}",
            log=log,
            event=toolbox.make_event(self.context, umo),
            data_dir=self.data_dir,
        )
        trace["memory"] = memory_result

    # ------------------------------------------------------------------
    # 调度
    # ------------------------------------------------------------------
    def _ensure_next_wake(
        self, umo: str, trace: dict[str, Any], *, bypass_limit: bool = False
    ) -> datetime | None:
        """兜底：确保始终有一个下次唤醒（否则作业链会断）。"""
        now = timeutil.now(self.tz())
        return self._schedule(
            umo,
            None,
            trace,
            now=now,
            sent_message=False,
            bypass_limit=bypass_limit,
        )

    def _schedule(
        self,
        umo: str,
        raw_when: Any,
        trace: dict[str, Any],
        *,
        now: datetime,
        sent_message: bool,
        bypass_limit: bool = False,
    ) -> datetime | None:
        """把模型给出的时间点变成真实的唤醒作业，并维护连续触发计数。"""
        settings = self.settings
        log = self.log
        session = self.store.session(umo)

        parsed = timeutil.parse_wake_time(raw_when, reference=now, tz=self.tz())
        if raw_when and parsed is None:
            trace["warnings"].append(f"时间点无法解析：{raw_when!r}，改用默认间隔")
        target = timeutil.clamp_wake(
            parsed,
            reference=now,
            min_minutes=settings.min_interval_minutes,
            max_minutes=settings.max_interval_minutes,
            default_minutes=settings.default_interval_minutes,
        )

        delay = session.get("delay_override_minutes")
        if delay is None:
            delay = settings.wake_delay_minutes
        try:
            delay_int = max(0, int(delay))
        except (TypeError, ValueError):
            delay_int = 0
        if delay_int:
            target = timeutil.plus_minutes(target, delay_int)

        replied = bool(session.get("user_replied_since_last_wake", True))
        try:
            count = int(session.get("silent_wake_count") or 0)
        except (TypeError, ValueError):
            count = 0
        if replied:
            # 用户刚说过话：本轮一定允许创建，并重新开始计数
            count = 1
        elif settings.count_only_when_message_sent and not sent_message:
            # 这次唤醒什么都没做：不推进计数
            count = max(count, 1)
        else:
            count += 1
        session["silent_wake_count"] = count

        cap = settings.max_consecutive_wakes
        blocked = (not replied) and cap != -1 and count > cap
        if blocked and not bypass_limit:
            self.scheduler.cancel(umo)
            session["next_wake_at"] = None
            session["last_error"] = "consecutive_limit_reached"
            trace["schedule_blocked"] = "consecutive_limit_reached"
            trace["next_wake_at"] = None
            log.info(
                f"[proactive_mind] 达到连续唤醒上限（{count} > {cap}），"
                f"暂停为 {umo} 创建新的唤醒作业"
            )
            return None

        try:
            actual = self.scheduler.set_wake(umo, target)
        except Exception as exc:  # pragma: no cover - 调度器异常
            session["last_error"] = f"schedule_failed: {exc}"
            trace["errors"].append(f"schedule_failed: {exc}")
            return None

        session["next_wake_at"] = timeutil.iso(actual)
        session["last_error"] = None
        session["user_replied_since_last_wake"] = False
        trace["next_wake_at"] = session["next_wake_at"]
        log.info(f"[proactive_mind] 为 {umo} 安排下次唤醒：{timeutil.human(actual)}")
        return actual

    # ------------------------------------------------------------------
    # 提示词
    # ------------------------------------------------------------------
    def _system_prompt(self, persona_prompt: str) -> str:
        extra = self.settings.persona_extra_instruction.strip()
        blocks = [persona_prompt.strip() or "你是一个有自己节奏的聊天搭子。", SYSTEM_RULES]
        if extra:
            blocks.append(f"补充要求：\n{extra}")
        return "\n\n".join(blocks)

    def _state_block(
        self,
        umo: str,
        session: dict[str, Any],
        trigger: str,
        now: datetime,
        flow_count: int,
        source: str,
    ) -> str:
        settings = self.settings
        cap = settings.max_consecutive_wakes
        cap_text = "无限" if cap == -1 else str(cap)
        lines = [
            f"当前时间：{timeutil.human(now)}（{self.tz()}）",
            f"触发原因：{TRIGGER_LABELS.get(trigger, trigger)}",
            f"会话：{umo}",
            f"距用户上次说话：{_minutes_since(session.get('last_user_message_at'), now)}",
            f"我上次主动说话：{_minutes_since(session.get('last_sent_at'), now)}",
            f"我上次主动说话内容：{str(session.get('last_sent_message') or '（没有）')[:200]}",
            f"连续触发次数：{int(session.get('silent_wake_count') or 0)} / 上限 {cap_text}",
            f"上次我的判断：{session.get('last_gate') or '（没有）'}",
            f"本次可参考流水：{flow_count} 条（来源 {source}）",
        ]
        return "\n".join(lines)

    def _gate_prompt(
        self, state_block: str, flow_text: str, settings, trigger: str = ""
    ) -> str:
        hint = ""
        if trigger == "user_message":
            hint = (
                "（注意：这次触发是因为你刚回复完用户。通常不需要紧接着再补一句，"
                "除非你有非说不可的新内容或想先做点别的事；否则把 act 设为 false，"
                "只安排下次醒来的时间。）\n"
            )
        elif trigger == "cron":
            hint = (
                "（注意：这次是你自己在之前定下的唤醒时间到点了。"
                "先看看流水里有没有新进展，判断此刻开口是否合适。）\n"
            )
        return (
            f"【状态】\n{state_block}\n\n"
            f"【最近对话流水（{settings.flow_source}）】\n{flow_text}\n\n"
            f"【现在需要你回答】\n{hint}"
            "1) 此刻是否要进行主动行为（主动开口、回忆、自己找点事做）？\n"
            "2) 如果不需要，你下一次想什么时候再醒来看看？\n\n"
            "只输出 JSON：\n"
            '{"act": true/false, "reason": "一句话理由（第一人称）", '
            '"next_wake_at": "下次唤醒时间"}\n'
            "next_wake_at 用带时区的 ISO8601（例：2026-11-16T22:30:00+08:00），"
            "也可以写 +90m 表示 90 分钟后；act=true 时可以留空字符串。"
        )

    def _plan_prompt(
        self,
        state_block: str,
        flow_text: str,
        reason: str,
        catalog: list[dict[str, Any]],
        settings,
    ) -> str:
        return (
            f"【状态】\n{state_block}\n\n"
            f"【最近对话流水】\n{flow_text}\n\n"
            f"【你刚才的判断】要主动行动。理由：{reason}\n\n"
            f"【可用工具（只能从这些里选，最多 {settings.max_tool_calls} 个）】\n"
            f"{toolbox.describe_catalog(catalog)}\n\n"
            "【现在需要你回答】\n"
            "先想清楚要做什么（可以先回忆记忆、再看看视频/网页），"
            "再按执行顺序列出要调用的工具与参数。不需要工具时 steps 给空数组。\n\n"
            "只输出 JSON：\n"
            '{"intent": "你要做的事（一句话）", '
            '"steps": [{"tool": "工具名", "args": {}}]}'
        )

    def _compose_prompt(
        self,
        state_block: str,
        flow_text: str,
        reason: str,
        intent: str,
        records: list[dict[str, Any]],
        settings,
        trigger: str = "",
    ) -> str:
        actions = toolbox.summarize_results(records)
        hint = ""
        if trigger == "user_message":
            hint = (
                "（你刚回复完用户：如果没有新的、非说不可的内容，就直接把 message 给 null，"
                "只把事情记下来并安排下次醒来。）\n"
            )
        return (
            f"【状态】\n{state_block}\n\n"
            f"【最近对话流水】\n{flow_text}\n\n"
            f"【你打算做的事】{intent or '（没有明确计划）'}\n"
            f"【你已经做的事与结果】\n{actions}\n\n"
            f"【现在需要你回答】\n{hint}"
            "1) 你此刻要对用户说的话（就是你要直接发出去的内容，不要再加括号说明、不要写旁白）。"
            "如果这次不该打扰，message 给 null。\n"
            "2) 把「最近流水 + 你即将发送的这条消息」放在一起看，判断下一次应该什么时候再醒来。\n\n"
            "只输出 JSON：\n"
            '{"thought": "你的内心想法（第一人称，1-3 句）", '
            '"message": "要说的话或 null", "next_wake_at": "下次唤醒时间"}\n'
            "next_wake_at 用带时区的 ISO8601 或 +90m 这种相对写法。"
        )
