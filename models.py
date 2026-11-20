"""插件内部使用的数据结构。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .decision_engine import PostDecisionPlan

ActionType = Literal["none", "reply", "join", "proactive"]
# join 仅为历史日志兼容，运行时与 reply 同义。
LengthType = Literal["short", "long"]
SceneType = Literal[
    "group_passive",
    "private_passive",
    "group_proactive",
    "private_proactive",
]
DescriptionSource = Literal["manual", "auto", "empty"]


def normalize_action(action: str) -> str:
    """将历史 join 规范为 reply；其它动作原样返回。不改写已落盘的日志。"""
    if action == "join":
        return "reply"
    return action


@dataclass
class MemeItem:
    """全局表情包元数据。"""

    id: str
    filename: str
    original_name: str = ""
    mime: str = "image/png"
    description: str = ""
    description_source: DescriptionSource = "empty"
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemeItem:
        source = data.get("description_source") or "empty"
        if source not in {"manual", "auto", "empty"}:
            source = "empty"
        return cls(
            id=str(data.get("id", "")),
            filename=str(data.get("filename", "")),
            original_name=str(data.get("original_name", "")),
            mime=str(data.get("mime") or "image/png"),
            description=str(data.get("description", "")),
            description_source=source,  # type: ignore[arg-type]
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
        )


@dataclass
class BufferedMessage:
    """供判断模型使用的精简聊天记录。"""

    message_id: str
    sender_id: str
    sender_name: str
    role: Literal["user", "assistant"]
    text: str
    is_at_bot: bool = False
    is_quote_bot: bool = False
    quote_id: str = ""
    quote_sender_id: str = ""
    quote_text: str = ""
    timestamp: float = 0.0

    def to_state_dict(self) -> dict[str, Any]:
        item: dict[str, Any] = {
            "message_id": self.message_id,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "role": self.role,
            "text": self.text,
            "timestamp": int(self.timestamp),
        }
        if self.is_at_bot:
            item["is_at_bot"] = True
        if self.is_quote_bot:
            item["is_quote_bot"] = True
        if self.quote_id:
            item["quote_id"] = self.quote_id
        if self.quote_sender_id:
            item["quote_sender_id"] = self.quote_sender_id
        if self.quote_text:
            item["quote_text"] = self.quote_text
        return item


QUESTION_ROLES = {
    "answer": "记录答案",
    "action": "控制发言",
    "target": "选择回复目标",
    "meme": "选择表情",
}


@dataclass
class DecisionQuestionOption:
    id: str
    criteria: str
    session_hint: str = ""
    weight: float = 1.0
    tool: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DecisionQuestion:
    id: str
    type: Literal["choice", "boolean"]
    instructions: str
    enabled: bool = True
    options: list[DecisionQuestionOption] = field(default_factory=list)
    true_session_hint: str = ""
    false_session_hint: str = ""
    true_weight: float = 1.0
    false_weight: float = 1.0
    true_tool: str = ""
    false_tool: str = ""
    role: str = "answer"
    use_threshold: bool = False
    proactive_instructions: str = ""
    proactive_none_criteria: str = ""
    ask_private: bool = False
    branches: dict[str, str] = field(default_factory=dict)
    triggers: list[dict] | None = None
    stage: Literal["pre_reply", "post_reply"] = "pre_reply"
    # None 继承插件默认模式，model/random 可覆盖单题。
    decision_mode: Literal["model", "random"] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            k: getattr(self, k)
            for k in ("id", "enabled", "type", "instructions", "role", "use_threshold")
        }
        if self.type == "choice":
            data["options"] = [item.to_dict() for item in self.options]
        else:
            for side in ("true", "false"):
                for suffix in ("session_hint", "weight", "tool"):
                    key = f"{side}_{suffix}"
                    data[key] = getattr(self, key)
        if self.triggers is not None:
            data["triggers"] = [dict(t) for t in self.triggers]
            if self.role == "action" and any(o.id == "reply" for o in self.options):
                data["ask_private"] = self.ask_private
        elif self.role == "action":
            data.update(
                proactive_instructions=self.proactive_instructions,
                ask_private=self.ask_private,
                proactive_none_criteria=self.proactive_none_criteria,
            )
        if self.branches:
            data["branches"] = dict(self.branches)
        if self.stage == "post_reply":
            data["stage"] = self.stage
        if self.decision_mode is not None:
            data["decision_mode"] = self.decision_mode
        return data


@dataclass
class ToolCall:
    name: str
    sources: list[str] = field(default_factory=list)
    args: dict[str, str] = field(default_factory=dict)
    status: str = "planned"
    reason: str = ""


@dataclass
class DecisionOutcome:
    """单题评估结果；raw 保留旧日志所需的提供商及采样数据。"""

    value: str | bool | None
    status: Literal["ok", "missing", "invalid", "skipped", "fallback"] = "ok"
    reason: str = ""
    probabilities: dict[str, float] = field(default_factory=dict)
    selection_probabilities: dict[str, float] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_answer(self) -> bool:
        return self.status == "ok" or self.reason == "private_passive_default_reply"


@dataclass
class DecisionResult:
    """一次结构化判断的业务结果。"""

    action: ActionType = "none"
    target_message_id: str | None = None
    length: LengthType | None = None
    use_meme: bool = False
    meme_id: str | None = None
    scene: SceneType = "group_passive"
    raw: dict[str, Any] = field(default_factory=dict)
    custom_answers: dict[str, Any] = field(default_factory=dict)
    session_hints: list[str] = field(default_factory=list)
    error: str | None = None
    degraded: bool = False
    duration_ms: int = 0
    dry_run: bool = False
    official_llm: bool = False
    outcomes: dict[str, DecisionOutcome] = field(default_factory=dict)

    tool_calls: dict[str, ToolCall] = field(default_factory=dict)
    batches: list[dict] = field(default_factory=list)
    round_id: str = ""
    decision_mode: Literal["model", "random", "mixed"] = "model"
    target_summary: str = ""
    meme_description: str = ""
    post_plan: PostDecisionPlan | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.use_meme and self.meme_id and not self.tool_calls:
            self.tool_calls["send_meme"] = ToolCall(
                "send_meme", args={"meme_id": self.meme_id}
            )

    @property
    def answers(self):
        return {
            key: o.value
            for key, o in self.outcomes.items()
            if o.has_answer
        }

    @property
    def failed(self) -> bool:
        return self.degraded or bool(self.error)

    def should_speak(self) -> bool:
        return not self.failed and normalize_action(self.action) in {
            "reply",
            "proactive",
        }


@dataclass
class SessionProfile:
    """会话的基本类型信息。"""

    umo: str
    is_private: bool
    platform_id: str = ""
    session_id: str = ""
    bot_id: str = ""
