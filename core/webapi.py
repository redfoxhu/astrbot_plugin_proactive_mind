"""插件自定义页面的 Web API。

路由统一注册在 ``/<plugin_name>/page/...``，前端通过
``window.AstrBotPluginView.apiGet/apiPost("page/...")`` 访问。
返回体统一为 ``{"status": "ok", "data": ...}`` / ``{"status": "error", "message": ...}``。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from . import flow as flow_mod
from . import llm, timeutil, toolbox

LOGGER_NAME = "astrbot"

CONFIG_PATCH_WHITELIST = {
    "basic.enabled",
    "basic.trigger_on_user_message",
    "basic.user_message_mode",
    "flow.flow_source",
    "flow.flow_message_count",
    "flow.follow_livingmemory_window",
    "wake.wake_delay_minutes",
    "wake.default_interval_minutes",
    "wake.min_interval_minutes",
    "wake.max_interval_minutes",
    "mind.max_tool_calls",
    "mind.tool_whitelist",
    "mind.tool_blacklist",
    "limit.max_consecutive_wakes",
    "limit.count_only_when_message_sent",
    "message.allow_empty_message",
    "echo.persist_proactive_to_platform_history",
    "echo.persist_proactive_to_livingmemory",
    "echo.dedupe_echo_in_flow",
}


def ok(data: Any = None) -> dict[str, Any]:
    """成功响应。"""
    return {"status": "ok", "data": data if data is not None else {}}


def fail(message: str, code: str | None = None) -> dict[str, Any]:
    """失败响应。"""
    payload: dict[str, Any] = {"status": "error", "message": message}
    if code:
        payload["code"] = code
    return payload


async def _read_payload() -> dict[str, Any]:
    """读取请求体 / query 参数（两者都兼容）。"""
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
    except Exception as exc:  # pragma: no cover - 宿主不可用
        logging.getLogger(LOGGER_NAME).debug(
            f"[proactive_mind] 读取请求体失败: {exc}"
        )
    return payload


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
        """返回插件状态、会话作业与最近轨迹。"""
        try:
            payload = await self._build_payload()
            return ok(payload)
        except Exception as exc:  # noqa: BLE001
            self.log.error(f"[proactive_mind] 面板状态读取失败: {exc}", exc_info=True)
            return fail(f"读取状态失败: {exc}")

    async def get_traces(self) -> dict[str, Any]:
        """返回最近决策轨迹。"""
        try:
            params = await _read_payload()
            limit = params.get("limit")
            try:
                limit_int = int(limit) if limit else 20
            except (TypeError, ValueError):
                limit_int = 20
            store = getattr(self.plugin, "store", None)
            traces = store.traces(limit_int) if store is not None else []
            return ok({"traces": traces})
        except Exception as exc:  # noqa: BLE001
            return fail(f"读取轨迹失败: {exc}")

    async def get_tools(self) -> dict[str, Any]:
        """返回当前可用工具清单（供排查）。"""
        try:
            settings = self.plugin.settings
            catalog = toolbox.build_catalog(self.plugin.context, settings, self.log)
            return ok({"tools": catalog, "count": len(catalog)})
        except Exception as exc:  # noqa: BLE001
            return fail(f"读取工具失败: {exc}")

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    async def post_wake_set(self) -> dict[str, Any]:
        """手动设置某会话的下次唤醒时间。"""
        params = await _read_payload()
        umo = str(params.get("umo") or "").strip()
        raw_when = params.get("wake_at") or params.get("when")
        if not umo:
            return fail("缺少 umo")
        if not raw_when:
            return fail("缺少 wake_at")
        return await self._set_wake(umo, str(raw_when))

    async def post_wake_cancel(self) -> dict[str, Any]:
        """取消某会话的唤醒作业，并停止继续自动创建。"""
        params = await _read_payload()
        umo = str(params.get("umo") or "").strip()
        if not umo:
            return fail("缺少 umo")
        scheduler = self.plugin.scheduler
        store = self.plugin.store
        scheduler.cancel(umo)
        store.set(umo, next_wake_at=None, last_error="cancelled_by_user")
        await store.save()
        return ok({"umo": umo, "cancelled": True})

    async def post_wake_delay(self) -> dict[str, Any]:
        """设置/清除某会话的延后分钟数覆盖。"""
        params = await _read_payload()
        umo = str(params.get("umo") or "").strip()
        if not umo:
            return fail("缺少 umo")
        raw = params.get("minutes")
        if raw in (None, "", "null", "default"):
            minutes: int | None = None
        else:
            try:
                minutes = max(0, min(1440, int(float(raw))))
            except (TypeError, ValueError):
                return fail("minutes 必须是数字")
        store = self.plugin.store
        store.set(umo, delay_override_minutes=minutes)
        await store.save()
        return ok({"umo": umo, "delay_override_minutes": minutes})

    async def post_run_now(self) -> dict[str, Any]:
        """立刻跑一次完整决策。"""
        params = await _read_payload()
        umo = str(params.get("umo") or "").strip()
        if not umo:
            return fail("缺少 umo")
        if getattr(self.plugin, "engine", None) is None:
            return fail("插件尚未就绪", code="not_ready")
        asyncio.create_task(
            self.plugin.run_cycle(umo, trigger="page", bypass_limit=True)
        )
        return ok({"umo": umo, "started": True})

    async def post_count_reset(self) -> dict[str, Any]:
        """把连续未回话计数清零。"""
        params = await _read_payload()
        umo = str(params.get("umo") or "").strip()
        if not umo:
            return fail("缺少 umo")
        store = self.plugin.store
        store.set(umo, silent_wake_count=0, last_error=None)
        await store.save()
        return ok({"umo": umo, "silent_wake_count": 0})

    async def post_config_patch(self) -> dict[str, Any]:
        """按白名单修改配置并保存。"""
        params = await _read_payload()
        patch = params.get("patch")
        if not isinstance(patch, dict):
            patch = {
                key: value
                for key, value in params.items()
                if key not in {"patch"} and isinstance(key, str) and "." in key
            }
        filtered = {
            key: value for key, value in (patch or {}).items() if key in CONFIG_PATCH_WHITELIST
        }
        if not filtered:
            return fail("没有可修改的配置项")
        applied = self.plugin.settings.update(filtered)
        saved = await self.plugin.save_settings()
        return ok({"applied": applied, "saved": saved})

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    async def _set_wake(self, umo: str, raw_when: str) -> dict[str, Any]:
        tz = self.plugin.scheduler.tz
        now = timeutil.now(tz)
        parsed = timeutil.parse_wake_time(raw_when, reference=now, tz=tz)
        if parsed is None:
            return fail(f"时间格式无法解析：{raw_when}")
        if parsed <= now:
            return fail("时间必须晚于现在")
        scheduler = self.plugin.scheduler
        store = self.plugin.store
        try:
            actual = scheduler.set_wake(umo, parsed)
        except Exception as exc:  # noqa: BLE001
            return fail(f"创建唤醒作业失败: {exc}")
        store.set(
            umo,
            next_wake_at=timeutil.iso(actual),
            last_error=None,
            silent_wake_count=0,
            user_replied_since_last_wake=True,
        )
        await store.save()
        return ok(
            {
                "umo": umo,
                "next_wake_at": timeutil.iso(actual),
                "next_wake_human": timeutil.human(actual),
            }
        )

    async def _build_payload(self) -> dict[str, Any]:
        plugin = self.plugin
        settings = plugin.settings
        store = plugin.store
        scheduler = plugin.scheduler
        tz = scheduler.tz
        now = timeutil.now(tz)

        livingmemory_plugin = flow_mod.find_livingmemory(settings, self.log)
        window, window_source = flow_mod.livingmemory_window(
            livingmemory_plugin, settings
        )
        catalog = toolbox.build_catalog(plugin.context, settings, self.log)
        provider_label = await self._provider_label()

        sessions: list[dict[str, Any]] = []
        for umo, session in (store.all_sessions() or {}).items():
            next_wake = timeutil.from_iso(session.get("next_wake_at"))
            next_run = scheduler.next_run(umo) if scheduler is not None else None
            if next_run is not None:
                next_wake = next_run
            countdown = None
            if next_wake is not None:
                countdown = max(0, int((next_wake - now).total_seconds()))
            sessions.append(
                {
                    "umo": umo,
                    "platform": session.get("platform")
                    or (umo.split(":", 1)[0] if ":" in umo else None),
                    "next_wake_at": timeutil.iso(next_wake),
                    "next_wake_human": timeutil.human(next_wake),
                    "countdown_seconds": countdown,
                    "silent_wake_count": int(session.get("silent_wake_count") or 0),
                    "user_replied_since_last_wake": bool(
                        session.get("user_replied_since_last_wake", True)
                    ),
                    "delay_override_minutes": session.get("delay_override_minutes"),
                    "last_sent_message": session.get("last_sent_message"),
                    "last_sent_at": session.get("last_sent_at"),
                    "last_decision_at": session.get("last_decision_at"),
                    "last_wake_at": session.get("last_wake_at"),
                    "last_error": session.get("last_error"),
                    "last_echo": session.get("last_echo") or {},
                    "disabled": bool(session.get("disabled")),
                    "last_trace_id": session.get("last_trace_id"),
                }
            )
        sessions.sort(
            key=lambda item: (
                item.get("next_wake_at") is None,
                item.get("next_wake_at") or "",
            )
        )

        return {
            "plugin": {
                "name": getattr(plugin, "name", "astrbot_plugin_proactive_mind"),
                "version": getattr(plugin, "version", "0.1.0"),
                "enabled": settings.enabled,
                "ready": getattr(plugin, "engine", None) is not None,
                "server_time": timeutil.iso(now),
                "server_time_human": timeutil.human(now),
                "timezone": str(tz),
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
                "pending_jobs": len(
                    [item for item in sessions if item.get("next_wake_at")]
                ),
            },
            "settings": {
                "wake_delay_minutes": settings.wake_delay_minutes,
                "default_interval_minutes": settings.default_interval_minutes,
                "min_interval_minutes": settings.min_interval_minutes,
                "max_interval_minutes": settings.max_interval_minutes,
                "max_consecutive_wakes": settings.max_consecutive_wakes,
                "count_only_when_message_sent": settings.count_only_when_message_sent,
                "user_message_mode": settings.user_message_mode,
                "trigger_on_user_message": settings.trigger_on_user_message,
                "allow_empty_message": settings.allow_empty_message,
                "persist_platform": settings.persist_platform,
                "persist_livingmemory": settings.persist_livingmemory,
                "write_memory": settings.write_memory,
            },
            "sessions": sessions,
            "traces": store.traces(20),
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
            meta = provider.meta()
            provider_id = str(getattr(meta, "id", "") or "")
        except Exception:
            provider_id = ""
        model = ""
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
    ("wake/set", "post_wake_set", ["POST"], "proactive-mind set wake time"),
    ("wake/cancel", "post_wake_cancel", ["POST"], "proactive-mind cancel wake"),
    ("wake/delay", "post_wake_delay", ["POST"], "proactive-mind delay override"),
    ("run-now", "post_run_now", ["POST"], "proactive-mind run decision now"),
    ("count/reset", "post_count_reset", ["POST"], "proactive-mind reset counter"),
    ("config/patch", "post_config_patch", ["POST"], "proactive-mind patch config"),
)
