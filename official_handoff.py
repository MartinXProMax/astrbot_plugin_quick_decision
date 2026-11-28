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


# 官方默认 Agent 认作输入的消息段：没有正文时，至少要有其中之一。
_DEFAULT_AGENT_INPUTS = frozenset({"Image", "File", "Record", "Video", "Reply"})


def _platform_name(event) -> str:
    getter = getattr(event, "get_platform_name", None)
    try:
        return str(getter() or "") if callable(getter) else ""
    except Exception:  # noqa: BLE001
        return ""


def has_default_agent_input(event) -> bool:
    """官方默认 Agent 跳过没有正文、也没有图片、文件、语音、视频或引用的消息，例如只 @ 了机器人。"""
    if (getattr(event, "message_str", "") or "").strip():
        return True
    comps = getattr(getattr(event, "message_obj", None), "message", None) or []
    return any(type(comp).__name__ in _DEFAULT_AGENT_INPUTS for comp in comps)


def provider_wake_prefix(config) -> str:
    """官方默认 Agent 要求的 LLM 唤醒前缀，去掉与机器人唤醒词重叠的开头（同 AgentRequestSubStage）。"""
    provider = config.get("provider_settings") or {}
    prefix = str(provider.get("wake_prefix") or "")
    for wake in config.get("wake_prefix") or []:
        if wake and prefix.startswith(wake):
            prefix = prefix[len(wake):]
    return prefix


def matches_provider_wake_prefix(event, prefix: str) -> bool:
    """WebChat 不要求 LLM 唤醒前缀；飞书先去掉开头空白再比较。"""
    platform = _platform_name(event)
    if not prefix or platform == "webchat":
        return True
    text = getattr(event, "message_str", "") or ""
    if platform == "lark":
        text = text.lstrip()
    return text.startswith(prefix)


async def _session_llm_enabled(event) -> bool:
    """AstrBot 4.29 的会话 LLM 开关；新版没有这个模块或读取失败时按开启处理。"""
    try:
        from astrbot.core.star.session_llm_manager import SessionServiceManager
    except Exception:  # noqa: BLE001
        return True
    check = getattr(SessionServiceManager, "should_process_llm_request", None)
    if not callable(check):
        return True
    try:
        return bool(await check(event))
    except Exception:  # noqa: BLE001
        logger.debug("读取会话 LLM 开关失败，按开启处理：%s", getattr(event, "unified_msg_origin", ""), exc_info=True)
        return True


async def official_skip_reason(event, config, *, default_agent: bool) -> str | None:
    """预判官方 Agent 不会处理本事件的原因；None 表示会处理。对应 AstrBot 4.29.4。

    插件提交的 request_llm 受 AI 能力总开关和会话 LLM 开关限制，本事件上已有别的模型请求时也不再提交；默认 Agent 还要求事件已唤醒、
    其他插件没有回复或接管、有正文或附件，并带上 LLM 唤醒前缀。官方不处理却占用会话时，
    要等超时才释放，期间本会话的消息都在排队。config 读不到时只检查会话开关和事件本身。
    """
    if config is not None and not (config.get("provider_settings") or {}).get("enable", True):
        return "AstrBot 未启用 AI 能力"
    if not await _session_llm_enabled(event):
        return "本会话已关闭 LLM"
    if not default_agent:
        # 优先级更高的处理器（如 AstrBot 概率主动回复）已在本事件上请求过模型，再提交一次会回复两遍。
        if event.get_extra("provider_request") is not None:
            return "其他插件已请求模型回复本条消息"
        return None
    if not default_official_agent_would_run(
        has_send_oper=bool(getattr(event, "_has_send_oper", False)),
        is_at_or_wake_command=bool(getattr(event, "is_at_or_wake_command", False)),
        call_llm=bool(getattr(event, "call_llm", False)),
        has_result=event.get_result() is not None,
        is_stopped=event.is_stopped(),
    ):
        return "其他插件已回复或接管本条消息"
    if not has_default_agent_input(event):
        return "消息没有正文或附件"
    if config is not None and not matches_provider_wake_prefix(event, provider_wake_prefix(config)):
        return "消息没有带 LLM 唤醒前缀"
    return None


def build_inbound_user_prompt(
    *,
    event_text: str,
    batch: Iterable[BufferedMessage],
    merged: bool,
) -> str:
    """构造交给官方 request_llm 的本轮用户文本。

    合并时按到达顺序带上本轮判断看过的全部用户消息（上一轮判断之后到达的），不带更早已回复过的消息。
    """
    if not merged:
        return (event_text or "").strip()
    parts: list[str] = []
    for item in batch:
        text = (item.text or "").strip()
        if not text:
            continue
        name = (item.sender_name or item.sender_id or "").strip()
        parts.append(f"{name}: {text}" if name else text)
    if parts:
        return "\n".join(parts)
    return (event_text or "").strip()


def group_context_enabled(config) -> bool:
    """AstrBot 内置「群聊上下文感知」是否开启；读不到配置时按未开启处理。"""
    if config is None:
        return False
    return bool((config.get("provider_ltm_settings") or {}).get("group_icl_enable"))


def split_batch_for_group_context(
    batch: list[BufferedMessage],
) -> tuple[list[BufferedMessage], str]:
    """合并回合交给「群聊上下文感知」时，返回仍写进提示词的消息和请求应指向的记录 id。

    群聊上下文感知在请求时注入本事件记录之前的群聊，只清掉到这条记录为止。合并回合的请求挂在第一条事件上，
    后面几条若也写进提示词，会留在记录里，下一次请求又被当作「上次回复之后」的群聊注入。改为指向本轮最后一条
    有记录、有正文的消息：更早的几条由它注入，提示词只写这条及之后的消息。没有这样的消息时原样返回整批、id 为空。
    """
    for index in range(len(batch) - 1, -1, -1):
        item = batch[index]
        if item.group_context_record_id and (item.text or "").strip():
            return list(batch[index:]), item.group_context_record_id
    return list(batch), ""


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

    被动回合之间由 runtime.lock 排队，不经过这里；没有流水线或主动回合占用时返回 None。
    """
    if getattr(runtime, "pending_pipeline", None) is not None:
        return runtime.pipeline_released
    if getattr(runtime, "proactive_running", False):
        return runtime.proactive_released
    return None
