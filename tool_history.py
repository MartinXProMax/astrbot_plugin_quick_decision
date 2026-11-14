"""整理会话里的工具调用，避免未配对调用被发给 Gemini。

只删除无法按 call id 唯一配对的工具元数据或因此失去内容的空消息。
不补造工具结果，不改写合法工具输出，不调整已配对结果的顺序。
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any

CHECKPOINT_ROLE = "_checkpoint"


@dataclass
class ToolHistoryStats:
    """结构统计。不含消息正文、参数或 call id。"""

    kept_exchanges: int = 0
    removed_calls: int = 0
    dropped_results: int = 0
    dropped_empty_assistants: int = 0
    role_counts: dict[str, int] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(
            self.removed_calls
            or self.dropped_results
            or self.dropped_empty_assistants
        )


def load_history_records(history_raw: str | list | None) -> list[Any]:
    """解析会话历史。非法 JSON 视为空，不猜测内容。"""
    if not history_raw:
        return []
    if isinstance(history_raw, str):
        try:
            records = json.loads(history_raw)
        except json.JSONDecodeError:
            return []
    elif isinstance(history_raw, list):
        records = history_raw
    else:
        return []
    if not isinstance(records, list):
        return []
    return list(records)


def _call_id(call: Any) -> str | None:
    if not isinstance(call, dict):
        return None
    value = call.get("id")
    if isinstance(value, str) and value:
        return value
    return None


def _result_id(message: Any) -> str | None:
    if not isinstance(message, dict):
        return None
    value = message.get("tool_call_id")
    if isinstance(value, str) and value:
        return value
    return None


def _assistant_has_visible_payload(content: Any) -> bool:
    """文本、加密或明文 reasoning、图片或音频都算可保留内容。"""
    if isinstance(content, str):
        return bool(content)
    if not isinstance(content, list):
        return False
    for part in content:
        if isinstance(part, str) and part:
            return True
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type == "text" and str(part.get("text") or ""):
            return True
        if part_type == "think" and (
            part.get("encrypted") or str(part.get("think") or "")
        ):
            return True
        if part_type in {"image_url", "image", "audio_url", "input_audio"}:
            return True
    return False


def _assistant_emits_model_text(content: Any) -> bool:
    """对应 4.29.4 Responses 转换：只有非空文本会变成 assistant message。"""
    if isinstance(content, str):
        return content != ""
    if not isinstance(content, list):
        return False
    for part in content:
        if isinstance(part, str) and part:
            return True
        if isinstance(part, dict) and part.get("type") == "text":
            if str(part.get("text") or "") != "":
                return True
    return False


def _unique_ids(calls: list[Any], results: list[Any]) -> set[str]:
    """call 与紧随的 tool 结果都恰好出现一次的 id。重复 id 不猜测归属。"""
    call_counts: dict[str, int] = {}
    result_counts: dict[str, int] = {}
    for call in calls:
        cid = _call_id(call)
        if cid:
            call_counts[cid] = call_counts.get(cid, 0) + 1
    for result in results:
        cid = _result_id(result)
        if cid:
            result_counts[cid] = result_counts.get(cid, 0) + 1
    return {
        cid
        for cid, count in call_counts.items()
        if count == 1 and result_counts.get(cid) == 1
    }


def repair_tool_history(
    messages: list[Any],
    *,
    preserve_open_tail: bool = False,
) -> tuple[list[Any], ToolHistoryStats]:
    """按相邻工具交换配对。

    合法交换是：一条带 tool_calls 的 assistant，后面只隔着 checkpoint，
    紧跟若干 role=tool，且每个 call id 与结果 id 一一对应。结果顺序可以
    和调用顺序不同。失败的工具输出只要 id 配对就原样保留。

    ``preserve_open_tail`` 只留给「列表末尾仍在等待工具执行」的调用方。
    发送前的钩子必须关闭它：本轮用户消息会接在历史后面，末尾未闭合调用
    同样会在模型内容之前留下 pending functionResponse。官方工具循环的
    下一次请求发生在结果已经写入消息列表之后，且不会再次进入该钩子。
    """
    stats = ToolHistoryStats()
    output: list[Any] = []
    index = 0
    total = len(messages)

    while index < total:
        message = messages[index]
        if not isinstance(message, dict):
            output.append(message)
            index += 1
            continue
        role = message.get("role")
        if role == "tool":
            stats.dropped_results += 1
            index += 1
            continue
        calls = message.get("tool_calls") if role == "assistant" else None
        if not isinstance(calls, list) or not calls:
            output.append(message)
            index += 1
            continue

        cursor = index + 1
        interstitial: list[Any] = []
        results: list[Any] = []
        while cursor < total:
            nxt = messages[cursor]
            if isinstance(nxt, dict) and nxt.get("role") == CHECKPOINT_ROLE:
                interstitial.append(nxt)
                cursor += 1
                continue
            if isinstance(nxt, dict) and nxt.get("role") == "tool":
                results.append(nxt)
                cursor += 1
                continue
            break

        open_tail = preserve_open_tail and cursor == total
        matched = _unique_ids(calls, results)
        call_ids = [_call_id(call) for call in calls]
        result_ids = [_result_id(result) for result in results]
        perfect = (
            bool(call_ids)
            and all(cid in matched for cid in call_ids)
            and all(cid in matched for cid in result_ids)
            and len(call_ids) == len(matched)
            and len(result_ids) == len(matched)
        )
        if perfect or open_tail:
            output.append(message)
            output.extend(interstitial)
            output.extend(results)
            if perfect:
                stats.kept_exchanges += 1
            index = cursor
            continue

        kept_calls = [call for call in calls if _call_id(call) in matched]
        kept_results = [
            result for result in results if _result_id(result) in matched
        ]
        stats.removed_calls += len(calls) - len(kept_calls)
        stats.dropped_results += len(results) - len(kept_results)
        if kept_calls:
            cloned = copy.deepcopy(message)
            cloned["tool_calls"] = kept_calls
            output.append(cloned)
            output.extend(interstitial)
            output.extend(kept_results)
            stats.kept_exchanges += 1
        else:
            cloned = copy.deepcopy(message)
            cloned.pop("tool_calls", None)
            cloned.pop("tool_call_id", None)
            if _assistant_has_visible_payload(cloned.get("content")):
                output.append(cloned)
                output.extend(interstitial)
            else:
                stats.dropped_empty_assistants += 1
                output.extend(interstitial)
        index = cursor

    stats.role_counts = {}
    for message in output:
        if isinstance(message, dict):
            role_name = str(message.get("role") or "")
            stats.role_counts[role_name] = stats.role_counts.get(role_name, 0) + 1
    return output, stats


def sanitize_provider_request(req: Any) -> ToolHistoryStats:
    """就地整理即将发送的 contexts。不写回会话库。"""
    raw = getattr(req, "contexts", None)
    if raw is None:
        return ToolHistoryStats()
    records = load_history_records(raw)
    repaired, stats = repair_tool_history(records, preserve_open_tail=False)
    if stats.changed:
        req.contexts = repaired
    return stats


def append_generated_turn(
    records: list[Any],
    *,
    proactive: bool,
    user_prompt: str,
    assistant_text: str,
) -> list[Any]:
    """在完整历史上追加本轮文本，不丢弃已有的 tool 消息。"""
    updated = list(records)
    if proactive:
        updated.append({"role": "assistant", "content": assistant_text})
    else:
        updated.append({"role": "user", "content": user_prompt})
        updated.append({"role": "assistant", "content": assistant_text})
    return updated


def pending_model_violations(messages: list[Any]) -> list[dict[str, int]]:
    """用 4.29.4 Responses 转换的顺序检查「内容出现时仍有未闭合 function_call」。

    不调用模型。think / 加密 reasoning 本身不产生 assistant 文本；
    tool_calls 产生 function_call；role=tool 产生 function_call_output。
    """
    pending: list[str] = []
    input_index = 0
    violations: list[dict[str, int]] = []
    for message_index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == CHECKPOINT_ROLE:
            continue
        if role == "tool":
            cid = _result_id(message)
            if cid in pending:
                pending.remove(cid)
            input_index += 1
            continue
        if role not in {"user", "assistant", "system", "developer"}:
            continue
        if role == "assistant":
            emits = _assistant_emits_model_text(message.get("content"))
        else:
            content = message.get("content")
            emits = content not in (None, "", [])
        if emits:
            if pending and role in {"user", "assistant"}:
                violations.append(
                    {
                        "message_index": message_index,
                        "input_index": input_index,
                        "pending": len(pending),
                    }
                )
            input_index += 1
        if role == "assistant":
            calls = message.get("tool_calls")
            if isinstance(calls, list):
                for call in calls:
                    cid = _call_id(call)
                    if cid:
                        pending.append(cid)
                        input_index += 1
    return violations
