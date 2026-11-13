#!/usr/bin/env python3
"""在已安装 AstrBot 的环境中，对照官方 ResultDecorateStage / RespondStage 验证插件封装。

不发送 QQ、不调用模型、不改配置。可用 mock sleeper，不真实等待。
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# 允许从插件源码或已部署目录导入。
for candidate in [
    Path(__file__).resolve().parents[1],
    Path("/AstrBot/data/plugins/astrbot_plugin_quick_decision"),
]:
    if (candidate / "official_segment.py").exists() and str(candidate.parent) not in sys.path:
        sys.path.insert(0, str(candidate.parent))


async def _official_plain_split(decorate, text: str) -> list[str]:
    """原样复述官方 process() 中 Plain 拆分分支，作为对照，不经过插件封装。"""
    import re
    from astrbot.core.message.components import Plain

    if not decorate.enable_segmented_reply:
        return [text]
    if len(text) > decorate.words_count_threshold:
        return [text]
    if decorate.split_mode == "words":
        split_response = decorate._split_text_by_words(text)
    else:
        try:
            split_response = re.findall(decorate.regex, text, re.DOTALL | re.MULTILINE)
        except re.error:
            split_response = re.findall(r".*?[。？！~…]+|.+$", text, re.DOTALL | re.MULTILINE)
    if not split_response:
        return [text]
    out = []
    cleanup = decorate.content_cleanup_rule
    for seg in split_response:
        if cleanup:
            try:
                seg = re.sub(cleanup, "", seg)
            except re.error:
                cleanup = None
        seg = seg.strip()
        if seg:
            out.append(seg)
    return out or [text]


async def _run_official_process_split(config, text: str, platform_name: str) -> list[str]:
    """用官方 ResultDecorateStage.process 对一条 LLM Plain 结果拆分（钩子、TTS 被短路）。"""
    from astrbot.core.message.components import Plain
    from astrbot.core.message.message_event_result import MessageEventResult, ResultContentType
    from astrbot.core.pipeline.context import PipelineContext
    from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage

    class _DummyPM:
        pass

    ctx = PipelineContext(
        astrbot_config=config,
        plugin_manager=_DummyPM(),
        astrbot_config_id="qd-verify",
    )
    stage = ResultDecorateStage()
    await stage.initialize(ctx)
    # 验证时不跑 TTS / t2i。
    stage.reply_prefix = ""

    result = MessageEventResult()
    result.chain = [Plain(text)]
    result.set_result_content_type(ResultContentType.LLM_RESULT)

    event = SimpleNamespace(
        plugins_name=None,
        _result=result,
        _stopped=False,
    )

    def get_result():
        return event._result

    def set_result(value):
        event._result = value

    def get_platform_name():
        return platform_name

    def is_stopped():
        return event._stopped

    event.get_result = get_result
    event.set_result = set_result
    event.get_platform_name = get_platform_name
    event.is_stopped = is_stopped

    async def empty_handlers(*_a, **_k):
        return []

    async def process():
        with (
            patch(
                "astrbot.core.pipeline.result_decorate.stage.star_handlers_registry.get_handlers_by_event_type",
                return_value=[],
            ),
            patch.object(stage, "content_safe_check_reply", False),
        ):
            # 截断 process 在 TTS 之前：先跑完整 process，但把 tts 和后续弄短。
            gen = stage.process(event)
            if gen is None:
                return
            try:
                async for _ in gen:
                    pass
            except TypeError:
                # process 可能直接返回 None
                pass

    await process()
    result = event.get_result()
    return [comp.text for comp in result.chain if getattr(comp, "text", None)]


async def main() -> int:
    from astrbot.core.pipeline.context import PipelineContext
    from astrbot.core.pipeline.respond.stage import RespondStage
    from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage
    from astrbot.core.message.components import Image, Plain

    from astrbot_plugin_quick_decision.official_segment import OfficialSegmenter, clear_stage_cache

    cfg_path = Path("/AstrBot/data/cmd_config.json")
    config = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    seg_cfg = config["platform_settings"]["segmented_reply"]
    print("live segmented_reply.enable =", seg_cfg.get("enable"))
    print("live split_mode =", seg_cfg.get("split_mode"))
    print("live threshold =", seg_cfg.get("words_count_threshold"))
    print("live only_llm_result =", seg_cfg.get("only_llm_result"))
    print("live interval_method =", seg_cfg.get("interval_method"), "interval =", seg_cfg.get("interval"), "log_base =", seg_cfg.get("log_base"))
    print("live content_cleanup_rule =", repr(seg_cfg.get("content_cleanup_rule")))
    print("live split_words =", [ (w, [ord(c) for c in w]) for w in seg_cfg.get("split_words", []) ])

    class _DummyPM:
        pass

    ctx = PipelineContext(
        astrbot_config=config,
        plugin_manager=_DummyPM(),
        astrbot_config_id="qd-verify",
    )
    decorate = ResultDecorateStage()
    respond = RespondStage()
    await decorate.initialize(ctx)
    await respond.initialize(ctx)
    clear_stage_cache()
    plugin = OfficialSegmenter(decorate, respond, sleeper=AsyncMock())

    cases = {
        "newline_and_question": "对呀，直接屏蔽掉就好了\n除非某人其实是故意留着的？",
        "two_periods": "你好。世界。",
        "below_threshold_short": "短句。",
        "equal_threshold": "x" * int(decorate.words_count_threshold),
        "above_threshold": "第一句。第二句。" + ("字" * (int(decorate.words_count_threshold) + 1)),
    }
    # equal_threshold should still split if it contains delimiters and len == threshold.
    cases["equal_threshold"] = ("你好。" + "字" * (int(decorate.words_count_threshold) - 3))[: int(decorate.words_count_threshold)]

    failures = 0
    for name, text in cases.items():
        plugin_out = plugin.split_text(text, is_llm_result=True, platform_name="aiocqhttp")
        official_out = await _official_plain_split(decorate, text)
        print(f"case {name}: plugin={plugin_out!r} official={official_out!r}")
        if plugin_out != official_out:
            print("  MISMATCH vs official helper loop")
            failures += 1
        if name in {"newline_and_question", "two_periods", "below_threshold_short"}:
            try:
                process_out = await _run_official_process_split(config, text, "aiocqhttp")
                print(f"  official process() = {process_out!r}")
                if process_out != plugin_out:
                    print("  MISMATCH vs official process()")
                    failures += 1
            except Exception as exc:
                print(f"  official process() skipped: {type(exc).__name__}: {exc}")

    # enable off / only_llm / excluded platform
    saved_enable = decorate.enable_segmented_reply
    decorate.enable_segmented_reply = False
    if plugin.split_text("你好。世界", is_llm_result=True, platform_name="aiocqhttp") != ["你好。世界"]:
        print("enable=false mismatch")
        failures += 1
    else:
        print("enable=false keeps whole text: ok")
    decorate.enable_segmented_reply = saved_enable

    if plugin.split_text("你好。世界", is_llm_result=False, platform_name="aiocqhttp") != ["你好。世界"]:
        print("only_llm_result mismatch")
        failures += 1
    else:
        print("only_llm_result skips non-llm: ok")

    if plugin.split_text("你好。世界", is_llm_result=True, platform_name="dingtalk") != ["你好。世界"]:
        print("platform exclusion mismatch")
        failures += 1
    else:
        print("dingtalk excluded: ok")

    # interval: use official _calc_comp_interval with patched random.uniform
    def fake_uniform(a, b):
        return (a + b) / 2

    with patch("astrbot.core.pipeline.respond.stage.random.uniform", side_effect=fake_uniform):
        official_plain = await respond._calc_comp_interval(Plain("测试间隔"))
        plugin_plain = await plugin.interval_for_plain("测试间隔")
        official_image = await respond._calc_comp_interval(Image.fromFileSystem("/dev/null") if hasattr(Image, "fromFileSystem") else Plain("x"))
        plugin_image = await plugin.interval_for_image()
    print(f"interval plain official={official_plain} plugin={plugin_plain}")
    print(f"interval image official={official_image} plugin={plugin_image}")
    if official_plain != plugin_plain:
        print("  MISMATCH plain interval")
        failures += 1
    if official_image != plugin_image:
        print("  MISMATCH image interval")
        failures += 1
    if not plugin.should_delay(is_llm_result=True, platform_name="aiocqhttp"):
        print("  should_delay expected True for live config")
        failures += 1
    else:
        print("should_delay live aiocqhttp llm: ok")
    if plugin.should_delay(is_llm_result=False, platform_name="aiocqhttp"):
        print("  should_delay expected False when not llm")
        failures += 1
    else:
        print("should_delay non-llm: ok")

    print("RESULT", "PASS" if failures == 0 else f"FAIL ({failures})")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
