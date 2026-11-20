"""消息发送出口。

统一走 ``context.send_message``（主动发送），并把"是否真的送达"作为可判定结果返回，
避免没送达也记成"我已经说过了"。

出站前会经过 ``core/textfx`` 的三件套（分段正则 / 内容过滤正则 / 替换规则），
所以主动消息也能享受平台 ``segmented_reply`` 那套处理。
"""

from __future__ import annotations

import asyncio
import logging

from . import textfx

LOGGER_NAME = "astrbot"


def trim_message(text: str | None, max_chars: int) -> str:
    """裁剪过长的主动消息（纯函数）。"""
    value = (text or "").strip()
    if not value:
        return ""
    limit = max(1, int(max_chars))
    if len(value) <= limit:
        return value
    return value[:limit].rstrip()


def build_chain(text: str):
    """构造消息链；宿主不可用时降级为纯字符串（便于单测）。"""
    try:
        from astrbot.core.message.components import Plain
        from astrbot.core.message.message_event_result import MessageChain

        return MessageChain([Plain(text)])
    except Exception:
        return text


def prepare_segments(text: str, settings=None, *, log: logging.Logger | None = None) -> list[str]:
    """按出站规则把一段文本切成要依次发送的若干条。"""
    return textfx.transform(text, settings, log=log)


async def send_text(
    context,
    *,
    umo: str,
    text: str,
    log: logging.Logger | None = None,
    settings=None,
) -> dict:
    """主动发送一条文本消息（可能被分段规则拆成多条）。"""
    log = log or logging.getLogger(LOGGER_NAME)
    message = (text or "").strip()
    if not message:
        return {"ok": False, "error": "empty_message"}
    if context is None:
        return {"ok": False, "error": "no_context"}

    segments = prepare_segments(message, settings, log=log) or [message]
    interval = 0.0
    if settings is not None and len(segments) > 1:
        try:
            interval = max(0.0, float(getattr(settings, "outbound_segment_interval", 0.6)))
        except (TypeError, ValueError):
            interval = 0.6

    sent_count = 0
    for index, segment in enumerate(segments):
        if index and interval:
            await asyncio.sleep(interval)
        chain = build_chain(segment)
        try:
            sent = await context.send_message(umo, chain)
        except Exception as exc:  # noqa: BLE001
            log.error(f"[proactive_mind] 主动消息发送异常 {umo}: {exc}")
            if sent_count:
                return {"ok": True, "segments": sent_count, "error": f"{exc}"}
            return {"ok": False, "error": f"{exc}"}
        if sent is False:
            log.warning(f"[proactive_mind] 未找到匹配平台，主动消息未送达 {umo}")
            if sent_count:
                return {"ok": True, "segments": sent_count, "error": "platform_not_found"}
            return {"ok": False, "error": "platform_not_found"}
        sent_count += 1

    log.info(
        f"[proactive_mind] 主动消息已发送 {umo}"
        f"（{len(message)} 字符，分 {sent_count} 段）"
    )
    return {"ok": True, "segments": sent_count, "text": " ".join(segments)}
