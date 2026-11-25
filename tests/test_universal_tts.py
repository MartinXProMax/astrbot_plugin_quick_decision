"""对接通用 TTS 的隔离测试：语音替换正文后的记录、主动发言转语音、判断语气。不访问网络，不发 QQ。"""

# ruff: noqa: E402, I001

from __future__ import annotations

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
from astrbot_plugin_quick_decision.config_util import PluginSettings, load_questions, optional_questions
from astrbot_plugin_quick_decision.decision_engine import ask_decision
from astrbot_plugin_quick_decision.models import DecisionResult
from astrbot_plugin_quick_decision.reply_generator import send_decision_message, session_hint_text
from astrbot_plugin_quick_decision.tts_adapter import UNIVERSAL_TTS_STAR_NAME, UniversalTTSAdapter, toned_engine

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
    """AstrBot MessageEventResult 中通用 TTS 会用到的部分。"""

    def __init__(self, chain) -> None:
        self.chain = chain
        self.kind = "llm"

    def is_llm_result(self) -> bool:
        return self.kind == "llm"

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
    """

    def __init__(self, engine, *, probability: int = 100) -> None:
        self.config = {"enable": True, "tts_probability": probability, "fallback_to_text_on_send_error": True}
        self._engine = engine
        self._runtime_enabled = True
        self._session_bindings: dict[str, str] = {}
        self._tts_audio_text: dict[str, str] = {}
        self.hook_calls = 0

    def _get_engine_for_session(self, session_id):
        return None if self._session_bindings.get(session_id) == "__disabled__" else self._engine

    def _suppress_core_tts(self, result) -> None:
        if result.is_llm_result():
            result.set_result_content_type("general")

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
        if not self._runtime_enabled or self.config["tts_probability"] <= 0:
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


def tts_context(universal=None, *, enable=True, provider_id="", dual_output=False, **extra):
    star = SimpleNamespace(star_cls=universal, activated=True, version="1.4.5") if universal else None
    return SimpleNamespace(
        get_registered_star=lambda name: star if name == UNIVERSAL_TTS_STAR_NAME else None,
        astrbot_config={"provider_tts_settings": {"enable": enable, "provider_id": provider_id,
                                                  "dual_output": dual_output}},
        **extra,
    )


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

    async def test_plugin_uses_voice_for_proactive_rounds_unless_turned_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, main = _make_plugin(tmp)
            plugin.context = self.context
            plugin.tts_adapter = self.adapter
            profile = _profile(FakeEvent(umo=UMO))
            with patch.object(main, "generate_text", AsyncMock(return_value="有人在吗")):
                await plugin._self_generate_and_send(profile, DecisionResult(action="proactive", scene="group_proactive"), None)
                self.assertTrue(is_record(self.sent[-1][0]))
                self.assertEqual(plugin.sessions.recent_messages(UMO, 5)[-1].text, "有人在吗")

                plugin.config["external_plugins"] = {"universal_tts": {"proactive_voice": False}}
                await plugin._self_generate_and_send(profile, DecisionResult(action="proactive", scene="group_proactive"), None)
                self.assertEqual(self.sent[-1][0].text, "有人在吗")

                plugin.config["external_plugins"] = {"universal_tts": {"enabled": False}}
                await plugin._self_generate_and_send(profile, DecisionResult(action="proactive", scene="group_proactive"), None)
                self.assertFalse(is_record(self.sent[-1][0]))


class PassiveVoiceTests(unittest.IsolatedAsyncioTestCase):
    """官方流水线：本插件的装饰钩子先跑，随后通用 TTS 的钩子，再由 RespondStage 调 event.send。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.plugin, self.main = _make_plugin(self.tmp.name)
        self.engine = MimoEngine()
        self.universal = FakeUniversalTTS(self.engine)
        context = tts_context(self.universal, conversation_manager=self.plugin.context.conversation_manager,
                              register_web_api=lambda *a, **k: None)
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

    async def test_tone_from_decision_is_used_once(self):
        _, row = await self.round(DecisionResult(action="reply", voice_tone="开心"))
        self.assertEqual([style for style, _, _ in self.engine.spoken], ["温柔 开心"])
        self.assertEqual(self.engine.style, "温柔")
        self.assertTrue(row["reply_voice"])
        self.assertEqual(row["voice_tone"], "开心")

    async def test_tone_ignored_when_adapter_disabled(self):
        self.plugin.config["external_plugins"] = {"universal_tts": {"enabled": False}}
        await self.round(DecisionResult(action="reply", voice_tone="开心"))
        self.assertEqual([style for style, _, _ in self.engine.spoken], ["温柔"])


class VoiceToneQuestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_answer_sets_tone_and_tells_the_reply_model(self):
        speech = {"id": "talk", "type": "boolean", "instructions": "接话吗？",
                  "triggers": [{"event": "message_received"}], "actions": {"true": [{"tool": "request_reply"}]}}
        tone = optional_questions()[0].to_dict()
        tone["triggers"] = [{"question": "talk", "option": "true", "ask_with_parent": True}]
        graph = {"version": 6, "questions": [speech, tone]}
        loaded, error = load_questions(graph, strict=True)
        self.assertIsNone(error)
        settings = PluginSettings({"decision_questions": dict(graph, questions=[q.to_dict() for q in loaded])})
        for picked, expected in (("happy", "开心"), ("plain", "")):
            with self.subTest(picked):
                judge = AsyncMock(return_value={"talk": DecisionBooleanAnswer(probability=1),
                                                "voice_tone": choice(picked, **{picked: 0.9})})
                result = await ask_decision(SimpleNamespace(decision=SimpleNamespace(ask=judge)), settings=settings,
                                            scene="group_passive", is_private=False, messages=[message("m1")],
                                            memes=[], at_or_quoted=False)
                self.assertFalse(result.failed, result.error)
                self.assertEqual(result.voice_tone, expected)
                self.assertEqual("开心" in (session_hint_text(result) or ""), bool(expected))

    def test_tone_action_needs_text(self):
        tone = optional_questions()[0].to_dict()
        tone["actions"]["happy"] = [{"tool": "voice_tone"}]
        tone["triggers"] = [{"event": "message_received"}]
        _, error = load_questions({"version": 6, "questions": [tone]}, strict=True)
        self.assertIn("设定语音语气", error)

    def test_optional_question_is_offered_but_not_restored_by_default(self):
        self.assertNotIn("voice_tone", {q["id"] for q in web_api.v8_default_questions()})
        self.assertIn("voice_tone", web_api.plugin_question_pool())
        self.assertTrue(PluginSettings({}).tts_proactive_voice)
        off = PluginSettings({"external_plugins": {"universal_tts": {"proactive_voice": False}}})
        self.assertFalse(off.tts_proactive_voice)


if __name__ == "__main__":
    unittest.main()
