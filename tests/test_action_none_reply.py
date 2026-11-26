"""群聊 action 改为 none/reply 二选一的回归测试。不访问网络，不连接 QQ。"""

# ruff: noqa: E402, I001

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_v11_config import _install_astrbot_stubs

_install_astrbot_stubs()


class MessageChain:
    def __init__(self) -> None:
        self.chain: list[object] = []

    def message(self, message: str):
        self.chain.append(type("Plain", (), {"text": message})())
        return self


class Reply:
    def __init__(self, id: str = "") -> None:
        self.id = id


sys.modules["astrbot.api.event"].MessageChain = MessageChain
sys.modules["astrbot.api.message_components"].Reply = Reply

import importlib
import astrbot_plugin_quick_decision.reply_generator as reply_generator

importlib.reload(reply_generator)

from astrbot.api.provider import DecisionChoiceAnswer
from astrbot_plugin_quick_decision.config_util import PluginSettings
from astrbot_plugin_quick_decision.config_util import ACTION_CRITERIA_GROUP, ACTION_CRITERIA_PROACTIVE
from engine_helpers import _build_questions, _parse_answers
from astrbot_plugin_quick_decision.models import (
    BufferedMessage,
    DecisionResult,
    normalize_action,
)
from astrbot_plugin_quick_decision.reply_generator import build_send_chain


class _DummyStore:
    def resolve_image(self, meme_id):  # noqa: ANN001, ARG002
        return None


def _action(
    choice: str, probability: float, **others: float
) -> DecisionChoiceAnswer:
    """构造群聊动作答案；允许概率和不加总为 1，以便验证不会把 reply+join 相加。"""
    probabilities = {"none": 0.0, "reply": 0.0, "join": 0.0}
    probabilities.update(others)
    probabilities[choice] = probability
    return DecisionChoiceAnswer(
        choice=choice,
        probability=probability,
        probabilities=probabilities,
        confidence=probability,
    )


def _settings(threshold: float = 0.7) -> PluginSettings:
    return PluginSettings({"choice_threshold": threshold, "decision_questions": "[]"})


def _sample_messages() -> list[BufferedMessage]:
    return [
        BufferedMessage(
            message_id="m1",
            sender_id="u1",
            sender_name="用户",
            role="user",
            text="在吗",
        ),
        BufferedMessage(
            message_id="m2",
            sender_id="u2",
            sender_name="另一人",
            role="user",
            text="继续聊",
        ),
    ]


