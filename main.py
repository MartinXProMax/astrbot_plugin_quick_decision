"""快速决策插件入口。"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from uuid import uuid4

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.platform import MessageType
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star

from .candidate_sources import candidate_type
from .config_util import PluginSettings
from .decision_engine import (
    _round_clock,
    ask_decision,
    evaluate_post_reply,
    has_on_match_actions,
)
from .decision_tools import run_tools, skip_tools
from .event_utils import (
    GROUP_CONTEXT_RECORD_KEY,
    event_has_command_handler,
    event_profile,
    event_session_name,
    inspect_mentions,
    is_self_message,
    to_buffered_message,
)
from .meme_adapter import UNAVAILABLE_HINT, MemeManagerAdapter
from .models import BufferedMessage, DecisionResult, SceneType, SessionProfile, decision_log_context
from .official_handoff import (
    begin_official_pipeline,
    begin_proactive_round,
    build_inbound_user_prompt,
    collect_event_media_urls,
    end_proactive_round,
    group_context_enabled,
    is_official_pipeline_busy,
    make_official_llm_request,
    official_skip_reason,
    release_official_pipeline,
    resolve_session_conversation,
    session_release_event,
    should_use_default_official_agent,
    split_batch_for_group_context,
    suppress_default_official_agent,
    touch_official_pipeline,
)
from .official_segment import platform_name_from_event
from .plugin_paths import load_plugin_data_dir
from .reply_generator import (
    active_event_runner,
    after_official_text_sent,
    apply_session_hints,
    generate_text,
    install_group_quote_guard,
    official_generation_failed,
    official_text_was_sent,
    prepare_official_result_chain,
    send_decision_message,
    send_followup_tools,
)
from .session_store import SessionStore
from .tts_adapter import UniversalTTSAdapter, has_record
from .web_api import WebApiMixin

PLUGIN_NAME = "astrbot_plugin_quick_decision"
LOG_TEXT_LIMIT = 120
# 同时进行的定时与冷场回合上限；超出的会话保持到期，留到下次检查。
PROACTIVE_CONCURRENCY = 4
# 合成语音前最多等多久正文生成后的语气判断；超时就用引擎默认语气。
VOICE_TONE_WAIT_SECONDS = 15
# 官方流水线最后一次有动静后（官方 Agent 仍在运行，或插件钩子、发送被调用），最多等多久发送完成钩子；
# 超时视为本轮已结束并释放会话。AstrBot 在结果为空、正文为空或只剩引用段等情况下直接结束发送阶段，
# 不调用 after_message_sent。
PIPELINE_GRACE_SECONDS = 180
# 官方 Agent 一直显示在运行时，占用会话的上限。
PIPELINE_MAX_SECONDS = 1800


def _log_text(value: str, limit: int = LOG_TEXT_LIMIT) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _next_timer_at(trigger, now, zone):
    if "interval_seconds" in trigger:
        return now + trigger["interval_seconds"]
    hour, minute = map(int, trigger["daily_time"].split(":"))
    next_time = datetime.fromtimestamp(now, zone).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if next_time.timestamp() <= now:
        next_time += timedelta(days=1)
    return next_time.timestamp()


class QuickDecisionPlugin(Star, WebApiMixin):
    """利用官方判断提供商统一决策是否发言、回复策略和配图。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.data_dir = load_plugin_data_dir(PLUGIN_NAME)
        self.meme_adapter = MemeManagerAdapter(self.context)
        self.tts_adapter = UniversalTTSAdapter(self.context)
        self.sessions = SessionStore(
            self.data_dir, log_limit=self.settings.decision_log_limit
        )
        self._silence_task: asyncio.Task[None] | None = None
        self._post_tasks: set[asyncio.Task[None]] = set()
        self._proactive_tasks: set[asyncio.Task[None]] = set()
        self.register_plugin_web_apis()

    @property
    def settings(self) -> PluginSettings:
        """当前配置对应的设置。

        配置内容不变时复用同一对象，免去每次读取都重新解析判断图；调用方只读，
        需要改动时请自行构造 PluginSettings。配置的任何改动（含原地修改嵌套项）都会重新解析。
        """
        try:
            key = json.dumps(self.config, sort_keys=True, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            key = None
        cached = getattr(self, "_settings_cache", None)
        if key is not None and cached is not None and cached[0] == key:
            return cached[1]
        settings = PluginSettings(self.config)
        warning = settings.decision_questions_warning
        # 问题配置的警告每次配置改动只提醒一次，不在每轮判断里重复；
        # 配置无法序列化、没法缓存时每次都重新解析，改为按警告内容去重。
        if warning and (key is not None or warning != getattr(self, "_uncached_warning", None)):
            logger.warning("判断问题配置：%s", warning)
        if key is None:
            self._uncached_warning = warning
        else:
            self._settings_cache = (key, settings)
        return settings

    async def initialize(self) -> None:
        self._silence_task = asyncio.create_task(self._silence_loop())
        logger.info("快速决策插件已启动。将在全部插件加载后再核对表情包管理器。请勿同时开启官方概率主动回复。")

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        status = self.meme_adapter.status()
        if status.available:
            logger.info(
                "已对接表情包管理器：version=%s apis=%s packs=%s candidates=%s",
                status.plugin_version or "未知",
                ",".join(status.registered_apis) or "无文档API",
                status.pack_count,
                status.candidate_count,
            )
        else:
            logger.warning("%s", status.message or UNAVAILABLE_HINT)
        tts = self.tts_adapter.status()
        if tts.plugin_loaded:
            logger.info("已对接通用 TTS：version=%s engine=%s %s", tts.plugin_version or "未知",
                        tts.engine or "未配置", tts.message)
            if tts.core_provider:
                logger.warning("AstrBot 内置 TTS 仍选着提供商 %s：通用 TTS 合成失败的回复会由它再转一次，"
                               "建议把 TTS 提供商留空", tts.core_provider)

    def _tts(self) -> UniversalTTSAdapter | None:
        """在「外部插件」中启用了通用 TTS 适配时返回适配器。"""
        adapter = getattr(self, "tts_adapter", None)
        if adapter is None or not self.settings.plugin_enabled("universal_tts"):
            return None
        return adapter

    async def _await_voice_tone(self, decision: DecisionResult) -> None:
        """语气在正文生成后判断时，等判断结束再合成；超时用引擎默认语气。"""
        judged = decision.tone_judged
        if judged is None or judged.is_set():
            return
        try:
            await asyncio.wait_for(judged.wait(), VOICE_TONE_WAIT_SECONDS)
        except TimeoutError:
            logger.warning("等待语气判断超过 %s 秒，本轮按引擎默认语气合成：%s", VOICE_TONE_WAIT_SECONDS, decision.round_id)

    async def _voice_available(self, scene: str, umo: str = "") -> bool:
        """本轮能否转语音；不能时跳过「是否用语音」等用到语音动作的题。

        给了 umo 时还检查本会话：通用 TTS 的 /tts_bind 0 禁音和 AstrBot 的会话 TTS 开关。
        """
        tts = self._tts()
        proactive = scene.endswith("_proactive")
        if tts is None or (proactive and not self.settings.tts_proactive_voice):
            return False
        status = tts.status()
        if not (status.available and status.runtime_enabled):
            return False
        # 流式回复不经过发送前处理，被动回复转不了语音；主动发言不走流式，不受影响。
        if not proactive and status.streaming:
            return False
        return await tts.session_voice_enabled(umo) if umo else True

    async def terminate(self) -> None:
        if self._silence_task and not self._silence_task.done():
            self._silence_task.cancel()
            try:
                await self._silence_task
            except asyncio.CancelledError:
                pass
        tasks = [*self._post_tasks, *self._proactive_tasks]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.sessions.flush_logs()
        logger.info("快速决策插件已卸载。")

    # --- 消息监听与决策 ---

    @filter.event_message_type(filter.EventMessageType.ALL, priority=20)
    async def on_chat_message(self, event: AstrMessageEvent):
        """监听群聊和私聊，在需要时进行判断。"""
        if event.get_message_type() == MessageType.OTHER_MESSAGE:
            return
        if is_self_message(event):
            return
        profile = event_profile(event)
        if event_has_command_handler(event):
            return
        buffered = to_buffered_message(event)
        runtime = self.sessions.append_message(
            profile, buffered, self.settings.context_message_limit
        )
        runtime.display_name = event_session_name(event) or runtime.display_name
        runtime.handlers += 1
        try:
            if not self.settings.enabled:
                return
            if (not profile.is_private and not runtime.display_name and callable(getattr(event, "get_group", None))
                    and time.time() - runtime.name_checked_at > 300):
                runtime.name_checked_at = time.time()
                task = asyncio.create_task(self._resolve_session_name(event, runtime))
                self._post_tasks.add(task)
                task.add_done_callback(self._post_tasks.discard)
            return await self._judge_in_turn(event, profile, runtime, buffered.seq)
        finally:
            runtime.handlers -= 1
            if runtime.handlers == 0:
                # 本会话没有处理器还在排队：停用期间到达、或等待中被取消而没轮到判断的消息，不再并入以后的回合。
                runtime.consumed_seq = runtime.seq

    async def _judge_in_turn(self, event: AstrMessageEvent, profile: SessionProfile, runtime, seq: int):
        """按到达顺序逐轮判断入站消息，同一会话同时只进行一轮被动判断。

        判断、官方流水线或主动回合进行期间到达的消息排队等待。轮到时，已被上一轮判断看过的消息直接结束；
        否则以全部尚未判断的消息为一批，再判断一轮。
        """
        # 先持锁再等待：释放后到本回合开始之间，定时与冷场检查不会抢占会话。
        async with runtime.lock:
            while seq > runtime.consumed_seq and (released := session_release_event(runtime)) is not None:
                await released.wait()
            if seq <= runtime.consumed_seq:
                event.stop_event()
                return None
            runtime.deciding = True
            try:
                return await self._run_passive_round(event, profile)
            finally:
                runtime.consumed_seq = max(runtime.consumed_seq, seq)
                if runtime.pending_pipeline is None:
                    runtime.deciding = False

    async def _resolve_session_name(self, event, runtime) -> None:
        get_group = getattr(event, "get_group", None)
        if callable(get_group):
            try:
                group = await asyncio.wait_for(get_group(), timeout=3)
                runtime.display_name = str(getattr(group, "group_name", "") or runtime.display_name)
                if runtime.display_name:
                    logs = self.sessions.recent_logs(limit=1, umo=runtime.profile.umo)
                    if logs and logs[0].get("round_id"):
                        await self.sessions.update_round_log(logs[0]["round_id"], session_name=runtime.display_name)
            except Exception:  # noqa: BLE001
                logger.debug("无法读取会话名称：%s", runtime.profile.umo)

    async def _run_passive_round(
        self, event: AstrMessageEvent, profile: SessionProfile
    ):
        settings = self.settings
        runtime = self.sessions.ensure(profile)
        runtime.pending_pipeline = None
        wait = settings.debounce_seconds
        if wait > 0:
            await asyncio.sleep(wait)

        # 本轮处理上一轮判断之后到达的全部用户消息，到每次判断前的快照为止；之后到达的留给下一轮。
        seen = runtime.seq
        batch = runtime.unconsumed_user_messages(seen)
        at_or_quoted, scene = self._resolve_passive_scene(event, profile, batch)
        # 默认官方 Agent 只会在本事件上运行；放行冷却和插话开关时，看本轮任意一条是否 @ 或唤醒。
        official_would_run = bool(event.is_at_or_wake_command)
        woken = official_would_run or any(item.is_wake for item in batch)
        skip_reason = self._passive_skip_reason(
            settings, profile, scene, at_or_quoted, woken
        )
        messages = self.sessions.recent_messages(
            profile.umo, settings.context_message_limit
        )
        immediate_actions, memes = self._immediate_actions(settings, scene, messages) if skip_reason else (False, None)
        if skip_reason and not immediate_actions:
            runtime.consumed_seq = max(runtime.consumed_seq, seen)
            result = DecisionResult(
                scene=scene,
                trigger_event="message_received",
                decision_mode=settings.decision_mode,
                raw={"skip_reason": skip_reason},
            )
            result.dry_run = settings.dry_run
            runtime.last_decision = result
            await self._log_decision(profile, result)
            logger.info("%s：%s", skip_reason, profile.umo)
            event.stop_event()
            return

        # 决策进行中若仍有新消息，最多再判断两次，保证只发出一轮回复。
        result: DecisionResult | None = None
        if memes is None:
            memes = self._meme_candidates(settings)
        voice_available = await self._voice_available(scene, profile.umo)
        for _ in range(3):
            seen = runtime.seq
            batch = runtime.unconsumed_user_messages(seen)
            at_or_quoted, scene = self._resolve_passive_scene(event, profile, batch)
            messages = self.sessions.recent_messages(
                profile.umo, settings.context_message_limit
            )
            result = await ask_decision(
                self.context,
                settings=settings,
                scene=scene,
                is_private=profile.is_private,
                messages=messages,
                at_or_quoted=at_or_quoted,
                memes=memes,
                umo=profile.umo,
                voice_available=voice_available,
            )
            if not runtime.has_user_messages_after(seen):
                break
            logger.info("决策期间收到新消息，将合并后重新判断：%s", profile.umo)
        # 最后一次判断之后到达的消息仍在排队，本轮结束后另起一轮判断。
        runtime.consumed_seq = max(runtime.consumed_seq, seen)
        woken = official_would_run or any(item.is_wake for item in batch)

        if result is None:
            return
        result.dry_run = settings.dry_run
        runtime.last_decision = result

        merged = len(batch) > 1
        use_default_official = should_use_default_official_agent(
            official_would_run=official_would_run,
            has_merged_messages=merged,
        )
        if result.failed:
            # 先落盘再返回，此时尚未决定是否走官方流水线。
            await self._log_decision(profile, result)
            if scene == "private_passive" and result.action == "reply" and not settings.dry_run:
                # 退回官方纯文字回复，不挂载失败的决策。事件未唤醒官方流水线（私聊需要唤醒词）或本轮合并了
                # 多条消息时，官方默认 Agent 不回复或只回复这一条，改为提交本轮全部消息。
                request, reason = await self._official_handoff(event, profile, batch, use_default_official)
                if reason is not None:
                    logger.warning("私聊判断失败，且%s，本轮不回复：%s", reason, profile.umo)
                    return None
                logger.warning("私聊判断失败，将退回官方纯文字回复：%s", result.error)
                return request
            event.stop_event()
            return

        if not settings.dry_run:
            await run_tools(self.context, umo=profile.umo, resolver=self.meme_adapter, decision=result, when="on_match")
        if not result.should_speak():
            await self._log_decision(profile, result)
            event.stop_event()
            return

        # 判断期间可能刚发过言进入冷却，或改了插话开关，按判断前的同一规则再看一次。
        if self._passive_skip_reason(settings, profile, scene, at_or_quoted, woken):
            skip_tools(result, "群聊插话已关闭或会话冷却中")
            await self._log_decision(profile, result)
            logger.info("会话处于冷却中，跳过群聊插话：%s", profile.umo)
            event.stop_event()
            return

        if settings.dry_run:
            await self._log_decision(profile, result)
            event.stop_event()
            logger.info(
                "试运行：将不会发送决策结果 %s / %s", profile.umo, result.action
            )
            return

        request, reason = await self._official_handoff(event, profile, batch, use_default_official)
        if reason is not None:
            logger.info("%s，本轮不回复：%s", reason, profile.umo)
            skip_tools(result, reason)
            await self._log_decision(profile, result)
            return None

        # 仅已接纳的回复才能在后续发送钩子中使用工具。
        result.official_llm = True
        await self._log_decision(profile, result)
        event.set_extra("quick_decision", result)
        install_group_quote_guard(event, result, on_stream_end=self._finish_official_send, private=profile.is_private,
                                  on_activity=lambda: touch_official_pipeline(runtime, result))
        begin_official_pipeline(runtime, result, event)
        return request

    async def _official_request(self, event: AstrMessageEvent, profile: SessionProfile, batch: list[BufferedMessage]):
        """群聊未 @ 插话、未唤醒的私聊，或本轮合并了多条用户消息：构造公开 request_llm。

        主生成事件不 stop_event，以免跳过装饰/发送/after-send；用 should_call_llm(True)
        阻止默认第二条 Agent。兄弟事件仍在 on_chat_message 里 stop_event。

        Returns:
            (request, None)；取消本轮生成时为 (None, 原因)。
        """
        suppress_default_official_agent(event)
        conversation = await resolve_session_conversation(self.context, event)
        if conversation is None:
            logger.error("无法取得官方会话，取消本轮官方生成：%s", profile.umo)
            return None, "无法取得官方会话"
        merged = len(batch) > 1
        prompt_batch = batch
        if merged and not profile.is_private and group_context_enabled(self._astrbot_config(profile.umo)):
            prompt_batch, record_id = split_batch_for_group_context(batch)
            if record_id:
                event.set_extra(GROUP_CONTEXT_RECORD_KEY, record_id)
        prompt = build_inbound_user_prompt(
            event_text=event.message_str or "", batch=prompt_batch, merged=merged
        )
        image_urls, audio_urls = collect_event_media_urls(event)
        if not prompt and not image_urls and not audio_urls:
            logger.warning("没有可提交给官方模型的用户内容，取消本轮生成：%s", profile.umo)
            return None, "没有可生成回复的正文或附件"
        request = make_official_llm_request(
            event,
            conversation=conversation,
            prompt=prompt,
            image_urls=image_urls,
            audio_urls=audio_urls,
        )
        return request, None

    async def _official_handoff(self, event, profile, batch, use_default_official):
        """交给官方主 Agent：放行默认 Agent，或构造 request_llm。

        官方不会处理时不能占用会话：发送回调不会来，要等超时才释放，期间本会话的消息都在排队。

        Returns:
            (request, None)，放行默认 Agent 时 request 为 None；官方不会处理或无法构造请求时为 (None, 原因)。
        """
        reason = await official_skip_reason(
            event, self._astrbot_config(profile.umo), default_agent=use_default_official
        )
        if reason is not None or use_default_official:
            return None, reason
        return await self._official_request(event, profile, batch)

    def _astrbot_config(self, umo: str):
        """本会话生效的 AstrBot 配置；读不到时为 None。"""
        getter = getattr(self.context, "get_config", None)
        if not callable(getter):
            return None
        try:
            config = getter(umo)
        except Exception:  # noqa: BLE001
            return None
        return config if callable(getattr(config, "get", None)) else None

    def _resolve_passive_scene(
        self, event: AstrMessageEvent, profile: SessionProfile, batch: list[BufferedMessage]
    ) -> tuple[bool, SceneType]:
        """本轮任意一条消息 @ 或引用了机器人，就按被 @ 处理。"""
        at_or_quoted = any(item.is_at_bot or item.is_quote_bot for item in batch)
        if not at_or_quoted:
            is_at, is_quote, _ = inspect_mentions(event)
            at_or_quoted = is_at or is_quote
        if profile.is_private:
            return at_or_quoted, "private_passive"
        return at_or_quoted, "group_passive"

    async def _self_generate_and_send(self, profile: SessionProfile, result: DecisionResult) -> None:
        """冷场和定时发言没有入站事件，不经过官方 Agent，自行调用聊天模型生成并发送。"""
        settings = self.settings
        if settings.dry_run:
            return
        # 没有入站事件可取平台名，按空处理（与之前一致）。
        platform_name = ""
        try:
            group_context = not profile.is_private and group_context_enabled(self._astrbot_config(profile.umo))
            text = await generate_text(self.context, umo=profile.umo, decision=result, platform_name=platform_name,
                                       group_context=group_context)
        except Exception:  # noqa: BLE001
            logger.exception("自行调用聊天模型生成回复失败")
            return
        if not text:
            logger.warning("聊天模型没有返回文本，取消发送：%s", profile.umo)
            return
        self._schedule_post_decision(result, text, profile.umo, platform_name)

        # 主动发言直接发给平台，不经过官方装饰阶段，通用 TTS 的钩子碰不到，这里替它转语音。
        # 判断给出发送方式时以判断为准：只发文字就不调用它，发语音则不看它的触发概率。
        mode = result.voice_mode
        tts = self._tts() if settings.tts_proactive_voice and mode != "text" else None
        voice = None
        if tts is not None:
            async def voice(body):
                # 通用 TTS 反正会跳过（超出字数上限或命中跳过规则）时，不必等语气判断。
                if not tts.would_skip(body):
                    await self._await_voice_tone(result)
                return await tts.voice_record(profile.umo, body, tone=result.voice_tone, force=mode == "voice")

        sent_texts: list[str] = []
        try:
            await send_decision_message(
                self.context, umo=profile.umo, text=text, resolver=self.meme_adapter,
                decision=result, dry_run=False, platform_name=platform_name, sent_texts=sent_texts,
                voice=voice, voice_fallback=tts.fallback_to_text() if tts is not None else True,
            )
        finally:
            if sent_texts:
                reply_text = "\n".join(sent_texts)
                self._record_bot_text(profile, reply_text)
                await self._update_log_reply(result, reply_text, complete=result.text_sent is True)
                self.sessions.mark_proactive(profile.umo)

    def _immediate_actions(self, settings, scene, messages, **trigger):
        """不能发言的回合是否仍有判断后立即发图的动作要跑；返回（是否有，读过的表情候选或 None）。

        先只看判断图，可能有时才读表情候选并按候选是否存在再判断一次。
        """
        if not has_on_match_actions(settings.decision_questions, scene, **trigger):
            return False, None
        memes = self._meme_candidates(settings)
        available = {
            "memes": bool(memes),
            "meme_categories": bool(memes),
            "recent_messages": any(item.role == "user" and item.message_id for item in messages),
        }
        return has_on_match_actions(settings.decision_questions, scene, candidate_available=available, **trigger), memes

    def _meme_candidates(self, settings):
        # 在「外部插件」中停用后视同表情包管理器不可用，选表情的问题因没有候选而跳过。
        if not settings.plugin_enabled("meme_manager"):
            return []
        by_id = {q.id: q for q in settings.decision_questions}
        if any(q.enabled and (candidate_type(q, by_id) in ("memes", "meme_categories")
                              or q.requires_candidates in ("memes", "meme_categories") or q.role == "meme")
               for q in settings.decision_questions):
            return self.meme_adapter.described_catalog()
        return []

    def _passive_skip_reason(
        self, settings, profile, scene, at_or_quoted, official_would_run
    ) -> str | None:
        if scene != "group_passive" or at_or_quoted or official_would_run:
            return None
        if not settings.group_enable_interrupt:
            return "群聊插话已关闭，跳过判断"
        if self.sessions.in_cooldown(profile.umo, settings.cooldown_seconds):
            return "冷却中跳过"
        return None

    def _proactive_skip_reason(self, settings, profile, scheduled) -> str | None:
        if not scheduled and not settings.proactive_enabled(profile.is_private):
            return "冷场主动发言已关闭，跳过判断"
        if self.sessions.in_cooldown(profile.umo, settings.cooldown_seconds):
            return "冷却中跳过"
        if not self.sessions.can_proactive(
            profile.umo, settings.hourly_proactive_limit(profile.is_private)
        ):
            return "每小时主动发言上限已满，跳过判断"
        return None

    def _record_bot_text(self, profile: SessionProfile, text: str) -> None:
        self.sessions.append_message(
            profile,
            BufferedMessage(
                message_id="",
                sender_id=profile.bot_id or "bot",
                sender_name="bot",
                role="assistant",
                text=text,
                timestamp=time.time(),
            ),
            self.settings.context_message_limit,
        )
        self.sessions.mark_bot_sent(profile.umo)

    async def _log_decision(
        self, profile: SessionProfile, result: DecisionResult, *, timer_roots=None
    ) -> None:
        if not result.should_speak():
            skip_tools(result, "本轮不发言", when="after_send")
        if not result.round_id:
            result.round_id = uuid4().hex
        runtime = self.sessions.get(profile.umo)
        log_context = result.log_context or decision_log_context(
            self.sessions.recent_messages(profile.umo, self.settings.context_message_limit),
            evaluated_at=time.time(), last_activity=runtime.last_activity if runtime else None, timer_roots=timer_roots,
        )
        trigger = next((m for m in reversed(log_context["messages"]) if m["role"] == "user"), None)
        record = {
            "round_id": result.round_id,
            "tool_calls": [asdict(c) for c in result.tool_calls.values()],
            "answers": result.answers,
            "batches": result.batches,
            "outcomes": {key: o.reason for key, o in result.outcomes.items() if not o.has_answer},
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "umo": profile.umo,
            "scene": result.scene,
            "trigger_event": result.trigger_event,
            "decision_mode": result.decision_mode,
            "action": result.action,
            "reply_sources": result.reply_sources,
            "length": result.length,
            "custom_answers": result.custom_answers,
            "session_hints": result.session_hints,
            "use_meme": result.use_meme,
            "meme_id": result.meme_id,
            "voice_mode": result.voice_mode,
            "voice_tone": result.voice_tone,
            "target_message_id": result.target_message_id,
            "duration_ms": result.duration_ms,
            "error": result.error,
            "degraded": result.degraded,
            "dry_run": result.dry_run,
            "official_llm": result.official_llm,
            "raw": result.raw,
            "session_name": runtime.display_name if runtime else "",
            "reply_sent": None if result.should_speak() and not result.dry_run else False,
        }
        if log_context.get("timer_roots") is not None:
            # 本轮到期的定时题；跳过记录也据此区分，不同定时题的跳过不会并成一条。
            record["timer_roots"] = log_context["timer_roots"]
        if self.settings.decision_log_include_content:
            record["replay_context"] = log_context
            if trigger:
                record.update(trigger_text=_log_text(trigger["text"]),
                              trigger_sender=trigger["sender_name"] or trigger["sender_id"])
        if result.raw.get("skip_reason") and not result.outcomes:
            await self.sessions.append_skip_log(record)
        else:
            await self.sessions.append_log(record)

    async def _update_log_reply(self, result: DecisionResult, text: str, *, complete=True) -> None:
        if result.round_id and text:
            fields = {"reply_sent": True, "reply_complete": complete}
            if result.voice_sent:
                fields["reply_voice"] = True
            if self.settings.decision_log_include_content:
                fields["reply_text"] = _log_text(text, 80)
            await self.sessions.update_round_log(result.round_id, **fields)

    # --- 定时判断与冷场主动发言 ---

    async def _silence_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(15)
                await self._release_stale_pipelines()
                await self._check_timers()
                await self._check_silence()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("检查定时判断或冷场主动发言时出错")

    async def _release_stale_pipelines(self, now: float | None = None) -> None:
        """释放迟迟没有回调发送完成钩子的官方流水线，免得会话永久挂起、之后的消息都不再回复。

        官方 Agent 仍在运行时一直等（最多 PIPELINE_MAX_SECONDS）；否则从最后一次动静起再等
        PIPELINE_GRACE_SECONDS，留给装饰、语音合成和分段发送。动静还包括插件钩子和发送（_touch_pipeline），
        读不到官方 Agent 状态时（其他 AstrBot 版本、第三方执行器）全靠它们。
        不在插件停用时跳过：停用期间卡住的会话，重新启用后也要能恢复。
        """
        now = time.monotonic() if now is None else now
        for runtime in self.sessions.all_sessions():
            decision = runtime.pending_pipeline
            if decision is None:
                continue
            try:
                runner = active_event_runner(runtime.pipeline_event) if runtime.pipeline_event is not None else None
                if runner is not None and not runner.done():
                    runtime.pipeline_active_at = now
            except Exception:  # noqa: BLE001
                logger.debug("无法读取官方 Agent 状态：%s", runtime.profile.umo, exc_info=True)
            held = now - runtime.pipeline_started_at
            if now - runtime.pipeline_active_at < PIPELINE_GRACE_SECONDS and held < PIPELINE_MAX_SECONDS:
                continue
            if not release_official_pipeline(runtime, decision):
                continue
            logger.warning("官方流水线 %d 秒未回调发送完成（可能是正文为空或被其他插件中止），已释放会话：%s",
                           held, runtime.profile.umo)
            try:
                await self._finish_tool_log(decision, "官方流水线未回调发送完成")
            except Exception:  # noqa: BLE001
                logger.exception("无法记录超时释放的回合：%s", decision.round_id)

    async def _check_timers(self) -> None:
        settings = self.settings
        if not settings.enabled:
            return
        for runtime in self.sessions.all_sessions():
            scene = "private_proactive" if runtime.profile.is_private else "group_proactive"
            timers = {
                (q.id, t.get("interval_seconds"), t.get("daily_time")): t
                for q in settings.decision_questions
                if q.enabled and q.stage == "pre_reply" and (not q.scenes or scene in q.scenes)
                for t in q.triggers or [] if t.get("event") == "timer_elapsed"
            }
            runtime.timer_due = {key: due for key, due in runtime.timer_due.items() if key in timers}
            if not timers or not runtime.last_user_ts:
                continue
            now, zone = _round_clock(self.context, runtime.profile.umo)
            for key, trigger in timers.items():
                if key not in runtime.timer_due:
                    runtime.timer_due[key] = _next_timer_at(trigger, now, zone)
            if is_official_pipeline_busy(runtime) or runtime.lock.locked():
                continue
            due = {key for key, deadline in runtime.timer_due.items() if deadline <= now}
            if not due or len(self._proactive_tasks) >= PROACTIVE_CONCURRENCY:
                continue
            for key in due:
                runtime.timer_due[key] = _next_timer_at(timers[key], now, zone)
            self._start_proactive(runtime, timer_roots={key[0] for key in due})

    async def _check_silence(self) -> None:
        # 停用期间仍在缓冲消息，清理闲置会话也不能停。
        self.sessions.prune_idle()
        settings = self.settings
        if not settings.enabled:
            return
        now = time.time()
        for runtime in self.sessions.all_sessions():
            profile = runtime.profile
            if is_official_pipeline_busy(runtime) or runtime.lock.locked() or not runtime.last_user_ts:
                continue
            if self.sessions.silence_already_evaluated(profile.umo):
                continue
            if now - runtime.last_activity < settings.silence_seconds(profile.is_private):
                continue
            scene = "private_proactive" if profile.is_private else "group_proactive"
            reason = self._proactive_skip_reason(settings, profile, False)
            if reason and not has_on_match_actions(settings.decision_questions, scene, trigger_event="silence_elapsed"):
                # 不能开口、也没有命中即执行的动作：本段冷场到此为止，不起回合，也不写跳过日志；
                # 仍记为本会话最近一次决策，/decision status 能看到冷场被跳过。
                self.sessions.mark_silence_evaluated(profile.umo)
                runtime.last_decision = DecisionResult(
                    scene=scene, trigger_event="silence_elapsed", decision_mode=settings.decision_mode,
                    raw={"skip_reason": reason}, dry_run=settings.dry_run,
                )
                continue
            if len(self._proactive_tasks) >= PROACTIVE_CONCURRENCY:
                continue
            self._start_proactive(runtime)

    def _start_proactive(self, runtime, *, timer_roots=None) -> None:
        """在后台执行主动回合，避免一个会话的判断或生成拖慢其他会话。"""
        # 同步占用会话，后续检查和新消息立刻能看到本会话正忙。
        begin_proactive_round(runtime)
        task = asyncio.create_task(self._proactive_round(runtime, timer_roots))
        self._proactive_tasks.add(task)
        task.add_done_callback(self._proactive_tasks.discard)

    async def _proactive_round(self, runtime, timer_roots) -> None:
        try:
            await self._run_proactive(runtime, timer_roots=timer_roots)
        except Exception:  # noqa: BLE001
            kind = "定时判断" if timer_roots is not None else "冷场主动发言"
            logger.exception("%s失败：%s", kind, runtime.profile.umo)
        finally:
            end_proactive_round(runtime)

    async def _run_proactive(self, runtime, *, timer_roots=None) -> None:
        profile: SessionProfile = runtime.profile
        settings = self.settings
        scheduled = timer_roots is not None
        if not scheduled:
            self.sessions.mark_silence_evaluated(profile.umo)
        scene: SceneType = (
            "private_proactive" if profile.is_private else "group_proactive"
        )
        trigger_event = "timer_elapsed" if scheduled else "silence_elapsed"
        limit_reason = self._proactive_skip_reason(settings, profile, scheduled)
        messages = self.sessions.recent_messages(
            profile.umo, settings.context_message_limit
        )
        immediate_actions, memes = (
            self._immediate_actions(settings, scene, messages, trigger_event=trigger_event, timer_roots=timer_roots)
            if limit_reason else (False, None)
        )
        if limit_reason and not immediate_actions:
            result = DecisionResult(
                scene=scene,
                trigger_event=trigger_event,
                decision_mode=settings.decision_mode,
                raw={"skip_reason": limit_reason},
            )
            result.dry_run = settings.dry_run
            runtime.last_decision = result
            await self._log_decision(profile, result, timer_roots=timer_roots)
            logger.info("%s：%s", limit_reason, profile.umo)
            return
        if memes is None:
            memes = self._meme_candidates(settings)
        result = await ask_decision(
            self.context,
            settings=settings,
            scene=scene,
            is_private=profile.is_private,
            messages=messages,
            at_or_quoted=False,
            memes=memes,
            umo=profile.umo,
            last_activity=runtime.last_activity,
            trigger_event=trigger_event,
            timer_roots=timer_roots,
            voice_available=await self._voice_available(scene, profile.umo),
        )
        result.dry_run = settings.dry_run
        runtime.last_decision = result
        if not settings.dry_run and not result.failed:
            await run_tools(self.context, umo=profile.umo, resolver=self.meme_adapter, decision=result, when="on_match")
        # 判断期间开关、冷却或次数可能已变，按判断前的同一规则再看一次。
        limited = self._proactive_skip_reason(settings, profile, scheduled) is not None
        if limited:
            skip_tools(result, "主动发言已关闭、冷却中或已达次数上限", when="after_send")
        await self._log_decision(profile, result)
        if limited or result.failed or result.action != "proactive":
            return
        if settings.dry_run:
            logger.info("试运行：主动判断为可以开口，但不会发送：%s", profile.umo)
            return
        try:
            await self._self_generate_and_send(profile, result)
        finally:
            await self._finish_tool_log(result)

    # --- 官方流水线钩子 ---

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        decision = self._pipeline_decision(event)
        if decision is None:
            return
        self._touch_pipeline(event, decision)
        if self.settings.dry_run:
            event.stop_event()
            await self._cancel_official_round(event, decision, "已切换到试运行")
            return
        if event.get_extra("_qd_hints_applied"):
            return
        apply_session_hints(req, decision)
        event.set_extra("_qd_hints_applied", True)

    def _schedule_post_decision(self, decision: DecisionResult, text: str, umo: str, platform_name: str) -> None:
        plan = decision.post_plan
        if plan is None or not text.strip():
            return
        decision.post_plan = None
        settings = self.settings
        if not settings.enabled or settings.dry_run:
            return
        if any(action.get("tool") == "voice_tone" for spec in getattr(plan, "specs", ())
               for actions in (getattr(spec, "actions", None) or {}).values() for action in actions):
            # 语气要按实际正文判断，合成语音的一方会等这个信号。
            decision.tone_judged = asyncio.Event()

        async def judge_and_log():
            try:
                try:
                    report = await evaluate_post_reply(self.context, plan, text, decision.answers, decision=decision)
                finally:
                    if decision.tone_judged is not None:
                        decision.tone_judged.set()
                fields = {"post_decision": report}
                if decision.voice_tone:
                    fields["voice_tone"] = decision.voice_tone
                await self.sessions.update_round_log(decision.round_id, **fields)
                if decision.tool_calls:
                    if self.settings.dry_run or not self.settings.enabled:
                        skip_tools(decision, "试运行或插件停用")
                    else:
                        await run_tools(self.context, umo=umo, resolver=self.meme_adapter, decision=decision, when="on_match")
                        if decision.text_sent is True:
                            await send_followup_tools(self.context, umo, self.meme_adapter, decision, platform_name)
                        elif decision.text_sent is False:
                            skip_tools(decision, "正文未确认发送", when="after_send")
                    await self._update_tool_log(decision)
            except Exception:
                logger.exception("无法记录本轮输出后判断：%s", decision.round_id)

        task = asyncio.create_task(judge_and_log())
        self._post_tasks.add(task)
        task.add_done_callback(self._post_tasks.discard)

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        decision = self._pipeline_decision(event)
        if decision is None:
            return
        self._touch_pipeline(event, decision)
        if resp and resp.role == "err":
            event.set_extra("_qd_llm_error", True)
            return
        if event.get_extra("_qd_streaming"):
            return
        if resp and resp.role == "assistant" and not resp.is_chunk and not resp.tools_call_name:
            self._schedule_post_decision(decision, resp.completion_text or "", event.unified_msg_origin,
                                         platform_name_from_event(event))
            # skills_like 的工具重查可先发送最终文字，再调用 on_llm_response。
            prior = event.get_extra("_qd_intermediate_receipt")
            if prior and prior[0] == resp.completion_text:
                await self._finish_official_send(event, stream_text=prior[0], complete=prior[1])

    @filter.on_using_llm_tool()
    async def on_using_llm_tool(self, event, tool, tool_args):
        if decision := self._pipeline_decision(event):
            self._touch_pipeline(event, decision)
            event.set_extra("_qd_intermediate_receipt", None)

    # 先于通用 TTS（priority=0）执行：它会把文字换成语音，并在跳过时把结果改标为普通消息。
    @filter.on_decorating_result(priority=10)
    async def on_decorating_result(self, event: AstrMessageEvent):
        decision = self._pipeline_decision(event)
        if decision is None:
            return
        self._touch_pipeline(event, decision)
        result = event.get_result()
        if result is None:
            return
        is_llm = getattr(result, "is_llm_result", lambda: False)() and not event.get_extra("_qd_streaming")
        if is_llm:
            decision.text_sent = None
            event.set_extra("_qd_text_receipt", False)
            event.set_extra("_qd_sent_text", "")
            event.set_extra("_qd_undecorated_text", "".join(c.text for c in result.chain if isinstance(c, Plain)))
        is_private = event.is_private_chat()
        settings = self.settings
        if not prepare_official_result_chain(
            result.chain,
            dry_run=settings.dry_run,
            should_speak=decision.should_speak(),
            clear_group_reply=getattr(result, "is_llm_result", lambda: False)(),
            is_private=is_private,
        ):
            event.stop_event()
            await self._cancel_official_round(event, decision, "已取消正文发送")
            return
        if is_llm:
            event.set_extra("_qd_llm_result", result)
            event.set_extra("_qd_llm_components", list(result.chain))
            # 通用 TTS 转语音时会再包一层 event.send；改回文字时按这个找回它包之前的发送函数。
            event.set_extra("_qd_send_before_tts", event.send)
            mode = decision.voice_mode
            tts = self._tts()
            if tts is not None and mode != "text":
                # 语气在正文生成后判断，先等它；只发文字、或通用 TTS 反正会跳过这段正文时不用等。
                if not tts.would_skip(event.get_extra("_qd_undecorated_text") or ""):
                    await self._await_voice_tone(decision)
                # 判断要发语音时不看通用 TTS 的触发概率；只给了语气时仍由它按概率决定。
                if mode == "voice" or decision.voice_tone:
                    await tts.voice_event(event, decision.voice_tone, force=mode == "voice")

    # 晚于通用 TTS（priority=0）执行，消除它留下的影响：
    # - 它不转语音时会把结果改标为普通消息，AstrBot 分段回复只认模型结果，这里改回来；
    #   本插件处理的每一轮都要改回（包括没判断是否用语音、/tts_off、停用适配的轮次），否则回复不分段；
    # - 判断为只发文字、它却按概率转了语音时，换回原文。
    @filter.on_decorating_result(priority=-10)
    async def settle_voice_choice(self, event: AstrMessageEvent):
        decision = self._pipeline_decision(event)
        if decision is None or event.get_extra("_qd_streaming"):
            return
        result = event.get_result()
        adapter = getattr(self, "tts_adapter", None)
        if result is None or result is not event.get_extra("_qd_llm_result") or adapter is None:
            return
        # 没装通用 TTS 时不动结果类型：别的插件改标普通消息另有用意。
        if adapter.plugin() is None:
            return
        text_only = decision.voice_mode == "text" and self._tts() is not None
        reverted = adapter.settle(result, voice=not text_only, original=event.get_extra("_qd_llm_components") or [])
        if reverted:
            adapter.drop_send_fallback(event, event.get_extra("_qd_send_before_tts"))

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent):
        await self._finish_official_send(event)

    async def _finish_official_send(self, event, *, stream_text=None, complete=True):
        # 先根据本事件上的决策记状态并释放流水线，再交给钩子发独立表情。
        # 表情发送会消费 extra，重复进入或其它事件不会再发图。
        decision = event.get_extra("quick_decision")
        if isinstance(decision, DecisionResult) and stream_text is None:
            runner = active_event_runner(event)
            if runner and not runner.done():
                if getattr(runner, "tool_schema_mode", None) == "skills_like":
                    event.set_extra("_qd_intermediate_receipt", (event.get_extra("_qd_undecorated_text"),
                                    decision.text_sent is not False and official_text_was_sent(event)))
                return
        if isinstance(decision, DecisionResult) and stream_text is None and official_generation_failed(event):
            stream_text, complete = "", False
        runtime = self.sessions.get(event.unified_msg_origin)
        reply_text = ""
        if runtime is not None and isinstance(decision, DecisionResult):
            text = stream_text or ""
            result = event.get_result()
            if stream_text is None and result and result.chain:
                text = "".join(
                    getattr(comp, "text", "")
                    for comp in result.chain
                    if isinstance(comp, Plain)
                ).strip()
                if not text and has_record(result.chain) and official_text_was_sent(event):
                    # 通用 TTS 把文字换成了语音（或语音失败后改发了原文），链上已经没有文字。
                    text = (event.get_extra("_qd_undecorated_text") or "").strip()
            if text:
                self._record_bot_text(runtime.profile, text)
                if stream_text is not None:
                    reply_text = text
                else:
                    reply_text = event.get_extra("_qd_sent_text") or (
                        text if official_text_was_sent(event) and decision.text_sent is not False else ""
                    )
                if stream_text is not None and complete:
                    self._schedule_post_decision(decision, text, event.unified_msg_origin, platform_name_from_event(event))
            elif stream_text is None:
                self.sessions.mark_bot_sent(runtime.profile.umo)
            release_official_pipeline(runtime, decision)
        try:
            await after_official_text_sent(
                event,
                context=self.context,
                resolver=self.meme_adapter,
                dry_run=self.settings.dry_run,
                text_sent=None if stream_text is None else complete and bool(stream_text.strip()),
            )
        finally:
            if isinstance(decision, DecisionResult):
                if reply_text:
                    await self._update_log_reply(decision, reply_text, complete=complete and decision.text_sent is not False)
                await self._finish_tool_log(decision)

    async def _cancel_official_round(self, event, decision, reason):
        event.set_extra("quick_decision", None)
        event.set_extra("_qd_quote_decision", None)
        release_official_pipeline(self.sessions.get(event.unified_msg_origin), decision)
        await self._finish_tool_log(decision, reason)

    async def _finish_tool_log(self, result, reason="正文未确认发送"):
        if result.text_sent is None:
            result.text_sent = False
        if not result.text_sent:
            skip_tools(result, reason, when="after_send")
        if result.round_id and not result.text_sent:
            row = self.sessions.find_log(result.round_id)
            if row and not row.get("reply_sent"):
                await self.sessions.update_round_log(result.round_id, reply_sent=False)
        await self._update_tool_log(result)

    async def _update_tool_log(self, result):
        if result.round_id and result.tool_calls:
            await self.sessions.update_tool_log(
                result.round_id, [asdict(c) for c in result.tool_calls.values()]
            )

    def _voice_status_line(self, settings) -> str:
        adapter = getattr(self, "tts_adapter", None)
        if not settings.plugin_enabled("universal_tts"):
            return "已在快速决策中停用通用 TTS 适配，主动发言只发文字"
        if adapter is None:
            return "未对接，按文字发送"
        status = adapter.status()
        if not status.available:
            return status.message or "未对接，按文字发送"
        line = f"通用 TTS {status.plugin_version or ''}，引擎 {status.engine}"
        return f"{line}（{status.message}）" if status.message else line

    def _touch_pipeline(self, event, decision: DecisionResult) -> None:
        """官方流水线仍有动静，推迟超时释放；读不到官方 Agent 状态时全靠这些信号。"""
        touch_official_pipeline(self.sessions.get(event.unified_msg_origin), decision)

    def _pipeline_decision(self, event: AstrMessageEvent) -> DecisionResult | None:
        extra = event.get_extra("quick_decision")
        if isinstance(extra, DecisionResult):
            return extra
        return None

    # --- 管理员指令：决策状态 ---

    @filter.command_group("decision")
    def decision_group(self):
        """快速决策查询"""

    @decision_group.command("status")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def decision_status(self, event: AstrMessageEvent):
        """查看插件状态以及当前会话最近一次决策。"""
        settings = self.settings
        lines = [
            f"快速决策：{'启用' if settings.enabled else '关闭'}",
            f"试运行：{'是' if settings.dry_run else '否'}",
            "决策模式：" + ("纯随机" if settings.decision_mode == "random" else "模型判断"),
            "表情包来源："
            + (
                "已在快速决策中停用，仅发文字"
                if not settings.plugin_enabled("meme_manager")
                else f"meme_manager {status.plugin_version or ''}，{status.candidate_count} 张可用"
                if (status := self.meme_adapter.status()).available
                else (status.message or "不可用，仅发文字")
            ),
            "语音：" + self._voice_status_line(settings),
        ]
        if settings.decision_mode == "model":
            lines.extend(
                [
                    "判断提供商：" + (settings.decision_provider_id or "（默认，需先在 WebUI 配置）"),
                    f"判断阈值：{settings.choice_threshold}",
                    f"判断选项温度：{settings.choice_temperature}",
                ]
            )
        runtime = self.sessions.get(event.unified_msg_origin)
        result = runtime.last_decision if runtime else None
        if result:
            lines.append("最近一次决策：")
            length_text = result.length or "（未判断）"
            lines.append(
                f"- 场景 {result.scene} / 动作 {result.action} / 长度 {length_text}"
            )
            answers = result.answers or result.custom_answers
            if answers:
                rendered = ", ".join(f"{key}={value}" for key, value in answers.items())
                lines.append(f"- 问题答案：{rendered}")
            if result.session_hints:
                lines.append("- 会话提示：" + " / ".join(result.session_hints))
            lines.append(
                f"- 配图 {'是 ' + (result.meme_id or '') if result.use_meme else '否'}"
            )
            lines.append(f"- 耗时 {result.duration_ms} ms")
            if result.error:
                lines.append(f"- 错误：{result.error}")
            if result.dry_run:
                lines.append("- 该次决策处于试运行，未发送消息")
        else:
            lines.append("当前会话还没有决策记录。")
        logs = self.sessions.recent_logs(limit=1, umo=event.unified_msg_origin)
        if logs:
            lines.append(f"日志时间：{logs[0].get('time', '')}")
        yield event.plain_result("\n".join(lines))
