"""爱好记忆的读取（只读 LivingMemory 的 ``livingmemory.db``）。

检索顺序（按需求）：
1. 近 N 条带"爱好标记"的记忆（``metadata.topics`` 含 tag，默认 ``proactive_hobby``）；
2. 不足时按关键词补（``hobby.keywords``：兴趣/爱好/喜欢/想学…）；
3. 仍不足时用近期记忆补满（按 created_at 倒序）。

只读连接、不改任何数据；失败一律降级为空列表，不影响主流程。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from typing import Any

LOGGER_NAME = "astrbot"
MAX_TEXT_CHARS = 400


@dataclass
class HobbyMemory:
    """一条记忆（用于展示与提示词）。"""

    mem_id: Any
    text: str
    topics: list[str]
    importance: float | None
    created_at: str | None
    via: str  # tag / keyword / recent

    def render(self) -> str:
        topics = f"（{','.join(self.topics[:4])}）" if self.topics else ""
        when = f"[{self.created_at[:16]}] " if self.created_at else ""
        text = self.text.strip().replace("\n", " ")
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS] + "…"
        return f"- {when}{text}{topics}"


def _db_candidates(settings) -> list[str]:
    """livingmemory.db 的候选路径。"""
    names = [settings.livingmemory_plugin_dir]
    if "astrbot_plugin_livingmemory" not in names:
        names.append("astrbot_plugin_livingmemory")
    paths: list[str] = []
    for name in names:
        try:
            from astrbot.core.star.star_tools import StarTools

            paths.append(str(StarTools.get_data_dir(name) / "livingmemory.db"))
        except Exception:
            pass
        try:
            from pathlib import Path

            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

            paths.append(str(Path(str(get_astrbot_plugin_data_path())) / name / "livingmemory.db"))
        except Exception:
            pass
    unique: list[str] = []
    for path in paths:
        if path not in unique:
            unique.append(path)
    return unique


def _parse_meta(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _query(db_path: str, sql: str, params: tuple) -> list[tuple]:
    uri = "file:" + db_path.replace("\\", "/") + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def scan(db_path: str, *, tag: str, keywords: list[str], limit: int) -> list[HobbyMemory]:
    """同步扫描（在 to_thread 里跑）。"""
    picked: list[HobbyMemory] = []
    seen: set[str] = set()
    limit = max(0, int(limit))
    if limit == 0:
        return picked

    def take(rows: list[tuple], via: str) -> None:
        for row in rows:
            mem_id, text, metadata, created_at = row[0], row[1], row[2], row[3]
            key = str(mem_id)
            if key in seen:
                continue
            meta = _parse_meta(metadata)
            if str(meta.get("status") or "active") == "archived":
                continue
            topics = [str(t) for t in (meta.get("topics") or []) if str(t).strip()]
            try:
                importance = float(meta.get("importance")) if meta.get("importance") is not None else None
            except (TypeError, ValueError):
                importance = None
            picked.append(
                HobbyMemory(
                    mem_id=mem_id,
                    text=str(text or ""),
                    topics=topics,
                    importance=importance,
                    created_at=str(created_at) if created_at else None,
                    via=via,
                )
            )
            seen.add(key)
            if len(picked) >= limit:
                return

    base = (
        "SELECT id, text, metadata, created_at FROM documents "
        "WHERE COALESCE(json_extract(metadata, '$.status'), 'active') != 'archived' "
    )

    # 1) 带爱好标记
    try:
        rows = _query(
            db_path,
            base
            + "AND (metadata LIKE ? OR metadata LIKE ?) "
            "ORDER BY created_at DESC LIMIT ?",
            (f'%"{tag}"%', f'%{tag}%', limit),
        )
        take(rows, "tag")
    except Exception:
        pass

    # 2) 关键词补（只搜 metadata：topics/key_facts；搜正文会把"喜欢/想"这类高频词
    #    带进一堆跟爱好无关的记忆，实测 30 条里 28 条是噪声）
    if len(picked) < limit and keywords:
        like_clause = " OR ".join(["metadata LIKE ?"] * len(keywords))
        params: list[Any] = []
        for word in keywords[:8]:
            params.append(f"%{word}%")
        params.append(limit - len(picked) + len(seen))
        try:
            rows = _query(
                db_path,
                base + f"AND ({like_clause}) ORDER BY created_at DESC LIMIT ?",
                tuple(params),
            )
            take(rows, "keyword")
        except Exception:
            pass

    # 3) 近期补
    if len(picked) < limit:
        try:
            rows = _query(
                db_path,
                base + "ORDER BY created_at DESC LIMIT ?",
                (limit - len(picked) + len(seen),),
            )
            take(rows, "recent")
        except Exception:
            pass
    return picked


async def read_hobby_memories(
    settings,
    *,
    limit: int | None = None,
    log: logging.Logger | None = None,
) -> tuple[list[HobbyMemory], dict[str, Any]]:
    """异步读取爱好记忆，返回 ``(items, stats)``。"""
    log = log or logging.getLogger(LOGGER_NAME)
    tag = settings.hobby_memory_tag
    keywords = settings.hobby_keywords
    count = settings.hobby_memory_count if limit is None else limit
    stats: dict[str, Any] = {"requested": count, "tag": tag, "source": None}

    for db_path in _db_candidates(settings):
        if not os.path.exists(db_path):
            continue
        try:
            items = await asyncio.to_thread(
                scan, db_path, tag=tag, keywords=keywords, limit=count
            )
        except Exception as exc:
            log.debug(f"[proactive_mind] 读取爱好记忆失败 {db_path}: {exc}")
            continue
        if items:
            stats.update(
                {
                    "source": db_path,
                    "count": len(items),
                    "by": {
                        via: sum(1 for item in items if item.via == via)
                        for via in ("tag", "keyword", "recent")
                    },
                }
            )
            return items, stats
        stats["source"] = db_path
    stats["count"] = 0
    return [], stats


def render_memories(items: list[HobbyMemory], max_chars: int = 6000) -> str:
    """渲染成提示词块（旧的先丢，保留最近的）。"""
    if not items:
        return "（暂无可用记忆）"
    lines = [item.render() for item in items]
    joined = "\n".join(lines)
    if len(joined) <= max_chars:
        return joined
    kept: list[str] = []
    total = 0
    for line in reversed(lines):
        if total + len(line) + 1 > max_chars and kept:
            break
        kept.append(line)
        total += len(line) + 1
    kept.reverse()
    return "（更早的记忆已省略）\n" + "\n".join(kept)
