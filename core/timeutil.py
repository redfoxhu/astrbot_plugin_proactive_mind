"""时间解析与钳制工具。

只依赖标准库，便于单测在无 AstrBot 环境下直接导入。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_REL_RE = re.compile(r"^\+?\s*(\d+(?:\.\d+)?)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)?$", re.I)
_CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")
_MONTH_DAY_RE = re.compile(r"^(\d{1,2})[-/](\d{1,2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?$")

_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

_REL_COMPOUND_RE = re.compile(
    r"^\+?\s*(?:(\d+(?:\.\d+)?)\s*(?:个?\s*天|个?\s*日|d)\s*)?"
    r"(?:(\d+(?:\.\d+)?)\s*(?:个?\s*(?:小时|钟头)|h|hr|hrs|hour|hours)\s*)?"
    r"(?:(\d+(?:\.\d+)?)\s*(?:分钟|分|m|min|mins|minute|minutes)?\s*)?$",
    re.I,
)
_REL_UNITS = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
}


def resolve_tz(name: str | None):
    """解析时区名；非法或空值回退到本机时区。"""
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(str(name).strip())
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo


def now(tz=None) -> datetime:
    """当前时间（带时区）。"""
    return datetime.now(tz or resolve_tz(None))


def iso(value: datetime | None) -> str | None:
    """序列化为秒级 ISO8601 字符串。"""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=resolve_tz(None))
    return value.isoformat(timespec="seconds")


def from_iso(text: str | None) -> datetime | None:
    """解析 ISO 字符串，失败返回 None。"""
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=resolve_tz(None))
    return parsed


def human(value: datetime | None) -> str:
    """人类可读时间，带中文星期。"""
    if value is None:
        return "（未安排）"
    return f"{value.strftime('%Y-%m-%d %H:%M')} {_WEEKDAY_CN[value.weekday()]}"


def _attach_tz(value: datetime, tz) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=tz)
    return value.astimezone(tz)


def parse_wake_time(value, *, reference: datetime, tz=None) -> datetime | None:
    """把大模型给出的时间点解析成带时区的 datetime。

    支持：ISO8601（带/不带时区、带 T 或空格）、``YYYY-MM-DD HH:MM``、
    ``HH:MM``（今天或明天）、``MM-DD HH:MM``（今年或明年）、相对量
    （``+30m`` / ``90`` / ``2h`` / ``1d``）、以及 epoch 秒。

    Args:
        value: 原始值（字符串/数字/None）。
        reference: 参照的当前时间（带时区）。
        tz: 目标时区，默认沿用 reference 的时区。

    Returns:
        解析成功的时间点；无法解析返回 None。
    """
    if value is None:
        return None
    tz = tz or reference.tzinfo or resolve_tz(None)

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        if seconds > 1e11:  # 毫秒时间戳
            seconds /= 1000.0
        if seconds > 1e9:
            try:
                return datetime.fromtimestamp(seconds, tz=tz)
            except (OverflowError, OSError, ValueError):
                return None
        return None

    text = str(value).strip()
    if not text:
        return None
    text = text.replace("T", " ") if " " not in text and "T" in text else text

    # 复合相对量：2h30m / 2小时30分 / +1h45 / 1天2小时
    compound = _REL_COMPOUND_RE.match(text)
    if compound:
        day_part, hour_part, minute_part = compound.groups()
        if any(part is not None for part in (day_part, hour_part, minute_part)):
            total = timedelta()
            for amount, unit_seconds in (
                (day_part, 86400),
                (hour_part, 3600),
                (minute_part, 60),
            ):
                if amount is not None:
                    total += timedelta(seconds=float(amount) * unit_seconds)
            if total:
                return reference + total

    # 相对量
    match = _REL_RE.match(text)
    if match:
        amount = float(match.group(1))
        unit = (match.group(2) or "m").lower()
        delta = timedelta(seconds=amount * _REL_UNITS.get(unit, 60))
        return reference + delta

    lowered = text.lower()
    if lowered in {"now", "现在", "马上", "立刻"}:
        return reference
    if lowered in {"tomorrow", "明天"}:
        return reference + timedelta(days=1)

    # 纯时钟 HH:MM
    clock = _CLOCK_RE.match(text)
    if clock:
        hour = int(clock.group(1))
        minute = int(clock.group(2))
        second = int(clock.group(3) or 0)
        if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
            return None
        candidate = reference.replace(hour=hour, minute=minute, second=second, microsecond=0)
        if candidate <= reference:
            candidate += timedelta(days=1)
        return candidate

    # MM-DD HH:MM
    month_day = _MONTH_DAY_RE.match(text)
    if month_day:
        month = int(month_day.group(1))
        day = int(month_day.group(2))
        hour = int(month_day.group(3))
        minute = int(month_day.group(4))
        second = int(month_day.group(5) or 0)
        try:
            candidate = datetime(
                reference.year, month, day, hour, minute, second, tzinfo=None
            )
        except ValueError:
            return None
        candidate = _attach_tz(candidate, tz)
        if candidate <= reference:
            candidate = candidate.replace(year=reference.year + 1)
        return candidate

    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except Exception:
        parsed = None
    if parsed is not None:
        return _attach_tz(parsed, tz)

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y.%m.%d %H:%M",
        "%Y年%m月%d日 %H:%M",
        "%Y/%m/%d %H:%M",
    ):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return _attach_tz(parsed, tz)
    return None


def clamp_wake(
    value: datetime | None,
    *,
    reference: datetime,
    min_minutes: int,
    max_minutes: int,
    default_minutes: int,
) -> datetime:
    """把时间点钳制到 [reference+min, reference+max]，缺失时用默认间隔。"""
    min_seconds = max(1, int(min_minutes)) * 60
    max_seconds = max(min_seconds, int(max_minutes) * 60)
    default_seconds = max(1, int(default_minutes)) * 60

    if value is None:
        return reference + timedelta(seconds=default_seconds)
    if value.tzinfo is None:
        value = value.replace(tzinfo=reference.tzinfo)

    delta = (value - reference).total_seconds()
    if delta < min_seconds:
        return reference + timedelta(seconds=min_seconds)
    if delta > max_seconds:
        return reference + timedelta(seconds=max_seconds)
    return value


def plus_minutes(value: datetime, minutes: int) -> datetime:
    """在时间点上加分钟。"""
    return value + timedelta(minutes=int(minutes))


def explicit_target(
    when: datetime | None,
    *,
    now: datetime,
    max_minutes: int,
    min_seconds: int = 5,
    delay_minutes: int | None = None,
) -> datetime | None:
    """把「显式指定的时间点」规范化（主 Agent 用 pmind_wake 排的唤醒）。

    与自主判断的区别：这里**不套用最短间隔**（用户说 2 分钟就是 2 分钟），
    只保证不早于 ``now+min_seconds``、不晚于 ``now+max_minutes``；
    ``delay_minutes`` 仅用于"本会话延后覆盖"，全局延后不参与。

    Args:
        when: 目标时间点（可为 naive，按 now 的时区解释）。
        now: 当前时间。
        max_minutes: 最远允许多少分钟。
        min_seconds: 最近允许多少秒（避免立刻触发）。
        delay_minutes: 本会话的延后覆盖（分钟），None 表示不延后。

    Returns:
        规范化后的时间点；``when`` 为 None 时返回 None。
    """
    if when is None:
        return None
    target = when
    if target.tzinfo is None:
        target = target.replace(tzinfo=now.tzinfo)
    floor = now + timedelta(seconds=max(1, int(min_seconds)))
    if target < floor:
        target = floor
    ceiling = now + timedelta(minutes=max(1, int(max_minutes)))
    if target > ceiling:
        target = ceiling
    if delay_minutes:
        try:
            target = target + timedelta(minutes=max(0, int(delay_minutes)))
        except (TypeError, ValueError):
            pass
    return target


def now_iso(tz=None) -> str | None:
    """当前时间 ISO 字符串。"""
    return iso(now(tz))


# ----------------------------------------------------------------------
# 回复里的"下次时间"标记（供主 Agent 用，装饰阶段会被抠掉，用户看不到）
# ----------------------------------------------------------------------
NEXT_MARKER_PATTERN = re.compile(
    r"\[\[\s*pmind[_-]?next\s*[:：]\s*([^\[\]]+?)\s*\]\]",
    re.IGNORECASE,
)


def split_marker(value: str) -> tuple[str, str]:
    """拆开 ``时间|话题``（支持全角竖线）。

    Returns:
        ``(时间串, 话题)``；没有竖线时话题为空。
    """
    text = str(value or "").strip()
    for sep in ("|", "｜", "¦"):
        if sep in text:
            head, _, tail = text.partition(sep)
            return head.strip(), tail.strip()
    return text, ""


def strip_next_marker(text: str) -> tuple[str, str | None]:
    """删除 ``[[pmind_next:+90m]]`` 标记。

    Returns:
        ``(清理后的文本, 标记里的原始时间值)``；有多个标记时取最后一个。
    """
    if not text or "pmind" not in text.lower():
        return text, None
    found: list[str] = []

    def _take(match: re.Match[str]) -> str:
        found.append(match.group(1).strip())
        return ""

    cleaned = NEXT_MARKER_PATTERN.sub(_take, text)
    if not found:
        return text, None
    # 标记被删掉后可能留下多余空白
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    return cleaned, found[-1]


def utc_iso(value: datetime | None) -> str | None:
    """转成 UTC ISO（便于跨时区比较）。"""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=resolve_tz(None))
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")
