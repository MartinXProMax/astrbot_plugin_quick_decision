"""v1.1 配置、判断问题和会话提示的本地测试。不访问网络，不连接 QQ。"""

# ruff: noqa: E402, I001

from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_astrbot_stubs() -> None:
    """用轻量桩模块代替完整 AstrBot，避免测试依赖网络和可选依赖。"""
    if "astrbot.api.provider" in sys.modules and hasattr(
        sys.modules["astrbot.api.provider"], "DecisionChoiceAnswer"
    ):
        return

    def ensure(name: str) -> types.ModuleType:
        if name in sys.modules:
            return sys.modules[name]
        module = types.ModuleType(name)
        module.__path__ = []  # type: ignore[attr-defined]
        sys.modules[name] = module
        if "." in name:
            parent_name, child = name.rsplit(".", 1)
            setattr(ensure(parent_name), child, module)
        return module

    for name in (
        "astrbot",
        "astrbot.api",
        "astrbot.api.event",
        "astrbot.api.message_components",
        "astrbot.api.provider",
        "astrbot.core",
        "astrbot.core.agent",
        "astrbot.core.agent.message",
    ):
        ensure(name)

    api = sys.modules["astrbot.api"]
    api.logger = types.SimpleNamespace(
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
        exception=lambda *args, **kwargs: None,
        debug=lambda *args, **kwargs: None,
    )

    @dataclass(frozen=True)
    class DecisionChoiceQuestion:
        instructions: str
        criteria: Mapping[str, str]

    @dataclass(frozen=True)
    class DecisionBooleanQuestion:
        instructions: str

    @dataclass(frozen=True)
    class DecisionChoiceAnswer:
        choice: str
        probability: float
        probabilities: Mapping[str, float]
        confidence: float

    @dataclass(frozen=True)
    class DecisionBooleanAnswer:
        probability: float

    class DecisionError(Exception):
        pass

    provider = sys.modules["astrbot.api.provider"]
    provider.DecisionChoiceQuestion = DecisionChoiceQuestion
    provider.DecisionBooleanQuestion = DecisionBooleanQuestion
    provider.DecisionChoiceAnswer = DecisionChoiceAnswer
    provider.DecisionBooleanAnswer = DecisionBooleanAnswer
    provider.DecisionProvider = type("DecisionProvider", (), {})
    provider.ProviderType = types.SimpleNamespace(DECISION="decision")
    provider.DecisionError = DecisionError
    provider.ProviderRequest = type("ProviderRequest", (), {})
    provider.LLMResponse = type("LLMResponse", (), {})

    class TextPart:
        def __init__(self, text: str = "") -> None:
            self.text = text
            self._no_save = False
            self.type = "text"

        def mark_as_temp(self) -> TextPart:
            self._no_save = True
            return self

    sys.modules["astrbot.core.agent.message"].TextPart = TextPart
    sys.modules["astrbot.api.event"].MessageChain = type("MessageChain", (), {})
    @dataclass
    class Plain:
        text: str

    @dataclass
    class Reply:
        id: str

    components = sys.modules["astrbot.api.message_components"]
    components.Plain = Plain
    components.Reply = Reply
    components.Image = type("Image", (), {})


_install_astrbot_stubs()

from astrbot.api.provider import DecisionBooleanAnswer, DecisionChoiceAnswer
from astrbot.core.agent.message import TextPart
from astrbot_plugin_quick_decision.config_util import (
    DEFAULT_LENGTH_INSTRUCTIONS,
    DEFAULT_LONG_CRITERIA,
    DEFAULT_LONG_SESSION_HINT,
    DEFAULT_SHORT_CRITERIA,
    DEFAULT_SHORT_SESSION_HINT,
    PluginSettings,
    default_decision_questions, default_graph, default_questions, load_questions,
    dump_decision_questions,
    parse_decision_questions,
)
from astrbot_plugin_quick_decision.decision_engine import build_state
from engine_helpers import _build_questions, _parse_answers
from astrbot_plugin_quick_decision.models import (
    BufferedMessage,
    DecisionQuestion,
    DecisionQuestionOption,
    DecisionResult,
)
from astrbot_plugin_quick_decision.reply_generator import (
    apply_session_hints,
    session_hint_text,
)


