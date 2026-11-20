"""插件自定义页面的 Web API（v2：三类作业）。

返回体统一为 ``{"status": "ok", "data": ...}`` / ``{"status": "error", "message": ...}``。
"""

from __future__ import annotations

import asyncio
import difflib
import logging
from datetime import datetime
from typing import Any

from . import flow as flow_mod
from . import jobs as jobs_mod
from . import llm, timeutil, toolbox
from .settings import DEFAULTS

LOGGER_NAME = "astrbot"
PLUGIN_NAME_FALLBACK = "astrbot_plugin_proactive_mind"

CONFIG_PATCH_WHITELIST = {
    "basic.enabled",
    "basic.trigger_on_user_message",
    "basic.user_message_mode",
    "outbound.segment_enabled",
    "outbound.segment_pattern",
    "outbound.segment_max",
    "outbound.segment_interval",
    "outbound.cleanup_enabled",
    "outbound.cleanup_pattern",
    "outbound.replace_enabled",
    "outbound.replace_rules",
    "flow.flow_source",
    "flow.flow_message_count",
    "flow.follow_livingmemory_window",
    "flow.flow_max_age_hours",
    "wake.wake_delay_minutes",
    "wake.min_interval_minutes",
    "wake.max_interval_minutes",
    "hobby.memory_count",
    "hobby.flow_count",
    "hobby.min_interval_minutes",
    "hobby.max_interval_minutes",
    "temp.max_jobs",
    "limit.max_consecutive_wakes",
    "limit.count_only_when_message_sent",
    "mind.dialogue_execute",
    "mind.temp_execute",
    "mind.output_hint",
    "mind.wake_hint",
    "mind.single_request",
    "mind.max_tool_calls",
    "mind.catalog_max_entries",
    "mind.catalog_char_budget",
    "mind.tool_deny_pattern",
    "mind.tool_whitelist",
    "mind.tool_blacklist",
    "memory.mode",
    "memory.importance",
    "echo.persist_proactive_to_platform_history",
    "echo.persist_proactive_to_livingmemory",
    "echo.dedupe_echo_in_flow",
    "panel.stale_hours",
}


def ok(data: Any = None) -> dict[str, Any]:
    return {"status": "ok", "data": data if data is not None else {}}


