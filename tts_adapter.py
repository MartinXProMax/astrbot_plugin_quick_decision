"""对接市场插件「通用 TTS」(CyrilPeng/astrbot_plugin_universal_tts) 的适配器。

通用 TTS 在 on_decorating_result 里把一条模型回复的全部文字换成一条语音，是否转换由它自己的
规则决定：插件总开关、/tts_off、AstrBot「启用 TTS」与会话开关、触发概率、字数上限、跳过规则、
会话绑定的引擎。它没有提供给其他插件调用的接口，所以本适配器借用它的钩子，不另写一套规则：

- 判断要求发语音时，以触发概率 100% 执行它的 tts_hook；其余规则照常生效；
- 判断要求只发文字时，它按自己的概率转出的语音会被改回原文；
- 主动发言不经过官方装饰阶段，这里用一个只带会话和正文的事件调用它的钩子，取回生成的语音；
- 判断给出本轮语气时，把会话引擎换成叠加了语气的副本。

以上依赖它 1.4.x 的方法名（tts_hook、_get_engine_for_session）和引擎属性（style、style_prompt）。
对不上时退回它的默认行为或只发文字，不中断本轮发送。
"""

from __future__ import annotations

import copy
import warnings
from dataclasses import asdict, dataclass
from typing import Any

from astrbot.api import logger
from astrbot.api.message_components import Plain

from .meme_adapter import read_star_version, unwrap_star

try:
    from astrbot.core.message.message_event_result import ResultContentType
except ImportError:  # 测试桩或旧版本
    ResultContentType = None

UNIVERSAL_TTS_STAR_NAME = "astrbot_plugin_universal_tts"
UNAVAILABLE_HINT = (
    "未检测到可用的「通用 TTS」(astrbot_plugin_universal_tts)，回复按文字发送。"
    "需要语音时请在插件市场安装 CyrilPeng/astrbot_plugin_universal_tts。"
)
_LLM_RESULT = getattr(ResultContentType, "LLM_RESULT", "llm_result")
_GENERAL_RESULT = getattr(ResultContentType, "GENERAL_RESULT", "general_result")


def _is_record(component) -> bool:
    return type(component).__name__ == "Record"


def has_record(chain) -> bool:
    return any(_is_record(c) for c in chain or ())


def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return default


def toned_engine(engine, tone: str):
    """复制引擎并叠加本轮语气；引擎不支持语气时原样返回。

    - MiMo V2.5-TTS：style 是一组风格词，追加在原有风格之后，如「温柔 慵懒 开心」；
    - Azure：express-as 只能有一个 style（有 role 属性），本轮语气替换原有 style；
    - MiMo VoiceClone 只有自然语言的 style_prompt，在末尾补一句语气要求。
    """
    tone = " ".join(str(tone or "").split())
    if engine is None or not tone:
        return engine
    style = getattr(engine, "style", None)
    prompt = getattr(engine, "style_prompt", None)
    if not isinstance(style, str) and not isinstance(prompt, str):
        return engine
    try:
        get_client = getattr(engine, "_get_client", None)
        if callable(get_client):
            # 先建好连接再复制，副本复用同一个客户端，不会每句话另开一个。
            get_client()
        toned = copy.copy(engine)
    except Exception:  # noqa: BLE001
        logger.debug("无法复制通用 TTS 引擎，本轮沿用默认语气", exc_info=True)
        return engine
    if isinstance(style, str):
        toned.style = tone if hasattr(engine, "role") else f"{style} {tone}".strip()
    else:
        toned.style_prompt = f"{prompt.strip()}\n用「{tone}」的语气朗读。".strip()
    return toned


class _PluginView:
    """把对通用 TTS 实例的读写原样转过去，只替换本轮要改的两处：

    - force：触发概率按 100% 计，是否转语音已由判断决定；
    - tone：会话引擎换成带语气的副本。
    """

    def __init__(self, instance, tone: str, force: bool) -> None:
        object.__setattr__(self, "_instance", instance)
        object.__setattr__(self, "_tone", tone)
        object.__setattr__(self, "_force", force)

    def __getattr__(self, name):
        return getattr(self._instance, name)

    def __setattr__(self, name, value) -> None:
        setattr(self._instance, name, value)

    @property
    def config(self):
        config = self._instance.config
        return {**config, "tts_probability": 100} if self._force else config

    def _get_engine_for_session(self, session_id):
        return toned_engine(self._instance._get_engine_for_session(session_id), self._tone)


