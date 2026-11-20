"""配置读取、类型归一与边界钳制。

所有配置项都通过本模块访问，保证：
1. UI 里可能出现的字符串/空值不会让决策流程崩溃；
2. 关键范围（连续唤醒上限、间隔、条数）在运行时再次钳制；
3. 单测可以不依赖 AstrBot 的 AstrBotConfig。
"""

from __future__ import annotations

import copy
import json
from typing import Any

TRUE_WORDS = {"1", "true", "yes", "on", "y", "t", "是", "开", "启用"}
FALSE_WORDS = {"0", "false", "no", "off", "n", "f", "否", "关", "禁用"}

# 这两项牵涉插件自身的调度与发送路径，任何配置下都屏蔽
BUILTIN_TOOL_BLACKLIST = ("future_task", "send_message_to_user")

DEFAULTS: dict[str, dict[str, Any]] = {
    "basic": {
        "enabled": True,
        "trigger_on_user_message": True,
        "user_message_mode": "decide",
        "next_time_marker": True,
        "debounce_seconds": 3,
        "post_reply_delay_seconds": 2,
    },
    "state": {
        "recent_sent_hours": 6,
    },
    "flow": {
        "flow_source": "livingmemory",
        "flow_message_count": 50,
        "follow_livingmemory_window": True,
        "livingmemory_plugin_dir": "astrbot_plugin_livingmemory",
        "flow_max_chars": 12000,
        "flow_max_age_hours": 24,
    },
    "wake": {
        "wake_delay_minutes": 0,
        "min_interval_minutes": 5,
        "max_interval_minutes": 1440,
        "catch_up_missed_wakes": False,
        "timezone": "",
    },
    "hobby": {
        "memory_count": 30,
        "flow_count": 20,
        "memory_tag": "proactive_hobby",
        "keywords": "兴趣,爱好,喜欢,想学,想试,追,B站",
        "min_interval_minutes": 30,
        "max_interval_minutes": 1440,
        "tool_whitelist": "bilibili_read\nsearch_memes\nweb_search",
        "scope_filter": False,
        "auto_start": True,
    },
    "temp": {
        "max_jobs": 5,
        "coalesce_seconds": 0,
    },
    "limit": {
        "max_consecutive_wakes": 3,
        "count_only_when_message_sent": False,
    },
    "mind": {
        # 对话/临时作业由谁执行：main_agent=投递给主 Agent 走完整管线（含工具循环）
        "dialogue_execute": "main_agent",
        "temp_execute": "main_agent",
        # 交还主 Agent 时先按随机区间垫一个保底时间
        "provisional_enabled": True,
        # 插件自己跑的工具轮：合成事件是否带管理员身份（决定 qts 浏览器等受限工具能否调用）
        "synthetic_admin": True,
        # 两次唤醒投递的最小间隔（秒）：防止保底 job 与真实到点重复唤醒
        "delivery_guard_seconds": 90,
        # 投递后等这么久还没回复，才补垫保底（秒）
        "provisional_grace_seconds": 120,
        # 记账只认最近 N 秒内的投递（秒），防止旧事件重复改时间
        "wake_record_window_seconds": 600,
        # 每轮注入（贴近用户消息）：提醒她结尾给时间+话题
        "output_hint": (
            "正文照常写，写完在最后单独一行写 [[pmind_next:时间|话题]]；"
            "时间必须具体（+30m / +2h / 明天09:00 这种），话题是下次想说的话（≤20 字）。"
            "这行用户看不到，别只写这一行——正文不能省。"
        ),
        # 仅“到点唤醒”那一轮注入
        "wake_hint": (
            "这是你自己到点醒来，没有人在跟你说话。\n"
            "上次留下的念头：{topic}\n"
            "距上次对话：{since}\n"
            "语气按你自己来，别像在回答提问；先想清楚要不要查/调工具，再开口。"
        ),
        "decision_provider_id": "",
        "llm_timeout_seconds": 90,
        "max_tool_calls": 3,
        "single_request": False,
        "catalog_max_entries": 24,
        "catalog_char_budget": 8000,
        "tool_deny_pattern": (
            r"(^|_)(delete|remove|drop|purge|wipe|truncate|unlink|"
            r"kick|ban|mute)(_|$)|shutdown|future_task|manage_wake|"
            r"pmind_|send_message_to_user"
        ),
        "tool_whitelist": "",
        "tool_blacklist": (
            "future_task\nsend_message_to_user\n"
            "qts_stop_conversation\nqts_refresh_messages\nqts_delete_message"
        ),
        "tool_result_max_chars": 2000,
        "persona_extra_instruction": "",
        "expose_wake_tool": True,
        "announce_wake_tool": True,
    },
    "memory": {
        "write_memory": True,
        "memory_importance": 0.7,
        "hobby_importance": 0.8,
        "mode": "tool",
        "memory_tool_name": "memorize_long_term_memory",
        "recall_tool_name": "recall_long_term_memory",
    },
    "echo": {
        "persist_proactive_to_platform_history": True,
        "persist_proactive_to_livingmemory": True,
        "livingmemory_write_trigger_reflection": False,
        "echo_bot_name": "",
        "dedupe_echo_in_flow": True,
    },
    "message": {
        "max_message_chars": 800,
        "allow_empty_message": True,
    },
    "outbound": {
        "segment_enabled": False,
        "segment_pattern": "",
        "segment_max": 4,
        "segment_interval": 0.6,
        "cleanup_enabled": False,
        "cleanup_pattern": "",
        "replace_enabled": True,
        # 与平台 regex_filter 对齐：全角逗号 → 3 个空格；再删掉星号
        "replace_rules": [
            {"pattern": "，", "replacement": "   "},
            {"pattern": "\\*", "replacement": ""},
        ],
    },
    "panel": {
        "stale_hours": 6,
        "trace_similarity": 0.85,
    },
    "debug": {
        "debug_log_prompts": False,
        "trace_keep": 30,
    },
}


