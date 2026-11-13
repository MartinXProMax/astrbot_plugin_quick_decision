"""根据决策生成文字，并在文字发送成功后另发一条独立表情。"""

from __future__ import annotations

import json

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image, Reply
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.message import TextPart

from pathlib import Path
from typing import Protocol

from .models import DecisionResult, normalize_action


class ImageResolver(Protocol):
    """按精确 id 解析图片路径；缺文件时返回 None。"""

    def resolve_image(self, meme_id: str) -> Path | None: ...


def session_hint_text(decision: DecisionResult) -> str | None:
    """拼接本轮要告诉会话模型的临时提示；没有可用提示时返回 None。"""
    hints = [str(item).strip() for item in decision.session_hints if str(item).strip()]
    if not hints:
        return None
    return "\n".join(hints)


def apply_session_hints(req: ProviderRequest, decision: DecisionResult) -> None:
    """把本轮判断得到的会话提示放到 extra_user_content_parts，并标记为临时内容。"""
    text = session_hint_text(decision)
    if not text:
        return
    hint = TextPart(text=f"<quick_decision>\n{text}\n</quick_decision>").mark_as_temp()
    req.extra_user_content_parts.append(hint)


def meme_component(resolver: ImageResolver, decision: DecisionResult):
    if not decision.use_meme or not decision.meme_id:
        return None
    path = resolver.resolve_image(decision.meme_id)
    if path is None:
        logger.info("决策选择的表情包不可用，将退回纯文字：%s", decision.meme_id)
        return None
    return Image.fromFileSystem(str(path))


def append_meme_to_chain(
    chain: list, resolver: ImageResolver, decision: DecisionResult
) -> bool:
    component = meme_component(resolver, decision)
    if component is None:
        return False
    chain.append(component)
    return True


def build_send_chain(
    text: str,
    resolver: ImageResolver,
    decision: DecisionResult,
    *,
    quote_target: bool = True,
) -> MessageChain:
    """只构造文字链（含按开关决定的引用）。表情必须走独立消息。"""
    chain = MessageChain()
    if (
        quote_target
        and decision.target_message_id
        and normalize_action(decision.action) == "reply"
    ):
        try:
            chain.chain.append(Reply(id=decision.target_message_id))
        except Exception:  # noqa: BLE001
            logger.debug("无法添加引用消息段，将发送普通消息")
    if text:
        chain.message(text)
    return chain


def build_meme_chain(resolver: ImageResolver, decision: DecisionResult) -> MessageChain:
    """构造不含文字、默认不引用的独立表情消息；最多一张。缺图时为空链。"""
    chain = MessageChain()
    append_meme_to_chain(chain.chain, resolver, decision)
    return chain


def take_event_decision(event) -> DecisionResult | None:
    """取出并清除事件上的决策，保证官方 after-send 钩子至多处理一次。"""
    getter = getattr(event, "get_extra", None)
    if not callable(getter):
        return None
    decision = getter("quick_decision")
    if not isinstance(decision, DecisionResult):
        return None
    setter = getattr(event, "set_extra", None)
    if callable(setter):
        setter("quick_decision", None)
    return decision


def official_text_was_sent(event) -> bool:
    """尽所知判断官方本轮文字是否已发出。全部失败时 aiocqhttp 不会置位 _has_send_oper。"""
    if getattr(event, "_has_send_oper", None) is False:
        return False
    get_result = getattr(event, "get_result", None)
    if not callable(get_result):
        return False
    result = get_result()
    chain = getattr(result, "chain", None) if result is not None else None
    if not chain:
        return False
    text = "".join(
        str(getattr(comp, "text", "") or "")
        for comp in chain
        if str(getattr(comp, "text", "") or "").strip()
    ).strip()
    return bool(text)


async def _send_chain(context, umo: str, chain: MessageChain) -> bool:
    if not chain.chain:
        return False
    try:
        sent = await context.send_message(umo, chain)
    except Exception:
        logger.exception("向会话 %s 发送消息失败", umo)
        return False
    return sent is not False


