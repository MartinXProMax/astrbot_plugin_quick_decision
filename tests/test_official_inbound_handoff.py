"""入站官方 request_llm 交接的隔离回归。不发 QQ，不调生产模型，不访问网络。"""

# ruff: noqa: E402, I001

from __future__ import annotations

import asyncio
import sys
import tempfile
import types
import unittest
from contextvars import ContextVar
from itertools import product
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_v11_config import _install_astrbot_stubs

_install_astrbot_stubs()


def _ensure(name: str) -> types.ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    module = types.ModuleType(name)
    module.__path__ = []  # type: ignore[attr-defined]
    sys.modules[name] = module
    if "." in name:
        parent, child = name.rsplit(".", 1)
        setattr(_ensure(parent), child, module)
    return module


def _install_main_stubs() -> None:
    for name in (
        "astrbot.api.platform",
        "astrbot.api.star",
        "astrbot.api.web",
        "astrbot.core.star",
        "astrbot.core.star.filter",
        "astrbot.core.star.filter.command",
        "astrbot.core.star.filter.command_group",
    ):
        _ensure(name)

    class MessageType:
        OTHER_MESSAGE = "OTHER_MESSAGE"
        GROUP_MESSAGE = "GROUP_MESSAGE"
        FRIEND_MESSAGE = "FRIEND_MESSAGE"

    _ensure("astrbot.api.platform").MessageType = MessageType

    class Star:
        def __init__(self, context=None) -> None:
            self.context = context

    _ensure("astrbot.api.star").Star = Star
    _ensure("astrbot.api.star").Context = type("Context", (), {})
    _ensure("astrbot.api").AstrBotConfig = dict

    class _Filter:
        class EventMessageType:
            ALL = "ALL"

        class PermissionType:
            ADMIN = "ADMIN"

        @staticmethod
        def _deco(*_a, **_k):
            def wrap(fn):
                return fn

            return wrap

        event_message_type = _deco
        on_llm_request = _deco
        on_llm_response = _deco
        on_using_llm_tool = _deco
        on_decorating_result = _deco
        after_message_sent = _deco
        on_astrbot_loaded = _deco
        command = _deco
        permission_type = _deco

        @staticmethod
        def command_group(*_a, **_k):
            def wrap(fn):
                def command(*_a2, **_k2):
                    def inner(method):
                        return method

                    return inner

                fn.command = command
                return fn

            return wrap

    event_mod = _ensure("astrbot.api.event")
    event_mod.filter = _Filter
    event_mod.AstrMessageEvent = type("AstrMessageEvent", (), {})

    components = _ensure("astrbot.api.message_components")
    if not hasattr(components, "Plain"):
        components.Plain = type("Plain", (), {})
    if not hasattr(components, "At"):
        components.At = type("At", (), {})
    if not hasattr(components, "AtAll"):
        components.AtAll = type("AtAll", (components.At,), {})
    if not hasattr(components, "Image"):
        components.Image = type("Image", (), {})
    if not hasattr(components, "Reply"):
        components.Reply = type("Reply", (), {})

    cmd = _ensure("astrbot.core.star.filter.command")
    cmd.CommandFilter = type("CommandFilter", (), {})
    grp = _ensure("astrbot.core.star.filter.command_group")
    grp.CommandGroupFilter = type("CommandGroupFilter", (), {})

    web = _ensure("astrbot.api.web")
    web.error_response = lambda *a, **k: None
    web.json_response = lambda *a, **k: None
    web.request = SimpleNamespace(query=SimpleNamespace(get=lambda *a, **k: None))


_install_main_stubs()

class Reply:
    def __init__(self, id: str = "", sender_id: str = "") -> None:
        self.id = id
        self.sender_id = sender_id

from astrbot.core.agent.message import TextPart
from astrbot_plugin_quick_decision.models import (
    BufferedMessage,
    DecisionResult,
    SessionProfile,
)
from astrbot_plugin_quick_decision.official_handoff import (
    begin_official_pipeline,
    build_inbound_user_prompt,
    default_official_agent_would_run,
    is_official_pipeline_busy,
    make_official_llm_request,
    merged_user_messages,
    release_official_pipeline,
    resolve_session_conversation,
    should_use_default_official_agent,
    suppress_default_official_agent,
)
from astrbot_plugin_quick_decision.reply_generator import (
    apply_session_hints,
    install_group_quote_guard,
    prepare_official_result_chain,
)
from astrbot_plugin_quick_decision.session_store import SessionRuntime, SessionStore


class ProviderRequest:
    def __init__(
        self,
        prompt="",
        conversation=None,
        contexts=None,
        image_urls=None,
        audio_urls=None,
        system_prompt="",
        extra_user_content_parts=None,
        **kwargs,
    ) -> None:
        if contexts and conversation:
            conversation = None
        self.prompt = prompt
        self.conversation = conversation
        self.contexts = list(contexts or [])
        self.image_urls = list(image_urls or [])
        self.audio_urls = list(audio_urls or [])
        self.system_prompt = system_prompt
        self.extra_user_content_parts = list(extra_user_content_parts or [])
        self.kwargs = kwargs


class FakeEvent:
    def __init__(
        self,
        *,
        umo="aiocqhttp:GroupMessage:100",
        text="",
        message_id="m1",
        timestamp=1000.0,
        is_private=False,
        at_or_wake=False,
        sender="user1",
        sender_name="Alice",
        self_id="bot",
        components=None,
    ) -> None:
        self.unified_msg_origin = umo
        self.message_str = text
        self.is_at_or_wake_command = at_or_wake
        self.call_llm = False
        self._stopped = False
        self._extra: dict = {}
        self._has_send_oper = False
        self._sends: list = []
        self.message_obj = SimpleNamespace(
            message_id=message_id,
            timestamp=timestamp,
            message=list(components or []),
            sender=SimpleNamespace(user_id=sender, nickname=sender_name),
        )
        self._is_private = is_private
        self._sender = sender
        self._sender_name = sender_name
        self._self_id = self_id
        self._result = None

    def get_message_type(self):
        from astrbot.api.platform import MessageType

        return (
            MessageType.FRIEND_MESSAGE
            if self._is_private
            else MessageType.GROUP_MESSAGE
        )

    def get_sender_id(self):
        return self._sender

    def get_sender_name(self):
        return self._sender_name

    def get_self_id(self):
        return self._self_id

    def get_session_id(self):
        return self.unified_msg_origin.split(":")[-1]

    def get_platform_id(self):
        return self.unified_msg_origin.split(":")[0]

    def is_private_chat(self):
        return self._is_private

    def get_messages(self):
        return self.message_obj.message

    def should_call_llm(self, call_llm: bool) -> None:
        self.call_llm = call_llm

    def stop_event(self) -> None:
        self._stopped = True

    def is_stopped(self) -> bool:
        return self._stopped

    def set_extra(self, key, value) -> None:
        self._extra[key] = value

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def get_result(self):
        return self._result

    def set_result(self, result) -> None:
        self._result = result

    def request_llm(self, prompt="", conversation=None, contexts=None, **kwargs):
        return ProviderRequest(
            prompt=prompt, conversation=conversation, contexts=contexts, **kwargs
        )

    async def send(self, message):
        self._sends.append(message)
        self._has_send_oper = True
        return True


class FakeConversation:
    def __init__(self, cid="cid-1", history="[]", token_usage=0) -> None:
        self.cid = cid
        self.history = history
        self.token_usage = token_usage
        self.persona_id = "default"


class FakeConvMgr:
    def __init__(self, conversation=None) -> None:
        self.conversation = conversation or FakeConversation()
        self.updated: list[dict] = []
        self.created = 0

    async def get_curr_conversation_id(self, umo):  # noqa: ARG002
        return self.conversation.cid

    async def new_conversation(self, umo, platform_id):  # noqa: ARG002
        self.created += 1
        return self.conversation.cid

    async def get_conversation(self, umo, cid):  # noqa: ARG002
        return self.conversation

    async def update_conversation(self, umo, cid, history=None, token_usage=None):
        self.updated.append(
            {
                "umo": umo,
                "cid": cid,
                "history": history,
                "token_usage": token_usage,
            }
        )


def _user(mid, text, ts, sender="u1", name="U1") -> BufferedMessage:
    return BufferedMessage(
        message_id=mid,
        sender_id=sender,
        sender_name=name,
        role="user",
        text=text,
        timestamp=ts,
    )


class OfficialContractTests(unittest.TestCase):
    def test_should_call_llm_true_blocks_default_second_agent(self) -> None:
        event = FakeEvent(at_or_wake=True)
        # 未抑制时，插件已 yield 并 set_result 后，默认 Agent 仍会再跑。
        self.assertTrue(
            default_official_agent_would_run(
                has_send_oper=False,
                is_at_or_wake_command=True,
                call_llm=event.call_llm,
                has_result=True,
                is_stopped=False,
            )
        )
        suppress_default_official_agent(event)
        self.assertTrue(event.call_llm)
        self.assertFalse(
            default_official_agent_would_run(
                has_send_oper=False,
                is_at_or_wake_command=True,
                call_llm=event.call_llm,
                has_result=True,
                is_stopped=False,
            )
        )

    def test_interrupt_does_not_trigger_default_agent(self) -> None:
        self.assertFalse(
            default_official_agent_would_run(
                has_send_oper=False,
                is_at_or_wake_command=False,
                call_llm=False,
                has_result=True,
                is_stopped=False,
            )
        )

    def test_use_default_official_only_without_merge(self) -> None:
        self.assertTrue(
            should_use_default_official_agent(
                official_would_run=True, has_merged_messages=False
            )
        )
        self.assertFalse(
            should_use_default_official_agent(
                official_would_run=True, has_merged_messages=True
            )
        )
        self.assertFalse(
            should_use_default_official_agent(
                official_would_run=False, has_merged_messages=False
            )
        )

    def test_request_llm_keeps_conversation_without_custom_contexts(self) -> None:
        event = FakeEvent()
        conv = FakeConversation()
        req = make_official_llm_request(event, conversation=conv, prompt="hello")
        self.assertIs(req.conversation, conv)
        self.assertEqual(req.contexts, [])
        self.assertEqual(req.prompt, "hello")

    def test_request_llm_drops_conversation_if_contexts_passed(self) -> None:
        # 官方实现：contexts 非空会丢掉 conversation。本交接不得走这条路。
        event = FakeEvent()
        req = event.request_llm(
            prompt="x", conversation=FakeConversation(), contexts=[{"role": "user"}]
        )
        self.assertIsNone(req.conversation)


