"""从消息事件中提取判断所需的精简信息。"""

from __future__ import annotations

import time
from collections.abc import Mapping

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, AtAll, Image, Plain, Reply
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter

from .models import BufferedMessage, SessionProfile


def sender_id(event: AstrMessageEvent) -> str:
    sid = event.get_sender_id()
    if sid:
        return str(sid)
    sender = getattr(event.message_obj, "sender", None)
    uid = getattr(sender, "user_id", None) if sender else None
    return str(uid) if uid is not None and str(uid) != "" else ""


def is_self_message(event: AstrMessageEvent) -> bool:
    self_id = str(event.get_self_id() or "")
    current = sender_id(event)
    return bool(self_id and current and self_id == current)


def event_profile(event: AstrMessageEvent) -> SessionProfile:
    umo = event.unified_msg_origin
    parts = str(umo).split(":", 2)
    platform_id = parts[0] if parts else ""
    session_id = parts[2] if len(parts) == 3 else event.get_session_id()
    return SessionProfile(
        umo=umo,
        is_private=event.is_private_chat(),
        platform_id=platform_id,
        session_id=str(session_id or ""),
        bot_id=str(event.get_self_id() or ""),
    )


def event_session_name(event: AstrMessageEvent) -> str:
    """返回适合日志展示的会话名称，取不到时留空让调用方回退。"""
    if event.is_private_chat():
        return str(event.get_sender_name() or "").strip()
    message_obj = getattr(event, "message_obj", None)
    group = getattr(message_obj, "group", None)
    return str(getattr(group, "group_name", "") or getattr(message_obj, "group_name", "") or "").strip()


def _plain_text(components: list | None) -> str:
    if not components:
        return ""
    parts: list[str] = []
    for comp in components:
        if isinstance(comp, Plain):
            text = getattr(comp, "text", None) or str(comp)
            if text:
                parts.append(text)
        elif isinstance(comp, Image):
            parts.append("[图片]")
        elif isinstance(comp, At):
            qq = getattr(comp, "qq", "")
            name = getattr(comp, "name", "") or qq
            parts.append(f"@{name}")
    return "".join(parts).strip()


def inspect_mentions(event: AstrMessageEvent) -> tuple[bool, bool, Reply | None]:
    """返回 (是否 @ 机器人, 是否引用机器人, 引用组件)。"""
    self_id = str(event.get_self_id() or "")
    is_at = False
    is_quote = False
    reply: Reply | None = None
    for comp in event.get_messages() or []:
        if isinstance(comp, At) and not isinstance(comp, AtAll):
            if self_id and str(comp.qq) == self_id:
                is_at = True
        elif isinstance(comp, Reply):
            reply = comp
            if self_id and str(comp.sender_id) == self_id:
                is_quote = True
    return is_at, is_quote, reply


def event_timestamp(event: AstrMessageEvent) -> float:
    ts = getattr(event.message_obj, "timestamp", None)
    try:
        value = float(ts or 0)
    except (TypeError, ValueError):
        value = 0.0
    return value or time.time()


def to_buffered_message(event: AstrMessageEvent) -> BufferedMessage:
    is_at, is_quote, reply = inspect_mentions(event)
    text = (event.message_str or "").strip() or _plain_text(event.get_messages())
    quote_id = str(reply.id) if reply and reply.id is not None else ""
    quote_sender = str(reply.sender_id) if reply and reply.sender_id else ""
    quote_text = ""
    if reply:
        quote_text = (reply.message_str or reply.text or "").strip()
        if not quote_text:
            quote_text = _plain_text(reply.chain)
    # aiocqhttp 的 message_obj.timestamp 是适配器处理时间，原事件 time 才是发送时间。
    raw = getattr(event.message_obj, "raw_message", None)
    sent_at = raw.get("time") if isinstance(raw, Mapping) else None
    if type(sent_at) is not int or not 0 < sent_at <= time.time():
        sent_at = None
    return BufferedMessage(
        message_id=str(getattr(event.message_obj, "message_id", "") or ""),
        sender_id=sender_id(event),
        sender_name=event.get_sender_name() or "",
        role="user",
        text=text,
        is_at_bot=is_at,
        is_quote_bot=is_quote,
        quote_id=quote_id,
        quote_sender_id=quote_sender,
        quote_text=quote_text[:80],
        timestamp=event_timestamp(event),
        sent_at=sent_at,
    )


def event_has_command_handler(event: AstrMessageEvent) -> bool:
    """若本轮还激活了指令处理器，则不要当作闲聊去判断。"""
    handlers = event.get_extra("activated_handlers") or []
    for handler in handlers:
        filters = getattr(handler, "event_filters", None) or []
        if any(
            isinstance(item, (CommandFilter, CommandGroupFilter)) for item in filters
        ):
            return True
    return False


def extract_images(event: AstrMessageEvent) -> list[Image]:
    images: list[Image] = []
    for comp in event.get_messages() or []:
        if isinstance(comp, Image):
            images.append(comp)
        elif isinstance(comp, Reply) and comp.chain:
            for inner in comp.chain:
                if isinstance(inner, Image):
                    images.append(inner)
    return images


def remainder_after_command(event: AstrMessageEvent, *keywords: str) -> str:
    """从 message_str 中截取指定子指令后的剩余文本。"""
    text = (event.message_str or "").strip()
    tokens = text.split()
    if not tokens:
        return ""
    lowered_keywords = {item.lower() for item in keywords}
    for index, token in enumerate(tokens):
        if token.lower() in lowered_keywords:
            return " ".join(tokens[index + 1 :]).strip()
    return text
