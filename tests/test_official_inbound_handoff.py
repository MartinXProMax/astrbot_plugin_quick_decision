"""入站官方 request_llm 交接的隔离回归。不发 QQ，不调生产模型，不访问网络。"""

# ruff: noqa: E402, I001

from __future__ import annotations

import asyncio
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

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
    install_direct_send_send_guard,
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
            DecisionResult(session_hints=["请简短回复", "  "]),
        )
        self.assertEqual(len(req.extra_user_content_parts), 1)
        part = req.extra_user_content_parts[0]
        self.assertTrue(part._no_save)
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


class DirectSendGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_guard_strips_reply_inserted_after_decorate_hook(self) -> None:
        event = FakeEvent(is_private=False)
        chain = [SimpleNamespace(text="你好")]
        # 钩子阶段：链上还没有官方引用。
        self.assertTrue(
            prepare_official_result_chain(
                chain,
                dry_run=False,
                should_speak=True,
                group_direct_send=True,
                is_private=False,
            )
        )
        install_direct_send_send_guard(event)
        # 官方 ResultDecorateStage 在钩子之后插入引用。
        chain.insert(0, Reply(id="inbound-1"))
        payload = SimpleNamespace(chain=chain)
        await event.send(payload)
        self.assertFalse(any(type(item).__name__ == "Reply" for item in payload.chain))
        self.assertEqual(getattr(payload.chain[0], "text", None), "你好")

    async def test_guard_not_used_when_direct_send_off(self) -> None:
        chain = [Reply(id="keep"), SimpleNamespace(text="hi")]
        self.assertTrue(
            prepare_official_result_chain(
                chain,
                dry_run=False,
                should_speak=True,
                group_direct_send=False,
                is_private=False,
            )
        )
        self.assertTrue(any(type(item).__name__ == "Reply" for item in chain))


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
    plugin._silence_task = None
    plugin._generate_calls: list = []
    plugin._send_calls: list = []
    return plugin, main_mod


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

        # 官方生成尚未结束时，第三条仍被抑制，不会再决策。
        third = FakeEvent(
            umo=first.unified_msg_origin,
            text="第三句",
            is_private=True,
            at_or_wake=True,
            message_id="c",
            timestamp=51,
        )
        again = await self.plugin.on_chat_message(third)
        self.assertIsNone(again)
        self.assertTrue(third.is_stopped())
        self.assertEqual(self.decision_calls, 1)

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

    async def test_on_llm_request_strips_orphan_without_decision(self) -> None:
        ev = FakeEvent(text="hi", at_or_wake=False, message_id="orphan", timestamp=4)
        req = ProviderRequest(
            contexts=[
                {"role": "user", "content": "q"},
                {
                    "role": "assistant",
                    "content": [{"type": "think", "think": "", "encrypted": "opaque"}],
                    "tool_calls": [
                        {
                            "id": "call-g",
                            "type": "function",
                            "function": {"name": "astrbot_file_read_tool", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "assistant", "content": [{"type": "text", "text": "kept"}]},
            ]
        )
        await self.plugin.on_llm_request(ev, req)
        self.assertNotIn("tool_calls", req.contexts[1])
        self.assertEqual(req.contexts[1]["content"][0]["encrypted"], "opaque")
        self.assertEqual(req.contexts[2]["content"][0]["text"], "kept")
        self.assertEqual(req.extra_user_content_parts, [])

    async def test_on_llm_request_attaches_temp_hints(self) -> None:
        ev = FakeEvent(text="hi", at_or_wake=False, message_id="h1", timestamp=4)
        req = await self.plugin.on_chat_message(ev)
        self.assertIsInstance(req, ProviderRequest)
        await self.plugin.on_llm_request(ev, req)
        self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertTrue(req.extra_user_content_parts[0]._no_save)

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

        generate_calls.clear()
        send_calls.clear()
        self.plugin.config["dry_run"] = True
        await self.plugin._run_proactive(runtime)
        self.assertEqual(generate_calls, [])
        self.assertEqual(send_calls, [])
        self.main_mod.send_decision_message = self._orig_send


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

        async def fake_send(context, umo, chain):  # noqa: ARG002
            texts = [
                getattr(comp, "text", None) or getattr(comp, "file", "")
                for comp in chain.chain
            ]
            if "fail" in texts:
                return False
            sent.append(" ".join(str(t) for t in texts))
            return True

        self._orig = rg._send_chain
        rg._send_chain = fake_send

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
            context=SimpleNamespace(),
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
        failed = await rg.send_decision_message(
            context=SimpleNamespace(),
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
        rg._send_chain = self._orig


if __name__ == "__main__":
    unittest.main()