class MergePromptTests(unittest.TestCase):
    def test_merge_prompt_keeps_all_window_user_messages(self) -> None:
        messages = [
            _user("old", "更早的话", 10),
            _user("m1", "第一句", 1000, sender="a", name="甲"),
            _user("m2", "第二句", 1000.2, sender="b", name="乙"),
            _user("m3", "第三句", 1000.4, sender="a", name="甲"),
        ]
        prompt = build_inbound_user_prompt(
            event_text="第一句",
            messages=messages,
            first_message_id="m1",
            first_timestamp=1000.0,
            merged=True,
        )
        self.assertIn("第一句", prompt)
        self.assertIn("第二句", prompt)
        self.assertIn("第三句", prompt)
        self.assertNotIn("更早的话", prompt)
        self.assertEqual(prompt, "甲: 第一句\n乙: 第二句\n甲: 第三句")

    def test_unmerged_prompt_is_current_event_only(self) -> None:
        prompt = build_inbound_user_prompt(
            event_text="  当前  ",
            messages=[_user("m1", "当前", 1), _user("m2", "其它", 2)],
            first_message_id="m1",
            first_timestamp=1,
            merged=False,
        )
        self.assertEqual(prompt, "当前")

    def test_merged_window_helper(self) -> None:
        items = merged_user_messages(
            [_user("m1", "a", 5), _user("m2", "b", 5.2), _user("old", "z", 1)],
            first_message_id="m1",
            first_timestamp=5,
        )
        self.assertEqual([i.text for i in items], ["a", "b"])


class HintsAndHistoryTests(unittest.TestCase):
    def test_hints_are_temp_and_not_saved_to_history(self) -> None:
        req = ProviderRequest(prompt="用户原文")
        apply_session_hints(
            req,
            DecisionResult(action="reply", session_hints=["请简短回复", "  "],
                           use_meme=True, meme_id="internal-meme-42", meme_description="企鹅点头"),
        )
        self.assertEqual(len(req.extra_user_content_parts), 1)
        part = req.extra_user_content_parts[0]
        self.assertTrue(part._no_save)
        self.assertIn('"动作":"回复"', part.text)
        self.assertIn('"表情描述":"企鹅点头"', part.text)
        self.assertNotIn("internal-meme-42", part.text)
        self.assertIn("请简短回复", part.text)

        # 模拟官方 _save_to_history：跳过 _no_save 的临时块，只保留用户原文。
        user_parts = [{"type": "text", "text": req.prompt, "_no_save": False}]
        for item in req.extra_user_content_parts:
            user_parts.append(
                {
                    "type": "text",
                    "text": item.text,
                    "_no_save": bool(getattr(item, "_no_save", False)),
                }
            )
        saved = [p["text"] for p in user_parts if not p["_no_save"]]
        self.assertEqual(saved, ["用户原文"])
        self.assertTrue(any(p["_no_save"] for p in user_parts))

    def test_empty_hints_do_not_inject(self) -> None:
        req = ProviderRequest()
        apply_session_hints(req, DecisionResult(session_hints=[]))
        self.assertEqual(req.extra_user_content_parts, [])

    def test_missing_meme_description_does_not_expose_its_id(self) -> None:
        req = ProviderRequest()
        decision = DecisionResult(action="reply", use_meme=True, meme_id="internal-meme-42")
        apply_session_hints(req, decision)
        text = req.extra_user_content_parts[0].text
        self.assertIn("尝试另发一张表情", text)
        self.assertNotIn("表情描述", text)
        self.assertNotIn("internal-meme-42", text)
        self.assertEqual(decision.tool_calls["send_meme"].args["meme_id"], "internal-meme-42")


class DirectSendGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_proactive_generation_gets_one_temporary_summary(self) -> None:
        from astrbot_plugin_quick_decision.reply_generator import generate_text

        context = SimpleNamespace(
            conversation_manager=FakeConvMgr(),
            persona_manager=SimpleNamespace(get_persona=AsyncMock(return_value=None)),
            get_current_chat_provider_id=AsyncMock(return_value="chat"),
            llm_generate=AsyncMock(return_value=SimpleNamespace(completion_text="周末过得怎么样？")),
        )
        decision = DecisionResult(action="proactive", scene="group_proactive", length="short",
                                  use_meme=True, meme_id="internal-meme-42", meme_description="企鹅点头")
        await generate_text(context, umo="room", decision=decision, user_prompt="")
        context.llm_generate.assert_awaited_once()
        parts = context.llm_generate.await_args.kwargs["extra_user_content_parts"]
        self.assertEqual(len(parts), 1)
        self.assertTrue(parts[0]._no_save)
        self.assertIn("冷场主动发言", parts[0].text)
        self.assertIn('"篇幅":"简短"', parts[0].text)
        self.assertIn('"表情描述":"企鹅点头"', parts[0].text)
        self.assertNotIn("internal-meme-42", parts[0].text)
        self.assertEqual(context.conversation_manager.updated[0]["history"],
                         [{"role": "assistant", "content": "周末过得怎么样？"}])

    async def test_proactive_generation_drops_tool_rounds_but_keeps_them_in_history(self) -> None:
        import json

        from astrbot_plugin_quick_decision.reply_generator import generate_text

        call = lambda cid: {"id": cid, "type": "function",  # noqa: E731
                            "function": {"name": "web_search", "arguments": "{}"}}
        history = [
            {"role": "user", "content": "查下天气"},
            {"role": "assistant", "content": None, "tool_calls": [call("c1")]},
            {"role": "tool", "tool_call_id": "c1", "content": "晴"},
            {"role": "assistant", "content": "我再确认一下", "tool_calls": [call("c2")]},
            {"role": "tool", "tool_call_id": "c2", "content": "晴，25 度"},
            {"role": "assistant", "content": "今天晴，25 度"},
        ]
        context = SimpleNamespace(
            conversation_manager=FakeConvMgr(FakeConversation(history=json.dumps(history))),
            persona_manager=SimpleNamespace(get_persona=AsyncMock(return_value=None)),
            get_current_chat_provider_id=AsyncMock(return_value="chat"),
            llm_generate=AsyncMock(return_value=SimpleNamespace(completion_text="出门记得防晒")),
        )
        decision = DecisionResult(action="proactive", scene="group_proactive")
        await generate_text(context, umo="room", decision=decision, user_prompt="")
        # 请求里不能出现没有 tool 结果的 tool_calls，否则 Anthropic 等接口返回 400。
        self.assertEqual(context.llm_generate.await_args.kwargs["contexts"], [
            {"role": "user", "content": "查下天气"},
            {"role": "assistant", "content": "我再确认一下"},
            {"role": "assistant", "content": "今天晴，25 度"},
        ])
        # 写回官方历史时原样保留工具调用轮次，只追加本轮发言。
        self.assertEqual(context.conversation_manager.updated[0]["history"],
                         [*history, {"role": "assistant", "content": "出门记得防晒"}])

    async def test_send_guard_strips_reply_inserted_after_decorate_hook(self) -> None:
        event = FakeEvent(is_private=False)
        chain = [SimpleNamespace(text="你好")]
        # 钩子阶段：链上还没有官方引用。
        self.assertTrue(
            prepare_official_result_chain(
                chain,
                dry_run=False,
                should_speak=True,
                clear_group_reply=True,
                is_private=False,
            )
        )
        decision = DecisionResult(action="reply")
        event.set_extra("quick_decision", decision)
        event.set_result(SimpleNamespace(chain=chain, is_llm_result=lambda: True))
        install_group_quote_guard(event, decision)
        # 官方 ResultDecorateStage 在钩子之后插入引用。
        chain.insert(0, Reply(id="inbound-1"))
        payload = SimpleNamespace(chain=chain)
        await event.send(payload)
        delivered = event._sends[0].chain
        self.assertFalse(any(type(item).__name__ == "Reply" for item in delivered))
        self.assertEqual(delivered[0].text, "你好")

    async def test_guard_not_used_when_direct_send_off(self) -> None:
        chain = [Reply(id="keep"), SimpleNamespace(text="hi")]
        self.assertTrue(
            prepare_official_result_chain(
                chain,
                dry_run=False,
                should_speak=True,
                clear_group_reply=False,
                is_private=False,
            )
        )
        self.assertTrue(any(type(item).__name__ == "Reply" for item in chain))

    def test_onebot_send_time_is_independent_of_arrival(self):
        from astrbot_plugin_quick_decision.event_utils import to_buffered_message
        event = FakeEvent(timestamp=2000)
        with patch("time.time", return_value=2000):
            for raw, expected in (({"time": 1000}, 1000), ({}, None), (None, None),
                                  ({"time": True}, None), ({"time": 2000000}, None),
                                  ({"time": float("nan")}, None), ({"time": -1}, None)):
                event.message_obj.raw_message = raw
                buffered = to_buffered_message(event)
                self.assertEqual((buffered.timestamp, buffered.sent_at), (2000, expected))

    async def test_quote_modes_apply_only_to_segments_of_one_result(self):
        from astrbot_plugin_quick_decision.reply_generator import Plain
        for mode, expected in (("first", ["old", None, None]), ("all", ["old"] * 3),
                               ("none", [None] * 3)):
            with self.subTest(mode=mode):
                event = FakeEvent()
                decision = DecisionResult(action="reply", target_message_id="old", target_quote_mode=mode)
                event.set_extra("quick_decision", decision)
                install_group_quote_guard(event, decision)
                pieces = [Plain("相同的正文") for _ in range(3)]
                image = object()
                event.set_result(SimpleNamespace(chain=[image, *pieces], is_llm_result=lambda: True))
                await event.send(SimpleNamespace(chain=[Reply("incoming"), image]))
                await event.send(SimpleNamespace(chain=[Reply("tool"), Plain("相同的正文")]))
                for piece in pieces:
                    await event.send(SimpleNamespace(chain=[Reply("incoming"), piece]))
                # 模型独立生成的另一份结果重新计算首段。
                independent = Plain("新的回复")
                event.set_result(SimpleNamespace(chain=[independent], is_llm_result=lambda: True))
                await event.send(SimpleNamespace(chain=[independent]))
                self.assertEqual([getattr(m.chain[0], "id", None) for m in event._sends],
                                 [None, "tool", *expected, expected[0]])

    async def test_stream_delivery_skips_tool_sends_and_preserves_first_after_failure(self):
        from astrbot_plugin_quick_decision import reply_generator as replies
        event = FakeEvent()
        decision = DecisionResult(action="reply", target_message_id="old")
        event.set_extra("quick_decision", decision)
        event.set_result(SimpleNamespace(chain=[], is_llm_result=lambda: False))
        marker = ContextVar("stream", default=None)
        send = AsyncMock(side_effect=[True, OSError("failed"), True, True])
        event.send = send
        install_group_quote_guard(event, decision)
        with patch.object(replies, "streaming_response_event", marker):
            await event.send(SimpleNamespace(chain=[Reply("tool"), replies.Plain("工具")]))
            marker.set(event)
            with self.assertRaises(OSError):
                await event.send(SimpleNamespace(chain=[replies.Plain("失败")]))
            for word in ("首段", "后段"):
                await event.send(SimpleNamespace(chain=[replies.Plain(word)]))
        self.assertEqual([getattr(c.args[0].chain[0], "id", None) for c in send.await_args_list],
                         ["tool", "old", "old", None])

    async def test_mixed_file_payload_keeps_quote_and_text_together(self):
        from astrbot_plugin_quick_decision.reply_generator import Plain
        class File:
            pass
        event = FakeEvent()
        decision = DecisionResult(action="reply", target_message_id="old", target_quote_mode="all")
        event.set_extra("quick_decision", decision)
        install_group_quote_guard(event, decision)
        parts = [Plain("甲"), File(), Plain("乙")]
        event.set_result(SimpleNamespace(chain=parts, is_llm_result=lambda: True))
        await event.send(SimpleNamespace(chain=[Reply("incoming"), *parts]))
        self.assertEqual([m.chain[-1] for m in event._sends], parts)
        self.assertEqual([len(m.chain) for m in event._sends], [2, 1, 2])
        self.assertEqual([event._sends[i].chain[0].id for i in (0, 2)], ["old", "old"])