def fail(message: str, code: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": "error", "message": message}
    if code:
        payload["code"] = code
    return payload


async def _read_payload() -> dict[str, Any]:
    payload: dict[str, Any] = {}
    try:
        from astrbot.api.web import request

        query = getattr(request, "query", None)
        if query is not None:
            try:
                payload.update({str(k): v for k, v in dict(query).items()})
            except Exception:
                pass
        if str(getattr(request, "method", "")).upper() in {"POST", "PUT", "PATCH"}:
            body = await request.json(default={})
            if isinstance(body, dict):
                payload.update(body)
    except Exception as exc:  # pragma: no cover
        logging.getLogger(LOGGER_NAME).debug(f"[proactive_mind] 读取请求体失败: {exc}")
    return payload


_BOILERPLATE_REASONS = (
    "记录模式",
    "主 Agent 到点发言后记账",
    "插件已关闭",
    "模型调用失败",
    "临时提醒",
    "她没说下次时间",
)


def _is_boilerplate_reason(reason: str) -> bool:
    """结构性套话（不含判断信息）不参与"复读"比对。"""
    text = reason.strip()
    return any(text.startswith(prefix) for prefix in _BOILERPLATE_REASONS)


class PageApi:
    """面板 API 实现。"""

    def __init__(self, plugin) -> None:
        self.plugin = plugin

    @property
    def log(self) -> logging.Logger:
        return getattr(self.plugin, "log", logging.getLogger(LOGGER_NAME))

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    async def get_state(self) -> dict[str, Any]:
        try:
            return ok(await self._build_payload())
        except Exception as exc:  # noqa: BLE001
            self.log.error(f"[proactive_mind] 面板状态读取失败: {exc}", exc_info=True)
            return fail(f"读取状态失败: {exc}")

    async def get_traces(self) -> dict[str, Any]:
        try:
            params = await _read_payload()
            limit = params.get("limit")
            try:
                limit_int = int(limit) if limit else 20
            except (TypeError, ValueError):
                limit_int = 20
            store = getattr(self.plugin, "store", None)
            traces = store.traces(limit_int) if store is not None else []
            return ok({"traces": self._mark_duplicates(traces)})
        except Exception as exc:  # noqa: BLE001
            return fail(f"读取轨迹失败: {exc}")

    async def get_tools(self) -> dict[str, Any]:
        try:
            settings = self.plugin.settings
            catalog = toolbox.build_catalog(self.plugin.context, settings, self.log)
            hobby = toolbox.build_catalog(
                self.plugin.context, settings, self.log, allowed=settings.hobby_tool_whitelist
            )
            return ok(
                {
                    "tools": catalog,
                    "count": len(catalog),
                    "hobby_tools": [item["name"] for item in hobby],
                }
            )
        except Exception as exc:  # noqa: BLE001
            return fail(f"读取工具失败: {exc}")

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    async def post_dialogue_set(self) -> dict[str, Any]:
        params = await _read_payload()
        umo = str(params.get("umo") or "").strip()
        when = params.get("wake_at") or params.get("when")
        if not umo or not when:
            return fail("缺少 umo 或 wake_at")
        result = await self.plugin.set_job_time(umo, jobs_mod.KIND_DIALOGUE, str(when))
        return ok(result) if result.get("ok") else fail(str(result.get("error")))

    async def post_hobby_set(self) -> dict[str, Any]:
        params = await _read_payload()
        umo = str(params.get("umo") or "").strip()
        when = params.get("wake_at") or params.get("when")
        if not umo or not when:
            return fail("缺少 umo 或 wake_at")
        intent = params.get("intent")
        result = await self.plugin.set_job_time(
            umo, jobs_mod.KIND_HOBBY, str(when), intent=str(intent) if intent else None
        )
        return ok(result) if result.get("ok") else fail(str(result.get("error")))

    async def post_hobby_intent(self) -> dict[str, Any]:
        params = await _read_payload()
        umo = str(params.get("umo") or "").strip()
        intent = str(params.get("intent") or "").strip()
        if not umo:
            return fail("缺少 umo")
        session = self.plugin.store.session(umo)
        jobs = jobs_mod.ensure_jobs(session)
        jobs["hobby"]["intent"] = intent[:600] or None
        jobs["hobby"]["intent_at"] = timeutil.now_iso()
        await self.plugin.store.save()
        return ok({"umo": umo, "intent": jobs["hobby"]["intent"]})

    async def post_temp_add(self) -> dict[str, Any]:
        params = await _read_payload()
        umo = str(params.get("umo") or "").strip()
        when = params.get("wake_at") or params.get("when")
        note = str(params.get("note") or "临时提醒")
        if not umo or not when:
            return fail("缺少 umo 或 wake_at")
        tz = self.plugin.scheduler.tz
        parsed = timeutil.parse_wake_time(str(when), reference=timeutil.now(tz), tz=tz)
        if parsed is None:
            return fail(f"时间无法解析：{when}")
        item = await self.plugin.schedule_explicit(umo, parsed, note, created_by="panel")
        if not item:
            return fail("创建失败（可能已达临时作业上限）")
        await self.plugin.store.save()
        return ok(item)

    async def post_temp_set(self) -> dict[str, Any]:
        """改某一条临时提醒的时间。"""
        params = await _read_payload()
        umo = str(params.get("umo") or "").strip()
        temp_id = str(params.get("temp_id") or "").strip()
        when = params.get("wake_at") or params.get("when")
        if not umo or not temp_id or not when:
            return fail("缺少 umo / temp_id / wake_at")
        result = await self.plugin.set_temp_time(umo, temp_id, str(when))
        return ok(result) if result.get("ok") else fail(str(result.get("error")))

    async def post_temp_cancel(self) -> dict[str, Any]:
        params = await _read_payload()
        umo = str(params.get("umo") or "").strip()
        temp_id = str(params.get("temp_id") or "").strip() or None
        if not umo:
            return fail("缺少 umo")
        await self.plugin.cancel_job(umo, jobs_mod.KIND_TEMP, temp_id)
        return ok({"umo": umo, "cancelled": temp_id or "all"})

    async def post_cancel(self) -> dict[str, Any]:
        params = await _read_payload()
        umo = str(params.get("umo") or "").strip()
        kind = str(params.get("kind") or "all").strip().lower()
        if not umo:
            return fail("缺少 umo")
        target = None if kind in {"all", ""} else kind
        if target is not None and target not in jobs_mod.KINDS:
            return fail("kind 只能是 dialogue/hobby/temp/all")
        await self.plugin.cancel_job(umo, target)
        return ok({"umo": umo, "cancelled": kind or "all"})

    async def post_run_now(self) -> dict[str, Any]:
        params = await _read_payload()
        umo = str(params.get("umo") or "").strip()
        kind = str(params.get("kind") or jobs_mod.KIND_DIALOGUE).strip().lower()
        if not umo:
            return fail("缺少 umo")
        if kind not in jobs_mod.KINDS:
            return fail("kind 只能是 dialogue/hobby/temp")
        if getattr(self.plugin, "engine", None) is None:
            return fail("插件尚未就绪", code="not_ready")
        asyncio.create_task(
            self.plugin.run_cycle(umo, kind=kind, trigger="page", bypass_limit=True)
        )
        return ok({"umo": umo, "kind": kind, "started": True})

    async def post_outbound_preview(self) -> dict[str, Any]:
        """预览出站三件套的效果（不改任何状态）。"""
        params = await _read_payload()
        text = str(params.get("text") or "").strip()
        if not text:
            return fail("缺少 text")
        from . import textfx

        segments = textfx.transform(text, self.plugin.settings, log=self.log)
        return ok({"input": text, "segments": segments, "count": len(segments)})

    async def post_count_reset(self) -> dict[str, Any]:
        """清零计数：默认清"连续触发次数"；``what=hobby_runs`` 清爱好轮数。"""
        params = await _read_payload()
        umo = str(params.get("umo") or "").strip()
        if not umo:
            return fail("缺少 umo")
        what = str(params.get("what") or "wake").strip().lower()
        store = self.plugin.store
        session = store.session(umo)
        if what in {"hobby_runs", "hobby", "runs"}:
            jobs = jobs_mod.ensure_jobs(session)
            jobs["hobby"]["runs"] = 0
            jobs["hobby"]["scheduled"] = 0
            await store.save()
            return ok({"umo": umo, "hobby_runs": 0, "scheduled": 0})
        if what == "all":
            jobs = jobs_mod.ensure_jobs(session)
            jobs["hobby"]["runs"] = 0
            jobs["hobby"]["scheduled"] = 0
            store.set(umo, silent_wake_count=0, last_error=None)
            await store.save()
            return ok({"umo": umo, "silent_wake_count": 0, "hobby_runs": 0})
        store.set(umo, silent_wake_count=0, last_error=None)
        await store.save()
        return ok({"umo": umo, "silent_wake_count": 0})

    async def post_config_patch(self) -> dict[str, Any]:
        params = await _read_payload()
        patch = params.get("patch")
        if not isinstance(patch, dict):
            patch = {
                key: value
                for key, value in params.items()
                if isinstance(key, str) and "." in key
            }
        filtered = {k: v for k, v in (patch or {}).items() if k in CONFIG_PATCH_WHITELIST}
        if not filtered:
            return fail("没有可修改的配置项")
        applied = self.plugin.settings.update(filtered)
        saved = await self.plugin.save_settings()
        return ok({"applied": applied, "saved": saved})

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _mark_duplicates(self, traces: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """给"疑似复读"打标（与相邻一条 reason 相似度 ≥ 阈值）。

        只比"有信息量"的理由：记录模式/主 Agent 记账这类结构性套话天然一字不差，
        拿来比对必然 100% 相似，标成复读纯属噪音，直接跳过。
        """
        threshold = float(self.plugin.settings.trace_similarity)
        result: list[dict[str, Any]] = []
        previous = ""
        for trace in traces:  # 新的在前
            reason = str(trace.get("reason") or "")
            item = dict(trace)
            if (
                previous
                and reason
                and not _is_boilerplate_reason(reason)
                and not _is_boilerplate_reason(previous)
            ):
                ratio = difflib.SequenceMatcher(None, previous, reason).ratio()
                item["reason_similarity"] = round(ratio, 3)
                item["looks_repeated"] = ratio >= threshold
            previous = reason or previous
            result.append(item)
        return result

    async def _last_conversation_line(self, umo: str) -> dict[str, Any] | None:
        """最近一条真实对话（不是插件自己的主动消息）。"""
        plugin = flow_mod.find_livingmemory(self.plugin.settings, self.log)
        manager = getattr(getattr(plugin, "initializer", None), "conversation_manager", None)
        getter = getattr(manager, "get_messages", None)
        if callable(getter):
            try:
                messages = await getter(umo, limit=1)
            except Exception:
                messages = []
            if messages:
                message = messages[-1]
                return {
                    "role": str(getattr(message, "role", "")),
                    "sender": getattr(message, "sender_name", None),
                    "text": flow_mod.content_to_text(getattr(message, "content", ""))[:160],
                    "at": timeutil.iso(
                        datetime.fromtimestamp(float(getattr(message, "timestamp", 0) or 0))
                    )
                    if getattr(message, "timestamp", None)
                    else None,
                }
        items, _source, _warn = await flow_mod.read_flow(
            self.plugin.context, self.plugin.settings, umo, self.log
        )
        if items:
            last = items[-1]
            return {"role": last.role, "sender": last.sender, "text": last.text[:160], "at": None}
        return None

    async def _build_payload(self) -> dict[str, Any]:
        plugin = self.plugin
        settings = plugin.settings
        store = plugin.store
        scheduler = plugin.scheduler
        settings_defaults = DEFAULTS
        tz = scheduler.tz
        now = timeutil.now(tz)

        livingmemory_plugin = flow_mod.find_livingmemory(settings, self.log)
        window, window_source = flow_mod.livingmemory_window(livingmemory_plugin, settings)
        catalog = toolbox.build_catalog(plugin.context, settings, self.log)
        provider_label = await self._provider_label()

        sessions: list[dict[str, Any]] = []
        for umo, session in (store.all_sessions() or {}).items():
            jobs = jobs_mod.ensure_jobs(session)
            dialogue = jobs.get("dialogue") or {}
            hobby = jobs.get("hobby") or {}
            temps = jobs_mod.sort_temp(jobs)

            def job_view(data: dict[str, Any], kind: str) -> dict[str, Any]:
                when = timeutil.from_iso(data.get("next_at")) if data.get("next_at") else None
                live = scheduler.next_run(umo, kind=kind)
                if live is not None:
                    when = live
                elif when is not None and when <= now:
                    # 已到点但没有存活作业（例如静默后按规则不重排）：按"未安排"显示，
                    # 避免面板一直停在"还有 0 秒"。
                    when = None
                return {
                    "next_at": timeutil.iso(when),
                    "human": timeutil.human(when),
                    "countdown": None if when is None else max(0, int((when - now).total_seconds())),
                    "cancelled": bool(data.get("cancelled")),
                }

            last_sent_at = timeutil.from_iso(session.get("last_sent_at"))
            age_hours = (
                (now - last_sent_at).total_seconds() / 3600 if last_sent_at is not None else None
            )
            sessions.append(
                {
                    "umo": umo,
                    "platform": session.get("platform") or (umo.split(":", 1)[0] if ":" in umo else None),
                    "dialogue": {
                        **job_view(dialogue, jobs_mod.KIND_DIALOGUE),
                        "topic": dialogue.get("topic"),
                        "topic_at": dialogue.get("topic_at"),
                        "provisional": bool(dialogue.get("provisional")),
                        "last_spoke_at": dialogue.get("last_spoke_at"),
                    },
                    "hobby": {
                        **job_view(hobby, jobs_mod.KIND_HOBBY),
                        "intent": hobby.get("intent"),
                        "intent_at": hobby.get("intent_at"),
                        "runs": int(hobby.get("runs") or 0),
                        "scheduled": int(hobby.get("scheduled") or 0),
                    },
                    "temp": [
                        {
                            "id": item.get("id"),
                            "next_at": item.get("next_at"),
                            "human": timeutil.human(timeutil.from_iso(item.get("next_at"))),
                            "note": item.get("note"),
                            "created_by": item.get("created_by"),
                        }
                        for item in temps
                    ],
                    "silent_wake_count": int(session.get("silent_wake_count") or 0),
                    "last_user_message_at": session.get("last_user_message_at"),
                    "last_error": session.get("last_error"),
                    "last_sent": {
                        "message": session.get("last_sent_message"),
                        "at": session.get("last_sent_at"),
                        "human": timeutil.human(last_sent_at),
                        "age_hours": round(age_hours, 1) if age_hours is not None else None,
                        "stale": bool(age_hours is not None and age_hours >= settings.stale_hours),
                    },
                    "last_dialogue": await self._last_conversation_line(umo),
                    "last_echo": session.get("last_echo") or {},
                }
            )

        return {
            "plugin": {
                "name": PLUGIN_NAME_FALLBACK,
                "version": getattr(plugin, "version", "0.5.0"),
                "enabled": settings.enabled,
                "ready": getattr(plugin, "engine", None) is not None,
                "server_time": timeutil.iso(now),
                "server_time_human": timeutil.human(now),
                "timezone": str(tz),
                "stale_hours": settings.stale_hours,
            },
            "runtime": {
                "provider": provider_label,
                "tool_count": len(catalog),
                "tools": [item["name"] for item in catalog],
                "livingmemory": livingmemory_plugin is not None,
                "window": window,
                "window_source": window_source,
                "flow_source": settings.flow_source,
                "scheduler_running": bool(getattr(scheduler, "started", False)),
            },
            "settings": {
                "wake_delay_minutes": settings.wake_delay_minutes,
                "default_interval_minutes": settings.default_interval_minutes,
                "min_interval_minutes": settings.min_interval_minutes,
                "max_interval_minutes": settings.max_interval_minutes,
                "hobby_default_interval_minutes": settings.hobby_default_interval_minutes,
                "hobby_min_interval_minutes": settings.hobby_min_interval_minutes,
                "hobby_max_interval_minutes": settings.hobby_max_interval_minutes,
                "hobby_memory_count": settings.hobby_memory_count,
                "hobby_flow_count": settings.hobby_flow_count,
                "max_consecutive_wakes": settings.max_consecutive_wakes,
                "single_request": settings.single_request,
                "memory_mode": settings.memory_mode,
                "stale_hours": settings.stale_hours,
                "flow_max_age_hours": settings.flow_max_age_hours,
                "user_message_mode": settings.user_message_mode,
                "next_time_marker": settings.next_time_marker,
                "catalog_max_entries": settings.catalog_max_entries,
                "catalog_char_budget": settings.catalog_char_budget,
                "temp_max_jobs": settings.temp_max_jobs,
                "user_message_mode_raw": settings.user_message_mode,
                "dialogue_execute": settings.dialogue_execute,
                "temp_execute": settings.temp_execute,
                "provisional_enabled": settings.provisional_enabled,
                "output_hint": settings.output_hint,
                "wake_hint": settings.wake_hint,
                "random_min_minutes": settings.min_interval_minutes,
                "random_max_minutes": settings.max_interval_minutes,
            },
            "outbound": {
                "replace_enabled": settings.outbound_replace_enabled,
                "replace_rules": settings.outbound_replace_rules,
                "segment_enabled": settings.outbound_segment_enabled,
                "segment_pattern": settings.outbound_segment_pattern,
                "segment_max": settings.outbound_segment_max,
                "segment_interval": settings.outbound_segment_interval,
                "cleanup_enabled": settings.outbound_cleanup_enabled,
                "cleanup_pattern": settings.outbound_cleanup_pattern,
            },
            "hint_defaults": {
                "output_hint": str(
                    (settings_defaults.get("mind") or {}).get("output_hint", "")
                ),
                "wake_hint": str(
                    (settings_defaults.get("mind") or {}).get("wake_hint", "")
                ),
            },
            "sessions": sessions,
            "traces": self._mark_duplicates(store.traces(20)),
        }

    async def _provider_label(self) -> str:
        try:
            provider = await llm.resolve_provider(
                self.plugin.context, self.plugin.settings, "", self.log
            )
        except Exception:
            return "（未知）"
        if provider is None:
            return "（未配置）"
        provider_id = ""
        try:
            provider_id = str(getattr(provider.meta(), "id", "") or "")
        except Exception:
            provider_id = ""
        try:
            model = str(provider.get_model() or "")
        except Exception:
            model = ""
        parts = [part for part in (provider_id, model) if part]
        return " / ".join(parts) if parts else "（已连接）"



ROUTES: tuple[tuple[str, str, list[str], str], ...] = (
    ("state", "get_state", ["GET"], "proactive-mind state"),
    ("traces", "get_traces", ["GET"], "proactive-mind decision traces"),
    ("tools", "get_tools", ["GET"], "proactive-mind available tools"),
    ("dialogue/set", "post_dialogue_set", ["POST"], "proactive-mind set dialogue time"),
    ("hobby/set", "post_hobby_set", ["POST"], "proactive-mind set hobby time"),
    ("hobby/intent", "post_hobby_intent", ["POST"], "proactive-mind set hobby intent"),
    ("temp/add", "post_temp_add", ["POST"], "proactive-mind add temp job"),
    ("temp/set", "post_temp_set", ["POST"], "proactive-mind update temp job time"),
    ("temp/cancel", "post_temp_cancel", ["POST"], "proactive-mind cancel temp job"),
    ("cancel", "post_cancel", ["POST"], "proactive-mind cancel jobs"),
    ("run-now", "post_run_now", ["POST"], "proactive-mind run a cycle now"),
    ("outbound/preview", "post_outbound_preview", ["POST"], "proactive-mind preview outbound regex"),
    ("count/reset", "post_count_reset", ["POST"], "proactive-mind reset counter"),
    ("config/patch", "post_config_patch", ["POST"], "proactive-mind patch config"),
)
