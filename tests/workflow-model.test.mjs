import assert from "node:assert/strict";
import test from "node:test";
import {
  actionAvailability, changeSource, changeStage, changeType, deleteOption, deleteQuestion, findSlot,
  normalizeQuestions, possibleScenes, questionIssues, replySources, roots, runChange, serializeQuestion,
  sourceChoices, templateQuestion, validate, wouldCycle,
} from "../views/meme_manager/workflow-model.mjs";

// 与 workflow_actions.ACTIONS 一致。
const CATALOG = {
  request_reply: { label: "调用会话模型发言", stages: ["pre_reply"], parameters: [] },
  add_context: { label: "将内容交给会话模型", stages: ["pre_reply"], parameters: ["from", "text", "label"] },
  quote_message: { label: "引用选中的消息", stages: ["pre_reply"], parameters: ["from"], source: "recent_messages" },
  send_meme: {
    label: "发送表情包", stages: ["pre_reply", "post_reply"], parameters: ["from", "when"],
    source: "memes", when: ["on_match", "after_send"],
  },
};

const base = (id, fields) => ({ id, enabled: true, instructions: id, use_threshold: false, actions: {}, ...fields });
const yesNo = { type: "boolean", true_weight: 1, false_weight: 1 };
const options = (...ids) => ({ type: "choice", options: ids.map((id) => ({ id, criteria: id, weight: 1 })) });

// 设计文档「用界面从空配置重建」里的 A–E，加上冷场入口和篇幅题。
const GRAPH = [
  base("join", { ...options("接话", "略过"), triggers: [{ event: "message_received" }],
    actions: { 接话: [{ tool: "request_reply" }] } }),
  base("specific", { ...options("要", "随聊"),
    triggers: [{ question: "join", option: "接话", ask_with_parent: true }] }),
  base("which", { ...options("*", "不针对"), candidate_source: "recent_messages", requires_candidates: "recent_messages",
    triggers: [{ question: "specific", option: "要", ask_with_parent: true }],
    actions: { "*": [{ tool: "add_context" }, { tool: "quote_message" }] }, default_option: "不针对" }),
  base("asking", { ...yesNo, triggers: [{ event: "message_received" }] }),
  base("fits", { ...yesNo, stage: "post_reply", triggers: [{ event: "reply_generated" }] }),
  base("pick", { ...options("*", "不配图"), stage: "post_reply", candidate_source: "memes",
    triggers: [{ question: "fits", option: "true", ask_with_parent: true }],
    actions: { "*": [{ tool: "send_meme", when: "after_send" }] }, default_option: "不配图" }),
  base("idle", { ...yesNo, triggers: [{ event: "silence_elapsed" }], scenes: ["group_proactive"],
    actions: { true: [{ tool: "request_reply" }] } }),
  base("length", { ...options("short", "long"),
    triggers: [{ question: "join", option: "接话", ask_with_parent: true }, { question: "idle", option: "true" }],
    actions: { short: [{ tool: "add_context" }, { tool: "add_context", label: "篇幅", text: "简短" }] } }),
];

const load = () => normalizeQuestions(structuredClone(GRAPH));
const byId = (questions, id) => questions.find((q) => q.id === id);
const dump = (questions) => questions.map(serializeQuestion);

test("读入再保存不改变配置，动作挂在对应选项上", () => {
  const questions = load();
  assert.deepEqual(dump(questions), GRAPH);
  assert.equal(validate(questions, CATALOG), null);
  assert.equal(findSlot(byId(questions, "join"), "接话").actions[0].tool, "request_reply");
});

test("改题名和选项名后，触发来源、动作和默认值随之更新", () => {
  const questions = load();
  const join = byId(questions, "join");
  join.id = "A";
  findSlot(join, "接话").id = "加入";
  findSlot(byId(questions, "which"), "不针对").id = "随便";
  const saved = dump(questions);
  assert.deepEqual(saved[1].triggers, [{ question: "A", option: "加入", ask_with_parent: true }]);
  assert.deepEqual(saved[0].actions, { 加入: [{ tool: "request_reply" }] });
  assert.equal(saved[2].default_option, "随便");
  assert.equal(validate(questions, CATALOG), null);
});

