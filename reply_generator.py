"""根据决策生成文字，并在文字发送成功后另发一条独立表情。"""

from __future__ import annotations

import json
from contextlib import nullcontext
from copy import copy
from pathlib import Path
from typing import Protocol

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain, Reply
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.message import TextPart

from .decision_engine import _TIME_TOKEN, _question_info
from .decision_tools import run_tools, send_chain, skip_tools
from .models import DecisionResult
from .tts_adapter import has_record

try:
    from astrbot.core.platform.astr_message_event import streaming_response_event
except ImportError:
    streaming_response_event = None

try:
    from astrbot.core.pipeline.process_stage.follow_up import _ACTIVE_AGENT_RUNNERS
except ImportError:
    _ACTIVE_AGENT_RUNNERS = {}

try:
    # 官方主 Agent 在整轮请求（含写回历史）期间持有的会话锁。
    from astrbot.core.utils.session_lock import session_lock_manager
except ImportError:
    session_lock_manager = None

try:
    # 内置「群聊上下文感知」注入暂存群聊时用的格式（AstrBot 4.29.4）。
    from astrbot.builtin_stars.astrbot.group_chat_context import _format_group_history_block
except ImportError:
    _format_group_history_block = None


class ImageResolver(Protocol):
    """按精确 id 解析图片路径；缺文件时返回 None。"""

    def resolve_image(self, meme_id: str) -> Path | None: ...


def session_hint_text(decision: DecisionResult) -> str | None:
    """在同一临时块中传递已生效的决定和手填会话提示。"""
    hints = [str(item).strip() for item in decision.session_hints if str(item).strip()]
    if decision.should_speak() and not decision.dry_run:
        summary: dict[str, object] = {"动作": "冷场主动发言" if decision.action == "proactive" else "回复"}
        if decision.target_summary:
            summary["回复目标"] = decision.target_summary
        if decision.context_fields:
            summary["本轮资料"] = decision.context_fields
        elif decision.length:
            summary["篇幅"] = "简短" if decision.length == "short" else "详细"
        if decision.custom_answers:
            summary["已选答案"] = decision.custom_answers
        call = next((c for c in decision.tool_calls.values() if c.when == "after_send" and c.status == "planned"), None)
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
        and decision.should_speak()
    ):
        try:
            chain.chain.append(Reply(id=decision.target_message_id))
        except Exception:  # noqa: BLE001
            logger.debug("无法添加引用消息段，将发送普通消息")
    if text:
        chain.message(text)
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
    receipt = event.get_extra("_qd_text_receipt") if hasattr(event, "get_extra") else None
    if receipt is not None:
        return receipt
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


def active_event_runner(event):
    runner = _ACTIVE_AGENT_RUNNERS.get(event.unified_msg_origin)
    if runner is not None and runner.run_context.context.event is event:
        return runner
    return None


def official_generation_failed(event) -> bool:
    if event.get_extra("_qd_llm_error") or event.get_extra("_third_party_runner_error"):
        return True
    # 内置 runner 收到 role=err 时会直接返回普通文字，跳过 on_llm_response。
    runner = active_event_runner(event)
    return runner is not None and getattr(runner.get_final_llm_resp(), "role", None) == "err"


