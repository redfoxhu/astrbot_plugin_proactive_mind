"""唤醒作业调度器（三类作业共用一套 APScheduler）。

作业 id：
* ``pmind:<umo>:dialogue``
* ``pmind:<umo>:hobby``
* ``pmind:<umo>:temp:<temp_id>``

约定：
1. 每类作业都是「一次性到点触发」，触发后由决策流程决定是否重建；
2. 时间点持久化在 StateStore 的 ``jobs`` 里，插件重载/重启后可恢复；
3. dialogue/hobby 恒 1 个（同 id 覆盖），temp 可多个（各 id 独立）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from . import jobs as jobs_mod
from . import timeutil

LOGGER_NAME = "astrbot"
JOB_PREFIX = "pmind:"


class WakeScheduler:
    """按会话+类型管理一次性唤醒作业。"""

    def __init__(
        self,
        *,
        on_fire: Callable[..., Awaitable[Any] | Any],
        timezone_name: str = "",
        logger: logging.Logger | None = None,
    ) -> None:
        self._on_fire = on_fire
        self.tz = timeutil.resolve_tz(timezone_name)
        self.log = logger or logging.getLogger(LOGGER_NAME)
        self._scheduler = None
        self._started = False
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动底层调度器（幂等）。"""
        if self._started:
            return
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        self._scheduler = AsyncIOScheduler(timezone=self.tz)
        self._scheduler.start()
        self._started = True

    async def shutdown(self) -> None:
        """停止调度器并取消在飞任务。"""
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._scheduler is not None and self._started:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception as exc:  # pragma: no cover
                self.log.debug(f"[proactive_mind] 调度器关闭异常: {exc}")
        self._scheduler = None
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    # ------------------------------------------------------------------
    # 作业 id
    # ------------------------------------------------------------------
    @staticmethod
    def job_id(umo: str, kind: str = jobs_mod.KIND_DIALOGUE, temp_id: str | None = None) -> str:
        base = f"{JOB_PREFIX}{umo}:{kind}"
        if kind == jobs_mod.KIND_TEMP and temp_id:
            return f"{base}:{temp_id}"
        return base

    # ------------------------------------------------------------------
    # 作业操作
    # ------------------------------------------------------------------
    def set_wake(
        self,
        umo: str,
        when: datetime,
        *,
        kind: str = jobs_mod.KIND_DIALOGUE,
        temp_id: str | None = None,
    ) -> datetime:
        """设置/替换某会话某类作业的触发时间。"""
        if when.tzinfo is None:
            when = when.replace(tzinfo=self.tz)
        if not self._started:
            self.start()
        job_id = self.job_id(umo, kind, temp_id)
        try:
            self._scheduler.add_job(
                self._trigger,
                "date",
                run_date=when,
                args=[umo, kind, temp_id],
                id=job_id,
                name=f"proactive_mind:{kind}:{umo}",
                replace_existing=True,
                misfire_grace_time=600,
                coalesce=True,
                max_instances=1,
            )
        except Exception as exc:
            self.log.error(f"[proactive_mind] 创建唤醒作业失败 {job_id}: {exc}")
            raise
        return when

    def cancel(
        self,
        umo: str,
        *,
        kind: str | None = jobs_mod.KIND_DIALOGUE,
        temp_id: str | None = None,
    ) -> bool:
        """取消作业；``kind=None`` 表示取消该会话全部作业。"""
        if self._scheduler is None:
            return False
        if kind is None:
            removed = False
            for each in jobs_mod.KINDS:
                for job in self._list_session_jobs(umo, each):
                    removed = self._remove(job.id) or removed
            return removed
        if kind == jobs_mod.KIND_TEMP and temp_id is None:
            removed = False
            for job in self._list_session_jobs(umo, kind):
                removed = self._remove(job.id) or removed
            return removed
        return self._remove(self.job_id(umo, kind, temp_id))

    def _list_session_jobs(self, umo: str, kind: str) -> list[Any]:
        prefix = f"{JOB_PREFIX}{umo}:{kind}"
        try:
            return [job for job in self._scheduler.get_jobs() if str(job.id).startswith(prefix)]
        except Exception:
            return []

    def _remove(self, job_id: str) -> bool:
        try:
            if self._scheduler.get_job(job_id) is not None:
                self._scheduler.remove_job(job_id)
                return True
        except Exception as exc:
            self.log.debug(f"[proactive_mind] 取消作业失败 {job_id}: {exc}")
        return False

    def next_run(
        self,
        umo: str,
        *,
        kind: str = jobs_mod.KIND_DIALOGUE,
        temp_id: str | None = None,
    ) -> datetime | None:
        """读取某作业的下次触发时间。"""
        if self._scheduler is None:
            return None
        try:
            job = self._scheduler.get_job(self.job_id(umo, kind, temp_id))
        except Exception:
            return None
        if job is None or job.next_run_time is None:
            return None
        return job.next_run_time

    def pending(self) -> dict[str, datetime | None]:
        """列出全部作业的触发时间（key 为作业 id）。"""
        result: dict[str, datetime | None] = {}
        if self._scheduler is None:
            return result
        try:
            jobs = self._scheduler.get_jobs()
        except Exception:
            return result
        for job in jobs or []:
            job_id = str(getattr(job, "id", ""))
            if job_id.startswith(JOB_PREFIX):
                result[job_id] = getattr(job, "next_run_time", None)
        return result

    # ------------------------------------------------------------------
    # 恢复
    # ------------------------------------------------------------------
    def restore(
        self,
        sessions: dict[str, dict[str, Any]],
        *,
        catch_up: bool,
        default_interval_minutes: int,
        min_interval_minutes: int,
        hobby_default_interval_minutes: int | None = None,
        hobby_min_interval_minutes: int | None = None,
    ) -> dict[str, str]:
        """按持久化的 jobs 恢复全部作业，返回 ``{"<umo>|<kind>": 动作}``。"""
        actions: dict[str, str] = {}
        now = timeutil.now(self.tz)
        hobby_default = hobby_default_interval_minutes or default_interval_minutes
        hobby_min = hobby_min_interval_minutes or min_interval_minutes

        def register(umo: str, kind: str, raw: Any, temp_id: str | None, *, fallback: int, min_minutes: int) -> None:
            key = f"{umo}|{kind}" + (f":{temp_id}" if temp_id else "")
            when = timeutil.from_iso(raw)
            if when is None:
                if raw:
                    actions[key] = "invalid"
                return
            if when > now:
                actions[key] = "restored"
            elif catch_up:
                when = now + timedelta(seconds=10)
                actions[key] = "catch_up"
            else:
                when = now + timedelta(minutes=max(1, int(fallback), int(min_minutes)))
                actions[key] = "postponed"
            try:
                self.set_wake(umo, when, kind=kind, temp_id=temp_id)
            except Exception as exc:  # pragma: no cover
                actions[key] = f"failed:{exc}"

        for umo, session in (sessions or {}).items():
            if not isinstance(session, dict) or session.get("disabled"):
                continue
            jobs = jobs_mod.ensure_jobs(session)

            dialogue = jobs.get("dialogue") or {}
            if not dialogue.get("cancelled") and dialogue.get("next_at"):
                register(
                    umo,
                    jobs_mod.KIND_DIALOGUE,
                    dialogue["next_at"],
                    None,
                    fallback=default_interval_minutes,
                    min_minutes=min_interval_minutes,
                )

            hobby = jobs.get("hobby") or {}
            if not hobby.get("cancelled") and hobby.get("next_at"):
                register(
                    umo,
                    jobs_mod.KIND_HOBBY,
                    hobby["next_at"],
                    None,
                    fallback=hobby_default,
                    min_minutes=hobby_min,
                )

            for item in jobs.get("temp") or []:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                if not item.get("next_at"):
                    continue
                register(
                    umo,
                    jobs_mod.KIND_TEMP,
                    item["next_at"],
                    str(item["id"]),
                    fallback=default_interval_minutes,
                    min_minutes=min_interval_minutes,
                )
        return actions

    # ------------------------------------------------------------------
    # 触发
    # ------------------------------------------------------------------
    async def _trigger(self, umo: str, kind: str, temp_id: str | None) -> None:
        """APScheduler 回调（协程，保证在事件循环内执行）。"""
        task = asyncio.create_task(self._run(umo, kind, temp_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, umo: str, kind: str, temp_id: str | None) -> None:
        started = time.monotonic()
        try:
            result = self._on_fire(umo, kind, temp_id)
            if asyncio.iscoroutine(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover
            self.log.error(
                f"[proactive_mind] 唤醒任务执行异常 {umo}/{kind}: {exc}", exc_info=True
            )
        finally:
            self.log.debug(
                f"[proactive_mind] 唤醒任务结束 {umo}/{kind} 用时 {time.monotonic() - started:.1f}s"
            )