class PipelineBusyTests(unittest.TestCase):
    def test_busy_flag_covers_pending_pipeline(self) -> None:
        runtime = SessionRuntime(profile=SessionProfile(umo="s", is_private=True))
        self.assertFalse(is_official_pipeline_busy(runtime))
        begin_official_pipeline(runtime, DecisionResult(action="reply"))
        self.assertTrue(is_official_pipeline_busy(runtime))
        release_official_pipeline(runtime)
        self.assertFalse(is_official_pipeline_busy(runtime))
        self.assertIsNone(runtime.pending_pipeline)
        self.assertFalse(runtime.deciding)


class ConversationResolveTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_session_conversation(self) -> None:
        event = FakeEvent()
        mgr = FakeConvMgr()
        conv = await resolve_session_conversation(
            SimpleNamespace(conversation_manager=mgr), event
        )
        self.assertIs(conv, mgr.conversation)

    async def test_resolve_missing_manager(self) -> None:
        conv = await resolve_session_conversation(SimpleNamespace(), FakeEvent())
        self.assertIsNone(conv)


def _profile(event: FakeEvent) -> SessionProfile:
    from astrbot_plugin_quick_decision.event_utils import event_profile

    return event_profile(event)


def _make_plugin(tmp: str, *, config=None, conv_mgr=None):
    import importlib
    import astrbot_plugin_quick_decision
    if "astrbot_plugin_quick_decision.main" in sys.modules:
        try:
            getattr(sys.modules["astrbot_plugin_quick_decision.main"], "QuickDecisionPlugin")
        except AttributeError:
            sys.modules.pop("astrbot_plugin_quick_decision.main")
    import astrbot_plugin_quick_decision.main as main_mod
    if not hasattr(main_mod, "QuickDecisionPlugin"):
        main_mod = importlib.reload(main_mod)

    plugin = object.__new__(main_mod.QuickDecisionPlugin)
    plugin.context = SimpleNamespace(
        conversation_manager=conv_mgr or FakeConvMgr(),
        register_web_api=lambda *a, **k: None,
    )
    plugin.config = {
        "enabled": True,
        "dry_run": False,
        "debounce_seconds": 0,
        "group": {
            "enable_interrupt": True,
            "direct_send": True,
            "cooldown_seconds": 0,
        },
        **(config or {}),
    }
    plugin.sessions = SessionStore(Path(tmp), log_limit=20)
    plugin.meme_adapter = SimpleNamespace(
        described_catalog=lambda: [],
        resolve_image=lambda meme_id: None,
        status=lambda: SimpleNamespace(
            available=False, message="stub", plugin_version="", registered_apis=[], pack_count=0, candidate_count=0
        ),
    )
    # 没有 get_registered_star：视同未安装通用 TTS。
    plugin.tts_adapter = main_mod.UniversalTTSAdapter(plugin.context)
    plugin._silence_task = None
    plugin._post_tasks = set()
    plugin._proactive_tasks = set()
    plugin._generate_calls: list = []
    plugin._send_calls: list = []
    return plugin, main_mod


async def _drain_proactive(plugin) -> None:
    """等待已启动的定时与冷场回合全部结束。"""
    await asyncio.gather(*list(getattr(plugin, "_proactive_tasks", ())))


class PluginHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.plugin, self.main_mod = _make_plugin(self.tmp.name)
        self.decision_calls = 0

        async def fake_ask(*_a, **k):
            self.decision_calls += 1
            return DecisionResult(
                action="reply",
                scene=k.get("scene") or "group_passive",
                session_hints=["请简短回复"],
            )

        self._orig_ask = self.main_mod.ask_decision
        self.main_mod.ask_decision = fake_ask

        async def boom_generate(*_a, **k):
            raise AssertionError("入站路径不应再调用 generate_text / llm_generate")

        self._orig_gen = self.main_mod.generate_text
        self.main_mod.generate_text = boom_generate

    async def asyncTearDown(self) -> None:
        self.main_mod.ask_decision = self._orig_ask
        self.main_mod.generate_text = self._orig_gen
        self.tmp.cleanup()

    def test_web_catalog_covers_questions_used_in_all_scenes(self) -> None:
        from astrbot_plugin_quick_decision.decision_engine import _build_questions
        from astrbot_plugin_quick_decision.models import BufferedMessage

        payload = self.plugin._decision_config_payload()
        self.assertNotIn("builtin_questions", payload)
        self.assertEqual(payload["version"], 8)
        self.assertIn("send_meme", payload["actions"])
        ids = {q["id"] for q in payload["decision_questions"]}
        messages = [BufferedMessage("msg1", "u1", "User", "user", "Hello")]
        asked = set()
        for scene in ("group_passive", "private_passive", "group_proactive", "private_proactive"):
            asked.update(_build_questions(scene, messages, [{"id": "cat", "description": "猫"}]))
        self.assertEqual(asked, ids)

    async def test_web_saves_random_mode_and_preserves_it_for_old_clients(self) -> None:
        from astrbot_plugin_quick_decision import web_api

        class Config(dict):
            save_config_async = AsyncMock()
            save_config = save_config_async

        self.plugin.config = Config(self.plugin.config)
        payload = self.plugin._decision_config_payload()
        self.assertEqual(payload["decision_mode"], "model")
        payload["decision_mode"] = "random"
        request = SimpleNamespace(method="POST", json=AsyncMock(return_value=payload))

        with (
            patch.object(web_api, "request", request),
            patch.object(web_api, "json_response", side_effect=lambda value: value),
        ):
            saved = await self.plugin.web_decision_config()
            self.assertEqual(saved["decision_mode"], "random")
            self.assertEqual(self.plugin.config["decision_mode"], "random")
            request.method = "GET"
            self.assertEqual((await self.plugin.web_decision_config())["decision_mode"], "random")
            request.method = "POST"
            payload.pop("decision_mode")
            self.assertEqual((await self.plugin.web_decision_config())["decision_mode"], "random")
            payload["decision_mode"] = []
            with patch.object(web_api, "error_response") as error:
                await self.plugin.web_decision_config()
                error.assert_called_once_with("请选择模型判断或纯随机", status_code=400)
        self.assertEqual(self.plugin.config.save_config_async.await_count, 2)

    async def test_failed_web_save_restores_previous_settings(self):
        from astrbot_plugin_quick_decision import web_api

        class Config(dict):
            save_config = AsyncMock(side_effect=OSError("disk full"))
            save_config_async = save_config

        self.plugin.config = Config(self.plugin.config)
        original = dict(self.plugin.config)
        payload = dict(self.plugin._decision_config_payload(), decision_mode="random")
        with (
            patch.object(web_api, "request", SimpleNamespace(method="POST", json=AsyncMock(return_value=payload))),
            patch.object(web_api, "error_response", side_effect=lambda message, status_code: status_code),
        ):
            self.assertEqual(await self.plugin.web_decision_config(), 500)
        self.assertEqual(dict(self.plugin.config), original)

    async def test_old_editor_cannot_erase_saved_branches(self) -> None:
        from astrbot_plugin_quick_decision import web_api

        class Config(dict):
            save_config = AsyncMock()
            save_config_async = save_config

        self.plugin.config = Config(self.plugin.config)
        payload = self.plugin._decision_config_payload()
        request = SimpleNamespace(method="POST", json=AsyncMock(return_value=payload))
        with (
            patch.object(web_api, "request", request),
            patch.object(web_api, "json_response", side_effect=lambda value: value),
            patch.object(web_api, "error_response", side_effect=lambda msg, status_code: status_code),
        ):
            await self.plugin.web_decision_config()
            saved = self.plugin.config["decision_questions"]
            for q in payload["decision_questions"]:
                q.pop("triggers", None)
            payload["version"] = 2
            self.assertEqual(await self.plugin.web_decision_config(), 409)
            self.assertEqual(self.plugin.config["decision_questions"], saved)
            self.plugin.config.save_config_async.assert_awaited_once()
            payload["version"] = 3
            self.assertEqual(await self.plugin.web_decision_config(), 409)
            payload["version"] = 2
            self.assertEqual(await self.plugin.web_decision_config(), 409)

    async def test_private_unmerged_keeps_default_official_path(self) -> None:
        event = FakeEvent(
            umo="aiocqhttp:FriendMessage:1",
            text="你好",
            is_private=True,
            at_or_wake=True,
            message_id="p1",
            timestamp=10,
        )
        returned = await self.plugin.on_chat_message(event)
        self.assertIsNone(returned)
        self.assertFalse(event.call_llm)
        self.assertFalse(event.is_stopped())
        self.assertTrue(event.get_extra("quick_decision").official_llm)
        runtime = self.plugin.sessions.get(event.unified_msg_origin)
        self.assertIsNotNone(runtime.pending_pipeline)
        self.assertTrue(runtime.deciding)
        self.assertEqual(self.decision_calls, 1)

    async def test_old_editor_cannot_erase_post_reply_stage(self):
        from astrbot_plugin_quick_decision import web_api
        from test_decision_engine import flat_gallery

        class Config(dict):
            save_config = AsyncMock()
            save_config_async = save_config

        self.plugin.config = Config(self.plugin.config)
        legacy = flat_gallery(self.plugin.settings)
        self.plugin.config["decision_questions"] = {"version": 4, "questions": [q.to_dict() for q in legacy.decision_questions]}
        payload = self.plugin._decision_config_payload()
        payload["version"] = 6
        length = next(q for q in payload["decision_questions"] if q["id"] == "length")
        length["stage"] = "post_reply"
        for t in length["triggers"]:
            t.pop("ask_with_parent", None)
        request = SimpleNamespace(method="POST", json=AsyncMock(return_value=payload))
        with (
            patch.object(web_api, "request", request),
            patch.object(web_api, "json_response", side_effect=lambda value: value),
            patch.object(web_api, "error_response", side_effect=lambda msg, status_code: status_code),
        ):
            self.assertEqual((await self.plugin.web_decision_config())["version"], 6)
            del length["stage"]
            for version in (2, 3):
                payload["version"] = version
                self.assertEqual(await self.plugin.web_decision_config(), 409)
            self.plugin.config.save_config_async.assert_awaited_once()
            reloaded = self.plugin.settings.decision_questions
            self.assertEqual(next(q.stage for q in reloaded if q.id == "length"), "post_reply")

    async def test_question_mode_override_requires_new_editor(self):
        from astrbot_plugin_quick_decision import web_api
        from test_decision_engine import flat_gallery

        class Config(dict):
            save_config = AsyncMock()
            save_config_async = save_config

        self.plugin.config = Config(self.plugin.config)
        legacy = flat_gallery(self.plugin.settings)
        self.plugin.config["decision_questions"] = {"version": 4, "questions": [q.to_dict() for q in legacy.decision_questions]}
        payload = self.plugin._decision_config_payload()
        payload["version"] = 6
        question = next(q for q in payload["decision_questions"] if q["id"] == "length")
        question["decision_mode"] = "random"
        request = SimpleNamespace(method="POST", json=AsyncMock(return_value=payload))
        with (
            patch.object(web_api, "request", request),
            patch.object(web_api, "json_response", side_effect=lambda value: value),
            patch.object(web_api, "error_response", side_effect=lambda msg, status_code: status_code),
        ):
            saved = await self.plugin.web_decision_config()
            self.assertEqual(saved["version"], 6)
            for q in payload["decision_questions"]:
                q.pop("decision_mode", None)
            payload["version"] = 3
            self.assertEqual(await self.plugin.web_decision_config(), 409)
            self.assertEqual(self.plugin.config.save_config_async.await_count, 1)

    async def test_official_final_text_schedules_one_post_workflow_for_original_round(self):
        from astrbot.api.provider import DecisionBooleanAnswer
        from astrbot_plugin_quick_decision.config_util import legacy_questions

        self.main_mod.ask_decision = self._orig_ask
        post = {"id": "check", "stage": "post_reply", "type": "boolean", "instructions": "达标？"}
        self.plugin.config["decision_questions"] = {"version": 2, "questions": [
            legacy_questions()[0].to_dict(),
            dict(post, branches={"true": "extra"}), dict(post, id="extra"),
        ]}
        started, release = asyncio.Event(), asyncio.Event()

        async def judge(questions, *, state):
            if "extra" in questions:
                raise TimeoutError("判断超时")
            started.set()
            await release.wait()
            return {"check": DecisionBooleanAnswer(probability=1)}

        ask = AsyncMock(side_effect=judge)
        self.plugin.context.decision = SimpleNamespace(ask=ask)
        event = FakeEvent(text="你好", is_private=True, at_or_wake=True)
        await self.plugin.on_chat_message(event)
        result = event.get_extra("quick_decision")
        response = dict(role="assistant", is_chunk=False, tools_call_name=[], completion_text="完整回复")
        for fields in ({"is_chunk": True}, {"tools_call_name": ["search"]}, {"role": "err"},
                       {"completion_text": "   "}):
            await self.plugin.on_llm_response(event, SimpleNamespace(**(response | fields)))
        self.assertFalse(self.plugin._post_tasks)
        final = SimpleNamespace(**response)
        await asyncio.wait_for(self.plugin.on_llm_response(event, final), timeout=1)
        await self.plugin.on_llm_response(event, final)
        await asyncio.wait_for(started.wait(), timeout=1)
        self.assertEqual(len(self.plugin._post_tasks), 1)
        await self.plugin.sessions.append_log({"round_id": "next"})
        release.set()
        await asyncio.gather(*self.plugin._post_tasks)
        next_round, original = self.plugin.sessions.recent_logs()
        self.assertNotIn("post_decision", next_round)
        self.assertEqual(original["round_id"], result.round_id)
        post_log = original["post_decision"]
        self.assertEqual(post_log["answers"], {"check": True})
        self.assertEqual(post_log["outcomes"]["extra"]["reason"], "batch_error")
        self.assertEqual(post_log["error"], "判断超时")
        self.assertFalse(result.failed)
        self.assertEqual(final.completion_text, "完整回复")
        self.assertEqual(ask.await_count, 2)

    async def test_new_editor_reads_migrates_previews_and_deletes_a_parent(self):
        from astrbot_plugin_quick_decision import web_api
        class Config(dict):
            save_config_async = AsyncMock()
            save_config = save_config_async
        self.plugin.config = Config(self.plugin.config)
        request = SimpleNamespace(method="GET", query={"version": "8"}, json=AsyncMock())
        with (patch.object(web_api, "request", request),
              patch.object(web_api, "json_response", side_effect=lambda body: body),
              patch.object(web_api, "error_response", side_effect=lambda msg, status_code: status_code)):
            initial = await self.plugin.web_decision_config()
            self.assertEqual(initial["version"], 8)
            self.assertTrue(all("role" not in q for q in initial["default_questions"]))
            self.assertEqual(set(initial["actions"]),
                             {"request_reply", "add_context", "quote_message", "send_meme",
                              "voice_reply", "text_reply", "voice_tone"})
            request.method = "POST"
            request.json.return_value = initial
            saved = await self.plugin.web_decision_config()
            self.assertEqual(saved["decision_questions"], initial["decision_questions"])
            sample = dict(saved, scene="group_passive",
                          answers={"action": "reply", "address_message": "true", "target_message_id": "*"})
            request.json.return_value = sample
            preview = await self.plugin.web_decision_preview()
            self.assertEqual(preview["quote"], "sample_message")
            self.assertEqual(preview["reply_sources"], ["action=reply"])
            saved["decision_questions"] = [q for q in saved["decision_questions"] if q["id"] != "action"]
            request.json.return_value = saved
            updated = await self.plugin.web_decision_config()
            orphan = next(q for q in updated["decision_questions"] if q["id"] == "address_message")
            self.assertEqual(orphan["triggers"], [])
            request.json.return_value = {"version": 6, "decision_questions": []}
            self.assertEqual(await self.plugin.web_decision_config(), 409)
            self.assertEqual(self.plugin.config.save_config_async.await_count, 2)

    async def test_v5_independent_picture_without_speech_when_interrupt_disabled(self):
        from astrbot.api.provider import DecisionChoiceAnswer
        self.main_mod.ask_decision = self._orig_ask
        self.plugin.config["group"]["enable_interrupt"] = False
        self.plugin.config["decision_questions"] = {"version": 5, "questions": [{
            "id": "react", "type": "choice", "instructions": "选表情",
            "triggers": [{"event": "message_received"}], "candidate_source": "memes",
            "options": [{"id": "*", "criteria": "{描述}"}, {"id": "skip", "criteria": "跳过"}],
            "actions": {"*": [{"tool": "send_meme"}]},
        }]}
        self.plugin.meme_adapter.described_catalog = lambda: [{"id": "cat", "description": "猫"}]
        ask = AsyncMock(return_value={"react": DecisionChoiceAnswer(choice="cat", probability=1,
                                                                    probabilities={"cat": 1}, confidence=1)})
        self.plugin.context.decision = SimpleNamespace(ask=ask)
        self.plugin.context.send_message = AsyncMock(return_value=True)
        event = FakeEvent(text="有意思")
        with patch("astrbot_plugin_quick_decision.decision_tools.meme_chain", return_value=SimpleNamespace(chain=["cat"])):
            await self.plugin.on_chat_message(event)
        ask.assert_awaited_once()
        self.plugin.context.send_message.assert_awaited_once()
        self.assertTrue(event.is_stopped())
        self.assertIsNone(event.get_extra("quick_decision"))
        log, = self.plugin.sessions.recent_logs()
        self.assertEqual((log["action"], log["tool_calls"][0]["status"]), ("none", "executed"))

    async def test_post_picture_joins_confirmed_send_in_either_order(self):
        from astrbot.api.provider import DecisionChoiceAnswer
        self.main_mod.ask_decision = self._orig_ask
        self.plugin.config["decision_questions"] = {"version": 5, "questions": [
            {"id": "start", "type": "boolean", "instructions": "开口？", "private_default": "false",
             "triggers": [{"event": "message_received"}], "actions": {"false": [{"tool": "request_reply"}]}},
            {"id": "picture", "type": "choice", "instructions": "选图", "stage": "post_reply",
             "triggers": [{"event": "reply_generated"}], "candidate_source": "memes",
             "options": [{"id": "*", "criteria": "{描述}"}, {"id": "skip", "criteria": "跳过"}],
             "actions": {"*": [{"tool": "send_meme", "when": "after_send"}]}}]}
        self.plugin.meme_adapter.described_catalog = lambda: [{"id": "cat", "description": "猫"}]
        self.plugin.context.decision = SimpleNamespace(ask=AsyncMock(return_value={
            "picture": DecisionChoiceAnswer(choice="cat", probability=1, probabilities={"cat": 1}, confidence=1)}))
        for index, (post_first, confirmed) in enumerate(((True, True), (False, True), (False, False), (True, False))):
            segmenter = SimpleNamespace(should_delay=lambda **_: True, interval_for_image=AsyncMock(return_value=2),
                                        sleep=AsyncMock())
            with (self.subTest(post_first=post_first, confirmed=confirmed),
                  patch("astrbot_plugin_quick_decision.decision_tools.meme_chain", return_value=SimpleNamespace(chain=["cat"])),
                  patch("astrbot_plugin_quick_decision.official_segment.OfficialSegmenter.from_context", return_value=segmenter)):
                send = self.plugin.context.send_message = AsyncMock(return_value=True)
                event = FakeEvent(umo=f"room{index}", text="hi", is_private=True, at_or_wake=True)
                await self.plugin.on_chat_message(event)
                decision = event.get_extra("quick_decision")
                self.assertEqual(decision.reply_sources, ["start=false"])
                text = self.main_mod.Plain("最终正文")
                event.set_result(SimpleNamespace(chain=[text], is_llm_result=lambda: True))
                await self.plugin.on_llm_response(event, SimpleNamespace(role="assistant", is_chunk=False,
                                                  tools_call_name=[], completion_text=text.text))
                if post_first:
                    await asyncio.gather(*self.plugin._post_tasks)
                    send.assert_not_awaited()
                event._has_send_oper = confirmed
                await self.plugin.after_message_sent(event)
                await asyncio.gather(*self.plugin._post_tasks)
                await self.plugin.after_message_sent(event)
                self.assertEqual(send.await_count, int(confirmed))
                self.assertEqual(segmenter.sleep.await_count, int(confirmed))
                self.assertEqual(decision.tool_calls["send_meme"].status, "executed" if confirmed else "skipped")

    async def test_wake_prefix_uses_default_agent_with_interrupts_disabled(self):
        self.plugin.config["group"]["enable_interrupt"] = False
        event = FakeEvent(text="小助手 帮忙看看", at_or_wake=True)
        self.assertIsNone(await self.plugin.on_chat_message(event))
        self.assertFalse(event.is_stopped())
        self.assertTrue(event.get_extra("quick_decision").official_llm)
        self.assertFalse(event.call_llm)

    async def test_group_interrupt_yields_request_llm_without_stop(self) -> None:
        event = FakeEvent(
            text="插句话",
            at_or_wake=False,
            is_private=False,
            message_id="g1",
            timestamp=20,
        )
        req = await self.plugin.on_chat_message(event)
        self.assertIsInstance(req, ProviderRequest)
        self.assertEqual(req.prompt, "插句话")
        self.assertIsNotNone(req.conversation)
        self.assertEqual(req.contexts, [])
        self.assertTrue(event.call_llm)
        self.assertFalse(event.is_stopped())
        self.assertTrue(req.conversation.cid)

    async def test_group_cooldown_skips_judgement_without_immediate_tools(self) -> None:
        self.plugin.config["cooldown_seconds"] = 300
        event = FakeEvent(
            umo="aiocqhttp:GroupMessage:cooldown",
            text="普通群消息",
            at_or_wake=False,
            is_private=False,
        )
        self.plugin.sessions.ensure(_profile(event)).last_bot_ts = 10**12

        await self.plugin.on_chat_message(event)

        self.assertEqual(self.decision_calls, 0)
        self.assertTrue(event.is_stopped())
        log = self.plugin.sessions.recent_logs()[0]
        self.assertEqual(log["raw"]["skip_reason"], "冷却中跳过")

    async def test_consecutive_cooldown_skips_share_one_log_record(self) -> None:
        self.plugin.config["cooldown_seconds"] = 300
        umo = "aiocqhttp:GroupMessage:busy-cooldown"

        async def message(text, mid, target=umo, cooling=True):
            event = FakeEvent(umo=target, text=text, message_id=mid, timestamp=10)
            self.plugin.sessions.ensure(_profile(event)).last_bot_ts = 10**12 if cooling else 0
            return await self.plugin.on_chat_message(event)

        await message("一", "c1")
        first_time = self.plugin.sessions.recent_logs()[0]["time"]
        await message("别的群", "o1", "aiocqhttp:GroupMessage:other")
        await message("二", "c2")
        await message("三", "c3")
        logs = self.plugin.sessions.recent_logs(umo=umo)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["skip_count"], 3)
        self.assertEqual(logs[0]["first_time"], first_time)
        self.assertEqual(logs[0]["trigger_text"], "三")
        # 并入的记录移到末尾，保持日志按时间排列。
        self.assertEqual(self.plugin.sessions.recent_logs()[0]["umo"], umo)
        self.assertEqual(len(self.plugin.sessions.recent_logs()), 2)

        # 中间有一轮真正的判断时，之后的跳过重新计数。
        self.assertIsInstance(await message("现在可以说话了", "c4", cooling=False), ProviderRequest)
        release_official_pipeline(self.plugin.sessions.get(umo))
        await message("四", "c5")
        latest = self.plugin.sessions.recent_logs(umo=umo)[0]
        self.assertEqual(latest["raw"]["skip_reason"], "冷却中跳过")
        self.assertNotIn("skip_count", latest)
        self.assertEqual(len(self.plugin.sessions.recent_logs(umo=umo)), 3)

    async def test_silence_without_any_possible_action_starts_no_round(self) -> None:
        import time

        from astrbot_plugin_quick_decision.event_utils import event_profile

        self.plugin.config["group"]["enable_proactive_after_silence"] = False
        self.plugin._run_proactive = AsyncMock()
        event = FakeEvent(umo="aiocqhttp:GroupMessage:quiet", text="hi", message_id="q1", timestamp=10)
        runtime = self.plugin.sessions.ensure(event_profile(event))
        runtime.last_user_ts = time.time() - 4000

        await self.plugin._check_silence()
        await self.plugin._check_silence()
        await _drain_proactive(self.plugin)
        self.plugin._run_proactive.assert_not_awaited()
        self.assertEqual(self.plugin.sessions.recent_logs(), [])
        self.assertTrue(self.plugin.sessions.silence_already_evaluated(event.unified_msg_origin))

        # 有命中即执行的动作（如命中后立刻发表情）时仍要起回合，由回合内按候选细查。
        runtime.last_user_ts = time.time() - 3999
        with patch.object(self.main_mod, "has_on_match_actions", lambda *a, **k: True):
            await self.plugin._check_silence()
        await _drain_proactive(self.plugin)
        self.plugin._run_proactive.assert_awaited_once()

    async def test_empty_meme_candidates_do_not_keep_cooldown_judgement_alive(self) -> None:
        self.plugin.config.update(
            cooldown_seconds=300,
            decision_questions={"version": 5, "questions": [{
                "id": "picture", "type": "choice", "instructions": "配图？",
                "triggers": [{"event": "message_received"}],
                "candidate_source": "memes",
                "options": [
                    {"id": "none", "criteria": "不配图"},
                    {"id": "*", "criteria": "{描述}"},
                ],
                "actions": {"*": [{"tool": "send_meme", "when": "on_match"}]},
            }]},
        )
        event = FakeEvent(
            umo="aiocqhttp:GroupMessage:empty-memes",
            text="普通群消息",
            at_or_wake=False,
            is_private=False,
        )
        self.plugin.sessions.ensure(_profile(event)).last_bot_ts = 10**12

        await self.plugin.on_chat_message(event)

        self.assertEqual(self.decision_calls, 0)

    async def test_merged_messages_one_generation_and_full_prompt(self) -> None:
        first = FakeEvent(
            umo="aiocqhttp:FriendMessage:9",
            text="第一句",
            is_private=True,
            at_or_wake=True,
            message_id="a",
            timestamp=50,
            sender="u",
            sender_name="用户",
        )
        # 开一个未完成的决策，让后续消息并入。
        runtime = self.plugin.sessions.ensure(_profile(first))
        runtime.deciding = True
        # 先写入第一条
        self.plugin.sessions.append_message(
            _profile(first),
            _user("a", "第一句", 50, sender="u", name="用户"),
            20,
        )
        second = FakeEvent(
            umo=first.unified_msg_origin,
            text="第二句请一起看",
            is_private=True,
            at_or_wake=True,
            message_id="b",
            timestamp=50.3,
            sender="u",
            sender_name="用户",
        )
        sibling = await self.plugin.on_chat_message(second)
        self.assertIsNone(sibling)
        self.assertTrue(second.is_stopped())
        self.assertFalse(second.call_llm)
        self.assertEqual(self.decision_calls, 0)

        # 主事件此时才完成判断并交接。手动构造已缓冲的窗口后调用 _run_passive_round。
        first_run = FakeEvent(
            umo=first.unified_msg_origin,
            text="第一句",
            is_private=True,
            at_or_wake=True,
            message_id="a",
            timestamp=50,
            sender="u",
            sender_name="用户",
        )
        runtime.deciding = True
        req = await self.plugin._run_passive_round(first_run, _profile(first_run))
        self.assertIsInstance(req, ProviderRequest)
        self.assertIn("第一句", req.prompt)
        self.assertIn("第二句请一起看", req.prompt)
        self.assertTrue(first_run.call_llm)
        self.assertFalse(first_run.is_stopped())
        self.assertEqual(self.decision_calls, 1)

        # 官方生成尚未结束时，第三条挂起；释放后补一轮判断。
        third = FakeEvent(
            umo=first.unified_msg_origin,
            text="第三句",
            is_private=True,
            at_or_wake=True,
            message_id="c",
            timestamp=51,
        )
        again_task = asyncio.create_task(self.plugin.on_chat_message(third))
        await asyncio.sleep(0)
        self.assertFalse(again_task.done())
        release_official_pipeline(runtime)
        again = await again_task
        self.assertIsNone(again)
        self.assertEqual(self.decision_calls, 2)

    async def test_message_during_official_reply_is_rejudged_after_release(self) -> None:
        first = FakeEvent(
            umo="aiocqhttp:GroupMessage:follow-up",
            text="第一句",
            message_id="first",
            timestamp=10,
        )
        self.assertIsInstance(await self.plugin.on_chat_message(first), ProviderRequest)

        second = FakeEvent(
            umo=first.unified_msg_origin,
            text="追问",
            message_id="second",
            timestamp=11,
        )
        task = asyncio.create_task(self.plugin.on_chat_message(second))
        await asyncio.sleep(0)
        self.assertFalse(task.done())

        runtime = self.plugin.sessions.get(first.unified_msg_origin)
        release_official_pipeline(runtime)
        follow_up = await task

        self.assertIsInstance(follow_up, ProviderRequest)
        self.assertEqual(self.decision_calls, 2)

    async def test_pipeline_without_after_send_is_released_after_grace(self) -> None:
        # AstrBot 在正文为空等情况下不调用 after_message_sent，会话不能因此永久挂起。
        first = FakeEvent(umo="aiocqhttp:FriendMessage:stale", text="你好", is_private=True,
                          at_or_wake=True, message_id="s1", timestamp=10)
        await self.plugin.on_chat_message(first)
        runtime = self.plugin.sessions.get(first.unified_msg_origin)
        stale = first.get_extra("quick_decision")
        self.assertIs(runtime.pending_pipeline, stale)
        self.assertIs(runtime.pipeline_event, first)

        second = FakeEvent(umo=first.unified_msg_origin, text="在吗", is_private=True,
                           at_or_wake=True, message_id="s2", timestamp=20)
        task = asyncio.create_task(self.plugin.on_chat_message(second))
        await asyncio.sleep(0)
        grace = self.main_mod.PIPELINE_GRACE_SECONDS
        await self.plugin._release_stale_pipelines(now=runtime.pipeline_started_at + grace - 1)
        self.assertIs(runtime.pending_pipeline, stale)
        self.assertFalse(task.done())

        await self.plugin._release_stale_pipelines(now=runtime.pipeline_started_at + grace)
        self.assertIsNone(await task)
        self.assertEqual(self.decision_calls, 2)
        self.assertIs(self.plugin.sessions.find_log(stale.round_id)["reply_sent"], False)
        self.assertFalse(stale.text_sent)

        # 第二轮占用会话后，第一轮迟到的发送钩子不能把它释放掉。
        current = second.get_extra("quick_decision")
        self.assertIs(runtime.pending_pipeline, current)
        await self.plugin.after_message_sent(first)
        self.assertIs(runtime.pending_pipeline, current)
        self.assertTrue(runtime.deciding)

    async def test_running_official_agent_keeps_pipeline_until_cap(self) -> None:
        event = FakeEvent(umo="aiocqhttp:FriendMessage:slow-agent", text="查一下", is_private=True,
                          at_or_wake=True, message_id="a1", timestamp=10)
        await self.plugin.on_chat_message(event)
        runtime = self.plugin.sessions.get(event.unified_msg_origin)
        decision = runtime.pending_pipeline
        started = runtime.pipeline_started_at
        runner = SimpleNamespace(done=lambda: False)
        with patch.object(self.main_mod, "active_event_runner", lambda ev: runner if ev is event else None):
            later = started + self.main_mod.PIPELINE_GRACE_SECONDS * 2
            await self.plugin._release_stale_pipelines(now=later)
            self.assertIs(runtime.pending_pipeline, decision)
            self.assertEqual(runtime.pipeline_active_at, later)
            await self.plugin._release_stale_pipelines(now=started + self.main_mod.PIPELINE_MAX_SECONDS)
        self.assertIsNone(runtime.pending_pipeline)
        self.assertIsNone(runtime.pipeline_event)
        self.assertFalse(runtime.deciding)
        self.assertTrue(runtime.pipeline_released.is_set())

    async def test_initialize_runs_legacy_migration_off_the_event_loop(self) -> None:
        import threading

        calls = []
        self.plugin.data_dir = Path(self.tmp.name)
        self.plugin.meme_adapter.resolve_data_dir = lambda: Path(self.tmp.name) / "meme_manager"

        def fake_migrate(data_dir, meme_dir):
            calls.append((data_dir, meme_dir, threading.current_thread()))

        with patch.object(self.main_mod, "maybe_migrate_legacy_library", fake_migrate):
            await self.plugin.initialize()
        self.plugin._silence_task.cancel()
        await asyncio.gather(self.plugin._silence_task, return_exceptions=True)
        self.assertEqual(len(calls), 1)
        data_dir, meme_dir, thread = calls[0]
        self.assertEqual((data_dir, meme_dir), (Path(self.tmp.name), Path(self.tmp.name) / "meme_manager"))
        self.assertIsNot(thread, threading.main_thread())

    async def test_release_ignores_other_rounds(self) -> None:
        from astrbot_plugin_quick_decision.session_store import SessionRuntime

        runtime = SessionRuntime(profile=SessionProfile("aiocqhttp:GroupMessage:guard", False))
        current, old = DecisionResult(action="reply"), DecisionResult(action="reply")
        begin_official_pipeline(runtime, current)
        self.assertFalse(release_official_pipeline(runtime, old))
        self.assertIs(runtime.pending_pipeline, current)
        self.assertTrue(release_official_pipeline(runtime, current))
        self.assertIsNone(runtime.pending_pipeline)

    async def test_follow_up_rejudgement_still_obeys_cooldown(self) -> None:
        first = FakeEvent(
            umo="aiocqhttp:GroupMessage:follow-up-cooldown",
            text="第一句",
            message_id="first",
            timestamp=10,
        )
        self.assertIsInstance(await self.plugin.on_chat_message(first), ProviderRequest)

        second = FakeEvent(
            umo=first.unified_msg_origin,
            text="追问",
            message_id="second",
            timestamp=11,
        )
        task = asyncio.create_task(self.plugin.on_chat_message(second))
        await asyncio.sleep(0)
        self.plugin.config["cooldown_seconds"] = 300
        self.plugin.sessions.get(first.unified_msg_origin).last_bot_ts = 10**12
        release_official_pipeline(self.plugin.sessions.get(first.unified_msg_origin))

        self.assertIsNone(await task)
        self.assertTrue(second.is_stopped())
        self.assertEqual(self.decision_calls, 1)

    async def test_cancelled_follow_up_is_removed_from_pending(self) -> None:
        first = FakeEvent(
            umo="aiocqhttp:GroupMessage:follow-up-cancel",
            text="第一句",
            message_id="first",
            timestamp=10,
        )
        self.assertIsInstance(await self.plugin.on_chat_message(first), ProviderRequest)

        second = FakeEvent(
            umo=first.unified_msg_origin,
            text="追问",
            message_id="second",
            timestamp=11,
        )
        task = asyncio.create_task(self.plugin.on_chat_message(second))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(self.plugin.sessions.get(first.unified_msg_origin).pending, [])

    async def test_mention_during_proactive_round_is_judged_after_it_ends(self) -> None:
        import time

        from astrbot_plugin_quick_decision.event_utils import event_profile

        event = FakeEvent(
            umo="aiocqhttp:GroupMessage:proactive-busy",
            text="在吗",
            message_id="during",
            timestamp=10,
            at_or_wake=True,
        )
        runtime = self.plugin.sessions.ensure(event_profile(event))
        runtime.last_user_ts = time.time() - 3600
        gate = asyncio.Event()

        async def slow_proactive(*_a, **_k):
            await gate.wait()

        self.plugin._run_proactive = slow_proactive
        silence = asyncio.create_task(self.plugin._check_silence())
        await asyncio.sleep(0)
        self.assertTrue(runtime.deciding)

        # 冷场回合判断、生成、发送期间，@ 消息挂起而不是被丢弃。
        task = asyncio.create_task(self.plugin.on_chat_message(event))
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertFalse(event.is_stopped())

        gate.set()
        await silence
        self.assertIsNone(await task)
        self.assertFalse(event.is_stopped())
        self.assertEqual(self.decision_calls, 1)
        self.assertIsNotNone(runtime.pending_pipeline)

    async def test_cancelled_wait_for_proactive_round_is_removed_from_pending(self) -> None:
        import time

        from astrbot_plugin_quick_decision.event_utils import event_profile

        event = FakeEvent(umo="aiocqhttp:GroupMessage:proactive-cancel", text="在吗", message_id="x", timestamp=10)
        runtime = self.plugin.sessions.ensure(event_profile(event))
        runtime.last_user_ts = time.time() - 3600
        gate = asyncio.Event()

        async def slow_proactive(*_a, **_k):
            await gate.wait()

        self.plugin._run_proactive = slow_proactive
        silence = asyncio.create_task(self.plugin._check_silence())
        await asyncio.sleep(0)
        task = asyncio.create_task(self.plugin.on_chat_message(event))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        gate.set()
        await silence
        await _drain_proactive(self.plugin)

        self.assertEqual(runtime.pending, [])
        self.assertFalse(runtime.lock.locked())
        self.assertFalse(runtime.deciding)
        self.assertEqual(self.decision_calls, 0)

    async def test_none_and_dry_run_do_not_hand_off_or_generate(self) -> None:
        async def none_ask(*_a, **k):
            self.decision_calls += 1
            return DecisionResult(action="none", scene="group_passive")

        self.main_mod.ask_decision = none_ask
        ev = FakeEvent(text="忽略我", at_or_wake=False, message_id="n1", timestamp=1)
        req = await self.plugin.on_chat_message(ev)
        self.assertIsNone(req)
        self.assertTrue(ev.is_stopped())
        self.assertFalse(ev.call_llm)
        self.assertIsNone(ev.get_extra("quick_decision"))
        runtime = self.plugin.sessions.get(ev.unified_msg_origin)
        self.assertIsNone(runtime.pending_pipeline)
        self.assertFalse(runtime.deciding)

        self.plugin.config["dry_run"] = True

        async def reply_ask(*_a, **k):
            self.decision_calls += 1
            return DecisionResult(action="reply", scene="group_passive")

        self.main_mod.ask_decision = reply_ask
        ev2 = FakeEvent(
            umo="aiocqhttp:GroupMessage:200",
            text="试运行",
            at_or_wake=False,
            message_id="d1",
            timestamp=2,
        )
        req2 = await self.plugin.on_chat_message(ev2)
        self.assertIsNone(req2)
        self.assertTrue(ev2.is_stopped())
        self.assertIsNone(ev2.get_extra("quick_decision"))
        self.assertFalse(self.plugin.sessions.get(ev2.unified_msg_origin).deciding)

    async def test_decision_error_has_zero_main_model_calls(self) -> None:
        async def bad_ask(*_a, **k):
            return DecisionResult(action="reply", error="System One timeout", scene="group_passive")

        self.main_mod.ask_decision = bad_ask
        ev = FakeEvent(text="x", at_or_wake=False, message_id="e1", timestamp=3)
        req = await self.plugin.on_chat_message(ev)
        self.assertIsNone(req)
        self.assertTrue(ev.is_stopped())
        self.assertIsNone(ev.get_extra("quick_decision"))

    async def test_real_decision_failure_preserves_private_official_fallback(
        self,
    ) -> None:
        from astrbot_plugin_quick_decision.decision_engine import ask_decision

        self.main_mod.ask_decision = ask_decision
        for private, dry_run, message in product(
            (True, False), (True, False), ("", "判断超时")
        ):
            with self.subTest(private=private, dry_run=dry_run, message=message):
                self.plugin.context.decision = SimpleNamespace(
                    ask=AsyncMock(side_effect=TimeoutError(message))
                )
                self.plugin.config["dry_run"] = dry_run
                kind = "FriendMessage" if private else "GroupMessage"
                ev = FakeEvent(
                    umo=f"aiocqhttp:{kind}:failure-{dry_run}-{bool(message)}",
                    is_private=private,
                    at_or_wake=True,
                    message_id="failure",
                )
                returned = await self.plugin.on_chat_message(ev)
                self.assertIsNone(returned)
                self.assertEqual(ev.is_stopped(), dry_run or not private)
                self.assertFalse(ev.call_llm)
                self.assertIsNone(ev.get_extra("quick_decision"))
                runtime = self.plugin.sessions.get(ev.unified_msg_origin)
                self.assertIsNone(runtime.pending_pipeline)
                self.assertFalse(runtime.deciding)
                self.assertEqual(runtime.last_decision.error, message or "TimeoutError")
                self.assertFalse(runtime.last_decision.should_speak())
                self.assertTrue(runtime.last_decision.outcomes)

    async def test_private_silence_never_enters_later_workflow(self) -> None:
        from astrbot.api.provider import DecisionChoiceAnswer
        from astrbot_plugin_quick_decision.decision_engine import ask_decision

        self.main_mod.ask_decision = ask_decision
        questions = [q.to_dict() for q in self.plugin.settings.decision_questions]
        action = next(q for q in questions if q["role"] == "action")
        action["ask_private"] = True
        self.plugin.config["decision_questions"] = {"version": 3, "questions": questions}
        self.plugin.context.decision = SimpleNamespace(ask=AsyncMock(side_effect=[
            {"action": DecisionChoiceAnswer(choice="none", probability=1,
                                          probabilities={"none": 1}, confidence=1)},
            TimeoutError("后继超时"),
        ]))
        ev = FakeEvent(umo="aiocqhttp:FriendMessage:silence", is_private=True, at_or_wake=True)
        await self.plugin.on_chat_message(ev)
        result = self.plugin.sessions.get(ev.unified_msg_origin).last_decision
        self.assertIsNone(result.error)
        self.plugin.context.decision.ask.assert_awaited_once()
        self.assertEqual(result.action, "none")
        self.assertTrue(ev.is_stopped())

    async def test_on_llm_request_attaches_temp_hints(self) -> None:
        ev = FakeEvent(text="hi", at_or_wake=False, message_id="h1", timestamp=4)
        req = await self.plugin.on_chat_message(ev)
        self.assertIsInstance(req, ProviderRequest)
        await self.plugin.on_llm_request(ev, req)
        self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertTrue(req.extra_user_content_parts[0]._no_save)
        second = ProviderRequest()
        await self.plugin.on_llm_request(ev, second)
        self.assertEqual(second.extra_user_content_parts, [])

    async def test_cancelled_handoff_cannot_send_meme_for_another_reply(self) -> None:
        self.plugin.context.send_message = AsyncMock()
        for text, manager, reason in (
            ("插话", None, "官方会话"), ("", FakeConvMgr(), "正文或附件"),
        ):
            with self.subTest(reason=reason):
                event = FakeEvent(umo=f"aiocqhttp:GroupMessage:{reason}", text=text)
                self.plugin.context.conversation_manager = manager
                result = DecisionResult(action="reply", use_meme=True, meme_id="cat")
                self.main_mod.ask_decision = AsyncMock(return_value=result)
                self.assertIsNone(await self.plugin.on_chat_message(event))
                self.assertIsNone(event.get_extra("quick_decision"))
                self.assertFalse(self.plugin.sessions.get(event.unified_msg_origin).deciding)
                event.set_result(SimpleNamespace(chain=[self.main_mod.Plain("其他插件的回复")]))
                event._has_send_oper = True
                await self.plugin.after_message_sent(event)
                row = self.plugin.sessions.recent_logs()[0]
                self.assertFalse(row["official_llm"])
                self.assertEqual(row["tool_calls"][0]["status"], "skipped")
                self.assertIn(reason, row["tool_calls"][0]["reason"])
                self.plugin.context.send_message.assert_not_awaited()

    async def test_interrupted_platform_send_is_logged_and_never_retried(self) -> None:
        started = asyncio.Event()

        async def wait_for_platform(*_args):
            started.set()
            await asyncio.Future()

        result = DecisionResult(action="reply", use_meme=True, meme_id="cat")
        self.main_mod.ask_decision = AsyncMock(return_value=result)
        self.plugin.context.send_message = AsyncMock(side_effect=wait_for_platform)
        event = FakeEvent(at_or_wake=True, text="你好")
        await self.plugin.on_chat_message(event)
        event.set_result(SimpleNamespace(chain=[self.main_mod.Plain("正文")]))
        event._has_send_oper = True
        with patch(
            "astrbot_plugin_quick_decision.decision_tools.meme_chain",
            return_value=SimpleNamespace(chain=["image"]),
        ):
            task = asyncio.create_task(self.plugin.after_message_sent(event))
            await asyncio.wait_for(started.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await self.plugin.after_message_sent(event)
        self.plugin.context.send_message.assert_awaited_once()
        receipt = self.plugin.sessions.recent_logs()[0]["tool_calls"][0]
        self.assertEqual(receipt["status"], "interrupted")
        self.assertIn("送达结果未确认", receipt["reason"])

    async def test_selected_target_quotes_only_first_group_text_segment(self) -> None:
        event = FakeEvent(message_id="new", text="最新消息", at_or_wake=True)
        self.main_mod.ask_decision = AsyncMock(
            return_value=DecisionResult(action="reply", target_message_id="old")
        )
        await self.plugin.on_chat_message(event)
        pieces = [self.main_mod.Plain("第一段"), self.main_mod.Plain("第二段")]
        event.set_result(SimpleNamespace(chain=pieces[:], is_llm_result=lambda: True))
        await self.plugin.on_decorating_result(event)
        await event.send(SimpleNamespace(chain=[Reply(id="new"), pieces[0]]))
        await event.send(SimpleNamespace(chain=[pieces[1]]))
        quotes = [c.id for msg in event._sends for c in msg.chain if hasattr(c, "id")]
        self.assertEqual(quotes, ["old"])
        self.assertEqual(event._sends[0].chain[0].id, "old")
        await self.plugin.after_message_sent(event)
        self.assertIsNone(event.get_extra("quick_decision"))
        next_answer = self.main_mod.Plain("工具执行后的另一份模型回复")
        event.set_result(SimpleNamespace(chain=[next_answer], is_llm_result=lambda: True))
        await self.plugin.on_decorating_result(event)
        await event.send(SimpleNamespace(chain=[Reply(id="new"), next_answer]))
        self.assertEqual(event._sends[-1].chain[0].id, "old")
        await self.plugin.after_message_sent(event)

    async def test_tool_preamble_waits_for_final_text_and_post_judgment(self):
        from astrbot_plugin_quick_decision import reply_generator as replies
        for requery_sent_first in (False, True):
            event = FakeEvent(umo=f"room-{requery_sent_first}", text="hi", at_or_wake=True)
            runner = SimpleNamespace(run_context=SimpleNamespace(context=SimpleNamespace(event=event)),
                                     tool_schema_mode="skills_like", done=lambda: False, get_final_llm_resp=lambda: None)
            decision = DecisionResult(action="reply", use_meme=True, meme_id="cat", post_plan=object())
            self.main_mod.ask_decision = AsyncMock(return_value=decision)
            send = self.plugin.context.send_message = AsyncMock(return_value=True)
            with (self.subTest(requery_sent_first=requery_sent_first),
                  patch.dict(replies._ACTIVE_AGENT_RUNNERS, {event.unified_msg_origin: runner}),
                  patch.object(self.main_mod, "evaluate_post_reply", new_callable=AsyncMock, return_value={}) as judge,
                  patch("astrbot_plugin_quick_decision.decision_tools.meme_chain", return_value=SimpleNamespace(chain=["cat"]))):
                await self.plugin.on_chat_message(event)
                async def deliver(text):
                    part = self.main_mod.Plain(text)
                    event.set_result(SimpleNamespace(chain=[part], is_llm_result=lambda: True))
                    await self.plugin.on_decorating_result(event)
                    await event.send(SimpleNamespace(chain=[part]))
                    await self.plugin.after_message_sent(event)
                    event.set_result(None)
                await deliver("查询中")
                self.assertIs(event.get_extra("quick_decision"), decision)
                send.assert_not_awaited()
                judge.assert_not_awaited()
                if requery_sent_first:
                    await deliver("最终正文")
                else:
                    await self.plugin.on_using_llm_tool(event, None, {})
                runner.done = lambda: True
                await self.plugin.on_llm_response(event, SimpleNamespace(role="assistant", is_chunk=False,
                                                  tools_call_name=[], completion_text="最终正文"))
                if not requery_sent_first:
                    await deliver("最终正文")
                await asyncio.gather(*self.plugin._post_tasks)
                self.assertIsNone(event.get_extra("quick_decision"))
                send.assert_awaited_once()
                self.assertEqual(judge.await_args.args[2], "最终正文")

    async def test_partial_nonstream_reply_skips_after_send_tools(self):
        for private in (True, False):
            with self.subTest(private=private):
                event = FakeEvent(umo=f"room-{private}", text="hi", is_private=private, at_or_wake=True)
                decision = DecisionResult(action="reply", use_meme=True, meme_id="cat")
                self.main_mod.ask_decision = AsyncMock(return_value=decision)
                event.send = AsyncMock(side_effect=[True, OSError("second segment failed")])
                self.plugin.context.send_message = AsyncMock()
                await self.plugin.on_chat_message(event)
                parts = [self.main_mod.Plain("第一段"), self.main_mod.Plain("第二段")]
                event.set_result(SimpleNamespace(chain=parts, is_llm_result=lambda: True))
                await event.send(SimpleNamespace(chain=parts[:1]))
                event._has_send_oper = True
                with self.assertRaises(OSError):
                    await event.send(SimpleNamespace(chain=parts[1:]))
                await self.plugin.after_message_sent(event)
                self.assertFalse(decision.text_sent)
                self.assertEqual(decision.tool_calls["send_meme"].reason, "正文未确认发送")
                self.plugin.context.send_message.assert_not_awaited()

    async def test_stream_partial_send_cannot_start_post_actions(self):
        from astrbot_plugin_quick_decision import reply_generator as replies
        marker = ContextVar("stream", default=None)
        with patch.object(replies, "streaming_response_event", marker):
            event = FakeEvent(text="hi", at_or_wake=True)
            event.send = AsyncMock(side_effect=[True, False])
            async def output():
                await self.plugin.on_llm_response(event, SimpleNamespace(role="assistant", is_chunk=False,
                                                  tools_call_name=[], completion_text="完整回复"))
                for text in ("送出", "失败"):
                    await event.send(SimpleNamespace(chain=[replies.Plain(text)]))
            event.send_streaming = output
            decision = DecisionResult(action="reply", use_meme=True, meme_id="cat", post_plan=object())
            self.main_mod.ask_decision = AsyncMock(return_value=decision)
            await self.plugin.on_chat_message(event)
            token = marker.set(event)
            try:
                await event.send_streaming()
            finally:
                marker.reset(token)
            self.assertFalse(self.plugin._post_tasks)
            self.assertFalse(decision.text_sent)
            self.assertEqual(decision.tool_calls["send_meme"].status, "skipped")

    async def test_stream_error_notice_never_counts_as_a_successful_reply(self):
        from astrbot_plugin_quick_decision import reply_generator as replies
        marker = ContextVar("stream", default=None)
        for source in ("hook", "third_party", "builtin"):
            with self.subTest(source=source), patch.object(replies, "streaming_response_event", marker):
                event = FakeEvent(umo=f"aiocqhttp:GroupMessage:{source}", at_or_wake=True, text="hi")
                runner = SimpleNamespace(run_context=SimpleNamespace(context=SimpleNamespace(event=event)),
                                         get_final_llm_resp=lambda: SimpleNamespace(role="err"))
                async def output():
                    if source == "hook":
                        await self.plugin.on_llm_response(event, SimpleNamespace(role="err"))
                    if source == "third_party":
                        event.set_extra("_third_party_runner_error", True)
                    await event.send(SimpleNamespace(chain=[replies.Plain("错误提示")]))
                event.send_streaming = output
                decision = DecisionResult(action="reply", target_message_id="42", use_meme=True, meme_id="cat")
                self.main_mod.ask_decision = AsyncMock(return_value=decision)
                await self.plugin.on_chat_message(event)
                token = marker.set(event)
                try:
                    runners = {event.unified_msg_origin: runner} if source == "builtin" else {}
                    with patch.object(replies, "_ACTIVE_AGENT_RUNNERS", runners):
                        await event.send_streaming()
                finally:
                    marker.reset(token)
                self.assertEqual([part.text for part in event._sends[0].chain], ["错误提示"])
                runtime = self.plugin.sessions.get(event.unified_msg_origin)
                self.assertFalse(is_official_pipeline_busy(runtime))
                self.assertEqual(runtime.messages[-1].role, "user")
                self.assertEqual(decision.tool_calls["send_meme"].status, "skipped")

    async def test_stream_end_releases_group_on_success_and_interruption(self):
        from astrbot_plugin_quick_decision import reply_generator as replies
        marker = ContextVar("stream", default=None)
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted), patch.object(replies, "streaming_response_event", marker):
                event = FakeEvent(umo=f"aiocqhttp:GroupMessage:stream-{interrupted}", at_or_wake=True, text="hi")
                async def output():
                    await event.send(SimpleNamespace(chain=[replies.Plain("第一段")]))
                    # MainAgentHooks.on_tool_end 清除结果，但当前流仍是同一份回复。
                    event.set_result(None)
                    await event.send(SimpleNamespace(chain=[replies.Plain("第二段")]))
                    if interrupted:
                        raise asyncio.CancelledError()
                event.send_streaming = output
                result = DecisionResult(action="reply", target_message_id="42")
                self.main_mod.ask_decision = AsyncMock(return_value=result)
                await self.plugin.on_chat_message(event)
                event.set_result(SimpleNamespace(chain=[], is_llm_result=lambda: False))
                token = marker.set(event)
                try:
                    if interrupted:
                        with self.assertRaises(asyncio.CancelledError):
                            await event.send_streaming()
                    else:
                        await event.send_streaming()
                finally:
                    marker.reset(token)
                self.assertIsNone(event.get_extra("quick_decision"))
                self.assertIs(event.send_streaming, output)
                runtime = self.plugin.sessions.get(event.unified_msg_origin)
                self.assertFalse(is_official_pipeline_busy(runtime))
                self.assertEqual(runtime.messages[-1].text, "第一段第二段")
                self.assertEqual(event._sends[0].chain[0].id, "42")
                self.assertEqual(len(event._sends[1].chain), 1)

    def test_late_messages_do_not_reopen_an_evaluated_silence(self) -> None:
        from astrbot_plugin_quick_decision.models import BufferedMessage

        profile = SessionProfile("room", False)
        store = self.plugin.sessions
        def record(ts):
            message = BufferedMessage(str(ts), "u", "User", "user", "hello", timestamp=ts)
            return store.append_message(profile, message, 10)

        with patch("time.time", return_value=1300):
            record(1200)
            store.mark_silence_evaluated(profile.umo)
            self.assertEqual(record(1000).last_activity, 1200)
            self.assertTrue(store.silence_already_evaluated(profile.umo))
            self.assertEqual(record(5000).last_activity, 1300)
            self.assertFalse(store.silence_already_evaluated(profile.umo))

    async def test_silence_timer_passes_session_time_and_actual_idle_duration(self) -> None:
        from astrbot.api.provider import DecisionChoiceAnswer
        from astrbot_plugin_quick_decision.decision_engine import ask_decision

        self.main_mod.ask_decision = ask_decision
        self.plugin.context.get_config = lambda umo: {"timezone": "UTC"}
        questions = [q.to_dict() for q in self.plugin.settings.decision_questions]
        gate = next(q for q in questions if q["id"] == "idle_action")
        gate["instructions"] = "是否应该开口？"
        length = next(q for q in questions if q["id"] == "length")
        length["instructions"] = "现在是 {当前时间}，已沉默 {冷场时长}。"
        self.plugin.config["decision_questions"] = {"version": 3, "questions": questions}
        service = AsyncMock(return_value={
            gate["id"]: DecisionChoiceAnswer(choice="none", probability=1,
                                             probabilities={"none": 1}, confidence=1),
        })
        self.plugin.context.decision = SimpleNamespace(ask=service)
        now = 1735689600
        for private in (False, True):
            runtime = self.plugin.sessions.ensure(SessionProfile(f"room-{private}", private))
            runtime.last_user_ts = now - 4000
            runtime.last_bot_ts = now - 2301
        with patch("time.time", return_value=now):
            await self.plugin._check_silence()
            await _drain_proactive(self.plugin)
        self.assertEqual(service.await_count, 2)
        for root in service.await_args_list:
            action_prompt = root.args[0][gate["id"]].instructions
            self.assertIn("当前时间：2025-01-01 00:00:00+00:00", action_prompt)
            self.assertIn("冷场时长：38 分 21 秒", action_prompt)
            self.assertNotIn("38 分", root.args[0]["use_meme"].instructions)
            self.assertIn("现在是 2025-01-01 00:00:00+00:00，已沉默 38 分 21 秒。", root.args[0]["length"].instructions)

    async def test_proactive_still_uses_self_generate(self) -> None:
        generate_calls = []

        async def fake_gen(*_a, **k):
            generate_calls.append(k)
            return "冷场招呼"

        send_calls = []

        async def fake_send(*_a, **k):
            send_calls.append(k)
            return True

        self.main_mod.generate_text = fake_gen
        self._orig_send = self.main_mod.send_decision_message
        self.main_mod.send_decision_message = fake_send

        async def proactive_ask(*_a, **k):
            return DecisionResult(action="proactive", scene="group_proactive")

        self.main_mod.ask_decision = proactive_ask
        profile = SessionProfile(umo="aiocqhttp:GroupMessage:7", is_private=False)
        runtime = self.plugin.sessions.ensure(profile)
        runtime.last_user_ts = 1
        await self.plugin._run_proactive(runtime)
        self.assertEqual(len(generate_calls), 1)
        self.assertEqual(len(send_calls), 1)
        self.assertIsNone(send_calls[0].get("event"))
        self.main_mod.send_decision_message = self._orig_send

    async def test_proactive_sends_before_post_decision_finishes(self):
        from astrbot.api.provider import DecisionChoiceAnswer

        self.main_mod.ask_decision = self._orig_ask
        gate = next(q.to_dict() for q in self.plugin.settings.decision_questions if q.id == "idle_action")
        post = {"id": "check", "type": "boolean", "stage": "post_reply", "instructions": "在问候吗？"}
        post["triggers"] = [{"event": "silence_elapsed"}]
        self.plugin.config["decision_questions"] = {"version": 3, "questions": [gate, post]}
        judging = asyncio.Event()

        async def judge(questions, *, state):
            if "check" in questions:
                self.assertEqual(state["assistant_reply"], "早上好")
                judging.set()
                await asyncio.Future()
            return {"idle_action": DecisionChoiceAnswer(choice="proactive", probability=1,
                                                    probabilities={"proactive": 1}, confidence=1)}

        ask = AsyncMock(side_effect=judge)
        self.plugin.context.decision = SimpleNamespace(ask=ask)
        runtime = self.plugin.sessions.ensure(SessionProfile("room", False))
        with (
            patch.object(self.main_mod, "generate_text", AsyncMock(return_value="早上好")),
            patch.object(self.main_mod, "send_decision_message", AsyncMock(return_value=True)) as send,
        ):
            await asyncio.wait_for(self.plugin._run_proactive(runtime), timeout=1)
            self.assertEqual(send.await_args.kwargs["text"], "早上好")
        await asyncio.wait_for(judging.wait(), timeout=1)
        task, = self.plugin._post_tasks
        await self.plugin.terminate()
        self.assertTrue(task.cancelled())
        self.assertFalse(self.plugin._post_tasks)
        self.assertEqual(ask.await_count, 2)


