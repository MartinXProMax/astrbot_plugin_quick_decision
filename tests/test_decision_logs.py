"""日志内容、真实重放和提供商健康状态的隔离回归。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_official_inbound_handoff import FakeEvent, _make_plugin, _profile, _user

from astrbot.api.provider import DecisionBooleanAnswer, DecisionChoiceAnswer, DecisionProvider
from astrbot_plugin_quick_decision import web_api
from astrbot_plugin_quick_decision.decision_engine import ask_decision
from astrbot_plugin_quick_decision.models import DecisionResult


def graph(*, post=False):
    questions = [{
        "id": "speak", "type": "choice", "instructions": "现在是{当前时间}，接话吗？",
        "triggers": [{"event": "message_received"}],
        "options": [{"id": "yes", "criteria": "接话"}, {"id": "no", "criteria": "沉默"}],
        "actions": {"yes": [{"tool": "request_reply"}]},
    }]
    if post:
        questions.append({"id": "check", "type": "boolean", "instructions": "正文自然吗？",
                          "stage": "post_reply", "triggers": [{"event": "reply_generated"}]})
    return questions


class Query(dict):
    def get(self, key, default=None, type=None):
        value = super().get(key, default)
        return type(value) if type else value


class DecisionLogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.plugin, self.main = _make_plugin(self.tmp.name)

    async def asyncTearDown(self):
        await asyncio.gather(*self.plugin._post_tasks)
        self.tmp.cleanup()

    async def test_delivered_reply_and_group_name_are_logged(self):
        event = FakeEvent(text="为什么没接话", message_id="trigger")
        event.message_obj.group = SimpleNamespace(group_name="测试群")
        with patch.object(self.main, "ask_decision", AsyncMock(return_value=DecisionResult(action="reply"))):
            await self.plugin.on_chat_message(event)
        row = self.plugin.sessions.recent_logs()[0]
        self.assertEqual(row["trigger_text"], "为什么没接话")
        self.assertEqual(row["session_name"], "测试群")
        self.assertNotIn("reply_text", row)
        event.set_result(SimpleNamespace(chain=[self.main.Plain("接下来继续")]))
        event._has_send_oper = True
        await self.plugin.after_message_sent(event)
        self.assertEqual(self.plugin.sessions.recent_logs()[0]["reply_text"], "接下来继续")

    async def test_group_name_lookup_updates_logged_name(self):
        event = FakeEvent()
        runtime = self.plugin.sessions.ensure(_profile(event))
        event.get_group = AsyncMock(return_value=SimpleNamespace(group_name="查到的群名"))
        await self.plugin.sessions.append_log({"round_id": "named", "umo": runtime.profile.umo})
        await self.plugin._resolve_session_name(event, runtime)
        self.assertEqual(runtime.display_name, "查到的群名")
        self.assertEqual(self.plugin.sessions.find_log("named")["session_name"], "查到的群名")

    async def test_snapshot_uses_judged_messages_and_respects_content_switch(self):
        event = FakeEvent(text="长" * 700, message_id="judged")
        runtime = self.plugin.sessions.append_message(_profile(event), _user("judged", event.message_str, 1000), 20)
        self.plugin.config["decision_questions"] = {"version": 6, "questions": graph()}
        self.plugin.context.decision = SimpleNamespace(ask=AsyncMock(return_value={
            "speak": DecisionChoiceAnswer("no", 1, {"no": 1, "yes": 0}, 1),
        }))
        result = await ask_decision(self.plugin.context, settings=self.plugin.settings, scene="group_passive",
                                    is_private=False, messages=list(runtime.messages), at_or_quoted=False,
                                    memes=[], evaluation_time=1234)
        runtime.messages.append(_user("late", "判断完成后才来的消息", 1001))
        await self.plugin._log_decision(runtime.profile, result)
        row = self.plugin.sessions.find_log(result.round_id)
        self.assertEqual(row["trigger_text"], "长" * 120 + "…")
        self.assertEqual(len(row["replay_context"]["messages"][0]["text"]), 500)
        self.assertEqual(row["replay_context"]["evaluated_at"], 1234)
        self.assertNotIn("判断完成后", str(row))
        self.plugin.config["decision_log_include_content"] = False
        result.round_id = "disabled"
        await self.plugin._log_decision(runtime.profile, result)
        await self.plugin._update_log_reply(result, "不能记录的回复")
        row = self.plugin.sessions.find_log("disabled")
        self.assertNotIn("trigger_text", row)
        self.assertNotIn("replay_context", row)
        self.assertNotIn("reply_text", row)
        self.assertTrue(row["reply_sent"])

    async def test_partial_delivery_logs_only_successful_prefix(self):
        event = FakeEvent(text="追问", at_or_wake=True)
        event.send = AsyncMock(side_effect=[True, False])
        with patch.object(self.main, "ask_decision", AsyncMock(return_value=DecisionResult(action="reply"))):
            await self.plugin.on_chat_message(event)
        first, failed = self.main.Plain("送达的部分"), self.main.Plain("失败的部分")
        event.set_result(SimpleNamespace(chain=[first, failed], is_llm_result=lambda: True))
        await self.plugin.on_decorating_result(event)
        await event.send(SimpleNamespace(chain=[first]))
        await event.send(SimpleNamespace(chain=[failed]))
        await self.plugin.after_message_sent(event)
        row = self.plugin.sessions.recent_logs()[0]
        self.assertEqual(row["reply_text"], "送达的部分")
        self.assertFalse(row["reply_complete"])

    async def test_failed_delivery_does_not_log_generated_text(self):
        event = FakeEvent(text="追问", at_or_wake=True)
        event.send = AsyncMock(return_value=False)
        with patch.object(self.main, "ask_decision", AsyncMock(return_value=DecisionResult(action="reply"))):
            await self.plugin.on_chat_message(event)
        text = self.main.Plain("未送达的正文")
        event.set_result(SimpleNamespace(chain=[text], is_llm_result=lambda: True))
        await self.plugin.on_decorating_result(event)
        await event.send(SimpleNamespace(chain=[text]))
        await self.plugin.after_message_sent(event)
        row = self.plugin.sessions.recent_logs()[0]
        self.assertNotIn("reply_text", row)
        self.assertFalse(row["reply_sent"])

    async def test_replay_calls_real_engine_with_snapshot_and_never_sends(self):
        row = {"round_id": "replay", "scene": "group_passive", "umo": "room", "action": "none",
               "reply_text": "保存的回复前缀", "trigger_event": "message_received",
               "replay_context": {"messages": [asdict(_user("before", "前文", 1200)),
                                                 asdict(_user("trigger", "追问", 1230))],
                                  "evaluated_at": 1234, "at_or_quoted": True}}
        await self.plugin.sessions.append_log(row)
        payload = {"version": 8, "round_id": "replay", "decision_mode": "model",
                   "decision_custom_context": "草稿背景", "decision_questions": graph(post=True)}
        service = AsyncMock(side_effect=[{
            "speak": DecisionChoiceAnswer("yes", 1, {"yes": 1, "no": 0}, 1),
        }, {"check": DecisionBooleanAnswer(0.9)}])
        self.plugin.context.decision = SimpleNamespace(ask=service)
        self.plugin.context.send_message = AsyncMock()
        old_config = dict(self.plugin.config)
        with (patch.object(web_api, "request", SimpleNamespace(json=AsyncMock(return_value=payload))),
              patch.object(web_api, "json_response", side_effect=lambda value: value)):
            report = await self.plugin.web_decision_replay()
        self.assertEqual(report["new"]["action"], "reply")
        self.assertEqual(service.await_count, 2)
        self.assertEqual(service.await_args_list[0].kwargs["state"]["recent_messages"][0]["text"], "前文")
        self.assertEqual(service.await_args.kwargs["state"]["assistant_reply"], "保存的回复前缀")
        self.assertEqual(self.plugin.sessions.find_log("replay"), row)
        self.assertEqual(dict(self.plugin.config), old_config)
        self.plugin.context.send_message.assert_not_awaited()

    async def test_replay_rejects_old_logs_and_missing_ids(self):
        await self.plugin.sessions.append_log({"round_id": "old", "trigger_text": "旧摘要"})
        for round_id, code in (("missing", 404), ("old", 400)):
            with (self.subTest(round_id=round_id),
                  patch.object(web_api, "request", SimpleNamespace(json=AsyncMock(return_value={"round_id": round_id}))),
                  patch.object(web_api, "error_response", side_effect=lambda text, status_code: status_code)):
                self.assertEqual(await self.plugin.web_decision_replay(), code)

    async def test_skipped_timer_replay_preserves_only_the_due_questions(self):
        questions = [{
            "id": name, "type": "boolean", "instructions": "应该开口吗？",
            "triggers": [{"event": "timer_elapsed", "interval_seconds": interval}],
            "true_weight": 1, "false_weight": 0,
            "actions": {"true": [{"tool": "request_reply"}]},
        } for name, interval in (("due", 60), ("not_due", 120))]
        self.plugin.config.update(cooldown_seconds=300, decision_mode="random",
                                  decision_questions={"version": 6, "questions": questions})
        runtime = self.plugin.sessions.ensure(_profile(FakeEvent()))
        runtime.last_bot_ts = 10**12
        await self.plugin._run_proactive(runtime, timer_roots={"due"})
        row = self.plugin.sessions.recent_logs()[0]
        self.assertEqual(row["replay_context"]["timer_roots"], ["due"])
        payload = {"version": 8, "round_id": row["round_id"], "decision_mode": "random",
                   "decision_questions": questions}
        with (patch.object(web_api, "request", SimpleNamespace(json=AsyncMock(return_value=payload))),
              patch.object(web_api, "json_response", side_effect=lambda value: value)):
            report = await self.plugin.web_decision_replay()
        self.assertEqual(report["new"]["answers"], {"due": True})

    async def test_session_filter_finds_sessions_outside_visible_limit(self):
        for room in ("earlier", "latest"):
            await self.plugin.sessions.append_log({"round_id": room, "umo": room, "session_name": room})
        with (patch.object(web_api, "request", SimpleNamespace(query=Query(limit=1, umo="earlier"))),
              patch.object(web_api, "json_response", side_effect=lambda value: value)):
            data = await self.plugin.web_list_logs()
        self.assertEqual(data["logs"][0]["umo"], "earlier")
        self.assertEqual(len(data["sessions"]), 2)

    async def test_provider_status_checks_default_missing_and_wrong_type(self):
        provider = DecisionProvider()
        provider.meta = lambda: SimpleNamespace(id="judge")
        manager = SimpleNamespace(get_using_provider_async=AsyncMock(return_value=provider),
                                  get_provider_by_id=AsyncMock(return_value=object()))
        self.plugin.context.decision = SimpleNamespace(provider_manager=manager)
        self.assertEqual((await self.plugin._decision_provider_status())["state"], "configured")
        manager.get_using_provider_async.return_value = None
        self.assertEqual((await self.plugin._decision_provider_status())["state"], "missing")
        self.plugin.config["decision_provider_id"] = "chat-instead-of-judge"
        self.assertEqual((await self.plugin._decision_provider_status())["state"], "missing")

    async def test_provider_probe_rejects_missing_answers_and_reports_failures(self):
        for value in ({}, RuntimeError("未配置判断提供商")):
            service = AsyncMock(side_effect=value) if isinstance(value, Exception) else AsyncMock(return_value=value)
            self.plugin.context.decision = SimpleNamespace(ask=service)
            with patch.object(web_api, "json_response", side_effect=lambda value: value):
                report = await self.plugin.web_test_decision_provider()
            self.assertFalse(report["ok"])
            self.assertTrue(report["error"])

    async def test_provider_probe_passes_configured_id_and_accepts_answer(self):
        self.plugin.config["decision_provider_id"] = "judge"
        service = AsyncMock(return_value={"health": DecisionBooleanAnswer(0.9)})
        self.plugin.context.decision = SimpleNamespace(ask=service)
        with patch.object(web_api, "json_response", side_effect=lambda value: value):
            report = await self.plugin.web_test_decision_provider()
        self.assertTrue(report["ok"])
        self.assertEqual(service.await_args.kwargs["provider_id"], "judge")

    async def test_health_rate_counts_judgement_failures_without_cooldown_skips(self):
        self.plugin.meme_adapter.status = lambda: SimpleNamespace(to_dict=lambda: {})
        self.plugin.sessions.log_limit = 200
        for i in range(6):
            await self.plugin.sessions.append_log({"round_id": str(i), "error": "错误" if i < 3 else None,
                                                  "batches": [{"asked": ["speak"]}]})
        for _ in range(60):
            await self.plugin.sessions.append_log({"raw": {"skip_reason": "冷却"}})
        with patch.object(web_api, "json_response", side_effect=lambda value: value):
            report = await self.plugin.web_status()
        self.assertEqual(report["recent_log_count"], 6)
        self.assertEqual(report["recent_failure_rate"], 0.5)
