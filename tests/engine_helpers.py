"""只供测试使用的判断与发图入口。

插件运行时走 ask_decision 与 run_tools；这些入口按给定答案解析一轮决策、列出本轮问题、
批量评估和构造独立表情消息，便于测试直接核对规则。导入前须先安装 AstrBot 测试桩。
"""

from __future__ import annotations

from astrbot_plugin_quick_decision import decision_engine as engine
from astrbot_plugin_quick_decision.config_util import PluginSettings, default_questions
from astrbot_plugin_quick_decision.decision_tools import meme_chain


async def evaluate_specs(decision, specs, *, state, provider_id=""):
    """批量提问并评估；批次异常交给调用者。"""
    questions = {spec.id: spec.question for spec in specs if spec.question is not None}
    answers = {}
    if questions:
        answers = await decision.ask(
            questions,
            state=state,
            **({"provider_id": provider_id} if provider_id else {}),
        )
    return engine._evaluate_answers(specs, dict(answers))


def _build_questions(scene, messages, memes, configured=None):
    """本轮会提出的问题（含按上级候选展开的下级题），按问题 ID 索引。"""
    specs = engine._build_specs(
        default_questions() if configured is None else configured,
        scene,
        messages,
        memes,
        PluginSettings({}),
    )
    by_id = {s.id: s for s in specs}
    for _ in specs:
        for spec in specs:
            parent = by_id.get(spec.source_question)
            if parent:
                items = [child for candidate in parent.candidate_items.values() for child in candidate.get("children", [])]
                engine._set_candidates(spec, items)
    return {s.id: s.question for s in specs if s.question is not None}


def _parse_answers(answers, *, scene, settings, memes, target_message_ids=None):
    """按给定答案解析一轮决策，不调用提供商。"""
    targets = target_message_ids
    if targets is None:
        target = next(
            (q for q in settings.decision_questions if q.role == "target" or q.candidate_source == "recent_messages"), None
        )
        answer = answers.get(target.id) if target else None
        if isinstance(answer, engine.DecisionChoiceAnswer):
            targets = set(answer.probabilities) | {answer.choice}
    specs = engine._build_specs(
        settings.decision_questions,
        scene,
        [],
        memes,
        settings,
        targets={k: k for k in targets or ()},
    )
    outcomes = {}
    workflow = engine._workflow(specs, outcomes, [])
    batch = next(workflow, None)
    while batch:
        try:
            batch = workflow.send(answers)
        except StopIteration:
            break
    return engine._assemble_result(specs, outcomes, scene=scene)


def build_meme_chain(resolver, decision):
    """不含文字、不引用的独立表情消息；最多一张，缺图时为空链。"""
    return meme_chain(resolver, decision.meme_id if decision.use_meme else None)
