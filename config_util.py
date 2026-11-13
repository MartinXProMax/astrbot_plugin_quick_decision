"""读取并规范化插件配置。"""

from __future__ import annotations

import json
from typing import Any

from .models import (
    RESERVED_QUESTION_IDS,
    CustomDecisionQuestion,
    DecisionQuestionOption,
)

DEFAULT_LENGTH_INSTRUCTIONS = "如果发言，回复应该简短还是稍详细？"
DEFAULT_SHORT_CRITERIA = "简短回复，一两句即可"
DEFAULT_SHORT_SESSION_HINT = "本轮回复策略：请给出简短、自然的回复，一两句即可。"
DEFAULT_LONG_CRITERIA = "更完整、稍详细的回复"
DEFAULT_LONG_SESSION_HINT = "本轮回复策略：请给出稍详细、完整的回复，但仍保持自然。"


def default_decision_questions() -> list[CustomDecisionQuestion]:
    """默认的长短判断问题，用词与 1.0 保持一致。"""
    return [
        CustomDecisionQuestion(
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


def dump_decision_questions(questions: list[CustomDecisionQuestion]) -> str:
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
    except (TypeError, ValueError):
        result = default
    return min(maximum, max(minimum, result))


def _clean_id(value: Any) -> str:
    return str(value or "").strip()


def _parse_choice_options(
    raw_options: Any, *, question_id: str, strict: bool
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
        seen.add(option_id)
        options.append(
            DecisionQuestionOption(
                id=option_id,
                criteria=criteria,
                session_hint=str(item.get("session_hint") or "").strip(),
            )
        )
    if len(options) < 2:
        return [], f"问题 {question_id} 至少需要两个有效选项"
    return options, None


def _parse_one_question(
    item: Any, *, index: int, seen_ids: set[str], strict: bool
) -> tuple[CustomDecisionQuestion | None, str | None]:
    label = f"第 {index + 1} 个问题"
    if not isinstance(item, dict):
        return None, f"{label}格式无效，应为对象"
    question_id = _clean_id(item.get("id"))
    if not question_id:
        return None, f"{label}缺少 id"
    if question_id in RESERVED_QUESTION_IDS:
        return None, f"问题 id「{question_id}」为内置保留项，不能使用"
    if question_id in seen_ids:
        return None, f"问题 id 重复：{question_id}"
    qtype = str(item.get("type") or "").strip().lower()
    if qtype not in {"choice", "boolean"}:
        return None, f"问题 {question_id} 的 type 只能是 choice 或 boolean"
    instructions = str(item.get("instructions") or "").strip()
    if not instructions:
        return None, f"问题 {question_id} 缺少判断标准（instructions）"
    enabled = _as_bool(item.get("enabled"), True)
    if qtype == "choice":
        options, error = _parse_choice_options(
            item.get("options"), question_id=question_id, strict=strict
        )
        if error:
            return None, error
        return (
            CustomDecisionQuestion(
                id=question_id,
                type="choice",
                instructions=instructions,
                enabled=enabled,
                options=options,
            ),
            None,
        )
    return (
        CustomDecisionQuestion(
            id=question_id,
            type="boolean",
            instructions=instructions,
            enabled=enabled,
            true_session_hint=str(item.get("true_session_hint") or "").strip(),
            false_session_hint=str(item.get("false_session_hint") or "").strip(),
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
    value: Any, *, strict: bool = False
) -> tuple[list[CustomDecisionQuestion], str | None]:
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

    questions: list[CustomDecisionQuestion] = []
    skipped: list[str] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(payload):
        question, item_error = _parse_one_question(
            item, index=index, seen_ids=seen_ids, strict=strict
        )
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
        if not questions and payload:
            return default_decision_questions(), warning + "，已回退到默认长短问题"
        return questions, warning
    return questions, None


class PluginSettings:
    """把 AstrBotConfig 转成带默认值的纯数据对象。"""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        data = config or {}
        group = data.get("group") or {}
        private = data.get("private") or {}
        self.enabled = _as_bool(data.get("enabled"), True)
        self.dry_run = _as_bool(data.get("dry_run"), False)
        self.decision_provider_id = str(data.get("decision_provider_id") or "").strip()
        self.vision_provider_id = str(data.get("vision_provider_id") or "").strip()
        self.choice_threshold = _as_float(data.get("choice_threshold"), 0.5)
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
        self.decision_questions, self.decision_questions_warning = (
            parse_decision_questions(data.get("decision_questions"))
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