async def send_meme_followup(
    context,
    *,
    umo: str,
    resolver: ImageResolver,
    decision: DecisionResult,
) -> bool:
    """在文字已经发出后，发送最多一张独立表情；不引用，失败不重试以免双图。"""
    if not decision.use_meme or not decision.meme_id:
        return False
    chain = build_meme_chain(resolver, decision)
    if not chain.chain:
        return False
    return await _send_chain(context, umo, chain)


def prepare_official_result_chain(
    chain: list,
    *,
    dry_run: bool,
    should_speak: bool,
    group_direct_send: bool,
    is_private: bool,
) -> bool:
    """整理官方即将发送的文字链：不附图；群聊 direct_send 时去掉引用。

    Returns:
        False 表示应取消发送（试运行或不发言）。
    """
    if dry_run or not should_speak:
        chain.clear()
        return False
    if group_direct_send and not is_private:
        strip_reply_components(chain)
    return True


def _is_reply_component(comp) -> bool:
    if isinstance(comp, Reply):
        return True
    return type(comp).__name__ == "Reply"


def strip_reply_components(chain: list | None) -> None:
    """从消息链中去掉引用段。"""
    if not chain:
        return
    chain[:] = [comp for comp in chain if not _is_reply_component(comp)]


def install_direct_send_send_guard(event) -> None:
    """在实际发送时去掉引用。

    官方 ResultDecorateStage 会在 on_decorating_result 钩子返回之后，才把
    reply_with_quote 的 Reply 插到链首。只在钩子里删一次不够，必须拦在 send。
    """
    if getattr(event, "_qd_direct_send_guard", False):
        return
    original = getattr(event, "send", None)
    if not callable(original):
        return

    async def _send_without_quote(message, *args, **kwargs):
        strip_reply_components(getattr(message, "chain", None))
        return await original(message, *args, **kwargs)

    try:
        event.send = _send_without_quote
        event._qd_direct_send_guard = True
    except Exception:  # noqa: BLE001
        logger.debug("无法为当前事件安装直接发送守卫，将只去掉装饰阶段已有的引用")


async def after_official_text_sent(
    event,
    *,
    context,
    resolver: ImageResolver,
    dry_run: bool = False,
    segmenter=None,
    sleeper=None,
) -> bool:
    """官方 after_message_sent 钩子：先清事件决策，文字确认发出后再发一张独立表情。

    若官方分段间隔启用，表情作为文字之后的独立一段，先按官方公式等待再发送。
    """
    decision = take_event_decision(event)
    if decision is None:
        return False
    if dry_run or not decision.should_speak():
        return False
    if not official_text_was_sent(event):
        return False
    from .official_segment import OfficialSegmenter, platform_name_from_event

    if segmenter is None:
        segmenter = await OfficialSegmenter.from_context(
            context,
            umo=getattr(event, "unified_msg_origin", None),
            sleeper=sleeper,
        )
    platform_name = platform_name_from_event(event)
    if segmenter.should_delay(is_llm_result=True, platform_name=platform_name):
        await segmenter.sleep(await segmenter.interval_for_image())
    return await send_meme_followup(
        context,
        umo=event.unified_msg_origin,
        resolver=resolver,
        decision=decision,
    )


def _history_as_contexts(history_raw: str | list | None) -> list[dict]:
    if not history_raw:
        return []
    if isinstance(history_raw, str):
        try:
            records = json.loads(history_raw)
        except json.JSONDecodeError:
            return []
    else:
        records = history_raw
    contexts: list[dict] = []
    for item in records:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if role not in {"system", "user", "assistant"}:
            continue
        contexts.append(item)
    return contexts


async def resolve_persona_prompt(context, umo: str, conversation) -> str:
    persona_mgr = context.persona_manager
    persona_id = getattr(conversation, "persona_id", None) if conversation else None
    if persona_id:
        try:
            persona = await persona_mgr.get_persona(persona_id)
            prompt = getattr(persona, "system_prompt", "") or ""
            if prompt:
                return prompt
        except Exception:  # noqa: BLE001
            logger.debug("会话绑定人格不可用，回退到默认人格")
    try:
        persona_v3 = await persona_mgr.get_default_persona_v3(umo)
    except Exception:  # noqa: BLE001
        return ""
    if isinstance(persona_v3, dict):
        return str(persona_v3.get("prompt") or "")
    return str(getattr(persona_v3, "prompt", "") or "")


