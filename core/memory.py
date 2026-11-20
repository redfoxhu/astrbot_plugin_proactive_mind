"""把一轮主动行为写进长期记忆。

约定（v0.4）：
* **不再拼接** ``【主动行为｜…】`` 这类模板；写什么由模型自己给出（输出字段 ``memory``），
  这里原样交给 LivingMemory 的写入工具，由它自己的总结模式（
  ``build_memory_from_structured_data → _build_storage_format``）规范化；
* ``memory.mode``：
  - ``tool``（默认）：调写入工具；
  - ``reflection``：不调工具，只靠主动消息 echo 进会话库、让它自己的反思窗口总结；
  - ``both``：两者都做；
* 工具缺失/失败时写本地 JSONL 兜底，保证不会"什么都没留下"。
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
MAX_MEMORY_CONTENT = 1600


def fallback_text(
    *,
    kind: str,
    thought: str = "",
    intent: str = "",
    steps: list[dict[str, Any]] | None = None,
    message: str | None = None,
) -> str:
    """模型没给 ``memory`` 时的兜底文本（纯自然语言，不带模板方括号）。"""
    actions = "、".join(
        f"{record.get('tool')}{'（成功）' if record.get('ok') else '（失败）'}"
        for record in (steps or [])
        if isinstance(record, dict)
    )
    parts = []
    if thought.strip():
        parts.append(thought.strip())
    if intent.strip() and intent.strip() != thought.strip():
        parts.append(intent.strip())
    if actions:
        parts.append(f"做了：{actions}")
    if message:
        parts.append(f"说了：{message.strip()}")
    text = "；".join(parts) or f"这一轮（{kind}）没有留下内容"
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
    importance: float | None = None,
    log: logging.Logger | None = None,
    event=None,
    data_dir: str | Path = ".",
    mode: str | None = None,
) -> dict[str, Any]:
    """写入一条长期记忆，返回结构化结果。

    ``content`` 是模型给出的自然语言总结（或兜底文本），不带任何固定格式。
    """
    log = log or logging.getLogger(LOGGER_NAME)
    text = (content or "").strip()
    if not text:
        return {"ok": False, "error": "empty_content"}
    if not settings.write_memory:
        return {"ok": False, "skipped": True, "reason": "write_memory_disabled"}

    effective_mode = (mode or settings.memory_mode or "tool").lower()
    record: dict[str, Any] = {"mode": effective_mode, "ok": False}

    if effective_mode == "reflection":
        # 只靠 echo + LivingMemory 自身反思；这里不算失败
        record.update({"ok": True, "skipped": True, "reason": "reflection_mode"})
        return record

    tool_name = settings.memory_tool_name
    weight = settings.memory_importance if importance is None else importance
    if toolbox.find_tool(context, tool_name) is not None:
        result = await toolbox.execute_step(
            context,
            settings,
            step={
                "tool": tool_name,
                "args": {
                    "memory": text,
                    "topics": list(topics or ["proactive_mind"]),
                    "importance": float(weight),
                    "reason": reason or "",
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
            log.info("[proactive_mind] 已写入主动行为记忆（LivingMemory 自带格式）")
            return record
        log.warning(f"[proactive_mind] 记忆工具写入失败，改用本地兜底: {record.get('error')}")
    else:
        record["error"] = "memory_tool_not_found"

    payload = {
        "timestamp": time.time(),
        "umo": umo,
        "kind": (topics or [""])[0] if topics else "",
        "mode": effective_mode,
        "importance": float(weight),
        "topics": list(topics or ["proactive_mind"]),
        "content": text,
        "error": record.get("error"),
    }
    try:
        path = await _write_fallback(data_dir, payload)
        record["fallback_file"] = path
        log.info(f"[proactive_mind] 主动行为记忆已写入本地兜底文件 {path}")
    except Exception as exc:
        record["error"] = f"{record.get('error') or 'write_failed'}: {exc}"
        log.error(f"[proactive_mind] 本地兜底写入失败: {exc}")
    return record