test("删除父题时移除指向它的触发来源，子题进入待连接，独立入口不受影响", () => {
  const questions = load();
  const join = byId(questions, "join");
  const preview = runChange(questions, CATALOG, deleteQuestion(questions, join), { commit: false });
  assert.deepEqual([preview.triggers, preview.actions], [2, 0]);
  assert.deepEqual(dump(questions), GRAPH, "试算不改动配置");
  runChange(questions, CATALOG, deleteQuestion(questions, join));
  assert.deepEqual(byId(questions, "specific").triggers, []);
  assert.deepEqual(byId(questions, "length").triggers.map((t) => t.parent.id), ["idle"]);
  assert.deepEqual(roots(questions, "message_received").map((q) => q.id), ["asking"]);
  assert.equal(validate(questions, CATALOG), null);
});

test("删除选项时一并移除它的动作和由它触发的后继", () => {
  const questions = load();
  const join = byId(questions, "join");
  const lost = runChange(questions, CATALOG, deleteOption(join, findSlot(join, "接话")));
  assert.deepEqual([lost.triggers, lost.actions], [2, 1]);
  assert.deepEqual(byId(questions, "specific").triggers, []);
});

test("动态候选改回固定选项时移除每条候选行和引用这道题的候选动作", () => {
  const questions = load();
  const which = byId(questions, "which");
  findSlot(byId(questions, "specific"), "要").actions.push({ tool: "quote_message", from: which });
  const lost = runChange(questions, CATALOG, changeSource(which, "options"));
  assert.equal(lost.actions, 3);
  assert.deepEqual(which.options.map((o) => o.id), ["不针对"]);
  assert.equal(which.requires_candidates, "");
  assert.equal(which.default_option.id, "不针对");
});

test("固定选项改为动态候选时转为选择题并追加每条候选行", () => {
  const questions = load();
  const asking = byId(questions, "asking");
  runChange(questions, CATALOG, changeSource(asking, "memes"));
  assert.deepEqual(asking.options.map((o) => o.id), ["是", "否", "*"]);
  assert.equal(asking.requires_candidates, "memes");
  const issues = questionIssues(asking, questions, CATALOG);
  assert.equal(issues.length, 0);
});

test("正文前改为正文后：换成正文生成完成，移除只能在正文前执行的动作和正文前后继", () => {
  const questions = load();
  const join = byId(questions, "join");
  const preview = runChange(questions, CATALOG, changeStage(join, "post_reply"), { commit: false });
  assert.deepEqual([preview.triggers, preview.actions], [2, 1]);
  assert.deepEqual(dump(questions), GRAPH);
  runChange(questions, CATALOG, changeStage(join, "post_reply"));
  assert.deepEqual(join.triggers, [{ event: "reply_generated" }]);
  assert.deepEqual(findSlot(join, "接话").actions, []);
  assert.deepEqual(byId(questions, "specific").triggers, []);
  assert.equal(validate(questions, CATALOG), null);
});

test("正文后的子题改为正文前时移除正文后的来源，跨阶段同批改为命中后再问", () => {
  const questions = load();
  const pick = byId(questions, "pick");
  const lost = runChange(questions, CATALOG, changeStage(byId(questions, "fits"), "pre_reply"));
  assert.equal(lost.triggers, 1);
  assert.equal(pick.triggers[0].ask_with_parent, false);
  assert.equal(validate(questions, CATALOG), null);
});

test("是否题转为选择题不丢绑定，选择题转为是否题会移除选项上的内容", () => {
  const questions = load();
  const idle = byId(questions, "idle");
  assert.equal(runChange(questions, CATALOG, changeType(idle, "choice")).total, 0);
  assert.deepEqual(serializeQuestion(idle).actions, { 是: [{ tool: "request_reply" }] });
  assert.deepEqual(serializeQuestion(byId(questions, "length")).triggers[1], { question: "idle", option: "是" });
  const join = byId(questions, "join");
  const lost = runChange(questions, CATALOG, changeType(join, "boolean"));
  assert.deepEqual([lost.triggers, lost.actions], [2, 1]);
});

