"""唤醒作业调度器。

设计要点：
1. 每个会话只保留**一个**唤醒作业，新的时间点直接替换旧的；
2. 作业是「一次性到点触发」（APScheduler DateTrigger），触发后由决策流程决定下一次时间；
3. 时间点持久化在 StateStore 里，插件重载/重启后可恢复；
4. 不占用 AstrBot 内置 cron 管理器（那套会再跑一遍主 Agent，与三段式决策冲突）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from . import timeutil

LOGGER_NAME = "astrbot"
JOB_PREFIX = "pmind:"


class WakeScheduler:
    """按会话管理一次性唤醒作业。"""

    def __init__(
        self,
        *,
        on_fire: Callable[[str], Awaitable[Any] | Any],
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
            except Exception as exc:  # pragma: no cover - 取决于 APScheduler 版本
                self.log.debug(f"[proactive_mind] 调度器关闭异常: {exc}")
        self._scheduler = None
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    # ------------------------------------------------------------------
    # 作业操作
    # ------------------------------------------------------------------
    @staticmethod
    def job_id(umo: str) -> str:
        return f"{JOB_PREFIX}{umo}"

    def set_wake(self, umo: str, when: datetime) -> datetime:
        """设置/替换某会话的唤醒时间点。"""
        if when.tzinfo is None:
            when = when.replace(tzinfo=self.tz)
        if not self._started:
            self.start()
        job_id = self.job_id(umo)
        try:
            self._scheduler.add_job(
                self._trigger,
                "date",
                run_date=when,
                args=[umo],
                id=job_id,
                name=f"proactive_mind:{umo}",
                replace_existing=True,
                misfire_grace_time=600,
                coalesce=True,
                max_instances=1,
            )
        except Exception as exc:
            self.log.error(f"[proactive_mind] 创建唤醒作业失败 {umo}: {exc}")
            raise
        return when

    def cancel(self, umo: str) -> bool:
        """取消某会话的唤醒作业，返回是否确实删除了一个作业。"""
        if self._scheduler is None:
            return False
        job_id = self.job_id(umo)
        removed = False
        try:
            if self._scheduler.get_job(job_id) is not None:
                self._scheduler.remove_job(job_id)
                removed = True
        except Exception as exc:
            self.log.debug(f"[proactive_mind] 取消唤醒作业失败 {umo}: {exc}")
        return removed

    def next_run(self, umo: str) -> datetime | None:
        """读取某会话作业的下次触发时间（UTC 或本地时区，取决于 APScheduler）。"""
        if self._scheduler is None:
            return None
        try:
            job = self._scheduler.get_job(self.job_id(umo))
        except Exception:
            return None
        if job is None or job.next_run_time is None:
            return None
        return job.next_run_time

    def pending(self) -> dict[str, datetime | None]:
        """列出全部作业的触发时间。"""
        result: dict[str, datetime | None] = {}
        if self._scheduler is None:
            return result
        try:
            import inspect

            get_jobs = self._scheduler.get_jobs
            jobs = get_jobs()
            if inspect.isawaitable(jobs):  # pragma: no cover - 兼容未来版本
                jobs = []
        except Exception:
            return result
        for job in jobs or []:
            job_id = str(getattr(job, "id", ""))
            if not job_id.startswith(JOB_PREFIX):
                continue
            result[job_id[len(JOB_PREFIX) :]] = getattr(job, "next_run_time", None)
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
    ) -> dict[str, str]:
        """按持久化状态恢复作业，返回 ``{umo: 动作}`` 说明。"""
        actions: dict[str, str] = {}
        now = timeutil.now(self.tz)
        for umo, session in (sessions or {}).items():
            if not isinstance(session, dict) or session.get("disabled"):
                continue
            raw = session.get("next_wake_at")
            when = timeutil.from_iso(raw)
            if when is None:
                if raw:
                    actions[umo] = "invalid"
                continue
            if when > now:
                self.set_wake(umo, when)
                actions[umo] = "restored"
                continue
            delay = session.get("delay_override_minutes")
            if delay is None:
                delay = 0
            try:
                delay = int(delay)
            except (TypeError, ValueError):
                delay = 0
            if catch_up:
                target = now + timedelta(seconds=10 + max(0, delay) * 60)
                self.set_wake(umo, target)
                actions[umo] = "catch_up"
            else:
                target = now + timedelta(
                    minutes=max(int(default_interval_minutes), int(min_interval_minutes))
                )
                self.set_wake(umo, target)
                actions[umo] = "postponed"
        return actions

    # ------------------------------------------------------------------
    # 触发
    # ------------------------------------------------------------------
    async def _trigger(self, umo: str) -> None:
        """APScheduler 回调（协程，保证在事件循环内执行）。

        必须是 async：AsyncIOScheduler 对同步函数会退化成线程池执行，
        那时 ``asyncio.create_task`` 没有事件循环可用。
        """
        task = asyncio.create_task(self._run(umo))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, umo: str) -> None:
        started = time.monotonic()
        try:
            result = self._on_fire(umo)
            if asyncio.iscoroutine(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - 兜底
            self.log.error(
                f"[proactive_mind] 唤醒任务执行异常 {umo}: {exc}", exc_info=True
            )
        finally:
            self.log.debug(
                f"[proactive_mind] 唤醒任务结束 {umo} 用时 {time.monotonic() - started:.1f}s"
            )
