"""插件设置缓存：配置不变时复用同一份解析结果，任何改动都会重新读取。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_v11_config import _install_astrbot_stubs  # isort: skip

_install_astrbot_stubs()

from test_decision_engine import message  # noqa: E402
from test_official_inbound_handoff import _make_plugin  # noqa: E402

from astrbot_plugin_quick_decision.config_util import PluginSettings  # noqa: E402
from astrbot_plugin_quick_decision.decision_engine import (  # noqa: E402
    ask_decision,
    has_on_match_actions,
    preview_workflow,
)
from astrbot_plugin_quick_decision.workflow_actions import migrate_actions  # noqa: E402

SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(encoding="utf-8"))
SCENES = ("group_passive", "private_passive", "group_proactive", "private_proactive")


def saved_text():
    """与 WebUI 保存格式一致的版本 6 判断图。"""
    questions, _ = migrate_actions(PluginSettings({}).decision_questions)
    return json.dumps({"version": 6, "questions": [q.to_dict() for q in questions]}, ensure_ascii=False, indent=2)


def snapshot(settings):
    return [q.to_dict() for q in settings.decision_questions]


class SettingsCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.plugin, self.main = _make_plugin(self.tmp.name)

    def count_builds(self):
        return patch.object(self.main, "PluginSettings", wraps=PluginSettings)

    def test_unchanged_config_is_parsed_once(self) -> None:
        for raw in (saved_text(), SCHEMA["decision_questions"]["default"], None, json.loads(saved_text())):
            with self.subTest(kind=type(raw).__name__), self.count_builds() as build:
                # 保存的图与默认图文本相同时会直接命中缓存；每种写法单独计数。
                self.plugin.__dict__.pop("_settings_cache", None)
                self.plugin.config["decision_questions"] = raw
                first = self.plugin.settings
                for _ in range(5):
                    self.assertIs(self.plugin.settings, first)
                self.assertEqual(build.call_count, 1)

    def test_every_kind_of_config_edit_is_picked_up(self) -> None:
        graph = json.loads(saved_text())
        self.plugin.config["decision_questions"] = graph
        self.assertEqual(self.plugin.settings.cooldown_seconds, 300)

        def replace_text(config):
            edited = json.loads(json.dumps(graph))
            edited["questions"][1]["instructions"] = "新题干"
            config["decision_questions"] = json.dumps(edited, ensure_ascii=False)

        edits = (
            ("top-level value", lambda c: c.__setitem__("cooldown_seconds", 5),
             lambda s: s.cooldown_seconds == 5),
            ("nested value changed in place", lambda c: c["group"].__setitem__("silence_seconds", 90),
             lambda s: s.group_silence_seconds == 90),
            ("question changed in place", lambda c: c["decision_questions"]["questions"][0].__setitem__("enabled", False),
             lambda s: s.decision_questions[0].enabled is False),
            ("question text replaced", replace_text,
             lambda s: s.decision_questions[1].instructions == "新题干"),
        )
        for label, edit, check in edits:
            with self.subTest(edit=label):
                before = self.plugin.settings
                edit(self.plugin.config)
                after = self.plugin.settings
                self.assertIsNot(after, before)
                self.assertTrue(check(after))

    def test_warnings_survive_the_cache(self) -> None:
        self.plugin.config["decision_questions"] = '{"version": 99, "questions": []}'
        for _ in range(2):
            self.assertEqual(self.plugin.settings.decision_questions, [])
            self.assertTrue(self.plugin.settings.decision_questions_warning)

    def test_settings_built_elsewhere_are_independent(self) -> None:
        # 引擎测试和日志重放会自行构造并修改设置，它们不能影响插件缓存或彼此。
        self.plugin.config["decision_questions"] = saved_text()
        cached = self.plugin.settings
        own = PluginSettings(dict(self.plugin.config))
        own.decision_questions[0].enabled = False
        own.decision_mode = "random"
        self.assertIsNot(own.decision_questions[0], cached.decision_questions[0])
        self.assertTrue(self.plugin.settings.decision_questions[0].enabled)
        self.assertEqual(self.plugin.settings.decision_mode, "model")
        self.assertTrue(PluginSettings(dict(self.plugin.config)).decision_questions[0].enabled)

    def test_unserializable_config_falls_back_to_a_fresh_parse(self) -> None:
        self.plugin.config["odd"] = {1: "x", "y": 2}
        with self.count_builds() as build:
            self.plugin.settings
            self.plugin.settings
        self.assertEqual(build.call_count, 2)

    async def test_rounds_do_not_change_the_cached_questions(self) -> None:
        for raw in (saved_text(), SCHEMA["decision_questions"]["default"]):
            self.plugin.config.update(decision_questions=raw, decision_mode="random")
            settings = self.plugin.settings
            expected = snapshot(PluginSettings(dict(self.plugin.config)))
            for scene in SCENES:
                has_on_match_actions(settings.decision_questions, scene)
                preview_workflow(settings, scene, {})
                await ask_decision(SimpleNamespace(), settings=settings, scene=scene,
                                   is_private=scene.startswith("private"), messages=[message("m1")],
                                   at_or_quoted=False, memes=[{"id": "cat", "description": "猫"}])
            self.assertIs(self.plugin.settings, settings)
            self.assertEqual(snapshot(settings), expected)


if __name__ == "__main__":
    unittest.main()