def _choice(choice: str, probability: float = 0.4) -> DecisionChoiceAnswer:
    others = {"short": 0.3, "long": 0.3}
    others[choice] = probability
    return DecisionChoiceAnswer(
        choice=choice,
        probability=probability,
        probabilities=others,
        confidence=probability,
    )


class DecisionConfigTests(unittest.TestCase):
    def test_schema_default_matches_builtin_length_question(self) -> None:
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn("decision_custom_context", schema)
        self.assertEqual(schema["choice_temperature"]["default"], 0)
        self.assertEqual(schema["decision_custom_context"]["type"], "text")
        self.assertEqual(schema["decision_custom_context"]["default"], "")
        self.assertEqual(schema["decision_questions"]["type"], "text")
        self.assertTrue(schema["decision_questions"]["editor_mode"])
        parsed_schema, warning = load_questions(schema["decision_questions"]["default"], strict=True)
        self.assertIsNone(warning)
        # 安装默认值与「恢复默认问题」相同，都是选项动作格式，含是否用语音和朗读语气。
        self.assertEqual([q.to_dict() for q in parsed_schema], [q.to_dict() for q in default_graph()])
        self.assertEqual([q.id for q in parsed_schema][-2:], ["use_voice", "voice_tone"])
        question = next(q for q in parsed_schema if q.id == "length")
        self.assertEqual(question.id, "length")
        self.assertEqual(question.instructions, DEFAULT_LENGTH_INSTRUCTIONS)
        by_id = {option.id: option for option in question.options}
        self.assertEqual(by_id["short"].criteria, DEFAULT_SHORT_CRITERIA)
        self.assertIn({"tool": "add_context", "text": DEFAULT_SHORT_SESSION_HINT}, question.actions["short"])
        self.assertEqual(by_id["long"].criteria, DEFAULT_LONG_CRITERIA)
        self.assertIn({"tool": "add_context", "text": DEFAULT_LONG_SESSION_HINT}, question.actions["long"])

    def test_missing_or_blank_questions_use_default(self) -> None:
        for value in (None, "", "  ", "\n"):
            questions, warning = parse_decision_questions(value)
            self.assertIsNone(warning, value)
            self.assertEqual(questions[0].id, "length")
            self.assertEqual(len(questions), 1)

    def test_invalid_json_falls_back_to_default(self) -> None:
        questions, warning = parse_decision_questions("{not json")
        self.assertIsNotNone(warning)
        self.assertIn("回退到默认长短问题", warning or "")
        self.assertEqual([item.id for item in questions], ["length"])
        settings = PluginSettings({"decision_questions": "[[[invalid"})
        self.assertEqual([q.id for q in settings.decision_questions], [q.id for q in default_questions()])
        self.assertIsNotNone(settings.decision_questions_warning)

    def test_empty_array_is_valid(self) -> None:
        questions, warning = parse_decision_questions("[]")
        self.assertIsNone(warning)
        self.assertEqual(questions, [])
        questions, error = parse_decision_questions([], strict=True)
        self.assertIsNone(error)
        self.assertEqual(questions, [])

    def test_reserved_ids_skipped_or_rejected(self) -> None:
        payload = [
            {
                "id": "action",
                "type": "choice",
                "instructions": "不该出现",
                "options": [
                    {"id": "a", "criteria": "A"},
                    {"id": "b", "criteria": "B"},
                ],
            },
            {
                "id": "tone",
                "type": "choice",
                "instructions": "语气如何？",
                "options": [
                    {
                        "id": "soft",
                        "criteria": "柔和",
                        "session_hint": "语气柔和一点。",
                    },
                    {"id": "direct", "criteria": "直接", "session_hint": ""},
                ],
            },
        ]
        questions, warning = parse_decision_questions(payload)
        self.assertIn("保留 ID", warning)
        self.assertEqual([q.id for q in questions], ["tone"])
        effective, warning = load_questions(payload)
        self.assertEqual(next(q for q in effective if q.id == "action").role, "action")
        effective, warning = load_questions({"version": 2, "questions": payload}, strict=True)
        self.assertIsNone(warning)
        self.assertEqual(next(q for q in effective if q.id == "action").role, "answer")

    def test_choice_needs_two_options(self) -> None:
        payload = [
            {
                "id": "tone",
                "type": "choice",
                "instructions": "语气如何？",
                "options": [{"id": "soft", "criteria": "柔和"}],
            }
        ]
        questions, warning = parse_decision_questions(payload)
        self.assertEqual([item.id for item in questions], ["length"])
        self.assertIsNotNone(warning)
        _, error = parse_decision_questions(payload, strict=True)
        self.assertIn("两个", error or "")

    def test_plugin_settings_custom_context_stripped(self) -> None:
        settings = PluginSettings({"decision_custom_context": "  机器人叫小狼  "})
        self.assertEqual(settings.decision_custom_context, "机器人叫小狼")
        empty = PluginSettings({"decision_custom_context": "   "})
        self.assertEqual(empty.decision_custom_context, "")

    def test_choice_temperature_defaults_and_is_clamped(self) -> None:
        self.assertEqual(PluginSettings({}).choice_temperature, 0.0)
        self.assertEqual(
            PluginSettings({"choice_temperature": 3}).choice_temperature,
            2.0,
        )
        self.assertEqual(
            PluginSettings({"choice_temperature": -1}).choice_temperature,
            0.0,
        )
        for value in (float("nan"), float("inf"), "invalid", 10**400):
            self.assertEqual(PluginSettings({"choice_temperature": value}).choice_temperature, 0.0)

    def test_question_can_override_default_decision_mode(self) -> None:
        payload = [{
            "id": "tone", "type": "choice", "instructions": "语气？",
            "decision_mode": "random",
            "options": [{"id": "soft", "criteria": "柔和"}, {"id": "direct", "criteria": "直接"}],
        }]
        questions, error = load_questions({"version": 2, "questions": payload}, strict=True)
        self.assertIsNone(error)
        self.assertEqual(questions[0].decision_mode, "random")
        self.assertEqual(questions[0].to_dict()["decision_mode"], "random")
        payload[0]["decision_mode"] = "invalid"
        self.assertIn("决策模式无效", load_questions({"version": 2, "questions": payload}, strict=True)[1])

    def test_choice_temperature_can_select_a_lower_probability_option(self) -> None:
        settings = PluginSettings(
            {
                "choice_temperature": 1.0,
                "decision_questions": dump_decision_questions(
                    [
                        DecisionQuestion(
                            id="length",
                            type="choice",
                            instructions="回复长短？",
                            options=[
                                DecisionQuestionOption(id="short", criteria="短"),
                                DecisionQuestionOption(id="long", criteria="长"),
                            ],
                        )
                    ]
                ),
            }
        )
        with patch(
            "astrbot_plugin_quick_decision.decision_engine.random.choices",
            return_value=["long"],
        ) as choices:
            result = _parse_answers(
                {"length": _choice("short", 0.8)},
                scene="private_passive",
                settings=settings,
                memes=[],
            )
        self.assertEqual(result.length, "long")
        self.assertEqual(result.raw["length"]["choice"], "short")
        self.assertEqual(result.raw["length"]["selected_choice"], "long")
        self.assertEqual(result.raw["length"]["selected_probability"], 0.3)
        choices.assert_called_once()

    def test_option_weights_roundtrip_and_invalid_weights_are_rejected(self) -> None:
        questions = default_decision_questions() + [
            DecisionQuestion(
                id="followup", type="boolean", instructions="要追问吗？",
                true_weight=3.0, false_weight=0.5,
            )
        ]
        questions[0].options[1].weight = 3.0
        payload = json.loads(dump_decision_questions(questions))
        loaded, error = parse_decision_questions(payload, strict=True)
        self.assertIsNone(error)
        self.assertEqual([item.to_dict() for item in loaded], payload)
        for value in (-1, 101, None, True, "invalid", float("nan"), float("inf")):
            for field in ("weight", "true_weight", "false_weight"):
                with self.subTest(value=value, field=field):
                    invalid = json.loads(dump_decision_questions(questions))
                    if field == "weight":
                        invalid[0]["options"][0][field] = value
                    else:
                        invalid[1][field] = value
                    _, error = parse_decision_questions(invalid, strict=True)
                    self.assertIn("权重", error or "")
        for question in payload:
            if question["type"] == "choice":
                for option in question["options"]:
                    option["weight"] = 0
            else:
                question["true_weight"] = question["false_weight"] = 0
            _, error = parse_decision_questions([question], strict=True)
            self.assertIn("至少需要一个", error or "")

    def test_weighted_choice_selects_lower_model_probability_and_its_hint(self) -> None:
        questions = default_decision_questions()
        questions[0].options[1].weight = 3.0
        result = _parse_answers(
            {"length": _choice("short", 0.7)},
            scene="private_passive",
            settings=PluginSettings({"decision_questions": dump_decision_questions(questions)}),
            memes=[],
        )
        self.assertEqual(result.length, "long")
        self.assertEqual(result.session_hints, [DEFAULT_LONG_SESSION_HINT])
        self.assertEqual(result.raw["length"]["choice"], "short")
        self.assertEqual(result.raw["length"]["option_weights"]["long"], 3.0)

    def test_temperature_adjusts_the_weighted_distribution(self) -> None:
        questions = default_decision_questions()
        questions[0].options[1].weight = 2.0
        answer = DecisionChoiceAnswer(
            choice="short", probability=0.8,
            probabilities={"short": 0.8, "long": 0.2, "unknown": 1.0}, confidence=0.8,
        )
        for temperature, expected_long in ((0.5, 0.2), (1.0, 1 / 3), (2.0, 1 / (1 + 2**0.5))):
            with self.subTest(temperature=temperature), patch(
                "astrbot_plugin_quick_decision.decision_engine.random.choices", return_value=["long"]
            ) as sample:
                result = _parse_answers(
                    {"length": answer}, scene="private_passive", memes=[],
                    settings=PluginSettings({
                        "choice_temperature": temperature,
                        "decision_questions": dump_decision_questions(questions),
                    }),
                )
                distribution = result.raw["length"]["selection_probabilities"]
                self.assertAlmostEqual(distribution["long"], expected_long)
                self.assertAlmostEqual(sum(distribution.values()), 1.0)
                self.assertEqual(set(sample.call_args.args[0]), {"short", "long"})
                passed = dict(zip(sample.call_args.args[0], sample.call_args.kwargs["weights"]))
                self.assertAlmostEqual(passed["long"] / sum(passed.values()), expected_long)
                self.assertEqual(result.session_hints, [DEFAULT_LONG_SESSION_HINT])

    def test_zero_weight_excludes_an_option_with_or_without_sampling(self) -> None:
        questions = default_decision_questions()
        questions[0].options[0].weight = 0.0
        for temperature in (0.0, 1.0):
            result = _parse_answers(
                {"length": _choice("short", 0.9)},
                scene="private_passive", memes=[],
                settings=PluginSettings({
                    "choice_temperature": temperature,
                    "decision_questions": dump_decision_questions(questions),
                }),
            )
            self.assertEqual(result.length, "long")
            self.assertNotIn("short", result.raw["length"]["selection_probabilities"])

    def test_weighted_boolean_uses_selected_hint(self) -> None:
        question = DecisionQuestion(
            id="followup", type="boolean", instructions="要追问吗？",
            true_session_hint="可以追问", false_session_hint="不用追问",
            true_weight=3.0,
        )
        for temperature in (0.0, 1.0):
            with patch(
                "astrbot_plugin_quick_decision.decision_engine.random.choices", return_value=["true"]
            ):
                result = _parse_answers(
                    {"followup": DecisionBooleanAnswer(probability=0.4)},
                    scene="private_passive", memes=[],
                    settings=PluginSettings({
                        "choice_temperature": temperature,
                        "decision_questions": dump_decision_questions([question]),
                    }),
                )
            self.assertTrue(result.custom_answers["followup"])
            self.assertEqual(result.session_hints, ["可以追问"])
            self.assertEqual(result.raw["followup"]["probability"], 0.4)
            if temperature == 1.0:
                self.assertAlmostEqual(result.raw["followup"]["selection_probabilities"]["true"], 2 / 3)

    def test_sampling_handles_very_low_temperature_and_invalid_probabilities(self) -> None:
        for probabilities, expected in (
            ({"short": 0.8, "long": 0.2}, "short"),
            ({"short": float("nan"), "long": float("inf")}, None),
            ({"short": 0.0, "long": -1.0}, None),
        ):
            result = _parse_answers(
                {"length": DecisionChoiceAnswer(
                    choice="long", probability=0.2, probabilities=probabilities, confidence=0.8,
                )},
                scene="private_passive", memes=[],
                settings=PluginSettings({"choice_temperature": 1e-300}),
            )
            self.assertEqual(result.length, expected)
            self.assertEqual(bool(result.session_hints), expected is not None)

    def test_sampling_does_not_bypass_builtin_confidence_threshold(self) -> None:
        with patch(
            "astrbot_plugin_quick_decision.decision_engine.random.choices", return_value=["reply"]
        ):
            result = _parse_answers(
                {"action": DecisionChoiceAnswer(
                    choice="none", probability=0.6,
                    probabilities={"none": 0.6, "reply": 0.4}, confidence=0.6,
                )},
                scene="group_passive", memes=[],
                settings=PluginSettings({"choice_temperature": 1, "decision_questions": "[]"}),
            )
        self.assertEqual(result.action, "none")
        self.assertFalse(result.should_speak())

    def test_build_state_custom_context_only_when_nonempty(self) -> None:
        messages = [
            BufferedMessage(
                message_id="m1",
                sender_id="u1",
                sender_name="用户",
                role="user",
                text="你好",
                timestamp=1,
            )
        ]
        empty = build_state(
            scene="group_passive",
            is_private=False,
            messages=messages,
            at_or_quoted=True,
            memes=[],
            custom_context="   ",
        )
        self.assertNotIn("custom_context", empty)
        self.assertEqual(
            set(empty),
            {
                "scene",
                "chat_type",
                "current_message_at_or_quoted_bot",
                "recent_messages",
            },
        )
        filled = build_state(
            scene="group_passive",
            is_private=False,
            messages=messages,
            at_or_quoted=False,
            memes=[{"id": "cat", "description": "微笑的猫"}],
            custom_context=" 机器人在群里叫小狼 ",
        )
        self.assertEqual(filled["custom_context"], "机器人在群里叫小狼")
        for forbidden in ("persona", "skills", "system_prompt", "history"):
            self.assertNotIn(forbidden, filled)

    def test_build_questions_includes_custom_and_keeps_builtin(self) -> None:
        configured = default_decision_questions() + [
            DecisionQuestion(
                id="ask_followup",
                type="boolean",
                instructions="要不要追问一句？",
                true_session_hint="可以自然追问一句。",
            ),
            DecisionQuestion(
                id="disabled_tone",
                type="choice",
                instructions="已停用",
                enabled=False,
                options=[
                    DecisionQuestionOption(id="a", criteria="A"),
                    DecisionQuestionOption(id="b", criteria="B"),
                ],
            ),
        ]
        questions = _build_questions(
            "group_passive",
            [
                BufferedMessage(
                    message_id="m1",
                    sender_id="u1",
                    sender_name="用户",
                    role="user",
                    text="在吗",
                )
            ],
            [{"id": "cat", "description": "微笑的猫"}],
            default_questions(configured),
        )
        self.assertIn("action", questions)
        self.assertIn("target_message_id", questions)
        self.assertIn("length", questions)
        self.assertIn("ask_followup", questions)
        self.assertIn("use_meme", questions)
        self.assertIn("meme_id", questions)
        self.assertNotIn("disabled_tone", questions)
        self.assertIn("这次回复是否适合配上一张表情包", questions["use_meme"].instructions)
        self.assertIn("正文发送成功后", questions["use_meme"].instructions)

    def test_parse_answers_collects_hints_and_length_compat(self) -> None:
        settings = PluginSettings(
            {
                "choice_threshold": 0.9,
                "decision_questions": dump_decision_questions(
                    default_decision_questions()
                    + [
                        DecisionQuestion(
                            id="tone",
                            type="choice",
                            instructions="语气？",
                            options=[
                                DecisionQuestionOption(
                                    id="soft",
                                    criteria="柔和",
                                    session_hint="请用柔和的语气。",
                                ),
                                DecisionQuestionOption(
                                    id="direct",
                                    criteria="直接",
                                    session_hint="",
                                ),
                            ],
                        ),
                        DecisionQuestion(
                            id="ask_followup",
                            type="boolean",
                            instructions="要不要追问？",
                            true_session_hint="可以自然追问一句。",
                            false_session_hint="",
                        ),
                    ]
                ),
            }
        )
        result = _parse_answers(
            {
                "action": _choice("reply", 0.95),
                "length": _choice("long", 0.2),  # 低于 choice_threshold 仍应采纳
                "tone": _choice("soft", 0.15),
                "ask_followup": DecisionBooleanAnswer(probability=0.7),
                "use_meme": DecisionBooleanAnswer(probability=0.1),
            },
            scene="group_passive",
            settings=settings,
            memes=[],
        )
        self.assertEqual(result.action, "reply")
        self.assertEqual(result.length, "long")
        self.assertEqual(result.custom_answers["length"], "long")
        self.assertEqual(result.custom_answers["tone"], "soft")
        self.assertTrue(result.custom_answers["ask_followup"])
        self.assertEqual(
            result.session_hints,
            [
                DEFAULT_LONG_SESSION_HINT,
                "请用柔和的语气。",
                "可以自然追问一句。",
            ],
        )
        self.assertFalse(result.use_meme)

    def test_missing_answer_skips_hint_and_does_not_invent_length(self) -> None:
        settings = PluginSettings({"decision_questions": "[]"})
        result = _parse_answers(
            {"action": _choice("join", 0.9)},
            scene="group_passive",
            settings=settings,
            memes=[],
        )
        self.assertEqual(result.action, "reply")  # 历史 join 规范为 reply
        self.assertIsNone(result.length)
        self.assertEqual(result.custom_answers, {})
        self.assertEqual(result.session_hints, [])
        hint = session_hint_text(result)
        self.assertIn('"动作":"回复"', hint)
        self.assertNotIn("篇幅", hint)
        self.assertNotIn("已选答案", hint)

        settings_with_length = PluginSettings({})
        skipped = _parse_answers(
            {"action": _choice("reply", 0.9), "length": object()},
            scene="group_passive",
            settings=settings_with_length,
            memes=[],
        )
        self.assertIsNone(skipped.length)
        self.assertEqual(skipped.session_hints, [])

    def test_session_hints_injection_and_empty_skip(self) -> None:
        class DummyReq:
            def __init__(self) -> None:
                self.extra_user_content_parts: list[TextPart] = []

        filled = DecisionResult(
            session_hints=[
                "本轮回复策略：请给出简短、自然的回复，一两句即可。",
                "请用柔和的语气。",
                "  ",
            ]
        )
        req = DummyReq()
        apply_session_hints(req, filled)  # type: ignore[arg-type]
        self.assertEqual(len(req.extra_user_content_parts), 1)
        part = req.extra_user_content_parts[0]
        self.assertTrue(getattr(part, "_no_save", False))
        text = part.text
        self.assertTrue(text.startswith("<quick_decision>\n"))
        self.assertIn("请给出简短、自然的回复，一两句即可。", text)
        self.assertIn("请用柔和的语气。", text)
        self.assertTrue(text.endswith("</quick_decision>"))

        empty_req = DummyReq()
        apply_session_hints(empty_req, DecisionResult())  # type: ignore[arg-type]
        self.assertEqual(empty_req.extra_user_content_parts, [])

    def test_custom_context_is_not_a_session_hint(self) -> None:
        settings = PluginSettings(
            {"decision_custom_context": "这段只能给判断模型看，不能出现在会话提示里。"}
        )
        result = _parse_answers(
            {"length": _choice("short", 0.9)},
            scene="private_passive",
            settings=settings,
            memes=[],
        )
        hint = session_hint_text(result)
        self.assertIsNotNone(hint)
        self.assertNotIn("只能给判断模型看", hint or "")
        self.assertNotIn(settings.decision_custom_context, hint or "")

    def test_dump_roundtrip_and_temp_dir_config_file(self) -> None:
        original = default_decision_questions()
        dumped = dump_decision_questions(original)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decision_questions.json"
            path.write_text(dumped, encoding="utf-8")
            loaded, warning = parse_decision_questions(path.read_text(encoding="utf-8"))
        self.assertIsNone(warning)
        self.assertEqual(
            [item.to_dict() for item in loaded],
            [item.to_dict() for item in original],
        )

if __name__ == "__main__":
    unittest.main()