def as_bool(value: Any, default: bool = False) -> bool:
    """宽松布尔解析。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in TRUE_WORDS:
        return True
    if text in FALSE_WORDS:
        return False
    return default


def as_int(value: Any, default: int, low: int | None = None, high: int | None = None) -> int:
    """宽松整数解析 + 范围钳制。"""
    try:
        if isinstance(value, bool):
            parsed = int(value)
        elif isinstance(value, (int, float)):
            parsed = int(value)
        else:
            text = str(value).strip()
            parsed = int(float(text)) if text else int(default)
    except (TypeError, ValueError):
        parsed = int(default)
    if low is not None and parsed < low:
        parsed = low
    if high is not None and parsed > high:
        parsed = high
    return parsed


def as_float(
    value: Any, default: float, low: float | None = None, high: float | None = None
) -> float:
    """宽松浮点解析 + 范围钳制。"""
    try:
        if isinstance(value, bool):
            parsed = float(value)
        elif isinstance(value, (int, float)):
            parsed = float(value)
        else:
            text = str(value).strip()
            parsed = float(text) if text else float(default)
    except (TypeError, ValueError):
        parsed = float(default)
    if low is not None and parsed < low:
        parsed = low
    if high is not None and parsed > high:
        parsed = high
    return parsed


def as_str(value: Any, default: str = "") -> str:
    """宽松字符串解析。"""
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def as_lines(value: Any) -> list[str]:
    """把多行文本/列表/逗号串解析为去空去重的名称列表。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw_items = [as_str(item) for item in value]
    else:
        text = as_str(value)
        raw_items = text.replace("，", "\n").replace(",", "\n").splitlines()
    result: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        name = item.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(name)
    return result


