import assert from "node:assert/strict";
import test from "node:test";
import {
  candidateSourceChoices, candidateSourceIssue, candidateType, configureCandidateCatalogs, previewCandidateItems,
  actionAvailability, changeSource, changeStage, changeType, deleteOption, deleteQuestion, findSlot,
  normalizeQuestions, pluginQuestions, possibleScenes, questionIssues, questionsForScene, questionsToAdd, replySources, roots,
  runChange, serializeQuestion,
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

test("两种定时触发可读写，副本修改不影响原题，切换阶段移除定时参数", () => {
  for (const schedule of [{ interval_seconds: 600 }, { daily_time: "08:00" }]) {
    const trigger = { event: "timer_elapsed", ...schedule };
    const raw = base("timer", { ...yesNo, triggers: [trigger] });
    const questions = normalizeQuestions([raw]);
    const q = questions[0];
    assert.deepEqual(serializeQuestion(q).triggers, [trigger]);
    assert.equal(validate(questions, CATALOG), null);
    assert.deepEqual([...possibleScenes(q)], ["group_proactive", "private_proactive"]);
    q.triggers[0].daily_time = "09:00";
    assert.deepEqual(raw.triggers, [trigger]);
    runChange(questions, CATALOG, changeStage(q, "post_reply"));
    assert.deepEqual(serializeQuestion(q).triggers, [{ event: "reply_generated" }]);
  }
});

test("定时参数缺失、冲突、非法间隔或时刻均定位到触发来源", () => {
  for (const schedule of [{}, { interval_seconds: 14 }, { interval_seconds: true }, { interval_seconds: 15.5 },
    { interval_seconds: 2147483648 }, { daily_time: "24:00" }, { daily_time: "8:00" }, { daily_time: "08:60" },
    { daily_time: "08:00\n" }, { daily_time: null }, { interval_seconds: 60, daily_time: "08:00" }]) {
    const questions = normalizeQuestions([base("timer", { ...yesNo, triggers: [{ event: "timer_elapsed", ...schedule }] })]);
    assert.equal(validate(questions, CATALOG)?.field, "trigger");
  }
});

test("定时预览包含定时入口及后继，与冷场入口分开", () => {
  const questions = normalizeQuestions([
    base("timer", { ...yesNo, triggers: [{ event: "timer_elapsed", interval_seconds: 600 }] }),
    base("child", { ...yesNo, triggers: [{ question: "timer", option: "true" }] }),
    base("idle", { ...yesNo, triggers: [{ event: "silence_elapsed" }] }),
  ]);
  assert.deepEqual(questionsForScene(questions, "group_proactive", "timer_elapsed").map((q) => q.id), ["timer", "child"]);
  assert.deepEqual(questionsForScene(questions, "private_proactive", "silence_elapsed").map((q) => q.id), ["idle"]);
});

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

const CANDIDATE_GRAPH = [
  base("category", { ...options("none", "*"), candidate_source: "meme_categories", triggers: [{ event: "message_received" }] }),
  base("image", { ...options("none", "*"), candidate_source: { question: "category" },
    triggers: [{ question: "category", option: "*" }], actions: { "*": [{ tool: "send_meme", when: "after_send" }] } }),
];

test("候选引用随重命名保存，图片类型可供发图动作使用", () => {
  const questions = normalizeQuestions(CANDIDATE_GRAPH);
  assert.equal(validate(questions, CATALOG), null);
  assert.equal(candidateType(questions[1]), "memes");
  questions[0].id = "图库分组";
  assert.deepEqual(serializeQuestion(questions[1]).candidate_source, { question: "图库分组" });
  assert.deepEqual(candidateSourceChoices(questions, questions[1]), [questions[0]]);
});

test("删除候选来源后保持失效引用，停用受影响题或修复来源后可以保存", () => {
  const questions = normalizeQuestions(CANDIDATE_GRAPH);
  const [category, image] = questions;
  runChange(questions, CATALOG, deleteQuestion(questions, category));
  assert.match(candidateSourceIssue(image, questions), /已删除/);
  assert.equal(validate(questions, CATALOG).field, "source");
  assert.deepEqual(serializeQuestion(image).candidate_source, { question: "category" });
  assert.equal(image.options[1].actions.length, 1);
  image.enabled = false;
  assert.equal(validate(questions, CATALOG), null);
  image.enabled = true;
  runChange(questions, CATALOG, changeSource(image, "memes"));
  image.triggers = [{ event: "message_received" }];
  assert.equal(candidateSourceIssue(image, questions), null);
  assert.equal(validate(questions, CATALOG), null);
});

test("候选引用禁止同批提前提问，并参与混合循环及阶段校验", () => {
  const questions = normalizeQuestions(CANDIDATE_GRAPH);
  const [category, image] = questions;
  image.triggers[0].ask_with_parent = true;
  assert.equal(validate(questions, CATALOG).field, "timing");
  runChange(questions, CATALOG, () => () => {});
  assert.equal(image.triggers[0].ask_with_parent, false);
  category.triggers = [{ parent: image, option: image.options[1], ask_with_parent: false }];
  assert.equal(wouldCycle(questions, image, category), true);
  assert.match(candidateSourceIssue(image, questions), /循环/);
  category.triggers = [{ event: "reply_generated" }];
  category.stage = "post_reply";
  assert.match(candidateSourceIssue(image, questions), /正文生成后/);
});

test("预览的示例选项随来源选项变化，来源无选择、停用或删除时没有下级", () => {
  const questions = normalizeQuestions(CANDIDATE_GRAPH);
  const [category, image] = questions;
  const catalogs = { meme_categories: [
    { id: "开心", description: "开心", children: [{ id: "smile", description: "微笑" }] },
    { id: "难过", description: "难过", children: [{ id: "cry", description: "流泪" }] },
  ] };
  const choices = new Map();
  assert.deepEqual(previewCandidateItems(image, catalogs, choices), []);
  choices.set(category, "开心");
  assert.deepEqual(previewCandidateItems(image, catalogs, choices).map((item) => item.id), ["smile"]);
  choices.set(category, "难过");
  assert.deepEqual(previewCandidateItems(image, catalogs, choices).map((item) => item.id), ["cry"]);
  category.enabled = false;
  assert.deepEqual(previewCandidateItems(image, catalogs, choices), []);
  image.candidate_from = null;
  assert.deepEqual(previewCandidateItems(image, catalogs, choices), []);
});

test("候选来源的场景限制也约束依赖题", () => {
  const [category, image] = normalizeQuestions(CANDIDATE_GRAPH);
  image.triggers = [{ event: "message_received" }];
  category.scenes = ["private_passive"];
  assert.deepEqual([...possibleScenes(image)], ["private_passive"]);
});

test("非图库的下级候选复用同一来源、类型与删除规则", () => {
  configureCandidateCatalogs({ folders: { label: "文件夹", children: "documents" }, documents: { label: "文件" } });
  const questions = normalizeQuestions([
    base("folder", { ...options("none", "*"), candidate_source: "folders", triggers: [{ event: "message_received" }] }),
    base("file", { ...options("none", "*"), candidate_source: { question: "folder" }, triggers: [{ event: "message_received" }] }),
  ]);
  assert.equal(candidateType(questions[1]), "documents");
  assert.deepEqual(candidateSourceChoices(questions, questions[1]), [questions[0]]);
  assert.equal(validate(questions, CATALOG), null);
  assert.equal(actionAvailability(questions, CATALOG, questions[1], questions[1].options[1], "send_meme"), "需要一道从图库表情中选择的题");
});

test("外部插件的默认问题按 ID 接到已有题上，跳过同名题并丢掉找不到的引用", () => {
  const existing = normalizeQuestions([
    base("action", { ...options("none", "reply"), triggers: [{ event: "message_received" }] }),
    base("meme_category", { ...options("none", "*"), candidate_source: "meme_categories" }),
  ]);
  const defaults = [
    base("use_meme", {
      ...yesNo,
      triggers: [{ question: "action", option: "reply", ask_with_parent: true }, { question: "idle_action", option: "proactive" }],
      actions: { true: [{ tool: "send_meme", from: "meme_id", when: "after_send" }] },
    }),
    base("meme_category", { ...options("none", "*"), candidate_source: "meme_categories" }),
    base("meme_id", { ...options("none", "*"), candidate_source: { question: "meme_category" }, triggers: [{ question: "meme_category", option: "*" }] }),
  ];
  const added = questionsToAdd(existing, defaults);
  assert.deepEqual(added.map((q) => q.id), ["use_meme", "meme_id"]);
  const [useMeme, memeId] = added;
  // 指向已有题的触发条件和候选来源直接引用已有对象，找不到的 idle_action 被丢掉。
  assert.equal(useMeme.triggers.length, 1);
  assert.equal(useMeme.triggers[0].parent, existing[0]);
  assert.equal(useMeme.triggers[0].option, findSlot(existing[0], "reply"));
  assert.equal(useMeme.sides.true.actions[0].from, memeId);
  assert.equal(memeId.candidate_from, existing[1]);
  assert.equal(memeId.triggers[0].parent, existing[1]);
  assert.equal(candidateType(memeId), "memes");
  // 原始默认问题不被改动，同一批默认问题可以重复使用。
  assert.equal(defaults[0].triggers.length, 2);
  assert.deepEqual(questionsToAdd([...existing, ...added], defaults), []);
  const questions = [...existing, ...added];
  assert.equal(validate(questions, CATALOG), null);
  assert.deepEqual(serializeQuestion(useMeme).triggers, [{ question: "action", option: "reply", ask_with_parent: true }]);
});

test("没有触发来源的默认问题进入待连接", () => {
  const [lonely] = questionsToAdd([], [base("use_meme", { ...yesNo, triggers: [{ question: "action", option: "reply" }] })]);
  assert.equal(lonely.triggers.length, 0);
});

test("按动作和候选来源找出用到外部插件的题", () => {
  const plugin = { actions: [{ id: "send_meme" }], catalogs: [{ id: "meme_categories" }, { id: "memes" }] };
  const questions = normalizeQuestions([
    base("action", { ...options("none", "reply"), triggers: [{ event: "message_received" }], actions: { reply: [{ tool: "request_reply" }] } }),
    base("use_meme", { ...yesNo, actions: { true: [{ tool: "send_meme", from: "meme_id" }] } }),
    base("meme_category", { ...options("none", "*"), candidate_source: "meme_categories" }),
    base("meme_id", { ...options("none", "*"), candidate_source: { question: "meme_category" } }),
    base("pick", { ...options("*", "skip"), candidate_source: "recent_messages", requires_candidates: "recent_messages" }),
  ]);
  assert.deepEqual(pluginQuestions(questions, plugin).map((q) => q.id), ["use_meme", "meme_category", "meme_id"]);
  assert.deepEqual(pluginQuestions(questions, { actions: [], catalogs: [] }), []);
});
