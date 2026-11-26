"""入站事件交接给官方主 Agent 的纯逻辑，不直接调用聊天模型或发送消息。"""

from __future__ import annotations

import time
from typing import Any, Iterable

from astrbot.api import logger

from .models import BufferedMessage


def default_official_agent_would_run(
    *,
    has_send_oper: bool,
    is_at_or_wake_command: bool,
    call_llm: bool,
    has_result: bool,
    is_stopped: bool,
) -> bool:
    """对应 AstrBot 4.29.4 ProcessStage 在插件 handler 之后的默认 Agent 条件。

    现网实现是 ``not event.call_llm``，``should_call_llm(True)`` 会把该标记设为 True，
    从而禁止默认链路，但不会禁止插件 yield 的 ``request_llm``。
    """
    if has_send_oper or not is_at_or_wake_command or call_llm:
        return False
    return (has_result and not is_stopped) or not has_result


def suppress_default_official_agent(event) -> None:
    """禁止本事件再走官方默认 LLM，保留插件 yield 的 ProviderRequest。"""
    setter = getattr(event, "should_call_llm", None)
    if callable(setter):
        setter(True)
        return
    event.call_llm = True


def should_use_default_official_agent(
    *, official_would_run: bool, has_merged_messages: bool
) -> bool:
    """私聊/普通 @ 且本轮没有并入其它用户消息时，仍放行官方默认 Agent。"""
    return bool(official_would_run) and not has_merged_messages


def merged_user_messages(
    messages: Iterable[BufferedMessage],
    *,
    first_message_id: str,
    first_timestamp: float,
) -> list[BufferedMessage]:
    """取出本轮 debounce / 判断窗口内的用户消息，不带入更早的历史。"""
    window_start = max(first_timestamp - 0.5, 0.0)
    selected: list[BufferedMessage] = []
    for item in messages:
        if item.role != "user":
            continue
        if first_message_id and item.message_id:
            if item.message_id == first_message_id or item.timestamp >= window_start:
                selected.append(item)
        elif item.timestamp >= window_start:
            selected.append(item)
    return selected


def build_inbound_user_prompt(
    *,
    event_text: str,
    messages: Iterable[BufferedMessage],
    first_message_id: str,
    first_timestamp: float,
    merged: bool,
) -> str:
    """构造交给官方 request_llm 的本轮用户文本。合并时不得丢掉窗口内用户消息。"""
    if not merged:
        return (event_text or "").strip()
    parts: list[str] = []
    for item in merged_user_messages(
        messages,
        first_message_id=first_message_id,
        first_timestamp=first_timestamp,
    ):
        text = (item.text or "").strip()
        if not text:
            continue
        name = (item.sender_name or item.sender_id or "").strip()
        parts.append(f"{name}: {text}" if name else text)
    if parts:
        return "\n".join(parts)
    return (event_text or "").strip()


def collect_event_media_urls(event) -> tuple[list[str], list[str]]:
    """收集当前入站事件上已有的图片/音频引用；不做网络下载。"""
    images: list[str] = []
    audios: list[str] = []
    comps = getattr(getattr(event, "message_obj", None), "message", None) or []
    for comp in comps:
        name = type(comp).__name__
        ref = getattr(comp, "url", None) or getattr(comp, "file", None)
        if not ref:
            continue
        if name == "Image":
            images.append(str(ref))
        elif name == "Record":
            audios.append(str(ref))
    return images, audios


async def resolve_session_conversation(context, event) -> Any | None:
    """取得当前 UMO 的官方会话对象。不传自定义 contexts，以免丢 conversation。"""
    conv_mgr = getattr(context, "conversation_manager", None)
    if conv_mgr is None:
        return None
    umo = getattr(event, "unified_msg_origin", "") or ""
    get_platform = getattr(event, "get_platform_id", None)
    platform_id = get_platform() if callable(get_platform) else ""
    try:
        cid = await conv_mgr.get_curr_conversation_id(umo)
        if not cid and hasattr(conv_mgr, "new_conversation"):
            cid = await conv_mgr.new_conversation(umo, platform_id)
        if not cid:
            return None
        conversation = await conv_mgr.get_conversation(umo, cid)
        if conversation is None and hasattr(conv_mgr, "new_conversation"):
            cid = await conv_mgr.new_conversation(umo, platform_id)
            conversation = await conv_mgr.get_conversation(umo, cid)
        return conversation
    except Exception:  # noqa: BLE001
        logger.exception("读取官方会话失败：%s", umo)
        return None


def make_official_llm_request(
    event,
    *,
    conversation,
    prompt: str,
    image_urls: list[str] | None = None,
    audio_urls: list[str] | None = None,
):
    """通过公开的 event.request_llm 构造 ProviderRequest，不附带自定义 contexts。"""
    return event.request_llm(
        prompt=prompt or "",
        conversation=conversation,
        image_urls=list(image_urls or []),
        audio_urls=list(audio_urls or []),
    )


def is_official_pipeline_busy(runtime) -> bool:
    return bool(getattr(runtime, "deciding", False)) or (
        getattr(runtime, "pending_pipeline", None) is not None
    )


def begin_official_pipeline(runtime, result, event=None) -> None:
    """占用会话直到官方流水线发完本轮正文；event 供超时检查查看官方 Agent 是否仍在运行。"""
    runtime.pending_pipeline = result
    runtime.pipeline_event = event
    runtime.pipeline_started_at = runtime.pipeline_active_at = time.monotonic()
    runtime.deciding = True
    runtime.pipeline_released.clear()


def touch_official_pipeline(runtime, decision) -> None:
    """官方流水线仍有动静（模型请求、工具调用、模型回复、装饰、发送）时记下时刻，推迟超时释放。

    只认仍占用会话的这一轮：已被超时释放的旧事件迟到的动静不能延长下一轮。
    """
    if runtime is not None and decision is not None and getattr(runtime, "pending_pipeline", None) is decision:
        runtime.pipeline_active_at = time.monotonic()


def release_official_pipeline(runtime, decision=None) -> bool:
    """结束官方流水线对会话的占用。

    给了 decision 时只释放这一轮：超时释放后，旧事件迟到的发送钩子不能结束下一轮。
    """
    if runtime is None:
        return False
    if decision is not None and getattr(runtime, "pending_pipeline", None) is not decision:
        return False
    runtime.pending_pipeline = None
    runtime.pipeline_event = None
    runtime.deciding = False
    runtime.pipeline_released.set()
    return True


def begin_proactive_round(runtime) -> None:
    runtime.proactive_running = True
    runtime.deciding = True
    runtime.proactive_released.clear()


def end_proactive_round(runtime) -> None:
    runtime.proactive_running = False
    runtime.deciding = False
    runtime.proactive_released.set()


def session_release_event(runtime):
    """返回占用会话的官方流水线或主动回合的结束信号。

    进行中的被动回合会自行合并新消息并重新判断，此时返回 None。
    """
    if getattr(runtime, "pending_pipeline", None) is not None:
        return runtime.pipeline_released
    if getattr(runtime, "proactive_running", False):
        return runtime.proactive_released
    return None