class Settings:
    """插件配置的只读视图（每次访问都重新读取，支持面板热改）。"""

    def __init__(self, config: Any = None) -> None:
        self.config = config if config is not None else {}

    # ------------------------------------------------------------------
    # 原始读取
    # ------------------------------------------------------------------
    def raw(self, section: str, key: str) -> Any:
        """读取原始配置值，缺失时返回该键的默认值。"""
        default = DEFAULTS.get(section, {}).get(key)
        block = None
        try:
            block = self.config.get(section) if hasattr(self.config, "get") else None
        except Exception:
            block = None
        if not isinstance(block, dict):
            return default
        if key not in block:
            return default
        value = block.get(key)
        return default if value is None else value

    def section(self, section: str) -> dict[str, Any]:
        """返回某分组归一后的完整字典。"""
        merged = dict(DEFAULTS.get(section, {}))
        try:
            block = self.config.get(section) if hasattr(self.config, "get") else None
        except Exception:
            block = None
        if isinstance(block, dict):
            merged.update({k: v for k, v in block.items() if v is not None})
        return merged

    def update(self, patch: dict[str, Any]) -> list[str]:
        """按 ``{"section.key": value}`` 形式写回配置，返回实际写入的键。"""
        applied: list[str] = []
        for dotted, value in (patch or {}).items():
            if not isinstance(dotted, str) or "." not in dotted:
                continue
            section, key = dotted.split(".", 1)
            if key not in DEFAULTS.get(section, {}):
                continue
            block = None
            try:
                block = self.config.get(section) if hasattr(self.config, "get") else None
            except Exception:
                block = None
            if not isinstance(block, dict):
                block = {}
                try:
                    self.config[section] = block
                except Exception:
                    continue
            expected = type(DEFAULTS[section][key])
            if expected is bool:
                block[key] = as_bool(value, DEFAULTS[section][key])
            elif expected is int:
                block[key] = as_int(value, DEFAULTS[section][key])
            elif expected is float:
                block[key] = as_float(value, DEFAULTS[section][key])
            elif expected is list:
                # 列表/字典型配置必须原样存，别 str() 成 "[]"（曾把替换规则整条弄丢）
                block[key] = copy.deepcopy(value) if isinstance(value, list) else []
            elif expected is dict:
                block[key] = copy.deepcopy(value) if isinstance(value, dict) else {}
            else:
                block[key] = as_str(value, DEFAULTS[section][key])
            applied.append(dotted)
        return applied

    def to_dict(self) -> dict[str, Any]:
        """导出全部归一后的配置，供面板展示。"""
        return {name: self.section(name) for name in DEFAULTS}

    # ------------------------------------------------------------------
    # basic
    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return as_bool(self.raw("basic", "enabled"), True)

    @property
    def trigger_on_user_message(self) -> bool:
        return as_bool(self.raw("basic", "trigger_on_user_message"), True)

    @property
    def user_message_mode(self) -> str:
        """用户消息后的处理方式。

        * ``record_only``：主 Agent 的回复即本轮对话 job 的执行体，插件只记账不补话；
        * ``decide``：插件也做一次判断，可能补一句；
        * ``schedule_only``：只重排时间，不判断不发言。
        """
        mode = as_str(self.raw("basic", "user_message_mode"), "decide").strip().lower()
        return mode if mode in {"record_only", "decide", "schedule_only"} else "decide"

    @property
    def debounce_seconds(self) -> int:
        return as_int(self.raw("basic", "debounce_seconds"), 3, 0, 120)

    @property
    def post_reply_delay_seconds(self) -> int:
        return as_int(self.raw("basic", "post_reply_delay_seconds"), 2, 0, 60)

    # ------------------------------------------------------------------
    # flow
    # ------------------------------------------------------------------
    @property
    def flow_source(self) -> str:
        source = as_str(self.raw("flow", "flow_source"), "livingmemory").strip().lower()
        return source if source in {"livingmemory", "platform", "hybrid"} else "livingmemory"

    @property
    def flow_message_count(self) -> int:
        return as_int(self.raw("flow", "flow_message_count"), 50, 5, 200)

    @property
    def follow_livingmemory_window(self) -> bool:
        return as_bool(self.raw("flow", "follow_livingmemory_window"), True)

    @property
    def livingmemory_plugin_dir(self) -> str:
        return as_str(
            self.raw("flow", "livingmemory_plugin_dir"), "astrbot_plugin_livingmemory"
        ).strip() or "astrbot_plugin_livingmemory"

    @property
    def flow_max_chars(self) -> int:
        return as_int(self.raw("flow", "flow_max_chars"), 12000, 2000, 60000)

    @property
    def flow_max_age_hours(self) -> float:
        """early 的流水（超过这个小时数）不再注入，避免旧话题反复锚定。"""
        return as_float(self.raw("flow", "flow_max_age_hours"), 24.0, 0.0, 720.0)

    # ------------------------------------------------------------------
    # wake
    # ------------------------------------------------------------------
    @property
    def wake_delay_minutes(self) -> int:
        return as_int(self.raw("wake", "wake_delay_minutes"), 0, 0, 1440)

    @property
    def default_interval_minutes(self) -> int:
        """已废弃：兜底改成在 min~max 之间随机，这里返回区间中点（仅供 restore/兼容用）。"""
        low = self.min_interval_minutes
        high = self.max_interval_minutes
        return max(low, min(high, int((low + high) / 2)))

    @property
    def min_interval_minutes(self) -> int:
        return as_int(self.raw("wake", "min_interval_minutes"), 5, 1, 1440)

    @property
    def max_interval_minutes(self) -> int:
        low = self.min_interval_minutes
        return as_int(self.raw("wake", "max_interval_minutes"), 1440, low, 1440)

    @property
    def catch_up_missed_wakes(self) -> bool:
        return as_bool(self.raw("wake", "catch_up_missed_wakes"), True)

    @property
    def timezone_name(self) -> str:
        return as_str(self.raw("wake", "timezone"), "").strip()

    # ------------------------------------------------------------------
    # limit
    # ------------------------------------------------------------------
    @property
    def max_consecutive_wakes(self) -> int:
        return as_int(self.raw("limit", "max_consecutive_wakes"), 3, -1, 100)

    @property
    def count_only_when_message_sent(self) -> bool:
        return as_bool(self.raw("limit", "count_only_when_message_sent"), False)

    # ------------------------------------------------------------------
    # mind
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # state / panel
    # ------------------------------------------------------------------
    @property
    def recent_sent_hours(self) -> float:
        """超过这个小时数就不再回放"上次主动说话内容"。"""
        return as_float(self.raw("state", "recent_sent_hours"), 6.0, 0.0, 168.0)

    @property
    def stale_hours(self) -> float:
        return as_float(self.raw("panel", "stale_hours"), 6.0, 0.0, 720.0)

    @property
    def trace_similarity(self) -> float:
        return as_float(self.raw("panel", "trace_similarity"), 0.85, 0.5, 1.0)

    # ------------------------------------------------------------------
    # 出站文本处理（模仿平台 segmented_reply 三件套）
    # ------------------------------------------------------------------
    @property
    def outbound_segment_enabled(self) -> bool:
        return as_bool(self.raw("outbound", "segment_enabled"), False)

    @property
    def outbound_segment_pattern(self) -> str:
        """分段正则（re.findall 语义）。留空=不分段。"""
        return as_str(self.raw("outbound", "segment_pattern"), "")

    @property
    def outbound_segment_max(self) -> int:
        return as_int(self.raw("outbound", "segment_max"), 4, 1, 20)

    @property
    def outbound_segment_interval(self) -> float:
        return as_float(self.raw("outbound", "segment_interval"), 0.6, 0.0, 30.0)

    @property
    def outbound_cleanup_enabled(self) -> bool:
        return as_bool(self.raw("outbound", "cleanup_enabled"), False)

    @property
    def outbound_cleanup_pattern(self) -> str:
        """内容过滤正则：删掉每段里匹配到的内容。留空=不过滤。"""
        return as_str(self.raw("outbound", "cleanup_pattern"), "")

    @property
    def outbound_replace_enabled(self) -> bool:
        return as_bool(self.raw("outbound", "replace_enabled"), True)

    @property
    def outbound_replace_rules(self) -> list[dict[str, str]]:
        """替换规则；配置可用 list，也可用每行 ``pattern=>replacement`` 的文本。"""
        from .textfx import DEFAULT_REPLACE_RULES, parse_rules

        raw = self.raw("outbound", "replace_rules")
        if raw is None:
            return [dict(rule) for rule in DEFAULT_REPLACE_RULES]
        # 旧版本被 str() 成 "[]" / "[{...}]" 这类字符串，尽量救回来
        if isinstance(raw, str):
            text = raw.strip()
            if text.startswith("[") and text.endswith("]"):
                try:
                    decoded = json.loads(text.replace("'", '"'))
                    if isinstance(decoded, list):
                        raw = decoded
                except Exception:
                    pass
        return parse_rules(raw)

    # ------------------------------------------------------------------
    # hobby / temp
    # ------------------------------------------------------------------
    @property
    def hobby_memory_count(self) -> int:
        return as_int(self.raw("hobby", "memory_count"), 30, 0, 100)

    @property
    def hobby_flow_count(self) -> int:
        return as_int(self.raw("hobby", "flow_count"), 20, 0, 100)

    @property
    def hobby_memory_tag(self) -> str:
        return as_str(self.raw("hobby", "memory_tag"), "proactive_hobby").strip() or "proactive_hobby"

    @property
    def hobby_keywords(self) -> list[str]:
        return as_lines(self.raw("hobby", "keywords")) or ["兴趣", "爱好", "喜欢", "想学"]

    @property
    def hobby_default_interval_minutes(self) -> int:
        """已废弃：爱好兜底改成在 hobby.min~max 之间随机，这里返回区间中点（兼容用）。"""
        low = self.hobby_min_interval_minutes
        high = self.hobby_max_interval_minutes
        return max(low, min(high, int((low + high) / 2)))

    @property
    def hobby_min_interval_minutes(self) -> int:
        return as_int(self.raw("hobby", "min_interval_minutes"), 30, 0, 4320)

    @property
    def hobby_max_interval_minutes(self) -> int:
        low = self.hobby_min_interval_minutes
        return as_int(self.raw("hobby", "max_interval_minutes"), 4320, low, 4320)

    @property
    def hobby_tool_whitelist(self) -> list[str]:
        return as_lines(self.raw("hobby", "tool_whitelist"))

    @property
    def hobby_scope_filter(self) -> bool:
        return as_bool(self.raw("hobby", "scope_filter"), False)

    @property
    def hobby_auto_start(self) -> bool:
        return as_bool(self.raw("hobby", "auto_start"), True)

    @property
    def temp_max_jobs(self) -> int:
        return as_int(self.raw("temp", "max_jobs"), 5, 1, 50)

    @property
    def temp_coalesce_seconds(self) -> int:
        return as_int(self.raw("temp", "coalesce_seconds"), 0, 0, 600)

    # ------------------------------------------------------------------
    # mind
    # ------------------------------------------------------------------
    @property
    def single_request(self) -> bool:
        """true=一轮只发一次请求（工具结果只进记忆，不回流定稿）。"""
        return as_bool(self.raw("mind", "single_request"), False)

    @property
    def catalog_max_entries(self) -> int:
        """提示词里最多给几个工具（0=不限）。"""
        return as_int(self.raw("mind", "catalog_max_entries"), 24, 0, 200)

    @property
    def catalog_char_budget(self) -> int:
        """工具目录的字符预算（0=不限）。"""
        return as_int(self.raw("mind", "catalog_char_budget"), 8000, 0, 60000)

    @property
    def tool_deny_pattern(self) -> str:
        """目录/执行额外剔除的工具名正则（留空=不额外剔除；黑名单仍生效）。"""
        default = (
            r"(^|_)(delete|remove|drop|purge|wipe|truncate|unlink|"
            r"kick|ban|mute)(_|$)|shutdown|future_task|manage_wake|"
            r"pmind_|send_message_to_user"
        )
        return as_str(self.raw("mind", "tool_deny_pattern"), default)

    @property
    def next_time_marker(self) -> bool:
        """是否解析并抠掉回复里的 [[pmind_next:...]] 标记。"""
        return as_bool(self.raw("basic", "next_time_marker"), True)

    @property
    def dialogue_execute(self) -> str:
        """对话 cron 由谁执行：main_agent（投递唤醒事件）| plugin（插件自己生成）。"""
        mode = as_str(self.raw("mind", "dialogue_execute"), "main_agent").strip().lower()
        return mode if mode in {"main_agent", "plugin"} else "main_agent"

    @property
    def temp_execute(self) -> str:
        """临时提醒到点由谁执行。"""
        mode = as_str(self.raw("mind", "temp_execute"), "main_agent").strip().lower()
        return mode if mode in {"main_agent", "plugin"} else "main_agent"

    @property
    def output_hint(self) -> str:
        """每轮注入的"结尾给时间+话题"提醒（空=不注入）。"""
        return as_str(self.raw("mind", "output_hint"), "")

    @property
    def wake_hint(self) -> str:
        """到点唤醒轮注入的提示模板，支持 {topic} / {since} / {kind}。"""
        return as_str(self.raw("mind", "wake_hint"), "")

    @property
    def wake_record_window_seconds(self) -> int:
        """记账只认最近 N 秒内的投递（秒）。"""
        return as_int(self.raw("mind", "wake_record_window_seconds"), 600, 60, 7200)

    @property
    def provisional_grace_seconds(self) -> int:
        """投递后等多久没回复才补垫保底（秒，0=不补）。"""
        return as_int(self.raw("mind", "provisional_grace_seconds"), 120, 0, 1800)

    @property
    def delivery_guard_seconds(self) -> int:
        """两次唤醒投递的最小间隔（秒）；0=不限制。"""
        return as_int(self.raw("mind", "delivery_guard_seconds"), 90, 0, 3600)

    @property
    def synthetic_admin(self) -> bool:
        """合成事件是否以管理员身份运行工具（默认开）。"""
        return as_bool(self.raw("mind", "synthetic_admin"), True)

    @property
    def provisional_enabled(self) -> bool:
        """交还主 Agent 时是否先垫保底时间（时间从随机区间里取）。"""
        return as_bool(self.raw("mind", "provisional_enabled"), True)

    @property
    def wake_random_min_minutes(self) -> int:
        """兜底随机下限 = 对话最短间隔。"""
        return self.min_interval_minutes

    @property
    def wake_random_max_minutes(self) -> int:
        """兜底随机上限 = 对话最长间隔。"""
        return self.max_interval_minutes

    @property
    def decision_provider_id(self) -> str:
        return as_str(self.raw("mind", "decision_provider_id"), "").strip()

    @property
    def llm_timeout_seconds(self) -> int:
        return as_int(self.raw("mind", "llm_timeout_seconds"), 90, 10, 300)

    @property
    def max_tool_calls(self) -> int:
        return as_int(self.raw("mind", "max_tool_calls"), 3, 0, 8)

    @property
    def tool_whitelist(self) -> list[str]:
        return as_lines(self.raw("mind", "tool_whitelist"))

    @property
    def tool_blacklist(self) -> list[str]:
        """黑名单 = 结构性保护项 + 配置项。

        ``BUILTIN_TOOL_BLACKLIST`` 里的两项牵涉调度与发送路径，始终屏蔽；
        其余（如 QQ 工具的回合控制/撤消息）只是默认值，用户可从配置里删掉。
        """
        configured = as_lines(self.raw("mind", "tool_blacklist"))
        merged: list[str] = []
        for name in list(BUILTIN_TOOL_BLACKLIST) + configured:
            if name and name not in merged:
                merged.append(name)
        return merged

    @property
    def tool_result_max_chars(self) -> int:
        return as_int(self.raw("mind", "tool_result_max_chars"), 2000, 200, 20000)

    @property
    def persona_extra_instruction(self) -> str:
        return as_str(self.raw("mind", "persona_extra_instruction"), "")

    @property
    def expose_wake_tool(self) -> bool:
        """是否把 pmind_wake 工具交给主 Agent。"""
        return as_bool(self.raw("mind", "expose_wake_tool"), True)

    @property
    def announce_wake_tool(self) -> bool:
        """是否在主 Agent 的系统提示里说明"延迟回复要用唤醒而不是原地等"。"""
        return as_bool(self.raw("mind", "announce_wake_tool"), True)

    # ------------------------------------------------------------------
    # memory
    # ------------------------------------------------------------------
    @property
    def write_memory(self) -> bool:
        return as_bool(self.raw("memory", "write_memory"), True)

    @property
    def memory_importance(self) -> float:
        return as_float(self.raw("memory", "memory_importance"), 0.7, 0.0, 1.0)

    @property
    def memory_tool_name(self) -> str:
        return (
            as_str(self.raw("memory", "memory_tool_name"), "memorize_long_term_memory").strip()
            or "memorize_long_term_memory"
        )

    @property
    def hobby_importance(self) -> float:
        return as_float(self.raw("memory", "hobby_importance"), 0.8, 0.0, 1.0)

    @property
    def memory_mode(self) -> str:
        """tool=主动调写入工具；reflection=只 echo 交给它的反思；both=两者都做。"""
        mode = as_str(self.raw("memory", "mode"), "tool").strip().lower()
        return mode if mode in {"tool", "reflection", "both"} else "tool"

    @property
    def recall_tool_name(self) -> str:
        return (
            as_str(self.raw("memory", "recall_tool_name"), "recall_long_term_memory").strip()
            or "recall_long_term_memory"
        )

    # ------------------------------------------------------------------
    # echo
    # ------------------------------------------------------------------
    @property
    def persist_platform(self) -> bool:
        return as_bool(self.raw("echo", "persist_proactive_to_platform_history"), True)

    @property
    def persist_livingmemory(self) -> bool:
        return as_bool(self.raw("echo", "persist_proactive_to_livingmemory"), True)

    @property
    def trigger_reflection(self) -> bool:
        return as_bool(self.raw("echo", "livingmemory_write_trigger_reflection"), False)

    @property
    def echo_bot_name(self) -> str:
        return as_str(self.raw("echo", "echo_bot_name"), "").strip()

    @property
    def dedupe_echo_in_flow(self) -> bool:
        return as_bool(self.raw("echo", "dedupe_echo_in_flow"), True)

    # ------------------------------------------------------------------
    # message / debug
    # ------------------------------------------------------------------
    @property
    def max_message_chars(self) -> int:
        return as_int(self.raw("message", "max_message_chars"), 800, 100, 4000)

    @property
    def allow_empty_message(self) -> bool:
        return as_bool(self.raw("message", "allow_empty_message"), True)

    @property
    def debug_log_prompts(self) -> bool:
        return as_bool(self.raw("debug", "debug_log_prompts"), False)

    @property
    def trace_keep(self) -> int:
        return as_int(self.raw("debug", "trace_keep"), 30, 5, 200)
