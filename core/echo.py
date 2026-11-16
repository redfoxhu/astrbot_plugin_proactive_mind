"""主动消息落库：写回 AstrBot 平台对话历史 + 写回 LivingMemory 会话库。

为什么两条都要写：
* AstrBot 历史：下一轮主 Agent 会把自己发过的话当作 assistant 记录，上下文连续；
* LivingMemory 会话库：它的反思流程读自己的 ``conversations.db``，并依据
  ``metadata.is_bot_message`` 把这条标注成 ``[Bot: 昵称 | ID: …]``，
  这样摘要才能正确归因为"Bot 自己说的"，再进入记忆数据库。

注意：不能走 LivingMemory 的 ``add_message_from_event``——它会主动丢弃
cron/agent_wakeup 这类合成事件（其 #389 行为），必须直接调 ``add_message``。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from . import flow

LOGGER_NAME = "astrbot"
MAX_IDENTITY_SCAN = 12

_LOCK = asyncio.Lock()


def build_assistant_record(text: str) -> dict[str, Any]:
    """构造 AstrBot 对话历史里的 assistant 记录（纯函数）。"""
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def _history_list(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except Exception:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _record_text(record: Any) -> str:
    if not isinstance(record, dict):
        return ""
    return flow.content_to_text(record.get("content")).strip()


async def echo_platform_history(
    context,
    *,
    umo: str,
    text: str,
    log: logging.Logger | None = None,
) -> dict[str, Any]:
    """把一条主动消息以 assistant 记录追加到平台对话历史，并回读校验。"""
    log = log or logging.getLogger(LOGGER_NAME)
    message = (text or "").strip()
    if not message:
        return {"ok": False, "error": "empty_message"}

    manager = getattr(context, "conversation_manager", None)
    if manager is None:
        return {"ok": False, "error": "no_conversation_manager"}

    async with _LOCK:
        conversation_id = None
        try:
            conversation_id = await manager.get_curr_conversation_id(umo)
        except Exception as exc:
            log.debug(f"[proactive_mind] 读取会话 ID 失败: {exc}")
        if not conversation_id:
            try:
                conversation_id = await manager.new_conversation(umo)
                log.info(f"[proactive_mind] 为 {umo} 新建了对话用于落库")
            except Exception as exc:
                return {"ok": False, "error": f"new_conversation_failed: {exc}"}

        record = build_assistant_record(message)
        for attempt in range(2):
            try:
                conversation = await manager.get_conversation(umo, conversation_id)
            except Exception as exc:
                return {"ok": False, "error": f"get_conversation_failed: {exc}"}
            history = _history_list(getattr(conversation, "history", None))
            if history and _record_text(history[-1]) == message:
                return {"ok": True, "deduped": True, "conversation_id": conversation_id}
            history.append(record)
            try:
                await manager.update_conversation(umo, conversation_id, history=history)
            except Exception as exc:
                log.warning(f"[proactive_mind] 写回平台历史失败: {exc}")
                return {"ok": False, "error": f"update_failed: {exc}"}

            # 回读校验：确认末尾确实是这一条
            try:
                verify = await manager.get_conversation(umo, conversation_id)
                verified_history = _history_list(getattr(verify, "history", None))
            except Exception:
                verified_history = []
            if verified_history and _record_text(verified_history[-1]) == message:
                return {
                    "ok": True,
                    "conversation_id": conversation_id,
                    "attempts": attempt + 1,
                }
            log.debug("[proactive_mind] 平台历史回读未命中，重试写入")
        return {"ok": False, "error": "verify_failed"}


async def resolve_bot_identity(
    context,
    settings,
    *,
    umo: str,
    session: dict[str, Any] | None,
    log: logging.Logger | None = None,
) -> tuple[str | None, str | None]:
    """解析 Bot 自身的 ID 与昵称。"""
    log = log or logging.getLogger(LOGGER_NAME)
    session = session or {}
    bot_id = session.get("bot_self_id")
    bot_name = session.get("bot_self_name") or settings.echo_bot_name or None
    if bot_id:
        return str(bot_id), str(bot_name) if bot_name else str(bot_id)

    plugin = flow.find_livingmemory(settings, log)
    manager = getattr(getattr(plugin, "initializer", None), "conversation_manager", None)
    getter = getattr(manager, "get_messages", None)
    if callable(getter):
        try:
            messages = await getter(umo, limit=MAX_IDENTITY_SCAN)
        except Exception as exc:
            messages = []
            log.debug(f"[proactive_mind] 读取历史消息以解析 Bot 身份失败: {exc}")
        for message in reversed(messages or []):
            if str(getattr(message, "role", "")) != "assistant":
                continue
            sender_id = getattr(message, "sender_id", None)
            if sender_id:
                name = getattr(message, "sender_name", None) or bot_name or str(sender_id)
                return str(sender_id), str(name)

    if settings.echo_bot_name:
        return None, settings.echo_bot_name
    platform = umo.split(":", 1)[0] if umo else "unknown"
    return f"bot:{platform}", bot_name or "Bot"


async def echo_livingmemory(
    context,
    settings,
    *,
    umo: str,
    text: str,
    session: dict[str, Any] | None = None,
    log: logging.Logger | None = None,
) -> dict[str, Any]:
    """把一条主动消息以 assistant/is_bot_message 身份写入 LivingMemory 会话库。"""
    log = log or logging.getLogger(LOGGER_NAME)
    message = (text or "").strip()
    if not message:
        return {"ok": False, "error": "empty_message"}

    plugin = flow.find_livingmemory(settings, log)
    if plugin is None:
        return {"ok": False, "error": "livingmemory_unavailable"}
    manager = getattr(getattr(plugin, "initializer", None), "conversation_manager", None)
    add_message = getattr(manager, "add_message", None)
    if not callable(add_message):
        return {"ok": False, "error": "livingmemory_not_ready"}

    bot_id, bot_name = await resolve_bot_identity(
        context, settings, umo=umo, session=session, log=log
    )
    platform = umo.split(":", 1)[0] if umo else "unknown"
    is_group = ":GroupMessage:" in umo or ":GROUP_MESSAGE:" in umo
    metadata = {"proactive_mind": True, "source": "proactive_mind"}

    try:
        created = await add_message(
            session_id=umo,
            role="assistant",
            content=message,
            sender_id=bot_id,
            sender_name=bot_name,
            group_id=umo if is_group else None,
            platform=platform,
            is_bot_message=True,
            metadata=metadata,
        )
    except TypeError:
        # 兼容旧签名：去掉 metadata/timestamp 之类的新参数
        try:
            created = await add_message(
                session_id=umo,
                role="assistant",
                content=message,
                sender_id=bot_id,
                sender_name=bot_name,
                platform=platform,
                is_bot_message=True,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"add_message_failed: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"add_message_failed: {exc}"}

    result = {
        "ok": True,
        "message_id": getattr(created, "id", None),
        "sender_id": bot_id,
        "sender_name": bot_name,
    }

    if settings.trigger_reflection:
        bumper = getattr(manager, "bump_session_revision", None)
        if callable(bumper):
            try:
                await bumper(umo)
                result["reflection"] = "revision_bumped"
            except Exception as exc:
                result["reflection"] = f"bump_failed: {exc}"
        else:
            result["reflection"] = "unsupported"
    log.info(
        f"[proactive_mind] 主动消息已写入 LivingMemory 会话库（sender={bot_id}, session={umo}）"
    )
    return result


async def echo_all(
    context,
    settings,
    *,
    umo: str,
    text: str,
    session: dict[str, Any] | None = None,
    log: logging.Logger | None = None,
) -> dict[str, Any]:
    """按配置执行两处落库，返回各自结果。"""
    log = log or logging.getLogger(LOGGER_NAME)
    result: dict[str, Any] = {}
    if settings.persist_platform:
        result["platform_history"] = await echo_platform_history(
            context, umo=umo, text=text, log=log
        )
    if settings.persist_livingmemory:
        result["livingmemory"] = await echo_livingmemory(
            context, settings, umo=umo, text=text, session=session, log=log
        )
    return result