def install_group_quote_guard(event, decision: DecisionResult, *, on_stream_end=None, private=False,
                              on_activity=None) -> None:
    """只改写本轮模型输出的最终发送链，包括官方缓存或实时分段的流式正文。

    on_activity 在本事件每次发送时调用，告诉插件官方流水线仍在运行。
    """
    original = event.send
    # after_message_sent 会消费一次性的工具计划，同一事件的下一份模型结果仍需使用引用策略。
    event.set_extra("_qd_quote_decision", decision)
    last_result = None
    quoted = False
    stream_text = []

    async def deliver(payload, streaming, args, kwargs):
        voice = has_record(payload.chain)
        try:
            receipt = await original(payload, *args, **kwargs)
        except Exception:
            # 语音发送抛错时通用 TTS 会改发原文，是否送达以之后的回执为准。
            if not voice:
                decision.text_sent = False
            raise
        if receipt is False:
            decision.text_sent = False
        else:
            delivered = "".join(c.text for c in payload.chain if isinstance(c, Plain) and c.text)
            if voice and not delivered.strip():
                # 通用 TTS 把整段文字换成了一条语音，记录时仍用原文。
                delivered = event.get_extra("_qd_undecorated_text") or ""
                decision.voice_sent = True
            if streaming:
                stream_text.append(delivered)
            elif delivered.strip():
                event.set_extra("_qd_text_receipt", True)
                event.set_extra("_qd_sent_text", (event.get_extra("_qd_sent_text", "") + delivered)[:80])
        return receipt

    async def send(message, *args, **kwargs):
        nonlocal quoted, last_result
        if on_activity is not None:
            on_activity()
        chain = getattr(message, "chain", None)
        if not chain or event.get_extra("_qd_quote_decision") is not decision:
            return await original(message, *args, **kwargs)
        result = event.get_result()
        streaming = streaming_response_event is not None and streaming_response_event.get(None) is event
        if streaming:
            if official_generation_failed(event):
                return await original(message, *args, **kwargs)
        else:
            # 通用 TTS 跳过一条回复时会把它标成普通结果，装饰钩子记下的仍是本轮模型结果。
            own_result = result is not None and result is event.get_extra("_qd_llm_result")
            if not own_result and not getattr(result, "is_llm_result", lambda: False)():
                return await original(message, *args, **kwargs)
            # 官方 derive 复用装饰后的组件。其他插件新建的消息即使同文也独立发送。
            # 语音发送失败时，通用 TTS 改发的是装饰钩子当时的原始组件。
            own = [*result.chain, *(event.get_extra("_qd_llm_components") or ())]
            if not any(c is item for c in chain if not _is_reply_component(c) for item in own):
                return await original(message, *args, **kwargs)
            if result is not last_result:
                last_result, quoted = result, False
        if private and not decision.target_message_id:
            return await deliver(message, streaming, args, kwargs)
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
            receipt = await deliver(payload, streaming, args, kwargs)
            if attach and receipt is not False:
                quoted = True
        return receipt

    event.send = send
    original_stream = getattr(event, "send_streaming", None)
    if on_stream_end is None or original_stream is None:
        return

    async def send_streaming(*args, **kwargs):
        nonlocal quoted
        if on_activity is not None:
            on_activity()
        if (event.get_extra("quick_decision") is not decision or streaming_response_event is None
                or streaming_response_event.get(None) is not event):
            return await original_stream(*args, **kwargs)
        complete = False
        quoted = False
        event.set_extra("_qd_streaming", True)
        try:
            result = await original_stream(*args, **kwargs)
            complete = (result is not False and decision.text_sent is not False and not official_generation_failed(event)
                        and not event.get_extra("agent_user_aborted"))
            return result
        finally:
            event.set_extra("_qd_streaming", False)
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
    decision.text_sent = bool(text_sent) and decision.text_sent is not False
    if not decision.text_sent:
        skip_tools(decision, "正文未确认发送", when="after_send")
        return False
    from .official_segment import platform_name_from_event
    return await send_followup_tools(context, event.unified_msg_origin, resolver, decision,
                                     platform_name_from_event(event), segmenter=segmenter, sleeper=sleeper)


async def send_followup_tools(context, umo, resolver, decision, platform_name, *, segmenter=None, sleeper=None):
    from .official_segment import OfficialSegmenter

    async with decision.followup_lock:
        if not any(c.status == "planned" and c.when == "after_send" for c in decision.tool_calls.values()):
            return False
        if segmenter is None:
            segmenter = await OfficialSegmenter.from_context(context, umo=umo, sleeper=sleeper)
        if segmenter.should_delay(is_llm_result=True, platform_name=platform_name):
            await segmenter.sleep(await segmenter.interval_for_image())
        return await run_tools(context, umo=umo, resolver=resolver, decision=decision)


def _history_records(history_raw: str | list | None) -> list:
    """官方会话历史原样解析成列表；写回时以它为底，不能丢掉其中的工具调用轮次。"""
    if not history_raw:
        return []
    if isinstance(history_raw, str):
        try:
            records = json.loads(history_raw)
        except json.JSONDecodeError:
            return []
    else:
        records = history_raw
    return list(records) if isinstance(records, list) else []


_TOOL_CALL_KEYS = frozenset({"tool_calls", "function_call"})


