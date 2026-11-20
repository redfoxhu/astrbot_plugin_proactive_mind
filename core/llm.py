"""LLM 调用与结构化（JSON）输出解析。

三段式决策都要求模型只返回一个 JSON 对象；这里负责：
* 解析各种"不听话"的输出（代码块、前后废话、嵌套花括号）；
* 解析失败时追加更严格的指令重试一次；
* 统一超时与异常降级，绝不让决策流程抛异常崩掉。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from typing import Any

LOGGER_NAME = "astrbot"

JSON_ONLY_REMINDER = (
    "\n\n[系统强制要求] 你上一条回复不是合法 JSON。"
    "请只输出一个 JSON 对象本身：不要代码块、不要前后解释、不要注释、不要多余文字。"
)

MAX_RAW_KEEP = 4000


def _iter_balanced_objects(text: str):
    """从文本中依次产出括号平衡的 {...} 片段。"""
    length = len(text)
    index = 0
    while index < length:
        start = text.find("{", index)
        if start < 0:
            return
        depth = 0
        in_string = False
        escaped = False
        for cursor in range(start, length):
            char = text[cursor]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    yield text[start : cursor + 1]
                    index = cursor + 1
                    break
        else:
            return


def _strip_fences(text: str) -> list[str]:
    """去掉 markdown 代码块围栏后的候选串。"""
    candidates: list[str] = []
    stripped = text.strip()
    if "```" in stripped:
        parts = stripped.split("```")
        for part in parts:
            chunk = part.strip()
            if chunk.lower().startswith("json"):
                chunk = chunk[4:].strip()
            if chunk:
                candidates.append(chunk)
    candidates.append(stripped)
    return candidates


def extract_json(text: Any) -> dict[str, Any] | None:
    """从任意文本中抽取第一个合法 JSON 对象。"""
    if text is None:
        return None
    if isinstance(text, dict):
        return text
    raw = str(text)
    if not raw.strip():
        return None
    for candidate in _strip_fences(raw):
        try:
            parsed = json.loads(candidate)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        for fragment in _iter_balanced_objects(candidate):
            try:
                parsed = json.loads(fragment)
            except Exception:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def resolve_provider(context, settings, umo: str, log: logging.Logger | None = None):
    """按优先级挑选决策用的 Provider。"""
    log = log or logging.getLogger(LOGGER_NAME)
    provider_manager = getattr(context, "provider_manager", None)

    async def by_id(provider_id: str):
        getter = getattr(provider_manager, "get_provider_by_id", None)
        if not callable(getter) or not provider_id:
            return None
        try:
            provider = await _maybe_await(getter(provider_id))
        except Exception as exc:
            log.warning(f"[proactive_mind] 按 ID 取 Provider 失败 {provider_id}: {exc}")
            return None
        if provider is not None:
            log.debug(f"[proactive_mind] 使用指定 Provider: {provider_id}")
        return provider

    configured = settings.decision_provider_id
    if configured:
        provider = await by_id(configured)
        if provider is not None:
            return provider

    getter = getattr(context, "get_current_chat_provider_id", None)
    if callable(getter):
        try:
            current_id = await _maybe_await(getter(umo))
        except Exception as exc:
            current_id = None
            log.debug(f"[proactive_mind] 读取当前 Provider ID 失败: {exc}")
        if current_id and current_id != configured:
            provider = await by_id(current_id)
            if provider is not None:
                return provider

    getter = getattr(context, "get_using_provider_async", None)
    if callable(getter):
        try:
            provider = await _maybe_await(getter(umo))
        except Exception as exc:
            provider = None
            log.warning(f"[proactive_mind] 获取 Provider 失败: {exc}")
        if provider is not None:
            return provider

    getter = getattr(context, "get_using_provider", None)
    if callable(getter):
        try:
            provider = getter(umo)
        except Exception:
            provider = None
        if provider is not None:
            return provider
    return None


async def chat(
    provider,
    *,
    system_prompt: str,
    user_prompt: str,
    timeout: float,
) -> tuple[str, str | None]:
    """单次纯文本调用，返回 ``(文本, 错误)``。"""
    try:
        response = await asyncio.wait_for(
            provider.text_chat(
                prompt=user_prompt,
                system_prompt=system_prompt,
                contexts=[],
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return "", "timeout"
    except Exception as exc:  # noqa: BLE001 - 任何异常都降级
        return "", f"error: {exc}"
    text = ""
    completion = getattr(response, "completion_text", None)
    if completion:
        text = str(completion)
    elif response is not None:
        text = str(response)
    return text, None


async def chat_json(
    context,
    settings,
    umo: str,
    *,
    system_prompt: str,
    user_prompt: str,
    log: logging.Logger | None = None,
) -> tuple[dict[str, Any] | None, str, list[str]]:
    """要求模型输出 JSON，解析失败自动严格重试一次。

    Returns:
        ``(data, raw_text, errors)``；data 为 None 表示彻底失败。
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    return await chat_json_messages(context, settings, umo, messages=messages, log=log)


async def chat_json_messages(
    context,
    settings,
    umo: str,
    *,
    messages: list[dict[str, Any]],
    log: logging.Logger | None = None,
) -> tuple[dict[str, Any] | None, str, list[str]]:
    """在同一份消息列表上请求 JSON 输出（列表本身只构造一次）。

    ``messages`` 里第一条 role=system 会作为 provider 的 system_prompt，
    其余按顺序作为 contexts——这样"人格/流水"在整轮里只出现一次。
    """
    log = log or logging.getLogger(LOGGER_NAME)
    errors: list[str] = []
    provider = await resolve_provider(context, settings, umo, log)
    if provider is None:
        errors.append("provider_unavailable")
        return None, "", errors

    system_prompt = ""
    contexts: list[dict[str, Any]] = []
    for message in messages or []:
        role = str(message.get("role") or "user")
        content = message.get("content") or ""
        if role == "system" and not system_prompt:
            system_prompt = str(content)
        else:
            contexts.append({"role": role, "content": content})

    timeout = float(settings.llm_timeout_seconds)
    raw = ""
    for attempt in range(2):
        if attempt:
            contexts = list(contexts)
            contexts.append({"role": "user", "content": JSON_ONLY_REMINDER.strip()})
        if settings.debug_log_prompts:
            log.info(
                f"[proactive_mind] (messages attempt {attempt + 1}) "
                f"system={system_prompt[:200]!r} contexts={len(contexts)}"
            )
        try:
            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=None,
                    system_prompt=system_prompt,
                    contexts=contexts,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            errors.append("timeout")
            continue
        except Exception as exc:  # noqa: BLE001
            errors.append(f"error: {exc}")
            # 网络抖动（Connection error 之类）重试一次再放弃
            if attempt == 0:
                await asyncio.sleep(1.5)
                continue
            return None, raw, errors
        text = ""
        completion = getattr(response, "completion_text", None)
        if completion:
            text = str(completion)
        elif response is not None:
            text = str(response)
        raw = text
        if settings.debug_log_prompts:
            log.info(f"[proactive_mind] raw: {text[:MAX_RAW_KEEP]}")
        data = extract_json(text)
        if data is not None:
            return data, text, errors
        errors.append("invalid_json")
    return None, raw, errors


def tool_names(data: dict[str, Any] | None) -> list[str]:
    """从模型输出中提取拟调用的工具名（供日志/轨迹）。"""
    if not isinstance(data, dict):
        return []
    steps = data.get("steps")
    if not isinstance(steps, list):
        return []
    names: list[str] = []
    for step in steps:
        if isinstance(step, dict):
            name = str(step.get("tool") or "").strip()
            if name:
                names.append(name)
    return names