class MemeAfterSegmentsTests(unittest.IsolatedAsyncioTestCase):
    async def test_meme_only_after_all_text_and_skipped_on_failure(self) -> None:
        import importlib
        import astrbot_plugin_quick_decision.reply_generator as rg

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
            def fromFileSystem(cls, path: str) -> "Image":
                return cls(path)

        sys.modules["astrbot.api.event"].MessageChain = MessageChain
        sys.modules["astrbot.api.message_components"].Image = Image
        sys.modules["astrbot.api.message_components"].Reply = Reply
        rg = importlib.reload(rg)
        sent: list[str] = []

        async def fake_send(umo, chain):  # noqa: ARG002
            texts = [
                getattr(comp, "text", None) or getattr(comp, "file", "")
                for comp in chain.chain
            ]
            if "fail" in texts:
                return False
            sent.append(" ".join(str(t) for t in texts))
            return True

        context = SimpleNamespace(send_message=fake_send)

        class Store:
            def resolve_image(self, meme_id):  # noqa: ARG002
                return Path("/tmp/fake.jpg")

        class Seg:
            def split_text(self, text, **_k):
                return text.split("|")

            def should_delay(self, **_k):
                return False

            async def interval_for_plain(self, _seg):
                return 0

            async def interval_for_image(self):
                return 0

            async def sleep(self, _s):
                return None

        decision = DecisionResult(action="reply", use_meme=True, meme_id="m1")
        ok = await rg.send_decision_message(
            context=context,
            umo="umo",
            text="一|二",
            resolver=Store(),
            decision=decision,
            dry_run=False,
            quote_target=False,
            segmenter=Seg(),
        )
        self.assertTrue(ok)
        self.assertEqual(len(sent), 3)
        self.assertTrue(sent[-1].endswith("fake.jpg"))

        sent.clear()
        decision = DecisionResult(action="reply", use_meme=True, meme_id="m1")
        failed = await rg.send_decision_message(
            context=context,
            umo="umo",
            text="一|fail|三",
            resolver=Store(),
            decision=decision,
            dry_run=False,
            quote_target=False,
            segmenter=Seg(),
        )
        self.assertTrue(failed)
        self.assertEqual(sent, ["一", "三"])
        self.assertFalse(any("fake.jpg" in item for item in sent))


if __name__ == "__main__":
    unittest.main()
