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
    event_has_command_handler,
    event_profile,
    event_session_name,
    event_timestamp,
    inspect_mentions,
    is_self_message,
    to_buffered_message,
)
from .meme_adapter import UNAVAILABLE_HINT, MemeManagerAdapter
from .meme_migrate import maybe_migrate_legacy_library
from .models import BufferedMessage, DecisionResult, SceneType, SessionProfile, decision_log_context
from .official_handoff import (
    begin_official_pipeline,
    begin_proactive_round,
    build_inbound_user_prompt,
    collect_event_media_urls,
    end_proactive_round,
    is_official_pipeline_busy,
    make_official_llm_request,
    release_official_pipeline,
    resolve_session_conversation,
    session_release_event,
    should_use_default_official_agent,
    suppress_default_official_agent,
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
from .web_api import WebApiMixin

PLUGIN_NAME = "astrbot_plugin_quick_decision"
LOG_TEXT_LIMIT = 120
# 同时进行的定时与冷场回合上限；超出的会话保持到期，留到下次检查。
PROACTIVE_CONCURRENCY = 4


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
            return PluginSettings(self.config)
        cached = getattr(self, "_settings_cache", None)
        if cached is None or cached[0] != key:
            cached = self._settings_cache = (key, PluginSettings(self.config))
        return cached[1]

    async def initialize(self) -> None:
        try:
            maybe_migrate_legacy_library(
                self.data_dir, self.meme_adapter.resolve_data_dir()
            )
        except Exception:  # noqa: BLE001
            logger.exception("旧自制图库迁移失败，旧库保持原样，本轮不回退旧库发图")
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
        if not self.settings.enabled:
            return
        if (not profile.is_private and not runtime.display_name and callable(getattr(event, "get_group", None))
                and time.time() - runtime.name_checked_at > 300):
            runtime.name_checked_at = time.time()
            task = asyncio.create_task(self._resolve_session_name(event, runtime))
            self._post_tasks.add(task)
            task.add_done_callback(self._post_tasks.discard)
        if is_official_pipeline_busy(runtime):
            runtime.pending.append(buffered)
            if session_release_event(runtime) is None:
                # 被动回合仍在防抖或判断，会合并这条消息后重新判断。
                event.stop_event()
                return
            try:
                # 先持锁再等待：释放后到本回合开始之间，定时与冷场检查不会抢占会话。
                async with runtime.lock:
                    while runtime.pending and (released := session_release_event(runtime)) is not None:
                        await released.wait()
                    if not runtime.pending or is_official_pipeline_busy(runtime):
                        event.stop_event()
                        return
                    runtime.deciding = True
                    try:
                        return await self._run_passive_round(event, profile)
                    finally:
                        if runtime.pending_pipeline is None:
                            runtime.deciding = False
                            runtime.pending.clear()
            except asyncio.CancelledError:
                if buffered in runtime.pending:
                    runtime.pending.remove(buffered)
                raise

        runtime.deciding = True
        try:
            return await self._run_passive_round(event, profile)
        finally:
            if runtime.pending_pipeline is None:
                runtime.deciding = False
                runtime.pending.clear()

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
        first_message_id = str(getattr(event.message_obj, "message_id", "") or "")
        first_timestamp = event_timestamp(event)
        wait = settings.debounce_seconds
        if wait > 0:
            await asyncio.sleep(wait)

        runtime = self.sessions.ensure(profile)
        at_or_quoted, scene = self._resolve_passive_scene(event, profile)
        official_would_run = bool(event.is_at_or_wake_command)
        skip_reason = self._passive_skip_reason(
            settings, profile, scene, at_or_quoted, official_would_run
        )
        messages = self.sessions.recent_messages(
            profile.umo, settings.context_message_limit
        )
        memes = None
        immediate_actions = has_on_match_actions(settings.decision_questions, scene)
        if skip_reason and immediate_actions:
            memes = self._meme_candidates(settings)
            immediate_actions = has_on_match_actions(
                settings.decision_questions,
                scene,
                candidate_available={
                    "memes": bool(memes),
                    "meme_categories": bool(memes),
                    "recent_messages": any(
                        item.role == "user" and item.message_id for item in messages
                    ),
                },
            )
        if skip_reason and not immediate_actions:
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
        for _ in range(3):
            runtime.pending.clear()
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
            )
            if not runtime.pending:
                break
            logger.info("决策期间收到新消息，将合并后重新判断：%s", profile.umo)

        if result is None:
            return
        result.dry_run = settings.dry_run
        runtime.last_decision = result

        if result.failed:
            # 先落盘再返回，此时尚未决定是否走官方流水线。
            await self._log_decision(profile, result)
            if scene == "private_passive" and result.action == "reply" and not settings.dry_run:
                logger.warning("私聊判断失败，将退回官方纯文字回复：%s", result.error)
                return
            event.stop_event()
            return

        if not settings.dry_run:
            await run_tools(self.context, umo=profile.umo, resolver=self.meme_adapter, decision=result, when="on_match")
        if not result.should_speak():
            await self._log_decision(profile, result)
            event.stop_event()
            return

        if (
            scene == "group_passive"
            and not at_or_quoted
            and not official_would_run
            and (not settings.group_enable_interrupt or self.sessions.in_cooldown(profile.umo, settings.cooldown_seconds))
        ):
            skip_tools(result, "群聊插话已关闭或会话冷却中")
            await self._log_decision(profile, result)
            logger.info("会话处于冷却中，跳过群聊插话：%s", profile.umo)
            event.stop_event()
            return

        merged = self._has_merged_messages(
            event, runtime, first_message_id, first_timestamp
        )
        use_default_official = should_use_default_official_agent(
            official_would_run=official_would_run,
            has_merged_messages=merged,
        )
        if settings.dry_run:
            await self._log_decision(profile, result)
            event.stop_event()
            logger.info(
                "试运行：将不会发送决策结果 %s / %s", profile.umo, result.action
            )
            return

        request = None
        if not use_default_official:
            # 群聊未 @ 插话，或 debounce 合并了多条用户消息：yield 公开 request_llm。
            # 主生成事件不 stop_event，以免跳过装饰/发送/after-send；用 should_call_llm(True)
            # 阻止默认第二条 Agent。兄弟事件仍在 on_chat_message 里 stop_event。
            suppress_default_official_agent(event)
            conversation = await resolve_session_conversation(self.context, event)
            if conversation is None:
                logger.error("无法取得官方会话，取消本轮官方生成：%s", profile.umo)
                skip_tools(result, "无法取得官方会话")
                await self._log_decision(profile, result)
                return None
            prompt = build_inbound_user_prompt(
                event_text=event.message_str or "",
                messages=runtime.messages,
                first_message_id=first_message_id,
                first_timestamp=first_timestamp,
                merged=merged,
            )
            image_urls, audio_urls = collect_event_media_urls(event)
            if not prompt and not image_urls and not audio_urls:
                logger.warning("没有可提交给官方模型的用户内容，取消本轮生成：%s", profile.umo)
                skip_tools(result, "没有可生成回复的正文或附件")
                await self._log_decision(profile, result)
                return None
            request = make_official_llm_request(
                event,
                conversation=conversation,
                prompt=prompt,
                image_urls=image_urls,
                audio_urls=audio_urls,
            )

        # 仅已接纳的回复才能在后续发送钩子中使用工具。
        result.official_llm = True
        await self._log_decision(profile, result)
        event.set_extra("quick_decision", result)
        install_group_quote_guard(event, result, on_stream_end=self._finish_official_send, private=profile.is_private)
        begin_official_pipeline(runtime, result)
        return request

    def _resolve_passive_scene(
        self, event: AstrMessageEvent, profile: SessionProfile
    ) -> tuple[bool, SceneType]:
        recent = self.sessions.recent_messages(
            profile.umo, self.settings.context_message_limit
        )
        # 以本轮合并窗口内是否出现 @ / 引用为准（大约为 debounce 前后到达的消息）。
        window_start = time.time() - max(self.settings.debounce_seconds, 0) - 1
        at_or_quoted = any(
            (item.is_at_bot or item.is_quote_bot) and item.timestamp >= window_start
            for item in recent
        )
        if not at_or_quoted:
            is_at, is_quote, _ = inspect_mentions(event)
            at_or_quoted = is_at or is_quote
        if profile.is_private:
            return at_or_quoted, "private_passive"
        return at_or_quoted, "group_passive"

    def _has_merged_messages(

        self,
        event: AstrMessageEvent,
        runtime,
        first_message_id: str,
        first_timestamp: float,
    ) -> bool:
        """判断在本事件等待或判断期间是否还并入了其他用户消息。"""
        for item in runtime.messages:
            if item.role != "user":
                continue
            if (
                first_message_id
                and item.message_id
                and item.message_id != first_message_id
            ):
                if item.timestamp >= max(first_timestamp - 0.5, 0):
                    return True
            elif not first_message_id and item.timestamp > first_timestamp:
                return True
        return bool(runtime.pending)

    async def _self_generate_and_send(
        self,
        profile: SessionProfile,
        result: DecisionResult,
        event: AstrMessageEvent | None,
    ) -> None:
        settings = self.settings
        if settings.dry_run:
            return
        prompt = ""
        if event is not None:
            prompt = (event.message_str or "").strip()
        if not prompt:
            recent = self.sessions.recent_messages(profile.umo, 5)
            parts = [
                f"{item.sender_name or item.sender_id}: {item.text}"
                for item in recent
                if item.role == "user" and item.text
            ]
            prompt = "\n".join(parts[-3:])
        try:
            text = await generate_text(
                self.context,
                umo=profile.umo,
                decision=result,
                user_prompt=prompt,
            )
        except Exception:  # noqa: BLE001
            logger.exception("自行调用聊天模型生成回复失败")
            return
        if not text:
            logger.warning("聊天模型没有返回文本，取消发送：%s", profile.umo)
            return
        platform_name = platform_name_from_event(event)
        self._schedule_post_decision(result, text, profile.umo, platform_name)

        sent_texts: list[str] = []
        try:
            await send_decision_message(
                self.context, umo=profile.umo, text=text, resolver=self.meme_adapter,
                decision=result, dry_run=False, platform_name=platform_name, sent_texts=sent_texts,
            )
        finally:
            if sent_texts:
                reply_text = "\n".join(sent_texts)
                self._record_bot_text(profile, reply_text)
                await self._update_log_reply(result, reply_text, complete=result.text_sent is True)
                if result.action == "proactive":
                    self.sessions.mark_proactive(profile.umo)

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
        if self.settings.decision_log_include_content:
            record["replay_context"] = log_context
            if trigger:
                record.update(trigger_text=_log_text(trigger["text"]),
                              trigger_sender=trigger["sender_name"] or trigger["sender_id"])
        await self.sessions.append_log(record)

    async def _update_log_reply(self, result: DecisionResult, text: str, *, complete=True) -> None:
        if result.round_id and text:
            fields = {"reply_sent": True, "reply_complete": complete}
            if self.settings.decision_log_include_content:
                fields["reply_text"] = _log_text(text, 80)
            await self.sessions.update_round_log(result.round_id, **fields)

    # --- 定时判断与冷场主动发言 ---

    async def _silence_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(15)
                await self._check_timers()
                await self._check_silence()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("检查定时判断或冷场主动发言时出错")

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
        settings = self.settings
        if not settings.enabled:
            return
        self.sessions.prune_idle()
        now = time.time()
        for runtime in self.sessions.all_sessions():
            profile = runtime.profile
            if is_official_pipeline_busy(runtime) or runtime.lock.locked() or not runtime.last_user_ts:
                continue
            if self.sessions.silence_already_evaluated(profile.umo):
                continue
            if now - runtime.last_activity < settings.silence_seconds(profile.is_private):
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
        memes = None
        immediate_actions = has_on_match_actions(
            settings.decision_questions,
            scene,
            trigger_event=trigger_event,
            timer_roots=timer_roots,
        )
        if limit_reason and immediate_actions:
            memes = self._meme_candidates(settings)
            immediate_actions = has_on_match_actions(
                settings.decision_questions,
                scene,
                trigger_event=trigger_event,
                timer_roots=timer_roots,
                candidate_available={
                    "memes": bool(memes),
                    "meme_categories": bool(memes),
                    "recent_messages": any(
                        item.role == "user" and item.message_id for item in messages
                    ),
                },
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
        )
        result.dry_run = settings.dry_run
        runtime.last_decision = result
        if not settings.dry_run and not result.failed:
            await run_tools(self.context, umo=profile.umo, resolver=self.meme_adapter, decision=result, when="on_match")
        limited = ((not scheduled and not settings.proactive_enabled(profile.is_private))
                   or self.sessions.in_cooldown(profile.umo, settings.cooldown_seconds)
                   or not self.sessions.can_proactive(profile.umo, settings.hourly_proactive_limit(profile.is_private)))
        if limited:
            skip_tools(result, "主动发言已关闭、冷却中或已达次数上限", when="after_send")
        await self._log_decision(profile, result)
        if limited or result.failed or result.action != "proactive":
            return
        if settings.dry_run:
            logger.info("试运行：主动判断为可以开口，但不会发送：%s", profile.umo)
            return
        try:
            await self._self_generate_and_send(profile, result, None)
        finally:
            await self._finish_tool_log(result)

    # --- 官方流水线钩子 ---

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        decision = self._pipeline_decision(event)
        if decision is None:
            return
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

        async def judge_and_log():
            try:
                report = await evaluate_post_reply(self.context, plan, text, decision.answers, decision=decision)
                await self.sessions.update_round_log(decision.round_id, post_decision=report)
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
        if self._pipeline_decision(event):
            event.set_extra("_qd_intermediate_receipt", None)

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        decision = self._pipeline_decision(event)
        if decision is None:
            return
        result = event.get_result()
        if result is None:
            return
        if getattr(result, "is_llm_result", lambda: False)() and not event.get_extra("_qd_streaming"):
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
            release_official_pipeline(runtime)
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
        release_official_pipeline(self.sessions.get(event.unified_msg_origin))
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
