"""从完整问题配置到同一轮工具调用和日志。"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_v11_config import DecisionBooleanAnswer, DecisionChoiceAnswer  # isort: skip

from astrbot_plugin_quick_decision.config_util import (
    PluginSettings,
    default_decision_questions,
    default_questions,
    load_questions,
)
from astrbot_plugin_quick_decision.decision_engine import ask_decision
from astrbot_plugin_quick_decision.decision_tools import run_tools
from astrbot_plugin_quick_decision.session_store import SessionStore


def choice(key):
    return DecisionChoiceAnswer(
        choice=key, probability=1.0, probabilities={key: 1.0}, confidence=1.0
    )


class QuestionToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.questions = [q.to_dict() for q in default_questions(default_decision_questions())]
        self.gate, _, self.length, self.trigger, self.picture = self.questions
        self.gate.update(id="speak", ask_private=True, instructions="现在要回吗？")
        self.picture["id"] = "illustration"
        self.trigger["enabled"] = False
        self.length["options"][0]["tool"] = "send_meme"

    async def evaluate(self, answers, scene="private_passive"):
        service = AsyncMock(return_value=answers)
        result = await ask_decision(
            SimpleNamespace(decision=SimpleNamespace(ask=service)),
            settings=PluginSettings(
                {"decision_questions": {"version": 2, "questions": self.questions}}
            ),
            scene=scene,
            is_private=scene == "private_passive",
            messages=[],
            at_or_quoted=False,
            memes=[{"id": "cat", "description": "点头"}],
        )
        return result, service

    async def test_edited_questions_decide_and_call_once_after_text(self):
        self.trigger.update(enabled=True, true_tool="", false_tool="send_meme")
        result, ask = await self.evaluate(
            {
                "speak": choice("reply"),
                "illustration": choice("cat"),
                "length": choice("short"),
                "use_meme": DecisionBooleanAnswer(probability=0),
            }
        )
        asked = ask.call_args.args[0]
        self.assertEqual(asked["speak"].instructions, "现在要回吗？")
        self.assertEqual(asked["illustration"].criteria["cat"], "点头")
        call = result.tool_calls["send_meme"]
        self.assertEqual(call.sources, ["length=short", "use_meme=false"])
        self.assertEqual(call.args, {"meme_id": "cat"})
        self.assertTrue(result.session_hints)
        send = AsyncMock(return_value=True)
        with patch(
            "astrbot_plugin_quick_decision.decision_tools.meme_chain",
            return_value=SimpleNamespace(chain=["image"]),
        ):
            ctx = SimpleNamespace(send_message=send)
            await asyncio.gather(
                *(
                    run_tools(ctx, umo="room", resolver=None, decision=result)
                    for _ in range(2)
                )
            )
        send.assert_awaited_once()
        self.assertEqual(call.status, "executed")

    async def test_missing_candidate_dry_run_and_silence_never_send(self):
        for gate, image, dry in [
            ("reply", "outside", False),
            ("reply", "cat", True),
            ("none", "cat", False),
        ]:
            with self.subTest(gate=gate, image=image, dry=dry):
                result, _ = await self.evaluate(
                    {
                        "speak": choice(gate),
                        "illustration": choice(image),
                        "length": choice("short"),
                    }
                )
                result.dry_run = dry
                ctx = SimpleNamespace(send_message=AsyncMock())
                await run_tools(ctx, umo="room", resolver=None, decision=result)
                ctx.send_message.assert_not_awaited()
                self.assertEqual(result.tool_calls["send_meme"].status, "skipped")

    async def test_deleted_questions_stay_deleted_after_reload(self):
        self.questions.clear()
        for scene, expected in [
            ("private_passive", "reply"),
            ("group_passive", "none"),
            ("group_proactive", "none"),
        ]:
            result, ask = await self.evaluate({}, scene)
            self.assertEqual(result.action, expected)
            self.assertFalse(result.tool_calls)
            ask.assert_not_awaited()
        self.assertEqual(len(load_questions("[]")[0]), 4)

    def test_reject_unknown_tools_and_ambiguous_role_before_save(self):
        self.questions.append(dict(self.picture, id="other"))
        self.assertIn(
            "只能启用一个",
            load_questions({"version": 2, "questions": self.questions}, strict=True)[1],
        )
        self.questions.pop()
        self.length["options"][0]["tool"] = "some_module.handler"
        self.assertIn(
            "工具",
            load_questions({"version": 2, "questions": self.questions}, strict=True)[1],
        )

    def test_loading_skips_only_the_conflicting_role(self):
        duplicate = dict(self.picture, id="duplicate_picture")
        self.questions.insert(0, dict(duplicate, id="disabled_picture", enabled=False))
        self.questions.extend([duplicate, dict(self.trigger, id="tone", enabled=True)])
        saved = {"version": 2, "questions": self.questions}
        questions, warning = load_questions(saved)
        expected_ids = [q["id"] for q in self.questions if q is not duplicate]
        self.assertEqual([q.id for q in questions], expected_ids)
        self.assertIn("duplicate_picture", warning)
        self.assertIn("illustration", warning)
        self.assertEqual(load_questions(saved, strict=True)[0], [])

    def test_branch_config_roundtrip_and_invalid_graphs(self):
        self.length["branches"] = {"short": "use_meme"}
        self.trigger["branches"] = {"true": "illustration"}
        saved = {"version": 2, "questions": self.questions}
        questions, warning = load_questions(saved, strict=True)
        self.assertIsNone(warning)
        self.assertEqual([q.to_dict() for q in questions], self.questions)
        for branches, message in [
            ({"true": "missing"}, "不存在"),
            ({"other": "length"}, "选项不存在"),
            ({"false": "use_meme"}, "循环"),
            ({"false": "length"}, "循环"),
            (["length"], "对象"),
        ]:
            self.trigger["branches"] = branches
            for strict in (True, False):
                questions, warning = load_questions(saved, strict=strict)
                self.assertEqual(questions, [])
                self.assertIn(message, warning)

    def test_legacy_invalid_configs_keep_the_old_defaults(self):
        expected = [q.to_dict() for q in default_questions(default_decision_questions())]
        for old in ("{}", {}, [{"id": "action", "type": "boolean", "instructions": "旧问题"}]):
            with self.subTest(old=old):
                loaded, warning = load_questions(old)
                self.assertEqual([q.to_dict() for q in loaded], expected)
                self.assertIn("回退", warning)
                self.assertTrue(load_questions(old, strict=True)[1])
        self.assertEqual(load_questions('{"version": 2, "questions": []}'), ([], None))

    def test_post_question_stage_and_forward_links_are_validated(self):
        saved = {"version": 2, "questions": self.questions}
        self.gate["branches"] = {"reply": "length"}
        self.length["stage"] = "post_reply"
        questions, warning = load_questions(saved, strict=True)
        self.assertIsNone(warning)
        self.assertEqual([q.to_dict() for q in questions], self.questions)
        self.length["branches"] = {"short": "illustration"}
        self.assertIn("不能指向输出前", load_questions(saved, strict=True)[1])
        self.gate["stage"] = "post_reply"
        self.assertIn("只支持记录答案", load_questions(saved, strict=True)[1])
        self.gate["stage"] = "unknown"
        self.assertIn("阶段无效", load_questions(saved, strict=True)[1])

    def test_legacy_extra_fields_do_not_grant_new_roles_or_tools(self):
        old = dict(self.gate, id="tone")
        old["options"][1]["tool"] = "send_meme"
        questions, warning = load_questions([old])
        self.assertIsNone(warning)
        self.assertEqual([q.id for q in questions if q.role == "action"], ["action"])
        tone = next(q for q in questions if q.id == "tone")
        self.assertEqual(tone.role, "answer")
        self.assertFalse(tone.use_threshold)
        self.assertFalse(any(o.tool for o in tone.options))

    async def test_tool_receipt_updates_only_its_original_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp), log_limit=2)
            for key in ("first", "second"):
                await store.append_log(
                    {"round_id": key, "raw": {"p": 0.5}, "tool_calls": []}
                )
            await store.update_tool_log("first", [{"status": "failed"}])
            await store.update_tool_log("already-pruned", [])
            second, first = store.recent_logs()
            self.assertEqual(second["tool_calls"], [])
            self.assertEqual(first["raw"], {"p": 0.5})
            self.assertEqual(first["tool_calls"], [{"status": "failed"}])
