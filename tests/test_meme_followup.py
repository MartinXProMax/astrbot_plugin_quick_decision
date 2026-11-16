"""文字与表情拆成两条消息的隔离测试。不访问网络，不发送 QQ。"""

# ruff: noqa: E402, I001

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_v11_config import _install_astrbot_stubs

_install_astrbot_stubs()


class MessageChain:
    def __init__(self) -> None:
        self.chain: list[object] = []

    def message(self, message: str):
        self.chain.append(type("Plain", (), {"text": message})())
        return self


class Image:
    def __init__(self, path: str = "") -> None:
        self.file = path

    @classmethod
    def fromFileSystem(cls, path: str) -> Image:
        return cls(path)


class Reply:
    def __init__(self, id: str = "") -> None:
        self.id = id


sys.modules["astrbot.api.event"].MessageChain = MessageChain
sys.modules["astrbot.api.message_components"].Image = Image
sys.modules["astrbot.api.message_components"].Reply = Reply
sys.modules["astrbot.api.message_components"].Plain = type("Plain", (), {})

import importlib
import astrbot_plugin_quick_decision.reply_generator as reply_generator

importlib.reload(reply_generator)

from astrbot_plugin_quick_decision.models import DecisionResult, SessionProfile
from astrbot_plugin_quick_decision.reply_generator import (
    after_official_text_sent,
    build_meme_chain,
    build_send_chain,
    official_text_was_sent,
    prepare_official_result_chain,
    send_decision_message,
    take_event_decision,
)
from astrbot_plugin_quick_decision.session_store import SessionStore


PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


class _FileStore:
    def __init__(self, path: Path | None) -> None:
        self.path = path

    def resolve_image(self, meme_id):  # noqa: ANN001, ARG002
        return self.path


class _RecordingContext:
    def __init__(
        self,
        *,
        fail_on: int | set[int] | None = None,
        return_false_on: int | set[int] | None = None,
    ) -> None:
        self.calls: list[list[object]] = []
        self.umos: list[str] = []
        self.fail_on = set() if fail_on is None else {fail_on} if isinstance(fail_on, int) else set(fail_on)
        self.return_false_on = (
            set()
            if return_false_on is None
            else {return_false_on} if isinstance(return_false_on, int) else set(return_false_on)
        )

    async def send_message(self, umo: str, chain: MessageChain):
        index = len(self.calls)
        if index in self.fail_on:
            raise RuntimeError("send failed")
        self.umos.append(umo)
        self.calls.append(list(chain.chain))
        if index in self.return_false_on:
            return False
        return True


class _FakeEvent:
    def __init__(
        self,
        *,
        umo: str,
        chain: list | None = None,
        decision: DecisionResult | None = None,
        has_send_oper: bool | None = True,
    ) -> None:
        self.unified_msg_origin = umo
        self._result = type("Result", (), {"chain": list(chain or [])})()
        self._extra = {"quick_decision": decision} if decision is not None else {}
        if has_send_oper is not None:
            self._has_send_oper = has_send_oper

    def get_result(self):
        return self._result

    def get_extra(self, key, default=None):  # noqa: ANN001
        return self._extra.get(key, default)

    def set_extra(self, key, value):  # noqa: ANN001
        self._extra[key] = value


def _decision(**kwargs) -> DecisionResult:
    data = {
        "action": "reply",
        "use_meme": True,
        "meme_id": "meme-1",
        "target_message_id": "123",
        "scene": "group_passive",
    }
    data.update(kwargs)
    return DecisionResult(**data)


def _plain(text: str):
    return type("Plain", (), {"text": text})()


def _run(coro):
    return asyncio.run(coro)


class MemeFollowupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.image = Path(self.temp.name) / "smile.png"
        self.image.write_bytes(PNG)
        self.store = _FileStore(self.image)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _chain_kinds(self, items: list[object]) -> list[str]:
        kinds = []
        for item in items:
            if isinstance(item, Image) or hasattr(item, "file"):
                kinds.append("image")
            elif isinstance(item, Reply) or getattr(item, "id", None) is not None and getattr(item, "text", None) is None:
                kinds.append("reply")
            else:
                kinds.append("text")
        return kinds

    def test_text_and_meme_are_two_ordered_sends(self) -> None:
        context = _RecordingContext()
        sent = _run(
            send_decision_message(
                context,
                umo="group:1",
                text="hello",
                resolver=self.store,
                decision=_decision(),
                dry_run=False,
                quote_target=False,
            )
        )
        self.assertTrue(sent)
        self.assertEqual(len(context.calls), 2)

        self.assertEqual(self._chain_kinds(context.calls[0]), ["text"])
        self.assertEqual(self._chain_kinds(context.calls[1]), ["image"])
        self.assertTrue(all(getattr(item, "text", None) == "hello" for item in context.calls[0]))
        self.assertEqual(Path(context.calls[1][0].file).read_bytes(), PNG)
        self.assertEqual(context.umos, ["group:1", "group:1"])
        # 不是同一条消息链里图文混排
        self.assertFalse(
            any("text" in self._chain_kinds(call) and "image" in self._chain_kinds(call) for call in context.calls)
        )

    def test_partial_send_reports_only_successful_text(self) -> None:
        class Seg:
            def split_text(self, text, **_kwargs):
                return ["一段", "二段", "三段"]

            def should_delay(self, **_kwargs):
                return False

            async def interval_for_image(self):
                return 0

            async def sleep(self, _seconds):
                return None

        context = _RecordingContext(return_false_on={1})
        sent_texts: list[str] = []
        sent = _run(
            send_decision_message(
                context,
                umo="group:1",
                text="一段。二段。三段",
                resolver=self.store,
                decision=_decision(use_meme=False, meme_id=None),
                dry_run=False,
                quote_target=False,
                segmenter=Seg(),
                sent_texts=sent_texts,
            )
        )
        self.assertTrue(sent)
        self.assertEqual(sent_texts, ["一段", "三段"])

    def test_idle_sessions_and_locks_are_pruned(self) -> None:
        store = SessionStore(Path(self.temp.name))
        profile = SessionProfile(umo="old", is_private=False)
        runtime = store.ensure(profile)
        runtime.last_user_ts = time.time() - 90000
        store.session_lock("old")
        self.assertEqual(store.prune_idle(), 1)
        self.assertIsNone(store.get("old"))
        self.assertNotIn("old", store._session_locks)

    def test_no_image_sends_text_only(self) -> None:
        context = _RecordingContext()
        decision = _decision(use_meme=False, meme_id=None)
        sent = _run(
            send_decision_message(
                context,
                umo="group:1",
                text="hello",
                resolver=self.store,
                decision=decision,
                dry_run=False,
                quote_target=False,
            )
        )
        self.assertTrue(sent)
        self.assertEqual(len(context.calls), 1)
        self.assertEqual(self._chain_kinds(context.calls[0]), ["text"])

    def test_missing_file_sends_text_only(self) -> None:
        context = _RecordingContext()
        sent = _run(
            send_decision_message(
                context,
                umo="group:1",
                text="hello",
                resolver=_FileStore(None),
                decision=_decision(),
                dry_run=False,
                quote_target=False,
            )
        )
        self.assertTrue(sent)
        self.assertEqual(len(context.calls), 1)
        self.assertEqual(self._chain_kinds(context.calls[0]), ["text"])

    def test_none_action_is_not_sent_by_upper_layer_helpers(self) -> None:
        decision = _decision(action="none", use_meme=False, meme_id=None)
        self.assertFalse(decision.should_speak())
        self.assertFalse(build_meme_chain(self.store, decision).chain)
        context = _RecordingContext()
        # 上层 none 不会调用发送；若误调用且无文字，也不发。
        sent = _run(
            send_decision_message(
                context,
                umo="group:1",
                text="",
                resolver=self.store,
                decision=decision,
                dry_run=False,
                quote_target=False,
            )
        )
        self.assertFalse(sent)
        self.assertEqual(context.calls, [])

    def test_dry_run_sends_nothing(self) -> None:
        context = _RecordingContext()
        sent = _run(
            send_decision_message(
                context,
                umo="group:1",
                text="hello",
                resolver=self.store,
                decision=_decision(),
                dry_run=True,
                quote_target=False,
            )
        )
        self.assertFalse(sent)
        self.assertEqual(context.calls, [])

    def test_text_send_failure_does_not_send_meme(self) -> None:
        context = _RecordingContext(fail_on={0, 1})
        sent = _run(
            send_decision_message(
                context,
                umo="group:1",
                text="hello",
                resolver=self.store,
                decision=_decision(),
                dry_run=False,
                quote_target=False,
            )
        )
        self.assertFalse(sent)
        # 文字失败不再整轮重发；失败段不重试，也不发表情。
        self.assertTrue(all("image" not in self._chain_kinds(call) for call in context.calls))
        self.assertLessEqual(len(context.calls), 1)

        context2 = _RecordingContext(return_false_on={0, 1})
        sent2 = _run(
            send_decision_message(
                context2,
                umo="group:1",
                text="hello",
                resolver=self.store,
                decision=_decision(),
                dry_run=False,
                quote_target=False,
            )
        )
        self.assertFalse(sent2)
        self.assertTrue(all("image" not in self._chain_kinds(call) for call in context2.calls))

    def test_group_direct_send_quotes_text_only_not_meme(self) -> None:
        decision = _decision()
        text_chain = build_send_chain("hello", self.store, decision, quote_target=False)
        self.assertEqual(self._chain_kinds(text_chain.chain), ["text"])
        meme_chain = build_meme_chain(self.store, decision)
        self.assertEqual(self._chain_kinds(meme_chain.chain), ["image"])
        self.assertFalse(any(isinstance(item, Reply) for item in meme_chain.chain))

        context = _RecordingContext()
        _run(
            send_decision_message(
                context,
                umo="group:1",
                text="hello",
                resolver=self.store,
                decision=decision,
                dry_run=False,
                quote_target=False,
            )
        )
        self.assertEqual(self._chain_kinds(context.calls[0]), ["text"])
        self.assertEqual(self._chain_kinds(context.calls[1]), ["image"])

    def test_private_quote_stays_on_text_meme_has_no_quote(self) -> None:
        decision = _decision()
        text_chain = build_send_chain("hello", self.store, decision, quote_target=True)
        self.assertEqual(self._chain_kinds(text_chain.chain), ["reply", "text"])
        meme_chain = build_meme_chain(self.store, decision)
        self.assertEqual(self._chain_kinds(meme_chain.chain), ["image"])

        context = _RecordingContext()
        _run(
            send_decision_message(
                context,
                umo="friend:1",
                text="hello",
                resolver=self.store,
                decision=decision,
                dry_run=False,
                quote_target=True,
            )
        )
        self.assertEqual(self._chain_kinds(context.calls[0]), ["reply", "text"])
        self.assertEqual(self._chain_kinds(context.calls[1]), ["image"])
        self.assertEqual(context.umos, ["friend:1", "friend:1"])

    def test_official_decorate_does_not_attach_image(self) -> None:
        chain = [_plain("hello"), Reply(id="123"), Image.fromFileSystem(str(self.image))]
        # 装饰阶段即使链里已有图，本助手也不再往上加；这里验证官方整理函数不会为了配图保留或追加图，
        # 并按群聊 direct_send 去掉引用。已误放入的图不属于本函数职责，调用方不应传入。
        text_only = [_plain("hello"), Reply(id="123")]
        kept = prepare_official_result_chain(
            text_only,
            dry_run=False,
            should_speak=True,
            group_direct_send=True,
            is_private=False,
        )
        self.assertTrue(kept)
        self.assertEqual([getattr(item, "text", None) for item in text_only], ["hello"])
        self.assertFalse(any(isinstance(item, Reply) for item in text_only))
        self.assertFalse(any(isinstance(item, Image) for item in text_only))

        private = [_plain("hello"), Reply(id="123")]
        prepare_official_result_chain(
            private,
            dry_run=False,
            should_speak=True,
            group_direct_send=True,
            is_private=True,
        )
        self.assertTrue(any(isinstance(item, Reply) for item in private))

    def test_official_hook_sends_once_and_clears_state(self) -> None:
        decision = _decision()
        event = _FakeEvent(
            umo="group:1",
            chain=[_plain("hello"), _plain("world")],
            decision=decision,
            has_send_oper=True,
        )
        context = _RecordingContext()
        pending = {"pipeline": decision}

        def consume_pending() -> None:
            extra = event.get_extra("quick_decision")
            if extra is not None:
                pending["pipeline"] = None

        consume_pending()
        sent = _run(
            after_official_text_sent(
                event,
                context=context,
                resolver=self.store,
                dry_run=False,
            )
        )
        self.assertTrue(sent)
        self.assertEqual(len(context.calls), 1)
        self.assertEqual(self._chain_kinds(context.calls[0]), ["image"])
        self.assertIsNone(event.get_extra("quick_decision"))
        self.assertIsNone(pending["pipeline"])

        # 分段场景下若钩子被再次调用，不得再发图，也不得留下决策。
        sent_again = _run(
            after_official_text_sent(
                event,
                context=context,
                resolver=self.store,
                dry_run=False,
            )
        )
        self.assertFalse(sent_again)
        self.assertEqual(len(context.calls), 1)
        self.assertIsNone(event.get_extra("quick_decision"))

    def test_official_hook_skips_when_text_failed(self) -> None:
        decision = _decision()
        event = _FakeEvent(
            umo="group:1",
            chain=[_plain("hello")],
            decision=decision,
            has_send_oper=False,
        )
        context = _RecordingContext()
        sent = _run(
            after_official_text_sent(
                event,
                context=context,
                resolver=self.store,
                dry_run=False,
            )
        )
        self.assertFalse(sent)
        self.assertEqual(context.calls, [])
        self.assertIsNone(event.get_extra("quick_decision"))

    def test_official_text_was_sent_requires_text(self) -> None:
        event = _FakeEvent(umo="group:1", chain=[], decision=_decision(), has_send_oper=True)
        self.assertFalse(official_text_was_sent(event))
        event._result.chain = [_plain("hi")]
        self.assertTrue(official_text_was_sent(event))

    def test_take_event_decision_is_single_shot(self) -> None:
        event = _FakeEvent(umo="group:1", decision=_decision())
        first = take_event_decision(event)
        self.assertIsInstance(first, DecisionResult)
        self.assertIsNone(take_event_decision(event))

    def test_session_pending_cleanup_does_not_cross_umo(self) -> None:
        store = SessionStore(Path(self.temp.name), log_limit=10)
        group = SessionProfile(umo="group:1", platform_id="qq", is_private=False)
        private = SessionProfile(umo="friend:1", platform_id="qq", is_private=True)
        g_runtime = store.ensure(group)
        p_runtime = store.ensure(private)
        g_runtime.pending_pipeline = _decision(meme_id="g")
        p_runtime.pending_pipeline = _decision(meme_id="p")
        event = _FakeEvent(
            umo="group:1",
            chain=[_plain("hello")],
            decision=g_runtime.pending_pipeline,
            has_send_oper=True,
        )
        context = _RecordingContext()
        if isinstance(event.get_extra("quick_decision"), DecisionResult):
            g_runtime.pending_pipeline = None
        _run(
            after_official_text_sent(
                event,
                context=context,
                resolver=self.store,
                dry_run=False,
            )
        )
        self.assertIsNone(g_runtime.pending_pipeline)
        self.assertIsNotNone(p_runtime.pending_pipeline)
        self.assertEqual(p_runtime.pending_pipeline.meme_id, "p")
        self.assertEqual(context.umos, ["group:1"])


if __name__ == "__main__":
    unittest.main()
