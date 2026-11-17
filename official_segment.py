"""复用 AstrBot 官方 segmented_reply 的分段与间隔，不另写一套拆分算法。

通过初始化官方 ResultDecorateStage / RespondStage，调用其已有方法：
- 文字拆分：enable / only_llm_result / 平台排除 / words_count_threshold 比较、
  split_mode=words 时的 ResultDecorateStage._split_text_by_words，或 regex 配置；
  以及 content_cleanup_rule。
- 发送间隔：RespondStage.is_seg_reply_required 与 _calc_comp_interval。

边界：
- 不跑完整 decorate/respond 流水线（钩子、TTS、回复前缀、内容安全、event.send），
  因为自生成路径已经 stop_event，再进官方阶段会重复触发本插件钩子。
- 官方 words 模式会去掉匹配到的分隔符；这是官方行为，本模块如实复用。
- 不改写用户的 segmented_reply 配置或人格。若配置里的 `\\n` 是两个字符
  （反斜杠和 n）而不是换行符，官方 words 不会按真实换行拆分。
- 本插件自生成走非流式 llm_generate；官方流式结果本身也不会在 decorate 里拆分。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from astrbot.api import logger

# 与官方 ResultDecorateStage.process / RespondStage.is_seg_reply_required 保持一致。
_OFFICIAL_NO_SEGMENT_PLATFORMS = (
    "qq_official_webhook",
    "weixin_official_account",
    "dingtalk",
)

_stage_cache: dict[str, tuple[Any, Any]] = {}


class NoOpSegmenter:
    """官方阶段不可用或未启用时：不拆分、不插入间隔。"""

    def __init__(self, sleeper=None) -> None:
        self._sleep = sleeper or asyncio.sleep

    def split_text(
        self,
        text: str,
        *,
        is_llm_result: bool = True,
        platform_name: str = "",
    ) -> list[str]:
        del is_llm_result, platform_name
        text = text or ""
        return [text] if text.strip() else []

    def should_delay(self, *, is_llm_result: bool = True, platform_name: str = "") -> bool:
        del is_llm_result, platform_name
        return False

    async def interval_for_plain(self, text: str) -> float:
        del text
        return 0.0

    async def interval_for_image(self) -> float:
        return 0.0

    async def sleep(self, seconds: float) -> None:
        if seconds:
            await self._sleep(seconds)


class OfficialSegmenter:
    """薄封装：把官方 decorate / respond 阶段的分段与间隔暴露给自生成发送链。"""

    def __init__(self, decorate, respond, *, sleeper=None) -> None:
        self._decorate = decorate
        self._respond = respond
        self._sleep = sleeper or asyncio.sleep

    @classmethod
    def noop(cls, sleeper=None) -> NoOpSegmenter:
        return NoOpSegmenter(sleeper=sleeper)

    @classmethod
    async def from_context(cls, context, *, umo: str | None = None, sleeper=None):
        config = _load_config(context, umo)
        if config is None:
            logger.debug("无法读取官方配置，自生成将按不分段发送")
            return cls.noop(sleeper=sleeper)
        try:
            decorate, respond = await _get_or_init_stages(config)
        except Exception:
            logger.exception("初始化官方分段阶段失败，自生成将按不分段发送")
            return cls.noop(sleeper=sleeper)
        return cls(decorate, respond, sleeper=sleeper)

    def split_text(
        self,
        text: str,
        *,
        is_llm_result: bool = True,
        platform_name: str = "",
    ) -> list[str]:
        """按官方 decorate 对单段 Plain 的规则拆分；不拆则原样返回一条。"""
        text = text or ""
        if not text.strip():
            return []
        decorate = self._decorate
        if not getattr(decorate, "enable_segmented_reply", False):
            return [text]
        if platform_name in _OFFICIAL_NO_SEGMENT_PLATFORMS:
            return [text]
        only_llm = getattr(decorate, "only_llm_result", True)
        if only_llm and not is_llm_result:
            return [text]
        if len(text) > int(getattr(decorate, "words_count_threshold", 0) or 0):
            return [text]
        split_mode = getattr(decorate, "split_mode", "regex")
        if split_mode == "words":
            split_response = decorate._split_text_by_words(text)
        else:
            split_response = _regex_split(decorate, text)
        if not split_response:
            return [text]
        cleanup = getattr(decorate, "content_cleanup_rule", None)
        result: list[str] = []
        for seg in split_response:
            if cleanup:
                try:
                    seg = re.sub(cleanup, "", seg)
                except re.error:
                    logger.exception("官方 content_cleanup_rule 无效，本次不再过滤")
                    cleanup = None
                    try:
                        decorate.content_cleanup_rule = None
                    except Exception:
                        pass
            seg = (seg or "").strip()
            if seg:
                result.append(seg)
        return result or [text]

    def should_delay(self, *, is_llm_result: bool = True, platform_name: str = "") -> bool:
        respond = self._respond
        probe = _ProbeEvent(platform_name=platform_name, is_model_result=is_llm_result)
        checker = getattr(respond, "is_seg_reply_required", None)
        if callable(checker):
            try:
                return bool(checker(probe))
            except Exception:
                logger.exception("调用官方 is_seg_reply_required 失败")
                return False
        return bool(getattr(respond, "enable_seg", False))

    async def interval_for_plain(self, text: str) -> float:
        calc = getattr(self._respond, "_calc_comp_interval", None)
        if not callable(calc):
            return 0.0
        return float(await calc(_make_plain(text)))

    async def interval_for_image(self) -> float:
        calc = getattr(self._respond, "_calc_comp_interval", None)
        if not callable(calc):
            return 0.0
        return float(await calc(_make_image()))

    async def sleep(self, seconds: float) -> None:
        if seconds:
            await self._sleep(seconds)


class _ProbeResult:
    def __init__(self, is_model_result: bool) -> None:
        self._is_model_result = is_model_result

    def is_model_result(self) -> bool:
        return self._is_model_result


class _ProbeEvent:
    def __init__(self, *, platform_name: str, is_model_result: bool) -> None:
        self._platform_name = platform_name
        self._result = _ProbeResult(is_model_result)

    def get_platform_name(self) -> str:
        return self._platform_name

    def get_result(self):
        return self._result


def _load_config(context, umo: str | None):
    getter = getattr(context, "get_config", None)
    if callable(getter):
        try:
            if umo:
                config = getter(umo=umo)
                if config:
                    return config
        except TypeError:
            pass
        except Exception:
            logger.exception("按会话读取官方配置失败，将回退到全局配置")
        try:
            return getter()
        except Exception:
            logger.exception("读取官方配置失败")
            return None
    return getattr(context, "astrbot_config", None)


def _cache_key(config) -> str:
    try:
        platform = config["platform_settings"]
        payload = {
            "segmented_reply": _as_mapping(platform["segmented_reply"]),
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return f"id:{id(config)}"


def _as_mapping(value):
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    try:
        return dict(value)
    except Exception:
        return {"_repr": str(value)}


async def _get_or_init_stages(config) -> tuple[Any, Any]:
    key = _cache_key(config)
    cached = _stage_cache.get(key)
    if cached is not None:
        return cached
    decorate, respond = await _init_official_stages(config)
    _stage_cache[key] = (decorate, respond)
    return decorate, respond


async def _init_official_stages(config) -> tuple[Any, Any]:
    from astrbot.core.pipeline.context import PipelineContext
    from astrbot.core.pipeline.respond.stage import RespondStage
    from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage

    class _DummyPluginManager:
        pass

    ctx = PipelineContext(
        astrbot_config=config,
        plugin_manager=_DummyPluginManager(),
        astrbot_config_id="quick_decision_segment",
    )
    decorate = ResultDecorateStage()
    respond = RespondStage()
    await decorate.initialize(ctx)
    await respond.initialize(ctx)
    return decorate, respond


def _regex_split(decorate, text: str) -> list[str]:
    """使用官方 decorate 上的 regex 配置拆分，失败时用官方默认正则。"""
    pattern = getattr(decorate, "regex", None) or r".*?[。？！~…]+|.+$"
    flags = re.DOTALL | re.MULTILINE
    try:
        return re.findall(pattern, text, flags)
    except re.error:
        logger.exception("官方 segmented-reply 正则无效，改用官方默认正则")
        return re.findall(r".*?[。？！~…]+|.+$", text, flags)


def _make_plain(text: str):
    try:
        from astrbot.core.message.components import Plain

        return Plain(text)
    except Exception:
        return type("Plain", (), {"text": text})()


def _make_image():
    try:
        from astrbot.core.message.components import Image

        if hasattr(Image, "fromFileSystem"):
            try:
                return Image.fromFileSystem("/dev/null")
            except Exception:
                pass
        try:
            return Image(file="/dev/null")
        except Exception:
            return Image()
    except Exception:
        return type("Image", (), {"file": "/dev/null"})()


def platform_name_from_event(event) -> str:
    if event is None:
        return ""
    getter = getattr(event, "get_platform_name", None)
    if callable(getter):
        try:
            return str(getter() or "")
        except Exception:
            return ""
    return str(getattr(event, "platform_name", "") or "")


def clear_stage_cache() -> None:
    """测试用：清空官方阶段缓存。"""
    _stage_cache.clear()
