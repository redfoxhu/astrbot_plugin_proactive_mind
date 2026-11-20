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
# 白名单是"点名要的工具"，不做预算裁剪，但仍留一个安全上限
EXPLICIT_LIST_LIMIT = 60

# 记忆/检索/内容类工具优先出现在提示词里（"它真会做的事"）
PRIORITY_HINTS = ("memor", "recall", "memory", "search", "bilibili", "fetch")
# 明确要点名的工具：即便排序也一定进目录
ALWAYS_INCLUDE_NAMES = ("bilibili_read",)
# 浏览器/截图这类重操作排最后，只有在还有余量时才给模型
LOW_PRIORITY_PATTERN = re.compile(
    r"(browser_|screenshot|scroll|crop|overlay|view_image)", re.IGNORECASE
)

# 同一族工具只展示一个（其余名字在描述里列出，仍可直接调用），
# 否则 14 个 web_search_* 会把目录预算吃光。
TOOL_FAMILIES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^web_search_", re.IGNORECASE), "web_search"),
    (re.compile(r"browser", re.IGNORECASE), "browser"),
    (re.compile(r"^astrbot_execute_", re.IGNORECASE), "execute"),
)


def tool_family(name: str) -> str:
    for pattern, family in TOOL_FAMILIES:
        if pattern.search(name):
            return family
    return ""
# 未显式配置白名单时，默认不把"破坏性 / 结构性冲突"工具塞给主动心智。
# 说明：唤醒节奏由本插件统一管理，决不允许模型去调别的定时器（qts_schedule 之类），
# 否则会出现"另一个调度器到点强行唤醒主 Agent"，绕开这里的 gate 与计数。
# v0.5：正则改为配置项 mind.tool_deny_pattern（默认只挡破坏性/自身工具，宽松很多）。
DEFAULT_DENY_PATTERN = (
    r"(^|_)(delete|remove|drop|purge|wipe|truncate|unlink|"
    r"kick|ban|mute)(_|$)|shutdown|future_task|manage_wake|"
    r"pmind_|send_message_to_user"
)
DENY_NAME_PATTERN = re.compile(DEFAULT_DENY_PATTERN, re.IGNORECASE)


def deny_pattern(settings=None) -> re.Pattern[str]:
    """取当前生效的"额外剔除"正则（配置非法时退回默认）。"""
    raw = ""
    if settings is not None:
        try:
            raw = str(getattr(settings, "tool_deny_pattern", "") or "").strip()
        except Exception:
            raw = ""
    if not raw:
        return DENY_NAME_PATTERN
    try:
        return re.compile(raw, re.IGNORECASE)
    except re.error:
        logging.getLogger(LOGGER_NAME).warning(
            f"[proactive_mind] mind.tool_deny_pattern 非法，已退回默认：{raw!r}"
        )
        return DENY_NAME_PATTERN


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


def get_llm_tool_manager(context) -> Any | None:
    """取 FuncToolManager（平台工具注册表本体）。"""
    getter = getattr(context, "get_llm_tool_manager", None)
    if callable(getter):
        try:
            manager = getter()
            if manager is not None:
                return manager
        except Exception:
            pass
    provider_manager = getattr(context, "provider_manager", None)
    return getattr(provider_manager, "llm_tools", None)


def iter_tools(context) -> list[Any]:
    """列出全部工具对象。

    平台上工具分两类：
    * plugin / MCP 工具 → ``get_full_tool_set().tools``（func_list）
    * **内置工具**（web_search_* / astrbot_file_* / astrbot_execute_* / cua_* …）
      → ``manager.iter_builtin_tools()``，它们不在 func_list 里，
      只看 func_list 会以为"没有搜索工具"（v0.5 修的就是这个）。
    """
    tools: list[Any] = []
    seen: set[str] = set()

    tool_set = get_tool_set(context)
    listed = getattr(tool_set, "tools", None)
    if isinstance(listed, list):
        for tool in listed:
            name = str(getattr(tool, "name", "") or "")
            if name and name not in seen:
                seen.add(name)
                tools.append(tool)

    manager = get_llm_tool_manager(context)
    iter_builtin = getattr(manager, "iter_builtin_tools", None)
    if callable(iter_builtin):
        try:
            for tool in iter_builtin() or []:
                name = str(getattr(tool, "name", "") or "")
                if name and name not in seen:
                    seen.add(name)
                    tools.append(tool)
        except Exception as exc:
            logging.getLogger(LOGGER_NAME).debug(
                f"[proactive_mind] 读取内置工具失败: {exc}"
            )
    return tools


