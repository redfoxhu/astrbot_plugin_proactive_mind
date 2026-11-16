"""最近对话流水的读取。

支持三种来源：
* ``livingmemory``：LivingMemory 插件的会话流水（默认，条数可跟随它的 context_window_size）；
* ``platform``：AstrBot 平台对话历史；
* ``hybrid``：优先记忆插件，读不到再回退平台历史。

模块顶层的工具函数（``content_to_text`` / ``records_to_items`` / ``rows_to_items`` /
``format_flow``）是纯函数，单测无需 AstrBot 环境。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

LOGGER_NAME = "astrbot"

BOT_LABEL = "我（Bot）"
USER_LABEL = "用户"
PROACTIVE_LABEL = "我（主动发出）"


@dataclass
class FlowItem:
    """一条流水。"""

    role: str
    text: str
    ts: float | None = None
    source: str = "platform"
    is_bot: bool = False
    sender: str | None = None
    proactive: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def speaker(self) -> str:
        """用于提示词的说话人标签。"""
        if self.proactive:
            return PROACTIVE_LABEL
        if self.is_bot or self.role == "assistant":
            return BOT_LABEL
        return USER_LABEL


def content_to_text(value: Any) -> str:
    """把任意形态的消息内容压平成纯文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return _maybe_json_text(value)
    if isinstance(value, (list, tuple)):
        parts: list[str] = []
        for item in value:
            text = content_to_text(item)
            if text:
                parts.append(text)
        return "\n".join(parts)
    if isinstance(value, dict):
        if str(value.get("type") or "") in {"think", "reasoning"}:
            # 思考片段不是"消息内容"，不进入流水
            return ""
        for key in ("text", "content", "message_str", "completion_text"):
            if key in value and value.get(key) is not None:
                inner = content_to_text(value.get(key))
                if inner:
                    return inner
        # 未知结构：整体序列化，便于排查
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)
    for attr in ("text", "content"):
        inner = getattr(value, attr, None)
        if inner is not None and not callable(inner):
            text = content_to_text(inner)
            if text:
                return text
    return str(value)


