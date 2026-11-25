"""对接通用 TTS 的隔离测试：由判断决定发语音还是文字、语音替换正文后的记录、主动发言转语音、朗读语气。

不访问网络，不发 QQ。
"""

# ruff: noqa: E402, I001

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_official_inbound_handoff import FakeEvent, _make_plugin, _profile

# isort: split
from test_decision_engine import DecisionBooleanAnswer, choice, message
from astrbot.api.message_components import Plain
from astrbot_plugin_quick_decision import reply_generator, web_api
from astrbot_plugin_quick_decision.config_util import PluginSettings, default_graph, load_questions
from astrbot_plugin_quick_decision.decision_engine import ask_decision, evaluate_post_reply, preview_workflow
from astrbot_plugin_quick_decision.models import DecisionResult
from astrbot_plugin_quick_decision.reply_generator import send_decision_message, session_hint_text
from astrbot_plugin_quick_decision.tts_adapter import (
    _GENERAL_RESULT,
    _LLM_RESULT,
    UNIVERSAL_TTS_STAR_NAME,
    UniversalTTSAdapter,
    toned_engine,
)

UMO = "aiocqhttp:GroupMessage:100"


class Record:
    def __init__(self, file: str = "", url: str = "") -> None:
        self.file = file
        self.url = url


class MessageChain:
    def __init__(self) -> None:
        self.chain: list = []

    def message(self, text):
        self.chain.append(Plain(text))
        return self


class Result:
    """AstrBot MessageEventResult 中通用 TTS 和快速决策会用到的部分。"""

    def __init__(self, chain) -> None:
        self.chain = chain
        self.kind = _LLM_RESULT

    def is_llm_result(self) -> bool:
        return self.kind == _LLM_RESULT

    def set_result_content_type(self, kind) -> None:
        self.kind = kind

    def derive(self, chain):
        derived = Result(chain)
        derived.kind = self.kind
        return derived


class MimoEngine:
    """通用 TTS 的 MiMo V2.5-TTS 引擎：style 是一组风格词，带懒加载的 HTTP 客户端。"""

    def __init__(self, style: str = "温柔") -> None:
        self.instance_name = "mimo"
        self.style = style
        self.style_prompt = ""
        self.singing_mode = False
        self._client = None
        self.spoken: list[tuple[str, str, object]] = []

    def _get_client(self):
        if self._client is None:
            self._client = object()
        return self._client

    async def synthesize(self, text):
        self.spoken.append((self.style, text, self._get_client()))
        return b"RIFF", "wav"


class AzureEngine(MimoEngine):
    role = ""


class FakeUniversalTTS:
    """按通用 TTS 1.4.5 tts_hook 的流程写的替身：

    暂停或概率落空时把结果标成普通消息并返回；只处理模型结果；合并全部文字，用会话引擎合成，
    第一段文字换成一条语音、其余文字删掉；再包一层 event.send，语音发送抛错时改发原始组件。
    概率介于 0 和 100 之间时用 dice 代替随机数。
    """

    def __init__(self, engine, *, probability: int = 100) -> None:
        self.config = {"enable": True, "tts_probability": probability, "fallback_to_text_on_send_error": True}
        self._engine = engine
        self._runtime_enabled = True
        self._session_bindings: dict[str, str] = {}
        self._tts_audio_text: dict[str, str] = {}
        self.hook_calls = 0
        self.dice = 100

    def _get_engine_for_session(self, session_id):
        return None if self._session_bindings.get(session_id) == "__disabled__" else self._engine

    def _suppress_core_tts(self, result) -> None:
        if result.is_llm_result():
            result.set_result_content_type(_GENERAL_RESULT)

    def _install_send_fallback(self, event, result, original_chain) -> None:
        original = event.send
        fallback = result.derive(list(original_chain))

        async def send(chain):
            try:
                return await original(chain)
            except Exception:  # noqa: BLE001
                return await original(fallback)

        event.send = send

    async def tts_hook(self, event):
        self.hook_calls += 1
        result = event.get_result()
        probability = self.config["tts_probability"]
        if not self._runtime_enabled or probability <= 0 or (probability < 100 and self.dice > probability):
            self._suppress_core_tts(result)
            return
        if not result or not result.chain or not result.is_llm_result():
            return
        text = "".join(c.text for c in result.chain if isinstance(c, Plain) and c.text).strip()
        if not text:
            return
        engine = self._get_engine_for_session(event.unified_msg_origin)
        if engine is None:
            self._suppress_core_tts(result)
            return
        try:
            _, fmt = await engine.synthesize(text)
        except Exception:  # noqa: BLE001
            return
        path = f"/tmp/universal_tts/tts_{self.hook_calls}.{fmt}"
        self._tts_audio_text[path] = text
        record = Record(file=path, url=path)
        original_chain = list(result.chain)
        rebuilt, inserted = [], False
        for comp in result.chain:
            if isinstance(comp, Plain):
                if comp.text and not inserted:
                    rebuilt.append(record)
                    inserted = True
                continue
            rebuilt.append(comp)
        result.chain = rebuilt
        self._install_send_fallback(event, result, original_chain)


