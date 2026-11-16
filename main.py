"""主动心智 Proactive Mind —— 插件入口。

职责：
* 装配状态仓库、唤醒调度器、决策引擎、面板 API；
* 接住 AstrBot 的事件：用户消息（回复后触发判断）、插件加载（恢复唤醒作业）；
* 提供 ``/mind`` 管理命令。

真正的"想不想说、说什么、下次何时醒"都在 ``core/decide.py``。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import astrbot.api.star as star
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.event.filter import PermissionType, permission_type
from astrbot.core.config.astrbot_config import AstrBotConfig

from .core import flow as flow_mod
from .core import timeutil, webapi
from .core.decide import DecisionEngine
from .core.scheduler import WakeScheduler
from .core.settings import Settings
from .core.state import StateStore

PLUGIN_NAME = "astrbot_plugin_proactive_mind"
PLUGIN_VERSION = "0.1.0"


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

        base_dir = Path(str(star.StarTools.get_data_dir(PLUGIN_NAME)))
        self.data_dir = base_dir
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

        self._register_page_routes()
        self._create_task(self._bootstrap())

    # ------------------------------------------------------------------
    # 装配
    # ------------------------------------------------------------------
    def _register_page_routes(self) -> None:
        """注册面板 API 路由。"""
        for suffix, handler_name, methods, desc in webapi.ROUTES:
            handler = getattr(self.page_api, handler_name, None)
            if handler is None:
                self.log.warning(f"[proactive_mind] 面板路由处理函数缺失: {handler_name}")
                continue
            try:
                self.context.register_web_api(
                    f"/{PLUGIN_NAME}/page/{suffix}", handler, methods, desc
                )
            except Exception as exc:  # pragma: no cover - 宿主版本差异
                self.log.error(
                    f"[proactive_mind] 注册面板路由失败 {suffix}: {exc}"
                )

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
        """延迟一点再初始化，避免与平台加载竞争。"""
        await asyncio.sleep(1)
        await self.ensure_initialized()

    async def ensure_initialized(self) -> bool:
        """加载状态、启动调度器并恢复唤醒作业（幂等）。"""
        async with self._init_lock:
            if self._initialized:
                return True
            try:
                self.store.trace_keep = self.settings.trace_keep
                await self.store.load()
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
                    )
                    if actions:
                        self.log.info(
                            f"[proactive_mind] 已恢复 {len(actions)} 个唤醒作业: {actions}"
                        )
                    await self.store.save()
                self._initialized = True
                self.log.info(
                    f"[proactive_mind] 插件就绪（flow={self.settings.flow_source}, "
                    f"上限={self.settings.max_consecutive_wakes}, "
                    f"延后={self.settings.wake_delay_minutes} 分钟）"
                )
                return True
            except Exception as exc:  # noqa: BLE001
                self.log.error(f"[proactive_mind] 初始化失败: {exc}", exc_info=True)
                return False

    async def initialize(self) -> None:
        """AstrBot 生命周期钩子。"""
        await self.ensure_initialized()

    # ------------------------------------------------------------------
    # 事件入口
    # ------------------------------------------------------------------
    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        """平台加载完成后再次确保作业已恢复。"""
        await self.ensure_initialized()

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent, *_args) -> None:
        """机器人回复发出后：记录用户发言、并按需触发一次判断。"""
        try:
            await self._handle_after_sent(event)
        except Exception as exc:  # noqa: BLE001
            self.log.error(f"[proactive_mind] after_message_sent 处理失败: {exc}")

    async def _handle_after_sent(self, event: AstrMessageEvent) -> None:
        platform_name = ""
        with contextlib.suppress(Exception):
            platform_name = str(event.get_platform_name() or "")
        if platform_name == "cron":
            return
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            return
        if not await self.ensure_initialized():
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
        self._schedule_user_trigger(umo)

    def _cache_bot_identity(self, event: AstrMessageEvent, session: dict[str, Any]) -> None:
        """从事件里记录 Bot 自身 ID/昵称，供主动消息落库时使用。"""
        bot_id = None
        for getter in ("get_self_id",):
            func = getattr(event, getter, None)
            if callable(func):
                with contextlib.suppress(Exception):
                    bot_id = func()
                if bot_id:
                    break
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

    def _schedule_user_trigger(self, umo: str) -> None:
        """用户消息触发的判断（带防抖）。"""
        existing = self._debounce.get(umo)
        if existing is not None and not existing.done():
            existing.cancel()
        delay = float(
            max(0, self.settings.debounce_seconds + self.settings.post_reply_delay_seconds)
        )

        async def runner() -> None:
            try:
                await asyncio.sleep(delay)
                await self.run_cycle(umo, trigger="user_message")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.log.error(f"[proactive_mind] 用户消息触发的判断失败: {exc}")

        self._debounce[umo] = asyncio.create_task(runner())

    async def _on_wake(self, umo: str) -> None:
        """唤醒作业到点。"""
        if not await self.ensure_initialized():
            return
        await self.run_cycle(umo, trigger="cron")

    # ------------------------------------------------------------------
    # 决策入口
    # ------------------------------------------------------------------
    async def run_cycle(
        self,
        umo: str,
        trigger: str = "manual",
        *,
        mode: str | None = None,
        bypass_limit: bool = False,
    ) -> dict[str, Any] | None:
        """执行一次完整决策。"""
        if self.engine is None:
            await self.ensure_initialized()
        if self.engine is None:
            self.log.warning("[proactive_mind] 决策引擎未就绪，跳过本次决策")
            return None
        trace = await self.engine.run(
            umo, trigger, mode=mode, bypass_limit=bypass_limit
        )
        self._last_state_save = 0.0
        return trace

    # ------------------------------------------------------------------
    # 配置保存
    # ------------------------------------------------------------------
    async def save_settings(self) -> bool:
        """把内存里的配置写回磁盘。"""
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
        """节流落盘（高频事件下避免频繁写盘）。"""
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
        """查看主动心智状态。"""
        if not await self.ensure_initialized():
            yield event.plain_result("主动心智还没初始化完成，稍后再试。")
            return
        umo = event.unified_msg_origin
        session = self.store.session(umo)
        next_wake = self.scheduler.next_run(umo)
        next_wake_text = timeutil.human(next_wake) if next_wake else "未安排"
        cap = self.settings.max_consecutive_wakes
        cap_text = "无限" if cap == -1 else f"{cap} 次"
        window, window_source = flow_mod.livingmemory_window(
            flow_mod.find_livingmemory(self.settings, self.log), self.settings
        )
        lines = [
            "【主动心智】",
            f"启用：{'是' if self.settings.enabled else '否'}",
            f"流水来源：{self.settings.flow_source}（{window} 条，window={window_source}）",
            f"下次唤醒：{next_wake_text}",
            f"连续触发：{int(session.get('silent_wake_count') or 0)} / 上限 {cap_text}",
            f"延后：{self.settings.wake_delay_minutes} 分钟",
            f"落库：平台历史={'开' if self.settings.persist_platform else '关'}，"
            f"LivingMemory={'开' if self.settings.persist_livingmemory else '关'}",
            f"最近决定：{session.get('last_gate') or '（无）'}",
        ]
        if session.get("last_error"):
            lines.append(f"最近异常：{session['last_error']}")
        yield event.plain_result("\n".join(lines))

    @permission_type(PermissionType.ADMIN)
    @mind.command("wake")
    async def mind_wake(
        self, event: AstrMessageEvent, when: str = ""
    ) -> AsyncGenerator[MessageEventResult, None]:
        """手动指定下次唤醒时间，例如 /mind wake +30m。"""
        if not when:
            yield event.plain_result("用法：/mind wake +30m 或 /mind wake 2026-11-17T08:00:00+08:00")
            return
        if not await self.ensure_initialized():
            yield event.plain_result("主动心智还没初始化完成，稍后再试。")
            return
        result = await self.page_api._set_wake(event.unified_msg_origin, when)
        if result.get("status") == "ok":
            data = result.get("data") or {}
            yield event.plain_result(f"已安排下次唤醒：{data.get('next_wake_human')}")
        else:
            yield event.plain_result(f"设置失败：{result.get('message')}")

    @permission_type(PermissionType.ADMIN)
    @mind.command("cancel")
    async def mind_cancel(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """取消当前会话的唤醒作业。"""
        if not await self.ensure_initialized():
            yield event.plain_result("主动心智还没初始化完成，稍后再试。")
            return
        umo = event.unified_msg_origin
        self.scheduler.cancel(umo)
        self.store.set(umo, next_wake_at=None, last_error="cancelled_by_user")
        await self.store.save()
        yield event.plain_result("已取消当前会话的唤醒作业。")

    @permission_type(PermissionType.ADMIN)
    @mind.command("delay")
    async def mind_delay(
        self, event: AstrMessageEvent, minutes: str = ""
    ) -> AsyncGenerator[MessageEventResult, None]:
        """设置本会话的唤醒延后分钟数（不带参数则恢复默认）。"""
        if not await self.ensure_initialized():
            yield event.plain_result("主动心智还没初始化完成，稍后再试。")
            return
        umo = event.unified_msg_origin
        if not minutes:
            self.store.set(umo, delay_override_minutes=None)
            await self.store.save()
            yield event.plain_result(
                f"已恢复默认延后：{self.settings.wake_delay_minutes} 分钟"
            )
            return
        try:
            value = max(0, min(1440, int(float(minutes))))
        except (TypeError, ValueError):
            yield event.plain_result("用法：/mind delay 15（分钟）")
            return
        self.store.set(umo, delay_override_minutes=value)
        await self.store.save()
        yield event.plain_result(f"本会话唤醒将自动延后 {value} 分钟。")

    @permission_type(PermissionType.ADMIN)
    @mind.command("run")
    async def mind_run(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """立刻跑一次完整决策（调试用）。"""
        if not await self.ensure_initialized():
            yield event.plain_result("主动心智还没初始化完成，稍后再试。")
            return
        umo = event.unified_msg_origin
        yield event.plain_result("开始跑一次主动决策，结果会写进记忆与轨迹面板。")
        self._create_task(self.run_cycle(umo, trigger="manual", bypass_limit=True))

    @permission_type(PermissionType.ADMIN)
    @mind.command("count")
    async def mind_count(
        self, event: AstrMessageEvent, action: str = "show"
    ) -> AsyncGenerator[MessageEventResult, None]:
        """查看或清零连续触发计数：/mind count reset。"""
        if not await self.ensure_initialized():
            yield event.plain_result("主动心智还没初始化完成，稍后再试。")
            return
        umo = event.unified_msg_origin
        if action.strip().lower() in {"reset", "清零", "clear"}:
            self.store.set(umo, silent_wake_count=0, last_error=None)
            await self.store.save()
            yield event.plain_result("连续触发计数已清零。")
            return
        session = self.store.session(umo)
        yield event.plain_result(
            f"当前连续触发次数：{int(session.get('silent_wake_count') or 0)}"
        )

    @permission_type(PermissionType.ADMIN)
    @mind.command("tools")
    async def mind_tools(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """列出模型可选的工具。"""
        from .core import toolbox

        catalog = toolbox.build_catalog(self.context, self.settings, self.log)
        if not catalog:
            yield event.plain_result("当前没有可用工具。")
            return
        names = "\n".join(f"- {item['name']}" for item in catalog)
        yield event.plain_result(f"可用工具（{len(catalog)} 个）：\n{names}")

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
        """停用主动心智（保留下次唤醒记录，不再创建新作业）。"""
        self.settings.update({"basic.enabled": False})
        await self.save_settings()
        for umo in list(self._debounce):
            task = self._debounce.get(umo)
            if task is not None and not task.done():
                task.cancel()
        self.scheduler.cancel(event.unified_msg_origin)
        yield event.plain_result("主动心智已停用。")

    # ------------------------------------------------------------------
    # 收尾
    # ------------------------------------------------------------------
    async def terminate(self) -> None:
        """插件卸载 / AstrBot 退出。"""
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
