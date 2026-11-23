"""WebUI 可视化视图使用的后端接口。"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from dataclasses import fields

from astrbot.api import logger
from astrbot.api.provider import DecisionBooleanAnswer, DecisionBooleanQuestion, DecisionProvider, ProviderType
from astrbot.api.web import error_response, json_response, request

from .candidate_sources import CATALOGS, example_catalogs, source_question
from .config_util import (
    EVENTS,
    OPTION_EFFECTS,
    PluginSettings,
    default_decision_questions,
    default_questions,
    load_questions,
)
from .decision_engine import ask_decision, evaluate_post_reply, preview_workflow
from .decision_tools import TOOLS
from .models import QUESTION_ROLES, BufferedMessage
from .workflow_actions import ACTIONS, SOURCES, WORKFLOW_EVENTS, migrate_actions

PLUGIN_NAME = "astrbot_plugin_quick_decision"


def _needs_candidate_editor(settings):
    return settings.workflow_version == 6 or any(source_question(q.candidate_source) for q in settings.decision_questions)


class WebApiMixin:
    """由插件主类混入，注册并处理可视化视图 API。"""

    def register_plugin_web_apis(self) -> None:
        routes = [
            (f"/{PLUGIN_NAME}/logs", self.web_list_logs, ["GET"], "最近决策日志"),
            (f"/{PLUGIN_NAME}/decision-replay", self.web_decision_replay, ["POST"], "用当前草稿重放决策"),
            (f"/{PLUGIN_NAME}/test-provider", self.web_test_decision_provider, ["POST"], "测试判断提供商"),
            (f"/{PLUGIN_NAME}/decision-preview", self.web_decision_preview, ["POST"], "预览判断请求"),
            (f"/{PLUGIN_NAME}/status", self.web_status, ["GET"], "插件状态"),
            (
                f"/{PLUGIN_NAME}/decision-config",
                self.web_decision_config,
                ["GET", "POST"],
                "读取或保存判断配置",
            ),
        ]
        for route, handler, methods, desc in routes:
            self.context.register_web_api(route, handler, methods, desc)

    async def web_list_logs(self):
        limit = max(1, min(request.query.get("limit", 30, type=int), self.settings.decision_log_limit))
        umo = request.query.get("umo") or ""
        logs = self.sessions.recent_logs(limit=self.settings.decision_log_limit)
        names = {}
        for item in logs:
            runtime = self.sessions.get(item.get("umo", ""))
            name = (runtime.display_name if runtime else "") or item.get("session_name") or ""
            key = item.get("umo", "")
            if not names.get(key):
                names[key] = name
        for item in logs:
            item["session_name"] = names.get(item.get("umo", ""), "")
        return json_response({
            "logs": [item for item in logs if not umo or item.get("umo") == umo][:limit],
            "sessions": [{"umo": key, "name": name} for key, name in names.items() if key],
        })

    async def _decision_provider_status(self):
        provider_id = self.settings.decision_provider_id
        manager = getattr(getattr(self.context, "decision", None), "provider_manager", None)
        if manager is None:
            return {"state": "unknown", "id": provider_id, "message": "无法读取判断提供商，请点击测试"}
        try:
            provider = (await manager.get_provider_by_id(provider_id) if provider_id
                        else await manager.get_using_provider_async(ProviderType.DECISION))
            if not isinstance(provider, DecisionProvider):
                return {"state": "missing", "id": provider_id, "message": "未配置有效的判断提供商"}
            return {"state": "configured", "id": provider.meta().id, "message": "判断提供商已配置"}
        except Exception:  # noqa: BLE001
            return {"state": "unknown", "id": provider_id, "message": "读取判断提供商失败，请点击测试"}

    async def web_status(self):
        settings = self.settings
        adapter_status = self.meme_adapter.status().to_dict()
        recent = [item for item in self.sessions.recent_logs(limit=settings.decision_log_limit)
                  if item.get("error") or item.get("batches") or (item.get("post_decision") or {}).get("batches")][:50]
        failures = sum(bool(item.get("error") or item.get("degraded")
                            or (item.get("post_decision") or {}).get("error")) for item in recent)
        return json_response(
            {
                "enabled": settings.enabled,
                "dry_run": settings.dry_run,
                "decision_provider_id": settings.decision_provider_id,
                "decision_provider": await self._decision_provider_status(),
                "provider_required": any(q.enabled and (q.decision_mode or settings.decision_mode) == "model"
                                         for q in settings.decision_questions),
                "provider_test": getattr(self, "_provider_test", None)
                if getattr(self, "_provider_test_id", None) == settings.decision_provider_id else None,
                "recent_log_count": len(recent),
                "recent_failure_rate": failures / len(recent) if recent else 0,
                "meme_count": adapter_status.get("candidate_count", 0),
                "meme_adapter": adapter_status,
            }
        )

    async def web_test_decision_provider(self):
        settings = self.settings
        started = time.monotonic()
        try:
            question = DecisionBooleanQuestion("请回答是。这里只用于测试判断提供商是否可用。")
            answers = await asyncio.wait_for(self.context.decision.ask(
                {"health": question}, state={"recent_messages": []},
                **({"provider_id": settings.decision_provider_id} if settings.decision_provider_id else {}),
            ), timeout=30)
            answer = answers.get("health") if isinstance(answers, Mapping) else None
            if not isinstance(answer, DecisionBooleanAnswer):
                raise ValueError("判断提供商没有返回有效的是否题答案")
            report = {
                "ok": True,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "provider_id": settings.decision_provider_id or "默认",
                "probability": answer.probability,
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("测试判断提供商失败")
            report = {
                "ok": False,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "provider_id": settings.decision_provider_id or "默认",
                "error": str(exc) or type(exc).__name__,
            }
        self._provider_test = report
        self._provider_test_id = settings.decision_provider_id
        return json_response(report)

    async def web_decision_replay(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        item = self.sessions.find_log(str(payload.get("round_id") or ""))
        if not item:
            return error_response("找不到这条决策日志", status_code=404)
        snapshot = item.get("replay_context")
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("messages"), list):
            return error_response("这条日志未保存判断上下文，无法重放", status_code=400)
        scene = item.get("scene")
        if scene not in ("group_passive", "group_proactive", "private_passive", "private_proactive"):
            return error_response("日志场景无效，无法重放", status_code=400)
        mode = payload.get("decision_mode", self.settings.decision_mode)
        version = payload.get("version", 8)
        edited = payload.get("decision_questions")
        if mode not in ("model", "random") or type(version) is not int or version not in (6, 7, 8) or not isinstance(edited, list):
            return error_response("当前草稿格式无效，请刷新配置后重试", status_code=400)
        questions, error = load_questions(
            {"version": 6 if version == 8 else 5 if version == 7 else 4, "questions": edited},
            strict=True,
        )
        if error:
            return error_response(error, status_code=400)
        settings = PluginSettings(dict(self.config))
        settings.decision_mode = mode
        settings.decision_custom_context = str(payload.get("decision_custom_context") or "").strip()
        settings.decision_questions = questions
        settings.dry_run = False
        try:
            message_fields = {f.name for f in fields(BufferedMessage)}
            messages = [BufferedMessage(**{k: v for k, v in m.items() if k in message_fields})
                        for m in snapshot["messages"]]
        except (TypeError, AttributeError):
            return error_response("日志上下文格式无效", status_code=400)
        if not hasattr(self, "_replay_lock"):
            self._replay_lock = asyncio.Lock()
        if self._replay_lock.locked():
            return error_response("已有一次重放正在运行，请稍后重试", status_code=429)
        async with self._replay_lock:
            try:
                async with asyncio.timeout(60):
                    result = await ask_decision(
                        self.context, settings=settings, scene=scene, is_private=scene.startswith("private"),
                        messages=messages, at_or_quoted=bool(snapshot.get("at_or_quoted")),
                        memes=self._meme_candidates(settings), umo=str(item.get("umo") or ""),
                        last_activity=snapshot.get("last_activity"),
                        trigger_event=item.get("trigger_event") or ("silence_elapsed" if scene.endswith("_proactive") else "message_received"),
                        timer_roots=set(snapshot["timer_roots"]) if snapshot.get("timer_roots") is not None else None,
                        evaluation_time=snapshot.get("evaluated_at"),
                    )
                    post = None
                    if result.post_plan and item.get("reply_text"):
                        post = await evaluate_post_reply(self.context, result.post_plan, item["reply_text"], result.answers)
            except TimeoutError:
                return error_response("重放超时，请检查判断提供商后重试", status_code=504)
        return json_response({
            "old": {
                "answers": item.get("answers") or item.get("custom_answers") or {},
                "raw": item.get("raw") or {},
                "action": item.get("action"),
                "error": item.get("error"),
                "duration_ms": item.get("duration_ms"),
                "post_decision": item.get("post_decision"),
            },
            "new": {
                "answers": result.answers or result.custom_answers,
                "raw": result.raw,
                "action": result.action,
                "error": result.error,
                "duration_ms": result.duration_ms,
                "decision_mode": result.decision_mode,
                "post_decision": post,
            },
            "message_count": len(messages),
        })

    def _decision_config_payload(self, warning: str | None = None, *, version=6) -> dict:
        settings = self.settings
        questions = settings.decision_questions
        referenced = version == 8 or _needs_candidate_editor(settings)
        defaults = default_questions() if referenced else default_questions(default_decision_questions())
        payload = {
            "decision_mode": settings.decision_mode,
            "decision_custom_context": settings.decision_custom_context,
            "version": 6,
            "events": EVENTS,
            "effects": OPTION_EFFECTS,
            "default_questions": [q.to_dict() for q in defaults],
            "roles": QUESTION_ROLES,
            "tools": TOOLS,
            "decision_questions": [item.to_dict() for item in questions],
            "warning": warning
            if warning is not None
            else settings.decision_questions_warning,
        }
        if version in (7, 8) or settings.workflow_version in (5, 6) or referenced:
            questions, migration_warning = migrate_actions(questions)
            defaults, _ = migrate_actions(defaults)
            for obsolete in ("roles", "effects", "tools"):
                payload.pop(obsolete)
            payload.update(version=8 if referenced else 7, workflow_version=6 if referenced else 5,
                           stored_version=settings.workflow_version,
                           events=WORKFLOW_EVENTS, actions=ACTIONS,
                           candidate_sources=SOURCES, candidate_catalogs=CATALOGS,
                           candidate_examples=example_catalogs(), decision_questions=[q.to_dict() for q in questions],
                           default_questions=[q.to_dict() for q in defaults],
                           warning="；".join(w for w in (payload["warning"], migration_warning) if w) or None)
        return payload

    async def web_decision_preview(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict) or payload.get("scene") not in (
            "group_passive", "group_proactive", "private_passive", "private_proactive"
        ):
            return error_response("请选择预览场景", status_code=400)
        choices = payload.get("answers", {})
        if not isinstance(choices, dict) or not all(isinstance(v, str) for v in choices.values()):
            return error_response("示例答案格式无效", status_code=400)
        mode = payload.get("decision_mode", "model")
        if mode not in ("model", "random"):
            return error_response("决策模式无效", status_code=400)
        settings = PluginSettings({"decision_mode": mode})
        version = payload.get("version", 6)
        if version not in (6, 7, 8):
            return error_response("预览配置版本无效", status_code=400)
        trigger_event = payload.get("trigger_event", "")
        allowed_events = ("silence_elapsed", "timer_elapsed") if payload["scene"].endswith("_proactive") else ("message_received",)
        if not isinstance(trigger_event, str) or (trigger_event and trigger_event not in allowed_events):
            return error_response("触发事件与预览场景不匹配", status_code=400)
        settings.decision_questions, error = load_questions(
            {"version": 6 if version == 8 else 5 if version == 7 else 4, "questions": payload.get("decision_questions")}, strict=True)
        if error:
            return error_response(error, status_code=400)
        return json_response(preview_workflow(settings, payload["scene"], choices, trigger_event=trigger_event))

    async def web_decision_config(self):
        if request.method == "GET":
            version = getattr(request, "query", {}).get("version")
            return json_response(self._decision_config_payload(version=8 if str(version) == "8" else 7 if str(version) == "7" else 6))

        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        settings = self.settings
        mode = payload.get("decision_mode", settings.decision_mode)
        if mode not in ("model", "random"):
            return error_response("请选择模型判断或纯随机", status_code=400)
        version = payload.get("version")
        if type(version) is not int or version not in (2, 3, 4, 5, 6, 7, 8):
            return error_response("无法识别的编辑器版本，请刷新页面", status_code=400)
        if version < 6:
            return error_response("当前判断使用生效条件，请刷新编辑页后再保存", status_code=409)
        if version < 8 and _needs_candidate_editor(settings):
            return error_response("当前配置使用候选来源引用，请刷新编辑页后再保存", status_code=409)
        if version == 6 and settings.workflow_version == 5:
            return error_response("当前配置使用选项动作，需要支持版本 7 的编辑器", status_code=409)
        custom_context = str(payload.get("decision_custom_context") or "").strip()
        edited = payload.get("decision_questions")
        if version in (7, 8) and isinstance(edited, list) and all(isinstance(q, dict) for q in edited):
            old_ids = {q.id for q in settings.decision_questions}
            deleted = old_ids.difference(q["id"] for q in edited if isinstance(q.get("id"), str))
            if deleted:
                for q in edited:
                    if isinstance(q.get("triggers"), list):
                        q["triggers"] = [t for t in q["triggers"] if not (
                            isinstance(t, dict) and isinstance(t.get("question"), str) and t["question"] in deleted)]
        questions, error = load_questions(
            {
                "version": 6 if version == 8 else 5 if version == 7 else 4,
                "questions": edited,
            },
            strict=True,
        )
        if error:
            return error_response(error, status_code=400)

        config = getattr(self, "config", None)
        if config is None or not hasattr(config, "save_config"):
            return error_response("当前环境无法写入官方插件配置", status_code=500)
        changes = {
            "decision_mode": mode,
            "decision_custom_context": custom_context,
            "decision_questions": json.dumps(
                {"version": 6 if version == 8 else 5 if version == 7 else 4, "questions": [q.to_dict() for q in questions]},
                ensure_ascii=False, indent=2,
            ),
        }
        if not hasattr(self, "_decision_save_lock"):
            self._decision_save_lock = asyncio.Lock()
        async with self._decision_save_lock:
            if version < 8 and _needs_candidate_editor(self.settings):
                return error_response("当前配置使用候选来源引用，请刷新编辑页后再保存", status_code=409)
            if version < 7 and self.settings.workflow_version == 5:
                return error_response("当前配置使用选项动作，需要支持版本 7 的编辑器", status_code=409)
            missing = object()
            previous = {key: config.get(key, missing) for key in changes}
            try:
                for key, value in changes.items():
                    config[key] = value
                save = getattr(config, "save_config_async", None)
                if callable(save):
                    await save()
                else:
                    config.save_config()
            except (Exception, asyncio.CancelledError) as exc:
                for key, value in previous.items():
                    if value is missing:
                        config.pop(key, None)
                    else:
                        config[key] = value
                if isinstance(exc, asyncio.CancelledError):
                    raise
                logger.exception("判断配置保存失败")
                return error_response("保存配置失败，请重试", status_code=500)
        logger.info("已更新判断配置")
        return json_response(self._decision_config_payload(warning=None))