def tts_context(universal=None, *, enable=True, provider_id="", dual_output=False, streaming=False, **extra):
    star = SimpleNamespace(star_cls=universal, activated=True, version="1.4.5") if universal else None
    return SimpleNamespace(
        get_registered_star=lambda name: star if name == UNIVERSAL_TTS_STAR_NAME else None,
        astrbot_config={"provider_tts_settings": {"enable": enable, "provider_id": provider_id,
                                                  "dual_output": dual_output},
                        "provider_settings": {"streaming_response": streaming}},
        **extra,
    )


def tip_states(context):
    status = UniversalTTSAdapter(context).status().to_dict()
    return [tip.state(status) for tip in web_api.EXTERNAL_PLUGINS["universal_tts"].tips]


def is_record(comp) -> bool:
    return type(comp).__name__ == "Record"


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_status_explains_what_blocks_voice(self):
        missing = UniversalTTSAdapter(tts_context()).status()
        self.assertFalse(missing.available)
        self.assertFalse(missing.plugin_loaded)
        self.assertIn("astrbot_plugin_universal_tts", missing.message)

        universal = FakeUniversalTTS(MimoEngine())
        ready = UniversalTTSAdapter(tts_context(universal)).status()
        self.assertTrue(ready.available)
        self.assertEqual((ready.plugin_version, ready.engine, ready.message), ("1.4.5", "mimo", ""))

        switch_off = UniversalTTSAdapter(tts_context(universal, enable=False)).status()
        self.assertFalse(switch_off.available)
        self.assertIn("启用 TTS", switch_off.message)

        both = UniversalTTSAdapter(tts_context(universal, provider_id="edge_tts", dual_output=True)).status()
        self.assertEqual(both.core_provider, "edge_tts")
        details = {row["label"]: row["value"] for row in
                   web_api.EXTERNAL_PLUGINS["universal_tts"].details(both.to_dict())}
        self.assertIn("edge_tts", details["AstrBot 内置 TTS 提供商"])
        self.assertIn("各发一遍", details["注意"])

        universal._runtime_enabled = False
        paused = UniversalTTSAdapter(tts_context(universal)).status()
        self.assertTrue(paused.available)
        self.assertIn("/tts_off", paused.message)

    def test_tips_are_checked_against_current_settings(self):
        universal = FakeUniversalTTS(MimoEngine(), probability=100)
        # 依次为：触发概率 0、启用 TTS、提供商留空、发送失败回退文字、关闭流式输出。
        self.assertEqual(tip_states(tts_context(universal)), [False, True, True, True, True])
        universal.config.update(tts_probability=0, fallback_to_text_on_send_error=False)
        self.assertEqual(tip_states(tts_context(universal, enable=False, provider_id="edge_tts", streaming=True)),
                         [True, False, False, False, False])
        # 没装通用 TTS 时只检查 AstrBot 自己的设置。
        self.assertEqual(tip_states(tts_context()), [None, True, True, None, True])

    async def test_voice_record_reuses_universal_rules(self):
        engine = MimoEngine()
        universal = FakeUniversalTTS(engine)
        adapter = UniversalTTSAdapter(tts_context(universal))
        record = await adapter.voice_record(UMO, "大家晚上好")
        self.assertTrue(is_record(record))
        self.assertEqual(universal._tts_audio_text[record.file], "大家晚上好")
        self.assertEqual(engine.spoken[0][:2], ("温柔", "大家晚上好"))

        universal.config["tts_probability"] = 0
        self.assertIsNone(await adapter.voice_record(UMO, "这句不转"))
        universal.config["tts_probability"] = 100
        universal._session_bindings[UMO] = "__disabled__"
        self.assertIsNone(await adapter.voice_record(UMO, "本会话禁音"))

        with patch.object(FakeUniversalTTS, "tts_hook", AsyncMock(side_effect=RuntimeError("引擎崩了"))):
            self.assertIsNone(await adapter.voice_record(UMO, "失败时改发文字"))
        self.assertIsNone(await UniversalTTSAdapter(tts_context()).voice_record(UMO, "没装插件"))

    async def test_forced_voice_ignores_probability_but_keeps_other_rules(self):
        universal = FakeUniversalTTS(MimoEngine(), probability=0)
        adapter = UniversalTTSAdapter(tts_context(universal))
        self.assertTrue(is_record(await adapter.voice_record(UMO, "判断说用语音", force=True)))
        self.assertEqual(universal.config["tts_probability"], 0, "不改通用 TTS 自己的配置")
        universal._runtime_enabled = False
        self.assertIsNone(await adapter.voice_record(UMO, "/tts_off 仍然有效", force=True))
        universal._runtime_enabled = True
        universal._session_bindings[UMO] = "__disabled__"
        self.assertIsNone(await adapter.voice_record(UMO, "会话禁音仍然有效", force=True))

    async def test_tone_uses_a_styled_copy_and_shares_the_client(self):
        engine = MimoEngine(style="温柔，慵懒")
        universal = FakeUniversalTTS(engine)
        adapter = UniversalTTSAdapter(tts_context(universal))
        await adapter.voice_record(UMO, "早上好", tone="开心")
        await adapter.voice_record(UMO, "再说一句")
        (toned_style, _, toned_client), (plain_style, _, plain_client) = engine.spoken
        self.assertEqual(toned_style, "温柔，慵懒 开心")
        self.assertEqual(plain_style, "温柔，慵懒")
        self.assertIs(toned_client, plain_client)
        self.assertEqual(engine.style, "温柔，慵懒")

    def test_tone_per_engine_type(self):
        azure = AzureEngine(style="calm")
        self.assertEqual(toned_engine(azure, "cheerful").style, "cheerful")
        clone = SimpleNamespace(style_prompt="像朋友一样说话")
        self.assertIn("「撒娇」", toned_engine(clone, "撒娇").style_prompt)
        plain = SimpleNamespace(instance_name="openai")
        self.assertIs(toned_engine(plain, "开心"), plain)
        self.assertIs(toned_engine(azure, "  "), azure)

    def test_settle_reverts_unwanted_voice_and_restores_result_type(self):
        adapter = UniversalTTSAdapter(tts_context(FakeUniversalTTS(MimoEngine())))
        text, image = Plain("只发文字"), SimpleNamespace(kind="image")
        result = Result([Record("/tmp/a.wav"), image])
        adapter.settle(result, voice=False, original=[text, image])
        self.assertEqual(result.chain, [text, image])
        self.assertTrue(result.is_llm_result())

        skipped = Result([text])
        skipped.kind = _GENERAL_RESULT
        adapter.settle(skipped, voice=True, original=[text])
        self.assertTrue(skipped.is_llm_result(), "跳过后改回模型结果，AstrBot 分段回复照常生效")

        core = UniversalTTSAdapter(tts_context(FakeUniversalTTS(MimoEngine()), provider_id="edge_tts"))
        guarded = Result([text])
        core.settle(guarded, voice=False, original=[text])
        self.assertFalse(guarded.is_llm_result(), "内置 TTS 选着提供商时保持普通消息，免得它再转语音")


class ProactiveVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sent: list[list] = []

        async def send_message(umo, chain):
            if self.fail_voice and any(is_record(c) for c in chain.chain):
                raise RuntimeError("语音发送失败")
            self.sent.append(list(chain.chain))
            return True

        self.fail_voice = False
        self.engine = MimoEngine()
        self.universal = FakeUniversalTTS(self.engine)
        self.context = tts_context(self.universal, send_message=send_message)
        self.adapter = UniversalTTSAdapter(self.context)
        self.chain_patch = patch.object(reply_generator, "MessageChain", MessageChain)
        self.chain_patch.start()

    async def asyncTearDown(self):
        self.chain_patch.stop()

    async def send(self, decision, *, fallback=True):
        texts: list[str] = []

        async def voice(text):
            return await self.adapter.voice_record(UMO, text, tone=decision.voice_tone)

        ok = await send_decision_message(
            self.context, umo=UMO, text="冷场好久啦。大家在忙什么？", resolver=SimpleNamespace(resolve_image=lambda _: None),
            decision=decision, dry_run=False, sent_texts=texts, voice=voice, voice_fallback=fallback,
        )
        return ok, texts

    async def test_voice_replaces_text_and_is_sent_once(self):
        decision = DecisionResult(action="proactive", scene="group_proactive", voice_tone="俏皮")
        ok, texts = await self.send(decision)
        self.assertTrue(ok)
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(is_record(self.sent[0][0]) and len(self.sent[0]) == 1)
        self.assertEqual(texts, ["冷场好久啦。大家在忙什么？"])
        self.assertTrue(decision.text_sent and decision.voice_sent)
        self.assertEqual(self.engine.spoken[0][0], "温柔 俏皮")

    async def test_failed_voice_falls_back_to_text_only_when_allowed(self):
        self.fail_voice = True
        decision = DecisionResult(action="proactive", scene="group_proactive")
        ok, texts = await self.send(decision)
        self.assertTrue(ok)
        self.assertEqual([c.text for chain in self.sent for c in chain], ["冷场好久啦。大家在忙什么？"])
        self.assertFalse(decision.voice_sent)

        self.sent.clear()
        decision = DecisionResult(action="proactive", scene="group_proactive")
        ok, texts = await self.send(decision, fallback=False)
        self.assertFalse(ok)
        self.assertEqual((self.sent, texts, decision.text_sent), ([], [], False))

    async def test_skipped_voice_sends_text(self):
        self.universal.config["tts_probability"] = 0
        decision = DecisionResult(action="proactive", scene="group_proactive")
        ok, _ = await self.send(decision)
        self.assertTrue(ok)
        self.assertFalse(any(is_record(c) for chain in self.sent for c in chain))

    async def test_plugin_follows_the_voice_decision_for_proactive_rounds(self):
        # 通用 TTS 的概率设为 0：是否转语音只看判断。
        self.universal.config["tts_probability"] = 0
        with tempfile.TemporaryDirectory() as tmp:
            plugin, main = _make_plugin(tmp)
            plugin.context = self.context
            plugin.tts_adapter = self.adapter
            profile = _profile(FakeEvent(umo=UMO))

            async def run(**decision):
                await plugin._self_generate_and_send(
                    profile, DecisionResult(action="proactive", scene="group_proactive", **decision), None)
                return self.sent[-1][0]

            with patch.object(main, "generate_text", AsyncMock(return_value="有人在吗")):
                self.assertTrue(is_record(await run(voice_mode="voice")))
                self.assertEqual(plugin.sessions.recent_messages(UMO, 5)[-1].text, "有人在吗")
                self.assertEqual((await run(voice_mode="text")).text, "有人在吗")
                self.assertEqual((await run()).text, "有人在吗", "没有判断时按通用 TTS 的概率")

                self.universal.config["tts_probability"] = 100
                calls = self.universal.hook_calls
                self.assertEqual((await run(voice_mode="text")).text, "有人在吗")
                self.assertEqual(self.universal.hook_calls, calls, "只发文字时不调用通用 TTS")

                async def judge_tone(context, plan, text, answers, *, decision):
                    await asyncio.sleep(0.01)
                    decision.voice_tone = "温柔"
                    return {"answers": {"voice_tone": "gentle"}}

                plan = SimpleNamespace(specs=[SimpleNamespace(actions={"gentle": [{"tool": "voice_tone", "text": "温柔"}]})])
                with patch.object(main, "evaluate_post_reply", side_effect=judge_tone):
                    record = await run(voice_mode="voice", post_plan=plan)
                    await asyncio.gather(*plugin._post_tasks)
                self.assertTrue(is_record(record))
                self.assertEqual(self.engine.spoken[-1][:2], ("温柔 温柔", "有人在吗"), "等正文生成后的语气判断")

                plugin.config["external_plugins"] = {"universal_tts": {"proactive_voice": False}}
                self.assertEqual((await run(voice_mode="voice")).text, "有人在吗")
                plugin.config["external_plugins"] = {"universal_tts": {"enabled": False}}
                self.assertFalse(is_record(await run(voice_mode="voice")))


