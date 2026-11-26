"""通过模型判断或按权重抽取，生成业务决策。"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
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

from .candidate_sources import (
    candidate_type,
    catalog_name,
    example_catalogs,
    meme_catalogs,
    source_question,
)
from .config_util import PluginSettings
from .models import (
    BufferedMessage,
    DecisionOutcome,
    DecisionResult,
    SceneType,
    ToolCall,
    decision_log_context,
)
from .workflow_actions import ACTIONS, VOICE_TOOLS, migrate_actions

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
    candidate_source: str
    default: str | bool | None = None
    threshold: float = 0.0
    temperature: float = 0.0
    allowed: frozenset[str] = frozenset()
    option_weights: dict[str, float] = field(default_factory=dict)
    actions: dict[str, list[dict]] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    is_root: bool = True
    parents: list[tuple[str, str, bool]] = field(default_factory=list)
    target_texts: dict[str, str] = field(default_factory=dict)
    candidate_descriptions: dict[str, str] = field(default_factory=dict)
    candidate_items: dict[str, dict] = field(default_factory=dict)
    source_question: str = ""
    choice_options: list[tuple] = field(default_factory=list)
    instructions: str = ""
    eligible: bool = True
    unavailable_reason: str = ""
    stage: str = "pre_reply"
    decision_mode: Literal["model", "random"] = "model"


@dataclass
class PostDecisionPlan:
    specs: list[DecisionSpec]
    state: dict[str, Any] | None
    provider_id: str
    inputs: dict = field(default_factory=dict)


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
    ordered = [spec for spec in specs if spec.candidate_source != "recent_messages"]
    ordered.extend(spec for spec in specs if spec.candidate_source == "recent_messages")
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
        if key not in spec.aliases
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


def build_state(
    *,
    scene: SceneType,
    is_private: bool,
    messages: list[BufferedMessage],
    at_or_quoted: bool,
    memes: list[dict[str, str]],
    custom_context: str = "",
    clock=None,
) -> dict[str, Any]:
    """只提供精简上下文，不包含人设、Skill 或官方完整注入。"""
    state: dict[str, Any] = {
        "scene": scene,
        "chat_type": "private" if is_private else "group",
        "current_message_at_or_quoted_bot": at_or_quoted,
        "recent_messages": [item.to_state_dict() for item in messages],
    }
    if clock is not None:
        for record, message in zip(state["recent_messages"], messages, strict=True):
            if message.role == "user":
                record["sent_at"] = _sent_time_label(message.sent_at, clock)
    context = custom_context.strip()
    if context:
        state["custom_context"] = context
    return state


def has_on_match_actions(
    configured,
    scene: SceneType,
    *,
    trigger_event: str = "",
    timer_roots: set[str] | None = None,
    candidate_available: dict[str, bool] | None = None,
) -> bool:
    """判断本轮可达的问题是否可能产生判断后立即执行的发图动作。"""
    configured, _ = migrate_actions(configured)
    event = trigger_event or (
        "silence_elapsed" if scene.endswith("_proactive") else "message_received"
    )
    by_id = {question.id: question for question in configured}
    enabled = {
        question.id: question
        for question in configured
        if question.enabled
        and question.stage == "pre_reply"
        and (not question.scenes or scene in question.scenes)
        and (
            candidate_available is None
            or (
                not question.requires_candidates
                or candidate_available.get(question.requires_candidates, False)
            )
        )
        and (
            candidate_available is None
            or catalog_name(question.candidate_source) == "options"
            or candidate_available.get(candidate_type(question, by_id), False)
        )
    }
    reachable = set()
    changed = True
    while changed:
        changed = False
        for question in enabled.values():
            if question.id in reachable:
                continue
            parent_id = source_question(question.candidate_source)
            if parent_id and parent_id not in reachable:
                continue
            if any(trigger.get("question") in reachable or (
                trigger.get("event") == event
                and (timer_roots is None or event != "timer_elapsed" or question.id in timer_roots)
            ) for trigger in question.triggers or []):
                reachable.add(question.id)
                changed = True
    return any(
        question.id in reachable
        and any(
            action.get("tool") == "send_meme"
            and action.get("when", "on_match") == "on_match"
            for actions in (question.actions or {}).values()
            for action in actions
        )
        for question in enabled.values()
    )


def _round_clock(context, umo):
    get_config = getattr(context, "get_config", None)
    zone = None
    try:
        name = get_config(umo).get("timezone") if umo and callable(get_config) else None
        if isinstance(name, str) and name.strip():
            name = name.strip()
            zone = timezone.utc if name == "UTC" else ZoneInfo(name)
    except Exception as exc:
        logger.warning("判断题无法读取会话时区，将使用系统时区：%s", exc)
    return time.time(), zone


def _sent_time_label(sent_at, clock):
    now, zone = clock
    if sent_at is None or not 0 < sent_at <= now:
        return "发送时间未知"
    instant = datetime.fromtimestamp(sent_at, timezone.utc).astimezone(zone)
    seconds = int(now - sent_at)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        age = f"{hours} 小时 {minutes} 分钟"
    elif minutes:
        age = f"{minutes} 分 {seconds} 秒"
    else:
        age = f"{seconds} 秒"
    return f"{instant.isoformat(sep=' ', timespec='seconds')}（{age}前）"


def _question_info(context, umo, scene, last_activity, clock=None):
    now, zone = clock or _round_clock(context, umo)
    instant = datetime.fromtimestamp(now, timezone.utc).astimezone(zone)
    silence = "不适用（本轮收到消息）"
    if scene.endswith("_proactive"):
        silence = "未知"
        if last_activity and last_activity > 0:
            minutes, seconds = divmod(max(0, int(now - last_activity)), 60)
            silence = f"{minutes} 分 {seconds} 秒"
    return {_TIME_TOKEN: instant.isoformat(sep=" ", timespec="seconds"), _SILENCE_TOKEN: silence}


def _build_specs(configured, scene, messages, memes, settings, *, targets=None,
                 context=None, umo="", last_activity=None, stage="pre_reply", clock=None,
                 trigger_event="", timer_roots=None, catalogs=None, unavailable_tools=frozenset()):
    specs = []
    info = None
    event = trigger_event or ("silence_elapsed" if scene.endswith("_proactive") else "message_received")
    candidates = {m.message_id: m for m in messages[-8:] if m.role == "user" and m.message_id}
    configured, _ = migrate_actions(configured)
    if unavailable_tools:
        # 对应插件不可用时，用到它动作的题视同停用，下游题也不再触发。
        configured = [replace(q, enabled=False) if q.enabled and any(
            action.get("tool") in unavailable_tools for actions in (q.actions or {}).values() for action in actions
        ) else q for q in configured]
    catalogs = deepcopy(catalogs) if catalogs is not None else meme_catalogs(memes)
    available = {key: bool(items) for key, items in catalogs.items()}
    available["recent_messages"] = bool(candidates if targets is None else targets)
    by_id = {question.id: question for question in configured}
    successors = {}
    for child in configured:
        if not child.enabled or (child.scenes and scene not in child.scenes) or (
            child.requires_candidates and not available.get(child.requires_candidates, False)
        ) or (
            catalog_name(child.candidate_source) != "options"
            and not source_question(child.candidate_source)
            and not available.get(catalog_name(child.candidate_source), False)
        ):
            continue
        for trigger in child.triggers:
            if "question" in trigger:
                successors.setdefault((trigger["question"], trigger["option"]), []).append(child.id)
    for item in configured:
        if not item.enabled or (stage is not None and item.stage != stage):
            continue
        in_scene = not item.scenes or scene in item.scenes
        parents = [(t["question"], t["option"], t.get("ask_with_parent", False))
                   for t in item.triggers if "question" in t and in_scene]
        root_event = "reply_generated" if item.stage == "post_reply" else event
        root = in_scene and any(t.get("event") == root_event for t in item.triggers)
        if root_event == "timer_elapsed" and timer_roots is not None:
            root = root and item.id in timer_roots
        if not in_scene and all("event" in t for t in item.triggers):
            continue
        fixed = item.private_default if scene == "private_passive" else ""
        default = fixed or item.default_option or (False if item.type == "boolean" and item.use_threshold else None)
        if item.type == "boolean" and isinstance(default, str):
            default = default == "true"
        eligible = in_scene and (not item.requires_candidates or available.get(item.requires_candidates, False))
        spec = DecisionSpec(
            id=item.id, kind="fixed" if fixed and eligible else item.type, candidate_source=candidate_type(item, by_id),
            question=None, default=default, actions=deepcopy(item.actions), aliases=item.aliases.copy(),
            is_root=root, parents=parents, stage=item.stage,
            source_question=source_question(item.candidate_source), eligible=eligible,
            decision_mode=item.decision_mode or settings.decision_mode,
            threshold=settings.choice_threshold if item.use_threshold else (0.5 if item.type == "boolean" else 0.0),
            temperature=0.0 if item.type == "boolean" and item.use_threshold else settings.choice_temperature,
        )
        specs.append(spec)
        if fixed or not eligible:
            continue

        def effect(key, item=item, spec=spec):
            parts = []
            for action in item.actions.get(key, ()):
                name = action["tool"]
                source = action.get("from", item.id)
                if name == "add_context":
                    if "text" in action:
                        parts.append("会话提示：" + action["text"])
                    elif source == item.id and spec.candidate_source == "recent_messages" and key == "*":
                        parts.append("将选中消息的发送人和原文交给会话模型，同时提供发送时间")
                    else:
                        parts.append(f"将 {source} 的答案交给会话模型")
                elif name == "quote_message":
                    mode = {"none": "无引用", "first": "仅首段引用", "all": "全段引用"}[settings.target_quote_mode]
                    parts.append(f"引用 {source} 选中的消息（{mode}）")
                elif name == "send_meme":
                    when = "正文发送成功后" if action.get("when", "on_match") == "after_send" else "命中后"
                    parts.append(f"{when}发送 {source} 选中的表情")
                elif name == "voice_tone":
                    parts.append(f"回复转成语音时用「{action['text']}」的语气朗读")
                elif name == "voice_reply":
                    parts.append("正文转成语音发送")
                elif name == "text_reply":
                    parts.append("正文只发文字，不转语音")
                else:
                    parts.append(ACTIONS[name]["label"])
            if children := successors.get((item.id, key)):
                parts.append("触发判断：" + "、".join(children))
            return "；".join(parts) or "记录答案"

        prompt = item.instructions
        if spec.decision_mode == "model" and (_TIME_TOKEN in prompt or _SILENCE_TOKEN in prompt):
            if info is None:
                clock = clock or _round_clock(context, umo)
                info = _question_info(context, umo, scene, last_activity, clock)
            for token, value in info.items():
                prompt = prompt.replace(token, value)
        if any(early for _, _, early in parents):
            prompt += "\n假设本题的上游条件已满足，请根据本轮信息独立判断。"
        if item.type == "boolean":
            prompt += "\n是 → " + effect("true")
            prompt += "\n否 → " + effect("false")
            spec.question = DecisionBooleanQuestion(prompt)
            spec.allowed = frozenset({"true", "false"})
            spec.option_weights = {"true": item.true_weight, "false": item.false_weight}
            continue
        spec.instructions = prompt
        spec.choice_options = [
            (option.id, option.criteria, option.weight, effect(option.id))
            for option in item.options
        ]
        items = catalogs.get(catalog_name(item.candidate_source), [])
        if catalog_name(item.candidate_source) == "recent_messages":
            if targets is not None:
                spec.target_texts = dict(targets)
                items = [{"id": key, "description": value} for key, value in targets.items()]
            else:
                clock = clock or _round_clock(context, umo)
                items = []
                for mid, msg in candidates.items():
                    header = f"回复 {msg.sender_name or msg.sender_id} · {_sent_time_label(msg.sent_at, clock)}："
                    spec.target_texts[mid] = header + (msg.text or "（无文本）")
                    items.append({"id": mid, "description": header + (msg.text[:40] or "（无文本）")})
        if not spec.source_question:
            _set_candidates(spec, items)
    return specs


def _set_candidates(spec, items):
    reserved = {key for key, _, _, _ in spec.choice_options} | set(spec.aliases)
    spec.candidate_items = {item["id"]: item for item in items if item["id"] not in reserved}
    spec.candidate_descriptions = {key: item["description"] for key, item in spec.candidate_items.items()}
    spec.question = None
    spec.allowed = frozenset()
    spec.option_weights = {}
    if not spec.eligible or (spec.candidate_source != "options" and not spec.candidate_items):
        return
    criteria = {}
    for key, description, weight, effect in spec.choice_options:
        expanded = spec.candidate_source != "options" and key == "*"
        entries = spec.candidate_descriptions if expanded else {key: description}
        for option_id, label in entries.items():
            if expanded:
                label = description.replace("{描述}", label) if "{描述}" in description else f"{description}：{label}"
            criteria[option_id] = f"{label}。选中后：{effect}"
            spec.option_weights[option_id] = weight
    if len(criteria) >= 2:
        spec.question = DecisionChoiceQuestion(instructions=spec.instructions, criteria=criteria)
        spec.allowed = frozenset(criteria)
        for alias, canonical in spec.aliases.items():
            if canonical in criteria and alias not in criteria:
                spec.allowed |= {alias}
                spec.option_weights[alias] = spec.option_weights[canonical]


def _resolve_candidate_source(spec, inputs):
    source, value = inputs.get(spec.source_question, (None, None))
    chosen = source.candidate_items.get(value) if source is not None and isinstance(value, str) else None
    children = chosen.get("children", []) if chosen else []
    if spec.kind != "fixed":
        _set_candidates(spec, children)
    if not children or (spec.kind != "fixed" and spec.question is None):
        spec.unavailable_reason = "no_child_candidates" if chosen else "candidate_source_unavailable"


def _option_key(spec, outcome):
    if not outcome.has_answer:
        return None
    if isinstance(outcome.value, bool):
        return "true" if outcome.value else "false"
    if outcome.value in spec.candidate_descriptions:
        return "*"
    return spec.aliases.get(outcome.value, outcome.value)


def _preview_outcome(spec, choices):
    possible = [key for key, weight in spec.option_weights.items() if weight > 0 and key not in spec.aliases]
    key = choices.get(spec.id)
    if key == "*" and spec.candidate_descriptions:
        key = next(iter(spec.candidate_descriptions))
    if not possible:
        return DecisionOutcome(spec.default, status="fallback", reason="no_selectable_option")
    if key not in possible:
        key = possible[0]
    return DecisionOutcome(key == "true" if spec.kind == "boolean" else key)


def _workflow(specs, outcomes, batches, *, assumed=None, prior_inputs=None):
    """先结算已激活的答案。同批预问只在自己的来源命中时使用。"""
    selected = {}
    remaining = list(specs)
    while remaining:
        inputs = {**(prior_inputs or {}), **_action_inputs(specs, outcomes)}
        waiting = {s.id for s in remaining}
        ready = [s for s in remaining
                 if (s.is_root or any(selected.get(parent) == option for parent, option, _ in s.parents))
                 and (not s.source_question or s.source_question not in waiting)]
        if not ready:
            break
        for spec in ready:
            if spec.source_question:
                _resolve_candidate_source(spec, inputs)
        local = [s for s in ready if s.question is None or s.decision_mode == "random"]
        if local:
            for spec in local:
                if spec.question is None:
                    outcome = _evaluate_answer(spec, _MISSING)
                elif assumed is not None:
                    outcome = _preview_outcome(spec, assumed)
                else:
                    outcome = _evaluate_random(spec)
                outcomes[spec.id] = outcome
                if spec.unavailable_reason:
                    outcome = outcomes[spec.id] = DecisionOutcome(None, status="skipped", reason=spec.unavailable_reason)
                selected[spec.id] = _option_key(spec, outcome)
                remaining.remove(spec)
            continue
        required = {s.id for s in ready}
        included = set(required)
        while True:
            more = {s.id for s in remaining if not s.source_question and s.question is not None and s.decision_mode == "model"
                    and any(early and parent in included for parent, _, early in s.parents)}
            if more <= included:
                break
            included.update(more)
        batch = [s for s in remaining if s.id in included]
        record = {"asked": [s.id for s in batch], "used": [], "discarded": []}
        if assumed is not None:
            record["candidates"] = {s.id: dict(s.candidate_descriptions) for s in batch if s.candidate_source != "options"}
        batches.append(record)
        answers = yield batch
        pending = list(batch)
        while pending:
            layer = [s for s in pending if s.id in required or any(
                early and parent in included and selected.get(parent) == option
                for parent, option, early in s.parents)]
            if not layer:
                break
            evaluated = {s.id: _preview_outcome(s, assumed) for s in layer} if assumed is not None else _evaluate_answers(layer, answers)
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


async def _run_workflow(context, specs, outcomes, batches, state, provider_id, prior_inputs=None):
    workflow = _workflow(specs, outcomes, batches, prior_inputs=prior_inputs)
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
        spec.is_root = spec.is_root or any(selected.get(parent) == option for parent, option, _ in spec.parents)
    return any(s.is_root for s in post_specs)


def preview_workflow(settings, scene, choices, *, trigger_event="", voice_available=True):
    """以示例候选和用户指定的有效答案预览，不调用提供商或工具。

    voice_available 为假时与实际运行一致，跳过用到语音动作的题。
    """
    message = BufferedMessage("sample_message", "user", "示例用户", "user", "示例消息")
    specs = _build_specs(settings.decision_questions, scene, [message], [], settings,
                         stage=None, trigger_event=trigger_event, catalogs=example_catalogs(),
                         unavailable_tools=frozenset() if voice_available else VOICE_TOOLS)
    batches, outcomes = [], {}
    before = [s for s in specs if s.stage == "pre_reply"]
    after = [s for s in specs if s.stage == "post_reply"]
    plan = DecisionResult(scene=scene)
    for stage, group in (("pre_reply", before), ("post_reply", after)):
        if stage == "post_reply" and (not plan.should_speak() or not _activate_post(before, outcomes, after)):
            break
        start = len(batches)
        for _ in _workflow(group, outcomes, batches, assumed=choices, prior_inputs=_action_inputs(before, outcomes) if stage == "post_reply" else None):
            pass
        for record in batches[start:]:
            record["stage"] = stage
        if stage == "pre_reply":
            plan = _assemble_result(before, dict(outcomes), scene=scene)
        else:
            _apply_actions(after, outcomes, plan, _action_inputs(before, outcomes))
    return {"batches": batches,
            "skipped": {key: {"no_child_candidates": "选中项没有下级候选", "candidate_source_unavailable": "候选来源没有有效选择"}.get(outcome.reason, "触发条件未命中或本轮无候选")
                        for key, outcome in outcomes.items() if not outcome.has_answer},
            "answers": {key: o.value for key, o in outcomes.items() if o.has_answer},
            "reply": plan.action, "reply_sources": plan.reply_sources, "quote": plan.target_message_id,
            "context": {"message": plan.target_summary, "answers": plan.custom_answers,
                        "fields": plan.context_fields, "hints": plan.session_hints},
            "tools": [dict(name=c.name, when=c.when, args=c.args, sources=c.sources,
                           status=c.status, reason=c.reason) for c in plan.tool_calls.values()]}


def _result_mode(specs, fallback):
    modes = {spec.decision_mode for spec in specs if spec.question is not None}
    if len(modes) == 1:
        return next(iter(modes))
    if len(modes) > 1:
        return "mixed"
    return fallback


async def evaluate_post_reply(context, plan: PostDecisionPlan, text: str, previous_answers: dict, *, decision=None):
    """使用生成前的快照评估回复，保留失败批次之前已经得到的答案。"""
    state = plan.state
    if state is not None:
        state = {**state, "stage": "post_reply", "assistant_reply": text,
                 "previous_answers": previous_answers}
    outcomes = {}
    report = {"batches": []}
    try:
        await _run_workflow(context, plan.specs, outcomes, report["batches"], state, plan.provider_id, plan.inputs)
    except Exception as exc:
        report["error"] = str(exc) or type(exc).__name__
        logger.warning("输出后判断失败：%s", report["error"])
    if decision is not None and "error" not in report:
        _apply_actions(plan.specs, outcomes, decision, plan.inputs)
    report["answers"] = {key: o.value for key, o in outcomes.items() if o.has_answer}
    report["outcomes"] = {
        key: {"status": o.status, "reason": o.reason} for key, o in outcomes.items()
        if not o.has_answer
    }
    report["raw"] = {key: o.raw for key, o in outcomes.items() if o.raw}
    return report


def _action_inputs(specs, outcomes):
    """动作只解析本轮实际采纳的答案和提问时的候选快照。"""
    return {s.id: (s, o.value) for s in specs if (o := outcomes.get(s.id)) and o.has_answer}


def _apply_actions(specs, outcomes, result, prior_inputs=None):
    inputs = {**(prior_inputs or {}), **_action_inputs(specs, outcomes)}
    for spec in specs:
        outcome = outcomes.get(spec.id)
        if not outcome or not outcome.has_answer:
            continue
        key = _option_key(spec, outcome)
        for action in spec.actions.get(key, ()):
            tool = action["tool"]
            source_id = action.get("from", spec.id)
            source, value = inputs.get(source_id, (None, None))
            dynamic = source is not None and value in source.candidate_descriptions
            if tool == "request_reply":
                result.action = "proactive" if result.scene.endswith("_proactive") else "reply"
                result.reply_sources.append(f"{spec.id}={key}")
            elif tool == "quote_message":
                if dynamic:
                    result.target_message_id = value
            elif tool == "voice_tone":
                result.voice_tone = action["text"].strip()
            elif tool in ("voice_reply", "text_reply"):
                result.voice_mode = "voice" if tool == "voice_reply" else "text"
            elif tool == "add_context":
                if "text" not in action and source is None:
                    continue
                is_message = dynamic and source.candidate_source == "recent_messages"
                content = action.get("text", value)
                if dynamic and "text" not in action:
                    content = source.target_texts[value] if is_message else source.candidate_descriptions[value]
                if "label" in action:
                    result.context_fields[action["label"]] = content
                elif "text" in action:
                    result.session_hints.append(content)
                elif is_message and not result.target_summary:
                    result.target_summary = content
                else:
                    result.custom_answers[source_id] = content
            elif tool == "send_meme":
                when = action.get("when", "on_match")
                args = {"meme_id": value} if dynamic else {}
                call = next((c for c in result.tool_calls.values() if c.name == tool
                             and c.when == when and c.args == args), None)
                if call is None:
                    call = ToolCall(tool, args=args, when=when)
                    call_id = tool if tool not in result.tool_calls else f"{tool}/{len(result.tool_calls)}"
                    result.tool_calls[call_id] = call
                call.sources.append(f"{spec.id}={str(outcome.value).lower() if isinstance(outcome.value, bool) else outcome.value}")
                if not dynamic:
                    call.status, call.reason = "skipped", "缺少本轮所需答案"
                elif when == "after_send":
                    result.use_meme, result.meme_id = True, value
                    result.meme_description = source.candidate_descriptions[value]
    # 兼容旧日志列；请求正文仅依赖显式绑定的上下文。
    result.length = {"简短": "short", "详细": "long"}.get(result.context_fields.get("篇幅"))


def _assemble_result(specs, outcomes, *, scene):
    result = DecisionResult(scene=scene, outcomes=outcomes,
                            raw={key: o.raw for key, o in outcomes.items() if o.raw})
    _apply_actions(specs, outcomes, result)
    return result


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
    trigger_event: str = "",
    timer_roots: set[str] | None = None,
    evaluation_time: float | None = None,
    voice_available: bool = False,
) -> DecisionResult:
    """按当前模式评估本轮问题。失败时返回带 error 的结果。

    voice_available 为假时（未对接通用 TTS 或主动发言不转语音），跳过用到语音动作的题。
    """
    started = time.monotonic()
    quote_mode = settings.target_quote_mode
    specs = []
    outcomes = {}
    batches = []
    state = None
    now, zone = _round_clock(context, umo)
    round_clock = (evaluation_time if evaluation_time is not None else now, zone)
    try:
        clock = round_clock if scene == "group_passive" else None
        all_specs = _build_specs(
            settings.decision_questions, scene, messages, memes, settings,
            context=context, umo=umo, last_activity=last_activity, stage=None, clock=round_clock,
            trigger_event=trigger_event, timer_roots=timer_roots,
            unavailable_tools=frozenset() if voice_available else VOICE_TOOLS,
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
                clock=clock,
            )
            if trigger_event == "timer_elapsed":
                state["trigger_event"] = trigger_event
        provider_id = settings.decision_provider_id
        await _run_workflow(context, specs, outcomes, batches, state, provider_id)
        result = _assemble_result(specs, outcomes, scene=scene)
        if post_specs and result.should_speak() and not settings.dry_run:
            if _activate_post(specs, outcomes, post_specs):
                result.post_plan = PostDecisionPlan(post_specs, state, provider_id, _action_inputs(specs, outcomes))
    except Exception as exc:  # noqa: BLE001
        error = str(exc) or type(exc).__name__
        if isinstance(exc, DecisionError):
            logger.warning("判断提供商调用失败：%s", error)
        else:
            logger.exception("判断过程出现未预期异常")
        allowed = any(action["tool"] == "request_reply" for s in specs if (o := outcomes.get(s.id)) and o.has_answer
                      for action in s.actions.get(_option_key(s, o), ()))
        result = DecisionResult(
            action="reply" if scene == "private_passive" and allowed else "none",
            scene=scene,
            error=error,
            degraded=True,
            outcomes=outcomes,
        )
    result.batches = batches
    result.trigger_event = trigger_event or ("silence_elapsed" if scene.endswith("_proactive") else "message_received")
    result.duration_ms = int((time.monotonic() - started) * 1000)
    result.dry_run = settings.dry_run
    result.target_quote_mode = quote_mode
    result.decision_mode = _result_mode(
        all_specs if "all_specs" in locals() else specs, settings.decision_mode
    )
    if settings.decision_log_include_content:
        result.log_context = decision_log_context(messages, evaluated_at=round_clock[0], at_or_quoted=at_or_quoted,
                                                  last_activity=last_activity, timer_roots=timer_roots)
    return result
