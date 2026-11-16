"""配置读取、类型归一与边界钳制。

所有配置项都通过本模块访问，保证：
1. UI 里可能出现的字符串/空值不会让决策流程崩溃；
2. 关键范围（连续唤醒上限、间隔、条数）在运行时再次钳制；
3. 单测可以不依赖 AstrBot 的 AstrBotConfig。
"""

from __future__ import annotations

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
        "debounce_seconds": 3,
        "post_reply_delay_seconds": 2,
    },
    "flow": {
        "flow_source": "livingmemory",
        "flow_message_count": 50,
        "follow_livingmemory_window": True,
        "livingmemory_plugin_dir": "astrbot_plugin_livingmemory",
        "flow_max_chars": 12000,
    },
    "wake": {
        "wake_delay_minutes": 0,
        "default_interval_minutes": 120,
        "min_interval_minutes": 5,
        "max_interval_minutes": 1440,
        "catch_up_missed_wakes": True,
        "timezone": "",
    },
    "limit": {
        "max_consecutive_wakes": 3,
        "count_only_when_message_sent": False,
    },
    "mind": {
        "decision_provider_id": "",
        "llm_timeout_seconds": 90,
        "max_tool_calls": 3,
        "tool_whitelist": "",
        "tool_blacklist": (
            "future_task\nsend_message_to_user\n"
            "qts_stop_conversation\nqts_refresh_messages\nqts_delete_message"
        ),
        "tool_result_max_chars": 2000,
        "persona_extra_instruction": "",
    },
    "memory": {
        "write_memory": True,
        "memory_importance": 0.7,
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
        mode = as_str(self.raw("basic", "user_message_mode"), "decide").strip().lower()
        return mode if mode in {"decide", "schedule_only"} else "decide"

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

    # ------------------------------------------------------------------
    # wake
    # ------------------------------------------------------------------
    @property
    def wake_delay_minutes(self) -> int:
        return as_int(self.raw("wake", "wake_delay_minutes"), 0, 0, 1440)

    @property
    def default_interval_minutes(self) -> int:
        return as_int(self.raw("wake", "default_interval_minutes"), 120, 5, 1440)

    @property
    def min_interval_minutes(self) -> int:
        return as_int(self.raw("wake", "min_interval_minutes"), 5, 1, 1440)

    @property
    def max_interval_minutes(self) -> int:
        low = self.min_interval_minutes
        return as_int(self.raw("wake", "max_interval_minutes"), 1440, low, 10080)

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