class _DetachedResult:
    """主动发言的正文。只实现通用 TTS 钩子会读写的部分。"""

    def __init__(self, chain, content_type=_LLM_RESULT) -> None:
        self.chain = chain
        self.result_content_type = content_type

    def is_llm_result(self) -> bool:
        return self.result_content_type == _LLM_RESULT

    def set_result_content_type(self, typ):
        self.result_content_type = typ
        return self

    def derive(self, chain):
        return _DetachedResult(list(chain), self.result_content_type)


class _DetachedEvent:
    """主动发言没有入站事件；只提供会话标识和结果，发送仍由快速决策负责。"""

    def __init__(self, umo: str, result: _DetachedResult) -> None:
        self.unified_msg_origin = umo
        self._result = result
        self._extras: dict[str, Any] = {}

    def get_result(self):
        return self._result

    def set_result(self, result) -> None:
        self._result = result

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value) -> None:
        self._extras[key] = value

    def get_platform_name(self) -> str:
        return self.unified_msg_origin.split(":", 1)[0]

    get_platform_id = get_platform_name

    def is_private_chat(self) -> bool:
        return "FriendMessage" in self.unified_msg_origin

    async def send(self, message):  # noqa: ARG002
        raise RuntimeError("主动发言由快速决策发送")


@dataclass
class TTSStatus:
    available: bool
    plugin_loaded: bool
    plugin_version: str | None
    message: str = ""
    engine: str = ""
    runtime_enabled: bool = True
    probability: int = 100
    fallback_to_text: bool = True
    astrbot_tts_enabled: bool = False
    core_provider: str = ""
    dual_output: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class UniversalTTSAdapter:
    def __init__(self, context) -> None:
        self.context = context

    def _registered(self):
        getter = getattr(self.context, "get_registered_star", None)
        if not callable(getter):
            return None
        try:
            return getter(UNIVERSAL_TTS_STAR_NAME)
        except Exception:  # noqa: BLE001
            return None

    def plugin(self):
        """已加载、已启用且钩子对得上的通用 TTS 实例。"""
        registered = self._registered()
        if registered is None or getattr(registered, "activated", True) is False:
            return None
        instance = unwrap_star(registered)
        if instance is None or not callable(getattr(type(instance), "tts_hook", None)):
            return None
        return instance

    def fallback_to_text(self) -> bool:
        """与通用 TTS 的「发送失败时回退为文字」一致。"""
        instance = self.plugin()
        config = getattr(instance, "config", None) or {}
        return _as_bool(config.get("fallback_to_text_on_send_error", True), True)

    def _astrbot_tts_settings(self) -> dict:
        config = getattr(self.context, "astrbot_config", None)
        if config is None:
            getter = getattr(self.context, "get_config", None)
            try:
                config = getter() if callable(getter) else None
            except Exception:  # noqa: BLE001
                config = None
        try:
            settings = (config or {}).get("provider_tts_settings") or {}
        except Exception:  # noqa: BLE001
            return {}
        return settings if isinstance(settings, dict) else dict(settings)

    def _core_provider(self, settings: dict) -> str:
        """AstrBot 内置 TTS 当前选中的提供商；没有选时为空。"""
        provider_id = str(settings.get("provider_id") or "").strip()
        if provider_id:
            return provider_id
        getter = getattr(self.context, "get_using_tts_provider", None)
        if not callable(getter):
            return ""
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                provider = getter()
        except Exception:  # noqa: BLE001
            return ""
        if provider is None:
            return ""
        try:
            return str(provider.meta().id)
        except Exception:  # noqa: BLE001
            return type(provider).__name__

    def status(self) -> TTSStatus:
        registered = self._registered()
        version = read_star_version(registered)
        settings = self._astrbot_tts_settings()
        common = dict(
            astrbot_tts_enabled=_as_bool(settings.get("enable"), False),
            core_provider=self._core_provider(settings),
            dual_output=_as_bool(settings.get("dual_output"), False),
        )
        instance = self.plugin()
        if instance is None:
            loaded = registered is not None
            message = "通用 TTS 已安装但未启用，或版本与适配器不兼容" if loaded else UNAVAILABLE_HINT
            return TTSStatus(False, loaded, version, message, **common)
        config = getattr(instance, "config", None) or {}
        engine = getattr(getattr(instance, "_engine", None), "instance_name", "") or ""
        try:
            probability = int(config.get("tts_probability", 100))
        except (TypeError, ValueError):
            probability = 100
        status = TTSStatus(
            available=True,
            plugin_loaded=True,
            plugin_version=version,
            engine=engine,
            runtime_enabled=getattr(instance, "_runtime_enabled", True) is not False,
            probability=probability,
            fallback_to_text=_as_bool(config.get("fallback_to_text_on_send_error", True), True),
            **common,
        )
        if not _as_bool(config.get("enable", True), True):
            status.available, status.message = False, "通用 TTS 的插件总开关已关闭"
        elif not engine:
            status.available, status.message = False, "通用 TTS 还没有可用的引擎，请先在它的页面配置"
        elif not status.astrbot_tts_enabled:
            status.available = False
            status.message = "AstrBot 的「启用 TTS」未打开，通用 TTS 不会合成；打开它，TTS 提供商留空即可"
        elif not status.runtime_enabled:
            status.message = "通用 TTS 已用 /tts_off 暂停，恢复前回复按文字发送"
        return status

    async def _run_hook(self, instance, event, tone: str, force: bool) -> None:
        target = _PluginView(instance, tone, force) if tone or force else instance
        await type(instance).tts_hook(target, event)

    async def voice_event(self, event, tone: str = "", *, force: bool = False) -> bool:
        """被动回复：赶在通用 TTS 自己的钩子之前，按本轮判断替它合成一次。

        它按规则跳过时会把结果标成普通消息，随后它自己的钩子不会再合成；合成失败则交给它的钩子重试。
        """
        instance = self.plugin()
        if instance is None or not (tone or force):
            return False
        try:
            await self._run_hook(instance, event, tone, force)
        except Exception:  # noqa: BLE001
            logger.exception("调用通用 TTS 合成语音失败，交还给通用 TTS 默认处理")
            return False
        result = event.get_result()
        return has_record(getattr(result, "chain", None))

    def settle(self, result, *, voice: bool, original) -> None:
        """所有装饰钩子跑完后落实本轮判断，消除通用 TTS 按自己规则留下的影响。

        - 判断只发文字、它却按概率转了语音：把语音换回原来的文字组件；
        - 它跳过时会把结果改标为普通消息，这会关掉 AstrBot 的分段回复，这里改回模型结果。
          但内置 TTS 选了提供商时，链上还有文字就保持普通消息，免得内置 TTS 再转语音。
        """
        chain = list(getattr(result, "chain", None) or ())
        if not voice and has_record(chain):
            texts = [c for c in original if isinstance(c, Plain)]
            rebuilt, restored = [], False
            for comp in chain:
                if _is_record(comp) and not any(comp is item for item in original):
                    if not restored:
                        rebuilt.extend(texts)
                        restored = True
                    continue
                rebuilt.append(comp)
            result.chain = chain = rebuilt
            logger.info("通用 TTS 按自己的概率转了语音，本轮判断为只发文字，已改回文字")
        setter = getattr(result, "set_result_content_type", None)
        if not callable(setter):
            return
        has_text = any(isinstance(c, Plain) and (c.text or "").strip() for c in chain)
        if has_text and self._core_provider(self._astrbot_tts_settings()):
            if result.is_llm_result():
                setter(_GENERAL_RESULT)
        elif not result.is_llm_result():
            setter(_LLM_RESULT)

    async def voice_record(self, umo: str, text: str, *, tone: str = "", force: bool = False):
        """主动发言：按通用 TTS 的规则把整段正文转成一条语音；不转或失败时返回 None。

        force 时不看它的触发概率，是否转语音已由判断决定。
        """
        instance = self.plugin()
        if instance is None or not (text or "").strip():
            return None
        event = _DetachedEvent(umo, _DetachedResult([Plain(text)]))
        try:
            await self._run_hook(instance, event, tone, force)
        except Exception:  # noqa: BLE001
            logger.exception("调用通用 TTS 合成主动发言语音失败，改发文字：%s", umo)
            return None
        chain = getattr(event.get_result(), "chain", None) or []
        return next((c for c in chain if _is_record(c)), None)
