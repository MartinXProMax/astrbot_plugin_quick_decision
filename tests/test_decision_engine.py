"""统一评估的公开入口、候选快照及旧行为兼容测试。"""

from __future__ import annotations

import asyncio
import random
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# 共享接口替身必须先于 AstrBot 导入。
from test_v11_config import DecisionBooleanAnswer, DecisionChoiceAnswer  # isort: skip

from astrbot.api.provider import DecisionError
from astrbot_plugin_quick_decision.config_util import (
    PluginSettings,
    default_decision_questions,
    default_questions,
    dump_decision_questions,
)
from astrbot_plugin_quick_decision.decision_engine import (
    _parse_answers,
    ask_decision,
    evaluate_specs,
)
from astrbot_plugin_quick_decision.models import (
    BufferedMessage,
    DecisionQuestion,
    DecisionResult,
)


def choice(selected: str, **probabilities: float) -> DecisionChoiceAnswer:
    return DecisionChoiceAnswer(
        choice=selected,
        probability=probabilities[selected],
        probabilities=probabilities,
        confidence=0.99,
    )


def message(mid: str) -> BufferedMessage:
    return BufferedMessage(
        message_id=mid,
        sender_id="u",
        sender_name="用户",
        role="user",
        text=mid,
    )


class UnifiedDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def ask(
        self,
        service,
        *,
        scene="group_passive",
        settings=None,
        messages=None,
        memes=None,
    ):
        return await ask_decision(
            SimpleNamespace(decision=service),
            settings=settings or PluginSettings({}),
            scene=scene,
            is_private=scene.startswith("private"),
            at_or_quoted=True,
            messages=messages if messages is not None else [message("m1")],
            memes=memes
            if memes is not None
            else [{"id": "cat", "description": "微笑"}],
        )

    async def test_random_mode_uses_weights_without_provider_temperature_or_threshold(self):
        questions = default_questions()
        action, target, length, use_meme, meme = questions
        next(o for o in action.options if o.id == "none").weight = 0
        length.options[0].weight, length.options[1].weight = 3, 1
        use_meme.true_weight, use_meme.false_weight = 1, 0
        target.options[0].session_hint = "只在回复目标题实际作答时注入"
        meme.options[0].session_hint = "只在表情候选题实际作答时注入"

        saved = {"version": 2, "questions": [q.to_dict() for q in questions]}
        for temperature in (0, 2):
            with self.subTest(temperature=temperature):
                # Draw above the 75% boundary so the less likely option wins.
                with patch("random._inst.random", return_value=0.9):
                    result = await ask_decision(
                        SimpleNamespace(),
                        settings=PluginSettings({
                            "decision_mode": "random",
                            "choice_temperature": temperature,
                            "choice_threshold": 1.0,
                            "decision_questions": saved,
                        }),
                        scene="group_passive",
                        is_private=False,
                        messages=[message("")],
                        at_or_quoted=False,
                        memes=[],
                    )
                self.assertFalse(result.failed)
                self.assertEqual(result.decision_mode, "random")
                self.assertEqual(result.action, "reply")
                self.assertEqual(
                    result.raw["action"]["selection_probabilities"], {"none": 0, "reply": 1}
                )
                length_log = result.raw["length"]
                self.assertAlmostEqual(length_log["selection_probabilities"]["short"], 0.75)
                self.assertEqual(length_log["probabilities"], {"short": 0.5, "long": 0.5})
                self.assertNotIn("selection_temperature", length_log)
                self.assertEqual(result.length, "long")
                self.assertIs(result.answers["use_meme"], True)
                self.assertEqual(result.tool_calls["send_meme"].status, "skipped")
                for question in (target, meme):
                    self.assertEqual(result.outcomes[question.id].reason, "not_asked")
                    self.assertNotIn(question.options[0].session_hint, result.session_hints)

    async def test_random_mode_preserves_fixed_private_reply_and_dynamic_candidate_weights(self):
        questions = default_questions()
        action, target, _, use_meme, meme = questions
        next(o for o in action.options if o.id == "reply").weight = 0
        target.options[0].weight = 0
        use_meme.false_weight = 0
        meme.options[0].weight, meme.options[1].weight = 0, 3
        # An old alias should not become another random candidate.
        settings = PluginSettings({
            "decision_mode": "random",
            "decision_questions": {"version": 2, "questions": [q.to_dict() for q in questions]},
        })
        service = SimpleNamespace(ask=AsyncMock())
        for scene in ("group_passive", "private_passive"):
            with self.subTest(scene=scene):
                result = await self.ask(
                    service,
                    settings=settings,
                    scene=scene,
                    memes=[{"id": "cat", "description": "微笑"}, {"id": "dog", "description": "点头"}],
                )
                self.assertFalse(result.failed)
                self.assertEqual(
                    result.raw["meme_id"]["selection_probabilities"],
                    {"none": 0, "cat": 0.5, "dog": 0.5},
                )
                if scene == "group_passive":
                    self.assertEqual(result.action, "none")
                    self.assertEqual(result.target_message_id, "m1")
                    self.assertNotIn("join", result.raw["action"]["probabilities"])
                else:
                    self.assertEqual(result.action, "reply")
                    self.assertIsNone(result.target_message_id)
                    self.assertEqual(result.raw["action"]["note"], "private_passive_default_reply")
                    self.assertIn(result.tool_calls["send_meme"].args["meme_id"], {"cat", "dog"})
        service.ask.assert_not_awaited()

    async def test_one_batch_preserves_question_and_sampling_order(self) -> None:
        configured = default_decision_questions() + [
            DecisionQuestion(
                id="followup",
                type="boolean",
                instructions="追问？",
                true_session_hint="继续追问",
                false_session_hint="无需追问",
            )
        ]
        service = SimpleNamespace(
            ask=AsyncMock(
                return_value={
                    "action": choice("reply", none=0.2, reply=0.8),
                    "target_message_id": choice("m1", latest=0.2, m1=0.8),
                    "length": choice("short", short=0.7, long=0.3),
                    "followup": DecisionBooleanAnswer(probability=0.8),
                    "use_meme": DecisionBooleanAnswer(probability=0.8),
                    "meme_id": choice("cat", none=0.2, cat=0.8),
                }
            )
        )
        with patch(
            "astrbot_plugin_quick_decision.decision_engine.random.choices",
            side_effect=[["reply"], ["long"], ["false"], ["cat"], ["m1"]],
        ) as sample:
            result = await self.ask(
                service,
                settings=PluginSettings(
                    {
                        "choice_temperature": 1.0,
                        "decision_provider_id": "system-one",
                        "decision_questions": dump_decision_questions(configured),
                    }
                ),
            )
        service.ask.assert_awaited_once()
        self.assertEqual(
            list(service.ask.call_args.args[0]),
            [
                "action",
                "target_message_id",
                "length",
                "followup",
                "use_meme",
                "meme_id",
            ],
        )
        self.assertEqual(service.ask.call_args.kwargs["provider_id"], "system-one")
        self.assertEqual(
            [list(c.args[0]) for c in sample.call_args_list],
            [
                ["none", "reply"],
                ["short", "long"],
                ["true", "false"],
                ["none", "cat"],
                ["latest", "m1"],
            ],
        )
        self.assertEqual(
            (result.action, result.target_message_id, result.length),
            ("reply", "m1", "long"),
        )
        self.assertEqual(result.custom_answers, {"length": "long", "followup": False, "use_meme": True})
        self.assertEqual(result.session_hints[-1], "无需追问")
        self.assertTrue(result.use_meme)
        self.assertEqual(result.meme_id, "cat")
        self.assertEqual(
            result.outcomes["length"].probabilities, {"short": 0.7, "long": 0.3}
        )
        for key, probability in {"short": 0.7, "long": 0.3}.items():
            self.assertAlmostEqual(
                result.outcomes["length"].selection_probabilities[key], probability
            )
        self.assertEqual(result.outcomes["use_meme"].selection_probabilities, {})

    async def test_snapshot_survives_changes_while_provider_is_running(self) -> None:
        configured = default_decision_questions()
        configured[0].options[1].weight = 3.0
        settings = PluginSettings(
            {"decision_questions": dump_decision_questions(configured)}
        )
        messages = [message("m1")]
        memes = [{"id": "cat", "description": "微笑"}]

        async def respond(questions, **_kwargs):
            self.assertEqual(
                set(questions["target_message_id"].criteria), {"latest", "m1"}
            )
            settings.choice_threshold = 1.0
            settings.choice_temperature = 1.0
            long_option = next(q for q in settings.decision_questions if q.id == "length").options[1]
            long_option.weight = 0.0
            long_option.session_hint = "updated during request"
            messages[:] = [message("m2")]
            memes[:] = [{"id": "dog", "description": "开心"}]
            return {
                "action": choice("reply", none=0.2, reply=0.8),
                "target_message_id": choice("m1", latest=0.2, m1=0.8),
                "length": choice("short", short=0.7, long=0.3),
                "use_meme": DecisionBooleanAnswer(probability=0.8),
                "meme_id": choice("cat", none=0.2, cat=0.8),
            }

        with patch(
            "astrbot_plugin_quick_decision.decision_engine.random.choices"
        ) as sample:
            result = await self.ask(
                SimpleNamespace(ask=respond),
                settings=settings,
                messages=messages,
                memes=memes,
            )
        sample.assert_not_called()
        self.assertEqual(
            (result.action, result.target_message_id, result.meme_id),
            ("reply", "m1", "cat"),
        )
        self.assertEqual(result.length, "long")
        self.assertEqual(result.session_hints, [configured[0].options[1].session_hint])

    async def test_unknown_target_is_rejected_against_asked_candidates(self) -> None:
        service = SimpleNamespace(
            ask=AsyncMock(
                return_value={
                    "action": choice("reply", none=0.2, reply=0.8),
                    "target_message_id": choice("outside", latest=0.1, outside=0.9),
                }
            )
        )
        result = await self.ask(service)
        self.assertIsNone(result.target_message_id)
        self.assertEqual(result.outcomes["target_message_id"].reason, "unknown_option")

    async def test_empty_batch_skips_provider(self) -> None:
        service = SimpleNamespace(ask=AsyncMock(side_effect=AssertionError("不应请求")))
        self.assertEqual(await evaluate_specs(service, [], state={}), {})
        service.ask.assert_not_awaited()

    async def test_meme_workflow_only_evaluates_the_selected_path(self):
        questions = default_questions()
        action, target, length, gate, meme = questions
        action.branches = {"reply": gate.id}
        gate.branches["false"] = target.id
        meme.branches = {"*": length.id}
        # 子题可以排在父题前；即使提供商每次返回所有答案，也只接受已提问项。
        settings = PluginSettings({"decision_questions": {
            "version": 2, "questions": [q.to_dict() for q in reversed(questions)],
        }})
        for use_meme in (True, False):
            service = SimpleNamespace(ask=AsyncMock(return_value={
                "action": choice("join", join=1.0),
                "use_meme": DecisionBooleanAnswer(probability=float(use_meme)),
                "meme_id": choice("cat", cat=1.0),
                "length": choice("short", short=1.0),
                "target_message_id": choice("m1", m1=1.0),
            }))
            result = await self.ask(service, settings=settings)
            calls = service.ask.await_args_list
            path = ["action", "use_meme", "meme_id", "length"] if use_meme else [
                "action", "use_meme", "target_message_id",
            ]
            self.assertEqual([list(c.args[0]) for c in calls], [[key] for key in path])
            self.assertEqual(set(result.answers), set(path))
            self.assertEqual(set(calls[-1].kwargs["state"]["previous_answers"]), set(path[:-1]))
            self.assertEqual(result.use_meme, use_meme)
            self.assertEqual(result.meme_id, "cat" if use_meme else None)
            self.assertEqual(bool(result.tool_calls), use_meme)
            self.assertEqual(bool(result.session_hints), use_meme)
            self.assertEqual(result.target_message_id, None if use_meme else "m1")

    async def test_unanswered_and_disabled_parents_do_not_activate_children(self):
        settings = PluginSettings({})
        action, _, length, gate, meme = settings.decision_questions
        action.branches = {"none": length.id, "reply": gate.id}
        action.use_threshold = True
        for answer in ({}, {"action": object()}, {"action": choice("none", none=0.4)}):
            service = SimpleNamespace(ask=AsyncMock(return_value=answer))
            result = await self.ask(service, settings=settings)
            service.ask.assert_awaited_once()
            for key in (length.id, gate.id, meme.id):
                self.assertEqual(result.outcomes[key].reason, "branch_not_selected")
        action.enabled = False
        settings.decision_mode = "random"
        result = await self.ask(None, settings=settings)
        self.assertEqual(set(result.answers), {"target_message_id"})
        self.assertFalse(result.tool_calls)

    async def test_random_workflow_deduplicates_shared_child_and_follows_fixed_reply(self):
        settings = PluginSettings({"decision_mode": "random"})
        action, _, length, gate, meme = settings.decision_questions
        action.branches = {"reply": meme.id}
        gate.false_weight = 0
        meme.options[0].weight = 0
        meme.branches = {"*": length.id}
        length.options[0].weight = 0
        with patch("random.choices", wraps=random.choices) as draw:
            result = await self.ask(None, settings=settings, scene="private_passive")
        self.assertEqual(draw.call_count, 3)
        self.assertEqual((result.meme_id, result.length), ("cat", "long"))
        gate.enabled = False
        result = await self.ask(None, settings=settings, scene="private_passive")
        self.assertEqual(result.answers[meme.id], "cat")
        self.assertNotIn("send_meme", result.tool_calls)

    async def test_failed_successor_discards_planned_effects(self):
        service = SimpleNamespace(ask=AsyncMock(side_effect=[
            {"action": choice("reply", reply=1), "use_meme": DecisionBooleanAnswer(probability=1)},
            DecisionError("timeout"),
        ]))
        result = await self.ask(service)
        self.assertTrue(result.failed)
        self.assertFalse(result.should_speak())
        self.assertFalse(result.tool_calls)
        self.assertEqual(result.outcomes["meme_id"].reason, "batch_error")

    async def test_extra_answers_to_unasked_questions_are_ignored(self) -> None:
        service = SimpleNamespace(
            ask=AsyncMock(
                return_value={
                    "action": choice("none", none=1.0),
                    "target_message_id": choice("latest", latest=1.0),
                    "meme_id": choice("none", none=1.0),
                }
            )
        )
        result = await self.ask(service, scene="private_passive", memes=[])
        self.assertEqual(set(service.ask.call_args.args[0]), {"length", "use_meme"})
        self.assertEqual(result.action, "reply")
        self.assertEqual(
            result.outcomes["action"].reason, "private_passive_default_reply"
        )
        for key in ("target_message_id", "meme_id"):
            reason = "branch_not_selected" if key == "meme_id" else "not_asked"
            self.assertEqual(
                (result.outcomes[key].status, result.outcomes[key].reason),
                ("skipped", reason),
            )
            self.assertNotIn(key, result.raw)

    async def test_batch_failure_keeps_legacy_scene_fallback(self) -> None:
        for scene in (
            "group_passive",
            "private_passive",
            "group_proactive",
            "private_proactive",
        ):
            for error in (DecisionError("timeout"), DecisionError(), TimeoutError()):
                with self.subTest(scene=scene, error=type(error).__name__):
                    service = SimpleNamespace(ask=AsyncMock(side_effect=error))
                    result = await self.ask(
                        service,
                        scene=scene,
                        settings=PluginSettings({"dry_run": True}),
                        memes=[],
                    )
                    service.ask.assert_awaited_once()
                    self.assertEqual(
                        result.action, "reply" if scene == "private_passive" else "none"
                    )
                    self.assertEqual(result.error, str(error) or type(error).__name__)
                    self.assertTrue(result.degraded)
                    self.assertTrue(result.dry_run)
                    self.assertFalse(result.should_speak())
                    self.assertEqual(
                        (result.raw, result.custom_answers, result.session_hints),
                        ({}, {}, []),
                    )
                    asked = set(service.ask.call_args.args[0])
                    self.assertEqual(
                        {
                            k
                            for k, o in result.outcomes.items()
                            if (o.status, o.reason) == ("fallback", "batch_error")
                        },
                        asked,
                    )
                    for key in result.outcomes.keys() - asked:
                        reason = {
                            "action": "private_passive_default_reply",
                            "meme_id": "branch_not_selected",
                        }.get(key, "not_asked")
                        self.assertEqual(
                            (result.outcomes[key].status, result.outcomes[key].reason),
                            ("skipped", reason),
                        )

    async def test_concurrent_rounds_keep_their_candidates_and_hints(self) -> None:
        entered = 0
        both_entered = asyncio.Event()

        async def respond(questions, **_kwargs):
            nonlocal entered
            entered += 1
            if entered == 2:
                both_entered.set()
            await both_entered.wait()
            mid = next(
                key
                for key in questions["target_message_id"].criteria
                if key != "latest"
            )
            return {
                "action": choice("reply", none=0.2, reply=0.8),
                "target_message_id": choice(mid, **{"latest": 0.2, mid: 0.8}),
                "length": choice("short", short=0.8, long=0.2),
            }

        async def run(mid):
            configured = default_decision_questions()
            configured[0].options[0].session_hint = mid
            return await self.ask(
                SimpleNamespace(ask=respond),
                messages=[message(mid)],
                settings=PluginSettings(
                    {
                        "decision_questions": dump_decision_questions(configured),
                    }
                ),
            )

        first, second = await asyncio.wait_for(
            asyncio.gather(run("m1"), run("m2")), timeout=2
        )
        self.assertEqual((first.target_message_id, first.session_hints), ("m1", ["m1"]))
        self.assertEqual(
            (second.target_message_id, second.session_hints), ("m2", ["m2"])
        )
        self.assertIsNot(first.outcomes, second.outcomes)


