"""群聊直接发送开关：不向 QQ 发消息，不访问网络。"""

# ruff: noqa: E402, I001

from __future__ import annotations

import sys
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
        plain = type("Plain", (), {"text": message})()
        self.chain.append(plain)
        return self


class Reply:
    def __init__(self, id: str = "") -> None:
        self.id = id


sys.modules["astrbot.api.event"].MessageChain = MessageChain
sys.modules["astrbot.api.message_components"].Reply = Reply

import importlib
import astrbot_plugin_quick_decision.reply_generator as reply_generator
importlib.reload(reply_generator)
from astrbot.api.message_components import Reply as ReplyComponent
from astrbot_plugin_quick_decision.config_util import PluginSettings
from astrbot_plugin_quick_decision.models import DecisionResult
from astrbot_plugin_quick_decision.reply_generator import build_send_chain


class _DummyStore:
    def resolve_image(self, meme_id):  # noqa: ANN001, ARG002
        return None


class DirectSendTests(unittest.TestCase):
    def test_old_and_new_installations_default_to_first(self) -> None:
        for group in ({}, {"direct_send": True}, {"direct_send": False}, {"target_quote_mode": "bad"}):
            self.assertEqual(PluginSettings({"group": group}).target_quote_mode, "first")

    def test_explicit_mode_wins_over_old_setting(self) -> None:
        for mode in ("none", "first", "all"):
            settings = PluginSettings({"group": {"direct_send": True, "target_quote_mode": mode}})
            self.assertEqual(settings.target_quote_mode, mode)

    def test_build_chain_omits_reply_when_disabled(self) -> None:
        decision = DecisionResult(action="reply", target_message_id="123")
        chain = build_send_chain("hello", _DummyStore(), decision, quote_target=False)
        self.assertFalse(any(getattr(item, "id", None) == "123" for item in chain.chain))

    def test_build_chain_includes_reply_when_enabled(self) -> None:
        decision = DecisionResult(action="reply", target_message_id="123")
        chain = build_send_chain("hello", _DummyStore(), decision, quote_target=True)
        self.assertTrue(any(getattr(item, "id", None) == "123" for item in chain.chain))


if __name__ == "__main__":
    unittest.main()
