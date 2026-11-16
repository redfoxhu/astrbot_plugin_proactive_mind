"""消息发送出口。

统一走 ``context.send_message``（主动发送），并把"是否真的送达"作为可判定结果返回，
避免没送达也记成"我已经说过了"。
"""

from __future__ import annotations

import logging

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


async def send_text(
    context,
    *,
    umo: str,
    text: str,
    log: logging.Logger | None = None,
) -> dict:
    """主动发送一条文本消息。"""
    log = log or logging.getLogger(LOGGER_NAME)
    message = (text or "").strip()
    if not message:
        return {"ok": False, "error": "empty_message"}
    if context is None:
        return {"ok": False, "error": "no_context"}

    chain = build_chain(message)
    try:
        sent = await context.send_message(umo, chain)
    except Exception as exc:  # noqa: BLE001
        log.error(f"[proactive_mind] 主动消息发送异常 {umo}: {exc}")
        return {"ok": False, "error": f"{exc}"}
    if sent is False:
        log.warning(f"[proactive_mind] 未找到匹配平台，主动消息未送达 {umo}")
        return {"ok": False, "error": "platform_not_found"}
    log.info(f"[proactive_mind] 主动消息已发送 {umo}（{len(message)} 字符）")
    return {"ok": True}
