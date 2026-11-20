"""WebUI 可视化视图使用的后端接口。"""

from __future__ import annotations

import json

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

from .config_util import default_questions, load_questions
from .decision_tools import TOOLS
from .models import QUESTION_ROLES

PLUGIN_NAME = "astrbot_plugin_quick_decision"


class WebApiMixin:
    """由插件主类混入，注册并处理可视化视图 API。"""

    def register_plugin_web_apis(self) -> None:
        routes = [
            (f"/{PLUGIN_NAME}/logs", self.web_list_logs, ["GET"], "最近决策日志"),
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

    def _decision_config_payload(self, warning: str | None = None) -> dict:
        settings = self.settings
        questions = settings.decision_questions
        version = 2
        if any(q.stage == "post_reply" or q.decision_mode is not None for q in questions):
            version = 4
        elif any(q.branches for q in questions):
            version = 3
        return {
            "decision_mode": settings.decision_mode,
            "decision_custom_context": settings.decision_custom_context,
            "version": version,
            "default_questions": [q.to_dict() for q in default_questions()],
            "roles": QUESTION_ROLES,
            "tools": {key: label for key, (label, _, _) in TOOLS.items()},
            "decision_questions": [item.to_dict() for item in questions],
            "warning": warning
            if warning is not None
            else settings.decision_questions_warning,
        }

    async def web_decision_config(self):
        if request.method == "GET":
            return json_response(self._decision_config_payload())

        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        settings = self.settings
        mode = payload.get("decision_mode", settings.decision_mode)
        if mode not in ("model", "random"):
            return error_response("请选择模型判断或纯随机", status_code=400)
        version = payload.get("version")
        if type(version) is not int or version not in (2, 3, 4):
            return error_response("无法识别的编辑器版本，请刷新页面", status_code=400)
        if version < 4 and any(
            q.stage == "post_reply" or q.decision_mode is not None
            for q in settings.decision_questions
        ):
            return error_response("当前配置含有新版问题设置，请刷新编辑页后再保存", status_code=409)
        if version == 2 and any(q.branches for q in settings.decision_questions):
            return error_response("当前配置含有后继问题，请刷新编辑页后再保存", status_code=409)
        custom_context = str(payload.get("decision_custom_context") or "").strip()
        questions, error = load_questions(
            {
                "version": 2,
                "questions": payload.get("decision_questions"),
            },
            strict=True,
        )
        if error:
            return error_response(error, status_code=400)

        config = getattr(self, "config", None)
        if config is None or not hasattr(config, "save_config"):
            return error_response("当前环境无法写入官方插件配置", status_code=500)
        config["decision_mode"] = mode
        config["decision_custom_context"] = custom_context
        config["decision_questions"] = json.dumps(
            {"version": 2, "questions": [q.to_dict() for q in questions]},
            ensure_ascii=False,
            indent=2,
        )
        save = getattr(config, "save_config_async", None)
        if callable(save):
            await save()
        else:
            config.save_config()
        logger.info("已更新判断配置")
        return json_response(self._decision_config_payload(warning=None))
