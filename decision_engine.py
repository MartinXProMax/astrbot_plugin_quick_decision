"""调用官方判断提供商，并解析为业务决策。"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Mapping
from typing import Any

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
    DecisionResult,
    LengthType,
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


def _provider_kwargs(settings: PluginSettings) -> dict[str, str]:
    if settings.decision_provider_id:
        return {"provider_id": settings.decision_provider_id}
    return {}


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


def _choice(
    answer: object,
    *,
    threshold: float,
    allowed: set[str],
    default: str,
    temperature: float = 0.0,
    option_weights: Mapping[str, float] | None = None,
) -> tuple[str, dict[str, Any]]:
    raw: dict[str, Any] = {}
    if not isinstance(answer, DecisionChoiceAnswer):
        return default, raw
    raw = {
        "choice": answer.choice,
        "probability": answer.probability,
        "probabilities": dict(answer.probabilities),
        "confidence": answer.confidence,
    }
    selected = answer.choice
    selected_probability = answer.probability
    option_weights = option_weights or {}
    if temperature > 0.0 or any(weight != 1.0 for weight in option_weights.values()):
        selected, distribution = _select_option(
            {key: value for key, value in answer.probabilities.items() if key in allowed},
            temperature=temperature,
            option_weights=option_weights,
        )
        raw.update(
            selected_choice=selected,
            selection_probabilities=distribution,
            selection_temperature=temperature,
            option_weights=dict(option_weights),
        )
        if selected is None:
            return default, raw
        selected_probability = answer.probabilities[selected]
        raw["selected_probability"] = selected_probability
    if selected in allowed and selected_probability >= threshold:
        return selected, raw
    return default, raw


def _boolean(answer: object, *, threshold: float) -> tuple[bool, dict[str, Any]]:
    if not isinstance(answer, DecisionBooleanAnswer):
        return False, {}
    raw = {"probability": answer.probability}
    return answer.probability >= threshold, raw


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


def _parse_custom_answers(
    answers: dict[str, object],
    configured: list[CustomDecisionQuestion],
    raw: dict[str, Any],
    *,
    temperature: float,
) -> tuple[dict[str, Any], list[str], LengthType | None]:
    """解析用户配置的问题，收集会话提示；不强制用阈值改写选项。"""
    custom_answers: dict[str, Any] = {}
    session_hints: list[str] = []
    length: LengthType | None = None
    for question in configured:
        if not question.enabled:
            continue
        answer = answers.get(question.id)
        if question.type == "choice":
            allowed = {option.id for option in question.options}
            chosen, chosen_raw = _choice(
                answer,
                threshold=0.0,  # 自定义选择题不使用 choice_threshold
                allowed=allowed,
                default="",
                temperature=temperature,
                option_weights={option.id: option.weight for option in question.options},
            )
            if chosen_raw:
                raw[question.id] = chosen_raw
            if not chosen:
                continue
            custom_answers[question.id] = chosen
            if question.id == "length" and chosen in {"short", "long"}:
                length = chosen  # type: ignore[assignment]
            hint_map = {option.id: option.session_hint for option in question.options}
            hint = (hint_map.get(chosen) or "").strip()
            if hint:
                session_hints.append(hint)
        elif question.type == "boolean":
            if not isinstance(answer, DecisionBooleanAnswer):
                continue
            chosen_raw = {"probability": answer.probability}
            raw[question.id] = chosen_raw
            # 是否题以 0.5 为界，不复用表情包的 choice_threshold。
            value = answer.probability >= 0.5
            weights = {"true": question.true_weight, "false": question.false_weight}
            if temperature > 0.0 or any(weight != 1.0 for weight in weights.values()):
                selected, distribution = _select_option(
                    {"true": answer.probability, "false": 1.0 - answer.probability},
                    temperature=temperature,
                    option_weights=weights,
                )
                chosen_raw.update(
                    selected_choice=selected,
                    selection_probabilities=distribution,
                    selection_temperature=temperature,
                    option_weights=weights,
                )
                if selected is None:
                    continue
                value = selected == "true"
            custom_answers[question.id] = value
            hint = (
                question.true_session_hint if value else question.false_session_hint
            ).strip()
            if hint:
                session_hints.append(hint)
    return custom_answers, session_hints, length


def _parse_answers(
    answers: dict[str, object],
    *,
    scene: SceneType,
    settings: PluginSettings,
    memes: list[dict[str, str]],
    target_message_ids: set[str] | None = None,
) -> DecisionResult:
    threshold = settings.choice_threshold
    raw: dict[str, Any] = {}
    if scene == "private_passive":
        action = "reply"
        action_raw = {"choice": "reply", "note": "private_passive_default_reply"}
    else:
        allowed = (
            # 提问已改为 none/reply；join 仅用于兼容尚未改写的旧答案。
            {"none", "reply", "join"}
            if scene == "group_passive"
            else {"none", "proactive"}
        )
        action, action_raw = _choice(
            answers.get("action"),
            threshold=threshold,
            allowed=allowed,
            default="none",
            temperature=settings.choice_temperature,
        )
        action = normalize_action(action)
    raw["action"] = action_raw
    custom_answers, session_hints, length = _parse_custom_answers(
        answers,
        settings.decision_questions,
        raw,
        temperature=settings.choice_temperature,
    )
    use_meme, use_raw = _boolean(answers.get("use_meme"), threshold=threshold)
    raw["use_meme"] = use_raw
    meme_id: str | None = None
    if "meme_id" in answers:
        chosen, meme_raw = _choice(
            answers.get("meme_id"),
            threshold=threshold,
            allowed={meme["id"] for meme in memes} | {"none"},
            default="none",
            temperature=settings.choice_temperature,
        )
        raw["meme_id"] = meme_raw
        if chosen != "none":
            meme_id = chosen
    if not use_meme:
        meme_id = None
    if meme_id is None:
        use_meme = False
    target: str | None = None
    target_answer = answers.get("target_message_id")
    if isinstance(target_answer, DecisionChoiceAnswer):
        allowed_targets = {"latest"}
        if target_message_ids is None:
            allowed_targets.update(target_answer.probabilities)
            allowed_targets.add(target_answer.choice)
        else:
            allowed_targets.update(target_message_ids)
        chosen_target, target_raw = _choice(
            target_answer,
            threshold=0.0,
            allowed=allowed_targets,
            default="latest",
            temperature=settings.choice_temperature,
        )
        raw["target_message_id"] = target_raw
        if (
            chosen_target
            and chosen_target != "latest"
            and (target_message_ids is None or chosen_target in target_message_ids)
        ):
            target = chosen_target
    return DecisionResult(
        action=action,  # type: ignore[arg-type]
        target_message_id=target,
        length=length,
        use_meme=bool(use_meme and meme_id),
        meme_id=meme_id if use_meme else None,
        scene=scene,
        raw=raw,
        custom_answers=custom_answers,
        session_hints=session_hints,
    )


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
    try:
        answers = await context.decision.ask(
            questions,
            state=state,
            **_provider_kwargs(settings),
        )
        result = _parse_answers(
            dict(answers),
            scene=scene,
            settings=settings,
            memes=memes,
            target_message_ids={
                item.message_id
                for item in messages[-8:]
                if item.role == "user" and item.message_id
            },
        )
    except DecisionError as exc:
        logger.warning("判断提供商调用失败：%s", exc)
        result = DecisionResult(
            action="none" if scene != "private_passive" else "reply",
            use_meme=False,
            meme_id=None,
            scene=scene,
            error=str(exc),
            degraded=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("判断过程出现未预期异常")
        result = DecisionResult(
            action="none" if scene != "private_passive" else "reply",
            scene=scene,
            error=str(exc),
            degraded=True,
        )
    result.duration_ms = int((time.monotonic() - started) * 1000)
    result.dry_run = settings.dry_run
    return result