def _history_as_contexts(history_raw: str | list | None) -> list[dict]:
    """把官方历史转成自生成请求的 contexts。

    自生成请求不带工具定义，所以工具调用轮次要整体去掉：丢弃 tool 消息的同时，也要去掉
    assistant 消息上的 tool_calls。只丢 tool 消息会留下没有结果的工具调用，Anthropic 等接口
    会直接返回 400（missing tool_result for preceding tool_use）。
    """
    contexts: list[dict] = []
    for item in _history_records(history_raw):
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if role not in {"system", "user", "assistant"}:
            continue
        if role == "assistant" and _TOOL_CALL_KEYS & item.keys():
            item = {k: v for k, v in item.items() if k not in _TOOL_CALL_KEYS}
            content = item.get("content")
            if not (content.strip() if isinstance(content, str) else content):
                continue
        contexts.append(item)
    return contexts


async def resolve_persona(context, umo: str, conversation, platform_name: str = "") -> tuple[str, list[dict]]:
    """按官方主 Agent 的规则取本会话生效的人格提示词和预设对话。

    依次看会话管理里强制指定的人格、对话自己的人格（``[%None]`` 表示已取消人格）和配置的默认人格，
    与 AstrBot 4.29.4 ``PersonaManager.resolve_selected_persona`` 一致。预设对话只取角色和正文，
    不带框架的 ``_no_save`` 标记。
    """
    resolver = getattr(getattr(context, "persona_manager", None), "resolve_selected_persona", None)
    if not callable(resolver):
        return "", []
    try:
        _, persona, _, _ = await resolver(
            umo=umo,
            conversation_persona_id=getattr(conversation, "persona_id", None) if conversation else None,
            platform_name=platform_name,
        )
    except Exception:  # noqa: BLE001
        logger.warning("无法读取本会话人格，本轮主动发言不带人格：%s", umo, exc_info=True)
        return "", []
    if not persona:
        return "", []
    dialogs = [
        {"role": item["role"], "content": item["content"]}
        for item in persona.get("_begin_dialogs_processed") or []
        if isinstance(item, dict) and item.get("role") in {"user", "assistant"} and "content" in item
    ]
    return str(persona.get("prompt") or ""), dialogs


def _group_chat_context(context):
    """内置「群聊上下文感知」的实例。依赖 4.29.4 内部属性，找不到时为 None。"""
    if _format_group_history_block is None:
        return None
    for star in getattr(context, "get_all_stars", list)() or []:
        gcc = getattr(getattr(star, "star_cls", None), "group_chat_context", None)
        if all(hasattr(gcc, name) for name in ("raw_records", "_record_ids", "_get_lock")):
            return gcc
    return None


async def _peek_group_context(gcc, umo: str) -> tuple[list[str], list[str]]:
    """读取官方暂存、尚未注入的群聊（上次回复之后的消息）及其记录 id，不清除。"""
    async with gcc._get_lock(umo):
        return list(gcc.raw_records.get(umo, ())), list(gcc._record_ids.get(umo, ()))


async def _drop_group_context(gcc, umo: str, record_ids: list[str]) -> None:
    """清掉已随本轮发言写进历史的群聊记录；生成期间新到的记录留给下一次请求注入。"""
    taken = set(record_ids)
    async with gcc._get_lock(umo):
        records, ids = gcc.raw_records.get(umo), gcc._record_ids.get(umo)
        if not records or ids is None or len(records) != len(ids):
            return
        kept = [(record, rid) for record, rid in zip(records, ids) if rid not in taken]
        records.clear()
        ids.clear()
        records.extend(record for record, _ in kept)
        ids.extend(rid for _, rid in kept)


async def _append_history(conv_mgr, umo: str, cid: str, original: list, added: list[dict]) -> bool:
    """生成期间官方历史可能已被改动，重新读取后只在原历史之上追加；返回是否已写入。

    对话已切换（``/new``、``/switch``），或历史不再以生成前读到的内容开头（``/reset`` 清空、压缩改写）时放弃写回，
    不用旧副本盖掉别人的改动。持有官方主 Agent 的会话锁，不与同会话正在保存的官方回合交错。
    """
    lock = session_lock_manager.acquire_lock(umo) if session_lock_manager is not None else nullcontext()
    async with lock:
        current = await conv_mgr.get_curr_conversation_id(umo)
        if current and current != cid:
            logger.info("生成期间已切换对话，本轮发言不写入官方历史：%s", umo)
            return False
        latest = await conv_mgr.get_conversation(umo, cid)
        history = _history_records(getattr(latest, "history", None)) if latest is not None else None
        if history is None or history[:len(original)] != original:
            logger.info("生成期间官方历史已被清空或改写，本轮发言不写入官方历史：%s", umo)
            return False
        await conv_mgr.update_conversation(umo, cid, history=[*history, *added])
        return True


