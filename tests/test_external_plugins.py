"""「外部插件」页：适配器目录、读取与保存适配开关，以及停用后的运行时行为。不访问网络。"""

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
from astrbot_plugin_quick_decision.candidate_sources import CATALOGS
from astrbot_plugin_quick_decision.config_util import PluginSettings
from astrbot_plugin_quick_decision.external_plugins import EXTERNAL_PLUGINS
from astrbot_plugin_quick_decision.workflow_actions import ACTIONS

SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(encoding="utf-8"))


class Config(dict):
    save_config = AsyncMock()
    save_config_async = save_config


class RegistryTests(unittest.TestCase):
    def test_every_adapter_declares_known_capabilities_and_its_own_default_questions(self):
        defaults = {q["id"]: q for q in web_api.v8_default_questions()}
        claimed = []
        for plugin in EXTERNAL_PLUGINS.values():
            with self.subTest(plugin.id):
                self.assertTrue(plugin.default_questions, "每个适配器都要自带默认问题")
                self.assertTrue(set(plugin.actions) <= ACTIONS.keys())
                self.assertTrue(set(plugin.catalogs) <= CATALOGS.keys())
                self.assertTrue(set(plugin.default_questions) <= defaults.keys())
                items = SCHEMA["external_plugins"]["items"][plugin.id]["items"]
                self.assertEqual(items["enabled"]["type"], "bool")
                self.assertIs(items["enabled"]["default"], True)
                claimed.extend(plugin.default_questions)
        self.assertEqual(len(claimed), len(set(claimed)), "一道默认问题只属于一个适配器")

    def test_schema_lists_only_registered_adapters(self):
        self.assertEqual(set(SCHEMA["external_plugins"]["items"]), set(EXTERNAL_PLUGINS))

    def test_settings_default_to_enabled_and_tolerate_malformed_config(self):
        for config in ({}, {"external_plugins": None}, {"external_plugins": {"meme_manager": "off"}},
                       {"external_plugins": {"meme_manager": {}}}):
            with self.subTest(config=config):
                self.assertTrue(PluginSettings(config).plugin_enabled("meme_manager"))
        off = PluginSettings({"external_plugins": {"meme_manager": {"enabled": False}}})
        self.assertFalse(off.plugin_enabled("meme_manager"))
        self.assertFalse(PluginSettings({"external_plugins": {"meme_manager": {"enabled": "false"}}})
                         .plugin_enabled("meme_manager"))


