"""人格（Persona）解析。

优先级：会话绑定的人格 → 默认人格 v3。结果带短 TTL 缓存，避免每次唤醒都查库。
"""

from __future__ import annotations

import logging
import time
from typing import Any

LOGGER_NAME = "astrbot"
CACHE_TTL_SECONDS = 60.0

GENERIC_PERSONA = (
    "你是用户的聊天搭子。保持自然、简短、有分寸的表达；"
    "你可以在合适的时候主动开口，也可以在没必要的时候保持安静。"
)


class PersonaResolver:
    """按 umo 解析当前人格提示词。"""

    def __init__(self, context, logger: logging.Logger | None = None) -> None:
        self.context = context
        self.log = logger or logging.getLogger(LOGGER_NAME)
        self._cache: dict[str, tuple[float, str, str]] = {}

    async def resolve(self, umo: str) -> tuple[str, str]:
        """返回 ``(system_prompt, persona_id)``。"""
        now = time.monotonic()
        cached = self._cache.get(umo)
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            return cached[1], cached[2]

        prompt, persona_id = await self._load(umo)
        self._cache[umo] = (now, prompt, persona_id)
        return prompt, persona_id

    def invalidate(self, umo: str | None = None) -> None:
        """清空缓存（配置或人格变更后调用）。"""
        if umo is None:
            self._cache.clear()
        else:
            self._cache.pop(umo, None)

    async def _load(self, umo: str) -> tuple[str, str]:
        persona_id = ""
        manager = getattr(self.context, "persona_manager", None)
        if manager is None:
            return GENERIC_PERSONA, persona_id

        conversation = await self._conversation(umo)
        bound_id = getattr(conversation, "persona_id", None) if conversation else None
        if bound_id:
            getter = getattr(manager, "get_persona", None)
            if callable(getter):
                try:
                    persona = await getter(bound_id)
                except Exception as exc:
                    self.log.debug(f"[proactive_mind] 读取会话人格失败 {bound_id}: {exc}")
                    persona = None
                if persona is not None:
                    prompt = str(getattr(persona, "system_prompt", "") or "").strip()
                    if prompt:
                        return prompt, str(bound_id)

        getter = getattr(manager, "get_default_persona_v3", None)
        if callable(getter):
            try:
                default_persona = await getter(umo=umo)
            except TypeError:
                try:
                    default_persona = await getter()
                except Exception as exc:
                    default_persona = None
                    self.log.debug(f"[proactive_mind] 读取默认人格失败: {exc}")
            except Exception as exc:
                default_persona = None
                self.log.debug(f"[proactive_mind] 读取默认人格失败: {exc}")
            if isinstance(default_persona, dict):
                prompt = str(default_persona.get("prompt") or "").strip()
                if prompt:
                    return prompt, str(default_persona.get("name") or "")
            elif default_persona is not None:
                prompt = str(getattr(default_persona, "system_prompt", "") or "").strip()
                if prompt:
                    return prompt, ""

        return GENERIC_PERSONA, persona_id

    async def _conversation(self, umo: str) -> Any | None:
        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            return None
        try:
            conversation_id = await manager.get_curr_conversation_id(umo)
            if not conversation_id:
                return None
            return await manager.get_conversation(umo, conversation_id)
        except Exception as exc:
            self.log.debug(f"[proactive_mind] 读取会话失败 {umo}: {exc}")
            return None
