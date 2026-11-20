"""单上下文决策流水线 + 三类作业收尾。

一次周期的顺序：
1. 准备：人格（一次）、上下文块（流水一次 / 爱好记忆一次）、工具目录；
2. 一次模型调用拿到：是否行动、要调的工具、草稿消息、记忆文本、下次时间；
3. 有工具则执行，然后在**同一份消息列表**上再问一次定稿（`single_request` 时跳过）；
4. 发送 → 落库（平台历史 / LivingMemory）→ 写记忆（模型给的纯文本）；
5. 收尾（最后一步才排作业）：
   - 对话：本轮说了话（含主 Agent 刚回复）→ **必须**排下一次；
   - 爱好：写记忆（含下轮计划）后再排下一个；
   - 临时：不排。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from . import context as ctx
from . import delivery, echo, flow as flow_mod, jobs as jobs_mod, llm, memory, memorydb
from . import timeutil, toolbox
from .persona import PersonaResolver

LOGGER_NAME = "astrbot"

DEFAULT_TRIGGERS = {
    jobs_mod.KIND_DIALOGUE: "dialogue_cron",
    jobs_mod.KIND_HOBBY: "hobby_cron",
    jobs_mod.KIND_TEMP: "temp_cron",
}

FOLLOWUP_ASK = """【工具执行结果】
{results}