class OutcomeTests(unittest.TestCase):
    def test_degraded_result_cannot_speak_even_without_an_error_message(self) -> None:
        self.assertFalse(DecisionResult(action="reply", degraded=True).should_speak())

    def test_statuses_and_legacy_raw_for_fixed_missing_invalid_and_threshold(
        self,
    ) -> None:
        private = _parse_answers(
            {}, scene="private_passive", settings=PluginSettings({}), memes=[]
        )
        self.assertEqual(private.outcomes["action"].status, "skipped")
        self.assertEqual(private.outcomes["length"].status, "missing")
        self.assertEqual(private.outcomes["meme_id"].reason, "branch_not_selected")
        self.assertEqual(
            private.raw["action"],
            {"choice": "reply", "note": "private_passive_default_reply"},
        )
        result = _parse_answers(
            {
                "action": choice("reply", none=0.4, reply=0.6),
                "length": object(),
                "use_meme": DecisionBooleanAnswer(probability=0.7),
                "meme_id": choice("cat", none=0.1, cat=0.9),
            },
            scene="group_passive",
            settings=PluginSettings({"choice_threshold": 0.8}),
            memes=[{"id": "cat", "description": "微笑"}],
        )
        self.assertEqual(
            (result.outcomes["action"].status, result.outcomes["action"].reason),
            ("fallback", "below_threshold"),
        )
        self.assertEqual(result.outcomes["length"].status, "invalid")
        self.assertEqual(result.outcomes["use_meme"].value, False)
        self.assertEqual(result.outcomes["meme_id"].reason, "branch_not_selected")
        self.assertFalse(result.use_meme)
        self.assertIsNone(result.meme_id)
        self.assertNotIn("length", result.raw)

    def test_boolean_thresholds_and_choice_provider_selection_stay_distinct(
        self,
    ) -> None:
        configured = default_decision_questions() + [
            DecisionQuestion(
                id="followup",
                type="boolean",
                instructions="追问？",
                true_session_hint="追问",
            )
        ]
        result = _parse_answers(
            {
                # 零温度、默认权重继续保留提供商原选，即使替身给出的分布不一致。
                "length": choice("long", short=0.8, long=0.2),
                "followup": DecisionBooleanAnswer(probability=0.5),
                "use_meme": DecisionBooleanAnswer(probability=0.5),
                "meme_id": choice("cat", none=0.1, cat=0.9),
            },
            scene="private_passive",
            settings=PluginSettings(
                {
                    "choice_threshold": 0.8,
                    "decision_questions": dump_decision_questions(configured),
                }
            ),
            memes=[{"id": "cat", "description": "微笑"}],
        )
        self.assertEqual(result.length, "long")
        self.assertTrue(result.custom_answers["followup"])
        self.assertFalse(result.use_meme)


if __name__ == "__main__":
    unittest.main()
