"""官方 segmented_reply 复用的隔离测试。不访问网络，不发送 QQ，不真实 sleep。"""

# ruff: noqa: E402, I001

from __future__ import annotations

import asyncio
import re
import sys
import tempfile
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

import importlib
import astrbot_plugin_quick_decision.reply_generator as reply_generator
import astrbot_plugin_quick_decision.official_segment as official_segment

importlib.reload(official_segment)
importlib.reload(reply_generator)

from astrbot_plugin_quick_decision.models import DecisionResult
from astrbot_plugin_quick_decision.official_segment import OfficialSegmenter
from astrbot_plugin_quick_decision.reply_generator import (
    after_official_text_sent,
    send_decision_message,
)


PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)

# 配置中若写成两个字符 \\n，不是换行。
LITERAL_BACKSLASH_N = "\\n"


def official_style_split_by_words(text: str, split_words: list[str]) -> list[str]:
    """与官方 ResultDecorateStage._split_text_by_words 相同的算法，供隔离测试使用。"""
    if not split_words:
        return [text]
    escaped_words = sorted([re.escape(word) for word in split_words], key=len, reverse=True)
    pattern = re.compile(f"(.*?({'|'.join(escaped_words)})|.+$)", re.DOTALL)
    segments = pattern.findall(text)
    result = []
    for seg in segments:
        if isinstance(seg, tuple):
            content = seg[0]
            if not isinstance(content, str):
                continue
            for word in split_words:
                if content.endswith(word):
                    content = content[: -len(word)]
                    break
            if content.strip():
                result.append(content)
        elif seg and seg.strip():
            result.append(seg)
    return result if result else [text]


class FakeDecorate:
    def __init__(
        self,
        *,
        enable: bool = True,
        only_llm: bool = True,
        mode: str = "words",
        threshold: int = 150,
        split_words: list[str] | None = None,
        regex: str = r".*?[。？！~…]+|.+$",
        cleanup: str = "",
    ) -> None:
        self.enable_segmented_reply = enable
        self.only_llm_result = only_llm
        self.split_mode = mode
        self.words_count_threshold = threshold
        self.split_words = split_words if split_words is not None else ["。", "？", "！", "~", "…", "\n"]
        self.regex = regex
        self.content_cleanup_rule = cleanup

    def _split_text_by_words(self, text: str) -> list[str]:
        return official_style_split_by_words(text, self.split_words)


class FakeRespond:
    def __init__(self, *, enable: bool = True, only_llm: bool = True, interval: float = 0.4) -> None:
        self.enable_seg = enable
        self.only_llm_result = only_llm
        self.interval = interval
        self.plain_calls: list[str] = []
        self.image_calls = 0

    def is_seg_reply_required(self, event) -> bool:  # noqa: ANN001
        if not self.enable_seg:
            return False
        result = event.get_result()
        if result is None:
            return False
        if self.only_llm_result and not result.is_model_result():
            return False
        if event.get_platform_name() in {
            "qq_official_webhook",
            "weixin_official_account",
            "dingtalk",
        }:
            return False
        return True

    async def _calc_comp_interval(self, comp) -> float:  # noqa: ANN001
        text = getattr(comp, "text", None)
        if text is not None:
            self.plain_calls.append(text)
        else:
            self.image_calls += 1
        return self.interval


class RecordingSleeper:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


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
        platform_name: str = "aiocqhttp",
    ) -> None:
        self.unified_msg_origin = umo
        self._result = type("Result", (), {"chain": list(chain or [])})()
        self._extra = {"quick_decision": decision} if decision is not None else {}
        if has_send_oper is not None:
            self._has_send_oper = has_send_oper
        self._platform_name = platform_name

    def get_result(self):
        return self._result

    def get_extra(self, key, default=None):  # noqa: ANN001
        return self._extra.get(key, default)

    def set_extra(self, key, value):  # noqa: ANN001
        self._extra[key] = value

    def get_platform_name(self) -> str:
        return self._platform_name


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