【现在只需要定稿】只输出 JSON：{{"message": "要说的话或 null", "memory": "本轮总结（自然语言）"}}"""


def _as_bool(value: Any, default: bool = False) -> bool:
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


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip() if not isinstance(value, str) else value.strip()


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
    # 基础
    # ------------------------------------------------------------------
    def lock_for(self, umo: str) -> asyncio.Lock:
        lock = self._locks.get(umo)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[umo] = lock
        return lock

    def tz(self):
        return self.scheduler.tz if self.scheduler is not None else timeutil.resolve_tz(
            self.settings.timezone_name
        )

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------
    async def run(
        self,
        umo: str,
        kind: str = jobs_mod.KIND_DIALOGUE,
        *,
        trigger: str | None = None,
        temp_id: str | None = None,
        bypass_limit: bool = False,
        spoke_before: bool = False,
        record_only: bool = False,
        next_at_hint: Any = None,
        main_reply_text: str = "",
    ) -> dict[str, Any]:
        """跑一次周期，返回 trace。

        Args:
            record_only: 记录模式——主 Agent 的回复就是本次对话 job 的执行体，
                这一轮**永不发消息**，只更新作业/记忆。
            next_at_hint: 主 Agent 回复里 [[pmind_next:...]] 标记解析出的时间。
        """
        trigger = trigger or DEFAULT_TRIGGERS.get(kind, kind)
        lock = self.lock_for(umo)
        if lock.locked():
            self.log.info(f"[proactive_mind] {umo} 正在决策中，跳过本次 {kind}/{trigger}")
            return {"skipped": "busy", "umo": umo, "kind": kind, "trigger": trigger}

        async with lock:
            trace = self.store.new_trace(umo, trigger, kind)
            try:
                await self._run_locked(
                    umo,
                    kind,
                    temp_id,
                    trigger,
                    trace,
                    bypass_limit=bypass_limit,
                    spoke_before=spoke_before,
                    record_only=record_only,
                    next_at_hint=next_at_hint,
                    main_reply_text=main_reply_text,
                )
            except asyncio.CancelledError:
                trace["errors"].append("cancelled")
                raise
            except Exception as exc:  # noqa: BLE001
                trace["errors"].append(f"fatal: {exc}")
                self.log.error(
                    f"[proactive_mind] 决策失败 {umo} ({kind}/{trigger}): {exc}", exc_info=True
                )
                # 崩溃/异常也要留下下一次（同样按默认间隔，且不推进连续计数）
                retry_now = timeutil.now(self.tz())
                if kind == jobs_mod.KIND_HOBBY:
                    self._schedule_hobby(umo, None, trace, now=retry_now, no_count=True)
                elif kind == jobs_mod.KIND_DIALOGUE:
                    self._schedule_dialogue(umo, None, trace, now=retry_now, no_count=True)
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
                except Exception as exc:  # pragma: no cover
                    self.log.error(f"[proactive_mind] 状态落盘失败: {exc}")
            return trace

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    async def _run_locked(
        self,
        umo: str,
        kind: str,
        temp_id: str | None,
        trigger: str,
        trace: dict[str, Any],
        *,
        bypass_limit: bool,
        spoke_before: bool,
        record_only: bool = False,
        next_at_hint: Any = None,
        main_reply_text: str = "",
    ) -> None:
        settings = self.settings
        log = self.log
        tz = self.tz()
        now = timeutil.now(tz)
        session = self.store.session(umo)
        jobs = jobs_mod.ensure_jobs(session)

        if not settings.enabled and not bypass_limit:
            trace["reason"] = "插件已关闭（basic.enabled=false）"
            return

        if kind == jobs_mod.KIND_DIALOGUE:
            session["last_wake_at"] = timeutil.iso(now)

        # —— A 模式（记录模式）——
        # 主 Agent 的回复就是这次对话 job 的执行体：这一轮**不调用模型、不读流水、不发消息**，
        # 只用回复里的 [[pmind_next:…]] 标记（或默认间隔）把对话节奏排下去。
        if record_only and kind == jobs_mod.KIND_DIALOGUE:
            trace["record_only"] = True
            session["last_gate"] = {"act": True}
            # 主 Agent 确实说了话：轨迹要如实记成"行动 / 已发送"，
            # 只是这句话不是插件发的（message_source=main_agent）。
            trace["act"] = True
            trace["message_source"] = "main_agent"
            reply = (main_reply_text or "").strip()
            if reply:
                trace["message"] = reply[:600]
                trace["message_sent"] = True
                session["last_sent_message"] = reply[:600]
                session["last_sent_at"] = timeutil.iso(now)
                jobs["dialogue"]["last_spoke_at"] = timeutil.iso(now)
            hint_raw = _as_str(next_at_hint)
            hint, marker_topic = timeutil.split_marker(hint_raw)
            if hint.lower() in {"none", "null", "off", "false", "no", "不", "别"}:
                # none 已废弃：当作"没给具体时间"→ 随机兜底
                hint = ""
            if marker_topic:
                jobs["dialogue"]["topic"] = marker_topic[:60]
                jobs["dialogue"]["topic_at"] = timeutil.iso(now)
                trace["topic"] = marker_topic[:60]
            tool_next = self._dialogue_set_by_tool(session, jobs)
            if not hint and tool_next:
                # 她这一轮自己调了 pmind_schedule(kind=dialogue) —— 用她定的时间
                trace["next_from"] = "tool"
                trace["reason"] = "记录模式：对话时间由她这轮的工具调用给出"
                self._schedule_dialogue(
                    umo, tool_next, trace, now=now, bypass_limit=bypass_limit, keep_if_later=True
                )
                return
            if hint:
                trace["reason"] = "记录模式：主 Agent 的回复即本轮执行体（不额外调用模型、不补话）"
                trace["next_from"] = "reply_marker"
                self._schedule_dialogue(
                    umo, hint, trace, now=now, bypass_limit=bypass_limit
                )
                return
            # 她没说时间：按配置区间随机兜底（不追问、不重试）
            trace["reason"] = "记录模式：她没说下次时间，按随机区间兜底"
            fallback = self._fallback_time(now, trace)
            self._schedule_dialogue(umo, fallback, trace, now=now, bypass_limit=bypass_limit)
            return

        # 到点即"消费"掉这个时间点：否则静默分支不重排时，state 里会残留过期时间，
        # 面板就会一直显示"还有 0 秒"。
        if trigger == "dialogue_cron":
            jobs["dialogue"]["next_at"] = None
            session["next_wake_at"] = None
        elif trigger == "hobby_cron":
            jobs["hobby"]["next_at"] = None

        # 爱好轮：真正到期执行时才把 runs +1（手动/面板触发不算）
        if kind == jobs_mod.KIND_HOBBY and trigger == "hobby_cron":
            hobby_job = jobs.get("hobby") or {}
            hobby_job["runs"] = int(hobby_job.get("runs") or 0) + 1
            trace["hobby_round"] = hobby_job["runs"]

        # 临时作业：取出备注（触发即消费）
        temp_note = ""
        if kind == jobs_mod.KIND_TEMP:
            item = jobs_mod.find_temp(jobs, temp_id or "") if temp_id else None
            if item is None and jobs.get("temp"):
                item = jobs_mod.sort_temp(jobs)[0]
            if item:
                temp_note = str(item.get("note") or "")
                trace["temp_id"] = item.get("id")
                trace["temp_note"] = temp_note
                jobs["temp"] = [x for x in jobs["temp"] if x is not item]
                if item.get("id"):
                    self.scheduler.cancel(umo, kind=jobs_mod.KIND_TEMP, temp_id=str(item["id"]))

        persona_prompt, persona_id = await self.persona.resolve(umo)
        trace["persona_id"] = persona_id

        # —— 上下文：流水/记忆各一次 ——
        flow_items: list[flow_mod.FlowItem] = []
        flow_source = ""
        if kind in {jobs_mod.KIND_DIALOGUE, jobs_mod.KIND_TEMP, jobs_mod.KIND_HOBBY}:
            limit = settings.hobby_flow_count if kind == jobs_mod.KIND_HOBBY else settings.flow_message_count
            flow_items, flow_source, warnings = await flow_mod.read_flow(
                self.context, settings, umo, log
            )
            trace["warnings"].extend(warnings)
            flow_items = flow_mod.drop_old(flow_items, settings.flow_max_age_hours)
            flow_items = flow_mod.merge_proactive(flow_items, session, settings)
            flow_items = flow_items[-limit:] if limit > 0 else []
        trace["flow_source"] = flow_source
        trace["flow_count"] = len(flow_items)
        flow_text = flow_mod.format_flow(flow_items, settings.flow_max_chars)

        hobby_items: list[memorydb.HobbyMemory] = []
        if kind == jobs_mod.KIND_HOBBY:
            hobby_items, mem_stats = await memorydb.read_hobby_memories(settings, log=log)
            trace["memory_stats"] = mem_stats
            trace["memory_count"] = len(hobby_items)

        whitelist = settings.hobby_tool_whitelist if kind == jobs_mod.KIND_HOBBY else None
        catalog = toolbox.build_catalog(self.context, settings, log, allowed=whitelist)
        catalog_text = toolbox.describe_catalog(catalog)

        state_block = ctx.build_state_block(
            settings=settings,
            session=session,
            kind=kind,
            trigger=trigger,
            now=now,
            tz_name=str(tz),
            flow_count=len(flow_items),
            memory_count=len(hobby_items),
            temp_note=temp_note,
        )

        if kind == jobs_mod.KIND_HOBBY:
            user_prompt = ctx.build_hobby_user(
                state_block,
                memorydb.render_memories(hobby_items),
                flow_text,
                catalog_text,
            )
        elif kind == jobs_mod.KIND_TEMP:
            user_prompt = ctx.build_temp_user(state_block, flow_text, catalog_text, temp_note)
        else:
            user_prompt = ctx.build_dialogue_user(state_block, flow_text, catalog_text)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": ctx.build_system(persona_prompt, settings)},
            {"role": "user", "content": user_prompt},
        ]
        trace["context_stats"] = {
            "requests": 1,
            "persona_units": 1,
            "flow_units": 1 if flow_items else 0,
            "memory_units": 1 if hobby_items else 0,
        }

        # —— 一次调用 ——
        data, raw, errors = await llm.chat_json_messages(
            self.context, settings, umo, messages=messages, log=log
        )
        trace["errors"].extend(errors)
        if data is None:
            # 断网 / 模型超时：也必须留下下一次，否则到点被消费后就变成"未安排"。
            # 这类失败不算她的"沉默"，所以不推进连续计数（no_count=True），
            # 按对话/爱好的默认间隔稍后重试，网络恢复自然接上。
            trace["reason"] = "模型调用失败（按默认间隔重试）"
            trace["retry_due_to_error"] = True
            if kind == jobs_mod.KIND_HOBBY:
                self._schedule_hobby(umo, None, trace, now=now, no_count=True)
            elif kind == jobs_mod.KIND_DIALOGUE:
                self._schedule_dialogue(umo, None, trace, now=now, no_count=True)
            return

        act = _as_bool(data.get("act"), False)
        reason = _as_str(data.get("reason")) or "（模型未给理由）"
        thought = _as_str(data.get("thought"))
        intent = _as_str(data.get("intent"))
        message = delivery.trim_message(_as_str(data.get("message")), settings.max_message_chars)
        memory_text = _as_str(data.get("memory"))
        dialogue_next = data.get("dialogue_next_at")
        hobby_next = data.get("hobby_next_at")

        trace["act"] = act
        trace["reason"] = reason
        trace["thought"] = thought
        session["last_gate"] = {"act": act}

        steps: list[dict[str, Any]] = []
        raw_steps = data.get("steps")
        if act and isinstance(raw_steps, list):
            for step in raw_steps[: settings.max_tool_calls]:
                if isinstance(step, dict) and _as_str(step.get("tool")):
                    steps.append(
                        {
                            "tool": _as_str(step.get("tool")),
                            "args": step.get("args") if isinstance(step.get("args"), dict) else {},
                        }
                    )

        records: list[dict[str, Any]] = []
        if steps:
            event = toolbox.make_event(self.context, umo)
            records = await toolbox.execute_steps(
                self.context, settings, steps=steps, log=log, event=event
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

        # —— 定稿（同一上下文，可选第二次请求）——
        if records and not settings.single_request:
            follow_messages = messages + [
                {
                    "role": "user",
                    "content": FOLLOWUP_ASK.format(results=toolbox.summarize_results(records)),
                }
            ]
            final, _raw, follow_errors = await llm.chat_json_messages(
                self.context, settings, umo, messages=follow_messages, log=log
            )
            trace["errors"].extend(follow_errors)
            trace["context_stats"]["requests"] = 2
            if isinstance(final, dict):
                message = delivery.trim_message(
                    _as_str(final.get("message")), settings.max_message_chars
                )
                if _as_str(final.get("memory")):
                    memory_text = _as_str(final.get("memory"))

        # —— 发送 ——
        # 记录模式（A 模式）：主 Agent 的回复已经发出去了，这一轮只记账，绝不补话。
        send_result: dict[str, Any] = {}
        if record_only:
            trace["record_only"] = True
            if message:
                trace["suppressed_message"] = message[:300]
                trace["warnings"].append("record_only：已丢弃本轮生成的 message（主 Agent 的回复即本轮回复）")
            message = ""
        if message:
            send_result = await delivery.send_text(
                self.context, umo=umo, text=message, log=log, settings=settings
            )
            trace["message"] = message
            trace["message_sent"] = bool(send_result.get("ok"))
            if send_result.get("segments"):
                trace["message_segments"] = int(send_result["segments"])
            if not send_result.get("ok"):
                trace["errors"].append(f"send_failed: {send_result.get('error')}")
                message = ""
            else:
                session["last_sent_message"] = message
                session["last_sent_at"] = timeutil.iso(now)
                if kind == jobs_mod.KIND_DIALOGUE:
                    jobs["dialogue"]["last_spoke_at"] = timeutil.iso(now)

        # —— 落库 ——
        if message:
            # 落库用"实际发出去的文本"（替换/过滤后），保证历史与真发的一致
            echo_text = send_result.get("text") or message
            echo_result = await echo.echo_all(
                self.context, settings, umo=umo, text=echo_text, session=session, log=log
            )
            trace["echo"] = echo_result
            session["last_echo"] = {
                key: bool(value.get("ok"))
                for key, value in echo_result.items()
                if isinstance(value, dict)
            }
            for key, value in (echo_result or {}).items():
                if isinstance(value, dict) and not value.get("ok"):
                    trace["warnings"].append(f"落库失败({key}): {value.get('error')}")

        # —— 记忆 ——
        # 只有爱好周期**必须**写（本轮总结 + 下轮计划）；
        # 对话/临时周期不自动写：由她自己决定要不要调 memorize_long_term_memory 工具。
        memory_result: dict[str, Any] = {}
        hobby_summary = ""
        if kind == jobs_mod.KIND_HOBBY:
            hobby_summary = memory_text or memory.fallback_text(
                kind=kind, thought=thought, intent=intent, steps=trace["steps"], message=message or None
            )
            memory_result = await memory.write_memory(
                self.context,
                settings,
                umo=umo,
                content=hobby_summary,
                topics=[
                    "proactive_mind",
                    f"trigger:{kind}",
                    settings.hobby_memory_tag,
                ],
                reason=reason,
                importance=settings.hobby_importance,
                log=log,
                event=toolbox.make_event(self.context, umo),
                data_dir=self.data_dir,
            )
        else:
            used_tool = any(
                record.get("tool") == settings.memory_tool_name and record.get("ok")
                for record in records
            )
            if used_tool:
                memory_result = {"ok": True, "by": "model_tool"}
            else:
                memory_result = {"skipped": True, "reason": "model_decides"}
        trace["memory"] = memory_result
        if hobby_summary:
            trace["memory_text"] = hobby_summary[:400]

        # —— 收尾：最后一步才排作业 ——
        # 对话节奏：**触发就必须更新**（不再以"这轮说没说话"为条件）。
        # 时间优先级：主 Agent 回复里的标记 > 模型给的 dialogue_next_at > 默认间隔。
        if kind == jobs_mod.KIND_HOBBY:
            self._schedule_hobby(
                umo,
                hobby_next,
                trace,
                now=now,
                intent=hobby_summary,
                memory_id=_extract_memory_id(memory_result),
                bypass_limit=bypass_limit,
            )
        elif kind == jobs_mod.KIND_DIALOGUE:
            hint_raw = _as_str(next_at_hint)
            hint, marker_topic = timeutil.split_marker(hint_raw)
            hint = hint or None
            if marker_topic:
                jobs["dialogue"]["topic"] = marker_topic[:60]
                jobs["dialogue"]["topic_at"] = timeutil.iso(now)
            raw_next = hint or dialogue_next
            if hint:
                trace["next_from"] = "reply_marker"
            elif dialogue_next:
                trace["next_from"] = "model"
            else:
                # 她说了话却没给时间：随机兜底
                raw_next = self._fallback_time(now, trace)
            self._schedule_dialogue(umo, raw_next, trace, now=now, bypass_limit=bypass_limit)
        else:
            trace["reason_short"] = "临时提醒不重建"
            # 但"只要她说了话就更新对话 job"：临时周期里发言同样要排下一次
            if message:
                trace["next_from"] = "temp_spoke"
                raw_next = dialogue_next
                if not raw_next:
                    raw_next = self._fallback_time(now, trace)
                self._schedule_dialogue(
                    umo, raw_next, trace, now=now, bypass_limit=bypass_limit
                )

    # ------------------------------------------------------------------
    # 排程
    # ------------------------------------------------------------------
    @staticmethod
    def _dialogue_set_by_tool(session: dict[str, Any], jobs: dict[str, Any]) -> Any:
        """主 Agent 这一轮是否自己调工具设过对话节奏？

        判据：``jobs.dialogue.updated_at`` 晚于（或等于）``last_user_message_at``，
        且当前有 next_at —— 说明是这轮对话里她通过 pmind_schedule 定的。
        """
        data = jobs.get("dialogue") or {}
        if not data.get("next_at") or data.get("cancelled"):
            return None
        updated = timeutil.from_iso(data.get("updated_at"))
        last_user = timeutil.from_iso(session.get("last_user_message_at"))
        if updated is None or last_user is None:
            return None
        if updated.timestamp() + 1.0 < last_user.timestamp():
            return None
        return data.get("next_at")

    def _bump_counter(self, session: dict[str, Any], *, replied: bool, sent: bool) -> int:
        settings = self.settings
        try:
            count = int(session.get("silent_wake_count") or 0)
        except (TypeError, ValueError):
            count = 0
        if replied:
            count = 1
        elif settings.count_only_when_message_sent and not sent:
            count = max(count, 1)
        else:
            count += 1
        session["silent_wake_count"] = count
        return count

    def _cap_blocked(self, session: dict[str, Any], *, replied: bool, count: int, bypass: bool) -> bool:
        cap = self.settings.max_consecutive_wakes
        if bypass or cap == -1:
            return False
        return (not replied) and count > cap

    def _schedule_dialogue(
        self,
        umo: str,
        raw_when: Any,
        trace: dict[str, Any],
        *,
        now: datetime,
        bypass_limit: bool = False,
        keep_if_later: bool = False,
        no_count: bool = False,
    ) -> datetime | None:
        settings = self.settings
        session = self.store.session(umo)
        jobs = jobs_mod.ensure_jobs(session)
        if jobs["dialogue"].get("cancelled") and not bypass_limit:
            trace["schedule_blocked"] = "dialogue_cancelled"
            return None
        parsed = timeutil.parse_wake_time(raw_when, reference=now, tz=self.tz())
        if keep_if_later and parsed is not None and parsed > now:
            # 时间是她自己用工具定的：原样采用，不再被 min/max 改写
            target = parsed
        elif parsed is None:
            # 没有可用时间：直接在随机区间里取（原"默认间隔"已并入随机区间）
            import random

            low = int(settings.wake_random_min_minutes)
            high = int(settings.wake_random_max_minutes)
            if high <= low:
                high = low + 1
            minutes = random.randint(low, high)
            target = timeutil.clamp_wake(
                timeutil.plus_minutes(now, minutes),
                reference=now,
                min_minutes=settings.min_interval_minutes,
                max_minutes=settings.max_interval_minutes,
                default_minutes=minutes,
            )
            trace["fallback_minutes"] = int((target - now).total_seconds() // 60)
            trace["fallback_range"] = [low, high]
            if raw_when:
                # 她给了时间但解析不出：如实标明"标记存在但解析失败"，别假装是 reply_marker
                trace["next_from"] = "random_fallback"
                trace["marker_unparsed"] = str(raw_when)[:60]
            else:
                trace.setdefault("next_from", "random_fallback")
        else:
            target = timeutil.clamp_wake(
                parsed,
                reference=now,
                min_minutes=settings.min_interval_minutes,
                max_minutes=settings.max_interval_minutes,
                default_minutes=settings.min_interval_minutes,
            )
        trace["schedule_fallback"] = parsed is None
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
        if no_count:
            # 失败重试：不动计数、不触发上限（否则断网几次就永久停摆）
            session["user_replied_since_last_wake"] = False
            session["last_error"] = None
        else:
            count = self._bump_counter(session, replied=replied, sent=True)
            if self._cap_blocked(session, replied=replied, count=count, bypass=bypass_limit):
                self.scheduler.cancel(umo, kind=jobs_mod.KIND_DIALOGUE)
                jobs["dialogue"]["next_at"] = None
                session["next_wake_at"] = None
                session["last_error"] = "consecutive_limit_reached"
                trace["schedule_blocked"] = "consecutive_limit_reached"
                return None
        try:
            actual = self.scheduler.set_wake(umo, target, kind=jobs_mod.KIND_DIALOGUE)
        except Exception as exc:  # pragma: no cover
            trace["errors"].append(f"schedule_failed: {exc}")
            return None
        jobs["dialogue"]["next_at"] = timeutil.iso(actual)
        jobs["dialogue"]["updated_at"] = timeutil.iso(now)
        # 真正排定了时间：清掉"保底"标记，否则面板会一直挂着"保底时间，等她回复后覆盖"
        jobs["dialogue"]["provisional"] = False
        # 已经排定了时间：清掉"等待回复"标记，宽限期任务就不会再补垫
        session["wake_pending_at"] = None
        session["next_wake_at"] = timeutil.iso(actual)
        session["user_replied_since_last_wake"] = False
        session["last_error"] = None
        trace["next_wake_at"] = session["next_wake_at"]
        return actual

    def _schedule_hobby(
        self,
        umo: str,
        raw_when: Any,
        trace: dict[str, Any],
        *,
        now: datetime,
        intent: str | None = None,
        memory_id: Any = None,
        bypass_limit: bool = False,
        no_count: bool = False,
    ) -> datetime | None:
        settings = self.settings
        session = self.store.session(umo)
        jobs = jobs_mod.ensure_jobs(session)
        if jobs["hobby"].get("cancelled") and not bypass_limit:
            trace["schedule_blocked"] = "hobby_cancelled"
            return None
        parsed = timeutil.parse_wake_time(raw_when, reference=now, tz=self.tz())
        if parsed is None:
            # 没给时间：直接在 hobby.min ~ hobby.max 之间随机（原"默认间隔"已并入区间）
            import random

            low = int(settings.hobby_min_interval_minutes)
            high = int(settings.hobby_max_interval_minutes)
            if high <= low:
                high = low + 1
            minutes = random.randint(low, high)
            target = timeutil.clamp_wake(
                timeutil.plus_minutes(now, minutes),
                reference=now,
                min_minutes=low,
                max_minutes=high,
                default_minutes=minutes,
            )
            trace["fallback_minutes"] = int((target - now).total_seconds() // 60)
            trace["fallback_range"] = [low, high]
        else:
            target = timeutil.clamp_wake(
                parsed,
                reference=now,
                min_minutes=settings.hobby_min_interval_minutes,
                max_minutes=settings.hobby_max_interval_minutes,
                default_minutes=settings.hobby_min_interval_minutes,
            )
        trace["schedule_fallback"] = parsed is None

        replied = bool(session.get("user_replied_since_last_wake", True))
        if no_count:
            session["user_replied_since_last_wake"] = False
            session["last_error"] = None
        else:
            count = self._bump_counter(session, replied=replied, sent=False)
            if self._cap_blocked(session, replied=replied, count=count, bypass=bypass_limit):
                self.scheduler.cancel(umo, kind=jobs_mod.KIND_HOBBY)
                jobs["hobby"]["next_at"] = None
                session["last_error"] = "consecutive_limit_reached"
                trace["schedule_blocked"] = "consecutive_limit_reached"
                return None
        try:
            actual = self.scheduler.set_wake(umo, target, kind=jobs_mod.KIND_HOBBY)
        except Exception as exc:  # pragma: no cover
            trace["errors"].append(f"schedule_failed: {exc}")
            return None
        jobs["hobby"]["next_at"] = timeutil.iso(actual)
        jobs["hobby"]["intent"] = (intent or "")[:600] or jobs["hobby"].get("intent")
        jobs["hobby"]["intent_at"] = timeutil.iso(now)
        jobs["hobby"]["memory_id"] = memory_id or jobs["hobby"].get("memory_id")
        # 注意：这里是"排程"，不动 runs（runs 只在真正到期执行时 +1）
        jobs["hobby"]["scheduled"] = int(jobs["hobby"].get("scheduled") or 0) + 1
        session["user_replied_since_last_wake"] = False
        session["last_error"] = None
        trace["next_wake_at"] = jobs["hobby"]["next_at"]
        return actual

    def add_temp(
        self,
        umo: str,
        when: datetime,
        note: str,
        *,
        created_by: str = "plugin",
    ) -> dict[str, Any] | None:
        """新增一条临时作业（受 temp.max_jobs 限制）。"""
        settings = self.settings
        session = self.store.session(umo)
        jobs = jobs_mod.ensure_jobs(session)
        if len(jobs.get("temp") or []) >= settings.temp_max_jobs:
            self.log.info(f"[proactive_mind] 临时作业已达上限 {settings.temp_max_jobs}，忽略")
            return None
        now = timeutil.now(self.tz())
        target = timeutil.explicit_target(
            when,
            now=now,
            max_minutes=settings.max_interval_minutes,
            delay_minutes=(
                None if session.get("delay_override_minutes") is None else int(session["delay_override_minutes"])
            ),
        )
        if target is None:
            return None
        temp_id = jobs_mod.new_temp_id()
        try:
            actual = self.scheduler.set_wake(umo, target, kind=jobs_mod.KIND_TEMP, temp_id=temp_id)
        except Exception as exc:  # pragma: no cover
            self.log.error(f"[proactive_mind] 创建临时作业失败: {exc}")
            return None
        item = {
            "id": temp_id,
            "next_at": timeutil.iso(actual),
            "note": note[:600],
            "created_by": created_by,
            "created_at": timeutil.iso(now),
        }
        jobs["temp"].append(item)
        return item


    # ------------------------------------------------------------------
    # 收不到下次时间时的兜底：在配置区间内随机挑
    # ------------------------------------------------------------------
    def _fallback_time(self, now: datetime, trace: dict[str, Any]) -> Any:
        """她没给时间时，按 wake.random_min/max_minutes 随机取一个。

        L3/L4 合并后的唯一兜底：不追问、不重试，也不再用固定默认间隔，
        而是在用户设定的区间里随机落点。
        """
        import random

        settings = self.settings
        low = int(settings.wake_random_min_minutes)
        high = int(settings.wake_random_max_minutes)
        if high <= low:
            high = low + 1
        minutes = random.uniform(low, high)
        target = timeutil.plus_minutes(now, int(round(minutes)))
        trace["next_from"] = "random_fallback"
        trace["fallback_minutes"] = int(round(minutes))
        trace["fallback_range"] = [low, high]
        trace["warnings"].append(
            f"没收到下次时间，按 {low}~{high} 分钟随机取了 {int(round(minutes))} 分钟"
        )
        return timeutil.iso(target)

    async def provisional_schedule(
        self, umo: str, kind: str = jobs_mod.KIND_DIALOGUE
    ) -> Any:
        """把 job 交给主 Agent 时先垫一个保底时间，避免"回复没发出来 → 未安排"。

        保底时间从对话区间随机取；**只在她没有未来时间时才垫**。
        与 record_main_agent_reply 共用一把 per-umo 锁，避免"她刚给的时间"
        被随后写入的保底覆盖（那会造成提前唤醒）。
        """
        async with self.lock_for(umo):
            return await self._provisional_schedule_locked(umo, kind)

    async def _provisional_schedule_locked(self, umo: str, kind: str) -> Any:
        settings = self.settings
        if not settings.provisional_enabled:
            return None
        tz = self.tz()
        now = timeutil.now(tz)
        session = self.store.session(umo)
        jobs = jobs_mod.ensure_jobs(session)
        if kind != jobs_mod.KIND_DIALOGUE:
            return None
        if jobs["dialogue"].get("cancelled"):
            return None
        # 规则：**只有当前没有未来时间时才垫**。
        # 已经有未来的 next_at（她刚给过时间 / 已排好）就原样保留，绝不提前。
        existing = timeutil.from_iso(jobs["dialogue"].get("next_at"))
        if existing is not None and existing > now + timedelta(seconds=60):
            return None
        import random

        low = int(settings.wake_random_min_minutes)
        high = int(settings.wake_random_max_minutes)
        if high <= low:
            high = low + 1
        minutes = int(round(random.uniform(low, high)))
        target = timeutil.plus_minutes(now, minutes)
        # 写入前再确认一次：期间可能刚被她/别的分支改过
        latest = timeutil.from_iso(self.store.session(umo)["jobs"]["dialogue"].get("next_at"))
        if latest is not None and latest > timeutil.now(tz) + timedelta(seconds=60):
            return None
        try:
            actual = self.scheduler.set_wake(umo, target, kind=jobs_mod.KIND_DIALOGUE)
        except Exception as exc:  # pragma: no cover
            self.log.error(f"[proactive_mind] 垫保底时间失败 {umo}: {exc}")
            return None
        session = self.store.session(umo)
        jobs = jobs_mod.ensure_jobs(session)
        jobs["dialogue"]["next_at"] = timeutil.iso(actual)
        jobs["dialogue"]["provisional"] = True
        session["next_wake_at"] = timeutil.iso(actual)
        self.log.info(
            f"[proactive_mind] 已垫对话保底时间 {timeutil.human(actual)}"
            f"（区间 {low}~{high} 分钟取 {minutes}；等她回复后会覆盖）"
        )
        return actual

    async def record_main_agent_reply(
        self,
        umo: str,
        *,
        kind: str = jobs_mod.KIND_DIALOGUE,
        text: str = "",
        marker: Any = None,
        temp_id: str | None = None,
        note: str = "",
        bypass_limit: bool = False,
        marker_from_think: bool = False,
    ) -> dict[str, Any]:
        """对话/临时 job 交给主 Agent 后，这里只记账：更新 job、清话题、写轨迹。

        **不调用模型、不发消息**（消息已经由主 Agent 走完整管线发出）。
        与 provisional_schedule 共用 per-umo 锁，避免"保底"和"她给的时间"互相覆盖。
        """
        async with self.lock_for(umo):
            return await self._record_main_agent_reply_locked(
                umo,
                kind=kind,
                text=text,
                marker=marker,
                temp_id=temp_id,
                note=note,
                bypass_limit=bypass_limit,
                marker_from_think=marker_from_think,
            )

    async def _record_main_agent_reply_locked(
        self,
        umo: str,
        *,
        kind: str,
        text: str,
        marker: Any,
        temp_id: str | None,
        note: str,
        bypass_limit: bool,
        marker_from_think: bool = False,
    ) -> dict[str, Any]:
        tz = self.tz()
        now = timeutil.now(tz)
        trace = self.store.new_trace(umo, "main_agent_wake", kind)
        session = self.store.session(umo)
        jobs = jobs_mod.ensure_jobs(session)

        trace["record_only"] = True
        trace["message_source"] = "main_agent"
        trace["act"] = True
        reply = (text or "").strip()
        if reply:
            trace["message"] = reply[:600]
            trace["message_sent"] = True
            session["last_sent_message"] = reply[:600]
            session["last_sent_at"] = timeutil.iso(now)
            jobs["dialogue"]["last_spoke_at"] = timeutil.iso(now)

        hint, marker_topic = timeutil.split_marker(_as_str(marker))
        if hint.lower() in {"none", "null", "off", "false", "no", "不", "别"}:
            hint = ""

        tool_next = self._dialogue_set_by_tool(session, jobs)
        if hint:
            trace["next_from"] = "reply_marker_think" if marker_from_think else "reply_marker"
            trace["marker_from_think"] = bool(marker_from_think)
            raw_next: Any = hint
        elif tool_next:
            trace["next_from"] = "tool"
            raw_next = tool_next
        else:
            raw_next = self._fallback_time(now, trace)

        if kind == jobs_mod.KIND_TEMP and temp_id:
            jobs["temp"] = [item for item in jobs["temp"] if item.get("id") != temp_id]
            self.scheduler.cancel(umo, kind=jobs_mod.KIND_TEMP, temp_id=temp_id)
            trace["reason_short"] = "临时提醒已执行（由主 Agent）"

        # 只要她说了话，对话 job 就必须更新
        if reply or kind == jobs_mod.KIND_DIALOGUE:
            self._schedule_dialogue(umo, raw_next, trace, now=now, bypass_limit=bypass_limit)
        # 话题：这一轮给了新话题就换新，否则用完即清（一次性）
        if marker_topic:
            jobs["dialogue"]["topic"] = marker_topic[:60]
            jobs["dialogue"]["topic_at"] = timeutil.iso(now)
            trace["topic"] = marker_topic[:60]
        else:
            jobs["dialogue"]["topic"] = None
            jobs["dialogue"]["topic_at"] = None

        trace["reason"] = note or "主 Agent 到点发言后记账"
        trace["finished_at"] = timeutil.now_iso(tz)
        self.store.add_trace(trace)
        self.store.set(umo, last_decision_at=trace["finished_at"], last_trace_id=trace["trace_id"])
        try:
            await self.store.save()
        except Exception as exc:  # pragma: no cover
            self.log.error(f"[proactive_mind] 记账落盘失败: {exc}")
        self.log.info(
            f"[proactive_mind] 主 Agent 唤醒记账完成 {umo}/{kind}"
            f"（next_from={trace.get('next_from')}，下次 {trace.get('next_wake_at')}）"
        )
        return trace


def _extract_memory_id(result: dict[str, Any]) -> Any:
    """从记忆工具返回里抠出 id（拿不到就算了）。"""
    body = result.get("result")
    if not isinstance(body, str):
        return None
    text = body.strip()
    if not text.startswith("{"):
        return None
    try:
        import json

        parsed = json.loads(text)
    except Exception:
        return None
    if isinstance(parsed, dict):
        return parsed.get("id")
    return None