class ActionNoneReplyTests(unittest.TestCase):
    def test_group_questions_are_none_or_reply_only(self) -> None:
        questions = _build_questions("group_passive", _sample_messages(), [])
        action = questions["action"]
        self.assertEqual(set(action.criteria), {"none", "reply"})
        self.assertNotIn("join", action.criteria)
        self.assertEqual(set(ACTION_CRITERIA_GROUP), {"none", "reply"})
        self.assertIn("自然参与", ACTION_CRITERIA_GROUP["reply"])
        self.assertIn("插话", ACTION_CRITERIA_GROUP["reply"])
        self.assertIn("针对某条", ACTION_CRITERIA_GROUP["reply"])
        self.assertNotIn("join", action.instructions)
        target = questions["target_message_id"]
        self.assertIn("latest", target.instructions)
        self.assertNotIn("join", target.instructions)
        self.assertIn("m1", target.criteria)
        self.assertIn("m2", target.criteria)

    def test_proactive_questions_unchanged(self) -> None:
        questions = _build_questions("group_proactive", _sample_messages(), [])
        self.assertEqual(set(questions["idle_action"].criteria), {"none", "proactive"})
        self.assertEqual(set(ACTION_CRITERIA_PROACTIVE), {"none", "proactive"})
        self.assertNotIn("target_message_id", questions)

    def test_private_passive_still_defaults_to_reply(self) -> None:
        questions = _build_questions("private_passive", _sample_messages(), [])
        self.assertNotIn("action", questions)
        result = _parse_answers(
            {},
            scene="private_passive",
            settings=_settings(),
            memes=[],
        )
        self.assertEqual(result.action, "reply")
        self.assertEqual(result.raw["action"].get("note"), "private_passive_default_reply")
        self.assertTrue(result.should_speak())

    def test_reply_above_threshold_speaks(self) -> None:
        result = _parse_answers(
            {"action": _action("reply", 0.75, none=0.25)},
            scene="group_passive",
            settings=_settings(0.7),
            memes=[],
        )
        self.assertEqual(result.action, "reply")
        self.assertTrue(result.should_speak())
        self.assertEqual(result.raw["action"]["choice"], "reply")
        self.assertEqual(result.raw["action"]["probability"], 0.75)

    def test_unknown_target_message_id_is_ignored(self) -> None:
        result = _parse_answers(
            {
                "action": _action("reply", 0.9, none=0.1),
                "target_message_id": _action("not-in-window", 0.9),
            },
            scene="group_passive",
            settings=_settings(0.7),
            memes=[],
            target_message_ids={"m1", "m2"},
        )
        self.assertIsNone(result.target_message_id)

    def test_reply_below_threshold_is_none_without_adding_join(self) -> None:
        # 与真实日志类似：reply 0.602、join 0.387，不得把两者相加后过阈值。
        result = _parse_answers(
            {
                "action": _action(
                    "reply",
                    0.6020004108843966,
                    none=0.011167873046894618,
                    join=0.38683171606870886,
                )
            },
            scene="group_passive",
            settings=_settings(0.7),
            memes=[],
        )
        self.assertEqual(result.action, "none")
        self.assertFalse(result.should_speak())
        raw = result.raw["action"]
        self.assertEqual(raw["choice"], "reply")
        self.assertEqual(raw["probability"], 0.6020004108843966)
        self.assertEqual(raw["probabilities"]["join"], 0.38683171606870886)

    def test_legacy_join_above_threshold_normalizes_to_reply(self) -> None:
        result = _parse_answers(
            {"action": _action("join", 0.81, reply=0.14, none=0.05)},
            scene="group_passive",
            settings=_settings(0.7),
            memes=[],
        )
        self.assertEqual(result.action, "reply")
        self.assertEqual(normalize_action("join"), "reply")
        self.assertTrue(result.should_speak())
        raw = result.raw["action"]
        # raw 保留提供商原选，便于对照历史日志，且概率仍是 join 本身，不是 reply+join。
        self.assertEqual(raw["choice"], "join")
        self.assertEqual(raw["probability"], 0.81)
        self.assertEqual(raw["probabilities"]["reply"], 0.14)

    def test_legacy_join_below_threshold_stays_none(self) -> None:
        result = _parse_answers(
            {"action": _action("join", 0.56, reply=0.34, none=0.10)},
            scene="group_passive",
            settings=_settings(0.7),
            memes=[],
        )
        self.assertEqual(result.action, "none")
        self.assertFalse(result.should_speak())
        self.assertEqual(result.raw["action"]["choice"], "join")
        self.assertEqual(result.raw["action"]["probability"], 0.56)

    def test_legacy_join_result_still_speaks_and_does_not_force_quote(self) -> None:
        legacy = DecisionResult(action="join", target_message_id="123")
        self.assertTrue(legacy.should_speak())
        self.assertEqual(normalize_action(legacy.action), "reply")
        omitted = build_send_chain(
            "hello", _DummyStore(), legacy, quote_target=False
        )
        self.assertFalse(any(getattr(item, "id", None) == "123" for item in omitted.chain))
        quoted = build_send_chain("hello", _DummyStore(), legacy, quote_target=True)
        self.assertTrue(any(getattr(item, "id", None) == "123" for item in quoted.chain))
        no_target = DecisionResult(action="reply", target_message_id=None)
        bare = build_send_chain("hello", _DummyStore(), no_target, quote_target=True)
        self.assertFalse(any(hasattr(item, "id") and getattr(item, "text", None) is None for item in bare.chain))
        self.assertTrue(any(getattr(item, "text", None) == "hello" for item in bare.chain))


if __name__ == "__main__":
    unittest.main()
