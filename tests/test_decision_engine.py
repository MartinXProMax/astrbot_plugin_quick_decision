"""统一评估的公开入口、候选快照及旧行为兼容测试。"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# 共享接口替身必须先于 AstrBot 导入。
from test_v11_config import DecisionBooleanAnswer, DecisionChoiceAnswer  # isort: skip

from astrbot.api.provider import DecisionError
from astrbot_plugin_quick_decision.config_util import (
    PluginSettings,
    default_decision_questions,
    dump_decision_questions,
)
from astrbot_plugin_quick_decision.decision_engine import (
    _parse_answers,
    ask_decision,
    evaluate_specs,
)
from astrbot_plugin_quick_decision.models import (
    BufferedMessage,
    CustomDecisionQuestion,
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

    async def test_one_batch_preserves_question_and_sampling_order(self) -> None:
        configured = default_decision_questions() + [
            CustomDecisionQuestion(
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
        self.assertEqual(result.custom_answers, {"length": "long", "followup": False})
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
            long_option = settings.decision_questions[0].options[1]
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
            self.assertEqual(
                (result.outcomes[key].status, result.outcomes[key].reason),
                ("skipped", "not_asked"),
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
                        reason = (
                            "private_passive_default_reply"
                            if key == "action"
                            else "not_asked"
                        )
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
        self.assertEqual(private.outcomes["meme_id"].reason, "not_asked")
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
        self.assertEqual(result.outcomes["meme_id"].value, "cat")
        self.assertFalse(result.use_meme)
        self.assertIsNone(result.meme_id)
        self.assertNotIn("length", result.raw)

    def test_boolean_thresholds_and_choice_provider_selection_stay_distinct(
        self,
    ) -> None:
        configured = default_decision_questions() + [
            CustomDecisionQuestion(
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
