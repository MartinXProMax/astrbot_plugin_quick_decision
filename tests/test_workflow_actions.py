"""从空配置重建，校验实际的判断批次和选项动作。"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_decision_engine import DecisionBooleanAnswer, choice, message
from astrbot_plugin_quick_decision.config_util import PluginSettings, default_questions, load_questions
from astrbot_plugin_quick_decision.workflow_actions import migrate_actions
from astrbot_plugin_quick_decision.decision_engine import ask_decision
from astrbot_plugin_quick_decision.reply_generator import session_hint_text


def question(name, trigger, **fields):
    return dict(id=name, instructions=name, type="boolean", triggers=[trigger]) | fields


class WorkflowActionTests(unittest.IsolatedAsyncioTestCase):
    async def run_graph(self, questions, answers, scene="group_passive"):
        graph = {"version": 5, "questions": questions}
        loaded, error = load_questions(graph, strict=True)
        self.assertIsNone(error)
        settings = PluginSettings({"decision_questions": dict(graph, questions=[q.to_dict() for q in loaded])})
        judge = AsyncMock(return_value=answers)
        result = await ask_decision(SimpleNamespace(decision=SimpleNamespace(ask=judge)),
                                    settings=settings, scene=scene, is_private=False,
                                    messages=[message("m1")], memes=[], at_or_quoted=False)
        self.assertFalse(result.failed, result.error)
        return result, judge

    async def test_independent_event_custom_speech_and_speculative_target(self):
        event = {"event": "message_received"}
        independent = question("topic", event)
        speech = question("join_chat", event, actions={"true": [{"tool": "request_reply"}]})
        wants_target = question("specific", {"question": "join_chat", "option": "true", "ask_with_parent": True})
        target = question("which", {"question": "specific", "option": "true", "ask_with_parent": True},
                          type="choice", candidate_source="recent_messages",
                          options=[{"id": "*", "criteria": "{描述}"}, {"id": "skip", "criteria": "跳过"}],
                          actions={"*": [{"tool": "add_context"}, {"tool": "quote_message"}]})
        yes = DecisionBooleanAnswer(probability=1)
        for speaks, targeted in ((False, True), (True, False), (True, True)):
            result, judge = await self.run_graph([speech, independent, wants_target, target], {
                "join_chat": DecisionBooleanAnswer(probability=float(speaks)), "topic": yes,
                "specific": DecisionBooleanAnswer(probability=float(targeted)), "which": choice("m1", m1=1)})
            judge.assert_awaited_once()
            self.assertTrue(result.answers["topic"])
            self.assertEqual(result.should_speak(), speaks)
            self.assertEqual(result.target_message_id, "m1" if speaks and targeted else None)
            self.assertEqual("m1" in (session_hint_text(result) or ""), speaks and targeted)
            self.assertNotIn("topic", session_hint_text(result) or "")
        result, judge = await self.run_graph([independent], {"topic": yes})
        judge.assert_awaited_once()
        self.assertEqual(result.answers, {"topic": True})
        self.assertFalse(result.should_speak())

    async def test_cold_event_false_option_and_multiple_speech_requests(self):
        event = {"event": "silence_elapsed"}
        qs = [question(name, event, actions={"false": [{"tool": "request_reply"}]}) for name in ("one", "two")]
        result, judge = await self.run_graph(qs, dict.fromkeys(("one", "two"), DecisionBooleanAnswer(probability=0)),
                                             scene="group_proactive")
        judge.assert_awaited_once()
        self.assertEqual(result.action, "proactive")
        self.assertEqual(result.reply_sources, ["one=false", "two=false"])

    async def test_context_and_quote_are_separate_and_silent_option_can_trigger(self):
        incoming = {"event": "message_received"}
        speech = question("talk", incoming, actions={"false": [{"tool": "request_reply"}]})
        for tool in ("quote_message", "add_context"):
            pick = question("pick", {"question": "talk", "option": "false"}, type="choice",
                            candidate_source="recent_messages", options=[{"id": "*", "criteria": "{描述}"},
                                                                        {"id": "pass", "criteria": "略过"}],
                            actions={"*": [{"tool": tool}]})
            result, judge = await self.run_graph([speech, pick], {"talk": DecisionBooleanAnswer(probability=0),
                                                                   "pick": choice("m1", m1=1)})
            self.assertEqual(judge.await_count, 2)
            self.assertEqual("m1" in session_hint_text(result), tool == "add_context")
            self.assertEqual(result.target_message_id == "m1", tool == "quote_message")

    def test_action_validation_rejects_bad_source_and_wrong_phase(self):
        for action in ({"tool": "quote_message"}, {"tool": "send_meme", "from": "missing"},
                       {"tool": "request_reply", "when": "after_send"}, {"tool": "add_context", "from": []}):
            graph = [question("flag", {"event": "message_received"}, actions={"true": [action]})]
            self.assertTrue(load_questions({"version": 5, "questions": graph}, strict=True)[1])
        post = question("more", {"event": "reply_generated"}, stage="post_reply",
                        actions={"true": [{"tool": "request_reply"}]})
        self.assertTrue(load_questions({"version": 5, "questions": [post]}, strict=True)[1])

    def test_valid_v4_no_picture_binding_migrates_to_a_saveable_graph(self):
        legacy = default_questions()
        picker = next(q for q in legacy if q.role == "meme")
        no_picture = next(o for o in picker.options if o.id == "none")
        no_picture.tool, no_picture.session_hint = "send_meme", "只发文字"
        loaded, error = load_questions({"version": 4, "questions": [q.to_dict() for q in legacy]}, strict=True)
        self.assertIsNone(error)
        upgraded, warning = migrate_actions(loaded)
        self.assertIn(picker.id, warning)
        saved, error = load_questions({"version": 5, "questions": [q.to_dict() for q in upgraded]}, strict=True)
        self.assertIsNone(error)
        self.assertEqual(next(q.actions["none"] for q in saved if q.id == picker.id),
                         [{"tool": "add_context", "text": "只发文字"}])
