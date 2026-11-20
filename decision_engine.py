"""通过模型判断或按权重抽取，生成业务决策。"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.provider import (
    DecisionBooleanAnswer,
    DecisionBooleanQuestion,
    DecisionChoiceAnswer,
    DecisionChoiceQuestion,
    DecisionError,
)

from .config_util import OPTION_EFFECTS, SPEAK_OPTION, PluginSettings, default_questions
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
_TIME_TOKEN = "{当前时间}"
_SILENCE_TOKEN = "{冷场时长}"


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
    is_root: bool = True
    parents: list[tuple[str, str, bool]] = field(default_factory=list)
    target_texts: dict[str, str] = field(default_factory=dict)
    candidate_descriptions: dict[str, str] = field(default_factory=dict)
    stage: str = "pre_reply"
    decision_mode: Literal["model", "random"] = "model"


@dataclass
class PostDecisionPlan:
    specs: list[DecisionSpec]
    state: dict[str, Any] | None
    provider_id: str


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


def _evaluate_random(spec: DecisionSpec) -> DecisionOutcome:
    if spec.question is None:
        return _evaluate_answer(spec, _MISSING)
    weights = {
        key: weight
        for key, weight in spec.option_weights.items()
        if spec.binding != "action" or key != "join"
    }
    probabilities = dict.fromkeys(weights, 1.0 / len(weights))
    total = sum(weights.values())
    distribution = {key: weight / total for key, weight in weights.items()} if total else {}
    eligible = {key: chance for key, chance in distribution.items() if chance > 0}
    selected = (
        random.choices(list(eligible), weights=list(eligible.values()), k=1)[0]
        if eligible
        else None
    )
    outcome = DecisionOutcome(
        value=spec.default,
        probabilities=probabilities,
        selection_probabilities=distribution,
        raw={
            "decision_mode": "random",
            "question_type": spec.kind,
            "probabilities": probabilities,
            "option_weights": weights,
            "selected_choice": selected,
            "selection_probabilities": distribution,
        },
    )
    if selected is None:
        outcome.status, outcome.reason = "fallback", "no_selectable_option"
    else:
        outcome.value = selected == "true" if spec.kind == "boolean" else selected
    return outcome


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
        "meme_catalog": [dict(meme) for meme in memes],
    }
    context = custom_context.strip()
    if context:
        state["custom_context"] = context
    return state


def _question_info(context, umo, scene, last_activity):
    now = time.time()
    get_config = getattr(context, "get_config", None)
    zone = None
    try:
        name = get_config(umo).get("timezone") if umo and callable(get_config) else None
        if isinstance(name, str) and name.strip():
            name = name.strip()
            zone = timezone.utc if name == "UTC" else ZoneInfo(name)
    except Exception as exc:
        logger.warning("判断题无法读取会话时区，将使用系统时区：%s", exc)
    instant = datetime.fromtimestamp(now, timezone.utc).astimezone(zone)
    silence = "不适用（本轮收到消息）"
    if scene.endswith("_proactive"):
        silence = "未知"
        if last_activity and last_activity > 0:
            minutes, seconds = divmod(max(0, int(now - last_activity)), 60)
            silence = f"{minutes} 分 {seconds} 秒"
    return {_TIME_TOKEN: instant.isoformat(sep=" ", timespec="seconds"), _SILENCE_TOKEN: silence}


def _build_specs(configured, scene, messages, memes, settings, *, targets=None,
                 context=None, umo="", last_activity=None, stage="pre_reply"):
    specs = []
    proactive = scene.endswith("_proactive")
    info = None
    event = "silence_elapsed" if proactive else "message_received"
    gate = next((q for q in configured if q.enabled and q.role == "action"
                 and any(t.get("event") == event for t in q.triggers or [])), None)
    successors = {}
    for child in configured:
        if not child.enabled or (child.role == "target" and scene != "group_passive"):
            continue
        for t in child.triggers or []:
            if "question" in t:
                successors.setdefault((t["question"], t["option"]), []).append(child.id)
            elif gate and child.role != "action" and t["event"] == event:
                successors.setdefault((gate.id, SPEAK_OPTION[event]), []).append(child.id)
    for item in configured:
        if not item.enabled or (stage is not None and item.stage != stage):
            continue
        role = item.role
        if role == "action" and item is not gate:
            continue
        parents = []
        for t in item.triggers or []:
            if "question" in t:
                parents.append((t["question"], t["option"], t.get("ask_with_parent", False)))
            elif item is not gate and gate and t["event"] == event:
                parents.append((gate.id, SPEAK_OPTION[event], t.get("ask_with_parent", False)))
        effects = OPTION_EFFECTS["post_reply" if item.stage == "post_reply" else role]

        def effect(key, hint, tool):
            parts = [effects.get(key, effects.get("*", ""))]
            if item.stage != "post_reply":
                if hint:
                    parts.append("会话提示：" + hint)
                if tool:
                    parts.append("正文发送成功后尝试：" + TOOLS[tool][0])
            if children := successors.get((item.id, key)):
                parts.append("触发判断：" + "、".join(children))
            return "；".join(parts)

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
            is_root=item is gate,
            parents=parents,
            stage=item.stage,
            decision_mode=item.decision_mode or settings.decision_mode,
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
        prompt = item.instructions
        if role == "action" and proactive:
            prompt = item.instructions
            for token in (_TIME_TOKEN, _SILENCE_TOKEN):
                if token not in prompt:
                    prompt += f"\n{token[1:-1]}：{token}"
        if spec.decision_mode == "model" and (_TIME_TOKEN in prompt or _SILENCE_TOKEN in prompt):
            if info is None:
                info = _question_info(context, umo, scene, last_activity)
            for token, value in info.items():
                prompt = prompt.replace(token, value)
        if any(early for _, _, early in parents):
            prompt += "\n假设本题的上游条件已满足，请根据本轮信息独立判断。"
        if item.type == "boolean":
            prompt += "\n是 → " + effect("true", item.true_session_hint, item.true_tool)
            prompt += "\n否 → " + effect("false", item.false_session_hint, item.false_tool)
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
            spec.target_texts = dict(targets) if targets is not None else {
                m.message_id: f"回复 {m.sender_name or m.sender_id} 的消息：{m.text or '（无文本）'}"
                for m in messages[-8:] if m.message_id and m.role == "user"
            }
        elif role == "meme":
            dynamic = {m["id"]: m["description"] for m in memes}
        spec.candidate_descriptions = dynamic
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
                criteria[key] = f"{label}。选中后：{effect(opt.id, opt.session_hint, opt.tool)}"
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


def _option_key(spec, outcome):
    if not outcome.has_answer:
        return None
    if spec.kind == "boolean":
        return "true" if outcome.value else "false"
    if spec.binding == "action":
        return normalize_action(outcome.value)
    if spec.binding in {"target", "meme"} and outcome.value != spec.default:
        return "*"
    return outcome.value


def _workflow(specs, outcomes, batches, *, assumed=None):
    """先结算已激活的答案。同批预问只在自己的来源命中时使用。"""
    selected = {}
    remaining = list(specs)
    while remaining:
        ready = [s for s in remaining if s.is_root or
                 any(selected.get(parent) == option for parent, option, _ in s.parents)]
        if not ready:
            break
        local = [s for s in ready if s.question is None or s.decision_mode == "random"]
        if local:
            for spec in local:
                if spec.question is None:
                    outcome = _evaluate_answer(spec, _MISSING)
                elif assumed is not None:
                    outcome = assumed[spec.id]
                else:
                    outcome = _evaluate_random(spec)
                outcomes[spec.id] = outcome
                selected[spec.id] = _option_key(spec, outcome)
                remaining.remove(spec)
            continue
        required = {s.id for s in ready}
        included = set(required)
        while True:
            more = {s.id for s in remaining if s.question is not None and s.decision_mode == "model"
                    and any(early and parent in included for parent, _, early in s.parents)}
            if more <= included:
                break
            included.update(more)
        batch = [s for s in remaining if s.id in included]
        record = {"asked": [s.id for s in batch], "used": [], "discarded": []}
        batches.append(record)
        answers = yield batch
        pending = list(batch)
        while pending:
            layer = [s for s in pending if s.id in required or any(
                early and parent in included and selected.get(parent) == option
                for parent, option, early in s.parents)]
            if not layer:
                break
            evaluated = assumed if assumed is not None else _evaluate_answers(layer, answers)
            for spec in layer:
                outcome = evaluated[spec.id]
                if outcome.raw:
                    outcome.raw.setdefault("decision_mode", "model")
                outcomes[spec.id] = outcome
                selected[spec.id] = _option_key(spec, outcome)
                if outcome.has_answer:
                    record["used"].append(spec.id)
                else:
                    record.setdefault("unanswered", []).append(spec.id)
                remaining.remove(spec)
                pending.remove(spec)
        record["discarded"] = [s.id for s in pending]
    discarded = {key for batch in batches for key in batch["discarded"]}
    for spec in remaining:
        reason = "preasked_discarded" if spec.id in discarded else "branch_not_selected"
        outcomes[spec.id] = DecisionOutcome(None, status="skipped", reason=reason)


async def _run_workflow(context, specs, outcomes, batches, state, provider_id):
    workflow = _workflow(specs, outcomes, batches)
    batch = next(workflow, None)
    while batch:
        previous = dict((state or {}).get("previous_answers", {}))
        previous.update({key: o.value for key, o in outcomes.items() if o.has_answer})
        model_state = {**state, "previous_answers": previous} if previous else state
        record = batches[-1]
        started = time.monotonic()
        try:
            answers = await context.decision.ask(
                {s.id: s.question for s in batch}, state=model_state,
                **({"provider_id": provider_id} if provider_id else {}),
            )
        except Exception as exc:
            record["error"] = str(exc) or type(exc).__name__
            for spec in batch:
                outcomes[spec.id] = DecisionOutcome(None, status="fallback", reason="batch_error")
            raise
        finally:
            record["duration_ms"] = round((time.monotonic() - started) * 1000)
        try:
            batch = workflow.send(answers)
        except StopIteration:
            break


def _activate_post(specs, outcomes, post_specs):
    selected = {s.id: _option_key(s, outcomes[s.id]) for s in specs}
    for spec in post_specs:
        spec.is_root = any(selected.get(parent) == option for parent, option, _ in spec.parents)
    return any(s.is_root for s in post_specs)


def preview_workflow(settings, scene, choices):
    """以示例候选和用户指定的有效答案预览，不调用提供商或工具。"""
    message = BufferedMessage("sample_message", "user", "示例用户", "user", "示例消息")
    specs = _build_specs(settings.decision_questions, scene, [message],
                         [{"id": "sample_meme", "description": "示例表情"}], settings, stage=None)
    assumed = {}
    for spec in specs:
        if spec.question is None:
            continue
        key = choices.get(spec.id)
        if key == "*" and spec.candidate_descriptions:
            key = next(iter(spec.candidate_descriptions))
        possible = [k for k, weight in spec.option_weights.items() if weight > 0 and not (spec.binding == "action" and k == "join")]
        if not possible:
            assumed[spec.id] = DecisionOutcome(spec.default, status="fallback", reason="no_selectable_option")
            continue
        if key not in possible:
            key = possible[0]
        assumed[spec.id] = DecisionOutcome(key == "true" if spec.kind == "boolean" else key)
    batches, outcomes = [], {}
    before = [s for s in specs if s.stage == "pre_reply"]
    after = [s for s in specs if s.stage == "post_reply"]
    for stage, group in (("pre_reply", before), ("post_reply", after)):
        if stage == "post_reply" and not _activate_post(before, outcomes, after):
            break
        start = len(batches)
        for _ in _workflow(group, outcomes, batches, assumed=assumed):
            pass
        for record in batches[start:]:
            record["stage"] = stage
    return {"batches": batches, "answers": {key: o.value for key, o in outcomes.items() if o.has_answer}}


def _result_mode(specs, fallback):
    modes = {spec.decision_mode for spec in specs if spec.question is not None}
    if len(modes) == 1:
        return next(iter(modes))
    if len(modes) > 1:
        return "mixed"
    return fallback


async def evaluate_post_reply(context, plan: PostDecisionPlan, text: str, previous_answers: dict):
    """使用生成前的快照评估回复，保留失败批次之前已经得到的答案。"""
    state = plan.state
    if state is not None:
        state = {**state, "stage": "post_reply", "assistant_reply": text,
                 "previous_answers": previous_answers}
    outcomes = {}
    report = {"batches": []}
    try:
        await _run_workflow(context, plan.specs, outcomes, report["batches"], state, plan.provider_id)
    except Exception as exc:
        report["error"] = str(exc) or type(exc).__name__
        logger.warning("输出后判断失败：%s", report["error"])
    report["answers"] = {key: o.value for key, o in outcomes.items() if o.has_answer}
    report["outcomes"] = {
        key: {"status": o.status, "reason": o.reason} for key, o in outcomes.items()
        if not o.has_answer
    }
    report["raw"] = {key: o.raw for key, o in outcomes.items() if o.raw}
    return report


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
        action="none",
    )
    role_answers = {}
    for spec in specs:
        outcome = outcomes[spec.id]
        value = outcome.value
        if outcome.raw:
            result.raw[spec.id] = outcome.raw
        if not outcome.has_answer:
            continue
        if spec.binding == "action":
            result.action = normalize_action(value)
        elif spec.binding == "target" and outcome.status == "ok" and value != "latest":
            result.target_message_id = value
            result.target_summary = spec.target_texts.get(value, "")
        elif spec.binding == "meme" and outcome.status == "ok" and value != "none":
            role_answers["meme"] = value
            result.meme_description = spec.candidate_descriptions.get(value, "")
        if spec.binding == "action" and value == "none":
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
    outcomes = {}
    workflow = _workflow(specs, outcomes, [])
    batch = next(workflow, None)
    while batch:
        try:
            batch = workflow.send(answers)
        except StopIteration:
            break
    return _assemble_result(specs, outcomes, scene=scene)


async def ask_decision(
    context,
    *,
    settings: PluginSettings,
    scene: SceneType,
    is_private: bool,
    messages: list[BufferedMessage],
    at_or_quoted: bool,
    memes: list[dict[str, str]],
    umo: str = "",
    last_activity: float | None = None,
) -> DecisionResult:
    """按当前模式评估本轮问题。失败时返回带 error 的结果。"""
    started = time.monotonic()
    if settings.decision_questions_warning:
        logger.warning("判断问题配置：%s", settings.decision_questions_warning)
    specs = []
    outcomes = {}
    batches = []
    state = None
    try:
        all_specs = _build_specs(
            settings.decision_questions, scene, messages, memes, settings,
            context=context, umo=umo, last_activity=last_activity, stage=None,
        )
        specs = [s for s in all_specs if s.stage == "pre_reply"]
        post_specs = [s for s in all_specs if s.stage == "post_reply"]
        if any(spec.decision_mode == "model" for spec in all_specs):
            state = build_state(
                scene=scene,
                is_private=is_private,
                messages=messages,
                at_or_quoted=at_or_quoted,
                memes=memes,
                custom_context=settings.decision_custom_context,
            )
        provider_id = settings.decision_provider_id
        await _run_workflow(context, specs, outcomes, batches, state, provider_id)
        result = _assemble_result(specs, outcomes, scene=scene)
        if post_specs and result.should_speak() and not settings.dry_run:
            if _activate_post(specs, outcomes, post_specs):
                result.post_plan = PostDecisionPlan(post_specs, state, provider_id)
    except Exception as exc:  # noqa: BLE001
        error = str(exc) or type(exc).__name__
        if isinstance(exc, DecisionError):
            logger.warning("判断提供商调用失败：%s", error)
        else:
            logger.exception("判断过程出现未预期异常")
        allowed = any(s.binding == "action" and (o := outcomes.get(s.id)) and
                      o.has_answer and normalize_action(o.value) == "reply" for s in specs)
        result = DecisionResult(
            action="reply" if scene == "private_passive" and allowed else "none",
            scene=scene,
            error=error,
            degraded=True,
            outcomes=outcomes,
        )
    result.batches = batches
    result.duration_ms = int((time.monotonic() - started) * 1000)
    result.dry_run = settings.dry_run
    result.decision_mode = _result_mode(
        all_specs if "all_specs" in locals() else specs, settings.decision_mode
    )
    return result
