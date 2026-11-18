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

from .config_util import PluginSettings, default_questions
from .decision_tools import TOOLS
from .models import (
    BufferedMessage,
    DecisionOutcome,
    DecisionResult,
    SceneType,
    ToolCall,
    normalize_action,
)

Question = DecisionBooleanQuestion | DecisionChoiceQuestion

_MISSING = object()


@dataclass
class DecisionSpec:
    """固定本轮题目、选择规则及结果用途；question=None 表示本轮无需提问。"""

    id: str
    kind: Literal["choice", "boolean", "fixed"]
    question: Question | None
    binding: str
    default: str | bool | None = None
    threshold: float = 0.0
    temperature: float = 0.0
    allowed: frozenset[str] = frozenset()
    option_weights: dict[str, float] = field(default_factory=dict)
    session_hints: dict[str | bool, str] = field(default_factory=dict)
    tools: dict[str | bool, str] = field(default_factory=dict)


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
    ordered = [spec for spec in specs if spec.binding != "target"]
    ordered.extend(spec for spec in specs if spec.binding == "target")
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


def _build_specs(configured, scene, messages, memes, settings, *, targets=None):
    specs = []
    for item in configured:
        if not item.enabled:
            continue
        role = item.role
        prompt = item.instructions
        fixed = role == "action" and scene == "private_passive" and not item.ask_private
        default = {
            "action": "reply" if fixed else "none",
            "target": "latest",
            "meme": "none",
        }.get(role)
        if item.type == "boolean":
            default = False if item.use_threshold else None
        spec = DecisionSpec(
            id=item.id,
            kind="fixed" if fixed else item.type,
            binding=role,
            question=None,
            default=default,
            threshold=settings.choice_threshold
            if item.use_threshold
            else (0.5 if item.type == "boolean" else 0.0),
            temperature=0.0
            if item.type == "boolean" and item.use_threshold
            else settings.choice_temperature,
        )
        specs.append(spec)
        if fixed:
            reply = next(o for o in item.options if o.id == "reply")
            spec.session_hints["reply"] = reply.session_hint
            spec.tools["reply"] = reply.tool
        if fixed or (role == "target" and scene != "group_passive"):
            continue
        if item.type == "boolean":
            spec.question = DecisionBooleanQuestion(prompt)
            spec.allowed = frozenset({"true", "false"})
            spec.option_weights = {"true": item.true_weight, "false": item.false_weight}
            spec.session_hints = {
                True: item.true_session_hint,
                False: item.false_session_hint,
            }
            spec.tools = {True: item.true_tool, False: item.false_tool}
            continue
        dynamic = {}
        if role == "target":
            dynamic = (
                targets
                if targets is not None
                else {
                    m.message_id: f"回复 {m.sender_name or m.sender_id} 的消息：{m.text[:40] or '（无文本）'}"
                    for m in messages[-8:]
                    if m.message_id and m.role == "user"
                }
            )
        elif role == "meme":
            dynamic = {m["id"]: m["description"] for m in memes}
        proactive = scene.endswith("_proactive")
        if role == "action" and proactive:
            prompt = item.proactive_instructions or prompt
        criteria = {}
        for opt in item.options:
            if role == "action" and opt.id == ("reply" if proactive else "proactive"):
                continue
            expanded = role in {"target", "meme"} and opt.id == "*"
            entries = dynamic if expanded else {opt.id: opt.criteria}
            for key, label in entries.items():
                if expanded and key in {default, "*"}:
                    continue
                if expanded and "{描述}" in opt.criteria:
                    label = opt.criteria.replace("{描述}", label)
                elif expanded:
                    label = f"{opt.criteria}：{label}"
                criteria[key] = label
                if (
                    role == "action"
                    and key == "none"
                    and proactive
                    and item.proactive_none_criteria
                ):
                    criteria[key] = item.proactive_none_criteria
                spec.option_weights[key] = opt.weight
                spec.session_hints[key] = opt.session_hint
                spec.tools[key] = opt.tool
        if len(criteria) >= 2:
            spec.question = DecisionChoiceQuestion(
                instructions=prompt, criteria=criteria
            )
            spec.allowed = frozenset(criteria)
            if role == "action" and "reply" in criteria:
                spec.allowed |= {"join"}
                spec.option_weights["join"] = spec.option_weights["reply"]
    return specs


def _build_questions(scene, messages, memes, configured=None):
    settings = PluginSettings({})
    specs = _build_specs(
        default_questions() if configured is None else configured,
        scene,
        messages,
        memes,
        settings,
    )
    return {s.id: s.question for s in specs if s.question is not None}


def _assemble_result(specs, outcomes, *, scene):
    result = DecisionResult(
        scene=scene,
        outcomes=outcomes,
        action="reply" if scene == "private_passive" else "none",
    )
    role_answers = {}
    for spec in specs:
        outcome = outcomes[spec.id]
        value = outcome.value
        if outcome.raw:
            result.raw[spec.id] = outcome.raw
        if spec.binding == "action":
            result.action = normalize_action(value)
        elif spec.binding == "target" and outcome.status == "ok" and value != "latest":
            result.target_message_id = value
        elif spec.binding == "meme" and outcome.status == "ok" and value != "none":
            role_answers["meme"] = value
        if outcome.status != "ok" and spec.kind != "fixed":
            continue
        if spec.binding == "answer":
            result.custom_answers[spec.id] = value
        if spec.id == "length" and value in {"short", "long"}:
            result.length = value
        effect_key = normalize_action(value) if spec.binding == "action" else value
        if hint := spec.session_hints.get(effect_key):
            result.session_hints.append(hint)
        if name := spec.tools.get(effect_key):
            call = result.tool_calls.setdefault(name, ToolCall(name))
            call.sources.append(
                f"{spec.id}={str(value).lower() if isinstance(value, bool) else value}"
            )
    for call in result.tool_calls.values():
        _, resolve_args, _ = TOOLS[call.name]
        args = resolve_args(role_answers)
        if args is None:
            call.status, call.reason = "skipped", "缺少本轮所需答案"
        else:
            call.args = args
    meme_call = result.tool_calls.get("send_meme")
    if meme_call and meme_call.args:
        result.use_meme, result.meme_id = True, meme_call.args["meme_id"]
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
    targets = target_message_ids
    if targets is None:
        target = next(
            (q for q in settings.decision_questions if q.role == "target"), None
        )
        answer = answers.get(target.id) if target else None
        if isinstance(answer, DecisionChoiceAnswer):
            targets = set(answer.probabilities) | {answer.choice}
    specs = _build_specs(
        settings.decision_questions,
        scene,
        [],
        memes,
        settings,
        targets={k: k for k in targets or ()},
    )
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
    specs = _build_specs(settings.decision_questions, scene, messages, memes, settings)
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