async def generate_text(
    context,
    *,
    umo: str,
    decision: DecisionResult,
    user_prompt: str,
) -> str:
    """尽量沿用官方会话历史和人格，但不把这些内容传给判断模型。"""
    provider_id = await context.get_current_chat_provider_id(umo)
    conv_mgr = context.conversation_manager
    curr_cid = await conv_mgr.get_curr_conversation_id(umo)
    conversation = None
    if curr_cid:
        conversation = await conv_mgr.get_conversation(umo, curr_cid)
    elif hasattr(conv_mgr, "get_conversation"):
        try:
            conversation = await conv_mgr.get_conversation(
                umo, "", create_if_not_exists=True
            )
            curr_cid = getattr(conversation, "cid", None) if conversation else None
        except Exception:  # noqa: BLE001
            conversation = None
    system_prompt = await resolve_persona_prompt(context, umo, conversation)
    contexts = _history_as_contexts(
        getattr(conversation, "history", None) if conversation else None
    )
    prompt = user_prompt.strip() or "请根据当前对话自然地回复。"
    if decision.scene in {"group_proactive", "private_proactive"}:
        prompt = "会话已经沉默一段时间，请结合上下文主动开一个自然的话题或打个招呼。"
    extra = []
    hint_text = session_hint_text(decision)
    if hint_text:
        extra.append(
            TextPart(
                text=f"<quick_decision>\n{hint_text}\n</quick_decision>"
            ).mark_as_temp()
        )
    response = await context.llm_generate(
        chat_provider_id=provider_id,
        prompt=prompt,
        contexts=contexts or None,
        system_prompt=system_prompt or None,
        extra_user_content_parts=extra,
    )
    text = (getattr(response, "completion_text", None) or "").strip()
    if text and curr_cid:
        try:
            history = list(contexts)
            if decision.scene in {"group_proactive", "private_proactive"}:
                history.append({"role": "assistant", "content": text})
            else:
                history.append({"role": "user", "content": prompt})
                history.append({"role": "assistant", "content": text})
            await conv_mgr.update_conversation(umo, curr_cid, history=history)
        except Exception:  # noqa: BLE001
            logger.exception("写入官方会话历史失败")
    return text


async def send_decision_message(
    context,
    *,
    umo: str,
    text: str,
    resolver: ImageResolver,
    decision: DecisionResult,
    dry_run: bool,
    quote_target: bool = True,
    platform_name: str = "",
    segmenter=None,
    sleeper=None,
) -> bool:
    """按官方分段配置发送文字段，全部成功后再发独立表情。

    某一段失败时不重发已成功段，也不把整段原文再发一遍；未成功的剩余文字仍尝试发出
    （与官方 RespondStage 遇错继续后续段一致），但只有全部文字段成功才发表情。
    """
    if dry_run:
        logger.info("试运行模式：已跳过向会话 %s 发送消息", umo)
        return False
    if not text:
        return False
    from .official_segment import OfficialSegmenter

    if segmenter is None:
        segmenter = await OfficialSegmenter.from_context(
            context, umo=umo, sleeper=sleeper
        )
    segments = segmenter.split_text(
        text, is_llm_result=True, platform_name=platform_name
    )
    if not segments:
        return False
    delay = segmenter.should_delay(is_llm_result=True, platform_name=platform_name)
    quote_this = quote_target
    all_text_ok = True
    any_text_sent = False
    for seg in segments:
        if delay:
            await segmenter.sleep(await segmenter.interval_for_plain(seg))
        text_chain = build_send_chain(
            seg, resolver, decision, quote_target=quote_this
        )
        quote_this = False
        if not text_chain.chain:
            all_text_ok = False
            continue
        if await _send_chain(context, umo, text_chain):
            any_text_sent = True
        else:
            all_text_ok = False
    if not any_text_sent or not all_text_ok:
        return any_text_sent
    if delay:
        await segmenter.sleep(await segmenter.interval_for_image())
    await send_meme_followup(
        context,
        umo=umo,
        resolver=resolver,
        decision=decision,
    )
    return True