test("校验与后端规则一致并定位到字段", () => {
  const questions = load();
  const asking = byId(questions, "asking");
  const which = byId(questions, "which");
  const fixed = findSlot(which, "不针对");
  fixed.actions.push({ tool: "quote_message", from: null });
  assert.equal(questionIssues(which, questions, CATALOG)[0].a, 0);
  fixed.actions = [{ tool: "add_context", from: null, text: " " }];
  assert.match(questionIssues(which, questions, CATALOG)[0].message, /固定文本/);
  fixed.actions = [];
  asking.triggers = [{ event: "reply_generated" }];
  assert.equal(questionIssues(asking, questions, CATALOG)[0].field, "trigger");
  asking.triggers = [{ event: "message_received" }];
  byId(questions, "pick").triggers[0].ask_with_parent = true;
  byId(questions, "pick").triggers[0].parent = byId(questions, "join");
  byId(questions, "pick").triggers[0].option = findSlot(byId(questions, "join"), "略过");
  assert.equal(questionIssues(byId(questions, "pick"), questions, CATALOG)[0].field, "timing");
});

test("触发来源成环时报错，选择会成环的上游可以提前判断", () => {
  const questions = load();
  const join = byId(questions, "join");
  const which = byId(questions, "which");
  assert.equal(wouldCycle(questions, join, which), true);
  assert.equal(wouldCycle(questions, byId(questions, "asking"), which), false);
  join.triggers.push({ parent: which, option: findSlot(which, "不针对"), ask_with_parent: false });
  assert.match(validate(questions, CATALOG).message, /循环/);
});

test("动作菜单按阶段和候选类型给出不可用原因", () => {
  const questions = load();
  const join = byId(questions, "join");
  const fits = byId(questions, "fits");
  const pick = byId(questions, "pick");
  assert.equal(actionAvailability(questions, CATALOG, join, findSlot(join, "接话"), "request_reply"), "已添加");
  assert.equal(actionAvailability(questions, CATALOG, join, findSlot(join, "略过"), "quote_message"), null);
  assert.deepEqual(sourceChoices(questions, CATALOG, join, findSlot(join, "略过"), "quote_message").map((q) => q.id), ["which"]);
  assert.match(actionAvailability(questions, CATALOG, fits, fits.sides.true, "request_reply"), /正文已生成/);
  assert.match(actionAvailability(questions, CATALOG, pick, findSlot(pick, "不配图"), "send_meme"), /固定选项/);
  assert.match(actionAvailability(questions, CATALOG, join, findSlot(join, "接话"), "send_meme"), /图库表情/);
});

test("按事件、上游和场景限制计算实际提问场景与发言来源", () => {
  const questions = load();
  const scenes = (id) => [...possibleScenes(byId(questions, id))].sort();
  assert.deepEqual(scenes("idle"), ["group_proactive"]);
  assert.deepEqual(scenes("length"), ["group_passive", "group_proactive", "private_passive"]);
  assert.deepEqual(scenes("fits").length, 4);
  const sources = replySources(questions, roots(questions, "message_received"));
  assert.deepEqual(sources.map(({ q, ref }) => `${q.id}=${ref.id}`), ["join=接话"]);
  byId(questions, "join").enabled = false;
  assert.deepEqual(replySources(questions, roots(questions, "message_received")), []);
});

test("模板生成普通题，选消息题自带交给会话模型和引用", () => {
  const questions = load();
  const q = templateQuestion("message", questions);
  const saved = serializeQuestion(q);
  assert.equal(saved.id, "pick_message_1");
  assert.deepEqual(saved.actions, { "*": [{ tool: "add_context" }, { tool: "quote_message" }] });
  assert.equal(saved.default_option, "不针对");
  assert.deepEqual(saved.triggers, []);
  const meme = serializeQuestion(templateQuestion("meme", questions));
  assert.deepEqual(meme.actions, { "*": [{ tool: "send_meme", when: "after_send" }] });
});
