"""读取并规范化插件配置。"""

from __future__ import annotations

import json
import math
from graphlib import CycleError, TopologicalSorter
from typing import Any

from .decision_tools import TOOLS
from .models import (
    QUESTION_ROLES,
    DecisionQuestion,
    DecisionQuestionOption,
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
TARGET_DEFAULT = {"latest": "针对最近一条消息回复"}
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


def default_questions(legacy=None) -> list[DecisionQuestion]:
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
    item: Any, *, index: int, seen_ids: set[str], strict: bool, legacy: bool
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
        or (role != "answer" and qtype != "choice")
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
        if required and {o.id for o in options} != required:
            return (
                None,
                f"问题 {question_id} 的用途要求选项为 {', '.join(sorted(required))}",
            )
        if role == "action" and any(
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
    value: Any, *, strict: bool = False, legacy: bool = True
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
            item, index=index, seen_ids=seen_ids, strict=strict, legacy=legacy
        )
        if question is not None and question.enabled and question.role != "answer":
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
    branch_error = _validate_branches(questions)
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


def load_questions(value, *, strict=False):
    """旧数组仅存附加题；版本 2 保存完整列表，空列表也有效。"""
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
            or value["version"] != 2
            or not isinstance(value.get("questions"), list)
        ):
            return [], "无法识别的问题配置版本或格式"
        return parse_decision_questions(value["questions"], strict=strict, legacy=False)
    legacy, warning = parse_decision_questions(value, strict=strict)
    if warning and strict:
        return [], warning
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
        self.group_direct_send = _as_bool(group.get("direct_send"), True)
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
        self.decision_custom_context = str(
            data.get("decision_custom_context") or ""
        ).strip()
        self.decision_questions, self.decision_questions_warning = load_questions(
            data.get("decision_questions")
        )

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

    def should_quote_target(self, is_private: bool) -> bool:
        """群聊在开启直接发送时不引用某一条消息；私聊仍保持原样。"""
        if is_private:
            return True
        return not self.group_direct_send
