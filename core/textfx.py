"""出站文本处理（模仿平台 `platform_settings.segmented_reply` 的三件套）。

1. **分段正则**：``re.findall(pattern, text)`` 把一段回复切成多条消息；
2. **内容过滤正则**：把每个分段里匹配到的内容删掉（平台叫 content_cleanup_rule）；
3. **替换规则**：正则 → 替换文本（默认把中文逗号换成 3 个空格）。

全部纯函数，方便单测；规则写坏了自动降级（原样返回，绝不吞消息）。
"""

from __future__ import annotations

import logging
import re
from typing import Any

LOGGER_NAME = "astrbot"

DEFAULT_SPLIT_PATTERN = ""
DEFAULT_CLEANUP_PATTERN = ""
# 默认替换规则与平台 regex_filter 对齐：
#   replace_rules: ，→ 3 个空格
#   delete_rules : 删除 *
DEFAULT_REPLACE_RULES: list[dict[str, str]] = [
    {"pattern": "，", "replacement": "   "},
    {"pattern": r"\*", "replacement": ""},
]


def _compile(pattern: str, log: logging.Logger | None = None) -> re.Pattern[str] | None:
    text = str(pattern or "")
    if not text.strip():
        return None
    try:
        return re.compile(text, re.DOTALL)
    except re.error as exc:
        (log or logging.getLogger(LOGGER_NAME)).warning(
            f"[proactive_mind] 出站正则非法，已忽略 {text!r}: {exc}"
        )
        return None


def split_text(text: str, pattern: str, *, max_segments: int = 4, log=None) -> list[str]:
    """按"分段正则"切分（等价于平台的 ``re.findall`` 语义）。"""
    value = (text or "").strip()
    if not value:
        return []
    regex = _compile(pattern, log)
    if regex is None:
        return [value]
    try:
        pieces = [piece.strip() for piece in regex.findall(value)]
    except Exception:  # pragma: no cover
        return [value]
    pieces = [piece for piece in pieces if piece]
    if not pieces:
        return [value]
    limit = max(1, int(max_segments or 4))
    if len(pieces) > limit:
        # 多出来的合并到最后一段，避免丢字
        head = pieces[: limit - 1]
        tail = "".join(pieces[limit - 1 :])
        pieces = head + [tail]
    return pieces


def cleanup_text(text: str, pattern: str, *, log=None) -> str:
    """"内容过滤正则"：删掉匹配内容。"""
    value = text or ""
    regex = _compile(pattern, log)
    if regex is None:
        return value
    try:
        return regex.sub("", value).strip()
    except Exception:  # pragma: no cover
        return value


def apply_replacements(
    text: str,
    rules: list[dict[str, Any]] | list[Any] | None,
    *,
    log=None,
) -> str:
    """按"替换规则"逐条替换（pattern → replacement）。"""
    value = text or ""
    for rule in rules or []:
        if isinstance(rule, dict):
            pattern = rule.get("pattern") or rule.get("regex") or ""
            replacement = rule.get("replacement")
            if replacement is None:
                replacement = rule.get("replace", "")
        elif isinstance(rule, (list, tuple)) and len(rule) >= 1:
            pattern = rule[0]
            replacement = rule[1] if len(rule) > 1 else ""
        else:
            continue
        regex = _compile(str(pattern), log)
        if regex is None:
            continue
        try:
            value = regex.sub("" if replacement is None else str(replacement), value)
        except Exception:  # pragma: no cover
            continue
    return value


def transform(text: str, settings, *, log=None) -> list[str]:
    """按配置把一段文本变成"要依次发出的若干条消息"。"""
    value = (text or "").strip()
    if not value:
        return []
    if settings is not None:
        if getattr(settings, "outbound_replace_enabled", False):
            value = apply_replacements(
                value, getattr(settings, "outbound_replace_rules", []), log=log
            )
        if getattr(settings, "outbound_segment_enabled", False):
            segments = split_text(
                value,
                getattr(settings, "outbound_segment_pattern", ""),
                max_segments=getattr(settings, "outbound_segment_max", 4),
                log=log,
            )
        else:
            segments = [value]
        if getattr(settings, "outbound_cleanup_enabled", False):
            pattern = getattr(settings, "outbound_cleanup_pattern", "")
            segments = [cleanup_text(item, pattern, log=log) for item in segments]
    else:
        segments = [value]
    segments = [item.strip() for item in segments if item and item.strip()]
    return segments


def parse_rules(raw: Any) -> list[dict[str, str]]:
    """把配置里的替换规则读成 ``[{pattern, replacement}]``。

    支持两种写法：
    * 列表（配置里直接是 list[dict] 或 list[list]）
    * 文本：每行 ``pattern=>replacement``（`=>` 或 `||` 分隔）
    """
    rules: list[dict[str, str]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                pattern = item.get("pattern") or item.get("regex") or ""
                if pattern:
                    rules.append(
                        {
                            "pattern": str(pattern),
                            "replacement": str(item.get("replacement", "")),
                        }
                    )
            elif isinstance(item, (list, tuple)) and item:
                rules.append(
                    {
                        "pattern": str(item[0]),
                        "replacement": str(item[1]) if len(item) > 1 else "",
                    }
                )
        return rules
    text = str(raw or "")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for sep in ("=>", "||", "\t"):
            if sep in line:
                pattern, _, replacement = line.partition(sep)
                if pattern.strip():
                    rules.append(
                        {"pattern": pattern.strip(), "replacement": replacement.strip()}
                    )
                break
    return rules
