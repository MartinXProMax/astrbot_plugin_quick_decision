"""WebUI「插件设置」接口：读取、校验与保存。不访问网络，不写真实配置文件。"""

# ruff: noqa: E402, I001

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_official_inbound_handoff import _make_plugin

# isort: split
# 先安装 AstrBot 测试桩，再加载插件。
from astrbot_plugin_quick_decision import web_api

SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(encoding="utf-8"))
# 在「判断配置」页编辑或已停用的配置项，不出现在「插件设置」页。
ELSEWHERE = {"decision_mode", "decision_custom_context", "decision_questions", "vision_provider_id"}


def schema_keys(node=SCHEMA, prefix=""):
    for key, item in node.items():
        if item.get("type") == "object":
            yield from schema_keys(item["items"], f"{prefix}{key}.")
        else:
            yield f"{prefix}{key}"


class Config(dict):
    save_config = AsyncMock()
    save_config_async = save_config


class SettingsApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.plugin, _ = _make_plugin(self._dir.name)
        self.plugin.config = Config(self.plugin.config)
        self.plugin.config.save_config_async = AsyncMock()

    def tearDown(self):
        self._dir.cleanup()

    async def call(self, method="GET", body=None):
        request = SimpleNamespace(method=method, json=AsyncMock(return_value=body))
        with (patch.object(web_api, "request", request),
              patch.object(web_api, "json_response", side_effect=lambda data: data),
              patch.object(web_api, "error_response", side_effect=lambda message, status_code: (status_code, message))):
            return await self.plugin.web_settings()

    def test_every_schema_option_is_on_the_settings_page_or_edited_elsewhere(self):
        shown = set(web_api.setting_fields())
        # 外部插件适配的开关在「外部插件」页编辑，见 test_external_plugins。
        plugin_keys = {key for key in schema_keys() if key.startswith("external_plugins.")}
        self.assertTrue(plugin_keys)
        self.assertEqual(shown, set(schema_keys()) - ELSEWHERE - plugin_keys)
        self.assertFalse(shown & ELSEWHERE)

    async def test_get_returns_schema_fields_and_effective_values(self):
        data = await self.call()
        self.assertEqual([s["id"] for s in data["sections"]], ["run", "judge", "pace", "group", "private", "logs"])
        fields = {f["key"]: f for s in data["sections"] for f in s["fields"]}
        silence = fields["group.silence_seconds"]
        self.assertEqual((silence["type"], silence["min"], silence["max"], silence["unit"]), ("int", 30, 21600, "秒"))
        self.assertEqual(silence["label"], SCHEMA["group"]["items"]["silence_seconds"]["description"])
        self.assertEqual([o["value"] for o in fields["group.target_quote_mode"]["options"]], ["first", "all", "none"])
        group_hint = next(s["hint"] for s in data["sections"] if s["id"] == "group")
        self.assertEqual(group_hint, SCHEMA["group"]["hint"])
        values = data["values"]
        # 未写入配置的项显示插件实际使用的默认值。
        self.assertEqual(values["group.silence_seconds"], 600)
        self.assertEqual(values["debounce_seconds"], 0)
        self.assertIs(values["enabled"], True)
        self.assertEqual(values["group.target_quote_mode"], "first")
        self.assertEqual(values["decision_provider_id"], "")

    async def test_post_saves_only_changed_keys_and_keeps_other_group_keys(self):
        self.plugin.config["decision_mode"] = "random"
        data = await self.call("POST", {"changes": {
            "dry_run": True, "choice_threshold": 0.65, "group.silence_seconds": 900.0,
            "group.target_quote_mode": "all", "decision_provider_id": "  judge-1  ",
            "private.hourly_proactive_limit": 5, "decision_log_limit": 500,
        }})
        config = self.plugin.config
        config.save_config_async.assert_awaited_once()
        self.assertIs(config["dry_run"], True)
        self.assertEqual(config["choice_threshold"], 0.65)
        self.assertEqual(config["decision_provider_id"], "judge-1")
        self.assertEqual(config["group"]["silence_seconds"], 900)
        self.assertIsInstance(config["group"]["silence_seconds"], int)
        self.assertEqual(config["group"]["target_quote_mode"], "all")
        self.assertIs(config["group"]["enable_interrupt"], True)
        self.assertIs(config["group"]["direct_send"], True)
        self.assertEqual(config["private"], {"hourly_proactive_limit": 5})
        self.assertEqual(config["decision_mode"], "random")
        self.assertEqual(self.plugin.sessions.log_limit, 500)
        self.assertEqual(data["values"]["group.silence_seconds"], 900)
        self.assertEqual(data["values"]["decision_provider_id"], "judge-1")
        self.assertIs(self.plugin.settings.dry_run, True)

    async def test_post_rejects_invalid_values_without_saving(self):
        before = json.loads(json.dumps(self.plugin.config))
        cases = {
            "unknown": {"decision_questions": "[]"},
            "bool as text": {"enabled": "false"},
            "bool for number": {"cooldown_seconds": True},
            "fraction for int": {"cooldown_seconds": 30.5},
            "below range": {"group.silence_seconds": 10},
            "above range": {"choice_temperature": 2.5},
            "not finite": {"debounce_seconds": float("nan")},
            "bad option": {"group.target_quote_mode": "both"},
            "too long": {"decision_provider_id": "x" * 201},
            "no changes object": None,
        }
        for name, changes in cases.items():
            with self.subTest(name):
                body = {"changes": changes} if changes is not None else {"enabled": False}
                status, message = await self.call("POST", body)
                self.assertEqual(status, 400)
                self.assertTrue(message)
        self.assertEqual(self.plugin.config, before)
        self.plugin.config.save_config_async.assert_not_awaited()

    async def test_empty_changes_return_current_values_without_saving(self):
        data = await self.call("POST", {"changes": {}})
        self.assertIn("values", data)
        self.plugin.config.save_config_async.assert_not_awaited()

    async def test_failed_save_restores_previous_values(self):
        group = self.plugin.config["group"]
        snapshot = dict(group)
        self.plugin.config.save_config_async = AsyncMock(side_effect=OSError("disk full"))
        status, _ = await self.call("POST", {"changes": {"enabled": False, "group.enable_interrupt": False,
                                                         "decision_log_limit": 300}})
        self.assertEqual(status, 500)
        self.assertIs(self.plugin.config["enabled"], True)
        self.assertIs(self.plugin.config["group"], group)
        self.assertEqual(group, snapshot)
        self.assertNotIn("decision_log_limit", self.plugin.config)
        self.assertEqual(self.plugin.sessions.log_limit, 20)

    async def test_environment_without_writable_config_reports_error(self):
        self.plugin.config = dict(self.plugin.config)
        status, _ = await self.call("POST", {"changes": {"enabled": False}})
        self.assertEqual(status, 500)


if __name__ == "__main__":
    unittest.main()
