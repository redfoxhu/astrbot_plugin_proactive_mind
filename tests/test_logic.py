"""主动心智插件单元测试（仅标准库 + 宿主 venv 里已有的 apscheduler）。

运行：
    cd <plugin_dir>
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import json
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

from core import decide as decide_mod  # noqa: E402
from core import delivery, echo, flow, llm, memory, state, timeutil  # noqa: E402
from core.settings import Settings  # noqa: E402
from core.state import StateStore  # noqa: E402

TZ = timezone(timedelta(hours=8))


def make_settings(**overrides) -> Settings:
    cfg = {}
    for key, value in overrides.items():
        if "." in key:
            section, name = key.split(".", 1)
            cfg.setdefault(section, {})[name] = value
        else:
            raise ValueError(f"配置键需要用 section.key 形式：{key}")
    return Settings(cfg)


class TimeutilTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 11, 16, 20, 0, 0, tzinfo=TZ)

    def test_parse_iso_with_offset(self) -> None:
        parsed = timeutil.parse_wake_time(
            "2026-11-17T08:30:00+08:00", reference=self.now, tz=TZ
        )
        self.assertEqual(parsed.hour, 8)
        self.assertEqual(parsed.minute, 30)

    def test_parse_relative_minutes(self) -> None:
        parsed = timeutil.parse_wake_time("+90m", reference=self.now, tz=TZ)
        self.assertEqual(parsed, self.now + timedelta(minutes=90))

    def test_parse_relative_hours_and_bare_number(self) -> None:
        self.assertEqual(
            timeutil.parse_wake_time("2h", reference=self.now, tz=TZ),
            self.now + timedelta(hours=2),
        )
        self.assertEqual(
            timeutil.parse_wake_time("45", reference=self.now, tz=TZ),
            self.now + timedelta(minutes=45),
        )

    def test_parse_clock_only_rolls_to_tomorrow(self) -> None:
        parsed = timeutil.parse_wake_time("08:00", reference=self.now, tz=TZ)
        self.assertEqual(parsed.date(), (self.now + timedelta(days=1)).date())
        self.assertEqual(parsed.hour, 8)

    def test_parse_epoch_and_garbage(self) -> None:
        epoch = int(self.now.timestamp())
        self.assertEqual(
            timeutil.parse_wake_time(epoch, reference=self.now, tz=TZ).hour, 20
        )
        self.assertIsNone(timeutil.parse_wake_time("明天下午吧", reference=self.now, tz=TZ))
        self.assertIsNone(timeutil.parse_wake_time(None, reference=self.now, tz=TZ))

    def test_clamp(self) -> None:
        early = self.now + timedelta(seconds=10)
        clamped = timeutil.clamp_wake(
            early, reference=self.now, min_minutes=5, max_minutes=60, default_minutes=30
        )
        self.assertEqual(clamped, self.now + timedelta(minutes=5))

        late = self.now + timedelta(days=3)
        clamped = timeutil.clamp_wake(
            late, reference=self.now, min_minutes=5, max_minutes=60, default_minutes=30
        )
        self.assertEqual(clamped, self.now + timedelta(minutes=60))

        fallback = timeutil.clamp_wake(
            None, reference=self.now, min_minutes=5, max_minutes=60, default_minutes=30
        )
        self.assertEqual(fallback, self.now + timedelta(minutes=30))

    def test_human_and_iso_roundtrip(self) -> None:
        text = timeutil.iso(self.now)
        self.assertEqual(timeutil.from_iso(text), self.now)
        self.assertIn("周一", timeutil.human(self.now))


class SettingsTests(unittest.TestCase):
    def test_defaults(self) -> None:
        settings = Settings({})
        self.assertEqual(settings.flow_message_count, 50)
        self.assertEqual(settings.max_consecutive_wakes, 3)
        self.assertTrue(settings.persist_platform)
        self.assertTrue(settings.persist_livingmemory)
        self.assertEqual(settings.wake_delay_minutes, 0)

    def test_clamping(self) -> None:
        settings = make_settings(
            **{
                "limit.max_consecutive_wakes": 500,
                "flow.flow_message_count": 1,
                "wake.wake_delay_minutes": -5,
                "wake.min_interval_minutes": 30,
                "wake.max_interval_minutes": 10,
            }
        )
        self.assertEqual(settings.max_consecutive_wakes, 100)
        self.assertEqual(settings.flow_message_count, 5)
        self.assertEqual(settings.wake_delay_minutes, 0)
        self.assertEqual(settings.max_interval_minutes, 30)

    def test_negative_infinity_cap(self) -> None:
        settings = make_settings(**{"limit.max_consecutive_wakes": "-1"})
        self.assertEqual(settings.max_consecutive_wakes, -1)

    def test_blacklist_always_contains_guards(self) -> None:
        settings = make_settings(**{"mind.tool_blacklist": "bilibili_read"})
        self.assertIn("future_task", settings.tool_blacklist)
        self.assertIn("send_message_to_user", settings.tool_blacklist)
        self.assertIn("bilibili_read", settings.tool_blacklist)

    def test_update_writes_typed_values(self) -> None:
        raw = {}
        settings = Settings(raw)
        applied = settings.update(
            {
                "limit.max_consecutive_wakes": "7",
                "wake.wake_delay_minutes": 15,
                "unknown.key": 1,
            }
        )
        self.assertEqual(
            sorted(applied), ["limit.max_consecutive_wakes", "wake.wake_delay_minutes"]
        )
        self.assertEqual(raw["limit"]["max_consecutive_wakes"], 7)
        self.assertEqual(raw["wake"]["wake_delay_minutes"], 15)
        self.assertEqual(settings.max_consecutive_wakes, 7)


class StateTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_defaults_and_persist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = StateStore(path, trace_keep=5)
            await store.load()
            session = store.session("aiocqhttp:FriendMessage:1")
            self.assertEqual(session["silent_wake_count"], 0)
            store.set(
                "aiocqhttp:FriendMessage:1",
                next_wake_at="2026-11-17T08:00:00+08:00",
                silent_wake_count=2,
            )
            await store.save()

            reloaded = StateStore(path, trace_keep=5)
            await reloaded.load()
            self.assertEqual(
                reloaded.get("aiocqhttp:FriendMessage:1", "silent_wake_count"), 2
            )

    async def test_trace_trim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.json", trace_keep=5)
            await store.load()
            for index in range(9):
                trace = store.new_trace("umo", "cron")
                trace["reason"] = f"n{index}"
                store.add_trace(trace)
            traces = store.traces()
            self.assertEqual(len(traces), 5)
            self.assertEqual(traces[0]["reason"], "n8")

    async def test_corrupt_file_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{not json", encoding="utf-8")
            store = StateStore(path, trace_keep=5)
            await store.load()
            self.assertEqual(store.all_sessions(), {})
            self.assertTrue(any(Path(tmp).glob("state.json.corrupt-*")))


class FlowTests(unittest.TestCase):
    def test_content_to_text_variants(self) -> None:
        self.assertEqual(flow.content_to_text("hi"), "hi")
        self.assertEqual(
            flow.content_to_text([{"type": "text", "text": "a"}, {"type": "think", "think": "b"}]),
            "a",
        )
        self.assertEqual(flow.content_to_text({"text": "x"}), "x")
        self.assertEqual(flow.content_to_text('{"text": "y"}'), "y")

    def test_records_to_items_marks_bot(self) -> None:
        items = flow.records_to_items(
            [
                {"role": "user", "content": [{"type": "text", "text": "在吗"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "在"}]},
                {"role": "tool", "content": "ignored"},
            ]
        )
        self.assertEqual(len(items), 2)
        self.assertFalse(items[0].is_bot)
        self.assertTrue(items[1].is_bot)
        self.assertEqual(items[1].speaker(), flow.BOT_LABEL)

    def test_rows_to_items_reads_proactive_flag(self) -> None:
        rows = [
            ("user", "你好", "u1", "小明", 1794826672.0, "{}"),
            (
                "assistant",
                "我主动说的",
                "b1",
                "Bot",
                1794826800.0,
                json.dumps({"is_bot_message": True, "proactive_mind": True}),
            ),
        ]
        items = flow.rows_to_items(rows)
        self.assertEqual(len(items), 2)
        self.assertTrue(items[1].is_bot)
        self.assertTrue(items[1].proactive)
        self.assertEqual(items[1].speaker(), flow.PROACTIVE_LABEL)

    def test_format_flow_truncates_from_oldest(self) -> None:
        items = [
            flow.FlowItem(role="user", text=f"消息{index}" * 10, ts=1794826672.0 + index)
            for index in range(50)
        ]
        text = flow.format_flow(items, max_chars=300)
        self.assertIn("更早的流水已省略", text)
        self.assertIn("消息49", text)
        self.assertNotIn("消息0消息0", text)

    def test_merge_proactive_dedupes(self) -> None:
        settings = make_settings()
        session = {"last_sent_message": "我刚说的话"}
        items = [flow.FlowItem(role="assistant", text="我刚说的话", is_bot=True)]
        merged = flow.merge_proactive(items, session, settings)
        self.assertEqual(len(merged), 1)

        merged = flow.merge_proactive([], session, settings)
        self.assertEqual(len(merged), 1)
        self.assertTrue(merged[0].proactive)

    def test_sanitize_text_strips_noise(self) -> None:
        raw = (
            "谢谢老婆（几口吃完） [MSG_ID:433752066]\n\n"
            "<system_reminder>Current datetime: 2026-11-16 18:57 (CST)</system_reminder>"
        )
        cleaned = flow.sanitize_text(raw)
        self.assertNotIn("MSG_ID", cleaned)
        self.assertNotIn("system_reminder", cleaned)
        self.assertIn("谢谢老婆", cleaned)
        self.assertFalse(cleaned.endswith("\n"))

    def test_format_flow_one_copy_per_message(self) -> None:
        items = [
            flow.FlowItem(role="user", text="在吗 [MSG_ID:1]", ts=1794826672.0),
            flow.FlowItem(role="assistant", text="在", ts=1794826680.0, is_bot=True),
        ]
        text = flow.format_flow(items, 12000)
        self.assertEqual(text.count("在吗"), 1)
        self.assertEqual(text.count("我（Bot）"), 1)
        self.assertNotIn("MSG_ID", text)


class LlmJsonTests(unittest.TestCase):
    def test_plain_json(self) -> None:
        self.assertEqual(llm.extract_json('{"act": true}'), {"act": True})

    def test_fenced_json_with_noise(self) -> None:
        raw = '好的，判断如下：\n```json\n{"act": false, "reason": "在忙"}\n```\n完毕'
        self.assertEqual(llm.extract_json(raw), {"act": False, "reason": "在忙"})

    def test_nested_braces(self) -> None:
        raw = 'prefix {"steps": [{"tool": "x", "args": {"a": {"b": 1}}}]} suffix'
        data = llm.extract_json(raw)
        self.assertEqual(data["steps"][0]["args"]["a"]["b"], 1)

    def test_invalid_returns_none(self) -> None:
        self.assertIsNone(llm.extract_json("完全不是 JSON"))
        self.assertIsNone(llm.extract_json(""))


class FakeProvider:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    async def text_chat(self, prompt=None, system_prompt=None, contexts=None, **kwargs):
        self.calls += 1
        reply = self.replies.pop(0) if self.replies else "{}"
        return SimpleNamespace(completion_text=reply)


class FakeContext:
    def __init__(self, provider=None):
        self.provider = provider
        self.provider_manager = None
        self.persona_manager = None
        self.conversation_manager = None

    async def get_using_provider_async(self, umo=None):
        return self.provider

    def get_llm_tool_manager(self):
        return SimpleNamespace(get_full_tool_set=lambda: SimpleNamespace(tools=[]))


class LlmChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_after_invalid_json(self) -> None:
        provider = FakeProvider(["不是 JSON", '{"act": true}'])
        context = FakeContext(provider)
        settings = make_settings()
        data, raw, errors = await llm.chat_json(
            context, settings, "umo", system_prompt="s", user_prompt="u"
        )
        self.assertEqual(data, {"act": True})
        self.assertIn("invalid_json", errors)
        self.assertEqual(provider.calls, 2)

    async def test_provider_missing(self) -> None:
        settings = make_settings()
        data, raw, errors = await llm.chat_json(
            FakeContext(None), settings, "umo", system_prompt="s", user_prompt="u"
        )
        self.assertIsNone(data)
        self.assertIn("provider_unavailable", errors)


class MemoryTests(unittest.TestCase):
    def test_build_memory_text(self) -> None:
        text = memory.build_memory_text(
            trigger="预定唤醒到点",
            thought="想问问她吃饭没",
            intent="看看视频再决定",
            steps=[{"tool": "bilibili_read", "ok": True}],
            message="吃饭了吗",
            next_wake_human="2026-11-17 08:00 周二",
        )
        self.assertIn("主动行为", text)
        self.assertIn("bilibili_read（成功）", text)
        self.assertIn("我说的话：吃饭了吗", text)
        self.assertIn("下次唤醒", text)


class FakeConversationManager:
    def __init__(self, history=None, conversation_id="c1"):
        self.history = list(history or [])
        self.conversation_id = conversation_id
        self.updated = 0

    async def get_curr_conversation_id(self, umo):
        return self.conversation_id

    async def new_conversation(self, umo):
        self.conversation_id = "c-new"
        return self.conversation_id

    async def get_conversation(self, umo, cid):
        return SimpleNamespace(history=json.dumps(self.history, ensure_ascii=False))

    async def update_conversation(self, umo, cid, history=None, **kwargs):
        self.history = list(history or [])
        self.updated += 1


class EchoTests(unittest.IsolatedAsyncioTestCase):
    async def test_assistant_record_shape(self) -> None:
        record = echo.build_assistant_record("你好")
        self.assertEqual(record["role"], "assistant")
        self.assertEqual(record["content"], [{"type": "text", "text": "你好"}])

    async def test_platform_history_append_and_verify(self) -> None:
        manager = FakeConversationManager(
            [{"role": "user", "content": [{"type": "text", "text": "在吗"}]}]
        )
        context = SimpleNamespace(conversation_manager=manager)
        result = await echo.echo_platform_history(context, umo="aiocqhttp:FriendMessage:1", text="我在")
        self.assertTrue(result["ok"])
        self.assertEqual(manager.history[-1]["role"], "assistant")
        self.assertEqual(manager.history[-1]["content"][0]["text"], "我在")

        again = await echo.echo_platform_history(context, umo="aiocqhttp:FriendMessage:1", text="我在")
        self.assertTrue(again.get("deduped"))

    async def test_livingmemory_write_marks_bot(self) -> None:
        calls = {}

        class FakeLivingMemoryManager:
            async def add_message(self, **kwargs):
                calls.update(kwargs)
                return SimpleNamespace(id=42)

            async def get_messages(self, umo, limit=10):
                return [
                    SimpleNamespace(role="assistant", sender_id="10001", sender_name="小蓝")
                ]

        plugin = SimpleNamespace(
            initializer=SimpleNamespace(conversation_manager=FakeLivingMemoryManager())
        )
        settings = make_settings()
        context = SimpleNamespace(conversation_manager=None)
        with mock.patch.object(flow, "find_livingmemory", return_value=plugin):
            result = await echo.echo_livingmemory(
                context,
                settings,
                umo="aiocqhttp:FriendMessage:1",
                text="我主动说的",
                session={},
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["message_id"], 42)
        self.assertEqual(calls["role"], "assistant")
        self.assertTrue(calls["is_bot_message"])
        self.assertEqual(calls["sender_id"], "10001")
        self.assertEqual(calls["platform"], "aiocqhttp")
        self.assertTrue(calls["metadata"]["proactive_mind"])


class FakeScheduler:
    def __init__(self):
        self.tz = TZ
        self.jobs = {}
        self.started = True
        self.cancelled = []

    def set_wake(self, umo, when):
        self.jobs[umo] = when
        return when

    def cancel(self, umo):
        self.cancelled.append(umo)
        return self.jobs.pop(umo, None) is not None

    def next_run(self, umo):
        return self.jobs.get(umo)


class FakeStore(StateStore):
    def __init__(self, tmp_path):
        super().__init__(Path(tmp_path) / "state.json", trace_keep=10)


class EngineHarness:
    """用假依赖装配 DecisionEngine，验证三段式流程。"""

    def __init__(self, tmp_path, script, *, settings=None, session_override=None):
        self.tmp = tmp_path
        self.store = FakeStore(tmp_path)
        self.scheduler = FakeScheduler()
        self.settings = settings or make_settings()
        self.script = list(script)
        self.sent = []
        self.echoed = []
        self.memorized = []
        self.context = SimpleNamespace(
            conversation_manager=None,
            persona_manager=None,
            get_llm_tool_manager=lambda: SimpleNamespace(
                get_full_tool_set=lambda: SimpleNamespace(tools=[])
            ),
        )

        async def fake_send(context, *, umo, text, log=None):
            self.sent.append(text)
            return {"ok": True}

        async def fake_echo(context, settings, *, umo, text, session=None, log=None):
            self.echoed.append(text)
            return {
                "platform_history": {"ok": True},
                "livingmemory": {"ok": True},
            }

        async def fake_memory(context, settings, **kwargs):
            self.memorized.append(kwargs.get("content"))
            return {"ok": True}

        async def fake_chat_json(context, settings, umo, *, system_prompt, user_prompt, log=None):
            data = self.script.pop(0) if self.script else None
            return data, json.dumps(data, ensure_ascii=False) if data else "", []

        self._patches = [
            mock.patch.object(decide_mod.llm, "chat_json", fake_chat_json),
            mock.patch.object(decide_mod.delivery, "send_text", fake_send),
            mock.patch.object(decide_mod.echo, "echo_all", fake_echo),
            mock.patch.object(decide_mod.memory, "write_memory", fake_memory),
            mock.patch.object(
                decide_mod.flow_mod,
                "read_flow",
                self._fake_read_flow,
            ),
        ]
        self.session_override = session_override or {}

    async def _fake_read_flow(self, context, settings, umo, log=None):
        items = [
            flow.FlowItem(role="user", text="今天的饭好咸", ts=1794826672.0),
            flow.FlowItem(role="assistant", text="下次别去那家了", ts=1794826680.0, is_bot=True),
        ]
        return items, "livingmemory", []

    async def __aenter__(self):
        for patch in self._patches:
            patch.start()
        await self.store.load()
        if self.session_override:
            self.store.set("umo", **self.session_override)
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


class DecisionEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_branch_schedules_without_message(self) -> None:
        script = [
            {"act": False, "reason": "刚聊完，先不打扰", "next_wake_at": "+90m"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            async with EngineHarness(tmp, script) as harness:
                trace = await harness.engine.run("umo", "cron")
                self.assertFalse(trace["act"])
                self.assertEqual(harness.sent, [])
                self.assertEqual(harness.memorized, [])
                self.assertIn("umo", harness.scheduler.jobs)
                self.assertEqual(trace["next_wake_at"] is not None, True)

    async def test_action_branch_runs_tools_sends_and_memories(self) -> None:
        script = [
            {"act": True, "reason": "想知道她在干嘛"},
            {"intent": "先看看她最近说过什么", "steps": []},
            {
                "thought": "干脆问问她",
                "message": "在忙吗",
                "next_wake_at": "+2h",
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            async with EngineHarness(tmp, script) as harness:
                trace = await harness.engine.run("umo", "cron")
                self.assertTrue(trace["act"])
                self.assertEqual(harness.sent, ["在忙吗"])
                self.assertTrue(trace["message_sent"])
                self.assertEqual(harness.echoed, ["在忙吗"])
                self.assertEqual(len(harness.memorized), 1)
                self.assertIn("在忙吗", harness.memorized[0])
                self.assertTrue(harness.scheduler.jobs)

    async def test_action_without_message_still_memories(self) -> None:
        script = [
            {"act": True, "reason": "想自己刷会视频"},
            {"intent": "刷会儿 b 站", "steps": []},
            {"thought": "不想打扰她", "message": None, "next_wake_at": "+3h"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            async with EngineHarness(tmp, script) as harness:
                trace = await harness.engine.run("umo", "cron")
                self.assertEqual(harness.sent, [])
                self.assertFalse(trace["message_sent"])
                self.assertEqual(harness.echoed, [])
                self.assertEqual(len(harness.memorized), 1)
                self.assertIn("刷会儿 b 站", harness.memorized[0])

    async def test_consecutive_limit_blocks_new_job(self) -> None:
        script = [
            {"act": False, "reason": "安静", "next_wake_at": "+30m"},
        ]
        settings = make_settings(**{"limit.max_consecutive_wakes": 1})
        session = {
            "silent_wake_count": 1,
            "user_replied_since_last_wake": False,
        }
        with tempfile.TemporaryDirectory() as tmp:
            async with EngineHarness(
                tmp, script, settings=settings, session_override=session
            ) as harness:
                trace = await harness.engine.run("umo", "cron")
                self.assertEqual(trace["schedule_blocked"], "consecutive_limit_reached")
                self.assertNotIn("umo", harness.scheduler.jobs)
                stored = harness.store.session("umo")
                self.assertIsNone(stored["next_wake_at"])
                self.assertEqual(stored["last_error"], "consecutive_limit_reached")

    async def test_cap_zero_allows_after_user_reply(self) -> None:
        script = [
            {"act": False, "reason": "先安静", "next_wake_at": "+30m"},
        ]
        settings = make_settings(**{"limit.max_consecutive_wakes": 0})
        session = {
            "silent_wake_count": 0,
            "user_replied_since_last_wake": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            async with EngineHarness(
                tmp, script, settings=settings, session_override=session
            ) as harness:
                await harness.engine.run("umo", "user_message")
                self.assertIn("umo", harness.scheduler.jobs)
                self.assertEqual(
                    harness.store.session("umo")["silent_wake_count"], 1
                )

    async def test_schedule_only_mode_never_sends(self) -> None:
        script = [
            {"act": True, "reason": "她想安静", "next_wake_at": "+45m"},
        ]
        settings = make_settings(**{"basic.user_message_mode": "schedule_only"})
        with tempfile.TemporaryDirectory() as tmp:
            async with EngineHarness(tmp, script, settings=settings) as harness:
                trace = await harness.engine.run("umo", "user_message")
                self.assertEqual(harness.sent, [])
                self.assertEqual(harness.memorized, [])
                self.assertIn("umo", harness.scheduler.jobs)
                self.assertTrue(trace.get("reason_short"))

    async def test_gate_failure_still_schedules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            async with EngineHarness(tmp, [None]) as harness:
                trace = await harness.engine.run("umo", "cron")
                self.assertIsNone(trace["act"])
                self.assertIn("umo", harness.scheduler.jobs)


class ToolCatalogTests(unittest.TestCase):
    def _tools(self):
        names = [
            "memorize_long_term_memory",
            "recall_long_term_memory",
            "bilibili_read",
            "qts_get_user_info",
            "qts_delete_message",
            "qts_stop_conversation",
            "qts_refresh_messages",
            "send_message_to_user",
            "future_task",
            "admin_kick_user",
            "some_random_thing",
        ]
        return [FakeTool(name, "ok") for name in names]

    def _context(self, tools):
        return SimpleNamespace(
            get_llm_tool_manager=lambda: SimpleNamespace(
                get_full_tool_set=lambda: SimpleNamespace(
                    tools=tools,
                    get_tool=lambda name: next(
                        (t for t in tools if t.name == name), None
                    ),
                )
            )
        )

    def test_default_catalog_filters_destructive_tools(self) -> None:
        from core import toolbox

        tools = self._tools()
        catalog = toolbox.build_catalog(self._context(tools), make_settings())
        names = [item["name"] for item in catalog]
        self.assertIn("memorize_long_term_memory", names)
        self.assertIn("recall_long_term_memory", names)
        self.assertIn("bilibili_read", names)
        self.assertNotIn("qts_delete_message", names)
        self.assertNotIn("qts_stop_conversation", names)
        self.assertNotIn("qts_refresh_messages", names)
        self.assertNotIn("send_message_to_user", names)
        self.assertNotIn("future_task", names)
        # 记忆/检索类排在最前
        self.assertEqual(names[0], "memorize_long_term_memory")

    def test_whitelist_overrides_deny_pattern(self) -> None:
        from core import toolbox

        tools = self._tools()
        default_names = [
            item["name"] for item in toolbox.build_catalog(self._context(tools), make_settings())
        ]
        self.assertNotIn("admin_kick_user", default_names)

        settings = make_settings(**{"mind.tool_whitelist": "admin_kick_user"})
        catalog = toolbox.build_catalog(self._context(tools), settings)
        self.assertEqual([item["name"] for item in catalog], ["admin_kick_user"])

    def test_blacklist_wins_over_whitelist(self) -> None:
        from core import toolbox

        settings = make_settings(**{"mind.tool_whitelist": "qts_delete_message"})
        catalog = toolbox.build_catalog(self._context(self._tools()), settings)
        self.assertEqual(catalog, [])

    def test_blacklist_blocks_execution(self) -> None:
        from core import toolbox

        settings = make_settings()
        self.assertFalse(toolbox.is_allowed("future_task", settings, for_catalog=False))
        self.assertTrue(toolbox.is_allowed("bilibili_read", settings, for_catalog=False))

    def test_browser_tools_are_last_and_bilibili_first(self) -> None:
        from core import toolbox

        tools = [
            FakeTool(f"qts_browser_{index}", "ok") for index in range(30)
        ] + [
            FakeTool("bilibili_read", "ok"),
            FakeTool("memorize_long_term_memory", "ok"),
            FakeTool("qts_get_user_info", "ok"),
        ]
        catalog = toolbox.build_catalog(self._context(tools), make_settings())
        names = [item["name"] for item in catalog]
        self.assertEqual(len(names), 24)
        self.assertIn("bilibili_read", names)
        self.assertIn("memorize_long_term_memory", names)
        # 重操作工具被挤到后面（条目有限时优先保留核心工具）
        self.assertNotIn("qts_browser_29", names)


class DeliveryTests(unittest.TestCase):
    def test_trim_message(self) -> None:
        self.assertEqual(delivery.trim_message("abc", 10), "abc")
        self.assertEqual(delivery.trim_message("abcdef", 3), "abc")
        self.assertEqual(delivery.trim_message(None, 10), "")


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_job_fires_replace_and_cancel(self) -> None:
        from core.scheduler import WakeScheduler

        fired: list[str] = []

        async def on_fire(umo: str) -> None:
            fired.append(umo)

        scheduler = WakeScheduler(on_fire=on_fire, timezone_name="Asia/Shanghai")
        scheduler.start()
        try:
            umo = "aiocqhttp:FriendMessage:1"
            now = timeutil.now(scheduler.tz)
            scheduler.set_wake(umo, now + timedelta(seconds=30))
            self.assertIsNotNone(scheduler.next_run(umo))

            # 替换成 0.4 秒后触发
            scheduler.set_wake(umo, timeutil.now(scheduler.tz) + timedelta(seconds=0.4))
            for _ in range(30):
                await asyncio.sleep(0.2)
                if fired:
                    break
            self.assertEqual(fired, [umo])

            other = "aiocqhttp:FriendMessage:2"
            scheduler.set_wake(other, timeutil.now(scheduler.tz) + timedelta(minutes=5))
            self.assertTrue(scheduler.cancel(other))
            self.assertIsNone(scheduler.next_run(other))
        finally:
            await scheduler.shutdown()
        self.assertFalse(scheduler.started)

    async def test_restore_actions(self) -> None:
        from core.scheduler import WakeScheduler

        async def on_fire(umo: str) -> None:
            return

        scheduler = WakeScheduler(on_fire=on_fire, timezone_name="Asia/Shanghai")
        scheduler.start()
        try:
            now = timeutil.now(scheduler.tz)
            sessions = {
                "a": {"next_wake_at": timeutil.iso(now + timedelta(minutes=10))},
                "b": {"next_wake_at": timeutil.iso(now - timedelta(hours=1))},
                "c": {"next_wake_at": None},
            }
            actions = scheduler.restore(
                sessions,
                catch_up=False,
                default_interval_minutes=120,
                min_interval_minutes=5,
            )
            self.assertEqual(actions["a"], "restored")
            self.assertEqual(actions["b"], "postponed")
            self.assertNotIn("c", actions)
        finally:
            await scheduler.shutdown()


class FakeTool:
    """最小可用的 FunctionTool 替身。"""

    def __init__(self, name: str, result: str):
        self.name = name
        self.description = f"{name} 的描述"
        self.parameters = {"type": "object", "properties": {}}
        self.active = True
        self.result = result
        self.calls: list[dict] = []

    async def call(self, context, **kwargs):
        self.calls.append(kwargs)
        return self.result


class RealPathTests(unittest.IsolatedAsyncioTestCase):
    """不替换 llm / memory，验证 Provider→JSON→工具→记忆 的真实链路。"""

    async def test_provider_tools_and_memory_roundtrip(self) -> None:
        replies = [
            json.dumps({"act": True, "reason": "想问她吃了没"}),
            json.dumps(
                {
                    "intent": "先看看她最近在追什么",
                    "steps": [{"tool": "bilibili_read", "args": {"keyword": "下饭"}}],
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "thought": "她可能在忙",
                    "message": "刚看到你追的那部更新了",
                    "next_wake_at": "+90m",
                },
                ensure_ascii=False,
            ),
        ]
        provider = FakeProvider(replies)
        biliread = FakeTool("bilibili_read", "视频：香锅教学")
        memorize = FakeTool("memorize_long_term_memory", '{"memorized": true}')
        tools = [biliread, memorize]

        def make_tool_set():
            return SimpleNamespace(
                tools=tools,
                get_tool=lambda name: next((t for t in tools if t.name == name), None),
            )

        context = SimpleNamespace(
            conversation_manager=None,
            persona_manager=None,
            provider=None,
            provider_manager=None,
            get_using_provider_async=lambda umo=None: self._provider_async(provider),
            get_llm_tool_manager=lambda: SimpleNamespace(get_full_tool_set=make_tool_set),
        )

        sent: list[str] = []
        echoed: list[str] = []

        async def fake_send(ctx, *, umo, text, log=None):
            sent.append(text)
            return {"ok": True}

        async def fake_echo(ctx, settings, *, umo, text, session=None, log=None):
            echoed.append(text)
            return {"platform_history": {"ok": True}, "livingmemory": {"ok": True}}

        async def fake_read_flow(ctx, settings, umo, log=None):
            return [flow.FlowItem(role="user", text="在吗", ts=1794826672.0)], "livingmemory", []

        with tempfile.TemporaryDirectory() as tmp:
            store = FakeStore(tmp)
            await store.load()
            scheduler = FakeScheduler()
            settings = make_settings()
            engine = decide_mod.DecisionEngine(
                context=context,
                settings=settings,
                store=store,
                scheduler=scheduler,
                data_dir=tmp,
            )
            with mock.patch.object(decide_mod.delivery, "send_text", fake_send), \
                 mock.patch.object(decide_mod.echo, "echo_all", fake_echo), \
                 mock.patch.object(decide_mod.flow_mod, "read_flow", fake_read_flow):
                trace = await engine.run("umo", "cron")

        self.assertEqual(sent, ["刚看到你追的那部更新了"])
        self.assertEqual(echoed, ["刚看到你追的那部更新了"])
        self.assertEqual(len(biliread.calls), 1)
        self.assertEqual(biliread.calls[0], {"keyword": "下饭"})
        self.assertTrue(trace["steps"][0]["ok"])
        self.assertEqual(len(memorize.calls), 1)
        self.assertIn("【主动行为", memorize.calls[0]["memory"])
        self.assertIn("bilibili_read（成功）", memorize.calls[0]["memory"])
        self.assertAlmostEqual(memorize.calls[0]["importance"], 0.7, places=3)
        self.assertTrue(trace["memory"]["ok"])
        self.assertIn("umo", scheduler.jobs)

    @staticmethod
    async def _provider_async(provider):
        return provider


if __name__ == "__main__":
    unittest.main()
