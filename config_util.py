"""读取并规范化插件配置。"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from copy import deepcopy
from graphlib import CycleError, TopologicalSorter
from typing import Any

from .candidate_sources import migrate_candidate_sources, source_question
from .decision_tools import TOOLS
from .external_plugins import EXTERNAL_PLUGINS
from .models import (
    QUESTION_ROLES,
    DecisionQuestion,
    DecisionQuestionOption,
)
from .workflow_actions import WORKFLOW_EVENTS, migrate_actions, validate_actions

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
GROUP_ACTION_PROMPT = (
    "根据近期群聊判断机器人现在应该怎么做。"
    "reply 表示发言，既包括针对某条消息回复，也包括自然参与讨论或插话；"
    "即使被 @ 或引用，也可以选择 none 不回复。"
)
PROACTIVE_PROMPT = "会话已经沉默一段时间。判断现在是否适合主动开口。"
TARGET_PROMPT = (
    "如果决定发言，可以针对哪一条消息？若不针对某一条特定消息，可选 latest。"
)
USE_MEME_PROMPT = "这次回复是否适合配上一张表情包？没有合适的表情时应为否。"
MEME_PROMPT = (
    "若需要配图，请根据描述选择最合适的一张表情包；不合适则选 none。"
    "只根据描述选择，不要假设你能看到原图。"
)
MEME_CATEGORY_PROMPT = "若需要配图，请先根据当前语境选择最合适的图库分类；没有合适分类则选 none。"
TARGET_DEFAULT = {"latest": "沿用会话上下文，自然参与当前话题"}
MEME_DEFAULT = {"none": "不配表情包"}


DEFAULT_LENGTH_INSTRUCTIONS = "如果发言，回复应该简短还是稍详细？"
DEFAULT_SHORT_CRITERIA = "简短回复，一两句即可"
DEFAULT_SHORT_SESSION_HINT = "本轮回复策略：请给出简短、自然的回复，一两句即可。"
DEFAULT_LONG_CRITERIA = "更完整、稍详细的回复"
DEFAULT_LONG_SESSION_HINT = "本轮回复策略：请给出稍详细、完整的回复，但仍保持自然。"


def default_decision_questions() -> list[DecisionQuestion]:
    """默认的长短判断问题，用词与 1.0 保持一致。"""
    return [
        DecisionQuestion(
            id="length",
            type="choice",
            instructions=DEFAULT_LENGTH_INSTRUCTIONS,
            enabled=True,
            options=[
                DecisionQuestionOption(
                    id="short",
                    criteria=DEFAULT_SHORT_CRITERIA,
                    session_hint=DEFAULT_SHORT_SESSION_HINT,
                ),
                DecisionQuestionOption(
                    id="long",
                    criteria=DEFAULT_LONG_CRITERIA,
                    session_hint=DEFAULT_LONG_SESSION_HINT,
                ),
            ],
        )
    ]


def legacy_questions(legacy=None) -> list[DecisionQuestion]:
    extra = default_decision_questions() if legacy is None else legacy
    action_options = {
        **ACTION_CRITERIA_GROUP,
        "proactive": ACTION_CRITERIA_PROACTIVE["proactive"],
    }
    return [
        DecisionQuestion(
            "action",
            "choice",
            GROUP_ACTION_PROMPT,
            role="action",
            use_threshold=True,
            proactive_instructions=PROACTIVE_PROMPT,
            proactive_none_criteria=ACTION_CRITERIA_PROACTIVE["none"],
            options=[DecisionQuestionOption(k, v) for k, v in action_options.items()],
        ),
        DecisionQuestion(
            "target_message_id",
            "choice",
            TARGET_PROMPT,
            role="target",
            options=[
                DecisionQuestionOption("latest", TARGET_DEFAULT["latest"]),
                DecisionQuestionOption("*", "{描述}"),
            ],
        ),
        *extra,
        DecisionQuestion(
            "use_meme",
            "boolean",
            USE_MEME_PROMPT,
            use_threshold=True,
            true_tool="send_meme",
            branches={"true": "meme_id"} if legacy is None else {},
        ),
        DecisionQuestion(
            "meme_id",
            "choice",
            MEME_PROMPT,
            role="meme",
            use_threshold=True,
            options=[
                DecisionQuestionOption("none", MEME_DEFAULT["none"]),
                DecisionQuestionOption("*", "{描述}"),
            ],
        ),
    ]


EVENTS = {"message_received": "收到消息", "silence_elapsed": "达到冷场条件"}
SPEAK_OPTION = {"message_received": "reply", "silence_elapsed": "proactive"}
OPTION_EFFECTS = {
    "action": {"none": "结束本轮，丢弃提前取得的后续答案", "reply": "允许生成回复，继续处理命中的后续题",
               "proactive": "允许根据近期会话生成冷场发言，继续处理命中的后续题"},
    "target_gate": {"true": "采用目标选择题选出的具体消息", "false": "自然接话，丢弃预选目标"},
    "target": {"latest": "沿用当前话题，不锁定具体消息",
               "*": "将选中消息的发送人和原文交给会话模型，同时提供发送时间；引用方式由群聊设置决定"},
    "meme": {"none": "不选择表情", "*": "选定本轮表情，已安排发送工具时在正文发送成功后尝试发送"},
    "answer": {"*": "记录该答案并交给会话模型"},
    "post_reply": {"*": "将答案记录到本轮回复后判断日志"},
}


def migrate_questions(old):
    """原独立题仍可提前提问，原显式分支继续等待父题。"""
    questions = deepcopy(old)
    by_id = {q.id: q for q in questions}
    links = [(q.id, option, child) for q in questions for option, child in q.branches.items()]
    children = {child for _, _, child in links}
    gates = {}
    warnings = []
    migrated = []
    for q in questions:
        q.branches = {}
        q.triggers = []
        if q.role != "action":
            migrated.append(q)
            continue
        cold = deepcopy(q)
        cold.id = "idle_" + q.id
        while cold.id in by_id:
            cold.id += "_2"
        by_id[cold.id] = cold
        cold.instructions = q.proactive_instructions or q.instructions
        cold.options = [o for o in cold.options if o.id != "reply"]
        for option in cold.options:
            if option.id == "none" and q.proactive_none_criteria:
                option.criteria = q.proactive_none_criteria
        cold.ask_private = False
        q.options = [o for o in q.options if o.id != "proactive"]
        if q.id in children:
            warnings.append(f"旧发言题 {q.id} 有上游条件，已停用，请重新设置入口")
            q.enabled = cold.enabled = False
        for gate, event in ((q, "message_received"), (cold, "silence_elapsed")):
            gate.triggers = [{"event": event}]
            gate.proactive_instructions = gate.proactive_none_criteria = ""
            silent = next(o for o in gate.options if o.id == "none")
            if silent.tool or silent.session_hint:
                warnings.append(f"{gate.id} 的沉默选项已停止执行提示和工具")
                silent.tool = silent.session_hint = ""
        gates[q.id] = cold.id
        migrated.extend((q, cold))
    for parent, option, child in links:
        q = by_id[child]
        if parent in gates and option == "none":
            warnings.append(f"已断开沉默分支 {parent} → {child}，可重新设置生效条件")
            continue
        if q.role != "action":
            source = gates[parent] if parent in gates and option == "proactive" else parent
            q.triggers.append({"question": source, "option": option})
    enabled_gates = {q.triggers[0]["event"]: q.id for q in migrated if q.role == "action" and q.enabled}
    for q in migrated:
        if q.role != "action" and q.id not in children:
            for event in EVENTS:
                if event == "silence_elapsed" and q.role == "target":
                    continue
                condition = ({"question": enabled_gates[event], "option": SPEAK_OPTION[event]}
                             if event in enabled_gates else {"event": event})
                if q.stage == "pre_reply":
                    condition["ask_with_parent"] = True
                q.triggers.append(condition)
        if q.role == "target":
            for option in q.options:
                if option.id == "latest" and option.criteria == "针对最近一条消息回复":
                    option.criteria = TARGET_DEFAULT["latest"]
    return migrated, "；".join(warnings) or None


def migrate_target_gate(questions):
    target = next((q for q in questions if q.enabled and q.role == "target"), None)
    if not target or not target.triggers or any(q.role == "target_gate" for q in questions):
        return questions, None
    latest = next(o for o in target.options if o.id == "latest")
    parents = {t.get("question") for t in target.triggers}
    # 已由自定义是否题控制时，保留原图。自然接话上的已有业务效果也不能被新题跳过。
    if any(q.type == "boolean" and q.id in parents for q in questions):
        return questions, f"已保留 {target.id} 的自定义上游是否题，可在判断配置中检查是否需要添加「判断是否针对单条消息」"
    if latest.session_hint or latest.tool or any(o.weight != 1 for o in target.options) or any(
        t.get("question") == target.id and t["option"] == "latest"
        for q in questions for t in q.triggers or []
    ):
        return questions, f"已保留 {target.id} 的自定义选项，可在上游添加「判断是否针对单条消息」"
    gate_id = "address_message"
    ids = {q.id for q in questions}
    while gate_id in ids:
        gate_id += "_2"
    gate = DecisionQuestion(
        gate_id, "boolean", "本次发言是否需要针对某一条具体消息？结合发送时间与话题是否仍在继续判断，"
        "只回答是或否。已经换题或只是自然接话时选否。",
        role="target_gate", use_threshold=True, triggers=target.triggers,
        decision_mode=target.decision_mode,
    )
    target.triggers = [{"question": gate.id, "option": "true", "ask_with_parent": True}]
    if target.instructions == TARGET_PROMPT:
        target.instructions = "假设需要针对一条消息，结合发送时间和当前话题选择目标。均不合适时选 latest。"
    questions.insert(questions.index(target), gate)
    return questions, None


def default_questions(legacy=None):
    questions, _ = migrate_questions(legacy_questions(legacy))
    if legacy is None:
        meme = next(q for q in questions if q.id == "meme_id")
        meme.triggers = [{"question": "meme_category", "option": "*"}]
        meme.candidate_source = {"question": "meme_category"}
        category = DecisionQuestion(
            "meme_category",
            "choice",
            MEME_CATEGORY_PROMPT,
            options=[
                DecisionQuestionOption("none", MEME_DEFAULT["none"]),
                DecisionQuestionOption("*", "{描述}"),
            ],
            candidate_source="meme_categories",
            triggers=[{"question": "use_meme", "option": "true"}],
        )
        questions.insert(questions.index(meme), category)
    return migrate_target_gate(questions)[0]


USE_VOICE_PROMPT = (
    "这轮回复适合用语音发出吗？简短、口语化、带情绪的回复适合语音；"
    "需要代码、链接、列表、数字细节或较长说明时选否。"
)
VOICE_STYLE_HINT = "本轮正文会转成语音发送：请写口语化、适合朗读的句子，不要使用列表、表格、代码、链接或颜文字。"
VOICE_TONE_PROMPT = "这轮回复转成语音时，用什么语气朗读最贴合回复内容和当前气氛？拿不准时选 plain。"
# (选项, 判断标准, 交给通用 TTS 的语气词)；plain 沿用语音引擎自带的语气。
VOICE_TONES = (
    ("plain", "平静自然，沿用语音引擎默认的语气", ""),
    ("happy", "开心、轻快，适合玩笑、夸奖和好消息", "开心"),
    ("gentle", "温柔、安抚，适合关心、安慰和道晚安", "温柔"),
    ("playful", "俏皮、撒娇，适合调侃和亲近的闲聊", "俏皮"),
    ("sad", "低落、委屈，适合遗憾或难过的话题", "难过"),
    ("angry", "生气、不满，适合抱怨或被冒犯的时候", "生气"),
)


def voice_questions() -> list[DecisionQuestion]:
    """是否用语音及朗读语气。由判断决定是否转语音，不看通用 TTS 的触发概率。

    语音不可用（未装通用 TTS、适配停用或暂停）时，用到语音动作的题在运行时跳过，不多问一次。
    """
    tones = {
        key: [{"tool": "voice_tone", "text": tone}, {"tool": "add_context", "label": "语气", "text": tone}]
        for key, _, tone in VOICE_TONES
        if tone
    }
    return [
        DecisionQuestion(
            "use_voice",
            "boolean",
            USE_VOICE_PROMPT,
            use_threshold=True,
            triggers=[
                {"question": "action", "option": "reply", "ask_with_parent": True},
                {"question": "idle_action", "option": "proactive", "ask_with_parent": True},
            ],
            actions={
                "true": [{"tool": "voice_reply"}, {"tool": "add_context", "text": VOICE_STYLE_HINT}],
                "false": [{"tool": "text_reply"}],
            },
        ),
        DecisionQuestion(
            "voice_tone",
            "choice",
            VOICE_TONE_PROMPT,
            options=[DecisionQuestionOption(key, criteria) for key, criteria, _ in VOICE_TONES],
            triggers=[{"question": "use_voice", "option": "true", "ask_with_parent": True}],
            actions=tones,
            default_option="plain",
        ),
    ]


def default_graph() -> list[DecisionQuestion]:
    """「恢复默认问题」、全新安装和配置缺失时使用的判断图（选项动作格式）。"""
    questions, _ = migrate_actions(default_questions())
    return [*questions, *voice_questions()]


def dump_decision_questions(questions: list[DecisionQuestion]) -> str:
    return json.dumps(
        [item.to_dict() for item in questions],
        ensure_ascii=False,
        indent=2,
    )


DEFAULT_DECISION_QUESTIONS_JSON = dump_decision_questions(default_decision_questions())


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _as_int(value: Any, default: int, *, minimum: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    return result


def _as_float(
    value: Any, default: float, *, minimum: float = 0.0, maximum: float = 1.0
) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        result = default
    if not math.isfinite(result):
        result = default
    return min(maximum, max(minimum, result))


def _parse_option_weight(value: Any) -> float:
    try:
        weight = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("权重必须是 0 到 100 之间的有限数字") from None
    if (
        isinstance(value, bool)
        or not math.isfinite(weight)
        or not 0.0 <= weight <= 100.0
    ):
        raise ValueError("权重必须是 0 到 100 之间的有限数字")
    return weight


def _parse_tool(value):
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or value not in TOOLS:
        raise ValueError("工具名称无效")
    return value


def _clean_id(value: Any) -> str:
    return str(value or "").strip()


def _parse_choice_options(
    raw_options: Any, *, question_id: str, strict: bool, legacy: bool
) -> tuple[list[DecisionQuestionOption], str | None]:
    if not isinstance(raw_options, list):
        return [], f"问题 {question_id} 的 options 必须是数组"
    options: list[DecisionQuestionOption] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_options):
        if not isinstance(item, dict):
            message = f"问题 {question_id} 的第 {index + 1} 个选项格式无效"
            if strict:
                return [], message
            continue
        option_id = _clean_id(item.get("id"))
        criteria = str(item.get("criteria") or "").strip()
        if not option_id or not criteria:
            message = (
                f"问题 {question_id} 的第 {index + 1} 个选项需要非空的 id 和 criteria"
            )
            if strict:
                return [], message
            continue
        if option_id in seen:
            message = f"问题 {question_id} 存在重复选项 id：{option_id}"
            if strict:
                return [], message
            continue
        try:
            tool = "" if legacy else _parse_tool(item.get("tool"))
            weight = _parse_option_weight(item.get("weight", 1.0))
        except ValueError as exc:
            return [], f"问题 {question_id} 的选项 {option_id}：{exc}"
        seen.add(option_id)
        options.append(
            DecisionQuestionOption(
                id=option_id,
                criteria=criteria,
                session_hint=str(item.get("session_hint") or "").strip(),
                weight=weight,
                tool=tool,
            )
        )
    if len(options) < 2:
        return [], f"问题 {question_id} 至少需要两个有效选项"
    if not any(option.weight > 0.0 for option in options):
        return [], f"问题 {question_id} 至少需要一个权重大于 0 的选项"
    return options, None


def _parse_one_question(
    item: Any, *, index: int, seen_ids: set[str], strict: bool, legacy: bool, trigger_format: bool = False
) -> tuple[DecisionQuestion | None, str | None]:
    label = f"第 {index + 1} 个问题"
    if not isinstance(item, dict):
        return None, f"{label}格式无效，应为对象"
    question_id = _clean_id(item.get("id"))
    if not question_id:
        return None, f"{label}缺少 id"
    if legacy and question_id in {"action", "target_message_id", "use_meme", "meme_id"}:
        return None, f"旧配置中的问题 {question_id} 使用了保留 ID"
    if question_id in seen_ids:
        return None, f"问题 id 重复：{question_id}"
    qtype = str(item.get("type") or "").strip().lower()
    if qtype not in {"choice", "boolean"}:
        return None, f"问题 {question_id} 的 type 只能是 choice 或 boolean"
    instructions = str(item.get("instructions") or "").strip()
    if not instructions:
        return None, f"问题 {question_id} 缺少判断标准（instructions）"
    enabled = _as_bool(item.get("enabled"), True)
    role = "answer" if legacy else item.get("role", "answer")
    if (
        not isinstance(role, str)
        or role not in QUESTION_ROLES
        or (role == "target_gate" and qtype != "boolean")
        or (role not in ("answer", "target_gate") and qtype != "choice")
    ):
        return None, f"问题 {question_id} 的用途与题型不匹配"
    stage = "pre_reply" if legacy else item.get("stage", "pre_reply")
    if stage not in ("pre_reply", "post_reply"):
        return None, f"问题 {question_id} 的判断阶段无效"
    if stage == "post_reply" and role != "answer":
        return None, f"输出后的问题 {question_id} 只支持记录答案"
    decision_mode = None if legacy else item.get("decision_mode")
    if decision_mode is not None and decision_mode not in ("model", "random"):
        return None, f"问题 {question_id} 的决策模式无效"
    common = {
        "role": role,
        "stage": stage,
        "decision_mode": decision_mode,
        "use_threshold": not legacy and _as_bool(item.get("use_threshold"), False),
        "branches": {} if legacy else item.get("branches", {}),
        "candidate_source": deepcopy(item.get("candidate_source", "options")),
        "requires_candidates": str(item.get("requires_candidates") or ""),
    }
    if qtype == "choice":
        options, error = _parse_choice_options(
            item.get("options"), question_id=question_id, strict=strict, legacy=legacy
        )
        if error:
            return None, error
        required = {
            "action": {"none", "reply", "proactive"},
            "target": {"latest", "*"},
            "meme": {"none", "*"},
        }.get(role)
        if trigger_format and role == "action":
            ids = {o.id for o in options}
            if ids not in ({"none", "reply"}, {"none", "proactive"}):
                return None, f"发言题 {question_id} 需要沉默和对应的发言选项"
            required = None
        if required and {o.id for o in options} != required:
            return (
                None,
                f"问题 {question_id} 的用途要求选项为 {', '.join(sorted(required))}",
            )
        if not trigger_format and role == "action" and any(
            not any(o.weight > 0 for o in options if o.id in ("none", active))
            for active in ("reply", "proactive")
        ):
            return None, f"问题 {question_id} 在每个场景至少需要一个正权重选项"
        return (
            DecisionQuestion(
                id=question_id,
                type="choice",
                instructions=instructions,
                enabled=enabled,
                options=options,
                **common,
                proactive_instructions=str(
                    item.get("proactive_instructions") or ""
                ).strip(),
                proactive_none_criteria=str(
                    item.get("proactive_none_criteria") or ""
                ).strip(),
                ask_private=_as_bool(item.get("ask_private"), False),
            ),
            None,
        )
    try:
        true_tool = "" if legacy else _parse_tool(item.get("true_tool"))
        false_tool = "" if legacy else _parse_tool(item.get("false_tool"))
        true_weight = _parse_option_weight(item.get("true_weight", 1.0))
        false_weight = _parse_option_weight(item.get("false_weight", 1.0))
    except ValueError as exc:
        return None, f"问题 {question_id}：{exc}"
    if true_weight == false_weight == 0.0:
        return None, f"问题 {question_id} 至少需要一个权重大于 0 的选项"
    return (
        DecisionQuestion(
            id=question_id,
            type="boolean",
            instructions=instructions,
            enabled=enabled,
            true_session_hint=str(item.get("true_session_hint") or "").strip(),
            false_session_hint=str(item.get("false_session_hint") or "").strip(),
            true_weight=true_weight,
            false_weight=false_weight,
            true_tool=true_tool,
            false_tool=false_tool,
            **common,
        ),
        None,
    )


def _coerce_questions_payload(value: Any) -> tuple[list[Any] | None, str | None]:
    if value is None:
        return None, "missing"
    if isinstance(value, list):
        return value, None
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None, "missing"
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            return None, f"判断问题 JSON 无效：{exc}"
        if not isinstance(parsed, list):
            return None, "判断问题 JSON 必须是数组"
        return parsed, None
    return None, "判断问题配置格式无效，需要 JSON 数组或数组本身"


def parse_decision_questions(
    value: Any, *, strict: bool = False, legacy: bool = True, trigger_format: bool = False,
    action_format: bool = False,
) -> tuple[list[DecisionQuestion], str | None]:
    """解析自定义判断问题。

    非严格模式：缺少配置或 JSON 无效时回退到默认长短问题；单个无效问题会被跳过。
    严格模式（保存配置时）：任何错误都会返回空列表和错误信息，不回退。
    """
    payload, error = _coerce_questions_payload(value)
    if error == "missing":
        if strict:
            return [], "判断问题不能为空文本；如需关闭全部自定义问题，请提交空数组 []"
        return default_decision_questions(), None
    if error:
        if strict:
            return [], error
        return default_decision_questions(), error + "，已回退到默认长短问题"

    questions: list[DecisionQuestion] = []
    # 跳过无效的父题会让其后继变成入口，因此带分支的配置必须整体验证。
    strict = strict or (
        not legacy and any(isinstance(item, dict) and "branches" in item for item in payload)
    )
    skipped: list[str] = []
    seen_ids: set[str] = set()
    active_roles: dict[str, str] = {}
    for index, item in enumerate(payload):
        question, item_error = _parse_one_question(
            item, index=index, seen_ids=seen_ids, strict=strict, legacy=legacy, trigger_format=trigger_format
        )
        if question is not None and trigger_format:
            if "branches" in item or not isinstance(item.get("triggers"), list):
                return [], f"问题 {question.id} 需要明确的 triggers 数组"
            question.triggers = item["triggers"]
            if action_format:
                old_keys = {"role", "true_tool", "false_tool", "true_session_hint", "false_session_hint", "ask_private"}
                if old_keys.intersection(item) or (question.type == "choice" and any(
                    "tool" in opt or "session_hint" in opt for opt in item["options"]
                )):
                    return [], f"问题 {question.id} 应使用 actions 配置选项动作"
                question.actions = item.get("actions", {})
                if not isinstance(question.actions, dict):
                    return [], f"问题 {question.id} 的 actions 应为对象"
                for key in ("default_option", "private_default", "requires_candidates"):
                    if key in item:
                        if not isinstance(item[key], str):
                            return [], f"问题 {question.id} 的 {key} 应为字符串"
                        setattr(question, key, item[key])
                question.scenes = item.get("scenes", [])
                question.aliases = item.get("aliases", {})
        if question is not None and question.enabled and question.role != "answer" and not (trigger_format and question.role == "action"):
            if question.role in active_roles:
                item_error = (
                    f"问题 {question.id} 与 {active_roles[question.role]} 用途冲突，"
                    f"同一用途只能启用一个问题：{QUESTION_ROLES[question.role]}"
                )
            else:
                active_roles[question.role] = question.id
        if item_error:
            if strict:
                return [], item_error
            skipped.append(item_error)
            continue
        if question is None:
            continue
        seen_ids.add(question.id)
        questions.append(question)

    if skipped:
        warning = "已跳过无效的判断问题：" + "；".join(skipped)
        if not questions and payload and legacy:
            return default_decision_questions(), warning + "，已回退到默认长短问题"
        return questions, warning
    branch_error = _validate_triggers(questions, action_format) if trigger_format else _validate_branches(questions)
    return ([], branch_error) if branch_error else (questions, None)


def _validate_branches(questions: list[DecisionQuestion]) -> str | None:
    by_id = {q.id: q for q in questions}
    graph = TopologicalSorter()
    for question in questions:
        if question.branches == {}:
            continue
        if not isinstance(question.branches, dict):
            return f"问题 {question.id} 的 branches 必须是选项到后继问题 ID 的对象"
        options = (
            {o.id for o in question.options} if question.type == "choice" else {"true", "false"}
        )
        for option, target in question.branches.items():
            if option not in options:
                return f"问题 {question.id} 的分支选项不存在：{option}"
            if not isinstance(target, str) or target not in by_id:
                return f"问题 {question.id} 的选项 {option} 指向不存在的后继问题：{target}"
            if question.stage == "post_reply" and by_id[target].stage == "pre_reply":
                return f"输出后的问题 {question.id} 不能指向输出前的问题 {target}"
            graph.add(target, question.id)
    try:
        graph.prepare()
    except CycleError as exc:
        return "后继问题不能形成循环：" + " → ".join(exc.args[1])
    return None


def _validate_triggers(questions, action_format=False):
    by_id = {q.id: q for q in questions}
    graph = TopologicalSorter()
    gates = {}
    for q in questions:
        seen = set()
        if action_format and (error := validate_actions(q, by_id)):
            return f"问题 {q.id}：{error}"
        source_id = source_question(q.candidate_source)
        if source_id and (q.enabled or source_id in by_id):
            graph.add(q.id, source_id)
        if q.role == "action" and (len(q.triggers) != 1 or not isinstance(q.triggers[0], dict)
                                   or "event" not in q.triggers[0]):
            return f"控制发言题 {q.id} 需要一个事件入口"
        for t in q.triggers:
            if not isinstance(t, dict) or type(t.get("ask_with_parent", False)) is not bool:
                return f"问题 {q.id} 的生效条件或提问时机无效"
            early = t.get("ask_with_parent", False)
            if source_id and early:
                return f"问题 {q.id} 需要等待候选来源的答案，不能同批提前提问"
            if "event" in t:
                event = t["event"]
                events = WORKFLOW_EVENTS if action_format else EVENTS
                allowed = {"event", "ask_with_parent"}
                if event == "timer_elapsed" and action_format:
                    allowed |= {"interval_seconds", "daily_time"}
                    interval, daily = t.get("interval_seconds"), t.get("daily_time")
                    if ("interval_seconds" in t) == ("daily_time" in t):
                        return f"问题 {q.id} 的定时触发须选择固定间隔或每日时刻"
                    if "interval_seconds" in t and (type(interval) is not int or not 15 <= interval <= 2147483647):
                        return f"问题 {q.id} 的定时间隔须为 15～2147483647 秒的整数"
                    if "daily_time" in t and (not isinstance(daily, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", daily)):
                        return f"问题 {q.id} 的每日时刻须为 HH:MM（00:00～23:59）"
                if set(t) - allowed or not isinstance(event, str) or event not in events:
                    return f"问题 {q.id} 的事件无效"
                if action_format and (early or (event == "reply_generated") != (q.stage == "post_reply")):
                    return f"问题 {q.id} 的事件与判断阶段或提问时机不符"
                key = (event,)
                if early and (q.role == "action" or q.stage == "post_reply"):
                    return f"问题 {q.id} 不能随发言入口提前提问"
                if q.role == "action":
                    if SPEAK_OPTION[event] not in {o.id for o in q.options}:
                        return f"问题 {q.id} 的发言选项与事件不匹配"
                    if q.enabled and event in gates:
                        return f"事件「{EVENTS[event]}」已有启用的发言题 {gates[event]}"
                    if q.enabled:
                        gates[event] = q.id
                    silent = next(o for o in q.options if o.id == "none")
                    if silent.tool or silent.session_hint:
                        return f"问题 {q.id} 的沉默选项不能绑定提示或工具"
            else:
                parent_id, option = t.get("question"), t.get("option")
                if not isinstance(parent_id, str) or not isinstance(option, str) or set(t) - {"question","option","ask_with_parent"}:
                    return f"问题 {q.id} 的上游条件无效"
                parent = by_id.get(parent_id)
                if parent is None:
                    return f"问题 {q.id} 的上游 {parent_id} 不存在"
                options = {o.id for o in parent.options} if parent.type == "choice" else {"true","false"}
                if option not in options or (parent.role == "action" and option == "none"):
                    return f"问题 {q.id} 的上游选项不存在或为沉默"
                if parent.stage != q.stage and (parent.stage == "post_reply" or early):
                    return f"问题 {q.id} 的触发条件与回复阶段冲突"
                key = (parent_id, option)
                graph.add(q.id, parent_id)
            if key in seen:
                return f"问题 {q.id} 有重复生效条件"
            seen.add(key)
    try:
        graph.prepare()
    except CycleError as exc:
        return "生效条件形成循环：" + " → ".join(exc.args[1])
    return None


def load_questions(value, *, strict=False):
    """加载判断图，在读入旧版本时迁移目标分支。"""
    if not strict and (value is None or (isinstance(value, str) and not value.strip())):
        return default_questions(), None
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
            if isinstance(decoded, (dict, list)):
                value = decoded
        except ValueError:
            pass
    if isinstance(value, dict) and "version" in value:
        if (
            type(value.get("version")) is not int
            or value["version"] not in (2, 3, 4, 5, 6)
            or not isinstance(value.get("questions"), list)
        ):
            return [], "无法识别的问题配置版本或格式"
        if value["version"] >= 3:
            questions, warning = parse_decision_questions(value["questions"], strict=True, legacy=False, trigger_format=True,
                                                         action_format=value["version"] >= 5)
            if warning:
                return questions, warning
            if value["version"] >= 4:
                if value["version"] < 6:
                    questions = migrate_candidate_sources(questions)
                return questions, None
        else:
            old, warning = parse_decision_questions(value["questions"], strict=strict, legacy=False)
            questions, migrated_warning = migrate_questions(old)
            warning = "；".join(w for w in (warning, migrated_warning) if w)
        questions, target_warning = migrate_target_gate(questions)
        return questions, "；".join(w for w in (warning, target_warning) if w) or None
    legacy, warning = parse_decision_questions(value, strict=strict)
    if warning and strict:
        return [], warning
    if warning and isinstance(value, str):
        return default_questions(), warning
    return default_questions(legacy), warning


class PluginSettings:
    """把 AstrBotConfig 转成带默认值的纯数据对象。"""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        data = config or {}
        group = data.get("group") or {}
        private = data.get("private") or {}
        self.enabled = _as_bool(data.get("enabled"), True)
        self.dry_run = _as_bool(data.get("dry_run"), False)
        self.decision_mode = "random" if data.get("decision_mode") == "random" else "model"
        self.decision_provider_id = str(data.get("decision_provider_id") or "").strip()
        self.vision_provider_id = str(data.get("vision_provider_id") or "").strip()
        self.choice_threshold = _as_float(data.get("choice_threshold"), 0.5)
        self.choice_temperature = _as_float(
            data.get("choice_temperature"), 0.0, maximum=2.0
        )
        self.context_message_limit = _as_int(
            data.get("context_message_limit"), 20, minimum=1
        )
        self.debounce_seconds = _as_float(
            data.get("debounce_seconds"), 1.5, minimum=0.0, maximum=30.0
        )
        self.cooldown_seconds = _as_int(data.get("cooldown_seconds"), 300, minimum=0)
        self.group_enable_interrupt = _as_bool(group.get("enable_interrupt"), True)
        mode = group.get("target_quote_mode")
        self.target_quote_mode = mode if mode in ("none", "first", "all") else "first"
        self.group_enable_proactive = _as_bool(
            group.get("enable_proactive_after_silence"), True
        )
        self.group_silence_seconds = _as_int(
            group.get("silence_seconds"), 600, minimum=10
        )
        self.group_hourly_proactive_limit = _as_int(
            group.get("hourly_proactive_limit"), 3, minimum=0
        )
        self.private_enable_proactive = _as_bool(
            private.get("enable_proactive_after_silence"), True
        )
        self.private_silence_seconds = _as_int(
            private.get("silence_seconds"), 1800, minimum=10
        )
        self.private_hourly_proactive_limit = _as_int(
            private.get("hourly_proactive_limit"), 3, minimum=0
        )
        self.decision_log_limit = _as_int(
            data.get("decision_log_limit"), 200, minimum=1
        )
        self.decision_log_include_content = _as_bool(
            data.get("decision_log_include_content"), True
        )
        self.decision_custom_context = str(
            data.get("decision_custom_context") or ""
        ).strip()
        graph = data.get("decision_questions")
        if isinstance(graph, str):
            try:
                decoded = json.loads(graph)
                if isinstance(decoded, (dict, list)):
                    graph = decoded
            except ValueError:
                pass
        self.workflow_version = graph.get("version") if isinstance(graph, dict) else None
        self.decision_questions, self.decision_questions_warning = load_questions(graph)
        plugins = data.get("external_plugins")
        plugins = plugins if isinstance(plugins, Mapping) else {}
        self.disabled_plugins = frozenset(
            plugin_id
            for plugin_id in EXTERNAL_PLUGINS
            if isinstance(plugins.get(plugin_id), Mapping)
            and not _as_bool(plugins[plugin_id].get("enabled"), True)
        )
        tts = plugins.get("universal_tts")
        self.tts_proactive_voice = _as_bool(
            tts.get("proactive_voice") if isinstance(tts, Mapping) else None, True
        )

    def plugin_enabled(self, plugin_id: str) -> bool:
        """外部插件适配器是否在快速决策中启用；未写配置时默认启用。"""
        return plugin_id not in self.disabled_plugins

    def silence_seconds(self, is_private: bool) -> int:
        return (
            self.private_silence_seconds if is_private else self.group_silence_seconds
        )

    def proactive_enabled(self, is_private: bool) -> bool:
        if is_private:
            return self.private_enable_proactive
        return self.group_enable_proactive

    def hourly_proactive_limit(self, is_private: bool) -> int:
        if is_private:
            return self.private_hourly_proactive_limit
        return self.group_hourly_proactive_limit