async def generate_text(
    context,
    *,
    umo: str,
    decision: DecisionResult,
    platform_name: str = "",
    group_context: bool = False,
) -> str:
    """生成冷场或定时发言：尽量沿用官方会话历史和人格，但不把这些内容传给判断模型。

    group_context 为 True（群聊且开启了「群聊上下文感知」）时，像官方请求一样带上上次回复之后暂存的群聊，
    并随发言写进历史、从暂存中清掉；否则下一次官方请求会把发言之前的群聊当作发言之后的注入。
    """
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
    system_prompt, begin_dialogs = await resolve_persona(context, umo, conversation, platform_name)
    history_raw = getattr(conversation, "history", None) if conversation else None
    # 官方主 Agent 把人格预设对话放在历史之前。
    contexts = [*begin_dialogs, *_history_as_contexts(history_raw)]
    gcc = _group_chat_context(context) if group_context else None
    records, record_ids = await _peek_group_context(gcc, umo) if gcc is not None else ([], [])
    group_block = _format_group_history_block(records) if records else ""
    # 不经过官方主 Agent，拿不到它注入的时间，这里自己给。
    now = _question_info(context, umo, decision.scene, None)[_TIME_TOKEN]
    prompt = f"当前时间：{now}。" + ("本轮由定时任务触发，请结合当前时间、会话上下文和判断提示自然地发言。"
                                    if decision.trigger_event == "timer_elapsed" else
                                    "会话已经沉默一段时间，请结合上下文主动开一个自然的话题或打个招呼。")
    parts = _session_hint_parts(decision)
    if group_block:
        parts.insert(0, TextPart(text=group_block))
    response = await context.llm_generate(
        chat_provider_id=provider_id,
        prompt=prompt,
        contexts=contexts or None,
        system_prompt=system_prompt or None,
        extra_user_content_parts=parts,
    )
    text = (getattr(response, "completion_text", None) or "").strip()
    if text and curr_cid:
        try:
            # 以原始历史为底追加，保留官方的工具调用轮次；用过滤后的 contexts 写回会把
            # tool 消息永久删掉，之后官方流水线也会因 tool_calls 缺少结果而报 400。
            # 生成提示不写进历史；暂存群聊和官方一样随用户消息保存，排在这句发言之前。
            added = [{"role": "user", "content": group_block}] if group_block else []
            added.append({"role": "assistant", "content": text})
            written = await _append_history(conv_mgr, umo, curr_cid, _history_records(history_raw), added)
            if written and record_ids:
                await _drop_group_context(gcc, umo, record_ids)
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
    voice=None,
    voice_fallback: bool = True,
) -> bool:
    """按官方分段配置发送文字段，全部成功后再发独立表情。

    某一段失败时不重发已成功段，也不把整段原文再发一遍；未成功的剩余文字仍尝试发出
    （与官方 RespondStage 遇错继续后续段一致），但只有全部文字段成功才发表情。

    给了 voice（正文 -> 语音组件或 None）时先尝试把整段正文作为一条语音发出，不分段、不引用；
    语音送达就不再发文字。语音发送失败时按 voice_fallback 决定是否改发文字。
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
    record = await voice(text) if voice is not None else None
    if record is not None:
        chain = MessageChain()
        chain.chain.append(record)
        if await send_chain(context, umo, chain):
            decision.text_sent = decision.voice_sent = True
            if sent_texts is not None:
                sent_texts.append(text)
            await send_followup_tools(context, umo, resolver, decision, platform_name, segmenter=segmenter)
            return True
        if not voice_fallback:
            decision.text_sent = False
            skip_tools(decision, "语音未发送", when="after_send")
            return False
        logger.warning("语音发送失败，改发文字：%s", umo)
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
    decision.text_sent = any_text_sent and all_text_ok
    if not decision.text_sent:
        skip_tools(decision, "正文未全部发送", when="after_send")
        return any_text_sent
    await send_followup_tools(context, umo, resolver, decision, platform_name, segmenter=segmenter)
    return True