class PassiveVoiceTests(unittest.IsolatedAsyncioTestCase):
    """官方流水线：本插件的装饰钩子先跑，随后通用 TTS，再是本插件的收尾钩子，最后 RespondStage 调 event.send。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.plugin, self.main = _make_plugin(self.tmp.name)
        self.engine = MimoEngine()
        self.universal = FakeUniversalTTS(self.engine)
        self.use_context()

    def use_context(self, **extra):
        context = tts_context(self.universal, conversation_manager=self.plugin.context.conversation_manager,
                              register_web_api=lambda *a, **k: None, **extra)
        self.plugin.context = context
        self.plugin.tts_adapter = UniversalTTSAdapter(context)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def round(self, decision, *, send=None, text="今天天气不错"):
        event = FakeEvent(text="天气怎么样", at_or_wake=True)
        if send is not None:
            event.send = send
        with patch.object(self.main, "ask_decision", AsyncMock(return_value=decision)):
            await self.plugin.on_chat_message(event)
        result = Result([Plain(text)])
        event.set_result(result)
        await self.plugin.on_decorating_result(event)
        await self.universal.tts_hook(event)
        await self.plugin.settle_voice_choice(event)
        self.final_kind = result.kind
        await event.send(result)
        await self.plugin.after_message_sent(event)
        return event, self.plugin.sessions.recent_logs()[0]

    async def test_voice_reply_counts_as_sent_and_keeps_the_text_in_context(self):
        event, row = await self.round(DecisionResult(action="reply"))
        delivered = event._sends[0].chain
        self.assertEqual(len(delivered), 1)
        self.assertTrue(is_record(delivered[0]))
        self.assertTrue(row["reply_sent"])
        self.assertTrue(row["reply_voice"])
        self.assertEqual(row["reply_text"], "今天天气不错")
        self.assertEqual(self.plugin.sessions.recent_messages(UMO, 5)[-1].text, "今天天气不错")

    async def test_skipped_voice_still_gets_quote_and_receipt(self):
        self.universal.config["tts_probability"] = 0
        event, row = await self.round(DecisionResult(action="reply", target_message_id="t1"))
        delivered = event._sends[0].chain
        self.assertEqual([type(c).__name__ for c in delivered], ["Reply", "Plain"])
        self.assertTrue(row["reply_sent"])
        self.assertNotIn("reply_voice", row)

    async def test_text_fallback_after_voice_error_is_recorded(self):
        sent = []

        async def send(message):
            if any(is_record(c) for c in message.chain):
                raise RuntimeError("NapCat 拒收语音")
            sent.append(message)
            return True

        event, row = await self.round(DecisionResult(action="reply"), send=send)
        self.assertEqual([c.text for c in sent[0].chain], ["今天天气不错"])
        self.assertTrue(row["reply_sent"])
        self.assertNotIn("reply_voice", row)

    async def test_voice_decision_ignores_universal_probability(self):
        self.universal.config["tts_probability"] = 0
        event, row = await self.round(DecisionResult(action="reply", voice_mode="voice", voice_tone="开心"))
        self.assertTrue(is_record(event._sends[0].chain[0]))
        self.assertTrue(row["reply_voice"])
        self.assertEqual((row["voice_mode"], row["voice_tone"]), ("voice", "开心"))
        self.assertEqual([style for style, _, _ in self.engine.spoken], ["温柔 开心"])
        self.assertEqual(self.final_kind, _LLM_RESULT)

    async def test_text_decision_undoes_probability_voice(self):
        event, row = await self.round(DecisionResult(action="reply", voice_mode="text", target_message_id="t1"))
        self.assertEqual([type(c).__name__ for c in event._sends[0].chain], ["Reply", "Plain"])
        self.assertEqual(event._sends[0].chain[1].text, "今天天气不错")
        self.assertTrue(row["reply_sent"])
        self.assertNotIn("reply_voice", row)
        self.assertEqual(self.final_kind, _LLM_RESULT)

    async def test_text_decision_keeps_segmenting_when_universal_skips(self):
        self.universal.config["tts_probability"] = 0
        await self.round(DecisionResult(action="reply", voice_mode="text"))
        self.assertEqual(self.final_kind, _LLM_RESULT)
        self.use_context(provider_id="edge_tts")
        await self.round(DecisionResult(action="reply", voice_mode="text"))
        self.assertEqual(self.final_kind, _GENERAL_RESULT, "内置 TTS 选着提供商时不让它转语音")

    async def test_voice_decision_still_respects_universal_rules(self):
        self.universal._runtime_enabled = False
        event, row = await self.round(DecisionResult(action="reply", voice_mode="voice"))
        self.assertEqual([type(c).__name__ for c in event._sends[0].chain], ["Plain"])
        self.assertNotIn("reply_voice", row)
        self.assertEqual(self.engine.spoken, [])

    async def test_voice_waits_for_the_tone_judged_after_the_reply(self):
        decision = DecisionResult(action="reply", voice_mode="voice")
        decision.tone_judged = asyncio.Event()

        async def judge_later():
            await asyncio.sleep(0.01)
            decision.voice_tone = "开心"
            decision.tone_judged.set()

        task = asyncio.create_task(judge_later())
        event, row = await self.round(decision)
        await task
        self.assertEqual([style for style, _, _ in self.engine.spoken], ["温柔 开心"])
        self.assertTrue(is_record(event._sends[0].chain[0]))

    async def test_slow_tone_judgment_falls_back_to_the_engine_tone(self):
        decision = DecisionResult(action="reply", voice_mode="voice")
        decision.tone_judged = asyncio.Event()
        with patch.object(self.main, "VOICE_TONE_WAIT_SECONDS", 0.01):
            event, _ = await self.round(decision)
        self.assertEqual([style for style, _, _ in self.engine.spoken], ["温柔"])
        self.assertTrue(is_record(event._sends[0].chain[0]))

    async def test_text_rounds_do_not_wait_for_the_tone(self):
        self.universal.config["tts_probability"] = 0
        decision = DecisionResult(action="reply", voice_mode="text")
        decision.tone_judged = asyncio.Event()
        event, _ = await asyncio.wait_for(self.round(decision), 2)
        self.assertEqual([type(c).__name__ for c in event._sends[0].chain], ["Plain"])

    async def test_tone_without_voice_decision_is_used_once(self):
        _, row = await self.round(DecisionResult(action="reply", voice_tone="开心"))
        self.assertEqual([style for style, _, _ in self.engine.spoken], ["温柔 开心"])
        self.assertEqual(self.engine.style, "温柔")
        self.assertTrue(row["reply_voice"])

    async def test_adapter_disabled_leaves_universal_alone(self):
        self.plugin.config["external_plugins"] = {"universal_tts": {"enabled": False}}
        event, _ = await self.round(DecisionResult(action="reply", voice_mode="text", voice_tone="开心"))
        self.assertEqual([style for style, _, _ in self.engine.spoken], ["温柔"])
        self.assertTrue(is_record(event._sends[0].chain[0]))

    def test_voice_questions_need_a_usable_tts(self):
        self.assertTrue(self.plugin._voice_available("group_passive"))
        self.assertTrue(self.plugin._voice_available("group_proactive"))
        self.plugin.config["external_plugins"] = {"universal_tts": {"proactive_voice": False}}
        self.assertTrue(self.plugin._voice_available("group_passive"))
        self.assertFalse(self.plugin._voice_available("group_proactive"))
        self.plugin.config["external_plugins"] = {"universal_tts": {"enabled": False}}
        self.assertFalse(self.plugin._voice_available("group_passive"))
        self.plugin.config.pop("external_plugins")
        self.universal._runtime_enabled = False
        self.assertFalse(self.plugin._voice_available("group_passive"))
        self.plugin.tts_adapter = UniversalTTSAdapter(tts_context())
        self.assertFalse(self.plugin._voice_available("group_passive"))


class VoiceQuestionTests(unittest.IsolatedAsyncioTestCase):
    def settings(self):
        graph = {"version": 6, "questions": [q.to_dict() for q in default_graph()]}
        loaded, error = load_questions(graph, strict=True)
        self.assertIsNone(error)
        return PluginSettings({"decision_questions": graph})

    async def ask(self, settings, answers, *, voice_available=True):
        judge = AsyncMock(return_value=answers)
        result = await ask_decision(SimpleNamespace(decision=SimpleNamespace(ask=judge)), settings=settings,
                                    scene="group_passive", is_private=False, messages=[message("m1")],
                                    memes=[], at_or_quoted=True, voice_available=voice_available)
        self.assertFalse(result.failed, result.error)
        asked = {qid for call in judge.await_args_list for qid in call.args[0]} if judge.await_args_list else set()
        return result, asked

    async def test_default_graph_decides_voice_and_tone(self):
        settings = self.settings()
        base = {"action": choice("reply", reply=1), "address_message": DecisionBooleanAnswer(probability=0),
                "length": choice("short", short=1), "use_meme": DecisionBooleanAnswer(probability=0)}
        voiced, asked = await self.ask(settings, {**base, "use_voice": DecisionBooleanAnswer(probability=0.9)})
        self.assertIn("use_voice", asked)
        self.assertNotIn("voice_tone", asked, "语气在正文生成后才判断")
        self.assertEqual((voiced.voice_mode, voiced.voice_tone), ("voice", ""))
        self.assertIn("适合朗读", session_hint_text(voiced))

        judge = AsyncMock(return_value={"voice_tone": choice("happy", happy=0.9)})
        report = await evaluate_post_reply(SimpleNamespace(decision=SimpleNamespace(ask=judge)), voiced.post_plan,
                                           "今天抽到大奖啦！", voiced.answers, decision=voiced)
        self.assertNotIn("error", report)
        self.assertEqual(list(judge.await_args.args[0]), ["voice_tone"])
        self.assertIn("今天抽到大奖啦！", repr(judge.await_args))
        self.assertEqual(voiced.voice_tone, "开心")

        texted, _ = await self.ask(settings, {**base, "use_voice": DecisionBooleanAnswer(probability=0.1)})
        self.assertEqual((texted.voice_mode, texted.voice_tone), ("text", ""))
        self.assertIsNone(texted.post_plan, "只发文字时不问语气")
        self.assertNotIn("适合朗读", session_hint_text(texted) or "")

    async def test_voice_questions_are_skipped_without_tts(self):
        settings = self.settings()
        answers = {"action": choice("reply", reply=1), "use_voice": DecisionBooleanAnswer(probability=1),
                   "voice_tone": choice("happy", happy=1)}
        result, asked = await self.ask(settings, answers, voice_available=False)
        self.assertFalse(asked & {"use_voice", "voice_tone"})
        self.assertEqual((result.voice_mode, result.voice_tone), ("", ""))

        preview = preview_workflow(settings, "group_passive", {"action": "reply"}, voice_available=False)
        self.assertFalse({"use_voice", "voice_tone"} & {q for b in preview["batches"] for q in b["asked"]})
        preview = preview_workflow(settings, "group_passive", {"action": "reply"})
        self.assertIn("use_voice", {q for b in preview["batches"] for q in b["asked"]})

    def test_tone_action_needs_text(self):
        tone = next(q for q in default_graph() if q.id == "voice_tone").to_dict()
        tone["actions"]["happy"] = [{"tool": "voice_tone"}]
        tone["triggers"] = [{"event": "message_received"}]
        _, error = load_questions({"version": 6, "questions": [tone]}, strict=True)
        self.assertIn("设定语音语气", error)

    def test_voice_questions_are_default_questions(self):
        defaults = [q["id"] for q in web_api.v8_default_questions()]
        self.assertEqual(defaults[-2:], ["use_voice", "voice_tone"])
        self.assertEqual(web_api.EXTERNAL_PLUGINS["universal_tts"].default_questions, ("use_voice", "voice_tone"))
        self.assertTrue(PluginSettings({}).tts_proactive_voice)
        off = PluginSettings({"external_plugins": {"universal_tts": {"proactive_voice": False}}})
        self.assertFalse(off.tts_proactive_voice)


if __name__ == "__main__":
    unittest.main()
