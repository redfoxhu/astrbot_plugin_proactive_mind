"""主动心智 v0.4 单元测试。

覆盖：时间/配置/状态迁移/三类作业/流水/记忆/上下文唯一性/决策流水线/调度/面板辅助。

运行：
    cd <plugin_dir>
    python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core import context as ctx_mod  # noqa: E402
from core import decide as decide_mod  # noqa: E402
from core import delivery, echo, flow, jobs as jobs_mod, llm, memory, memorydb, timeutil  # noqa: E402
from core.settings import Settings  # noqa: E402
from core.state import StateStore  # noqa: E402

TZ = timezone(timedelta(hours=8))
UMO = "aiocqhttp:FriendMessage:1"


def make_settings(**overrides) -> Settings:
    cfg: dict = {}
    for key, value in overrides.items():
        if "." not in key:
            raise ValueError(f"配置键需要用 section.key 形式：{key}")
        section, name = key.split(".", 1)
        cfg.setdefault(section, {})[name] = value
    return Settings(cfg)


# ----------------------------------------------------------------------
# 时间
# ----------------------------------------------------------------------
class TimeutilTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 11, 18, 12, 0, tzinfo=TZ)

    def test_long_marker_is_parsed(self) -> None:
        """标记内容超过 64 字也要能解析（模型爱写长话题）。"""
        long_topic = "接着从收藏夹列表里点开子收藏夹，挑两个有料的原图看标题、用真正看懂B站视频解析，截图发给主人并锐评，别再忘标记"
        text = f"正文一句话。\n\n[[pmind_next:2026-11-20 22:10|{long_topic}]]"
        cleaned, value = timeutil.strip_next_marker(text)
        self.assertIsNotNone(value)
        self.assertNotIn("pmind_next", cleaned)
        hint, topic = timeutil.split_marker(value)
        self.assertEqual(hint, "2026-11-20 22:10")
        self.assertEqual(topic, long_topic)
        self.assertGreater(len(value), 64)

    def test_parse_compound_offsets(self) -> None:
        """模型常写 +2h30m / +2小时30分：必须能解析成 +150 分钟。"""
        now = timeutil.now(TZ)
        for raw in ("+2h30m", "2h30m", "+2小时30分", "+150m", "+2.5h"):
            self.assertIsNotNone(timeutil.parse_wake_time(raw, reference=now, tz=TZ), raw)
        minutes = (
            timeutil.parse_wake_time("+2h30m", reference=now, tz=TZ) - now
        ).total_seconds() / 60
        self.assertAlmostEqual(minutes, 150, delta=1)
        minutes2 = (
            timeutil.parse_wake_time("+1h45", reference=now, tz=TZ) - now
        ).total_seconds() / 60
        self.assertAlmostEqual(minutes2, 105, delta=1)

    def test_parse_variants(self) -> None:
        self.assertEqual(
            timeutil.parse_wake_time("+90m", reference=self.now, tz=TZ),
            self.now + timedelta(minutes=90),
        )
        self.assertEqual(
            timeutil.parse_wake_time("2h", reference=self.now, tz=TZ),
            self.now + timedelta(hours=2),
        )
        parsed = timeutil.parse_wake_time("2026-11-18T20:30:00+08:00", reference=self.now, tz=TZ)
        self.assertEqual(parsed.hour, 20)
        self.assertIsNone(timeutil.parse_wake_time("随便吧", reference=self.now, tz=TZ))

    def test_clamp(self) -> None:
        early = timeutil.clamp_wake(
            self.now + timedelta(seconds=1),
            reference=self.now,
            min_minutes=5,
            max_minutes=60,
            default_minutes=30,
        )
        self.assertEqual(early, self.now + timedelta(minutes=5))
        missing = timeutil.clamp_wake(
            None, reference=self.now, min_minutes=5, max_minutes=60, default_minutes=30
        )
        self.assertEqual(missing, self.now + timedelta(minutes=30))

    def test_explicit_target(self) -> None:
        target = timeutil.explicit_target(
            self.now + timedelta(minutes=2), now=self.now, max_minutes=1440
        )
        self.assertEqual(target, self.now + timedelta(minutes=2))
        self.assertEqual(
            timeutil.explicit_target(
                self.now + timedelta(minutes=2), now=self.now, max_minutes=1440, delay_minutes=3
            ),
            self.now + timedelta(minutes=5),
        )
        self.assertIsNone(timeutil.explicit_target(None, now=self.now, max_minutes=60))


# ----------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------
class SettingsTests(unittest.TestCase):
    def test_defaults(self) -> None:
        settings = Settings({})
        self.assertEqual(settings.flow_message_count, 50)
        self.assertEqual(settings.hobby_memory_count, 30)
        self.assertEqual(settings.hobby_flow_count, 20)
        self.assertEqual(settings.hobby_memory_tag, "proactive_hobby")
        self.assertEqual(settings.memory_mode, "tool")
        self.assertEqual(settings.recent_sent_hours, 6.0)
        self.assertFalse(settings.single_request)
        self.assertIn("兴趣", settings.hobby_keywords)

    def test_blacklist_builtin(self) -> None:
        settings = make_settings(**{"mind.tool_blacklist": "bilibili_read"})
        self.assertIn("future_task", settings.tool_blacklist)
        self.assertIn("send_message_to_user", settings.tool_blacklist)
        self.assertIn("bilibili_read", settings.tool_blacklist)

    def test_update_typed(self) -> None:
        raw: dict = {}
        settings = Settings(raw)
        applied = settings.update(
            {"hobby.memory_count": "12", "temp.max_jobs": 8, "unknown.key": 1}
        )
        self.assertEqual(sorted(applied), ["hobby.memory_count", "temp.max_jobs"])
        self.assertEqual(raw["hobby"]["memory_count"], 12)
        self.assertEqual(settings.temp_max_jobs, 8)

    def test_update_keeps_list_structure(self) -> None:
        """列表配置不能被 str() 成 "[]"（曾把替换规则整条弄丢）。"""
        raw: dict = {}
        settings = Settings(raw)
        rules = [{"pattern": "[，,]", "replacement": "   "}]
        settings.update({"outbound.replace_rules": rules})
        self.assertIsInstance(raw["outbound"]["replace_rules"], list)
        self.assertEqual(settings.outbound_replace_rules, rules)

        # 旧版本存成字符串 "[]" 时也要能读回来（空列表，不报错）
        raw2: dict = {"outbound": {"replace_rules": "[]"}}
        self.assertEqual(Settings(raw2).outbound_replace_rules, [])
        # 存成 "[{...}]" 的字符串也要救回来
        raw3: dict = {
            "outbound": {
                "replace_rules": "[{'pattern': '[，,]', 'replacement': '   '}]"
            }
        }
        self.assertEqual(
            Settings(raw3).outbound_replace_rules,
            [{"pattern": "[，,]", "replacement": "   "}],
        )

    def test_user_message_mode_values(self) -> None:
        self.assertEqual(
            make_settings(**{"basic.user_message_mode": "record_only"}).user_message_mode,
            "record_only",
        )
        self.assertEqual(
            make_settings(**{"basic.user_message_mode": "schedule_only"}).user_message_mode,
            "schedule_only",
        )
        # 非法值退回 decide
        self.assertEqual(
            make_settings(**{"basic.user_message_mode": "???"}).user_message_mode, "decide"
        )

    def test_single_request_flag(self) -> None:
        self.assertTrue(make_settings(**{"mind.single_request": "true"}).single_request)


# ----------------------------------------------------------------------
# 状态与迁移
# ----------------------------------------------------------------------
class StateTests(unittest.IsolatedAsyncioTestCase):
    async def test_defaults_and_persist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = StateStore(path, trace_keep=5)
            await store.load()
            session = store.session(UMO)
            self.assertIn("jobs", session)
            self.assertIn("dialogue", session["jobs"])
            store.set(UMO, silent_wake_count=2)
            await store.save()

            reloaded = StateStore(path, trace_keep=5)
            await reloaded.load()
            self.assertEqual(reloaded.get(UMO, "silent_wake_count"), 2)

    async def test_trace_trim_and_kind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.json", trace_keep=5)
            await store.load()
            for index in range(8):
                trace = store.new_trace(UMO, "hobby_cron", jobs_mod.KIND_HOBBY)
                trace["reason"] = f"n{index}"
                store.add_trace(trace)
            traces = store.traces()
            self.assertEqual(len(traces), 5)
            self.assertEqual(traces[0]["kind"], jobs_mod.KIND_HOBBY)

    async def test_migrate_v1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            v1 = {
                "version": 1,
                "sessions": {
                    UMO: {
                        "next_wake_at": "2026-11-18T20:00:00+08:00",
                        "explicit_wake_at": None,
                        "pending_note": "去刷会儿视频",
                        "silent_wake_count": 3,
                    }
                },
                "traces": [],
            }
            path.write_text(json.dumps(v1, ensure_ascii=False), encoding="utf-8")
            store = StateStore(path, trace_keep=5)
            await store.load()
            session = store.session(UMO)
            self.assertEqual(session["jobs"]["dialogue"]["next_at"], "2026-11-18T20:00:00+08:00")
            self.assertEqual(session["jobs"]["hobby"]["intent"], "去刷会儿视频")
            self.assertEqual(session["jobs"]["temp"], [])
            self.assertEqual(session["silent_wake_count"], 3)
            self.assertTrue(any(Path(tmp).glob("state.json.bak-v1-*")))

    async def test_migrate_v1_explicit_to_temp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            v1 = {
                "version": 1,
                "sessions": {
                    UMO: {
                        "explicit_wake_at": "2026-11-18T21:00:00+08:00",
                        "pending_note": "两分钟后回复他",
                    }
                },
                "traces": [],
            }
            path.write_text(json.dumps(v1, ensure_ascii=False), encoding="utf-8")
            store = StateStore(path, trace_keep=5)
            await store.load()
            jobs = store.session(UMO)["jobs"]
            self.assertEqual(len(jobs["temp"]), 1)
            self.assertEqual(jobs["temp"][0]["note"], "两分钟后回复他")
            self.assertIsNone(jobs["dialogue"]["next_at"])


# ----------------------------------------------------------------------
# 作业模型
# ----------------------------------------------------------------------
class JobsTests(unittest.TestCase):
    def test_defaults_and_ensure(self) -> None:
        session: dict = {}
        jobs = jobs_mod.ensure_jobs(session)
        self.assertEqual(jobs["dialogue"]["next_at"], None)
        self.assertEqual(jobs["temp"], [])
        jobs["temp"] = "bad"
        jobs_mod.ensure_jobs(session)
        self.assertEqual(session["jobs"]["temp"], [])

    def test_temp_sort_and_find(self) -> None:
        jobs = jobs_mod.default_jobs()
        jobs["temp"] = [
            {"id": "b", "next_at": "2026-11-18T22:00:00+08:00"},
            {"id": "a", "next_at": "2026-11-18T21:00:00+08:00"},
        ]
        self.assertEqual([i["id"] for i in jobs_mod.sort_temp(jobs)], ["a", "b"])
        self.assertEqual(jobs_mod.find_temp(jobs, "a")["id"], "a")
        self.assertIsNone(jobs_mod.find_temp(jobs, "zzz"))

    def test_summarize_for_prompt(self) -> None:
        now = datetime(2026, 11, 18, 12, 0, tzinfo=TZ)
        jobs = jobs_mod.default_jobs()
        jobs["dialogue"]["next_at"] = "2026-11-18T14:00:00+08:00"
        jobs["hobby"]["next_at"] = "2026-11-18T18:00:00+08:00"
        jobs["hobby"]["runs"] = 2
        jobs["temp"] = [{"id": "t1", "next_at": "2026-11-18T13:00:00+08:00", "note": "x"}]
        text = jobs_mod.summarize_for_prompt(jobs, now)
        self.assertIn("对话节奏", text)
        self.assertIn("已执行 2 轮", text)
        self.assertIn("临时提醒：1 个", text)


# ----------------------------------------------------------------------
# 流水
# ----------------------------------------------------------------------
class FlowTests(unittest.TestCase):
    def test_content_and_records(self) -> None:
        self.assertEqual(
            flow.content_to_text([{"type": "text", "text": "a"}, {"type": "think", "think": "b"}]),
            "a",
        )
        items = flow.records_to_items(
            [
                {"role": "user", "content": [{"type": "text", "text": "在吗"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "在"}]},
            ]
        )
        self.assertEqual(len(items), 2)
        self.assertTrue(items[1].is_bot)

    def test_system_note_marked(self) -> None:
        rows = [
            ("user", "[系统唤醒] 备注: 回来看一眼", "w", "系统唤醒", 1794873270.0, "{}"),
            ("assistant", "哦。", "b", "Bot", 1794873271.0, '{"is_bot_message": true}'),
        ]
        items = flow.rows_to_items(rows)
        self.assertTrue(items[0].system)
        text = flow.format_flow(items, 2000)
        self.assertIn(flow.SYSTEM_LABEL, text)
        self.assertNotIn("[系统唤醒]", text)

    def test_drop_old(self) -> None:
        now_ts = 1794900000.0
        items = [
            flow.FlowItem(role="user", text="旧", ts=now_ts - 30 * 3600),
            flow.FlowItem(role="user", text="新", ts=now_ts - 60),
        ]
        kept = flow.drop_old(items, 24, now_ts=now_ts)
        self.assertEqual([i.text for i in kept], ["新"])
        self.assertEqual(len(flow.drop_old(items, 0, now_ts=now_ts)), 2)

    def test_merge_proactive_dedupe(self) -> None:
        settings = make_settings()
        session = {"last_sent_message": "我说过的话"}
        items = [flow.FlowItem(role="assistant", text="我说过的话", is_bot=True)]
        self.assertEqual(len(flow.merge_proactive(items, session, settings)), 1)
        merged = flow.merge_proactive([], session, settings)
        self.assertTrue(merged[0].proactive)

    def test_sanitize_noise(self) -> None:
        raw = "吃了吗 [MSG_ID:1]\n\n<system_reminder>now</system_reminder>"
        cleaned = flow.sanitize_text(raw)
        self.assertNotIn("MSG_ID", cleaned)
        self.assertNotIn("system_reminder", cleaned)
        self.assertIn("吃了吗", cleaned)


class LlmJsonTests(unittest.TestCase):
    def test_extract(self) -> None:
        self.assertEqual(llm.extract_json('{"a": 1}'), {"a": 1})
        raw = '前面废话 ```json\n{"act": false}\n``` 后面'
        self.assertEqual(llm.extract_json(raw), {"act": False})
        self.assertIsNone(llm.extract_json("不是 JSON"))

    def test_strip_next_marker(self) -> None:
        cleaned, value = timeutil.strip_next_marker("今晚见\n[[pmind_next:+90m]]")
        self.assertEqual(value, "+90m")
        self.assertNotIn("pmind", cleaned)
        self.assertEqual(cleaned, "今晚见")

        cleaned, value = timeutil.strip_next_marker("好的[[pmind_next: 2026-11-18T21:30:00+08:00 ]]")
        self.assertTrue(value.startswith("2026-11-18T21:30:00"))

        text = "没有标记的普通句子"
        self.assertEqual(timeutil.strip_next_marker(text), (text, None))

    def test_marker_last_wins(self) -> None:
        cleaned, value = timeutil.strip_next_marker(
            "a[[pmind_next:+10m]] b [[pmind_next:none]]"
        )
        self.assertEqual(value, "none")
        self.assertNotIn("pmind", cleaned)

    def test_split_marker_time_and_topic(self) -> None:
        self.assertEqual(timeutil.split_marker("+20m|等他授权登录B站"), ("+20m", "等他授权登录B站"))
        self.assertEqual(timeutil.split_marker("+40m｜聊刚看的视频"), ("+40m", "聊刚看的视频"))
        self.assertEqual(timeutil.split_marker("+15m"), ("+15m", ""))

    async def test_marker_with_topic_sets_time_and_topic(self) -> None:
        """[[+20m|话题]] 必须能解析出时间，并把话题存进 job（之前解析失败落到 5 分钟默认）。"""
        settings = make_settings(**{"wake.min_interval_minutes": 1, "wake.max_interval_minutes": 1440})
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, [], settings=settings) as h:
                trace = await h.engine.run(
                    UMO,
                    jobs_mod.KIND_DIALOGUE,
                    trigger="user_message",
                    spoke_before=True,
                    record_only=True,
                    next_at_hint="+20m|等他授权登录B站",
                    main_reply_text="登录按钮点了两次。",
                )
                self.assertEqual(trace.get("next_from"), "reply_marker")
                job = h.store.session(UMO)["jobs"]["dialogue"]
                self.assertEqual(job.get("topic"), "等他授权登录B站")
                scheduled = h.scheduler.jobs[(UMO, jobs_mod.KIND_DIALOGUE, None)]
                delta = (scheduled - timeutil.now(TZ)).total_seconds() / 60
                self.assertAlmostEqual(delta, 20, delta=3)


class ToolboxCatalogTests(unittest.TestCase):
    def _tools(self, count: int):
        tools = []
        for index in range(count):
            tools.append(
                SimpleNamespace(
                    name=f"tool_{index:02d}",
                    description="d" * 100,
                    parameters={"type": "object", "properties": {"q": {"type": "string"}}},
                    active=True,
                )
            )
        return tools

    def _context(self, tools):
        return SimpleNamespace(
            get_llm_tool_manager=lambda: SimpleNamespace(
                get_full_tool_set=lambda: SimpleNamespace(
                    tools=tools, get_tool=lambda name: None
                )
            )
        )

    def test_default_cap_24(self) -> None:
        from core import toolbox

        catalog = toolbox.build_catalog(self._context(self._tools(60)), make_settings(), None)
        self.assertEqual(len(catalog), 24)

    def test_unlimited_and_budget(self) -> None:
        from core import toolbox

        unlimited = make_settings(**{"mind.catalog_max_entries": 0, "mind.catalog_char_budget": 0})
        self.assertEqual(
            len(toolbox.build_catalog(self._context(self._tools(60)), unlimited, None)), 60
        )
        budgeted = make_settings(**{"mind.catalog_max_entries": 0, "mind.catalog_char_budget": 1000})
        catalog = toolbox.build_catalog(self._context(self._tools(60)), budgeted, None)
        self.assertLess(len(catalog), 60)
        self.assertGreater(len(catalog), 2)

    def test_deny_pattern_relaxed_and_configurable(self) -> None:
        from core import toolbox

        tools = self._tools(3) + [
            SimpleNamespace(name="qts_delete_message", description="", parameters={}, active=True),
            SimpleNamespace(name="qts_change_group_card", description="", parameters={}, active=True),
            SimpleNamespace(name="pmind_wake", description="", parameters={}, active=True),
        ]
        names = [item["name"] for item in toolbox.build_catalog(self._context(tools), make_settings(), None)]
        self.assertNotIn("qts_delete_message", names)
        self.assertNotIn("pmind_wake", names)
        self.assertIn("qts_change_group_card", names)  # 放宽后不再被一刀切

        strict = make_settings(**{"mind.tool_deny_pattern": "qts_"})
        strict_names = [
            item["name"] for item in toolbox.build_catalog(self._context(tools), strict, None)
        ]
        self.assertNotIn("qts_change_group_card", strict_names)

    def test_unconfigured_search_tools_are_hidden(self) -> None:
        """只有配了密钥的 web_search_* 才进目录（否则调用会返回"未配密钥"）。"""
        from core import toolbox

        tools = [
            SimpleNamespace(name="web_search_deepseek", description="", parameters={}, active=True),
            SimpleNamespace(name="web_search_bocha", description="", parameters={}, active=True),
            SimpleNamespace(name="search_memes", description="", parameters={}, active=True),
        ]
        context = self._context(tools)
        context.get_config = lambda: {
            "provider_settings": {
                "web_search": True,
                "websearch_provider": "deepseek",
                "websearch_deepseek_key": ["sk-test"],
                "websearch_bocha_key": [],
            }
        }
        names = [item["name"] for item in toolbox.build_catalog(context, make_settings(), None)]
        self.assertIn("web_search_deepseek", names)
        self.assertNotIn("web_search_bocha", names)

        # 白名单里点名了没密钥的那个，也应该被挡掉
        hobby = [
            item["name"]
            for item in toolbox.build_catalog(
                context, make_settings(), None, allowed=["web_search"]
            )
        ]
        self.assertEqual(hobby, ["web_search_deepseek"])

    def test_search_filter_no_config_means_no_filter(self) -> None:
        from core import toolbox

        tools = [
            SimpleNamespace(name="web_search_bocha", description="", parameters={}, active=True),
        ]
        names = [item["name"] for item in toolbox.build_catalog(self._context(tools), make_settings(), None)]
        self.assertIn("web_search_bocha", names)  # 拿不到配置就不瞎过滤
        from core import toolbox

        settings = make_settings(**{"mind.tool_deny_pattern": "([bad"})
        self.assertEqual(toolbox.deny_pattern(settings).pattern, toolbox.DEFAULT_DENY_PATTERN)

    def test_whitelist_prefix_and_family_dedupe(self) -> None:
        from core import toolbox

        tools = [
            SimpleNamespace(name="web_search_serpapi", description="", parameters={}, active=True),
            SimpleNamespace(name="web_search_tavily", description="", parameters={}, active=True),
            SimpleNamespace(name="search_memes", description="", parameters={}, active=True),
            SimpleNamespace(name="qts_browser_open", description="", parameters={}, active=True),
            SimpleNamespace(name="qts_browser_click", description="", parameters={}, active=True),
        ]
        hobby = [
            item["name"]
            for item in toolbox.build_catalog(
                self._context(tools),
                make_settings(),
                None,
                allowed=["web_search", "search_memes"],
            )
        ]
        self.assertIn("web_search_serpapi", hobby)
        self.assertIn("web_search_tavily", hobby)  # 白名单=点名要的，不做同族去重

        everything = [
            item["name"]
            for item in toolbox.build_catalog(self._context(tools), make_settings(), None)
        ]
        self.assertEqual(len([n for n in everything if n.startswith("web_search")]), 1)
        self.assertEqual(len([n for n in everything if "browser" in n]), 1)
        self.assertTrue(toolbox.whitelist_match("web_search_deepseek", ["web_search"]))
        self.assertTrue(toolbox.whitelist_match("bilibili_read", ["bilibili_*"]))
        self.assertFalse(toolbox.whitelist_match("qts_poke_user", ["web_search"]))


# ----------------------------------------------------------------------
# 记忆
# ----------------------------------------------------------------------
class MemoryTests(unittest.IsolatedAsyncioTestCase):
    def test_fallback_text_has_no_template(self) -> None:
        text = memory.fallback_text(
            kind=jobs_mod.KIND_HOBBY,
            thought="想看看视频",
            steps=[{"tool": "bilibili_read", "ok": True}],
            message="这视频不错",
        )
        self.assertNotIn("【主动行为", text)
        self.assertIn("bilibili_read", text)
        self.assertIn("这视频不错", text)

    async def test_reflection_mode_skips_tool(self) -> None:
        settings = make_settings(**{"memory.mode": "reflection"})
        result = await memory.write_memory(
            SimpleNamespace(), settings, umo=UMO, content="纯文本总结", topics=["x"]
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result.get("skipped"))

    async def test_tool_mode_uses_plain_text(self) -> None:
        calls: list[dict] = []

        class FakeTool:
            name = settings_tool_name = "memorize_long_term_memory"
            description = "d"
            parameters = {}
            active = True

            async def call(self, context, **kwargs):
                calls.append(kwargs)
                return '{"memorized": true, "id": 7}'

        tool = FakeTool()
        tool_set = SimpleNamespace(
            tools=[tool], get_tool=lambda name: tool if name == tool.name else None
        )
        context = SimpleNamespace(
            get_llm_tool_manager=lambda: SimpleNamespace(get_full_tool_set=lambda: tool_set)
        )
        settings = make_settings(**{"memory.mode": "tool"})
        result = await memory.write_memory(
            context, settings, umo=UMO, content="自然语言总结", topics=["proactive_hobby"]
        )
        self.assertTrue(result["ok"])
        self.assertEqual(calls[0]["memory"], "自然语言总结")
        self.assertEqual(calls[0]["topics"], ["proactive_hobby"])


class MemoryDbTests(unittest.TestCase):
    def _make_db(self, path: Path) -> None:
        con = sqlite3.connect(str(path))
        con.execute(
            "CREATE TABLE documents (id INTEGER PRIMARY KEY, text TEXT, metadata TEXT, created_at TEXT, updated_at TEXT)"
        )
        rows = [
            ("喜欢看香锅教学", {"topics": ["proactive_hobby"], "status": "active"}, "2026-11-18 10:00:00"),
            ("最近在学剪辑", {"topics": ["兴趣", "剪辑"], "status": "active"}, "2026-11-18 09:00:00"),
            ("晚饭吃了面", {"topics": ["晚饭"], "status": "active"}, "2026-11-18 08:00:00"),
            ("被归档的爱好", {"topics": ["proactive_hobby"], "status": "archived"}, "2026-11-18 07:00:00"),
        ]
        for text, meta, created in rows:
            con.execute(
                "INSERT INTO documents (text, metadata, created_at, updated_at) VALUES (?,?,?,?)",
                (text, json.dumps(meta, ensure_ascii=False), created, created),
            )
        con.commit()
        con.close()

    def test_tag_keyword_recent_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "livingmemory.db"
            self._make_db(db)
            items = memorydb.scan(
                str(db), tag="proactive_hobby", keywords=["兴趣", "爱好"], limit=3
            )
            self.assertEqual(len(items), 3)
            self.assertEqual(items[0].via, "tag")
            self.assertEqual(items[1].via, "keyword")
            self.assertEqual(items[2].via, "recent")
            self.assertTrue(all("归档" not in item.text for item in items))

    def test_limit_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "livingmemory.db"
            self._make_db(db)
            self.assertEqual(
                memorydb.scan(str(db), tag="proactive_hobby", keywords=[], limit=0), []
            )

    def test_render(self) -> None:
        items = [
            memorydb.HobbyMemory(1, "文本" * 400, ["proactive_hobby"], 0.8, "2026-11-18 10:00:00", "tag")
        ]
        text = memorydb.render_memories(items)
        self.assertIn("proactive_hobby", text)
        self.assertLessEqual(len(text), 6500)


# ----------------------------------------------------------------------
# 上下文（人格/流水唯一性 + 不回放旧话）
# ----------------------------------------------------------------------
class ContextTests(unittest.TestCase):
    def test_state_block_no_replay(self) -> None:
        settings = make_settings(**{"state.recent_sent_hours": 6})
        now = datetime(2026, 11, 18, 12, 0, tzinfo=TZ)
        session = {
            "last_sent_message": "少给我打岔…三个字，你倒是打出来",
            "last_sent_at": timeutil.iso(now - timedelta(hours=48)),
            "last_gate": {"act": False, "reason": "等他打那三个字"},
            "last_user_message_at": timeutil.iso(now - timedelta(hours=1)),
            "silent_wake_count": 2,
            "jobs": jobs_mod.default_jobs(),
        }
        block = ctx_mod.build_state_block(
            settings=settings,
            session=session,
            kind=jobs_mod.KIND_DIALOGUE,
            trigger="dialogue_cron",
            now=now,
            tz_name=str(TZ),
            flow_count=10,
        )
        self.assertNotIn("三个字", block)
        self.assertIn("内容不再回放", block)
        self.assertIn("上次我的判断：不打扰", block)

    def test_state_block_recent_replay(self) -> None:
        settings = make_settings(**{"state.recent_sent_hours": 6})
        now = datetime(2026, 11, 18, 12, 0, tzinfo=TZ)
        session = {
            "last_sent_message": "刚说的话",
            "last_sent_at": timeutil.iso(now - timedelta(minutes=30)),
            "jobs": jobs_mod.default_jobs(),
        }
        block = ctx_mod.build_state_block(
            settings=settings,
            session=session,
            kind=jobs_mod.KIND_HOBBY,
            trigger="hobby_cron",
            now=now,
            tz_name=str(TZ),
        )
        self.assertIn("刚说的话", block)
        self.assertIn("上次我的判断：无", block)

    def test_prompts_have_single_flow_and_persona(self) -> None:
        settings = make_settings()
        system = ctx_mod.build_system("人格文本", settings)
        user = ctx_mod.build_dialogue_user("状态", "流水文本", "工具")
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        joined = json.dumps(messages, ensure_ascii=False)
        self.assertEqual(joined.count("人格文本"), 1)
        self.assertEqual(joined.count("流水文本"), 1)
        self.assertIn("不复读", system)

    def test_hobby_prompt_contains_memory_instruction(self) -> None:
        settings = make_settings()
        user = ctx_mod.build_hobby_user("状态", "记忆块", "流水块", "工具块")
        self.assertIn("记忆块", user)
        self.assertIn("流水块", user)
        self.assertIn("下一轮", user)


# ----------------------------------------------------------------------
# 决策流水线
# ----------------------------------------------------------------------
class FakeScheduler:
    def __init__(self) -> None:
        self.tz = TZ
        self.jobs: dict[tuple[str, str, str | None], datetime] = {}
        self.cancelled: list[tuple[str, str, str | None]] = []
        self.order: list[str] = []
        self.started = True

    def _key(self, umo, kind, temp_id):
        return (umo, kind, temp_id)

    def set_wake(self, umo, when, *, kind=jobs_mod.KIND_DIALOGUE, temp_id=None):
        self.jobs[self._key(umo, kind, temp_id)] = when
        self.order.append(f"schedule:{kind}")
        return when

    def cancel(self, umo, *, kind=None, temp_id=None):
        if kind is None:
            for key in list(self.jobs):
                if key[0] == umo:
                    self.jobs.pop(key, None)
            return True
        self.cancelled.append((umo, kind, temp_id))
        return self.jobs.pop(self._key(umo, kind, temp_id), None) is not None

    def next_run(self, umo, *, kind=jobs_mod.KIND_DIALOGUE, temp_id=None):
        return self.jobs.get(self._key(umo, kind, temp_id))


class FakeStore(StateStore):
    def __init__(self, tmp_path) -> None:
        super().__init__(Path(tmp_path) / "state.json", trace_keep=10)


class Harness:
    """装配 DecisionEngine，替换外部依赖。"""

    def __init__(
        self,
        tmp_path,
        script,
        *,
        settings=None,
        session_override=None,
        tools=None,
        flow_items=None,
        memories=None,
        kinds_meta=None,
    ) -> None:
        self.tmp = tmp_path
        self.store = FakeStore(tmp_path)
        self.scheduler = FakeScheduler()
        self.settings = settings or make_settings()
        self.script = list(script)
        self.sent: list[str] = []
        self.echoed: list[str] = []
        self.memory_calls: list[dict] = []
        self.flow_items = flow_items if flow_items is not None else [
            flow.FlowItem(role="user", text="在吗", ts=timeutil.now(TZ).timestamp()),
            flow.FlowItem(role="assistant", text="在", ts=timeutil.now(TZ).timestamp(), is_bot=True),
        ]
        self.memories = memories or []
        self.tools = tools or []
        self.session_override = session_override or {}
        self.requests: list[list[dict]] = []
        self.context = SimpleNamespace(
            conversation_manager=None,
            persona_manager=SimpleNamespace(
                get_default_persona_v3=self._persona,
            ),
            get_llm_tool_manager=lambda: SimpleNamespace(
                get_full_tool_set=lambda: SimpleNamespace(
                    tools=self.tools,
                    get_tool=lambda name: next((t for t in self.tools if t.name == name), None),
                )
            ),
        )

    async def _persona(self, umo=None):
        return {"prompt": "测试人格", "name": "test"}

    async def _fake_chat(self, context, settings, umo, *, messages, log=None):
        self.requests.append(json.loads(json.dumps(messages, ensure_ascii=False)))
        data = self.script.pop(0) if self.script else None
        return data, json.dumps(data, ensure_ascii=False) if data else "", []

    async def _fake_flow(self, context, settings, umo, log=None):
        return list(self.flow_items), "livingmemory", []

    async def _fake_memories(self, settings, log=None, limit=None):
        return list(self.memories), {"count": len(self.memories)}

    async def _fake_memory_write(self, context, settings, **kwargs):
        self.memory_calls.append(kwargs)
        self.scheduler.order.append("memory")
        return {"ok": True, "result": '{"memorized": true, "id": 99}'}

    async def _fake_send(self, context, *, umo, text, log=None, settings=None):
        self.sent.append(text)
        return {"ok": True, "segments": 1}

    async def _fake_echo(self, context, settings, *, umo, text, session=None, log=None):
        self.echoed.append(text)
        return {"platform_history": {"ok": True}, "livingmemory": {"ok": True}}

    async def __aenter__(self):
        self._patches = [
            mock.patch.object(decide_mod.llm, "chat_json_messages", self._fake_chat),
            mock.patch.object(decide_mod.flow_mod, "read_flow", self._fake_flow),
            mock.patch.object(decide_mod.memorydb, "read_hobby_memories", self._fake_memories),
            mock.patch.object(decide_mod.memory, "write_memory", self._fake_memory_write),
            mock.patch.object(decide_mod.delivery, "send_text", self._fake_send),
            mock.patch.object(decide_mod.echo, "echo_all", self._fake_echo),
        ]
        for patch in self._patches:
            patch.start()
        await self.store.load()
        if self.session_override:
            self.store.set(UMO, **self.session_override)
        self.engine = decide_mod.DecisionEngine(
            context=self.context,
            settings=self.settings,
            store=self.store,
            scheduler=self.scheduler,
            data_dir=self.tmp,
            logger=None,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb):
        for patch in reversed(self._patches):
            patch.stop()
        return False


class DialogueCycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_spoke_creates_dialogue_job_and_no_auto_memory(self) -> None:
        """对话周期：说了话就必须排对话作业；记忆由她自己决定，不自动写。"""
        script = [
            {
                "act": True,
                "reason": "他刚提到晚上没吃饭，问一句",
                "thought": "别太啰嗦",
                "steps": [],
                "message": "晚饭吃了没",
                "dialogue_next_at": "+90m",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, script) as h:
                trace = await h.engine.run(UMO, jobs_mod.KIND_DIALOGUE, trigger="user_message", spoke_before=True)
                self.assertEqual(h.sent, ["晚饭吃了没"])
                self.assertEqual(h.memory_calls, [])
                self.assertEqual(trace["memory"].get("reason"), "model_decides")
                self.assertIn((UMO, jobs_mod.KIND_DIALOGUE, None), h.scheduler.jobs)
                self.assertEqual(trace["context_stats"]["requests"], 1)
                self.assertEqual(trace["context_stats"]["persona_units"], 1)
                self.assertEqual(trace["context_stats"]["flow_units"], 1)
                self.assertIn("schedule:dialogue", h.scheduler.order)

    async def test_dialogue_memory_when_model_calls_tool(self) -> None:
        """对话周期若她自己调了 memorize 工具，就记为"她写的"。"""
        settings = make_settings()
        script = [
            {
                "act": True,
                "reason": "记一下",
                "steps": [
                    {
                        "tool": settings.memory_tool_name,
                        "args": {"memory": "他今天没吃饭"},
                    }
                ],
                "message": "记得吃饭",
                "dialogue_next_at": "+60m",
            }
        ]
        tool = SimpleNamespace(name=settings.memory_tool_name, description="d", parameters={}, active=True)

        async def fake_steps(context, settings_, *, steps, log=None, event=None):
            return [{"tool": settings_.memory_tool_name, "ok": True, "result": '{"memorized": true}'}]

        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, script, settings=settings, tools=[tool]) as h:
                with mock.patch.object(decide_mod.toolbox, "execute_steps", fake_steps):
                    trace = await h.engine.run(
                        UMO, jobs_mod.KIND_DIALOGUE, trigger="user_message", spoke_before=True
                    )
                self.assertEqual(h.memory_calls, [])
                self.assertEqual(trace["memory"].get("by"), "model_tool")

    async def test_cron_failure_reschedules_with_default(self) -> None:
        """断网/模型失败：对话 cron 也要按随机区间留下下一次，不能变成"未安排"。"""
        settings = make_settings(
            **{"wake.min_interval_minutes": 119, "wake.max_interval_minutes": 121}
        )
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, [], settings=settings) as h:  # 空脚本 → 模型调用失败
                session = h.store.session(UMO)
                session["jobs"]["dialogue"]["next_at"] = timeutil.iso(
                    timeutil.now(TZ) - timedelta(seconds=1)
                )
                session["silent_wake_count"] = 2
                trace = await h.engine.run(UMO, jobs_mod.KIND_DIALOGUE, trigger="dialogue_cron")
                self.assertTrue(trace.get("retry_due_to_error"))
                self.assertEqual(
                    h.store.session(UMO)["silent_wake_count"], 2  # 失败不推进计数
                )
                job = h.store.session(UMO)["jobs"]["dialogue"]
                self.assertIsNotNone(job["next_at"])
                scheduled = h.scheduler.jobs[(UMO, jobs_mod.KIND_DIALOGUE, None)]
                delta = (scheduled - timeutil.now(TZ)).total_seconds() / 60
                self.assertAlmostEqual(delta, 120, delta=3)

    async def test_cron_failure_ignores_cap(self) -> None:
        """已到连续上限时，失败重试仍要留下下一次（否则断网几次就永久停摆）。"""
        settings = make_settings(**{"limit.max_consecutive_wakes": 1})
        session_override = {"silent_wake_count": 9, "user_replied_since_last_wake": False}
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, [], settings=settings, session_override=session_override) as h:
                trace = await h.engine.run(UMO, jobs_mod.KIND_DIALOGUE, trigger="dialogue_cron")
                self.assertNotEqual(trace.get("schedule_blocked"), "consecutive_limit_reached")
                self.assertIn((UMO, jobs_mod.KIND_DIALOGUE, None), h.scheduler.jobs)

    async def test_hobby_failure_reschedules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, []) as h:
                trace = await h.engine.run(UMO, jobs_mod.KIND_HOBBY, trigger="hobby_cron")
                self.assertTrue(trace.get("retry_due_to_error"))
                self.assertIn((UMO, jobs_mod.KIND_HOBBY, None), h.scheduler.jobs)

    async def test_cron_always_reschedules_even_without_message(self) -> None:
        """对话周期改成"触发就必须更新"：静默也要排下一次。"""
        script = [
            {
                "act": False,
                "reason": "刚聊完，先不打扰",
                "steps": [],
                "message": None,
                "memory": "",
                "dialogue_next_at": "+40m",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, script) as h:
                session = h.store.session(UMO)
                session["jobs"]["dialogue"]["next_at"] = timeutil.iso(
                    timeutil.now(TZ) - timedelta(seconds=1)
                )
                session["next_wake_at"] = session["jobs"]["dialogue"]["next_at"]
                trace = await h.engine.run(UMO, jobs_mod.KIND_DIALOGUE, trigger="dialogue_cron")
                self.assertEqual(h.sent, [])
                self.assertEqual(trace.get("act"), False)
                # 到点被消费后立刻排新的（触发就必须更新）
                job = h.store.session(UMO)["jobs"]["dialogue"]
                self.assertIsNotNone(job["next_at"])
                self.assertIn((UMO, jobs_mod.KIND_DIALOGUE, None), h.scheduler.jobs)
                self.assertEqual(trace.get("next_from"), "model")

    async def test_record_only_marks_action_and_reply(self) -> None:
        """主 Agent 说了话：轨迹要记成 act=True / message_sent=True / message_source=main_agent。"""
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, []) as h:
                trace = await h.engine.run(
                    UMO,
                    jobs_mod.KIND_DIALOGUE,
                    trigger="user_message",
                    spoke_before=True,
                    record_only=True,
                    next_at_hint="+50m",
                    main_reply_text="（端着碗）吃你的饭，别问东问西。",
                )
                self.assertIs(trace.get("act"), True)
                self.assertIs(trace.get("message_sent"), True)
                self.assertEqual(trace.get("message_source"), "main_agent")
                self.assertIn("别问东问西", trace.get("message") or "")
                session = h.store.session(UMO)
                self.assertIn("别问东问西", session.get("last_sent_message") or "")
                self.assertIsNotNone(session.get("last_sent_at"))
                self.assertEqual(h.requests, [])

    async def test_record_only_uses_tool_time_if_agent_set_it(self) -> None:
        """她这轮自己调了 pmind_schedule(kind=dialogue)：用她定的时间，标 next_from=tool。"""
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, []) as h:
                now = timeutil.now(TZ)
                session = h.store.session(UMO)
                session["last_user_message_at"] = timeutil.iso(now - timedelta(seconds=5))
                session["jobs"]["dialogue"]["next_at"] = timeutil.iso(now + timedelta(days=2))
                session["jobs"]["dialogue"]["updated_at"] = timeutil.iso(now - timedelta(seconds=2))
                trace = await h.engine.run(
                    UMO,
                    jobs_mod.KIND_DIALOGUE,
                    trigger="user_message",
                    spoke_before=True,
                    record_only=True,
                    main_reply_text="行，明天见。",
                )
                self.assertEqual(trace.get("next_from"), "tool")
                scheduled = h.scheduler.jobs[(UMO, jobs_mod.KIND_DIALOGUE, None)]
                # 两天 > 默认 max(1440m)，说明没被 clamp 改写
                self.assertGreater((scheduled - now).total_seconds(), 24 * 3600)
                self.assertEqual(h.requests, [])

    def test_dialogue_set_by_tool_detection(self) -> None:
        from core.decide import DecisionEngine

        now = timeutil.now(TZ)
        session = {"last_user_message_at": timeutil.iso(now - timedelta(seconds=3))}
        fresh = {
            "dialogue": {
                "next_at": timeutil.iso(now + timedelta(hours=2)),
                "updated_at": timeutil.iso(now - timedelta(seconds=1)),
            }
        }
        stale = {
            "dialogue": {
                "next_at": timeutil.iso(now + timedelta(hours=2)),
                "updated_at": timeutil.iso(now - timedelta(hours=5)),
            }
        }
        self.assertIsNotNone(DecisionEngine._dialogue_set_by_tool(session, fresh))
        self.assertIsNone(DecisionEngine._dialogue_set_by_tool(session, stale))

    async def test_record_only_makes_no_model_call(self) -> None:
        """A 模式：不调用模型、不发消息，只按标记/默认间隔排下一次。"""
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, []) as h:  # 没有脚本 → 若调用模型会立刻失败
                trace = await h.engine.run(
                    UMO,
                    jobs_mod.KIND_DIALOGUE,
                    trigger="user_message",
                    spoke_before=True,
                    record_only=True,
                    next_at_hint="+70m",
                )
                self.assertEqual(h.requests, [])  # 一次模型调用都没有
                self.assertEqual(h.sent, [])
                self.assertTrue(trace.get("record_only"))
                self.assertEqual(trace.get("next_from"), "reply_marker")
                scheduled = h.scheduler.jobs[(UMO, jobs_mod.KIND_DIALOGUE, None)]
                delta = (scheduled - timeutil.now(TZ)).total_seconds() / 60
                self.assertAlmostEqual(delta, 70, delta=3)

    async def test_missing_marker_uses_random_range(self) -> None:
        """没给时间 → 随机兜底，落在用户设定区间内。"""
        settings = make_settings(
            **{"wake.min_interval_minutes": 60, "wake.max_interval_minutes": 61}
        )
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, [], settings=settings) as h:
                trace = await h.engine.run(
                    UMO,
                    jobs_mod.KIND_DIALOGUE,
                    trigger="user_message",
                    spoke_before=True,
                    record_only=True,
                    main_reply_text="说完了。",
                )
                self.assertEqual(trace.get("next_from"), "random_fallback")
                scheduled = h.scheduler.jobs[(UMO, jobs_mod.KIND_DIALOGUE, None)]
                delta = (scheduled - timeutil.now(TZ)).total_seconds() / 60
                self.assertAlmostEqual(delta, 60, delta=2)

    async def test_scheduling_clears_pending_marker(self) -> None:
        """一旦排定了时间，就清掉"等待回复"标记 → 宽限期任务不会再补垫。"""
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, [], settings=make_settings()) as h:
                session = h.store.session(UMO)
                session["wake_pending_at"] = timeutil.iso(timeutil.now(TZ))
                trace = h.store.new_trace(UMO, "user_message", jobs_mod.KIND_DIALOGUE)
                h.engine._schedule_dialogue(
                    UMO, "+40m", trace, now=timeutil.now(TZ), bypass_limit=True
                )
                self.assertIsNone(h.store.session(UMO).get("wake_pending_at"))

    async def test_provisional_skips_when_future_time_exists(self) -> None:
        """只在没有未来时间时才垫：已有未来时间必须原样保留（不许提前）。"""
        settings = make_settings(
            **{"mind.provisional_enabled": True, "wake.min_interval_minutes": 1, "wake.max_interval_minutes": 10}
        )
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, [], settings=settings) as h:
                session = h.store.session(UMO)
                future = timeutil.now(TZ) + timedelta(hours=2)
                session["jobs"]["dialogue"]["next_at"] = timeutil.iso(future)
                result = await h.engine.provisional_schedule(UMO, jobs_mod.KIND_DIALOGUE)
                self.assertIsNone(result)  # 不垫
                self.assertEqual(
                    h.store.session(UMO)["jobs"]["dialogue"]["next_at"],
                    timeutil.iso(future),  # 未来时间没被动过
                )
                self.assertNotIn((UMO, jobs_mod.KIND_DIALOGUE, None), h.scheduler.jobs)

    async def test_provisional_schedule_prevents_unset(self) -> None:
        """交还主 Agent 时先垫保底时间：回复没发出来也不会变"未安排"。"""
        settings = make_settings(**{"wake.min_interval_minutes": 19, "wake.max_interval_minutes": 21})
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, [], settings=settings) as h:
                session = h.store.session(UMO)
                session["jobs"]["dialogue"]["next_at"] = None
                target = await h.engine.provisional_schedule(UMO, jobs_mod.KIND_DIALOGUE)
                self.assertIsNotNone(target)
                self.assertIn((UMO, jobs_mod.KIND_DIALOGUE, None), h.scheduler.jobs)
                job = h.store.session(UMO)["jobs"]["dialogue"]
                self.assertIsNotNone(job["next_at"])
                self.assertTrue(job.get("provisional"))
                delta = (timeutil.from_iso(job["next_at"]) - timeutil.now(TZ)).total_seconds() / 60
                self.assertAlmostEqual(delta, 20, delta=2)

    async def test_record_main_agent_reply_books_the_job(self) -> None:
        """主 Agent 到点发言后的记账：act/sent 如实、job 更新、话题清空。"""
        settings = make_settings(
            **{"wake.min_interval_minutes": 10, "wake.max_interval_minutes": 11}
        )
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, [], settings=settings) as h:
                session = h.store.session(UMO)
                session["jobs"]["dialogue"]["topic"] = "问他睡没睡"
                trace = await h.engine.record_main_agent_reply(
                    UMO,
                    kind=jobs_mod.KIND_DIALOGUE,
                    text="（翻个身）还睁着眼呢。",
                    marker=None,
                    note="到点唤醒",
                )
                self.assertIs(trace.get("act"), True)
                self.assertIs(trace.get("message_sent"), True)
                self.assertEqual(trace.get("message_source"), "main_agent")
                self.assertEqual(trace.get("next_from"), "random_fallback")
                session = h.store.session(UMO)
                self.assertIsNone(session["jobs"]["dialogue"]["topic"])  # 话题用完即清
                self.assertIsNotNone(session["jobs"]["dialogue"]["next_at"])

    async def test_marker_none_falls_back_to_random(self) -> None:
        """none 已废弃：当作没给时间 → 在配置区间内随机兜底。"""
        settings = make_settings(
            **{"wake.min_interval_minutes": 30, "wake.max_interval_minutes": 31}
        )
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, [], settings=settings) as h:
                trace = await h.engine.run(
                    UMO,
                    jobs_mod.KIND_DIALOGUE,
                    trigger="user_message",
                    spoke_before=True,
                    record_only=True,
                    next_at_hint="none",
                    main_reply_text="谁要主动找你。",
                )
                self.assertEqual(trace.get("next_from"), "random_fallback")
                self.assertEqual(trace.get("fallback_range"), [30, 31])
                scheduled = h.scheduler.jobs[(UMO, jobs_mod.KIND_DIALOGUE, None)]
                delta = (scheduled - timeutil.now(TZ)).total_seconds() / 60
                self.assertAlmostEqual(delta, 30, delta=2)

    async def test_record_only_suppresses_message_but_schedules(self) -> None:
        """A 模式绝不发第二条消息，也不调模型；缺时间就随机兜底。"""
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, []) as h:
                trace = await h.engine.run(
                    UMO,
                    jobs_mod.KIND_DIALOGUE,
                    trigger="user_message",
                    spoke_before=True,
                    record_only=True,
                    main_reply_text="（把碗一推）吃你的。",
                )
                self.assertEqual(h.sent, [])  # 从不说第二句
                self.assertEqual(h.requests, [])  # 一次模型都不调
                self.assertEqual(trace.get("next_from"), "random_fallback")
                self.assertIn((UMO, jobs_mod.KIND_DIALOGUE, None), h.scheduler.jobs)

    async def test_marker_time_wins_over_model(self) -> None:
        """回复标记里的时间优先于模型自己给的 dialogue_next_at。"""
        script = [
            {
                "act": False,
                "reason": "记账",
                "steps": [],
                "message": None,
                "dialogue_next_at": "+180m",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, script) as h:
                trace = await h.engine.run(
                    UMO,
                    jobs_mod.KIND_DIALOGUE,
                    trigger="user_message",
                    spoke_before=True,
                    record_only=True,
                    next_at_hint="+45m",
                )
                self.assertEqual(trace.get("next_from"), "reply_marker")
                scheduled = h.scheduler.jobs[(UMO, jobs_mod.KIND_DIALOGUE, None)]
                delta = (scheduled - timeutil.now(TZ)).total_seconds() / 60
                self.assertAlmostEqual(delta, 45, delta=3)

    async def test_tools_trigger_second_request(self) -> None:
        script = [
            {
                "act": True,
                "reason": "先看看她最近在追什么",
                "steps": [{"tool": "bilibili_read", "args": {"keyword": "香锅"}}],
                "message": "草稿",
                "memory": "看了视频",
                "dialogue_next_at": "+60m",
            },
            {"message": "刚看到你追的那部更新了", "memory": "看了视频并告诉她更新了"},
        ]
        tool = SimpleNamespace(name="bilibili_read", description="d", parameters={}, active=True)
        executed: list[dict] = []

        async def fake_steps(context, settings, *, steps, log=None, event=None):
            executed.extend(steps)
            return [{"tool": "bilibili_read", "ok": True, "result": "视频列表"}]

        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, script, tools=[tool]) as h:
                with mock.patch.object(decide_mod.toolbox, "execute_steps", fake_steps):
                    trace = await h.engine.run(UMO, jobs_mod.KIND_DIALOGUE, trigger="user_message", spoke_before=True)
                self.assertEqual(executed[0]["tool"], "bilibili_read")
                self.assertEqual(h.sent, ["刚看到你追的那部更新了"])
                self.assertEqual(trace["context_stats"]["requests"], 2)
                self.assertEqual(h.memory_calls, [])

    async def test_single_request_skips_finalize(self) -> None:
        settings = make_settings(**{"mind.single_request": "true"})
        script = [
            {
                "act": True,
                "reason": "看视频",
                "steps": [{"tool": "bilibili_read", "args": {}}],
                "message": "草稿消息",
                "memory": "看了视频",
                "dialogue_next_at": "+60m",
            }
        ]
        tool = SimpleNamespace(name="bilibili_read", description="d", parameters={}, active=True)

        async def fake_steps(context, settings, *, steps, log=None, event=None):
            return [{"tool": "bilibili_read", "ok": True, "result": "x"}]

        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, script, settings=settings, tools=[tool]) as h:
                with mock.patch.object(decide_mod.toolbox, "execute_steps", fake_steps):
                    trace = await h.engine.run(UMO, jobs_mod.KIND_DIALOGUE, trigger="user_message", spoke_before=True)
                self.assertEqual(trace["context_stats"]["requests"], 1)
                self.assertEqual(h.sent, ["草稿消息"])

    async def test_cap_blocks_dialogue_creation(self) -> None:
        settings = make_settings(**{"limit.max_consecutive_wakes": 1})
        session = {"silent_wake_count": 5, "user_replied_since_last_wake": False}
        script = [
            {
                "act": True,
                "reason": "想说话",
                "steps": [],
                "message": "在吗",
                "memory": "x",
                "dialogue_next_at": "+30m",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, script, settings=settings, session_override=session) as h:
                trace = await h.engine.run(UMO, jobs_mod.KIND_DIALOGUE, trigger="dialogue_cron")
                self.assertEqual(trace["schedule_blocked"], "consecutive_limit_reached")
                self.assertNotIn((UMO, jobs_mod.KIND_DIALOGUE, None), h.scheduler.jobs)


class HobbyCycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_hobby_injects_memories_and_flow(self) -> None:
        memory_items = [
            memorydb.HobbyMemory(1, "喜欢看香锅教学", ["proactive_hobby"], 0.8, "2026-11-18 10:00:00", "tag"),
            memorydb.HobbyMemory(2, "想学剪辑", ["proactive_hobby"], 0.8, "2026-11-18 09:00:00", "tag"),
        ]
        script = [
            {
                "act": True,
                "reason": "今晚想看看香锅视频",
                "thought": "翻一下收藏",
                "steps": [],
                "message": "这个香锅视频不错",
                "memory": "这轮看了香锅教学，下一轮想找剪辑教程",
                "hobby_next_at": "+3h",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, script, memories=memory_items) as h:
                trace = await h.engine.run(UMO, jobs_mod.KIND_HOBBY, trigger="hobby_cron")
                self.assertEqual(trace["context_stats"]["flow_units"], 1)
                self.assertEqual(trace["memory_count"], 2)
                prompt = json.dumps(h.requests[0], ensure_ascii=False)
                self.assertIn("喜欢看香锅教学", prompt)
                self.assertIn("在吗", prompt)  # 20 条流水里带上了对话
                self.assertEqual(h.sent, ["这个香锅视频不错"])
                self.assertIn((UMO, jobs_mod.KIND_HOBBY, None), h.scheduler.jobs)
                self.assertEqual(h.store.session(UMO)["jobs"]["hobby"]["intent"][:4], "这轮看了")
                self.assertLess(h.scheduler.order.index("memory"), h.scheduler.order.index("schedule:hobby"))

    async def test_hobby_runs_counts_execution_not_scheduling(self) -> None:
        """runs 只在真正到期执行时 +1；手动触发/面板改时间不计数。"""
        script = [
            {
                "act": True,
                "reason": "做事",
                "steps": [],
                "message": None,
                "memory": "这轮做了点事，下轮继续",
                "hobby_next_at": "+3h",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, script * 2) as h:
                await h.engine.run(UMO, jobs_mod.KIND_HOBBY, trigger="hobby_cron")
                sessions = h.store.session(UMO)["jobs"]["hobby"]
                self.assertEqual(sessions["runs"], 1)
                self.assertEqual(sessions["scheduled"], 1)

                await h.engine.run(UMO, jobs_mod.KIND_HOBBY, trigger="manual", bypass_limit=True)
                sessions = h.store.session(UMO)["jobs"]["hobby"]
                self.assertEqual(sessions["runs"], 1)  # 手动不算
                self.assertEqual(sessions["scheduled"], 2)

    async def test_hobby_fallback_uses_its_range(self) -> None:
        """爱好兜底也在 hobby.min~max 之间随机（不再用固定默认间隔）。"""
        settings = make_settings(
            **{"hobby.min_interval_minutes": 119, "hobby.max_interval_minutes": 121}
        )
        script = [
            {
                "act": True,
                "reason": "自己待会儿",
                "steps": [],
                "message": None,
                "memory": "这轮什么都没做，下轮继续翻收藏夹",
                "hobby_next_at": None,
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, script, settings=settings) as h:
                trace = await h.engine.run(UMO, jobs_mod.KIND_HOBBY, trigger="hobby_cron")
                self.assertTrue(trace.get("schedule_fallback"))
                self.assertEqual(trace.get("fallback_range"), [119, 121])
                scheduled = h.scheduler.jobs[(UMO, jobs_mod.KIND_HOBBY, None)]
                delta = (scheduled - timeutil.now(TZ)).total_seconds() / 60
                self.assertAlmostEqual(delta, 120, delta=3)

    async def test_hobby_range_allows_zero_to_three_days(self) -> None:
        settings = make_settings(
            **{"hobby.min_interval_minutes": 0, "hobby.max_interval_minutes": 4320}
        )
        self.assertEqual(settings.hobby_min_interval_minutes, 0)
        self.assertEqual(settings.hobby_max_interval_minutes, 4320)

    async def test_hobby_silent_still_loops(self) -> None:
        script = [
            {
                "act": True,
                "reason": "只想自己待着",
                "steps": [],
                "message": None,
                "memory": "这轮没做什么，下一轮去翻翻收藏夹",
                "hobby_next_at": None,
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, script) as h:
                trace = await h.engine.run(UMO, jobs_mod.KIND_HOBBY, trigger="hobby_cron")
                self.assertEqual(h.sent, [])
                self.assertTrue(trace["schedule_fallback"])
                self.assertIn((UMO, jobs_mod.KIND_HOBBY, None), h.scheduler.jobs)

    async def test_hobby_cancelled_blocks(self) -> None:
        session = {"jobs": jobs_mod.default_jobs()}
        session["jobs"]["hobby"]["cancelled"] = True
        script = [
            {"act": True, "reason": "x", "steps": [], "message": None, "memory": "y", "hobby_next_at": "+3h"}
        ]
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, script, session_override={}) as h:
                h.store.session(UMO)["jobs"]["hobby"]["cancelled"] = True
                trace = await h.engine.run(UMO, jobs_mod.KIND_HOBBY, trigger="hobby_cron")
                self.assertEqual(trace["schedule_blocked"], "hobby_cancelled")


class TempCycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_temp_speaking_updates_dialogue_job(self) -> None:
        """临时提醒到点她说了话 → 对话 job 也必须更新（只要说话就更新）。"""
        script = [
            {
                "act": True,
                "reason": "两分钟到了，说一句",
                "steps": [],
                "message": "行，两分钟到了。",
                "dialogue_next_at": "+75m",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, script) as h:
                session = h.store.session(UMO)
                session["jobs"]["temp"].append(
                    {"id": "t-2", "next_at": timeutil.iso(timeutil.now(TZ)), "note": "两分钟后说话"}
                )
                trace = await h.engine.run(
                    UMO, jobs_mod.KIND_TEMP, trigger="temp_cron", temp_id="t-2"
                )
                self.assertEqual(h.sent, ["行，两分钟到了。"])
                self.assertEqual(trace.get("next_from"), "temp_spoke")
                self.assertIn((UMO, jobs_mod.KIND_DIALOGUE, None), h.scheduler.jobs)
                self.assertIsNotNone(h.store.session(UMO)["jobs"]["dialogue"]["next_at"])
    async def test_temp_consumed_and_not_rescheduled(self) -> None:
        script = [
            {
                "act": True,
                "reason": "到点了，告诉他能说话了",
                "steps": [],
                "message": "时间到了",
                "memory": "两分钟禁言结束，我提醒了他",
                "dialogue_next_at": "+30m",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, script) as h:
                session = h.store.session(UMO)
                session["jobs"]["temp"].append(
                    {
                        "id": "t-1",
                        "next_at": timeutil.iso(timeutil.now(TZ) - timedelta(seconds=5)),
                        "note": "两分钟内别说话",
                        "created_by": "test",
                    }
                )
                trace = await h.engine.run(
                    UMO, jobs_mod.KIND_TEMP, trigger="temp_cron", temp_id="t-1"
                )
                self.assertEqual(session["jobs"]["temp"], [])
                self.assertEqual(h.sent, ["时间到了"])
                self.assertNotIn((UMO, jobs_mod.KIND_TEMP, "t-1"), h.scheduler.jobs)
                # 临时作业本身不重建，但"她说了话 → 对话 job 必须更新"
                self.assertIn((UMO, jobs_mod.KIND_DIALOGUE, None), h.scheduler.jobs)
                self.assertEqual(trace["temp_note"], "两分钟内别说话")


class EngineAddTempTests(unittest.IsolatedAsyncioTestCase):
    async def test_add_temp_and_limit(self) -> None:
        settings = make_settings(**{"temp.max_jobs": 1})
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, [], settings=settings) as h:
                now = timeutil.now(TZ)
                first = h.engine.add_temp(UMO, now + timedelta(minutes=2), "提醒一")
                self.assertIsNotNone(first)
                second = h.engine.add_temp(UMO, now + timedelta(minutes=3), "提醒二")
                self.assertIsNone(second)
                self.assertEqual(len(h.store.session(UMO)["jobs"]["temp"]), 1)


# ----------------------------------------------------------------------
# 调度器（真实 APScheduler）
# ----------------------------------------------------------------------
class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_kinds_and_temp_ids(self) -> None:
        from core.scheduler import WakeScheduler

        fired: list[tuple] = []

        async def on_fire(umo, kind, temp_id):
            fired.append((umo, kind, temp_id))

        scheduler = WakeScheduler(on_fire=on_fire, timezone_name="Asia/Shanghai")
        scheduler.start()
        try:
            now = timeutil.now(scheduler.tz)
            scheduler.set_wake(UMO, now + timedelta(minutes=5), kind=jobs_mod.KIND_DIALOGUE)
            scheduler.set_wake(UMO, now + timedelta(minutes=6), kind=jobs_mod.KIND_HOBBY)
            scheduler.set_wake(UMO, now + timedelta(seconds=0.4), kind=jobs_mod.KIND_TEMP, temp_id="t1")
            self.assertEqual(
                scheduler.job_id(UMO, jobs_mod.KIND_TEMP, "t1"),
                f"pmind:{UMO}:temp:t1",
            )
            for _ in range(30):
                await asyncio.sleep(0.2)
                if fired:
                    break
            self.assertEqual(fired, [(UMO, jobs_mod.KIND_TEMP, "t1")])
            self.assertTrue(scheduler.cancel(UMO, kind=jobs_mod.KIND_TEMP, temp_id="t1") is False)
            self.assertTrue(scheduler.cancel(UMO, kind=jobs_mod.KIND_DIALOGUE))
        finally:
            await scheduler.shutdown()

    async def test_restore_three_kinds(self) -> None:
        from core.scheduler import WakeScheduler

        async def on_fire(umo, kind, temp_id):
            return

        scheduler = WakeScheduler(on_fire=on_fire, timezone_name="Asia/Shanghai")
        scheduler.start()
        try:
            now = timeutil.now(scheduler.tz)
            jobs = jobs_mod.default_jobs()
            jobs["dialogue"]["next_at"] = timeutil.iso(now + timedelta(minutes=10))
            jobs["hobby"]["next_at"] = timeutil.iso(now - timedelta(hours=1))
            jobs["temp"] = [
                {"id": "t9", "next_at": timeutil.iso(now + timedelta(minutes=20)), "note": "x"}
            ]
            actions = scheduler.restore(
                {UMO: {"jobs": jobs}},
                catch_up=False,
                default_interval_minutes=120,
                min_interval_minutes=5,
                hobby_default_interval_minutes=180,
                hobby_min_interval_minutes=30,
            )
            self.assertEqual(actions[f"{UMO}|dialogue"], "restored")
            self.assertEqual(actions[f"{UMO}|hobby"], "postponed")
            self.assertEqual(actions[f"{UMO}|temp:t9"], "restored")
            self.assertIsNotNone(scheduler.next_run(UMO, kind=jobs_mod.KIND_TEMP, temp_id="t9"))
        finally:
            await scheduler.shutdown()


# ----------------------------------------------------------------------
# 面板辅助
# ----------------------------------------------------------------------
class WebapiHelperTests(unittest.TestCase):
    def test_brief_args_truncates_long_values(self) -> None:
        """工具入参记录：长字符串截断，嵌套只留概要。"""
        from core.decide import _brief_args

        brief = _brief_args(
            {
                "url": "https://www.bilibili.com/video/BV1Pv4764Ear?" + "x" * 300,
                "wait": 3,
                "flag": True,
                "items": [1, 2, 3],
                "nested": {"a": 1},
                "none": None,
            }
        )
        self.assertTrue(brief["url"].endswith("…"))
        self.assertLessEqual(len(brief["url"]), 161)
        self.assertEqual(brief["wait"], "3")
        self.assertEqual(brief["flag"], "True")
        self.assertEqual(brief["items"], "[3 items]")
        self.assertEqual(brief["nested"], "{…}")
        self.assertEqual(brief["none"], "None")
        self.assertEqual(_brief_args(None), {})

    def test_boilerplate_reasons_not_marked_repeated(self) -> None:
        """记录模式这类套话一字不差也不算复读（没有判断信息）。"""
        from core import webapi

        plugin = SimpleNamespace(
            settings=make_settings(**{"panel.trace_similarity": 0.8}), log=None
        )
        api = webapi.PageApi(plugin)
        traces = [
            {"reason": "记录模式：主 Agent 的回复即本轮执行体（不额外调用模型、不补话）"},
            {"reason": "记录模式：她没说下次时间，按随机区间兜底"},
            {"reason": "主 Agent 到点发言后记账"},
        ]
        marked = api._mark_duplicates(traces)
        for item in marked:
            self.assertFalse(item.get("looks_repeated"), item.get("reason"))
            self.assertNotIn("reason_similarity", item)

    def test_mark_duplicates(self) -> None:
        from core import webapi

        plugin = SimpleNamespace(
            settings=make_settings(**{"panel.trace_similarity": 0.8}), log=None
        )
        api = webapi.PageApi(plugin)
        traces = [
            {"reason": "晾着他，看他打不打那三个字"},
            {"reason": "晾着他，看他打不打那三个字"},
            {"reason": "完全不同的另一件事"},
        ]
        marked = api._mark_duplicates(traces)
        self.assertTrue(marked[1]["looks_repeated"])
        self.assertFalse(marked[0].get("looks_repeated"))


class DeliveryTests(unittest.TestCase):
    def test_trim(self) -> None:
        self.assertEqual(delivery.trim_message("abc", 10), "abc")
        self.assertEqual(delivery.trim_message("abcdef", 3), "abc")

    def test_replace_comma_to_three_spaces(self) -> None:
        from core import textfx

        rules = textfx.parse_rules([{"pattern": "[，,]", "replacement": "   "}])
        self.assertEqual(
            textfx.apply_replacements("吃了吗，在干嘛,嗯", rules), "吃了吗   在干嘛   嗯"
        )

    def test_split_by_regex(self) -> None:
        from core import textfx

        segments = textfx.split_text("第一句。第二句！第三句？", r"[^。！？]+[。！？]?")
        self.assertEqual(len(segments), 3)
        self.assertTrue(segments[0].startswith("第一句"))
        # 超过上限时合并，不丢字
        merged = textfx.split_text("a。b。c。d。e。", r"[^。]+。", max_segments=2)
        self.assertEqual(len(merged), 2)
        self.assertIn("e", merged[-1])

    def test_cleanup_regex(self) -> None:
        from core import textfx

        self.assertEqual(textfx.cleanup_text("好的。", r"[。！？]"), "好的")
        self.assertEqual(textfx.cleanup_text("没有标记", ""), "没有标记")

    def test_transform_pipeline(self) -> None:
        from core import textfx

        settings = make_settings(
            **{
                "outbound.segment_enabled": "true",
                "outbound.segment_pattern": r"[^。]+。",
                "outbound.cleanup_enabled": "true",
                "outbound.cleanup_pattern": r"[。]",
                "outbound.replace_enabled": "true",
                "outbound.replace_rules": [{"pattern": "[，,]", "replacement": "   "}],
            }
        )
        segments = textfx.transform("先说一句，再说一句。最后一句。", settings)
        self.assertEqual(len(segments), 2)
        self.assertIn("   ", segments[0])  # 逗号被换成 3 个空格
        self.assertNotIn("。", segments[0])  # 句号被过滤掉

    def test_bad_regex_never_eats_text(self) -> None:
        from core import textfx

        settings = make_settings(
            **{
                "outbound.segment_enabled": "true",
                "outbound.segment_pattern": "([bad",
                "outbound.replace_enabled": "true",
                "outbound.replace_rules": [{"pattern": "([bad", "replacement": "x"}],
            }
        )
        self.assertEqual(textfx.transform("原文不动", settings), ["原文不动"])

    def test_parse_rules_text_form(self) -> None:
        from core import textfx

        rules = textfx.parse_rules("，=>   \n,=> ")
        self.assertEqual(len(rules), 2)
        self.assertEqual(rules[0]["replacement"], "")


class EchoTests(unittest.IsolatedAsyncioTestCase):
    async def test_assistant_record(self) -> None:
        record = echo.build_assistant_record("你好")
        self.assertEqual(record["role"], "assistant")
        self.assertEqual(record["content"][0]["text"], "你好")


if __name__ == "__main__":
    unittest.main()