def find_tool(context, name: str) -> Any | None:
    """按名字查找工具对象（含内置工具）。"""
    if not name:
        return None
    manager = get_llm_tool_manager(context)
    getter = getattr(manager, "get_tool", None)
    if callable(getter):
        try:
            tool = getter(name)
            if tool is not None:
                return tool
        except Exception:
            pass
    tool_set = get_tool_set(context)
    setter = getattr(tool_set, "get_tool", None)
    if callable(setter):
        try:
            tool = setter(name)
            if tool is not None:
                return tool
        except Exception:
            pass
    for tool in iter_tools(context):
        if str(getattr(tool, "name", "")) == name:
            return tool
    return None


def provider_settings(context) -> dict[str, Any]:
    """取平台的 provider_settings（搜工具的密钥就配在这里）。"""
    for name in ("get_config", "get_astrbot_config"):
        getter = getattr(context, name, None)
        if not callable(getter):
            continue
        try:
            config = getter()
        except Exception:
            continue
        if config is None:
            continue
        try:
            section = config.get("provider_settings") if hasattr(config, "get") else None
        except Exception:
            section = None
        if isinstance(section, dict):
            return section
    return {}


def _nonempty(value: Any) -> bool:
    if isinstance(value, (list, tuple)):
        return any(str(item).strip() for item in value)
    return bool(str(value or "").strip())


def available_search_tools(context) -> dict[str, bool] | None:
    """各搜索供应商是否配了密钥（None=无法判断，不做过滤）。

    平台里所有搜索类工具共用 ``provider_settings.websearch_<provider>_key``：
    ``web_search_bocha`` / ``news_search_serpapi`` / ``tavily_extract_web_page`` …
    没配密钥时调用会直接返回 "xxx API key is not configured"，因此不能放进目录。
    """
    settings = provider_settings(context)
    if not settings:
        return None
    if not settings.get("web_search", True):
        return {}
    ready: dict[str, bool] = {}
    for key, value in settings.items():
        lowered = str(key).lower()
        if lowered.startswith("websearch_") and lowered.endswith("_key"):
            provider = lowered[len("websearch_") : -len("_key")]
            if provider:
                ready[provider] = _nonempty(value)
    # 百度用的是 app_builder key，名字对不上
    if any("baidu" in str(k).lower() for k in settings):
        ready["baidu"] = _nonempty(settings.get("websearch_baidu_app_builder_key"))
    return ready


def _search_provider_ok(name: str, ready: dict[str, bool] | None) -> bool:
    """名字里带某供应商、而该供应商没配密钥 → 不进目录。"""
    if ready is None:
        return True
    lowered = name.lower()
    for provider, ok in ready.items():
        if provider and provider in lowered:
            return ok
    return True


def whitelist_match(name: str, whitelist: list[str]) -> bool:
    """白名单支持"精确名"与"前缀"两种写法（如 web_search 可匹配 web_search_serpapi）。"""
    if not whitelist or not name:
        return False
    lowered = name.lower()
    for item in whitelist:
        entry = str(item or "").strip()
        if not entry:
            continue
        if name == entry:
            return True
        if entry.endswith("*") and lowered.startswith(entry[:-1].lower()):
            return True
        if lowered.startswith(entry.lower()):
            return True
    return False


