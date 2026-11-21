"""WebUI 可视化视图使用的后端接口。"""

from __future__ import annotations

import asyncio
import json

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

from .config_util import EVENTS, OPTION_EFFECTS, PluginSettings, default_questions, load_questions
from .decision_engine import preview_workflow
from .decision_tools import TOOLS
from .models import QUESTION_ROLES
from .workflow_actions import ACTIONS, SOURCES, WORKFLOW_EVENTS, migrate_actions

PLUGIN_NAME = "astrbot_plugin_quick_decision"


class WebApiMixin:
    """由插件主类混入，注册并处理可视化视图 API。"""

    def register_plugin_web_apis(self) -> None:
        routes = [
            (f"/{PLUGIN_NAME}/logs", self.web_list_logs, ["GET"], "最近决策日志"),
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
        limit = request.query.get("limit", 30, type=int)
        return json_response({"logs": self.sessions.recent_logs(limit=limit)})

    async def web_status(self):
        settings = self.settings
        adapter_status = self.meme_adapter.status().to_dict()
        return json_response(
            {
                "enabled": settings.enabled,
                "dry_run": settings.dry_run,
                "decision_provider_id": settings.decision_provider_id,
                "meme_count": adapter_status.get("candidate_count", 0),
                "meme_adapter": adapter_status,
            }
        )

    def _decision_config_payload(self, warning: str | None = None, *, version=6) -> dict:
        settings = self.settings
        questions = settings.decision_questions
        payload = {
            "decision_mode": settings.decision_mode,
            "decision_custom_context": settings.decision_custom_context,
            "version": 6,
            "events": EVENTS,
            "effects": OPTION_EFFECTS,
            "default_questions": [q.to_dict() for q in default_questions()],
            "roles": QUESTION_ROLES,
            "tools": TOOLS,
            "decision_questions": [item.to_dict() for item in questions],
            "warning": warning
            if warning is not None
            else settings.decision_questions_warning,
        }
        if version == 7 or settings.workflow_version == 5:
            questions, migration_warning = migrate_actions(questions)
            defaults, _ = migrate_actions(default_questions())
            for obsolete in ("roles", "effects", "tools"):
                payload.pop(obsolete)
            # stored_version 是落盘图的版本；为 5 以前时首次保存会升级，旧编辑页随后无法再写入。
            payload.update(version=7, workflow_version=5, stored_version=settings.workflow_version,
                           events=WORKFLOW_EVENTS, actions=ACTIONS,
                           candidate_sources=SOURCES, decision_questions=[q.to_dict() for q in questions],
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
        if version not in (6, 7):
            return error_response("预览配置版本无效", status_code=400)
        settings.decision_questions, error = load_questions(
            {"version": 5 if version == 7 else 4, "questions": payload.get("decision_questions")}, strict=True)
        if error:
            return error_response(error, status_code=400)
        return json_response(preview_workflow(settings, payload["scene"], choices))

    async def web_decision_config(self):
        if request.method == "GET":
            version = getattr(request, "query", {}).get("version")
            return json_response(self._decision_config_payload(version=7 if str(version) == "7" else 6))

        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        settings = self.settings
        mode = payload.get("decision_mode", settings.decision_mode)
        if mode not in ("model", "random"):
            return error_response("请选择模型判断或纯随机", status_code=400)
        version = payload.get("version")
        if type(version) is not int or version not in (2, 3, 4, 5, 6, 7):
            return error_response("无法识别的编辑器版本，请刷新页面", status_code=400)
        if version < 6:
            return error_response("当前判断使用生效条件，请刷新编辑页后再保存", status_code=409)
        if version == 6 and settings.workflow_version == 5:
            return error_response("当前配置使用选项动作，需要支持版本 7 的编辑器", status_code=409)
        custom_context = str(payload.get("decision_custom_context") or "").strip()
        edited = payload.get("decision_questions")
        if version == 7 and isinstance(edited, list) and all(isinstance(q, dict) for q in edited):
            old_ids = {q.id for q in settings.decision_questions}
            deleted = old_ids.difference(q["id"] for q in edited if isinstance(q.get("id"), str))
            if deleted:
                for q in edited:
                    if isinstance(q.get("triggers"), list):
                        q["triggers"] = [t for t in q["triggers"] if not (
                            isinstance(t, dict) and isinstance(t.get("question"), str) and t["question"] in deleted)]
        questions, error = load_questions(
            {
                "version": 5 if version == 7 else 4,
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
                {"version": 5 if version == 7 else 4, "questions": [q.to_dict() for q in questions]},
                ensure_ascii=False, indent=2,
            ),
        }
        if not hasattr(self, "_decision_save_lock"):
            self._decision_save_lock = asyncio.Lock()
        async with self._decision_save_lock:
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
