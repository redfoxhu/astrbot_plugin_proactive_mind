"""把"主动行为的思考与动作"写进长期记忆。

优先调用 LivingMemory 已注册的记忆写入工具（默认 ``memorize_long_term_memory``），
这样去重、Embedding、图记忆、原子链路都会正常生效；工具缺失时退化成插件本地的
JSONL 审计文件，保证不会"什么都没留下"。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from . import toolbox

LOGGER_NAME = "astrbot"
FALLBACK_FILE = "memory_fallback.jsonl"
MAX_MEMORY_CONTENT = 1200


def build_memory_text(
    *,
    trigger: str,
    thought: str,
    intent: str,
    steps: list[dict[str, Any]],
    message: str | None,
    next_wake_human: str,
) -> str:
    """拼出写入记忆的正文（纯函数，便于测试）。"""
    action_lines: list[str] = []
    for record in steps or []:
        name = record.get("tool") or "?"
        status = "成功" if record.get("ok") else f"失败({record.get('error') or 'unknown'})"
        action_lines.append(f"{name}（{status}）")
    actions = "、".join(action_lines) if action_lines else "没有调用工具"

    parts = [
        f"【主动行为｜触发：{trigger}】",
        f"我的想法：{(thought or intent or '（没有记录想法）').strip()}",
    ]
    if intent and thought and intent.strip() != thought.strip():
        parts.append(f"我的打算：{intent.strip()}")
    parts.append(f"我做的事：{actions}")
    if message:
        parts.append(f"我说的话：{message.strip()}")
    else:
        parts.append("我说的话：（这次没有发消息）")
    if next_wake_human:
        parts.append(f"下次唤醒：{next_wake_human}")
    text = "\n".join(parts)
    if len(text) > MAX_MEMORY_CONTENT:
        text = text[:MAX_MEMORY_CONTENT] + "…"
    return text


async def _write_fallback(data_dir: str | Path, payload: dict[str, Any]) -> str:
    """把记忆写到本地 JSONL（工具缺失时的兜底）。"""
    path = Path(data_dir) / FALLBACK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False)

    def _append() -> None:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    await asyncio.to_thread(_append)
    return str(path)


async def write_memory(
    context,
    settings,
    *,
    umo: str,
    content: str,
    topics: list[str] | None = None,
    reason: str = "",
    log: logging.Logger | None = None,
    event=None,
    data_dir: str | Path = ".",
) -> dict[str, Any]:
    """写入一条长期记忆，返回结构化结果。"""
    log = log or logging.getLogger(LOGGER_NAME)
    text = (content or "").strip()
    if not text:
        return {"ok": False, "error": "empty_content"}
    if not settings.write_memory:
        return {"ok": False, "skipped": True, "reason": "write_memory_disabled"}

    tool_name = settings.memory_tool_name
    record: dict[str, Any] = {"tool": tool_name, "ok": False}
    if toolbox.find_tool(context, tool_name) is not None:
        result = await toolbox.execute_step(
            context,
            settings,
            step={
                "tool": tool_name,
                "args": {
                    "memory": text,
                    "topics": list(topics or ["proactive_mind"]),
                    "importance": settings.memory_importance,
                    "reason": reason or "主动行为记录",
                },
            },
            log=log,
            event=event,
        )
        record.update(
            {
                "ok": bool(result.get("ok")),
                "error": result.get("error"),
                "result": str(result.get("result") or "")[:600],
            }
        )
        if record["ok"]:
            log.info("[proactive_mind] 已通过记忆工具写入主动行为记录")
            return record
        log.warning(
            f"[proactive_mind] 记忆工具写入失败，改用本地兜底: {record.get('error')}"
        )
    else:
        record["error"] = "memory_tool_not_found"

    payload = {
        "timestamp": time.time(),
        "umo": umo,
        "tool": tool_name,
        "importance": settings.memory_importance,
        "topics": list(topics or ["proactive_mind"]),
        "content": text,
        "error": record.get("error"),
    }
    try:
        path = await _write_fallback(data_dir, payload)
        record["fallback_file"] = path
        log.info(f"[proactive_mind] 主动行为记录已写入本地兜底文件 {path}")
    except Exception as exc:
        record["error"] = f"{record.get('error') or 'write_failed'}: {exc}"
        log.error(f"[proactive_mind] 本地兜底写入失败: {exc}")
    return record
