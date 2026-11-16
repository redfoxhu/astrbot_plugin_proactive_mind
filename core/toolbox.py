"""工具目录与工具执行。

* 目录：把平台已注册的 LLM 工具（记忆召回/写入、bilibili_read、web_search …）
  压成一份简短清单塞进提示词，让模型只从真实存在的工具里挑；
* 执行：按模型给出的顺序串行调用，超时/异常都记成结构化结果，不中断整条链路。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from types import SimpleNamespace
from typing import Any

LOGGER_NAME = "astrbot"
MAX_CATALOG_ENTRIES = 24
DESCRIPTION_LIMIT = 160
PARAMETERS_LIMIT = 420
RESULT_HARD_LIMIT = 20000

# 记忆/检索/内容类工具优先出现在提示词里（"它真会做的事"）
PRIORITY_HINTS = ("memor", "recall", "memory", "search", "bilibili", "fetch")
# 明确要点名的工具：即便排序也一定进目录
ALWAYS_INCLUDE_NAMES = ("bilibili_read",)
# 浏览器/截图这类重操作排最后，只有在还有余量时才给模型
LOW_PRIORITY_PATTERN = re.compile(
    r"(browser_|screenshot|scroll|crop|overlay|view_image)", re.IGNORECASE
)
# 未显式配置白名单时，默认不把"回合控制 / 群管理 / 破坏性"工具塞给主动心智
DENY_NAME_PATTERN = re.compile(
    r"(delete|stop_conversation|refresh_messages|mute|kick|ban|revoke|withdraw|"
    r"recall_message|change_group_card|set_group_|set_essence|set_special_title|"
    r"set_admin|leave_group)",
    re.IGNORECASE,
)


def _priority_score(name: str, settings) -> int:
    """0=核心工具，1=相关工具，2=普通，3=重操作。"""
    lowered = name.lower()
    core_names = {
        settings.memory_tool_name,
        settings.recall_tool_name,
        *ALWAYS_INCLUDE_NAMES,
    }
    if name in core_names or lowered.startswith("bilibili_"):
        return 0
    if LOW_PRIORITY_PATTERN.search(name):
        return 3
    if any(hint in lowered for hint in PRIORITY_HINTS):
        return 1
    return 2


def get_tool_set(context) -> Any | None:
    """取平台的 ToolSet（含全部可用工具）。"""
    manager = None
    getter = getattr(context, "get_llm_tool_manager", None)
    if callable(getter):
        try:
            manager = getter()
        except Exception:
            manager = None
    if manager is None:
        provider_manager = getattr(context, "provider_manager", None)
        manager = getattr(provider_manager, "llm_tools", None)
    if manager is None:
        return None
    full_getter = getattr(manager, "get_full_tool_set", None)
    if callable(full_getter):
        try:
            return full_getter()
        except Exception:
            return None
    return manager


def iter_tools(context) -> list[Any]:
    """列出全部工具对象。"""
    tool_set = get_tool_set(context)
    tools = getattr(tool_set, "tools", None)
    if isinstance(tools, list):
        return tools
    return []


def find_tool(context, name: str) -> Any | None:
    """按名字查找工具对象。"""
    if not name:
        return None
    tool_set = get_tool_set(context)
    getter = getattr(tool_set, "get_tool", None)
    if callable(getter):
        try:
            tool = getter(name)
            if tool is not None:
                return tool
        except Exception:
            pass
    for tool in iter_tools(context):
        if str(getattr(tool, "name", "")) == name:
            return tool
    return None


def is_allowed(name: str, settings, *, for_catalog: bool = True) -> bool:
    """判断工具是否可用（黑名单总是生效，白名单只约束模型挑选）。"""
    if not name:
        return False
    if name in settings.tool_blacklist:
        return False
    whitelist = settings.tool_whitelist
    if for_catalog and whitelist:
        return name in whitelist
    return True


def build_catalog(context, settings, log: logging.Logger | None = None) -> list[dict[str, Any]]:
    """构建给模型看的工具清单（按相关性排序并限量，避免提示词爆炸）。"""
    log = log or logging.getLogger(LOGGER_NAME)
    whitelist = settings.tool_whitelist
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for order, tool in enumerate(iter_tools(context)):
        name = str(getattr(tool, "name", "") or "")
        if not name or not is_allowed(name, settings, for_catalog=True):
            continue
        if not bool(getattr(tool, "active", True)):
            continue
        if whitelist:
            score = 0 if name in whitelist else 1
        elif DENY_NAME_PATTERN.search(name):
            continue
        else:
            score = _priority_score(name, settings)
        description = str(getattr(tool, "description", "") or "").strip()
        parameters = getattr(tool, "parameters", None) or {}
        try:
            parameters_text = json.dumps(parameters, ensure_ascii=False)
        except Exception:
            parameters_text = "{}"
        scored.append(
            (
                score,
                order,
                {
                    "name": name,
                    "description": description[:DESCRIPTION_LIMIT],
                    "parameters": parameters_text[:PARAMETERS_LIMIT],
                },
            )
        )
    scored.sort(key=lambda item: (item[0], item[1]))
    catalog = [item[2] for item in scored[:MAX_CATALOG_ENTRIES]]
    log.debug(
        f"[proactive_mind] 工具目录 {len(catalog)}/{len(scored)} 个: "
        f"{[item['name'] for item in catalog]}"
    )
    return catalog


def describe_catalog(catalog: list[dict[str, Any]]) -> str:
    """把工具清单渲染成提示词片段。"""
    if not catalog:
        return "（当前没有可用工具，steps 必须为空数组）"
    lines: list[str] = []
    for item in catalog:
        lines.append(
            f"- {item['name']}: {item['description']}\n  参数 schema: {item['parameters']}"
        )
    return "\n".join(lines)


def make_event(context, umo: str, note: str = "[主动心智]"):
    """构造合成事件，供需要 event 的工具（会话/身份/管理员判定）使用。"""
    try:
        from astrbot.core.cron.events import CronMessageEvent
        from astrbot.core.platform.message_session import MessageSession

        session = MessageSession.from_str(umo)
        return CronMessageEvent(
            context=context,
            session=session,
            message=note,
            extras={"proactive_mind": True},
        )
    except Exception as exc:  # pragma: no cover - 依赖宿主版本
        logging.getLogger(LOGGER_NAME).debug(
            f"[proactive_mind] 构造合成事件失败: {exc}"
        )
        return None


def make_tool_context(context, event=None):
    """构造工具调用需要的上下文包装。"""
    try:
        from astrbot.core.agent.run_context import ContextWrapper
        from astrbot.core.astr_agent_context import AstrAgentContext

        return ContextWrapper(context=AstrAgentContext(context=context, event=event))
    except Exception:
        return SimpleNamespace(
            context=SimpleNamespace(context=context, event=event),
            messages=[],
            tool_call_timeout=120,
        )


def result_to_text(result: Any) -> str:
    """把工具返回值压成文本。"""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    content = getattr(result, "content", None)
    if content is not None:
        try:
            return json.dumps(content, ensure_ascii=False)
        except Exception:
            return str(content)
    try:
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception:
        return str(result)


async def _invoke(tool, wrapper, arguments: dict[str, Any]) -> Any:
    caller = getattr(tool, "call", None)
    if callable(caller):
        return await caller(wrapper, **arguments)
    handler = getattr(tool, "handler", None)
    if callable(handler):
        outcome = handler(**arguments)
        if asyncio.iscoroutine(outcome):
            return await outcome
        return outcome
    raise RuntimeError(f"工具 {getattr(tool, 'name', '?')} 不可调用")


async def execute_step(
    context,
    settings,
    *,
    step: dict[str, Any],
    log: logging.Logger | None = None,
    event=None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """执行一个步骤，返回结构化结果。"""
    log = log or logging.getLogger(LOGGER_NAME)
    name = str((step or {}).get("tool") or "").strip()
    arguments = (step or {}).get("args")
    if not isinstance(arguments, dict):
        arguments = {}
    record: dict[str, Any] = {"tool": name, "args": arguments, "ok": False, "result": ""}

    if not name:
        record["error"] = "missing_tool_name"
        return record
    if not is_allowed(name, settings, for_catalog=False):
        record["error"] = "tool_blocked"
        return record

    tool = find_tool(context, name)
    if tool is None:
        record["error"] = "tool_not_found"
        return record

    wrapper = make_tool_context(context, event)
    timeout = float(timeout_seconds or settings.llm_timeout_seconds)
    try:
        raw = await asyncio.wait_for(_invoke(tool, wrapper, arguments), timeout=timeout)
    except asyncio.TimeoutError:
        record["error"] = "timeout"
        log.warning(f"[proactive_mind] 工具 {name} 调用超时 {timeout}s")
        return record
    except Exception as exc:  # noqa: BLE001 - 工具异常只记录
        record["error"] = f"error: {exc}"
        log.warning(f"[proactive_mind] 工具 {name} 调用失败: {exc}")
        return record

    text = result_to_text(raw)
    limit = min(int(settings.tool_result_max_chars), RESULT_HARD_LIMIT)
    record["ok"] = True
    record["result"] = text[:limit]
    record["truncated"] = len(text) > limit
    log.info(f"[proactive_mind] 工具 {name} 执行完成，返回 {len(text)} 字符")
    return record


async def execute_steps(
    context,
    settings,
    *,
    steps: list[dict[str, Any]],
    log: logging.Logger | None = None,
    event=None,
) -> list[dict[str, Any]]:
    """串行执行计划里的全部步骤（受 max_tool_calls 限制）。"""
    log = log or logging.getLogger(LOGGER_NAME)
    limit = int(settings.max_tool_calls)
    if limit <= 0:
        return []
    records: list[dict[str, Any]] = []
    for step in (steps or [])[:limit]:
        if not isinstance(step, dict):
            continue
        record = await execute_step(
            context, settings, step=step, log=log, event=event
        )
        records.append(record)
    return records


def summarize_results(records: list[dict[str, Any]], max_chars: int = 1500) -> str:
    """把执行结果渲染成提示词片段。"""
    if not records:
        return "（没有调用任何工具）"
    lines: list[str] = []
    for record in records:
        status = "成功" if record.get("ok") else f"失败（{record.get('error') or 'unknown'}）"
        snippet = str(record.get("result") or "").replace("\n", " ")
        if len(snippet) > max_chars:
            snippet = snippet[:max_chars] + "…"
        lines.append(f"- {record.get('tool')}: {status} | {snippet}")
    return "\n".join(lines)
