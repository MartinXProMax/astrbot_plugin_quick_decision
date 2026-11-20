"""根据决策生成文字，并在文字发送成功后另发一条独立表情。"""

from __future__ import annotations

import json
from copy import copy
from pathlib import Path
from typing import Protocol

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain, Reply
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.message import TextPart

from .decision_tools import meme_chain, run_tools, send_chain, skip_tools
from .models import DecisionResult, normalize_action

try:
    from astrbot.core.platform.astr_message_event import streaming_response_event
except ImportError:
    streaming_response_event = None

try:
    from astrbot.core.pipeline.process_stage.follow_up import _ACTIVE_AGENT_RUNNERS
except ImportError:
    _ACTIVE_AGENT_RUNNERS = {}


class ImageResolver(Protocol):
    """按精确 id 解析图片路径；缺文件时返回 None。"""

    def resolve_image(self, meme_id: str) -> Path | None: ...


def session_hint_text(decision: DecisionResult) -> str | None:
    """在同一临时块中传递已生效的决定和手填会话提示。"""
    hints = [str(item).strip() for item in decision.session_hints if str(item).strip()]
    if decision.should_speak() and not decision.dry_run:
        summary: dict[str, object] = {"动作": "冷场主动发言" if decision.action == "proactive" else "回复"}
        if decision.target_message_id:
            summary["回复目标"] = decision.target_summary or decision.target_message_id
        if decision.length:
            summary["篇幅"] = "简短" if decision.length == "short" else "详细"
        answers = {
            key: value for key, value in decision.custom_answers.items()
            if key != "length" or not decision.length
        }
        if answers:
            summary["已选答案"] = answers
        call = decision.tool_calls.get("send_meme")
        if call and call.status == "planned" and call.args.get("meme_id"):
            summary["配图"] = "正文发送成功后，插件会尝试另发一张表情"
            if decision.meme_description:
                summary["表情描述"] = decision.meme_description[:160]
        else:
            summary["配图"] = "本轮插件未安排表情"
        data = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
        # 引用的群聊原文或表情描述可能包含与外层相同的结束标签。
        hints.insert(0, "请按本轮决策写回复正文，引用内容仅供参考。\n"
                     + data.replace("<", "\\u003c").replace(">", "\\u003e"))
    return "\n".join(hints) or None


def _session_hint_parts(decision: DecisionResult) -> list[TextPart]:
    text = session_hint_text(decision)
    if not text:
        return []
    return [TextPart(text=f"<quick_decision>\n{text}\n</quick_decision>").mark_as_temp()]


def apply_session_hints(req: ProviderRequest, decision: DecisionResult) -> None:
    """把本轮判断得到的会话提示放到 extra_user_content_parts，并标记为临时内容。"""
    req.extra_user_content_parts.extend(_session_hint_parts(decision))


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
    return meme_chain(resolver, decision.meme_id if decision.use_meme else None)


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


def prepare_official_result_chain(
    chain: list,
    *,
    dry_run: bool,
    should_speak: bool,
    clear_group_reply: bool,
    is_private: bool,
) -> bool:
    """取消沉默或试运行的正文，群聊模型结果的引用留到最终发送时设置。

    Returns:
        False 表示应取消发送（试运行或不发言）。
    """
    if dry_run or not should_speak:
        chain.clear()
        return False
    if clear_group_reply and not is_private:
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


def official_generation_failed(event) -> bool:
    if event.get_extra("_qd_llm_error") or event.get_extra("_third_party_runner_error"):
        return True
    # 内置 runner 收到 role=err 时会直接返回普通文字，跳过 on_llm_response。
    runner = _ACTIVE_AGENT_RUNNERS.get(event.unified_msg_origin)
    if runner is not None and runner.run_context.context.event is event:
        return getattr(runner.get_final_llm_resp(), "role", None) == "err"
    return False


