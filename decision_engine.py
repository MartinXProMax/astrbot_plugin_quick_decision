"""调用官方判断提供商，并解析为业务决策。"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from astrbot.api import logger
from astrbot.api.provider import (
    DecisionBooleanAnswer,
    DecisionBooleanQuestion,
    DecisionChoiceAnswer,
    DecisionChoiceQuestion,
    DecisionError,
)

from .config_util import PluginSettings
from .models import (
    RESERVED_QUESTION_IDS,
    BufferedMessage,
    CustomDecisionQuestion,
    DecisionOutcome,
    DecisionResult,
    SceneType,
    normalize_action,
)

ACTION_CRITERIA_GROUP = {
    "none": "现在不适合发言，保持沉默",
    "reply": (
        "现在适合发言：可以针对某条具体消息回复，"
        "也可以自然参与当前讨论或插话，不必强行引用某一条"
    ),
}
ACTION_CRITERIA_PROACTIVE = {
    "none": "现在仍应保持沉默，不要主动开口",
    "proactive": "现在适合在冷场后主动开口",
}

Question = DecisionBooleanQuestion | DecisionChoiceQuestion
Binding = Literal["action", "target_message_id", "use_meme", "meme_id", "session_hint"]
_MISSING = object()


@dataclass
class DecisionSpec:
    """固定本轮题目、选择规则及结果用途；question=None 表示本轮无需提问。"""

    id: str
    kind: Literal["choice", "boolean", "fixed"]
    question: Question | None
    binding: Binding
    default: str | bool | None = None
    threshold: float = 0.0
    temperature: float = 0.0
    allowed: frozenset[str] = frozenset()
    option_weights: dict[str, float] = field(default_factory=dict)
    session_hints: dict[str | bool, str] = field(default_factory=dict)
    stage: str = "pre_reply"


def _select_option(
    probabilities: Mapping[str, float],
    *,
    temperature: float,
    option_weights: Mapping[str, float],
) -> tuple[str | None, dict[str, float]]:
    scores = {
        option_id: math.log(probability) + math.log(option_weights.get(option_id, 1.0))
        for option_id, probability in probabilities.items()
        if isinstance(probability, (int, float))
        and math.isfinite(probability)
        and 0.0 < probability <= 1.0
        and option_weights.get(option_id, 1.0) > 0.0
    }
    if not scores:
        return None, {}
    best = max(scores, key=scores.__getitem__)
    if temperature == 0.0:
        return best, {key: float(key == best) for key in scores}
    # 先减去最大分数，避免极低温度下权重全部下溢为零。
    weights = {
        key: math.exp((score - scores[best]) / temperature)
        for key, score in scores.items()
    }
    total = sum(weights.values())
    distribution = {key: weight / total for key, weight in weights.items()}
    selected = random.choices(list(weights), weights=list(weights.values()), k=1)[0]
    return selected, distribution


def _evaluate_answer(spec: DecisionSpec, answer: object) -> DecisionOutcome:
    outcome = DecisionOutcome(value=spec.default)
    if spec.kind == "fixed":
        outcome.status = "skipped"
        outcome.reason = "private_passive_default_reply"
        outcome.raw = {"choice": spec.default, "note": outcome.reason}
        return outcome
    if spec.question is None:
        outcome.status, outcome.reason = "skipped", "not_asked"
        return outcome
    if answer is _MISSING:
        outcome.status, outcome.reason = "missing", "missing_answer"
        return outcome
    is_choice = spec.kind == "choice"
    raw = outcome.raw
    if is_choice and isinstance(answer, DecisionChoiceAnswer):
        raw.update(
            choice=answer.choice,
            probability=answer.probability,
            probabilities=dict(answer.probabilities),
            confidence=answer.confidence,
        )
        outcome.probabilities = dict(answer.probabilities)
        selected = answer.choice
    elif not is_choice and isinstance(answer, DecisionBooleanAnswer):
        raw["probability"] = answer.probability
        outcome.probabilities = {
            "true": answer.probability,
            "false": 1.0 - answer.probability,
        }
        selected = "true" if answer.probability >= spec.threshold else "false"
    else:
        outcome.status, outcome.reason = "invalid", "answer_type"
        return outcome
    selected_probability = answer.probability
    if spec.temperature > 0.0 or any(w != 1.0 for w in spec.option_weights.values()):
        selected, distribution = _select_option(
            {
                key: value
                for key, value in outcome.probabilities.items()
                if key in spec.allowed
            },
            temperature=spec.temperature,
            option_weights=spec.option_weights,
        )
        outcome.selection_probabilities = distribution
        raw.update(
            selected_choice=selected,
            selection_probabilities=distribution,
            selection_temperature=spec.temperature,
            option_weights=dict(spec.option_weights),
        )
        if selected is None:
            outcome.status, outcome.reason = "invalid", "no_selectable_option"
            return outcome
        selected_probability = outcome.probabilities[selected]
        if is_choice:
            raw["selected_probability"] = selected_probability
    if selected not in spec.allowed:
        outcome.status, outcome.reason = "invalid", "unknown_option"
    elif is_choice and not selected_probability >= spec.threshold:
        outcome.status, outcome.reason = "fallback", "below_threshold"
    else:
        outcome.value = selected if is_choice else selected == "true"
    return outcome


def _evaluate_answers(
    specs: list[DecisionSpec], answers: Mapping[str, object]
) -> dict[str, DecisionOutcome]:
    # 旧解析器最后才采样回复目标；保留顺序，避免同一随机输入下选中不同选项。
    ordered = [spec for spec in specs if spec.binding != "target_message_id"]
    ordered.extend(spec for spec in specs if spec.binding == "target_message_id")
    return {
        spec.id: _evaluate_answer(spec, answers.get(spec.id, _MISSING))
        for spec in ordered
    }


async def evaluate_specs(
    decision,
    specs: list[DecisionSpec],
    *,
    state: dict[str, Any],
    provider_id: str = "",
) -> dict[str, DecisionOutcome]:
    """批量提问并评估；批次异常交由调用者按原场景规则降级。"""
    questions = {spec.id: spec.question for spec in specs if spec.question is not None}
    answers = {}
    if questions:
        answers = await decision.ask(
            questions,
            state=state,
            **({"provider_id": provider_id} if provider_id else {}),
        )
    return _evaluate_answers(specs, dict(answers))


def build_state(
    *,
    scene: SceneType,
    is_private: bool,
    messages: list[BufferedMessage],
    at_or_quoted: bool,
    memes: list[dict[str, str]],
    custom_context: str = "",
) -> dict[str, Any]:
    """只提供精简上下文，不包含人设、Skill 或官方完整注入。"""
    state: dict[str, Any] = {
        "scene": scene,
        "chat_type": "private" if is_private else "group",
        "current_message_at_or_quoted_bot": at_or_quoted,
        "recent_messages": [item.to_state_dict() for item in messages],
        "meme_catalog": memes,
    }
    context = custom_context.strip()
    if context:
        state["custom_context"] = context
    return state


def _add_custom_questions(
    questions: dict[str, DecisionBooleanQuestion | DecisionChoiceQuestion],
    configured: list[CustomDecisionQuestion],
) -> None:
    """追加用户配置的判断问题；不会覆盖内置的发言、对象和表情问题。"""
    for item in configured:
        if (
            not item.enabled
            or not item.id
            or item.id in questions
            or item.id in RESERVED_QUESTION_IDS
        ):
            continue
        if item.type == "choice":
            criteria = {option.id: option.criteria for option in item.options}
            if len(criteria) < 2:
                continue
            questions[item.id] = DecisionChoiceQuestion(
                instructions=item.instructions,
                criteria=criteria,
            )
        elif item.type == "boolean":
            questions[item.id] = DecisionBooleanQuestion(item.instructions)


def _build_questions(
    scene: SceneType,
    messages: list[BufferedMessage],
    memes: list[dict[str, str]],
    configured: list[CustomDecisionQuestion] | None = None,
) -> dict[str, DecisionBooleanQuestion | DecisionChoiceQuestion]:
    questions: dict[str, DecisionBooleanQuestion | DecisionChoiceQuestion] = {}
    if scene == "group_passive":
        questions["action"] = DecisionChoiceQuestion(
            instructions=(
                "根据近期群聊判断机器人现在应该怎么做。"
                "reply 表示发言，既包括针对某条消息回复，也包括自然参与讨论或插话；"
                "即使被 @ 或引用，也可以选择 none 不回复。"
            ),
            criteria=ACTION_CRITERIA_GROUP,
        )
        # 选择类问题至少需要两个选项；始终提供 latest 作为兜底。
        target_criteria = {"latest": "针对最近一条消息回复"}
        for item in messages[-8:]:
            if item.message_id and item.role == "user":
                preview = item.text[:40] or "（无文本）"
                target_criteria[item.message_id] = (
                    f"回复 {item.sender_name or item.sender_id} 的消息：{preview}"
                )
        if len(target_criteria) >= 2:
            questions["target_message_id"] = DecisionChoiceQuestion(
                instructions="如果决定发言，可以针对哪一条消息？若不针对某一条特定消息，可选 latest。",
                criteria=target_criteria,
            )
    elif scene in {"group_proactive", "private_proactive"}:
        questions["action"] = DecisionChoiceQuestion(
            instructions="会话已经沉默一段时间。判断现在是否适合主动开口。",
            criteria=ACTION_CRITERIA_PROACTIVE,
        )
    # 私聊被动默认要回复，不问 action。
    _add_custom_questions(questions, configured or [])
    questions["use_meme"] = DecisionBooleanQuestion(
        "这次回复是否适合配上一张表情包？没有合适的表情时应为否。"
    )
    if memes:
        meme_criteria = {"none": "不配表情包"}
        for meme in memes:
            meme_criteria[meme["id"]] = meme["description"]
        questions["meme_id"] = DecisionChoiceQuestion(
            instructions=(
                "若需要配图，请根据描述选择最合适的一张表情包；不合适则选 none。"
                "只根据描述选择，不要假设你能看到原图。"
            ),
            criteria=meme_criteria,
        )
    return questions


def _build_specs(
    questions: Mapping[str, Question],
    *,
    scene: SceneType,
    settings: PluginSettings,
) -> list[DecisionSpec]:
    """将内置规则和旧自定义配置映射到同一种题目定义，并固定候选及提示。"""
    threshold = settings.choice_threshold
    temperature = settings.choice_temperature
    private_default = scene == "private_passive"
    action_allowed = (
        {"none", "reply", "join"} if scene == "group_passive" else {"none", "proactive"}
    )
    specs = [
        DecisionSpec(
            id="action",
            kind="fixed" if private_default else "choice",
            question=questions.get("action"),
            binding="action",
            default="reply" if private_default else "none",
            threshold=threshold,
            temperature=temperature,
            allowed=frozenset(action_allowed),
        )
    ]
    target = questions.get("target_message_id")
    specs.append(
        DecisionSpec(
            id="target_message_id",
            kind="choice",
            question=target,
            binding="target_message_id",
            default="latest",
            temperature=temperature,
            allowed=frozenset(target.criteria if target is not None else {"latest"}),
        )
    )
    for item in settings.decision_questions:
        question = questions.get(item.id)
        if not item.enabled or item.id in RESERVED_QUESTION_IDS or question is None:
            continue
        if item.type == "choice":
            weights = {option.id: option.weight for option in item.options}
            hints = {option.id: option.session_hint for option in item.options}
            allowed = frozenset(question.criteria)
        else:
            weights = {"true": item.true_weight, "false": item.false_weight}
            hints = {True: item.true_session_hint, False: item.false_session_hint}
            allowed = frozenset({"true", "false"})
        specs.append(
            DecisionSpec(
                id=item.id,
                kind=item.type,
                question=question,
                binding="session_hint",
                threshold=0.5 if item.type == "boolean" else 0.0,
                temperature=temperature,
                allowed=allowed,
                option_weights=weights,
                session_hints=hints,
            )
        )
    meme_question = questions.get("meme_id")
    specs.extend(
        [
            DecisionSpec(
                id="use_meme",
                kind="boolean",
                question=questions.get("use_meme"),
                binding="use_meme",
                default=False,
                threshold=threshold,
                allowed=frozenset({"true", "false"}),
            ),
            DecisionSpec(
                id="meme_id",
                kind="choice",
                question=meme_question,
                binding="meme_id",
                default="none",
                threshold=threshold,
                temperature=temperature,
                allowed=frozenset(
                    meme_question.criteria if meme_question is not None else {"none"}
                ),
            ),
        ]
    )
    return specs


def _assemble_result(
    specs: list[DecisionSpec],
    outcomes: dict[str, DecisionOutcome],
    *,
    scene: SceneType,
) -> DecisionResult:
    """统一结果只投影一次；旧调用者、提示注入和发送路径继续读取兼容字段。"""
    result = DecisionResult(scene=scene, outcomes=outcomes)
    by_id = {spec.id: spec for spec in specs}
    for question_id, outcome in outcomes.items():
        spec = by_id[question_id]
        binding, value = spec.binding, outcome.value
        if (
            binding in {"action", "use_meme"}
            or outcome.raw
            or (binding == "meme_id" and outcome.status not in {"missing", "skipped"})
        ):
            result.raw[question_id] = outcome.raw
        if binding == "session_hint":
            if outcome.status != "ok":
                continue
            result.custom_answers[question_id] = value
            if question_id == "length" and value in {"short", "long"}:
                result.length = value
            hint = spec.session_hints.get(value, "").strip()
            if hint:
                result.session_hints.append(hint)
        elif binding == "action":
            result.action = normalize_action(value)
        elif binding == "target_message_id":
            result.target_message_id = value if value != "latest" else None
        elif binding == "use_meme":
            result.use_meme = bool(value)
        elif binding == "meme_id":
            result.meme_id = value if value != "none" else None
    if not result.use_meme or result.meme_id is None:
        result.use_meme, result.meme_id = False, None
    return result


def _parse_answers(
    answers: dict[str, object],
    *,
    scene: SceneType,
    settings: PluginSettings,
    memes: list[dict[str, str]],
    target_message_ids: set[str] | None = None,
) -> DecisionResult:
    """保留本地解析入口；实际调用通过 evaluate_specs 使用提问时的同一快照。"""
    questions = _build_questions(scene, [], memes, settings.decision_questions)
    if scene == "group_passive":
        allowed = {"latest"}
        target_answer = answers.get("target_message_id")
        if target_message_ids is not None:
            allowed.update(target_message_ids)
        elif isinstance(target_answer, DecisionChoiceAnswer):
            allowed.update(target_answer.probabilities)
            allowed.add(target_answer.choice)
        if len(allowed) >= 2:
            questions["target_message_id"] = DecisionChoiceQuestion(
                instructions="回复目标", criteria={mid: mid for mid in allowed}
            )
    specs = _build_specs(questions, scene=scene, settings=settings)
    return _assemble_result(specs, _evaluate_answers(specs, answers), scene=scene)


async def ask_decision(
    context,
    *,
    settings: PluginSettings,
    scene: SceneType,
    is_private: bool,
    messages: list[BufferedMessage],
    at_or_quoted: bool,
    memes: list[dict[str, str]],
) -> DecisionResult:
    """一次 ask() 问完本轮需要的问题。失败时返回带 error 的结果。"""
    started = time.monotonic()
    if settings.decision_questions_warning:
        logger.warning("判断问题配置：%s", settings.decision_questions_warning)
    state = build_state(
        scene=scene,
        is_private=is_private,
        messages=messages,
        at_or_quoted=at_or_quoted,
        memes=memes,
        custom_context=settings.decision_custom_context,
    )
    questions = _build_questions(scene, messages, memes, settings.decision_questions)
    specs = _build_specs(questions, scene=scene, settings=settings)
    try:
        outcomes = await evaluate_specs(
            context.decision,
            specs,
            state=state,
            provider_id=settings.decision_provider_id,
        )
        result = _assemble_result(specs, outcomes, scene=scene)
    except Exception as exc:  # noqa: BLE001
        error = str(exc) or type(exc).__name__
        if isinstance(exc, DecisionError):
            logger.warning("判断提供商调用失败：%s", error)
        else:
            logger.exception("判断过程出现未预期异常")
        result = DecisionResult(
            action="none" if scene != "private_passive" else "reply",
            scene=scene,
            error=error,
            degraded=True,
            outcomes={
                spec.id: _evaluate_answer(spec, _MISSING)
                if spec.question is None
                else DecisionOutcome(
                    value=spec.default, status="fallback", reason="batch_error"
                )
                for spec in specs
            },
        )
    result.duration_ms = int((time.monotonic() - started) * 1000)
    result.dry_run = settings.dry_run
    return result
