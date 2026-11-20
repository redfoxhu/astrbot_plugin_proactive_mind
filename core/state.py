"""每会话状态与决策轨迹的持久化。

状态文件：``<plugin_data>/state.json``，当前版本 **2**。
v2 引入三类唤醒作业 ``jobs.dialogue / jobs.hobby / jobs.temp``；
v1 的 ``next_wake_at`` / ``explicit_wake_at`` / ``pending_note`` 在加载时自动迁移。
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from . import jobs as jobs_mod
from . import timeutil

STATE_VERSION = 2

SESSION_DEFAULTS: dict[str, Any] = {
    # —— 兼容/镜像字段 ——
    "next_wake_at": None,
    "explicit_wake_at": None,
    "pending_note": None,
    "pending_note_at": None,
    # —— 节奏与计数 ——
    "delay_override_minutes": None,
    "silent_wake_count": 0,
    "user_replied_since_last_wake": True,
    "last_wake_at": None,
    # 交还主 Agent 后的记账/去重字段
    "wake_pending_at": None,
    "last_delivery_at": None,
    "last_wake_id": None,
    "last_wake_recorded": False,
    "recorded_for_delivery": None,
    "last_decision_at": None,
    "last_user_message_at": None,
    # —— 身份与发送记录 ——
    "bot_self_id": None,
    "bot_self_name": None,
    "last_sent_message": None,
    "last_sent_at": None,
    "last_echo": {},
    "last_trace_id": None,
    "last_error": None,
    "disabled": False,
    "platform": None,
    "created_at": None,
    # —— 三类作业 ——
    "jobs": jobs_mod.default_jobs,
}

MAX_TRACES_HARD_LIMIT = 500


class StateStore:
    """状态仓库（进程内单例，按 umo 分桶）。"""

    def __init__(self, path: str | Path, trace_keep: int = 30) -> None:
        self.path = Path(path)
        self.trace_keep = max(5, int(trace_keep))
        self._lock = asyncio.Lock()
        self._data: dict[str, Any] = {"version": STATE_VERSION, "sessions": {}, "traces": []}
        self._loaded = False
        self.last_reason: str | None = None

    # ------------------------------------------------------------------
    # 加载 / 落盘
    # ------------------------------------------------------------------
    async def load(self) -> None:
        """从磁盘加载状态；文件损坏时备份后重建；v1 自动迁移到 v2。"""
        async with self._lock:
            await asyncio.to_thread(self._load_sync)

    def _load_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._data = {"version": STATE_VERSION, "sessions": {}, "traces": []}
            self._loaded = True
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
        except Exception:
            try:
                backup = self.path.with_suffix(self.path.suffix + f".corrupt-{int(time.time())}")
                os.replace(self.path, backup)
                self.last_reason = f"corrupt_backup:{backup.name}"
            except OSError:
                pass
            self._data = {"version": STATE_VERSION, "sessions": {}, "traces": []}
            self._loaded = True
            return

        if not isinstance(data, dict):
            data = {}
        sessions = data.get("sessions")
        traces = data.get("traces")
        version = int(data.get("version") or 1)
        self._data = {
            "version": version,
            "sessions": sessions if isinstance(sessions, dict) else {},
            "traces": traces if isinstance(traces, list) else [],
        }
        if version < STATE_VERSION:
            self._migrate_v1_to_v2()
        for session in self._data["sessions"].values():
            if isinstance(session, dict):
                jobs_mod.ensure_jobs(session)
        self._loaded = True

    def _migrate_v1_to_v2(self) -> None:
        """v1 单一唤醒 → v2 三类作业（迁移前先备份）。"""
        try:
            backup = self.path.with_suffix(self.path.suffix + f".bak-v1-{int(time.time())}")
            shutil.copy2(self.path, backup)
            self.last_reason = f"migrated_v1_backup:{backup.name}"
        except OSError:
            pass
        for session in self._data.get("sessions", {}).values():
            if not isinstance(session, dict):
                continue
            jobs = jobs_mod.default_jobs()
            next_at = session.get("next_wake_at")
            explicit = session.get("explicit_wake_at")
            note = session.get("pending_note")
            if next_at and not explicit:
                jobs["dialogue"]["next_at"] = next_at
                jobs["dialogue"]["updated_at"] = session.get("last_decision_at")
            if explicit:
                jobs["temp"].append(
                    {
                        "id": jobs_mod.new_temp_id(),
                        "next_at": explicit,
                        "note": note or "用户点名的时间（v1 迁移）",
                        "created_by": "migration",
                        "created_at": timeutil.now_iso(),
                    }
                )
            elif note:
                jobs["hobby"]["intent"] = note
                jobs["hobby"]["intent_at"] = session.get("pending_note_at")
            session["jobs"] = jobs
        self._data["version"] = STATE_VERSION

    async def save(self) -> None:
        """异步落盘。"""
        async with self._lock:
            await asyncio.to_thread(self._write_sync)

    def _write_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        self._data["version"] = STATE_VERSION
        payload = json.dumps(self._data, ensure_ascii=False, indent=2)
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------
    def all_sessions(self) -> dict[str, dict[str, Any]]:
        """返回全部会话状态的拷贝。"""
        return copy.deepcopy(self._data.get("sessions") or {})

    def session(self, umo: str) -> dict[str, Any]:
        """取出（并按需补齐默认字段）某会话的状态字典。"""
        sessions = self._data.setdefault("sessions", {})
        current = sessions.get(umo)
        if not isinstance(current, dict):
            current = {}
            sessions[umo] = current
        for key, value in SESSION_DEFAULTS.items():
            if key not in current:
                current[key] = value() if callable(value) else copy.deepcopy(value)
        jobs_mod.ensure_jobs(current)
        if not current.get("created_at"):
            current["created_at"] = timeutil.now_iso()
        return current

    def get(self, umo: str, key: str, default: Any = None) -> Any:
        """读取某会话的单个字段。"""
        return self.session(umo).get(key, default)

    def set(self, umo: str, **fields: Any) -> dict[str, Any]:
        """写入某会话的若干字段并返回该会话状态。"""
        current = self.session(umo)
        for key, value in fields.items():
            if key in SESSION_DEFAULTS or key.startswith("last_"):
                current[key] = value
        return current

    def reset_session(self, umo: str) -> dict[str, Any]:
        """把会话状态重置为默认值（保留 platform/created_at）。"""
        sessions = self._data.setdefault("sessions", {})
        previous = sessions.get(umo) or {}
        fresh: dict[str, Any] = {}
        for key, value in SESSION_DEFAULTS.items():
            fresh[key] = value() if callable(value) else copy.deepcopy(value)
        fresh["platform"] = previous.get("platform")
        fresh["created_at"] = previous.get("created_at") or timeutil.now_iso()
        sessions[umo] = fresh
        return fresh

    def clear_next_wake(self, umo: str, reason: str | None = None) -> dict[str, Any]:
        """清空对话节奏并记录原因。"""
        session = self.session(umo)
        session["jobs"]["dialogue"]["next_at"] = None
        session["next_wake_at"] = None
        session["explicit_wake_at"] = None
        session["last_error"] = reason
        return session

    # ------------------------------------------------------------------
    # 轨迹
    # ------------------------------------------------------------------
    def new_trace(self, umo: str, trigger: str, kind: str = "dialogue") -> dict[str, Any]:
        """创建一条新的决策轨迹（不落盘）。"""
        return {
            "trace_id": uuid.uuid4().hex[:12],
            "umo": umo,
            "kind": kind,
            "trigger": trigger,
            "started_at": timeutil.now_iso(),
            "finished_at": None,
            "flow_source": None,
            "flow_count": 0,
            "memory_count": 0,
            "persona_id": None,
            "act": None,
            "reason": "",
            "intent": "",
            "thought": "",
            "steps": [],
            "message": "",
            "message_sent": False,
            "echo": {},
            "memory": {},
            "next_wake_at": None,
            "schedule_blocked": None,
            "schedule_fallback": False,
            "context_stats": {},
            "warnings": [],
            "errors": [],
        }

    def add_trace(self, trace: dict[str, Any]) -> dict[str, Any]:
        """把轨迹追加进环形缓冲并裁剪。"""
        traces = self._data.setdefault("traces", [])
        trace.setdefault("finished_at", timeutil.now_iso())
        traces.append(copy.deepcopy(trace))
        limit = min(max(self.trace_keep, 5), MAX_TRACES_HARD_LIMIT)
        if len(traces) > limit:
            del traces[: len(traces) - limit]
        return trace

    def traces(self, limit: int | None = None) -> list[dict[str, Any]]:
        """读取最近轨迹（新的在前）。"""
        traces = list(self._data.get("traces") or [])
        traces.reverse()
        if limit:
            return traces[: int(limit)]
        return traces

    def find_trace(self, trace_id: str) -> dict[str, Any] | None:
        """按 id 查找轨迹。"""
        for trace in self._data.get("traces") or []:
            if trace.get("trace_id") == trace_id:
                return trace
        return None