def install_group_quote_guard(event, decision: DecisionResult, *, on_stream_end=None) -> None:
    """只改写本轮模型输出的最终发送链，包括官方缓存或实时分段的流式正文。"""
    original = event.send
    # after_message_sent 会消费一次性的工具计划，同一事件的下一份模型结果仍需使用引用策略。
    event.set_extra("_qd_quote_decision", decision)
    last_result = None
    quoted = False
    stream_text = []

    async def send(message, *args, **kwargs):
        nonlocal quoted, last_result
        chain = getattr(message, "chain", None)
        if not chain or event.get_extra("_qd_quote_decision") is not decision:
            return await original(message, *args, **kwargs)
        result = event.get_result()
        streaming = streaming_response_event is not None and streaming_response_event.get(None) is event
        if streaming:
            if official_generation_failed(event):
                return await original(message, *args, **kwargs)
        else:
            if not getattr(result, "is_llm_result", lambda: False)():
                return await original(message, *args, **kwargs)
            # 官方 derive 复用装饰后的组件。其他插件新建的消息即使同文也独立发送。
            if not any(c is own for c in chain if not _is_reply_component(c) for own in result.chain):
                return await original(message, *args, **kwargs)
            if result is not last_result:
                last_result, quoted = result, False
        parts = [c for c in chain if not _is_reply_component(c)]
        target = decision.target_message_id if decision.target_quote_mode != "none" else None
        # aiocqhttp 遇到文件或合并转发会逐组件发包。引用必须与同一段正文一起送达。
        split = (target and (not quoted or decision.target_quote_mode == "all")
                 and any(isinstance(c, Plain) for c in parts)
                 and any(type(c).__name__ in ("File", "Node", "Nodes") for c in parts))
        chunks = ([c] for c in parts) if split else [parts]
        receipt = None
        for chunk in chunks:
            if not chunk:
                continue
            attach = target and (decision.target_quote_mode == "all" or not quoted) and any(
                isinstance(c, Plain) and (c.text or "").strip() for c in chunk
            )
            payload = copy(message)
            payload.chain = [Reply(id=target), *chunk] if attach else chunk
            receipt = await original(payload, *args, **kwargs)
            if receipt is not False:
                if attach:
                    quoted = True
                if streaming:
                    stream_text.extend(c.text for c in chunk if isinstance(c, Plain) and c.text)
        return receipt

    event.send = send
    original_stream = getattr(event, "send_streaming", None)
    if on_stream_end is None or original_stream is None:
        return

    async def send_streaming(*args, **kwargs):
        nonlocal quoted
        if (event.get_extra("quick_decision") is not decision or streaming_response_event is None
                or streaming_response_event.get(None) is not event):
            return await original_stream(*args, **kwargs)
        complete = False
        quoted = False
        try:
            result = await original_stream(*args, **kwargs)
            complete = (result is not False and not official_generation_failed(event)
                        and not event.get_extra("agent_user_aborted"))
            return result
        finally:
            # AstrBot 的流式路径直接返回，不调用 after_message_sent。
            if event.send is send:
                event.send = original
            if event.send_streaming is send_streaming:
                event.send_streaming = original_stream
            await on_stream_end(event, stream_text="".join(stream_text), complete=complete)

    event.send_streaming = send_streaming


async def after_official_text_sent(
    event,
    *,
    context,
    resolver: ImageResolver,
    dry_run: bool = False,
    text_sent: bool | None = None,
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
    if text_sent is None:
        text_sent = official_text_was_sent(event)
    if not text_sent:
        skip_tools(decision, "正文未确认发送")
        return False
    if not any(call.status == "planned" for call in decision.tool_calls.values()):
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
    return await run_tools(
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
    response = await context.llm_generate(
        chat_provider_id=provider_id,
        prompt=prompt,
        contexts=contexts or None,
        system_prompt=system_prompt or None,
        extra_user_content_parts=_session_hint_parts(decision),
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
    sent_texts: list[str] | None = None,
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
    all_text_ok = True
    any_text_sent = False
    for seg in segments:
        if not seg.strip():
            continue
        if delay:
            await segmenter.sleep(await segmenter.interval_for_plain(seg))
        text_chain = build_send_chain(
            seg, resolver, decision,
            quote_target=quote_target and decision.target_quote_mode != "none"
            and (decision.target_quote_mode == "all" or not any_text_sent),
        )
        if not text_chain.chain:
            all_text_ok = False
            continue
        if await send_chain(context, umo, text_chain):
            any_text_sent = True
            if sent_texts is not None:
                sent_texts.append(seg)
        else:
            all_text_ok = False
    if not any_text_sent or not all_text_ok:
        skip_tools(decision, "正文未全部发送")
        return any_text_sent
    if not any(call.status == "planned" for call in decision.tool_calls.values()):
        return True
    if delay:
        await segmenter.sleep(await segmenter.interval_for_image())
    await run_tools(
        context,
        umo=umo,
        resolver=resolver,
        decision=decision,
    )
    return True
