"""快速决策插件入口。"""

from __future__ import annotations

import asyncio
import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.platform import MessageType
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

from .config_util import PluginSettings
from .decision_engine import ask_decision
from .event_utils import (
    event_has_command_handler,
    event_profile,
    event_timestamp,
    inspect_mentions,
    is_self_message,
    to_buffered_message,
)
from .meme_adapter import MemeManagerAdapter, UNAVAILABLE_HINT
from .meme_migrate import maybe_migrate_legacy_library
from .plugin_paths import load_plugin_data_dir
from .models import BufferedMessage, DecisionResult, SceneType, SessionProfile
from .official_handoff import (
    begin_official_pipeline,
    build_inbound_user_prompt,
    collect_event_media_urls,
    is_official_pipeline_busy,
    make_official_llm_request,
    release_official_pipeline,
    resolve_session_conversation,
    should_use_default_official_agent,
    suppress_default_official_agent,
)
from .reply_generator import (
    after_official_text_sent,
    apply_session_hints,
    generate_text,
    install_direct_send_send_guard,
    prepare_official_result_chain,
    send_decision_message,
)
from .session_store import SessionStore
from .web_api import WebApiMixin

PLUGIN_NAME = "astrbot_plugin_quick_decision"


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
        self.register_plugin_web_apis()

    @property
    def settings(self) -> PluginSettings:
        return PluginSettings(self.config)

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
        if not self.settings.enabled:
            return

        if is_official_pipeline_busy(runtime):
            runtime.pending.append(buffered)
            event.stop_event()
            return

        runtime.deciding = True
        try:
            return await self._run_passive_round(event, profile)
        finally:
            if runtime.pending_pipeline is None:
                runtime.deciding = False
                runtime.pending.clear()

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
        if scene is None:
            return

        # 决策进行中若仍有新消息，最多再判断两次，保证只发出一轮回复。
        result: DecisionResult | None = None
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
                memes=self.meme_adapter.described_catalog(),
            )
            if not runtime.pending:
                break
            logger.info("决策期间收到新消息，将合并后重新判断：%s", profile.umo)

        if result is None:
            return
        result.dry_run = settings.dry_run
        runtime.last_decision = result

        if result.error:
            # 先落盘再返回，此时尚未决定是否走官方流水线。
            await self._log_decision(profile, result)
            if scene == "private_passive":
                logger.warning("私聊判断失败，将退回官方纯文字回复：%s", result.error)
                return
            event.stop_event()
            return

        if not result.should_speak():
            await self._log_decision(profile, result)
            event.stop_event()
            return

        if (
            scene == "group_passive"
            and not at_or_quoted
            and self.sessions.in_cooldown(profile.umo, settings.cooldown_seconds)
        ):
            await self._log_decision(profile, result)
            logger.info("会话处于冷却中，跳过群聊插话：%s", profile.umo)
            event.stop_event()
            return

        official_would_run = bool(event.is_at_or_wake_command)
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

        result.official_llm = True
        # 必须在赋值后再写日志，避免误记 official_llm=false。
        await self._log_decision(profile, result)
        event.set_extra("quick_decision", result)
        begin_official_pipeline(runtime, result)

        if use_default_official:
            # 私聊 / 普通 @ 且未合并：放行官方默认 Agent，由官方收集附件与历史。
            return None

        # 群聊未 @ 插话，或 debounce 合并了多条用户消息：yield 公开 request_llm。
        # 主生成事件不 stop_event，以免跳过装饰/发送/after-send；用 should_call_llm(True)
        # 阻止默认第二条 Agent。兄弟事件仍在 on_chat_message 里 stop_event。
        suppress_default_official_agent(event)
        conversation = await resolve_session_conversation(self.context, event)
        if conversation is None:
            logger.error("无法取得官方会话，取消本轮官方生成：%s", profile.umo)
            release_official_pipeline(runtime)
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
            release_official_pipeline(runtime)
            return None
        return make_official_llm_request(
            event,
            conversation=conversation,
            prompt=prompt,
            image_urls=image_urls,
            audio_urls=audio_urls,
        )

    def _resolve_passive_scene(
        self, event: AstrMessageEvent, profile: SessionProfile
    ) -> tuple[bool, SceneType | None]:
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
        if not at_or_quoted and not self.settings.group_enable_interrupt:
            return at_or_quoted, None
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
        except Exception:
            logger.exception("自行调用聊天模型生成回复失败")
            return
        if not text:
            logger.warning("聊天模型没有返回文本，取消发送：%s", profile.umo)
            return
        from .official_segment import platform_name_from_event

        sent = await send_decision_message(
            self.context,
            umo=profile.umo,
            text=text,
            resolver=self.meme_adapter,
            decision=result,
            dry_run=False,
            quote_target=self.settings.should_quote_target(profile.is_private),
            platform_name=platform_name_from_event(event),
        )
        if sent:
            self._record_bot_text(profile, text)
            if result.action == "proactive":
                self.sessions.mark_proactive(profile.umo)

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
        self, profile: SessionProfile, result: DecisionResult
    ) -> None:
        await self.sessions.append_log(
            {
                "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "umo": profile.umo,
                "scene": result.scene,
                "action": result.action,
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
            }
        )

    # --- 冷场主动发言 ---

    async def _silence_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(15)
                await self._check_silence()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("检查冷场主动发言时出错")

    async def _check_silence(self) -> None:
        settings = self.settings
        if not settings.enabled:
            return
        now = time.time()
        for runtime in self.sessions.all_sessions():
            profile = runtime.profile
            if runtime.deciding or not runtime.last_user_ts:
                continue
            if not settings.proactive_enabled(profile.is_private):
                continue
            if self.sessions.silence_already_evaluated(profile.umo):
                continue
            last_activity = max(runtime.last_user_ts, runtime.last_bot_ts or 0)
            if now - last_activity < settings.silence_seconds(profile.is_private):
                continue
            if self.sessions.in_cooldown(profile.umo, settings.cooldown_seconds):
                continue
            if not self.sessions.can_proactive(
                profile.umo, settings.hourly_proactive_limit(profile.is_private)
            ):
                self.sessions.mark_silence_evaluated(profile.umo)
                logger.info("已达每小时主动上限，跳过本轮冷场评估：%s", profile.umo)
                continue
            runtime.deciding = True
            try:
                await self._run_proactive(runtime)
            finally:
                runtime.deciding = False

    async def _run_proactive(self, runtime) -> None:
        profile: SessionProfile = runtime.profile
        settings = self.settings
        self.sessions.mark_silence_evaluated(profile.umo)
        scene: SceneType = (
            "private_proactive" if profile.is_private else "group_proactive"
        )
        messages = self.sessions.recent_messages(
            profile.umo, settings.context_message_limit
        )
        result = await ask_decision(
            self.context,
            settings=settings,
            scene=scene,
            is_private=profile.is_private,
            messages=messages,
            at_or_quoted=False,
            memes=self.meme_adapter.described_catalog(),
        )
        result.dry_run = settings.dry_run
        runtime.last_decision = result
        await self._log_decision(profile, result)
        if result.error or result.action != "proactive":
            return
        if settings.dry_run:
            logger.info("试运行：冷场判断为可以主动开口，但不会发送：%s", profile.umo)
            return
        await self._self_generate_and_send(profile, result, None)

    # --- 官方流水线钩子 ---

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        decision = self._pipeline_decision(event)
        if decision is None:
            return
        if self.settings.dry_run:
            event.stop_event()
            release_official_pipeline(self.sessions.get(event.unified_msg_origin))
            return
        apply_session_hints(req, decision)

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        decision = self._pipeline_decision(event)
        if decision is None:
            return
        result = event.get_result()
        if result is None:
            return
        is_private = event.is_private_chat()
        if (
            self.settings.group_direct_send
            and not is_private
        ):
            install_direct_send_send_guard(event)
        if not prepare_official_result_chain(
            result.chain,
            dry_run=self.settings.dry_run,
            should_speak=decision.should_speak(),
            group_direct_send=self.settings.group_direct_send,
            is_private=is_private,
        ):
            event.stop_event()
            release_official_pipeline(self.sessions.get(event.unified_msg_origin))
            return

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent):
        # 先根据本事件上的决策记状态并清 pending，再交给钩子发独立表情。
        # 表情发送会消费 extra，重复进入或其它事件不会再发图。
        decision = event.get_extra("quick_decision")
        runtime = self.sessions.get(event.unified_msg_origin)
        if runtime is not None and isinstance(decision, DecisionResult):
            text = ""
            result = event.get_result()
            if result and result.chain:
                text = "".join(
                    getattr(comp, "text", "")
                    for comp in result.chain
                    if isinstance(comp, Plain)
                ).strip()
            if text:
                self._record_bot_text(runtime.profile, text)
            else:
                self.sessions.mark_bot_sent(runtime.profile.umo)
            release_official_pipeline(runtime)
        await after_official_text_sent(
            event,
            context=self.context,
            resolver=self.meme_adapter,
            dry_run=self.settings.dry_run,
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
            "判断提供商："
            + (settings.decision_provider_id or "（默认，需先在 WebUI 配置）"),
            "表情包来源："
            + (
                f"meme_manager {status.plugin_version or ''}，{status.candidate_count} 张可用"
                if (status := self.meme_adapter.status()).available
                else (status.message or "不可用，仅发文字")
            ),
            f"判断阈值：{settings.choice_threshold}",
        ]
        runtime = self.sessions.get(event.unified_msg_origin)
        result = runtime.last_decision if runtime else None
        if result:
            lines.append("最近一次决策：")
            length_text = result.length or "（未判断）"
            lines.append(
                f"- 场景 {result.scene} / 动作 {result.action} / 长度 {length_text}"
            )
            if result.custom_answers:
                rendered = ", ".join(
                    f"{key}={value}" for key, value in result.custom_answers.items()
                )
                lines.append(f"- 自定义判断：{rendered}")
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
