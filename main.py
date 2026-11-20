"""主动心智 Proactive Mind —— 插件入口。

三类唤醒作业（对话 / 爱好 / 临时）的统一装配：
* 状态与作业：`core/state.py`（v2，含 jobs）+ `core/jobs.py`
* 调度：`core/scheduler.py`（每类一个作业 id）
* 决策：`core/decide.py`（单上下文流水线）
* 面板 API：`core/webapi.py`

本文件只做接线：事件钩子、初始化/恢复、命令、工具注册、收尾。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from datetime import timedelta
from pathlib import Path
from typing import Any

import astrbot.api.star as star
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.event.filter import PermissionType, permission_type
from astrbot.core.config.astrbot_config import AstrBotConfig

from .core import flow as flow_mod
from .core import jobs as jobs_mod
from .core import timeutil, webapi
from .core import toolbox
from .core.decide import DecisionEngine
from .core.scheduler import WakeScheduler
from .core.settings import Settings
from .core.state import StateStore
from .core.memory import write_memory
from .core.tools import ProactiveScheduleTool, ProactiveWakeTool

PLUGIN_NAME = "astrbot_plugin_proactive_mind"
PLUGIN_VERSION = "0.5.0"


class ProactiveMindPlugin(star.Star):
    """主动心智插件主类。"""

    def __init__(self, context: star.Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.context = context
        self.config: AstrBotConfig = config
        self.settings = Settings(config)
        self.log = logger
        self.name = PLUGIN_NAME
        self.version = PLUGIN_VERSION

        self.data_dir = Path(str(star.StarTools.get_data_dir(PLUGIN_NAME)))
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.store = StateStore(self.data_dir / "state.json", self.settings.trace_keep)
        self.scheduler = WakeScheduler(
            on_fire=self._on_wake,
            timezone_name=self.settings.timezone_name,
            logger=self.log,
        )
        self.engine: DecisionEngine | None = None
        self.page_api = webapi.PageApi(self)

        self._tasks: set[asyncio.Task] = set()
        self._debounce: dict[str, asyncio.Task] = {}
        self._initialized = False
        self._init_lock = asyncio.Lock()
        self._last_state_save = 0.0
        self._wake_tool_registered = False

        self._register_page_routes()
        self._create_task(self._bootstrap())

    # ------------------------------------------------------------------
    # 装配
    # ------------------------------------------------------------------
    def _register_page_routes(self) -> None:
        for suffix, handler_name, methods, desc in webapi.ROUTES:
            handler = getattr(self.page_api, handler_name, None)
            if handler is None:
                self.log.warning(f"[proactive_mind] 面板路由处理函数缺失: {handler_name}")
                continue
            try:
                self.context.register_web_api(
                    f"/{PLUGIN_NAME}/page/{suffix}", handler, methods, desc
                )
            except Exception as exc:  # pragma: no cover
                self.log.error(f"[proactive_mind] 注册面板路由失败 {suffix}: {exc}")

    def _create_task(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)
        return task

    def _on_task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self.log.error(f"[proactive_mind] 后台任务异常: {exc}", exc_info=exc)

    async def _bootstrap(self) -> None:
        await asyncio.sleep(1)
        await self.ensure_initialized()

    async def ensure_initialized(self) -> bool:
        """加载状态、启动调度器、恢复三类作业（幂等）。"""
        async with self._init_lock:
            if self._initialized:
                return True
            try:
                self.store.trace_keep = self.settings.trace_keep
                await self.store.load()
                if self.store.last_reason:
                    self.log.info(f"[proactive_mind] 状态文件：{self.store.last_reason}")
                self.engine = DecisionEngine(
                    context=self.context,
                    settings=self.settings,
                    store=self.store,
                    scheduler=self.scheduler,
                    data_dir=self.data_dir,
                    logger=self.log,
                )
                if self.settings.enabled:
                    self.scheduler.tz = timeutil.resolve_tz(self.settings.timezone_name)
                    self.scheduler.start()
                    actions = self.scheduler.restore(
                        self.store.all_sessions(),
                        catch_up=self.settings.catch_up_missed_wakes,
                        default_interval_minutes=self.settings.default_interval_minutes,
                        min_interval_minutes=self.settings.min_interval_minutes,
                        hobby_default_interval_minutes=self.settings.hobby_default_interval_minutes,
                        hobby_min_interval_minutes=self.settings.hobby_min_interval_minutes,
                    )
                    if actions:
                        self.log.info(
                            f"[proactive_mind] 已恢复 {len(actions)} 个作业: {actions}"
                        )
                    self._auto_start_hobby()
                    await self.store.save()
                    await self._drain_stale_wake_events()
                self._initialized = True
                self._register_wake_tool()
                self.log.info(
                    f"[proactive_mind] 插件就绪 v{PLUGIN_VERSION}"
                    f"（dialogue/hobby/temp，hobby 默认 {self.settings.hobby_default_interval_minutes} 分钟）"
                )
                return True
            except Exception as exc:  # noqa: BLE001
                self.log.error(f"[proactive_mind] 初始化失败: {exc}", exc_info=True)
                return False

    async def _drain_stale_wake_events(self) -> int:
        """清掉事件队列里积压的 pmind 唤醒事件（重载/补跑曾一次塞进多条）。

        只丢弃 extras 里带 pmind_wake 的 cron 事件，其他事件原样放回队列。
        """
        dropped = 0
        try:
            queue = self.context.get_event_queue()
        except Exception:
            return 0
        retained = []
        while True:
            try:
                event = queue.get_nowait()
            except Exception:
                break
            keep = True
            with contextlib.suppress(Exception):
                if event.get_platform_name() == "cron" and event.get_extra(self.PMIND_WAKE_EXTRA):
                    keep = False
            if keep:
                retained.append(event)
            else:
                dropped += 1
        for event in retained:
            with contextlib.suppress(Exception):
                queue.put_nowait(event)
        if dropped:
            self.log.warning(f"[proactive_mind] 已清掉队列里积压的 {dropped} 条唤醒事件")
        return dropped

    def _auto_start_hobby(self) -> None:
        """给已有活动的会话补一个爱好节奏起点（没有就按默认间隔排）。"""
        if not self.settings.hobby_auto_start:
            return
        now = timeutil.now(self.scheduler.tz)
        for umo, session in (self.store.all_sessions() or {}).items():
            if not isinstance(session, dict):
                continue
            jobs = jobs_mod.ensure_jobs(self.store.session(umo))
            hobby = jobs.get("hobby") or {}
            if hobby.get("cancelled") or hobby.get("next_at"):
                continue
            import random

            low = int(self.settings.hobby_min_interval_minutes)
            high = max(low + 1, int(self.settings.hobby_max_interval_minutes))
            target = now + timedelta(minutes=random.randint(low, high))
            try:
                actual = self.scheduler.set_wake(umo, target, kind=jobs_mod.KIND_HOBBY)
            except Exception as exc:  # pragma: no cover
                self.log.error(f"[proactive_mind] 爱好节奏起点创建失败 {umo}: {exc}")
                continue
            hobby["next_at"] = timeutil.iso(actual)
            self.log.info(f"[proactive_mind] 为 {umo} 建立爱好节奏起点：{timeutil.human(actual)}")

    async def initialize(self) -> None:
        """AstrBot 生命周期钩子。"""
        await self.ensure_initialized()

    def _register_wake_tool(self) -> None:
        """把调度工具交给主 Agent（pmind_wake=临时，pmind_schedule=三类都能排）。"""
        if self._wake_tool_registered or not self.settings.expose_wake_tool:
            return
        try:
            self.context.add_llm_tools(ProactiveWakeTool(plugin=self))
            self.context.add_llm_tools(ProactiveScheduleTool(plugin=self))
            self._wake_tool_registered = True
            self.log.info(
                "[proactive_mind] 已向主 Agent 暴露 pmind_wake / pmind_schedule（三类节奏都能排）"
            )
        except Exception as exc:  # pragma: no cover
            self.log.error(f"[proactive_mind] 注册调度工具失败: {exc}")

    async def write_memory_text(
        self,
        umo: str,
        text: str,
        *,
        topics: list[str] | None = None,
        importance: float | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        """给工具用：按 LivingMemory 原有格式写一条记忆。"""
        if not await self.ensure_initialized():
            return {"ok": False, "error": "not_ready"}
        return await write_memory(
            self.context,
            self.settings,
            umo=umo,
            content=text,
            topics=topics,
            reason=reason,
            importance=importance,
            log=self.log,
            event=toolbox.make_event(self.context, umo),
            data_dir=self.data_dir,
        )

    async def schedule_explicit(self, umo: str, when, note: str = "", *, created_by: str = "pmind_wake"):
        """主 Agent 通过工具安排临时作业。"""
        if not await self.ensure_initialized() or self.engine is None:
            return None
        return self.engine.add_temp(
            umo,
            when,
            note or "到点回来看一眼",
            created_by=created_by,
        )

    # ------------------------------------------------------------------
    # 事件入口
    # ------------------------------------------------------------------
    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        await self.ensure_initialized()

    WAKE_HINT_MARKER = "【主动心智·延时规则】"
    PMIND_WAKE_EXTRA = "pmind_wake"

    # ------------------------------------------------------------------
    # 每轮注入（output_hint）+ 唤醒轮注入（wake_hint）
    # ------------------------------------------------------------------
    @filter.on_llm_request()
    async def inject_wake_hint(self, event: AstrMessageEvent, req) -> None:
        """给主 Agent 注入两条提示：

        * ``output_hint``：每轮都加，提醒她结尾给 `[[pmind_next:时间|话题]]`；
        * ``wake_hint``：只在 pmind 到点唤醒那一轮加，说明"你自己醒来"并带上上次念头。
        """
        if not self.settings.enabled:
            return
        parts: list[str] = []

        wake_extra = None
        with contextlib.suppress(Exception):
            wake_extra = event.get_extra(self.PMIND_WAKE_EXTRA)
        if isinstance(wake_extra, dict):
            topic = str(wake_extra.get("topic") or "").strip()
            since = str(wake_extra.get("since") or "").strip()
            kind = str(wake_extra.get("kind") or "dialogue")
            template = self.settings.wake_hint or ""
            if template:
                rendered = template
                if topic:
                    rendered = rendered.replace("{topic}", topic)
                else:
                    # 没念头就把这一行整条去掉，避免"上次留下的念头："后面空着
                    rendered = "\n".join(
                        line for line in rendered.splitlines() if "{topic}" not in line
                    )
                rendered = rendered.replace("{since}", since or "未知")
                rendered = rendered.replace("{kind}", kind)
                parts.append(rendered.strip())

        output_hint = self.settings.output_hint or ""
        if output_hint:
            parts.append(output_hint.strip())

        if not parts:
            return
        block = "\n\n【主动心智·输出约定】\n" + "\n".join(p for p in parts if p)
        appended = False
        with contextlib.suppress(Exception):
            container = getattr(req, "extra_user_content_parts", None)
            if isinstance(container, list):
                container.append({"type": "text", "text": block, "_no_save": True})
                appended = True
        if not appended:
            with contextlib.suppress(Exception):
                prompt = getattr(req, "system_prompt", None) or ""
                if "【主动心智·输出约定】" not in prompt:
                    req.system_prompt = prompt + block

    @filter.on_llm_response(priority=100)
    async def strip_next_marker_on_response(self, event: AstrMessageEvent, resp) -> None:
        """在 LLM 原始响应上就把 ``[[pmind_next:...]]`` 抠掉。

        比只做 ``on_decorating_result`` 更早：那时候文本已经进了结果链/历史，
        一旦装饰阶段没生效（分段插件先改写、流式拼接等），标记就会连同话题被发出去。
        """
        if not self.settings.next_time_marker:
            return
        text = ""
        with contextlib.suppress(Exception):
            text = str(getattr(resp, "completion_text", "") or "")
        cleaned, value = timeutil.strip_next_marker(text)
        if value:
            with contextlib.suppress(Exception):
                event.set_extra("pmind_next_at", value)
        if cleaned != text:
            with contextlib.suppress(Exception):
                resp.completion_text = cleaned
        if value:
            return
        # 正文里没有：读思考块（reasoning_content）兜底。
        # 推理模型常把 [[pmind_next:…]] 当"给自己的备注"写进 think，正文只写对用户说的话。
        think = ""
        with contextlib.suppress(Exception):
            think = str(getattr(resp, "reasoning_content", "") or "")
        if not think or "pmind" not in think.lower():
            return
        _, think_value = timeutil.strip_next_marker(think)
        if think_value:
            with contextlib.suppress(Exception):
                event.set_extra("pmind_next_at", think_value)
                event.set_extra("pmind_next_from_think", True)
            self.log.info(
                f"[proactive_mind] 标记取自思考块：{think_value}（正文里没有）"
            )

    @filter.on_decorating_result()
    async def strip_next_time_marker(self, event: AstrMessageEvent) -> None:
        """把回复里的 [[pmind_next:...]] 抠掉，并把时间存进事件 extra 供后续使用。"""
        if not self.settings.next_time_marker:
            return
        try:
            result = event.get_result()
        except Exception:
            return
        if result is None:
            return
        chain = getattr(result, "chain", None)
        if not isinstance(chain, list) or not chain:
            return
        extracted: str | None = None
        changed = False
        for component in chain:
            if getattr(component, "type", "") != "Plain":
                continue
            text = getattr(component, "text", "") or ""
            cleaned, value = timeutil.strip_next_marker(text)
            if value:
                extracted = value
            if cleaned != text:
                try:
                    component.text = cleaned
                    changed = True
                except Exception:
                    pass
        if extracted:
            with contextlib.suppress(Exception):
                event.set_extra("pmind_next_at", extracted)
        if changed:
            # 标记单独占一行时，Plain 组件会变成空串；空串保留会导致空消息
            remaining = [
                component
                for component in chain
                if not (
                    getattr(component, "type", "") == "Plain"
                    and not (getattr(component, "text", "") or "").strip()
                )
            ]
            if remaining:
                with contextlib.suppress(Exception):
                    result.chain = remaining
            else:
                # 只剩标记、没有正文：这轮基本等于空回复，记一条日志便于排查
                self.log.warning(
                    "[proactive_mind] 回复里只有 [[pmind_next]] 标记、没有正文，本轮可能不会发出消息"
                )

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent, *_args) -> None:
        try:
            await self._handle_after_sent(event)
        except Exception as exc:  # noqa: BLE001
            self.log.error(f"[proactive_mind] after_message_sent 处理失败: {exc}")

    async def _handle_after_sent(self, event: AstrMessageEvent) -> None:
        platform_name = ""
        with contextlib.suppress(Exception):
            platform_name = str(event.get_platform_name() or "")
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if platform_name == "cron":
            # 主 Agent 对我们投递的唤醒事件作出的回复：只记账（更新 job、清话题）
            wake_extra = None
            with contextlib.suppress(Exception):
                wake_extra = event.get_extra(self.PMIND_WAKE_EXTRA)
            if isinstance(wake_extra, dict) and umo:
                if not await self.ensure_initialized():
                    return
                wake_id = str(wake_extra.get("wake_id") or "")
                session_now = self.store.session(umo)
                # 只认"最近一次投递"：没有对应投递的 cron 发送（旧事件被管线重复处理）
                # 一律不记账，否则会把下次时间改早。
                anchor = session_now.get("last_delivery_at") or session_now.get("wake_pending_at")
                anchor_dt = timeutil.from_iso(anchor)
                window = int(self.settings.wake_record_window_seconds)
                now_rec = timeutil.now(self.scheduler.tz)
                in_window = (
                    anchor_dt is not None
                    and 0 <= (now_rec - anchor_dt).total_seconds() <= max(60, window)
                )
                if not in_window and not (wake_id and session_now.get("last_wake_id") == wake_id):
                    self.log.info(
                        f"[proactive_mind] 跳过记账：找不到对应的最近投递"
                        f"（anchor={anchor}，窗口 {window}s）"
                    )
                    return
                # 同一次投递只记一次账：after_message_sent 在流式回复时会按分片多次触发
                delivery_stamp = session_now.get("last_delivery_at") or session_now.get("wake_pending_at")
                if delivery_stamp and session_now.get("recorded_for_delivery") == delivery_stamp:
                    self.log.info(
                        f"[proactive_mind] 跳过重复记账：本次投递 {delivery_stamp} 已记过"
                    )
                    return
                if delivery_stamp:
                    session_now["recorded_for_delivery"] = delivery_stamp
                if wake_id and session_now.get("last_wake_id") == wake_id:
                    if session_now.get("last_wake_recorded"):
                        self.log.info(
                            f"[proactive_mind] 跳过重复记账：wake_id={wake_id[:8]} 已记过"
                        )
                        return
                    session_now["last_wake_recorded"] = True
                if self.engine is not None:
                    marker = None
                    marker_from_think = False
                    with contextlib.suppress(Exception):
                        marker = event.get_extra("pmind_next_at")
                        marker_from_think = bool(event.get_extra("pmind_next_from_think"))
                    with contextlib.suppress(Exception):
                        await self.engine.record_main_agent_reply(
                            umo,
                            kind=str(wake_extra.get("kind") or jobs_mod.KIND_DIALOGUE),
                            text=self._sent_text(event),
                            marker=marker,
                            temp_id=wake_extra.get("temp_id") or None,
                            note="主 Agent 到点发言后记账",
                            marker_from_think=marker_from_think,
                        )
            return
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            return
        if not await self.ensure_initialized():
            return

        # 其他插件的定时器塞进来的 "[系统唤醒] 备注: …" 不是用户发言
        message_str = ""
        with contextlib.suppress(Exception):
            message_str = str(getattr(event, "message_str", "") or "")
        if flow_mod.is_system_note(message_str):
            self._cache_bot_identity(event, self.store.session(umo))
            await self._save_state()
            return

        now = timeutil.now(self.scheduler.tz)
        session = self.store.session(umo)
        session["platform"] = platform_name or session.get("platform")
        session["last_user_message_at"] = timeutil.iso(now)
        session["silent_wake_count"] = 0
        session["user_replied_since_last_wake"] = True
        session["last_error"] = None
        self._cache_bot_identity(event, session)
        await self._save_state()

        if not self.settings.enabled or not self.settings.trigger_on_user_message:
            return
        mode = (self.settings.user_message_mode or "decide").strip().lower()
        next_hint = None
        with contextlib.suppress(Exception):
            next_hint = event.get_extra("pmind_next_at")
        self._schedule_user_trigger(
            umo,
            record_only=(mode == "record_only"),
            next_at_hint=next_hint,
            main_reply_text=self._sent_text(event),
        )

    @staticmethod
    def _sent_text(event: AstrMessageEvent) -> str:
        """取刚发出去的那条回复的文本（供轨迹如实记录"是谁说的话"）。"""
        text = ""
        with contextlib.suppress(Exception):
            result = event.get_result()
            chain = getattr(result, "chain", None) or []
            parts: list[str] = []
            for component in chain:
                if getattr(component, "type", "") != "Plain":
                    continue
                piece = getattr(component, "text", "") or ""
                if piece.strip():
                    parts.append(piece)
            text = "\n".join(parts).strip()
        if not text:
            with contextlib.suppress(Exception):
                text = str(event.get_result().get_plain_text() or "").strip()
        return text[:1200]

    def _cache_bot_identity(self, event: AstrMessageEvent, session: dict[str, Any]) -> None:
        bot_id = None
        func = getattr(event, "get_self_id", None)
        if callable(func):
            with contextlib.suppress(Exception):
                bot_id = func()
        message_obj = getattr(event, "message_obj", None)
        if not bot_id and message_obj is not None:
            bot_id = getattr(message_obj, "self_id", None)
        if bot_id:
            session["bot_self_id"] = str(bot_id)
            name = None
            for source in (message_obj, event):
                for key in ("self_name", "bot_name", "bot_nickname"):
                    name = getattr(source, key, None)
                    if name:
                        break
                if name:
                    break
            if name:
                session["bot_self_name"] = str(name)

    def _schedule_user_trigger(
        self,
        umo: str,
        *,
        record_only: bool = False,
        next_at_hint: Any = None,
        main_reply_text: str = "",
    ) -> None:
        existing = self._debounce.get(umo)
        if existing is not None and not existing.done():
            existing.cancel()
        delay = float(
            max(0, self.settings.debounce_seconds + self.settings.post_reply_delay_seconds)
        )

        async def runner() -> None:
            try:
                await asyncio.sleep(delay)
                # 用户消息周期 = 对话节奏；主 Agent 刚回复过，视为"自己说了话"。
                # record_only=True：主 Agent 的回复就是本次 job 的执行体，插件不补话。
                await self.run_cycle(
                    umo,
                    kind=jobs_mod.KIND_DIALOGUE,
                    trigger="user_message",
                    spoke_before=True,
                    record_only=record_only,
                    next_at_hint=next_at_hint,
                    main_reply_text=main_reply_text,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.log.error(f"[proactive_mind] 用户消息触发的判断失败: {exc}")

        self._debounce[umo] = asyncio.create_task(runner())

    async def _on_wake(self, umo: str, kind: str, temp_id: str | None) -> None:
        if not await self.ensure_initialized():
            return
        # 对话 / 临时：交给主 Agent 走完整管线（含工具循环、人格、记忆、正则）
        if kind in {jobs_mod.KIND_DIALOGUE, jobs_mod.KIND_TEMP}:
            mode = (
                self.settings.dialogue_execute
                if kind == jobs_mod.KIND_DIALOGUE
                else self.settings.temp_execute
            )
            if mode == "main_agent" and await self._deliver_wake(umo, kind, temp_id):
                return
        trigger = {
            jobs_mod.KIND_HOBBY: "hobby_cron",
            jobs_mod.KIND_TEMP: "temp_cron",
        }.get(kind, "dialogue_cron")
        await self.run_cycle(umo, kind=kind, trigger=trigger, temp_id=temp_id)

    async def _grace_pad(self, umo: str, stamp: str) -> None:
        """等一段宽限期：如果这段时间里她一直没回复（没记账），才补垫一个保底时间。

        这样"她给了下次时间"时保底永远不会生效——它只在真的没回应时才出现。
        """
        grace = int(self.settings.provisional_grace_seconds)
        if grace <= 0:
            return
        try:
            await asyncio.sleep(grace)
        except asyncio.CancelledError:
            raise
        if self.engine is None:
            return
        session = self.store.session(umo)
        if session.get("wake_pending_at") != stamp:
            return  # 期间她已经回复/已经有新安排
        padded = await self.engine.provisional_schedule(umo, jobs_mod.KIND_DIALOGUE)
        if padded is not None:
            session["wake_pending_at"] = None
            self.log.info(
                f"[proactive_mind] 宽限期 {grace}s 内没有回复，已补垫保底时间 "
                f"{timeutil.human(padded)}"
            )
        await self.store.save()

    async def _deliver_wake(self, umo: str, kind: str, temp_id: str | None) -> bool:
        """把"到点了"投递给主 Agent：构造 CronMessageEvent 放进事件队列。

        主 Agent 走完整 agent 管线：人格、LivingMemory 记忆召回、工具循环、正则分段。
        """
        session = self.store.session(umo)
        jobs = jobs_mod.ensure_jobs(session)
        topic = ""
        note = ""
        now_delivery = timeutil.now(self.scheduler.tz)
        # 去重闸门：距离上一次投递不到 guard 秒 → 认为是保底/重复 job，直接吞掉
        guard = int(self.settings.delivery_guard_seconds)
        last_delivery = timeutil.from_iso(session.get("last_delivery_at"))
        if guard > 0 and last_delivery is not None:
            elapsed = (now_delivery - last_delivery).total_seconds()
            if 0 <= elapsed < guard:
                self.log.info(
                    f"[proactive_mind] 跳过本次唤醒：距上次投递仅 {int(elapsed)} 秒"
                    f"（<{guard}s，疑似保底/重复 job）"
                )
                return True
        if kind == jobs_mod.KIND_DIALOGUE:
            # 幂等：上一轮唤醒还没被处理完（pending 未清）时不再投递，
            # 否则会一次堆进多条唤醒事件，被管线依次执行、看起来像"连续提前说话"。
            pending_iso = session.get("wake_pending_at")
            pending_dt = timeutil.from_iso(pending_iso)
            if pending_dt is not None and (now_delivery - pending_dt).total_seconds() < 1800:
                self.log.info(
                    f"[proactive_mind] 跳过投递：上一轮唤醒（{pending_iso}）还在处理中"
                )
                return True
            topic = str(jobs["dialogue"].get("topic") or "").strip()
            now = timeutil.now(self.scheduler.tz)
            existing = timeutil.from_iso(jobs["dialogue"].get("next_at"))
            has_future = existing is not None and existing > now + timedelta(seconds=60)
            if has_future:
                # 已经有未来时间（她刚给过/已排好）：这次"到点"其实是旧的保底 job 迟到触发，
                # 直接吞掉，不再唤醒主 Agent —— 否则就会"提前说话"。
                self.log.info(
                    f"[proactive_mind] 跳过本次唤醒：已有未来时间 "
                    f"{timeutil.human(existing)}（疑似旧保底 job 迟到）"
                )
                return True
            # 关键：**投递时不再垫保底**。
            # 垫了就会变成一条独立 job，随机时间可能比她给的下次时间更早，造成"提前说话"。
            # 现在改成：到点即消费 → 记一个"等待回复"标记 → 由 grace 任务在真的没有回复时才补垫。
            jobs["dialogue"]["next_at"] = None
            session["next_wake_at"] = None
            session["wake_pending_at"] = timeutil.iso(now)
            if self.engine is not None and self.settings.provisional_enabled:
                self._create_task(self._grace_pad(umo, session["wake_pending_at"]))
        else:
            item = jobs_mod.find_temp(jobs, temp_id or "") if temp_id else None
            if item is None and jobs["temp"]:
                item = jobs_mod.sort_temp(jobs)[0]
            if item:
                temp_id = str(item.get("id") or temp_id or "")
                note = str(item.get("note") or "")
        session["last_wake_at"] = timeutil.now_iso(self.scheduler.tz)

        last_user = timeutil.from_iso(session.get("last_user_message_at"))
        now = timeutil.now(self.scheduler.tz)
        if last_user is None:
            since = "很久"
        else:
            minutes = int((now - last_user).total_seconds() // 60)
            since = f"{minutes} 分钟前" if minutes < 120 else f"{minutes // 60} 小时前"

        import uuid

        wake_id = uuid.uuid4().hex
        session["last_wake_id"] = wake_id
        session["last_wake_recorded"] = False
        try:
            from astrbot.core.cron.events import CronMessageEvent
            from astrbot.core.platform.message_session import MessageSession

            message_session = MessageSession.from_str(umo)
            label = "对话节奏" if kind == jobs_mod.KIND_DIALOGUE else "临时提醒"
            message = f"[系统唤醒] 主动心智·{label}到点"
            if note:
                message += f"｜备注：{note}"
            event = CronMessageEvent(
                context=self.context,
                session=message_session,
                message=message,
                extras={
                    self.PMIND_WAKE_EXTRA: {
                        "kind": kind,
                        "topic": topic,
                        "since": since,
                        "note": note,
                        "temp_id": temp_id or "",
                        "wake_id": wake_id,
                    }
                },
                message_type=message_session.message_type,
            )
            event.is_wake = True
            event.is_at_or_wake_command = True
        except Exception as exc:  # pragma: no cover
            self.log.error(f"[proactive_mind] 构造唤醒事件失败 {umo}/{kind}: {exc}")
            return False

        try:
            queue = self.context.get_event_queue()
            await queue.put(event)
        except Exception as exc:  # pragma: no cover
            self.log.error(f"[proactive_mind] 投递唤醒事件失败 {umo}/{kind}: {exc}")
            return False
        session["last_delivery_at"] = timeutil.iso(now_delivery)
        # 立刻落盘：否则插件重载时 store.load() 会把这次投递记录回滚成旧值
        with contextlib.suppress(Exception):
            await self.store.save()
        self.log.info(
            f"[proactive_mind] 已把{kind}到点事件交给主 Agent（{umo}，topic={topic[:20]!r}）"
        )
        return True

    # ------------------------------------------------------------------
    # 决策入口
    # ------------------------------------------------------------------
    async def run_cycle(
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
    ) -> dict[str, Any] | None:
        if self.engine is None:
            await self.ensure_initialized()
        if self.engine is None:
            self.log.warning("[proactive_mind] 决策引擎未就绪，跳过本次决策")
            return None
        trace = await self.engine.run(
            umo,
            kind,
            trigger=trigger,
            temp_id=temp_id,
            bypass_limit=bypass_limit,
            spoke_before=spoke_before,
            record_only=record_only,
            next_at_hint=next_at_hint,
            main_reply_text=main_reply_text,
        )
        self._last_state_save = 0.0
        return trace

    # ------------------------------------------------------------------
    # 手动操作（面板/命令共用）
    # ------------------------------------------------------------------
    async def set_job_time(
        self, umo: str, kind: str, when: str, *, intent: str | None = None
    ) -> dict[str, Any]:
        if not await self.ensure_initialized() or self.engine is None:
            return {"ok": False, "error": "not_ready"}
        tz = self.scheduler.tz
        now = timeutil.now(tz)
        parsed = timeutil.parse_wake_time(when, reference=now, tz=tz)
        if parsed is None:
            return {"ok": False, "error": f"时间无法解析：{when}"}
        if parsed <= now:
            return {"ok": False, "error": "时间必须晚于现在"}
        session = self.store.session(umo)
        jobs = jobs_mod.ensure_jobs(session)
        if kind == jobs_mod.KIND_DIALOGUE:
            jobs["dialogue"]["cancelled"] = False
            target = self.engine._schedule_dialogue(
                umo, timeutil.iso(parsed), {"errors": [], "warnings": []}, now=now, bypass_limit=True
            )
        elif kind == jobs_mod.KIND_HOBBY:
            jobs["hobby"]["cancelled"] = False
            target = self.engine._schedule_hobby(
                umo,
                timeutil.iso(parsed),
                {"errors": [], "warnings": []},
                now=now,
                intent=intent,
                bypass_limit=True,
            )
        else:
            item = await self.schedule_explicit(umo, parsed, intent or "面板新增的临时提醒", created_by="panel")
            target = timeutil.from_iso(item["next_at"]) if item else None
        if target is None:
            return {"ok": False, "error": "安排失败"}
        await self.store.save()
        return {"ok": True, "next_at": timeutil.iso(target), "human": timeutil.human(target)}

    async def set_temp_time(self, umo: str, temp_id: str, when: str) -> dict[str, Any]:
        """改某一条临时提醒的时间（每条 temp 各有一份时间）。"""
        if not await self.ensure_initialized():
            return {"ok": False, "error": "not_ready"}
        tz = self.scheduler.tz
        now = timeutil.now(tz)
        parsed = timeutil.parse_wake_time(when, reference=now, tz=tz)
        if parsed is None:
            return {"ok": False, "error": f"时间无法解析：{when}"}
        if parsed <= now:
            return {"ok": False, "error": "时间必须晚于现在"}
        session = self.store.session(umo)
        jobs = jobs_mod.ensure_jobs(session)
        item = jobs_mod.find_temp(jobs, temp_id)
        if item is None:
            return {"ok": False, "error": "找不到这条临时提醒（可能已经触发）"}
        try:
            actual = self.scheduler.set_wake(
                umo, parsed, kind=jobs_mod.KIND_TEMP, temp_id=temp_id
            )
        except Exception as exc:  # pragma: no cover
            return {"ok": False, "error": f"重建作业失败：{exc}"}
        item["next_at"] = timeutil.iso(actual)
        item["updated_at"] = timeutil.iso(now)
        await self.store.save()
        return {"ok": True, "temp_id": temp_id, "next_at": timeutil.iso(actual), "human": timeutil.human(actual)}

    async def cancel_job(self, umo: str, kind: str | None, temp_id: str | None = None) -> dict[str, Any]:
        if not await self.ensure_initialized():
            return {"ok": False, "error": "not_ready"}
        session = self.store.session(umo)
        jobs = jobs_mod.ensure_jobs(session)
        if kind is None:
            self.scheduler.cancel(umo, kind=None)
            jobs["dialogue"]["cancelled"] = True
            jobs["hobby"]["cancelled"] = True
            jobs["dialogue"]["next_at"] = None
            jobs["hobby"]["next_at"] = None
            jobs["temp"] = []
            session["next_wake_at"] = None
        elif kind == jobs_mod.KIND_TEMP:
            self.scheduler.cancel(umo, kind=jobs_mod.KIND_TEMP, temp_id=temp_id)
            jobs["temp"] = [
                item
                for item in jobs["temp"]
                if not (temp_id is None or item.get("id") == temp_id)
            ]
        else:
            self.scheduler.cancel(umo, kind=kind)
            jobs[kind]["cancelled"] = True
            jobs[kind]["next_at"] = None
            if kind == jobs_mod.KIND_DIALOGUE:
                session["next_wake_at"] = None
        session["last_error"] = "cancelled_by_user"
        await self.store.save()
        return {"ok": True}

    # ------------------------------------------------------------------
    # 配置保存
    # ------------------------------------------------------------------
    async def save_settings(self) -> bool:
        saver = getattr(self.config, "save_config_async", None)
        try:
            if callable(saver):
                await saver()
                return True
            sync_saver = getattr(self.config, "save_config", None)
            if callable(sync_saver):
                await asyncio.to_thread(sync_saver)
                return True
        except Exception as exc:  # noqa: BLE001
            self.log.error(f"[proactive_mind] 保存插件配置失败: {exc}")
        return False

    async def _save_state(self) -> None:
        import time as _time

        now = _time.monotonic()
        if now - self._last_state_save < 1.5:
            return
        self._last_state_save = now
        with contextlib.suppress(Exception):
            await self.store.save()

    # ------------------------------------------------------------------
    # 管理命令
    # ------------------------------------------------------------------
    @filter.command_group("mind")
    def mind(self):
        """主动心智管理命令组。"""
        pass

    @permission_type(PermissionType.ADMIN)
    @mind.command("status")
    async def mind_status(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """查看三类作业与本次决策概况。"""
        if not await self.ensure_initialized():
            yield event.plain_result("主动心智还没初始化完成，稍后再试。")
            return
        umo = event.unified_msg_origin
        session = self.store.session(umo)
        jobs = jobs_mod.ensure_jobs(session)
        now = timeutil.now(self.scheduler.tz)

        def line(kind: str) -> str:
            data = jobs.get(kind) or {}
            if kind == jobs_mod.KIND_TEMP:
                items = jobs_mod.sort_temp(jobs)
                if not items:
                    return "临时：无"
                return "临时：" + "、".join(
                    f"{timeutil.human(timeutil.from_iso(i.get('next_at')))}「{(i.get('note') or '')[:12]}」"
                    for i in items[:3]
                )
            when = timeutil.from_iso(data.get("next_at")) if data.get("next_at") else None
            state = "已取消" if data.get("cancelled") else (timeutil.human(when) if when else "未安排")
            return f"{jobs_mod.KIND_LABELS[kind]}：{state}"

        cap = self.settings.max_consecutive_wakes
        cap_text = "无限" if cap == -1 else f"{cap} 次"
        lines = [
            "【主动心智】",
            f"启用：{'是' if self.settings.enabled else '否'}　版本：{self.version}",
            f"流水：{self.settings.flow_source}（对话 {self.settings.flow_message_count} 条）"
            f"　爱好：记忆 {self.settings.hobby_memory_count} 条 + 流水 {self.settings.hobby_flow_count} 条",
            line(jobs_mod.KIND_DIALOGUE),
            line(jobs_mod.KIND_HOBBY),
            line(jobs_mod.KIND_TEMP),
            f"连续触发：{int(session.get('silent_wake_count') or 0)} / 上限 {cap_text}",
            f"记忆写入：{self.settings.memory_mode}　延后：{self.settings.wake_delay_minutes} 分钟",
        ]
        if jobs["hobby"].get("intent"):
            lines.append(f"爱好下轮计划：{str(jobs['hobby']['intent'])[:120]}")
        last_trace = self.store.traces(1)
        if last_trace:
            trace = last_trace[0]
            stats = trace.get("context_stats") or {}
            lines.append(
                f"上次决策：{trace.get('kind')}/{trace.get('trigger')} "
                f"act={trace.get('act')} 请求{stats.get('requests', '?')}次"
                f"（人格{stats.get('persona_units', '?')}/流水{stats.get('flow_units', '?')}）"
            )
        yield event.plain_result("\n".join(lines))

    @permission_type(PermissionType.ADMIN)
    @mind.command("dialogue")
    async def mind_dialogue(
        self, event: AstrMessageEvent, when: str = ""
    ) -> AsyncGenerator[MessageEventResult, None]:
        """设置对话节奏的下次时间：/mind dialogue +30m。"""
        if not when:
            yield event.plain_result("用法：/mind dialogue +30m")
            return
        result = await self.set_job_time(event.unified_msg_origin, jobs_mod.KIND_DIALOGUE, when)
        yield event.plain_result(
            f"对话节奏已设为：{result.get('human')}" if result.get("ok") else f"设置失败：{result.get('error')}"
        )

    @permission_type(PermissionType.ADMIN)
    @mind.command("hobby")
    async def mind_hobby(
        self, event: AstrMessageEvent, when: str = "", intent: str = ""
    ) -> AsyncGenerator[MessageEventResult, None]:
        """设置爱好节奏与（可选）下轮计划：/mind hobby +3h 去翻翻收藏夹。"""
        if not when:
            yield event.plain_result("用法：/mind hobby +3h [下轮想做什么]")
            return
        result = await self.set_job_time(
            event.unified_msg_origin, jobs_mod.KIND_HOBBY, when, intent=intent or None
        )
        yield event.plain_result(
            f"爱好节奏已设为：{result.get('human')}" if result.get("ok") else f"设置失败：{result.get('error')}"
        )

    @permission_type(PermissionType.ADMIN)
    @mind.command("temp")
    async def mind_temp(
        self, event: AstrMessageEvent, when: str = "", note: str = ""
    ) -> AsyncGenerator[MessageEventResult, None]:
        """新增临时提醒：/mind temp +10m 提醒他喝水。"""
        if not when:
            yield event.plain_result("用法：/mind temp +10m 提醒内容")
            return
        umo = event.unified_msg_origin
        tz = self.scheduler.tz
        parsed = timeutil.parse_wake_time(when, reference=timeutil.now(tz), tz=tz)
        if parsed is None:
            yield event.plain_result("时间格式无法解析。")
            return
        item = await self.schedule_explicit(umo, parsed, note or "临时提醒", created_by="command")
        if not item:
            yield event.plain_result("创建失败（可能已达临时作业上限）。")
            return
        await self.store.save()
        yield event.plain_result(f"已加临时提醒：{timeutil.human(timeutil.from_iso(item['next_at']))}")

    @permission_type(PermissionType.ADMIN)
    @mind.command("cancel")
    async def mind_cancel(
        self, event: AstrMessageEvent, target: str = "all"
    ) -> AsyncGenerator[MessageEventResult, None]:
        """取消：/mind cancel [dialogue|hobby|temp|temp:<id>|all]。"""
        umo = event.unified_msg_origin
        target = (target or "all").strip().lower()
        if target in {"all", ""}:
            await self.cancel_job(umo, None)
            yield event.plain_result("已取消该会话的全部作业（对话/爱好/临时）。")
            return
        if target.startswith("temp"):
            temp_id = target.split(":", 1)[1] if ":" in target else None
            await self.cancel_job(umo, jobs_mod.KIND_TEMP, temp_id)
            yield event.plain_result("已取消临时提醒。")
            return
        if target in {jobs_mod.KIND_DIALOGUE, jobs_mod.KIND_HOBBY}:
            await self.cancel_job(umo, target)
            yield event.plain_result(f"已取消{jobs_mod.KIND_LABELS[target]}节奏。")
            return
        yield event.plain_result("用法：/mind cancel [dialogue|hobby|temp[:id]|all]")

    @permission_type(PermissionType.ADMIN)
    @mind.command("run")
    async def mind_run(
        self, event: AstrMessageEvent, kind: str = "dialogue"
    ) -> AsyncGenerator[MessageEventResult, None]:
        """立刻跑一次：/mind run [dialogue|hobby|temp]。"""
        kind = (kind or "dialogue").strip().lower()
        if kind not in jobs_mod.KINDS:
            yield event.plain_result("用法：/mind run [dialogue|hobby|temp]")
            return
        umo = event.unified_msg_origin
        yield event.plain_result(f"开始跑一次{kind}周期，结果会写进轨迹与记忆。")
        self._create_task(self.run_cycle(umo, kind=kind, trigger="manual", bypass_limit=True))

    @permission_type(PermissionType.ADMIN)
    @mind.command("count")
    async def mind_count(
        self, event: AstrMessageEvent, action: str = "show"
    ) -> AsyncGenerator[MessageEventResult, None]:
        """查看或清零连续触发计数：/mind count reset。"""
        umo = event.unified_msg_origin
        if action.strip().lower() in {"reset", "清零", "clear"}:
            self.store.set(umo, silent_wake_count=0, last_error=None)
            await self.store.save()
            yield event.plain_result("连续触发计数已清零。")
            return
        yield event.plain_result(
            f"当前连续触发次数：{int(self.store.session(umo).get('silent_wake_count') or 0)}"
        )

    @permission_type(PermissionType.ADMIN)
    @mind.command("tools")
    async def mind_tools(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """列出模型可选的工具。"""
        from .core import toolbox

        catalog = toolbox.build_catalog(self.context, self.settings, self.log)
        hobby_catalog = toolbox.build_catalog(
            self.context, self.settings, self.log, allowed=self.settings.hobby_tool_whitelist
        )
        names = "\n".join(f"- {item['name']}" for item in catalog)
        hobby_names = "、".join(item["name"] for item in hobby_catalog) or "（空）"
        yield event.plain_result(
            f"对话/临时可用工具（{len(catalog)}）：\n{names}\n\n爱好轮工具：{hobby_names}"
        )

    @permission_type(PermissionType.ADMIN)
    @mind.command("on")
    async def mind_on(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """启用主动心智。"""
        self.settings.update({"basic.enabled": True})
        await self.save_settings()
        await self.ensure_initialized()
        yield event.plain_result("主动心智已启用。")

    @permission_type(PermissionType.ADMIN)
    @mind.command("off")
    async def mind_off(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """停用主动心智。"""
        self.settings.update({"basic.enabled": False})
        await self.save_settings()
        for umo in list(self._debounce):
            task = self._debounce.get(umo)
            if task is not None and not task.done():
                task.cancel()
        await self.cancel_job(event.unified_msg_origin, None)
        yield event.plain_result("主动心智已停用，全部作业已取消。")

    # ------------------------------------------------------------------
    # 收尾
    # ------------------------------------------------------------------
    async def terminate(self) -> None:
        self.log.info("[proactive_mind] 正在停止主动心智...")
        for task in list(self._debounce.values()):
            if not task.done():
                task.cancel()
        self._debounce.clear()
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        with contextlib.suppress(Exception):
            await self.scheduler.shutdown()
        with contextlib.suppress(Exception):
            await self.store.save()
        self.log.info("[proactive_mind] 主动心智已停止。")