def _maybe_json_text(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            parsed = json.loads(stripped)
        except Exception:
            return text
        if isinstance(parsed, dict):
            for key in ("text", "content"):
                if key in parsed:
                    return content_to_text(parsed[key])
    return text


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def records_to_items(records: Any, *, source: str = "platform") -> list[FlowItem]:
    """把 AstrBot 对话历史记录（dict 列表）转成流水。"""
    items: list[FlowItem] = []
    if isinstance(records, str):
        try:
            records = json.loads(records)
        except Exception:
            records = []
    if not isinstance(records, list):
        return items
    for record in records:
        if not isinstance(record, dict):
            continue
        role = str(record.get("role") or "user")
        if role not in {"user", "assistant", "system"}:
            continue
        text = content_to_text(record.get("content"))
        if not text.strip():
            continue
        items.append(
            FlowItem(
                role=role,
                text=text.strip(),
                ts=record.get("timestamp") if isinstance(record.get("timestamp"), (int, float)) else None,
                source=source,
                is_bot=role == "assistant",
                extra={"raw_role": role},
            )
        )
    return items


def rows_to_items(rows: Any, *, source: str = "livingmemory") -> list[FlowItem]:
    """把 LivingMemory ``messages`` 表行转成流水。

    行可以是 dict、sqlite3.Row 或 tuple（顺序：role, content, sender_id,
    sender_name, timestamp, metadata）。
    """
    items: list[FlowItem] = []
    for row in rows or []:
        role = content = None
        sender_id = sender_name = metadata = None
        timestamp = None
        if isinstance(row, dict):
            role = row.get("role")
            content = row.get("content")
            sender_id = row.get("sender_id")
            sender_name = row.get("sender_name")
            timestamp = row.get("timestamp")
            metadata = row.get("metadata")
        elif hasattr(row, "keys"):
            keys = set(row.keys())
            role = row["role"] if "role" in keys else None
            content = row["content"] if "content" in keys else None
            sender_id = row["sender_id"] if "sender_id" in keys else None
            sender_name = row["sender_name"] if "sender_name" in keys else None
            timestamp = row["timestamp"] if "timestamp" in keys else None
            metadata = row["metadata"] if "metadata" in keys else None
        elif isinstance(row, (list, tuple)) and len(row) >= 5:
            role, content, sender_id, sender_name, timestamp = row[:5]
            metadata = row[5] if len(row) > 5 else None

        text = content_to_text(content)
        if not text.strip():
            continue
        meta: dict[str, Any] = {}
        if isinstance(metadata, str) and metadata.strip().startswith("{"):
            try:
                parsed = json.loads(metadata)
                if isinstance(parsed, dict):
                    meta = parsed
            except Exception:
                meta = {}
        elif isinstance(metadata, dict):
            meta = metadata
        is_bot = str(role) == "assistant" or _truthy(meta.get("is_bot_message"))
        items.append(
            FlowItem(
                role=str(role or "user"),
                text=text.strip(),
                ts=float(timestamp) if isinstance(timestamp, (int, float)) else None,
                source=source,
                is_bot=is_bot,
                sender=str(sender_name or sender_id) if (sender_name or sender_id) else None,
                proactive=_truthy(meta.get("proactive_mind")),
                extra=meta,
            )
        )
    return items


NOISE_PATTERNS = (
    # AstrBot 每轮附加的时间提示（流水里存的是原始内容）
    re.compile(r"<system_reminder>.*?</system_reminder>", re.DOTALL | re.IGNORECASE),
    # QQ 适配器追加的消息 ID，判断用不上
    re.compile(r"\[MSG_ID:[^\]]*\]", re.IGNORECASE),
    # LivingMemory 的记忆注入包裹（旧记录可能残留）
    re.compile(r"<memory_injection>.*?</memory_injection>", re.DOTALL | re.IGNORECASE),
)
BLANK_LINE_RE = re.compile(r"\n{3,}")


def sanitize_text(text: str) -> str:
    """去掉流水里的系统提示/消息 ID 等噪声（纯函数）。"""
    cleaned = str(text or "")
    for pattern in NOISE_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    cleaned = cleaned.replace("\u200b", "")
    cleaned = BLANK_LINE_RE.sub("\n\n", cleaned)
    return cleaned.strip()


def format_flow(items: list[FlowItem], max_chars: int = 12000) -> str:
    """把流水拼成提示词文本，从最旧的消息开始裁剪。"""
    lines: list[str] = []
    for item in items:
        text = sanitize_text(item.text).replace("\r", " ").strip()
        if not text:
            continue
        stamp = ""
        if item.ts:
            try:
                from datetime import datetime

                stamp = datetime.fromtimestamp(float(item.ts)).strftime("%m-%d %H:%M ")
            except Exception:
                stamp = ""
        lines.append(f"[{stamp}{item.speaker()}] {text}")
    if not lines:
        return "（暂无历史流水）"
    joined = "\n".join(lines)
    if len(joined) <= max_chars:
        return joined
    kept: list[str] = []
    total = 0
    for line in reversed(lines):
        if total + len(line) + 1 > max_chars and kept:
            break
        kept.append(line)
        total += len(line) + 1
    kept.reverse()
    return "（更早的流水已省略）\n" + "\n".join(kept)


# ----------------------------------------------------------------------
# LivingMemory 定位
# ----------------------------------------------------------------------
def find_livingmemory(settings, logger: logging.Logger | None = None):
    """在进程内定位 LivingMemory 插件实例，失败返回 None。"""
    log = logger or logging.getLogger(LOGGER_NAME)
    names = {settings.livingmemory_plugin_dir, "astrbot_plugin_livingmemory"}
    try:
        from astrbot.core.star.star import star_map
    except Exception as exc:  # pragma: no cover - 非 AstrBot 环境
        log.debug(f"[proactive_mind] 无法导入 star_map: {exc}")
        return None
    for meta in list(getattr(star_map, "values", lambda: [])()):
        root_name = str(getattr(meta, "root_dir_name", "") or "")
        module_path = str(getattr(meta, "module_path", "") or "")
        if root_name not in names and not any(name in module_path for name in names):
            continue
        if not bool(getattr(meta, "activated", True)):
            continue
        plugin = getattr(meta, "star_cls", None)
        if plugin is not None:
            return plugin
    return None


def livingmemory_window(plugin, settings) -> tuple[int, str]:
    """读取 LivingMemory 的上下文窗口大小，返回 (条数, 来源说明)。"""
    configured = settings.flow_message_count
    if plugin is None or not settings.follow_livingmemory_window:
        return configured, "config"
    manager = getattr(plugin, "config_manager", None)
    getter = getattr(manager, "get", None)
    if not callable(getter):
        return configured, "config"
    try:
        window = getter("session_manager.context_window_size", configured)
    except Exception:
        return configured, "config"
    try:
        window_int = int(window)
    except (TypeError, ValueError):
        return configured, "config"
    if window_int <= 0:
        return configured, "config"
    return max(5, min(200, window_int)), "livingmemory"


# ----------------------------------------------------------------------
# 三个来源
# ----------------------------------------------------------------------
async def _read_livingmemory(context, settings, umo: str, log):
    plugin = find_livingmemory(settings, log)
    if plugin is None:
        return [], "livingmemory_unavailable"
    window, _ = livingmemory_window(plugin, settings)
    initializer = getattr(plugin, "initializer", None)
    manager = getattr(initializer, "conversation_manager", None)
    if manager is None:
        return [], "livingmemory_not_ready"
    items: list[FlowItem] = []
    getter = getattr(manager, "get_context", None)
    if callable(getter):
        try:
            raw = await getter(umo, max_messages=window, format_for_llm=True)
            if isinstance(raw, list):
                for entry in raw:
                    if not isinstance(entry, dict):
                        continue
                    text = content_to_text(entry.get("content"))
                    if not text.strip():
                        continue
                    role = str(entry.get("role") or "user")
                    items.append(
                        FlowItem(
                            role=role,
                            text=text.strip(),
                            source="livingmemory",
                            is_bot=role == "assistant",
                        )
                    )
        except Exception as exc:
            log.warning(f"[proactive_mind] 读取 LivingMemory 上下文失败: {exc}")
    if not items:
        get_messages = getattr(manager, "get_messages", None)
        if callable(get_messages):
            try:
                messages = await get_messages(umo, limit=window)
                for message in messages or []:
                    role = str(getattr(message, "role", "user"))
                    text = content_to_text(getattr(message, "content", ""))
                    if not text.strip():
                        continue
                    metadata = getattr(message, "metadata", {}) or {}
                    items.append(
                        FlowItem(
                            role=role,
                            text=text.strip(),
                            ts=getattr(message, "timestamp", None),
                            source="livingmemory",
                            is_bot=role == "assistant"
                            or _truthy(
                                metadata.get("is_bot_message")
                                if isinstance(metadata, dict)
                                else False
                            ),
                            sender=getattr(message, "sender_name", None),
                            proactive=_truthy(
                                metadata.get("proactive_mind")
                                if isinstance(metadata, dict)
                                else False
                            ),
                        )
                    )
            except Exception as exc:
                log.warning(f"[proactive_mind] 读取 LivingMemory 消息失败: {exc}")
    if items:
        return items[-window:], "livingmemory"
    sqlite_items = await _read_livingmemory_sqlite(settings, umo, log)
    if sqlite_items:
        return sqlite_items[-window:], "livingmemory_sqlite"
    return [], "livingmemory_empty"


def _livingmemory_db_candidates(settings) -> list[str]:
    """尽量多地猜测 conversations.db 的位置。"""
    names = [settings.livingmemory_plugin_dir]
    if "astrbot_plugin_livingmemory" not in names:
        names.append("astrbot_plugin_livingmemory")
    paths: list[str] = []
    for name in names:
        try:
            from astrbot.core.star.star_tools import StarTools

            data_dir = StarTools.get_data_dir(name)
            if data_dir:
                paths.append(str(data_dir / "conversations.db"))
        except Exception:
            pass
        try:
            from pathlib import Path

            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

            base = Path(str(get_astrbot_plugin_data_path()))
            paths.append(str(base / name / "conversations.db"))
        except Exception:
            pass
    unique: list[str] = []
    for path in paths:
        if path not in unique:
            unique.append(path)
    return unique


def _query_messages(db_path: str, umo: str, limit: int) -> list[tuple]:
    """只读查询 LivingMemory 的消息表。"""
    uri = "file:" + db_path.replace("\\", "/") + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        cursor = connection.execute(
            "SELECT role, content, sender_id, sender_name, timestamp, metadata "
            "FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (umo, int(limit)),
        )
        rows = cursor.fetchall()
    finally:
        connection.close()
    rows.reverse()
    return rows


async def _read_livingmemory_sqlite(settings, umo: str, log) -> list[FlowItem]:
    for db_path in _livingmemory_db_candidates(settings):
        try:
            import os

            if not os.path.exists(db_path):
                continue
            rows = await asyncio.to_thread(
                _query_messages, db_path, umo, settings.flow_message_count
            )
        except Exception as exc:
            log.debug(f"[proactive_mind] 直读 LivingMemory 数据库失败 {db_path}: {exc}")
            continue
        items = rows_to_items(rows)
        if items:
            return items
    return []


async def _read_platform(context, settings, umo: str, log):
    manager = getattr(context, "conversation_manager", None)
    if manager is None:
        return [], "platform_unavailable"
    conversation_id = None
    try:
        conversation_id = await manager.get_curr_conversation_id(umo)
    except Exception as exc:
        log.debug(f"[proactive_mind] 获取会话 ID 失败: {exc}")
    if not conversation_id:
        return [], "platform_no_conversation"
    try:
        conversation = await manager.get_conversation(umo, conversation_id)
    except Exception as exc:
        log.warning(f"[proactive_mind] 读取平台对话失败: {exc}")
        return [], "platform_error"
    history = getattr(conversation, "history", None)
    items = records_to_items(history, source="platform")
    count = settings.flow_message_count
    return items[-count:], "platform"


async def read_flow(context, settings, umo: str, log: logging.Logger | None = None):
    """读取最近流水，返回 ``(items, source_used, warnings)``。"""
    log = log or logging.getLogger(LOGGER_NAME)
    warnings: list[str] = []
    source = settings.flow_source

    if source in {"livingmemory", "hybrid"}:
        try:
            items, used = await _read_livingmemory(context, settings, umo, log)
        except Exception as exc:
            items, used = [], f"error:{exc}"
            warnings.append(f"读取记忆插件流水异常：{exc}")
        if items:
            return items, used, warnings
        if used and used != "livingmemory_empty":
            warnings.append(f"记忆插件流水不可用（{used}）")

    if source == "livingmemory":
        return [], source, warnings

    try:
        items, used = await _read_platform(context, settings, umo, log)
    except Exception as exc:
        warnings.append(f"读取平台流水异常：{exc}")
        return [], "platform_error", warnings
    if items:
        return items, used, warnings
    warnings.append(f"平台流水不可用（{used}）")
    return [], used, warnings


def merge_proactive(items: list[FlowItem], session: dict[str, Any], settings) -> list[FlowItem]:
    """把插件自己刚发出的消息补进流水（流水里已有则不重复）。"""
    if not isinstance(session, dict):
        return items
    message = session.get("last_sent_message")
    if not message:
        return items
    text = str(message).strip()
    if not text:
        return items
    if settings.dedupe_echo_in_flow:
        for item in reversed(items[-3:]):
            if text in item.text:
                return items
    items = list(items)
    items.append(
        FlowItem(
            role="assistant",
            text=text,
            ts=None,
            source="proactive_mind",
            is_bot=True,
            proactive=True,
        )
    )
    return items
