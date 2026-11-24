"""定时触发的配置、到期分发和发送限制；不访问模型或平台。"""

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_official_inbound_handoff import _make_plugin
from test_decision_engine import DecisionBooleanAnswer, message
from astrbot_plugin_quick_decision import web_api
from astrbot_plugin_quick_decision.config_util import PluginSettings, load_questions
from astrbot_plugin_quick_decision.decision_engine import ask_decision, evaluate_post_reply, preview_workflow
from astrbot_plugin_quick_decision.models import DecisionResult, SessionProfile
from astrbot_plugin_quick_decision.reply_generator import generate_text


def timer_question(name="timer", **schedule):
    return dict(id=name, type="boolean", instructions="现在适合开口吗？当前时间：{当前时间}",
                true_weight=1, false_weight=0, decision_mode="random",
                triggers=[dict(event="timer_elapsed", **(schedule or {"interval_seconds": 60}))],
                actions={"true": [{"tool": "request_reply"}]})


class TimerTriggerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.plugin, self.main = _make_plugin(self.tmp.name)
        self.plugin.context.get_config = lambda umo: {"timezone": "UTC"}
        self.plugin._self_generate_and_send = AsyncMock()
        self.set_questions(timer_question())
        self.runtime = self.plugin.sessions.ensure(SessionProfile("group", False))
        self.runtime.last_user_ts = 1000

    def set_questions(self, *questions):
        self.plugin.config["decision_questions"] = {"version": 5, "questions": list(questions)}

    async def check(self, now):
        with patch("time.time", return_value=now):
            await self.plugin._check_timers()

    def test_reject_invalid_schedules_and_wrong_phase(self):
        invalid = ({}, {"interval_seconds": True}, {"interval_seconds": 14},
                   {"interval_seconds": 60.5}, {"interval_seconds": 2147483648},
                   {"daily_time": "24:00"}, {"daily_time": "8:00"}, {"daily_time": "08:60"},
                   {"daily_time": "08:00\n"}, {"daily_time": None},
                   {"daily_time": "08:00", "interval_seconds": 60}, {"cron": "* * * * *"})
        for fields in invalid:
            with self.subTest(fields=fields):
                q = timer_question()
                q["triggers"] = [dict(event="timer_elapsed", **fields)]
                self.assertIsNotNone(load_questions({"version": 5, "questions": [q]}, strict=True)[1])
        q = dict(timer_question(), stage="post_reply", actions={})
        self.assertIsNotNone(load_questions({"version": 5, "questions": [q]}, strict=True)[1])

    async def test_interval_batches_due_questions_without_resetting_on_messages(self):
        self.set_questions(timer_question(), timer_question("other", interval_seconds=120))
        await self.check(1000)
        await self.check(1059)
        self.assertIsNone(self.runtime.last_decision)
        self.runtime.last_user_ts = 1059
        await self.check(1060)
        self.assertEqual(self.runtime.last_decision.answers, {"timer": True})
        self.assertEqual(self.runtime.last_decision.trigger_event, "timer_elapsed")
        self.assertEqual(self.runtime.silence_evaluated_at_user_ts, 0)
        await self.check(1060)
        self.assertEqual(self.plugin._self_generate_and_send.await_count, 1)
        await self.check(1120)
        self.assertEqual(self.runtime.last_decision.answers, {"timer": True, "other": True})
        self.assertEqual(self.plugin._self_generate_and_send.await_count, 2)
        self.assertEqual(self.plugin.sessions.recent_logs()[0]["trigger_event"], "timer_elapsed")

    async def test_daily_time_uses_session_zone_and_runs_once_each_day(self):
        self.set_questions(timer_question(daily_time="08:00"))
        zone = timezone(timedelta(hours=8))
        before = datetime(2026, 11, 22, 7, 59, 50, tzinfo=zone).timestamp()
        with patch.object(self.main, "_round_clock", side_effect=lambda *_: (self.now, zone)):
            for self.now in (before, before + 9):
                await self.check(self.now)
            self.plugin._self_generate_and_send.assert_not_awaited()
            self.now = before + 10
            await self.check(self.now)
            await self.check(self.now)
            self.assertEqual(self.plugin._self_generate_and_send.await_count, 1)
            self.now += 86400
            await self.check(self.now)
        self.assertEqual(self.plugin._self_generate_and_send.await_count, 2)

    async def test_busy_session_delays_once_without_catching_up_missed_ticks(self):
        await self.check(1000)
        self.runtime.pending_pipeline = DecisionResult()
        await self.check(1200)
        self.assertIsNone(self.runtime.last_decision)
        self.runtime.pending_pipeline = None
        await self.check(1300)
        await self.check(1300)
        self.assertEqual(self.plugin._self_generate_and_send.await_count, 1)
        self.assertEqual(next(iter(self.runtime.timer_due.values())), 1360)

    async def test_waiting_follow_up_runs_before_due_timer(self):
        await self.check(1000)
        # 挂起的追问持有会话锁，等待期间到期的定时题不能抢先占用会话。
        async with self.runtime.lock:
            await self.check(1060)
        self.assertIsNone(self.runtime.last_decision)
        await self.check(1061)
        self.assertEqual(self.runtime.last_decision.answers, {"timer": True})

    async def test_scene_filter_config_change_disable_and_idle_cleanup(self):
        self.set_questions(dict(timer_question(), scenes=["group_proactive"]))
        private = self.plugin.sessions.ensure(SessionProfile("private", True))
        private.last_user_ts = 1000
        await self.check(1000)
        self.assertFalse(private.timer_due)
        with patch("time.time", return_value=100000):
            self.assertEqual(self.plugin.sessions.prune_idle(), 1)
        self.assertIsNotNone(self.plugin.sessions.get("group"))
        self.set_questions(timer_question(interval_seconds=120))
        await self.check(1060)
        self.assertEqual(next(iter(self.runtime.timer_due.values())), 1180)
        self.set_questions(dict(timer_question(), enabled=False))
        await self.check(1180)
        self.assertFalse(self.runtime.timer_due)
        self.assertIsNone(self.runtime.last_decision)
        with patch("time.time", return_value=100000):
            self.assertEqual(self.plugin.sessions.prune_idle(), 1)

    async def test_dry_run_cooldown_quota_and_global_disable(self):
        for limit in ("dry_run", "cooldown", "quota", "none"):
            with self.subTest(limit=limit):
                self.runtime.timer_due.clear()
                self.runtime.proactive_times.clear()
                self.runtime.last_bot_ts = 0
                self.plugin.config.update(dry_run=limit == "dry_run", cooldown_seconds=300)
                self.plugin.config["group"].update(enable_proactive_after_silence=False, hourly_proactive_limit=3)
                self.plugin._self_generate_and_send.reset_mock()
                await self.check(1000)
                if limit == "cooldown":
                    self.runtime.last_bot_ts = 1050
                if limit == "quota":
                    self.runtime.proactive_times.extend([1001, 1002, 1003])
                await self.check(1060)
                self.assertEqual(self.plugin._self_generate_and_send.await_count, int(limit == "none"))
                self.assertIsNotNone(self.runtime.last_decision)
        self.plugin.config["enabled"] = False
        self.plugin._self_generate_and_send.reset_mock()
        await self.check(1120)
        self.plugin._self_generate_and_send.assert_not_awaited()

    async def test_session_failure_does_not_block_other_sessions_and_cancellation_releases_busy(self):
        other = self.plugin.sessions.ensure(SessionProfile("other", False))
        other.last_user_ts = 1000
        self.plugin._run_proactive = AsyncMock(side_effect=[RuntimeError("failed"), None])
        await self.check(1000)
        await self.check(1060)
        self.assertEqual(self.plugin._run_proactive.await_count, 2)
        self.assertFalse(self.runtime.deciding or other.deciding)
        self.plugin._run_proactive.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.check(1120)
        self.assertFalse(self.runtime.deciding)

    async def test_only_due_roots_and_their_successors_run_for_both_chat_types(self):
        graph = [timer_question(), timer_question("later", daily_time="08:00"),
                 dict(timer_question("incoming"), triggers=[{"event": "message_received"}]),
                 dict(timer_question("idle"), triggers=[{"event": "silence_elapsed"}]),
                 dict(timer_question("child"), decision_mode="model",
                      triggers=[{"question": "timer", "option": "true"}]),
                 dict(timer_question("post"), stage="post_reply", actions={},
                      triggers=[{"event": "reply_generated"}])]
        settings = PluginSettings({"decision_questions": {"version": 5, "questions": graph}})
        for private in (False, True):
            judge = AsyncMock(return_value={"child": DecisionBooleanAnswer(probability=1)})
            result = await ask_decision(SimpleNamespace(decision=SimpleNamespace(ask=judge)), settings=settings,
                                        scene="private_proactive" if private else "group_proactive",
                                        is_private=private, messages=[message("m1")], memes=[], at_or_quoted=False,
                                        trigger_event="timer_elapsed", timer_roots={"timer"})
            self.assertEqual(result.answers, {"timer": True, "child": True})
            self.assertEqual(result.reply_sources, ["timer=true", "child=true"])
            judge.assert_awaited_once()
            self.assertEqual(judge.await_args.kwargs["state"]["trigger_event"], "timer_elapsed")
            self.assertIsNotNone(result.post_plan)
            post = await evaluate_post_reply(None, result.post_plan, "你好", result.answers, decision=result)
            self.assertTrue(post["answers"]["post"])
        preview = preview_workflow(settings, "group_proactive", {}, trigger_event="timer_elapsed")
        self.assertEqual(set(preview["answers"]), {"timer", "later", "child", "post"})

    async def test_web_save_reload_and_timer_preview(self):
        class Config(dict):
            save_config = AsyncMock()
            save_config_async = save_config
        self.plugin.config = Config(self.plugin.config)
        graph = [timer_question(), timer_question("daily", daily_time="08:00")]
        payload = dict(version=7, decision_questions=graph)
        request = SimpleNamespace(method="POST", json=AsyncMock(return_value=payload))
        with patch.object(web_api, "request", request), patch.object(web_api, "json_response", side_effect=lambda value: value):
            saved = await self.plugin.web_decision_config()
            self.assertIn("timer_elapsed", saved["events"])
            self.assertEqual([q["triggers"] for q in saved["decision_questions"]], [q["triggers"] for q in graph])
            self.assertEqual(len(self.plugin.settings.decision_questions), 2)
            payload.update(scene="group_proactive", trigger_event="timer_elapsed")
            self.assertEqual(set((await self.plugin.web_decision_preview())["answers"]), {"timer", "daily"})
            payload["scene"] = "group_passive"
            with patch.object(web_api, "error_response") as error:
                await self.plugin.web_decision_preview()
                error.assert_called_once_with("触发事件与预览场景不匹配", status_code=400)

    async def test_generation_uses_timer_prompt_and_preserves_history(self):
        self.plugin.context.persona_manager = SimpleNamespace(
            get_persona=AsyncMock(return_value=SimpleNamespace(system_prompt="人格提示")))
        self.plugin.context.get_current_chat_provider_id = AsyncMock(return_value="provider")
        self.plugin.context.llm_generate = AsyncMock(return_value=SimpleNamespace(completion_text="早上好"))
        result = DecisionResult(action="proactive", scene="group_proactive", trigger_event="timer_elapsed")
        text = await generate_text(self.plugin.context, umo="group", decision=result, user_prompt="")
        self.assertEqual(text, "早上好")
        prompt = self.plugin.context.llm_generate.await_args.kwargs["prompt"]
        self.assertIn("定时", prompt)
        self.assertNotIn("沉默", prompt)
        self.assertEqual(self.plugin.context.conversation_manager.updated[0]["history"],
                         [{"role": "assistant", "content": "早上好"}])