class ExternalPluginsApiTests(unittest.IsolatedAsyncioTestCase):
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
            return await self.plugin.web_external_plugins()

    async def test_get_lists_adapter_status_actions_and_default_questions(self):
        data = await self.call()
        meme = next(p for p in data["plugins"] if p["id"] == "meme_manager")
        self.assertEqual(meme["name"], "表情包管理器")
        self.assertEqual(meme["values"], {"enabled": True})
        self.assertEqual([f["key"] for f in meme["fields"]], ["enabled"])
        self.assertEqual(meme["fields"][0]["label"], SCHEMA["external_plugins"]["items"]["meme_manager"]["items"]["enabled"]["description"])
        self.assertEqual(meme["status"]["available"], False)
        self.assertEqual(meme["status"]["message"], "stub")
        send = meme["actions"][0]
        self.assertEqual((send["id"], send["label"], send["source_label"]), ("send_meme", "发送表情包", "全部图库图片"))
        self.assertEqual(send["when"], ["on_match", "after_send"])
        self.assertEqual([c["id"] for c in meme["catalogs"]], ["meme_categories", "memes"])
        self.assertEqual(meme["catalogs"][0]["children_label"], "全部图库图片")
        # 自带的默认问题与判断配置页「恢复默认问题」里的同名题一致。
        restore = {q["id"]: q for q in self.plugin._decision_config_payload(version=8)["default_questions"]}
        self.assertEqual([q["id"] for q in meme["default_questions"]], ["use_meme", "meme_category", "meme_id"])
        for question in meme["default_questions"]:
            self.assertEqual(question, restore[question["id"]])
        # 测试替身没有报告表情包管理器的设置，建议都无法检查。
        self.assertEqual([tip["done"] for tip in meme["tips"]], [None, None])
        self.assertTrue(all(tip["text"] for tip in meme["tips"]))

    async def test_available_adapter_reports_version_and_pack_details(self):
        packs = [{"id": "daily", "name": "日常", "enabled": True, "present": True},
                 {"id": "old", "name": "旧图", "enabled": False, "present": False}]
        status = {"available": True, "plugin_loaded": True, "plugin_version": "v3.2.1", "message": "",
                  "candidate_count": 12, "packs": packs}
        self.plugin.meme_adapter = SimpleNamespace(status=lambda: SimpleNamespace(to_dict=lambda: status))
        meme = (await self.call())["plugins"][0]
        self.assertEqual(meme["status"]["plugin_version"], "v3.2.1")
        self.assertEqual(meme["details"], [
            {"label": "图包", "value": "日常、旧图（已停用）（文件缺失）"},
            {"label": "可选表情", "value": "12 张带描述的表情"},
        ])

    async def test_tips_show_which_settings_are_already_done(self):
        status = {"available": True, "plugin_loaded": True, "plugin_version": "v3.2.1", "message": "",
                  "candidate_count": 0, "packs": [], "send_tool_enabled": False, "send_probability": 100}
        self.plugin.meme_adapter = SimpleNamespace(status=lambda: SimpleNamespace(to_dict=lambda: status))
        meme = (await self.call())["plugins"][0]
        self.assertEqual([(tip["text"], tip["done"]) for tip in meme["tips"]],
                         [("关闭「启用主动发送表情工具」", True), ("把「表情发送概率」设为 0", False)])

    async def test_timer_tip_reads_the_decision_config(self):
        def timer_tip(data):
            card = next(p for p in data["plugins"] if p["id"] == "universal_tts")
            return card["tips"][-1]["done"]

        self.assertTrue(timer_tip(await self.call()), "默认配置没有定时发言")
        graph = [*web_api.v8_default_questions(), {
            "id": "timer_speak", "type": "boolean", "instructions": "定时发言吗？",
            "triggers": [{"event": "timer_elapsed", "interval_seconds": 3600}],
            "actions": {"true": [{"tool": "request_reply"}]}}]
        self.plugin.config["decision_questions"] = {"version": 6, "questions": graph}
        self.assertFalse(timer_tip(await self.call()))

    def test_meme_manager_settings_follow_its_config_and_defaults(self):
        from astrbot_plugin_quick_decision.meme_adapter import meme_manager_settings

        def star(config):
            return SimpleNamespace(star_cls=SimpleNamespace(config=config))

        self.assertEqual(meme_manager_settings(star({"generation": {"emotion": {
            "send_tool_enabled": False, "probability": 0}}})), {"send_tool_enabled": False, "send_probability": 0})
        self.assertEqual(meme_manager_settings(star({})), {"send_tool_enabled": True, "send_probability": 100})
        self.assertEqual(meme_manager_settings(star({"generation": {"emotion": {"probability": "x"}}})),
                         {"send_tool_enabled": True, "send_probability": None})
        self.assertEqual(meme_manager_settings(None), {"send_tool_enabled": None, "send_probability": None})

    async def test_status_failure_is_reported_instead_of_raising(self):
        def broken():
            raise OSError("registry unreadable")

        self.plugin.meme_adapter = SimpleNamespace(status=broken)
        meme = (await self.call())["plugins"][0]
        self.assertFalse(meme["status"]["available"])
        self.assertTrue(meme["status"]["message"])

    async def test_post_disables_adapter_and_keeps_other_keys(self):
        self.plugin.config["external_plugins"] = {"meme_manager": {"enabled": True, "note": "keep"}, "other": {"x": 1}}
        data = await self.call("POST", {"changes": {"meme_manager": {"enabled": False}}})
        config = self.plugin.config
        config.save_config_async.assert_awaited_once()
        self.assertEqual(config["external_plugins"], {"meme_manager": {"enabled": False, "note": "keep"}, "other": {"x": 1}})
        self.assertEqual(data["plugins"][0]["values"], {"enabled": False})
        self.assertFalse(self.plugin.settings.plugin_enabled("meme_manager"))

    async def test_post_rejects_invalid_changes_without_saving(self):
        before = json.loads(json.dumps(self.plugin.config))
        cases = {
            "no changes object": {"enabled": False},
            "unknown plugin": {"changes": {"nope": {"enabled": False}}},
            "options not object": {"changes": {"meme_manager": False}},
            "unknown field": {"changes": {"meme_manager": {"volume": 3}}},
            "bool as text": {"changes": {"meme_manager": {"enabled": "false"}}},
        }
        for name, body in cases.items():
            with self.subTest(name):
                status, message = await self.call("POST", body)
                self.assertEqual(status, 400)
                self.assertTrue(message)
        self.assertEqual(self.plugin.config, before)
        self.plugin.config.save_config_async.assert_not_awaited()

    async def test_failed_save_restores_previous_value(self):
        self.plugin.config.save_config_async = AsyncMock(side_effect=OSError("disk full"))
        status, _ = await self.call("POST", {"changes": {"meme_manager": {"enabled": False}}})
        self.assertEqual(status, 500)
        self.assertNotIn("external_plugins", self.plugin.config)
        self.assertTrue(self.plugin.settings.plugin_enabled("meme_manager"))


class DisabledAdapterRuntimeTests(unittest.TestCase):
    def test_disabled_meme_adapter_provides_no_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = _make_plugin(tmp)
            catalog = [{"id": "a" * 64, "description": "点头", "category": "daily/开心"}]
            plugin.meme_adapter.described_catalog = lambda: catalog
            self.assertEqual(plugin._meme_candidates(plugin.settings), catalog)
            plugin.config["external_plugins"] = {"meme_manager": {"enabled": False}}
            self.assertEqual(plugin._meme_candidates(plugin.settings), [])


if __name__ == "__main__":
    unittest.main()