def _kinds(items: list[object]) -> list[str]:
    kinds = []
    for item in items:
        if isinstance(item, Image) or hasattr(item, "file"):
            kinds.append("image")
        elif isinstance(item, Reply) or (
            getattr(item, "id", None) is not None and getattr(item, "text", None) is None
        ):
            kinds.append("reply")
        else:
            kinds.append("text")
    return kinds


def _texts(items: list[object]) -> list[str]:
    return [getattr(item, "text", "") for item in items if getattr(item, "text", None)]


class OfficialSegmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.image = Path(self.temp.name) / "smile.png"
        self.image.write_bytes(PNG)
        self.store = _FileStore(self.image)
        official_segment.clear_stage_cache()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _segmenter(self, decorate=None, respond=None, sleeper=None):
        return OfficialSegmenter(
            decorate or FakeDecorate(),
            respond or FakeRespond(),
            sleeper=sleeper or RecordingSleeper(),
        )

    def test_disabled_keeps_newlines_in_one_message(self) -> None:
        text = "对呀，直接屏蔽掉就好了\n除非某人其实是故意留着的？"
        segmenter = self._segmenter(FakeDecorate(enable=False))
        self.assertEqual(segmenter.split_text(text), [text])

    def test_threshold_equal_splits_greater_does_not(self) -> None:
        text = "你好。世界"
        # 官方判定是 len(text) > threshold 才不分段。
        equal = self._segmenter(FakeDecorate(mode="words", threshold=len(text), split_words=["。"]))
        self.assertEqual(equal.split_text(text), ["你好", "世界"])
        above = self._segmenter(FakeDecorate(mode="words", threshold=len(text) - 1, split_words=["。"]))
        self.assertEqual(above.split_text(text), [text])

    def test_words_mode_strips_official_delimiters(self) -> None:
        decorate = FakeDecorate(mode="words", split_words=["。", "？", "！", "~", "…", "\n"], cleanup="")
        segmenter = self._segmenter(decorate)
        text = "对呀，直接屏蔽掉就好了\n除非某人其实是故意留着的？"
        self.assertEqual(
            segmenter.split_text(text),
            ["对呀，直接屏蔽掉就好了", "除非某人其实是故意留着的"],
        )

    def test_current_config_literal_backslash_n_does_not_split_real_newline(self) -> None:
        decorate = FakeDecorate(
            mode="words",
            split_words=["。", "？", "！", "~", "…", LITERAL_BACKSLASH_N],
            cleanup="",
        )
        segmenter = self._segmenter(decorate)
        text = "对呀，直接屏蔽掉就好了\n除非某人其实是故意留着的？"
        # 官方只会吃掉末尾问号，真实换行仍留在同一段里。
        self.assertEqual(segmenter.split_text(text), ["对呀，直接屏蔽掉就好了\n除非某人其实是故意留着的"])

    def test_only_llm_result_skips_non_llm_text(self) -> None:
        segmenter = self._segmenter(FakeDecorate(only_llm=True, split_words=["。"]))
        self.assertEqual(segmenter.split_text("你好。世界", is_llm_result=False), ["你好。世界"])
        self.assertEqual(segmenter.split_text("你好。世界", is_llm_result=True), ["你好", "世界"])

    def test_regex_mode_uses_official_pattern(self) -> None:
        decorate = FakeDecorate(mode="regex", regex=r".*?[。？！~…]+|.+$", cleanup="")
        segmenter = self._segmenter(decorate)
        self.assertEqual(segmenter.split_text("你好。世界！"), ["你好。", "世界！"])

    def test_cleanup_rule_strips_configured_chars(self) -> None:
        decorate = FakeDecorate(mode="regex", regex=r".*?[。？！~…]+|.+$", cleanup="。")
        segmenter = self._segmenter(decorate)
        self.assertEqual(segmenter.split_text("你好。世界"), ["你好", "世界"])

    def test_excluded_platform_is_not_split(self) -> None:
        segmenter = self._segmenter(FakeDecorate(split_words=["。"]))
        self.assertEqual(
            segmenter.split_text("你好。世界", platform_name="qq_official_webhook"),
            ["你好。世界"],
        )

    def test_send_order_intervals_and_meme_after_all_text(self) -> None:
        sleeper = RecordingSleeper()
        respond = FakeRespond(interval=0.4)
        segmenter = OfficialSegmenter(
            FakeDecorate(split_words=["。", "\n"]),
            respond,
            sleeper=sleeper,
        )
        context = _RecordingContext()
        sent = _run(
            send_decision_message(
                context,
                umo="group:1",
                text="第一段。第二段",
                resolver=self.store,
                decision=_decision(),
                dry_run=False,
                quote_target=False,
                platform_name="aiocqhttp",
                segmenter=segmenter,
            )
        )
        self.assertTrue(sent)
        self.assertEqual(len(context.calls), 3)
        self.assertEqual(_kinds(context.calls[0]), ["text"])
        self.assertEqual(_kinds(context.calls[1]), ["text"])
        self.assertEqual(_kinds(context.calls[2]), ["image"])
        self.assertEqual(_texts(context.calls[0]), ["第一段"])
        self.assertEqual(_texts(context.calls[1]), ["第二段"])
        self.assertEqual(sleeper.delays, [0.4, 0.4, 0.4])
        self.assertEqual(respond.plain_calls, ["第一段", "第二段"])
        self.assertEqual(respond.image_calls, 1)

    def test_disabled_segment_sends_text_then_meme_without_delay(self) -> None:
        sleeper = RecordingSleeper()
        segmenter = OfficialSegmenter(
            FakeDecorate(enable=False),
            FakeRespond(enable=False),
            sleeper=sleeper,
        )
        context = _RecordingContext()
        _run(
            send_decision_message(
                context,
                umo="group:1",
                text="A\nB",
                resolver=self.store,
                decision=_decision(),
                dry_run=False,
                quote_target=False,
                segmenter=segmenter,
            )
        )
        self.assertEqual(len(context.calls), 2)
        self.assertEqual(_texts(context.calls[0]), ["A\nB"])
        self.assertEqual(_kinds(context.calls[1]), ["image"])
        self.assertEqual(sleeper.delays, [])

    def test_quote_only_on_first_text_segment(self) -> None:
        segmenter = self._segmenter(FakeDecorate(split_words=["。", "\n"]))
        context = _RecordingContext()
        _run(
            send_decision_message(
                context,
                umo="friend:1",
                text="第一段。第二段",
                resolver=self.store,
                decision=_decision(),
                dry_run=False,
                quote_target=True,
                segmenter=segmenter,
            )
        )
        self.assertEqual(_kinds(context.calls[0]), ["reply", "text"])
        self.assertEqual(_kinds(context.calls[1]), ["text"])
        self.assertEqual(_kinds(context.calls[2]), ["image"])

    def test_configured_quote_modes_and_first_success(self):
        segmenter = self._segmenter(FakeDecorate(split_words=["。"]))
        for mode, failures, expected in (
            ("all", set(), [True, True, True]), ("none", set(), [False] * 3),
            ("first", {0}, [True, True, False]),
        ):
            with self.subTest(mode=mode):
                context = _RecordingContext(return_false_on=failures)
                _run(send_decision_message(context, umo="group:1", text="一。二。三", resolver=self.store,
                                           decision=_decision(target_quote_mode=mode), dry_run=False,
                                           segmenter=segmenter))
                self.assertEqual(["reply" in _kinds(c) for c in context.calls[:3]], expected)

    def test_mid_segment_failure_does_not_resend_success_or_meme(self) -> None:
        segmenter = self._segmenter(FakeDecorate(split_words=["。"]))
        context = _RecordingContext(return_false_on={1})
        sent = _run(
            send_decision_message(
                context,
                umo="group:1",
                text="一段。二段。三段",
                resolver=self.store,
                decision=_decision(),
                dry_run=False,
                quote_target=False,
                segmenter=segmenter,
            )
        )
        # 第一段成功后不因后段失败而重发；仍尝试后续文字；不发表情。
        self.assertTrue(sent)
        self.assertEqual([_texts(call) for call in context.calls], [["一段"], ["二段"], ["三段"]])
        self.assertTrue(all("image" not in _kinds(call) for call in context.calls))

    def test_first_segment_failure_does_not_retry_whole_text(self) -> None:
        segmenter = self._segmenter(FakeDecorate(split_words=["。"]))
        context = _RecordingContext(return_false_on={0})
        sent = _run(
            send_decision_message(
                context,
                umo="group:1",
                text="一段。二段",
                resolver=self.store,
                decision=_decision(),
                dry_run=False,
                quote_target=False,
                segmenter=segmenter,
            )
        )
        self.assertTrue(sent)  # 第二段仍发出
        texts = [_texts(call) for call in context.calls]
        self.assertEqual(texts[0], ["一段"])
        self.assertEqual(texts[1], ["二段"])
        self.assertTrue(all("image" not in _kinds(call) for call in context.calls))
        # 没有把整段原文再发一遍
        self.assertFalse(any("一段。二段" in _texts(call) for call in context.calls))

    def test_official_hook_applies_interval_once_and_no_double_image(self) -> None:
        sleeper = RecordingSleeper()
        respond = FakeRespond(interval=0.5)
        segmenter = OfficialSegmenter(FakeDecorate(), respond, sleeper=sleeper)
        event = _FakeEvent(
            umo="group:1",
            chain=[_plain("hello"), _plain("world")],
            decision=_decision(),
            has_send_oper=True,
        )
        context = _RecordingContext()
        sent = _run(
            after_official_text_sent(
                event,
                context=context,
                resolver=self.store,
                dry_run=False,
                segmenter=segmenter,
            )
        )
        self.assertTrue(sent)
        self.assertEqual(len(context.calls), 1)
        self.assertEqual(_kinds(context.calls[0]), ["image"])
        self.assertEqual(sleeper.delays, [0.5])
        sent_again = _run(
            after_official_text_sent(
                event,
                context=context,
                resolver=self.store,
                dry_run=False,
                segmenter=segmenter,
            )
        )
        self.assertFalse(sent_again)
        self.assertEqual(len(context.calls), 1)

    def test_concurrent_sessions_do_not_mix_sends(self) -> None:
        async def _both() -> tuple[_RecordingContext, _RecordingContext]:
            left = _RecordingContext()
            right = _RecordingContext()
            await asyncio.gather(
                send_decision_message(
                    left,
                    umo="group:1",
                    text="甲。乙",
                    resolver=self.store,
                    decision=_decision(meme_id="meme-1"),
                    dry_run=False,
                    quote_target=False,
                    segmenter=self._segmenter(FakeDecorate(split_words=["。"]), FakeRespond(enable=False)),
                ),
                send_decision_message(
                    right,
                    umo="friend:2",
                    text="丙。丁",
                    resolver=self.store,
                    decision=_decision(meme_id="meme-1"),
                    dry_run=False,
                    quote_target=False,
                    segmenter=self._segmenter(FakeDecorate(split_words=["。"]), FakeRespond(enable=False)),
                ),
            )
            return left, right

        left, right = _run(_both())
        self.assertEqual(left.umos, ["group:1", "group:1", "group:1"])
        self.assertEqual(right.umos, ["friend:2", "friend:2", "friend:2"])
        self.assertEqual([_texts(c) for c in left.calls if "text" in _kinds(c)], [["甲"], ["乙"]])
        self.assertEqual([_texts(c) for c in right.calls if "text" in _kinds(c)], [["丙"], ["丁"]])
        self.assertEqual(_kinds(left.calls[-1]), ["image"])
        self.assertEqual(_kinds(right.calls[-1]), ["image"])


if __name__ == "__main__":
    unittest.main()