def is_allowed(name: str, settings, *, for_catalog: bool = True) -> bool:
    """判断工具是否可用。

    * 黑名单永远生效（含插件自身调度/发送相关的结构性保护项）；
    * 白名单只约束"模型挑工具"，不影响插件内部调用（支持前缀写法）；
    * 目录模式下还会剔除配置的额外正则（默认只挡破坏性/自身工具）。
    """
    if not name:
        return False
    if name in settings.tool_blacklist:
        return False
    whitelist = settings.tool_whitelist
    if whitelist_match(name, whitelist):
        return True
    if deny_pattern(settings).search(name):
        return False
    if for_catalog and whitelist:
        return False
    return True


def build_catalog(
    context,
    settings,
    log: logging.Logger | None = None,
    *,
    allowed: list[str] | None = None,
) -> list[dict[str, Any]]:
    """构建给模型看的工具清单（按相关性排序并限量，避免提示词爆炸）。

    Args:
        allowed: 额外的白名单（例如爱好轮只给内容类工具）；为空表示用配置里的白名单。
    """
    log = log or logging.getLogger(LOGGER_NAME)
    whitelist = list(allowed) if allowed else settings.tool_whitelist
    pattern = deny_pattern(settings)
    search_ready = available_search_tools(context)  # 没配密钥的搜索工具直接不进目录
    max_entries = int(settings.catalog_max_entries)  # 0=不限
    char_budget = int(settings.catalog_char_budget)  # 0=不限
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for order, tool in enumerate(iter_tools(context)):
        name = str(getattr(tool, "name", "") or "")
        # pmind_wake / pmind_schedule 是给主 Agent 用的排程工具，不该出现在主动心智自己的工具表里
        if name in {"pmind_wake", "pmind_schedule"}:
            continue
        if not name or not bool(getattr(tool, "active", True)):
            continue
        # 黑名单永远生效
        if name in settings.tool_blacklist:
            continue
        if not _search_provider_ok(name, search_ready):
            continue
        if whitelist:
            # 显式白名单（配置或按类型传入）：只留名单内（支持前缀，如 web_search）
            if not whitelist_match(name, whitelist):
                continue
            score = 0
        else:
            if pattern.search(name):
                continue
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
    if whitelist:
        # 显式白名单（配置或按类型传入）= 用户/她自己点名的工具：
        # 原样全部给，不做"同族去重"，也不按条数/字符预算截断（只留硬上限保护）。
        catalog = [item[2] for item in scored[:EXPLICIT_LIST_LIMIT]]
        log.debug(
            f"[proactive_mind] 白名单工具 {len(catalog)} 个（不去重不截断）: "
            f"{[item['name'] for item in catalog]}"
        )
        return catalog

    # 同族去重：只保留第一个，其余名字并进描述（仍然可以按名字调用）
    family_members: dict[str, list[str]] = {}
    for _score, _order, entry in scored:
        family = tool_family(entry["name"])
        if family:
            family_members.setdefault(family, []).append(entry["name"])
    kept_family: set[str] = set()
    deduped: list[tuple[int, int, dict[str, Any]]] = []
    for item in scored:
        family = tool_family(item[2]["name"])
        if family:
            if family in kept_family:
                continue
            kept_family.add(family)
            others = [n for n in family_members.get(family, []) if n != item[2]["name"]]
            if others:
                item[2]["description"] = (
                    item[2]["description"]
                    + f"（同族还有：{', '.join(others[:6])}，可直接按名字调用）"
                )[: DESCRIPTION_LIMIT + 120]
        deduped.append(item)

    catalog: list[dict[str, Any]] = []
    used_chars = 0
    for _score, _order, entry in deduped:
        if max_entries and len(catalog) >= max_entries:
            break
        size = (
            len(entry["name"])
            + len(entry["description"])
            + len(entry["parameters"])
            + 24  # 渲染时的固定开销
        )
        if char_budget and catalog and used_chars + size > char_budget:
            break
        catalog.append(entry)
        used_chars += size
    log.debug(
        f"[proactive_mind] 工具目录 {len(catalog)}/{len(scored)} 个"
        f"（上限 {max_entries or '不限'} 条 / 预算 {char_budget or '不限'} 字符，"
        f"实际 {used_chars} 字符）: {[item['name'] for item in catalog]}"
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
