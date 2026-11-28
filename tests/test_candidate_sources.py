"""显式候选引用通过配置、调度、模型请求和预览的完整路径。"""

import json
import tempfile
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_decision_engine import choice, message
from test_official_inbound_handoff import _make_plugin

# isort: split
# 先安装 AstrBot 测试桩，再加载插件。
from astrbot_plugin_quick_decision.candidate_sources import CATALOGS
from astrbot_plugin_quick_decision.config_util import PluginSettings, load_questions
from astrbot_plugin_quick_decision.decision_engine import (
    _build_specs,
    _workflow,
    ask_decision,
    evaluate_post_reply,
    has_on_match_actions,
    preview_workflow,
)


def pick(name, source, triggers=None, **fields):
    return dict(id=name, type="choice", instructions=name, candidate_source=source,
                options=[{"id": "none", "criteria": "跳过"}, {"id": "*", "criteria": "{描述}"}],
                triggers=triggers if triggers is not None else [{"event": "message_received"}],
                actions={}, **fields)


def graph():
    return [pick("folders", "meme_categories"), pick("files", {"question": "folders"})]


class CandidateSourceTests(unittest.IsolatedAsyncioTestCase):
    async def run_graph(self, questions, answers, memes=None, scene="group_passive"):
        settings = PluginSettings({"decision_questions": {"version": 6, "questions": questions}})
        self.assertIsNone(settings.decision_questions_warning)
        judge = AsyncMock(side_effect=answers)
        result = await ask_decision(SimpleNamespace(decision=SimpleNamespace(ask=judge)), settings=settings,
                                    scene=scene, is_private=scene.startswith("private"), messages=[message("m")],
                                    at_or_quoted=False, memes=memes if memes is not None else [
                                        {"id": "cat", "description": "微笑", "category": "开心"},
                                        {"id": "dog", "description": "哭泣", "category": "难过"}])
        self.assertFalse(result.failed, result.error)
        return result, judge

    async def test_explicit_source_waits_despite_independent_event_and_hides_children(self):
        result, judge = await self.run_graph(graph(), [
            {"folders": choice("开心", 开心=1)}, {"files": choice("cat", cat=1)}])
        self.assertEqual([list(c.args[0]) for c in judge.await_args_list], [["folders"], ["files"]])
        self.assertEqual(set(judge.await_args.args[0]["files"].criteria), {"none", "cat"})
        self.assertNotIn("微笑", str(judge.await_args_list[0].args))
        self.assertNotIn("children", str(judge.await_args_list[0].kwargs))
        self.assertEqual(result.answers["files"], "cat")

    async def test_source_none_missing_and_unselected_branch_skip_without_fallback(self):
        for answer in ({}, {"folders": choice("none", none=1)}):
            with self.subTest(answer=answer):
                result, judge = await self.run_graph(graph(), [answer])
                judge.assert_awaited_once()
                self.assertEqual(result.outcomes["files"].reason, "candidate_source_unavailable")
                self.assertNotIn("files", result.answers)

    async def test_random_child_uses_only_selected_items(self):
        questions = graph()
        questions[1].update(decision_mode="random")
        questions[1]["options"][0]["weight"] = 0
        result, judge = await self.run_graph(questions, [{"folders": choice("开心", 开心=1)}])
        judge.assert_awaited_once()
        self.assertEqual(result.answers["files"], "cat")
        self.assertNotIn("dog", result.raw["files"]["selection_probabilities"])

    async def test_private_fixed_child_keeps_actions_and_successors_with_valid_source(self):
        questions = graph()
        questions[1].update(private_default="none", actions={"none": [{"tool": "request_reply"}]})
        questions.append({"id": "next", "type": "boolean", "instructions": "继续判断", "decision_mode": "random",
                          "true_weight": 1, "false_weight": 0, "triggers": [{"question": "files", "option": "none"}],
                          "actions": {"true": [{"tool": "add_context", "text": "私聊固定选项已采用"}]}})
        result, judge = await self.run_graph(questions, [{"folders": choice("开心", 开心=1)}], scene="private_passive")
        judge.assert_awaited_once()
        self.assertEqual(result.answers["files"], "none")
        self.assertEqual(result.outcomes["files"].reason, "private_passive_default_reply")
        self.assertTrue(result.answers["next"])
        self.assertTrue(result.should_speak())
        self.assertIn("私聊固定选项已采用", result.session_hints)
        for answers, memes in (([{"folders": choice("none", none=1)}], None), ([], [])):
            with self.subTest(answers=answers, memes=memes):
                skipped, _ = await self.run_graph(questions, answers, memes, scene="private_passive")
                self.assertNotIn("files", skipped.answers)
                self.assertNotIn("next", skipped.answers)
                self.assertFalse(skipped.should_speak())

    def test_on_match_requires_reachable_candidate_source(self):
        questions = graph()
        questions[1]["actions"] = {"*": [{"tool": "send_meme", "when": "on_match"}]}
        settings = PluginSettings({"decision_questions": {"version": 6, "questions": questions}})
        available = {"memes": True, "meme_categories": True}
        self.assertTrue(has_on_match_actions(settings.decision_questions, "group_passive", candidate_available=available))
        settings.decision_questions[0].scenes = ["private_passive"]
        self.assertFalse(has_on_match_actions(settings.decision_questions, "group_passive", candidate_available=available))

    def test_generic_nested_candidates_with_non_meme_catalog_and_empty_children(self):
        metadata = {"topics": {"label": "话题分类", "children": "prompts"}, "prompts": {"label": "话题"}}
        with patch.dict(CATALOGS, metadata):
            questions, error = load_questions({"version": 6, "questions": [
                pick("topics", {"catalog": "topics"}), pick("prompt", {"question": "topics"})]}, strict=True)
            self.assertIsNone(error)
            for children in ([{"id": "weather", "description": "谈谈天气"}], []):
                specs = _build_specs(questions, "group_passive", [], [], PluginSettings({}), catalogs={
                    "topics": [{"id": "daily", "description": "日常", "children": children}]})
                outcomes, batches = {}, []
                for _ in _workflow(specs, outcomes, batches, assumed={"topics": "*", "prompt": "*"}):
                    pass
                self.assertEqual(outcomes["prompt"].has_answer, bool(children))
                if not children:
                    self.assertEqual(outcomes["prompt"].reason, "no_child_candidates")
                else:
                    self.assertEqual(outcomes["prompt"].value, "weather")

    async def test_post_reply_reads_frozen_pre_reply_candidate(self):
        questions = graph()
        questions[0]["actions"] = {"*": [{"tool": "request_reply"}]}
        questions[1].update(stage="post_reply", triggers=[{"event": "reply_generated"}])
        result, _ = await self.run_graph(questions, [{"folders": choice("开心", 开心=1)}])
        judge = AsyncMock(return_value={"files": choice("cat", cat=1)})
        report = await evaluate_post_reply(SimpleNamespace(decision=SimpleNamespace(ask=judge)), result.post_plan,
                                           "你好", result.answers)
        self.assertEqual(report["answers"]["files"], "cat")
        self.assertEqual(set(judge.await_args.args[0]["files"].criteria), {"none", "cat"})

    def test_validation_cycles_missing_source_stage_type_and_early_asking(self):
        cases = []
        missing = graph(); missing[1]["candidate_source"] = {"question": "missing"}; cases.append(missing)
        early = graph(); early[1]["triggers"] = [{"question": "folders", "option": "*", "ask_with_parent": True}]; cases.append(early)
        cycle = graph(); cycle[0]["triggers"] = [{"question": "files", "option": "*"}]; cases.append(cycle)
        wrong = graph(); wrong[0]["candidate_source"] = "memes"; cases.append(wrong)
        post = graph(); post[0].update(stage="post_reply", triggers=[{"event": "reply_generated"}]); cases.append(post)
        malformed = graph(); malformed[1]["candidate_source"] = {"question": []}; cases.append(malformed)
        malformed_parent = graph(); malformed_parent[0]["candidate_source"] = {"question": ["invalid"]}; cases.append(malformed_parent[::-1])
        malformed_catalog = graph(); malformed_catalog[0]["candidate_source"] = {"catalog": ["invalid"]}; cases.append(malformed_catalog[::-1])
        for questions in cases:
            with self.subTest(questions=questions):
                self.assertTrue(load_questions({"version": 6, "questions": questions}, strict=True)[1])

    async def test_deleting_category_requires_repair_and_direct_gallery_is_independent(self):
        question = graph()[1]
        self.assertTrue(load_questions({"version": 6, "questions": [question]}, strict=True)[1])
        question["candidate_source"] = "memes"
        question["actions"] = {"*": [{"tool": "send_meme", "when": "on_match"}]}
        result, judge = await self.run_graph([question], [{"files": choice("dog", dog=1)}])
        self.assertEqual(set(judge.await_args.args[0]["files"].criteria), {"none", "cat", "dog"})
        self.assertEqual(result.tool_calls["send_meme"].args, {"meme_id": "dog"})

    def test_migration_and_explicit_v6_source_have_different_meanings(self):
        old = graph()
        old[1].update(candidate_source="memes", triggers=[{"question": "folders", "option": "*"}])
        loaded, error = load_questions({"version": 5, "questions": deepcopy(old)}, strict=True)
        self.assertIsNone(error)
        self.assertEqual(loaded[1].candidate_source, {"question": "folders"})
        explicit, error = load_questions({"version": 6, "questions": old}, strict=True)
        self.assertIsNone(error)
        self.assertEqual(explicit[1].candidate_source, "memes")

    def test_preview_renamed_source_keeps_real_candidate_chain(self):
        settings = PluginSettings({"decision_questions": {"version": 6, "questions": graph()}})
        selected = preview_workflow(settings, "group_passive", {"folders": "*", "files": "*"})
        self.assertEqual([b["asked"] for b in selected["batches"]], [["folders"], ["files"]])
        self.assertEqual(len(selected["batches"][1]["candidates"]["files"]), 1)
        skipped = preview_workflow(settings, "group_passive", {"folders": "none", "files": "*"})
        self.assertEqual(len(skipped["batches"]), 1)
        self.assertIn("files", skipped["skipped"])

    async def test_editor_v8_roundtrip_delete_repair_and_old_editor_protection(self):
        from astrbot_plugin_quick_decision import web_api

        class Config(dict):
            save_config = AsyncMock()
            save_config_async = save_config

        with tempfile.TemporaryDirectory() as directory:
            plugin, _ = _make_plugin(directory)
            plugin.config = Config(plugin.config)
            payload = plugin._decision_config_payload()
            self.assertEqual(payload["version"], 8)
            self.assertEqual(next(q for q in payload["decision_questions"] if q["id"] == "meme_id")["candidate_source"],
                             {"question": "meme_category"})
            request = SimpleNamespace(method="POST", json=AsyncMock(return_value=payload))
            with (patch.object(web_api, "request", request),
                  patch.object(web_api, "json_response", side_effect=lambda body: body),
                  patch.object(web_api, "error_response", side_effect=lambda message, status_code: status_code)):
                saved = await plugin.web_decision_config()
                self.assertEqual(saved["version"], 8)
                self.assertEqual(json.loads(plugin.config["decision_questions"])["version"], 6)
                original = plugin.config["decision_questions"]
                payload["version"] = 7
                self.assertEqual(await plugin.web_decision_config(), 409)
                self.assertEqual(plugin.config["decision_questions"], original)
                payload["version"] = 8
                payload["decision_questions"] = [q for q in payload["decision_questions"] if q["id"] != "meme_category"]
                self.assertEqual(await plugin.web_decision_config(), 400)
                self.assertEqual(plugin.config["decision_questions"], original)
                image = next(q for q in payload["decision_questions"] if q["id"] == "meme_id")
                image["enabled"] = False
                saved = await plugin.web_decision_config()
                self.assertIsInstance(saved, dict)
                self.assertEqual(plugin.settings.decision_questions_warning, None)

    async def test_old_editor_cannot_erase_references_before_first_v8_save(self):
        from astrbot_plugin_quick_decision import web_api

        class Config(dict):
            save_config = AsyncMock()
            save_config_async = save_config

        with tempfile.TemporaryDirectory() as directory:
            plugin, _ = _make_plugin(directory)
            plugin.config = Config(plugin.config)
            for version in (4, 5):
                with self.subTest(version=version):
                    old = graph()
                    old[1].update(candidate_source="memes", triggers=[{"question": "folders", "option": "*"}],
                                  actions={"*": [{"tool": "add_context"}]})
                    plugin.config["decision_questions"] = {"version": version, "questions": old}
                    original = deepcopy(plugin.config["decision_questions"])
                    projected = plugin._decision_config_payload()
                    self.assertEqual(projected["version"], 8)
                    self.assertEqual(projected["stored_version"], version)
                    projected["version"] = 7
                    projected["decision_questions"][1]["candidate_source"] = "options"
                    request = SimpleNamespace(method="POST", json=AsyncMock(return_value=projected))
                    with (patch.object(web_api, "request", request),
                          patch.object(web_api, "error_response", side_effect=lambda message, status_code: status_code)):
                        self.assertEqual(await plugin.web_decision_config(), 409)
                    self.assertEqual(plugin.config["decision_questions"], original)
            plugin.config.save_config.assert_not_awaited()
