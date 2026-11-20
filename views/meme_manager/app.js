import { probabilityRows } from "./log-probabilities.mjs";

const bridge = window.AstrBotPluginView;

const SCENE_LABELS = {
  group_passive: "群聊",
  private_passive: "私聊",
  group_proactive: "群聊冷场",
  private_proactive: "私聊冷场",
};

const ACTION_LABELS = {
  none: "不发言",
  reply: "回复",
  proactive: "主动开口",
};

const SAVE_MESSAGES = {
  clean: "已与插件配置一致",
  dirty: "有未保存的更改",
  saving: "正在保存…",
  saved: "已保存到插件配置，重载插件后同样生效",
  error: "保存失败",
  notice: "",
};

const TYPE_LABELS = { choice: "选择题", boolean: "是否题" };

// 专门用途的选项 ID 固定，旁边配一句人话说明。
const FIXED_OPTION_LABELS = {
  action: { none: "保持沉默", reply: "发言", proactive: "冷场时主动开口" },
  target: { latest: "最近一条消息", "*": "每条候选消息" },
  meme: { none: "不配图", "*": "每张候选表情" },
};

const PROMPT_TOKENS = ["当前时间", "冷场时长"];

const ICONS = {
  up: '<path d="m6 15 6-6 6 6"/>',
  down: '<path d="m6 9 6 6 6-6"/>',
  chevron: '<path d="m9 6 6 6-6 6"/>',
  trash:
    '<path d="M4 7h16"/><path d="M10 11v6M14 11v6"/><path d="M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12"/><path d="M9 7V4h6v3"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  arrow: '<path d="M4 12h15"/><path d="m13 6 6 6-6 6"/>',
  image:
    '<rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="m21 16-5-5-9 8"/>',
  hint: '<path d="M21 15a2 2 0 0 1-2 2H8l-5 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
  zap: '<path d="M13 2 4 14h7l-1 8 9-12h-7l1-8z"/>',
};

// ---------- 小工具 ----------

function $(id) {
  return document.getElementById(id);
}

const PROPERTY_KEYS = new Set(["value", "checked", "disabled", "hidden", "rows", "readOnly"]);

function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") {
      el.addEventListener(key.slice(2), value);
    } else if (PROPERTY_KEYS.has(key)) el[key] = value;
    else el.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false || child === "") continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

function cls(...names) {
  return names.filter(Boolean).join(" ");
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  svg.innerHTML = ICONS[name] || "";
  return svg;
}

function iconButton(name, label, onClick, { disabled = false, danger = false, act } = {}) {
  return h(
    "button",
    {
      type: "button",
      class: cls("icon-btn", danger && "is-danger"),
      title: label,
      "aria-label": label,
      disabled,
      dataset: act ? { act } : undefined,
      onclick: onClick,
    },
    icon(name),
  );
}

// 第一次点击变成「确认删除」，几秒内再点一次才执行。
function confirmButton(label, onConfirm, { onArm, onDisarm } = {}) {
  let timer = 0;
  const button = iconButton("trash", label, () => {
    if (button.classList.contains("is-armed")) {
      window.clearTimeout(timer);
      onConfirm();
      return;
    }
    button.classList.add("is-armed");
    button.replaceChildren("确认删除");
    button.setAttribute("aria-label", `再次点击确认：${label}`);
    onArm?.();
    timer = window.setTimeout(() => {
      button.classList.remove("is-armed");
      button.replaceChildren(icon("trash"));
      button.setAttribute("aria-label", label);
      onDisarm?.();
    }, 4000);
  }, { danger: true, act: "delete" });
  return button;
}

const NEEDS_AUTOSIZE = !globalThis.CSS?.supports?.("field-sizing", "content");

function autosize(el) {
  if (!NEEDS_AUTOSIZE || !el?.matches?.("textarea.autosize")) return;
  el.style.height = "auto";
  el.style.height = `${el.scrollHeight + 2}px`;
}

function nextId(prefix, used) {
  let index = 1;
  while (used.has(`${prefix}${index}`)) index += 1;
  return `${prefix}${index}`;
}

function moveItem(list, index, delta) {
  const next = index + delta;
  if (next < 0 || next >= list.length) return false;
  const [item] = list.splice(index, 1);
  list.splice(next, 0, item);
  return true;
}

function normalizeAction(action) {
  return action === "join" ? "reply" : action;
}

const percent = new Intl.NumberFormat("zh-CN", { style: "percent", maximumFractionDigits: 2 });
const roughPercent = new Intl.NumberFormat("zh-CN", { style: "percent", maximumFractionDigits: 1 });

// ---------- 状态 ----------

const state = {
  questions: [],
  defaults: [], roles: {}, tools: {},
  saved: { mode: "model", context: "", questions: [] },
  savedSnapshot: "",
  saving: false,
  fresh: null,
  loaded: false,
  // 展开的问题以对象记录，改名和排序后保持展开。
  open: new Set(),
  // 读到的配置里每道题的序列化结果，用来标出新增和修改过的题。
  baseline: new Map(),
  baselineOrder: [],
  logs: [],
  logFilter: "all",
};

function getMode() {
  return document.querySelector('input[name="decision-mode"]:checked')?.value === "random" ? "random" : "model";
}

function setMode(mode) {
  for (const input of document.querySelectorAll('input[name="decision-mode"]')) {
    input.checked = input.value === mode;
  }
}

function normalizeQuestion(raw) {
  const type = raw?.type === "boolean" ? "boolean" : "choice";
  const question = {
    id: String(raw?.id || "").trim(),
    enabled: raw?.enabled !== false,
    type,
    instructions: String(raw?.instructions || ""),
    options: [],
    role: raw?.role || "answer",
    decision_mode: raw?.decision_mode === "random" || raw?.decision_mode === "model" ? raw.decision_mode : "inherit",
    stage: raw?.stage || "pre_reply",
    use_threshold: !!raw?.use_threshold,
    ask_private: !!raw?.ask_private,
    proactive_instructions: raw?.proactive_instructions || "",
    proactive_none_criteria: raw?.proactive_none_criteria || "",
    true_tool: raw?.true_tool || "", false_tool: raw?.false_tool || "",
    true_session_hint: String(raw?.true_session_hint || ""),
    false_session_hint: String(raw?.false_session_hint || ""),
    true_weight: raw?.true_weight ?? 1,
    false_weight: raw?.false_weight ?? 1,
  };
  if (type === "choice") {
    const options = Array.isArray(raw?.options) ? raw.options : [];
    question.options = options.map((option) => ({
      id: String(option?.id || "").trim(),
      criteria: String(option?.criteria || ""),
      session_hint: String(option?.session_hint || ""),
      weight: option?.weight ?? 1, tool: option?.tool || "",
    }));
    ensureTwoOptions(question);
  }
  return question;
}

function ensureTwoOptions(question) {
  question.options = question.options || [];
  const used = new Set(question.options.map((item) => item.id));
  while (question.options.length < 2) {
    const id = nextId("option_", used);
    used.add(id);
    question.options.push({ id, criteria: "", session_hint: "", weight: 1, tool: "" });
  }
}

function setQuestions(raw) {
  state.questions = raw.map(normalizeQuestion);
  // 编辑期间引用问题对象，改名和排序后仍指向同一道题。
  const byId = new Map(state.questions.map(q => [q.id, q]));
  state.questions.forEach((q, index) => {
    const branches = raw[index].branches || {};
    if (q.type === "boolean") {
      q.true_next = byId.get(branches.true);
      q.false_next = byId.get(branches.false);
    } else {
      for (const option of q.options) option.next = byId.get(branches[option.id]);
    }
  });
}

function branchLinks(question) {
  const links = question.type === "boolean"
    ? [["true", question.true_next], ["false", question.false_next]]
    : question.options.map(o => [o.id.trim(), o.next]);
  return links.filter(([, target]) => target && state.questions.includes(target));
}

function parentsOf(question) {
  return state.questions.flatMap((parent) =>
    branchLinks(parent).filter(([, target]) => target === question).map(([key]) => [parent, key]));
}

function collectQuestionsFromForm() {
  return state.questions.map(serializeQuestion);
}

function serializeQuestion(question) {
  const common = {id: question.id.trim(), role: question.role, enabled: question.enabled,
    type: question.type, instructions: question.instructions, use_threshold: question.use_threshold};
  if (question.decision_mode !== "inherit") common.decision_mode = question.decision_mode;
  if (question.stage === "post_reply") common.stage = question.stage;
  const branches = Object.fromEntries(branchLinks(question).map(([key, target]) => [key, target.id.trim()]));
  if (Object.keys(branches).length) common.branches = branches;
  if (question.role === "action") Object.assign(common, {ask_private: question.ask_private,
    proactive_instructions: question.proactive_instructions, proactive_none_criteria: question.proactive_none_criteria});
  if (question.type === "boolean") {
    return {
      ...common, true_tool: question.true_tool, false_tool: question.false_tool,
      true_session_hint: question.true_session_hint,
      false_session_hint: question.false_session_hint,
      true_weight: question.true_weight,
      false_weight: question.false_weight,
    };
  }
  return {
    ...common,
    options: (question.options || []).map((option) => ({
      id: String(option.id || "").trim(),
      criteria: option.criteria,
      session_hint: option.session_hint,
      weight: option.weight, tool: option.tool || "",
    })),
  };
}

// ---------- 改动追踪 ----------

function captureBaseline() {
  state.baseline = new Map(state.questions.map((q) => [q, { id: q.id.trim(), json: JSON.stringify(serializeQuestion(q)) }]));
  state.baselineOrder = [...state.questions];
}

// 恢复默认问题会换成新对象；ID 与已保存的题相同时沿用它的基准，避免整页都标成新增。
function rebindBaseline() {
  const orphans = new Map();
  for (const [q, base] of state.baseline) if (!state.questions.includes(q)) orphans.set(base.id, q);
  for (const q of state.questions) {
    if (state.baseline.has(q)) continue;
    const old = orphans.get(q.id.trim());
    if (!old) continue;
    orphans.delete(q.id.trim());
    state.baseline.set(q, state.baseline.get(old));
    state.baseline.delete(old);
    state.baselineOrder[state.baselineOrder.indexOf(old)] = q;
  }
}

function changeOf(question) {
  const base = state.baseline.get(question);
  if (!base) return "new";
  return base.json === JSON.stringify(serializeQuestion(question)) ? "" : "changed";
}

const CHANGE_LABELS = { new: "新增", changed: "已修改" };

function describeChanges() {
  const parts = [];
  if (getMode() !== state.saved.mode) parts.push("决策模式");
  if ($("custom-context").value.trim() !== String(state.saved.context || "").trim()) parts.push("背景信息");
  const counts = { new: 0, changed: 0 };
  for (const q of state.questions) {
    const change = changeOf(q);
    if (change) counts[change] += 1;
  }
  const removed = [...state.baseline.keys()].filter((q) => !state.questions.includes(q)).length;
  if (counts.changed) parts.push(`修改 ${counts.changed} 个问题`);
  if (counts.new) parts.push(`新增 ${counts.new} 个问题`);
  if (removed) parts.push(`删除 ${removed} 个问题`);
  const kept = state.questions.filter((q) => state.baseline.has(q));
  const order = state.baselineOrder.filter((q) => state.questions.includes(q));
  if (kept.some((q, i) => q !== order[i])) parts.push("调整顺序");
  return parts.length ? `未保存：${parts.join("、")}` : SAVE_MESSAGES.dirty;
}

function refreshChangeMarks() {
  for (const card of $("questions").querySelectorAll(".question[data-q]")) {
    const question = state.questions[Number(card.dataset.q)];
    if (!question) continue;
    const change = changeOf(question);
    card.classList.toggle("is-changed", !!change);
    const mark = card.querySelector(".q-change");
    mark.hidden = !change;
    mark.textContent = CHANGE_LABELS[change] || "";
    mark.dataset.change = change;
  }
}

function snapshot() {
  return JSON.stringify({
    mode: getMode(),
    context: $("custom-context").value.trim(),
    questions: collectQuestionsFromForm(),
  });
}

function isDirty() {
  return state.loaded && snapshot() !== state.savedSnapshot;
}

// ---------- 保存栏 ----------

function setSaveState(kind, text) {
  $("savebar").dataset.state = kind;
  $("config-msg").textContent = text || SAVE_MESSAGES[kind] || "";
  $("discard").hidden = !(kind === "dirty" || kind === "notice" || kind === "error") || !isDirty();
  // 判断配置没读到时禁止保存，避免用空表单覆盖已有配置。
  $("save-config").disabled = kind === "saving" || !state.loaded;
}

function updateDirty() {
  if (state.saving) return;
  const current = $("savebar").dataset.state;
  if (isDirty()) {
    setSaveState("dirty", describeChanges());
  } else if (current === "dirty" || current === "error") {
    setSaveState("clean");
  }
}

// ---------- 问题的文字说明 ----------

function qName(question) {
  return question.id.trim() || "未命名问题";
}

function qIndexOf(question) {
  return state.questions.indexOf(question);
}

function roleLabel(question) {
  return state.roles[question.role] || question.role;
}

function targetTitle(question) {
  const parts = [qName(question)];
  if (question.role !== "answer") parts.push(`· ${roleLabel(question)}`);
  if (question.stage === "post_reply") parts.push("（回复后）");
  if (!question.enabled) parts.push("（已停用）");
  return parts.join(" ");
}

// 页面上显示问题名的地方都带 data-name-of，改 ID 时就地刷新。
function nameRef(question, tag = "code") {
  return h(tag, { dataset: { nameOf: qIndexOf(question) } }, qName(question));
}

function answerLabel(question, key) {
  if (question.type === "boolean") return key === "true" ? "是" : "否";
  if (key === "*") return question.role === "meme" ? "任一表情" : "任一消息";
  return key || "未命名选项";
}

// 每个答案的权重、效果和后继，选择题与是否题统一成同一种形状。
function answerSlots(question) {
  if (question.type === "boolean") {
    return ["true", "false"].map((side) => ({
      key: side, owner: question, weightKey: `${side}_weight`,
      hintKey: `${side}_session_hint`, toolKey: `${side}_tool`, nextKey: `${side}_next`,
    }));
  }
  return question.options.map((option, index) => ({
    key: option.id.trim(), owner: option, index, weightKey: "weight",
    hintKey: "session_hint", toolKey: "tool", nextKey: "next",
  }));
}

function weightShares(question) {
  const slots = answerSlots(question);
  const weights = slots.map((slot) => slot.owner[slot.weightKey]);
  const valid = weights.every((w) => Number.isFinite(w) && w >= 0 && w <= 100);
  const total = valid ? weights.reduce((sum, w) => sum + w, 0) : 0;
  return weights.map((w) => (total > 0 ? w / total : null));
}

function showsShares(question) {
  // 回复目标和表情的 * 会展开成多个候选，控制发言按场景只取部分选项，这几种不显示固定比例。
  return (question.decision_mode === "random" || (question.decision_mode === "inherit" && getMode() === "random"))
    && question.role === "answer";
}

function shareText(share) {
  if (share === null) return "—";
  if (share === 0) return "不会抽中";
  return `抽中约 ${roughPercent.format(share)}`;
}

// ---------- 校验（与后端 strict 规则一致） ----------

function weightError(value) {
  return !Number.isFinite(value) || value < 0 || value > 100;
}

function questionIssue(question) {
  const label = question.id.trim() ? `问题「${question.id.trim()}」` : "这个问题";
  if (!question.id.trim()) return { field: "id", message: "还没有填写问题 ID" };
  if (!question.instructions.trim()) {
    return { field: "instructions", message: `${label}还没有填写要问判断模型的问题` };
  }
  if (question.type === "boolean") {
    for (const side of ["true", "false"]) {
      if (weightError(question[`${side}_weight`])) {
        return { o: side, field: "weight", message: `${label}的权重必须是 0 到 100 之间的数字` };
      }
    }
    if (question.true_weight === 0 && question.false_weight === 0) {
      return { o: "true", field: "weight", message: `${label}至少需要一个权重大于 0 的答案` };
    }
    return null;
  }
  const optionIds = new Set();
  for (const [o, option] of question.options.entries()) {
    const id = option.id.trim();
    const scene = question.role === "action" && id === "proactive" ? "proactive" : "passive";
    if (!id) return { o, scene, field: "opt-id", message: `${label}的选项 ${o + 1} 还没有填写 ID` };
    if (optionIds.has(id)) return { o, scene, field: "opt-id", message: `${label}的选项 ID「${id}」重复了` };
    optionIds.add(id);
    if (!option.criteria.trim()) {
      return { o, scene, field: "opt-criteria", message: `${label}的选项「${id}」还没有填写选择标准` };
    }
    if (weightError(option.weight)) {
      return { o, scene, field: "weight", message: `${label}的选项「${id}」权重必须是 0 到 100 之间的数字` };
    }
  }
  if (!question.options.some((option) => option.weight > 0)) {
    return { o: 0, field: "weight", message: `${label}至少需要一个权重大于 0 的选项` };
  }
  if (question.role === "action") {
    const weight = (id) => question.options.find((option) => option.id.trim() === id)?.weight ?? 0;
    for (const [active, scene, text] of [["reply", "passive", "收到消息时"], ["proactive", "proactive", "冷场时"]]) {
      if (!(weight("none") > 0 || weight(active) > 0)) {
        return { o: question.options.findIndex((option) => option.id.trim() === "none"), scene, field: "weight",
          message: `${label}在${text}至少需要一个权重大于 0 的选项` };
      }
    }
  }
  return null;
}

function findCycle() {
  const visiting = new Set();
  const done = new Set();
  const path = [];
  function walk(question) {
    if (done.has(question)) return null;
    if (visiting.has(question)) return [...path.slice(path.indexOf(question)), question];
    visiting.add(question);
    path.push(question);
    for (const [, target] of branchLinks(question)) {
      const cycle = walk(target);
      if (cycle) return cycle;
    }
    path.pop();
    visiting.delete(question);
    done.add(question);
    return null;
  }
  for (const question of state.questions) {
    const cycle = walk(question);
    if (cycle) return cycle;
  }
  return null;
}

function validate() {
  const used = new Set();
  const roles = new Map();
  for (const [q, question] of state.questions.entries()) {
    const issue = questionIssue(question);
    if (issue) return { q, ...issue, message: issue.message.replace(/^还没有/, `第 ${q + 1} 个问题还没有`) };
    const id = question.id.trim();
    if (used.has(id)) return { q, field: "id", message: `问题 ID「${id}」重复了` };
    used.add(id);
    if (question.enabled && question.role !== "answer") {
      if (roles.has(question.role)) {
        return { q, field: "role", message: `「${roleLabel(question)}」只能启用一个问题，${roles.get(question.role)} 已经在用` };
      }
      roles.set(question.role, id);
    }
    if (question.stage === "post_reply") {
      for (const slot of answerSlots(question)) {
        const target = slot.owner[slot.nextKey];
        if (target && state.questions.includes(target) && target.stage !== "post_reply") {
          return { q, o: slot.index ?? slot.key, field: "next",
            message: `回复后的问题「${id}」不能接着问回复前的问题「${qName(target)}」` };
        }
      }
    }
  }
  const cycle = findCycle();
  if (cycle) {
    return { q: qIndexOf(cycle[0]), field: "next",
      message: `「接着问」形成了循环：${cycle.map(qName).join(" → ")}` };
  }
  return null;
}

function markInvalid(error) {
  const question = state.questions[error.q];
  if (!question) return;
  if (question.role === "action") question._scene = error.scene === "proactive" ? "proactive" : "passive";
  state.open.add(question);
  renderQuestions();
  const card = $("questions").querySelector(`[data-q="${error.q}"]`);
  const scope = error.o === undefined ? card : card?.querySelector(`[data-o="${error.o}"]`);
  const field = scope?.querySelector(`[data-field="${error.field}"]`) || card;
  if (!field) return;
  field.classList.add("is-invalid");
  field.scrollIntoView({ block: "center", behavior: "smooth" });
  field.focus?.({ preventScroll: true });
}

// ---------- 判断流程 ----------

function renderFlow() {
  const box = $("flow");
  box.hidden = !state.questions.length;
  if (box.hidden) return;
  const children = new Set(state.questions.flatMap(q => branchLinks(q).map(([, target]) => target)));
  const shown = new Set();
  function node(question, edge) {
    const repeated = shown.has(question);
    const item = h("li", null,
      edge && h("span", { class: "flow-edge" }, edge, icon("arrow")),
      h("button", {
        type: "button",
        class: cls("flow-node", !question.enabled && "is-off", question.stage === "post_reply" && "is-post",
          { new: "is-changed is-added", changed: "is-changed" }[changeOf(question)]),
        title: question.instructions.trim() || undefined,
        onclick: () => revealQuestion(question),
      },
        h("span", { class: "flow-num" }, qIndexOf(question) + 1),
        nameRef(question),
        h("span", { class: "flow-meta" }, question.role === "answer" ? TYPE_LABELS[question.type] : roleLabel(question)),
        question.stage === "post_reply" && h("span", { class: "q-tag is-post" }, "回复后"),
        !question.enabled && h("span", { class: "q-tag is-off" }, "已停用"),
        repeated && h("span", { class: "flow-meta" }, "（同上）"),
      ));
    if (repeated) return item;
    shown.add(question);
    const links = branchLinks(question);
    if (links.length) {
      item.append(h("ul", null, links.map(([key, target]) => node(target, answerLabel(question, key)))));
    }
    return item;
  }
  const roots = state.questions.filter(q => !children.has(q));
  const groups = [
    ["回复前判断", roots.filter(q => q.stage !== "post_reply")],
    ["会话模型写完回复后判断", roots.filter(q => q.stage === "post_reply")],
  ].filter(([, list]) => list.length);
  const body = $("flow-body");
  body.replaceChildren(...groups.map(([title, list]) => h("div", { class: "flow-group" },
    groups.length > 1 && h("p", { class: "flow-group-title" }, title),
    h("ul", { class: "flow-tree" }, list.map(q => node(q))))));
  // 尚未断开的循环里没有入口，也要显示出来。
  const orphans = state.questions.filter(q => !shown.has(q));
  if (orphans.length) {
    body.append(h("div", { class: "flow-group" },
      h("p", { class: "flow-group-title is-danger" }, "这些问题互相「接着问」形成了循环，没有入口"),
      h("ul", { class: "flow-tree" }, orphans.filter(q => !shown.has(q)).map(q => node(q)))));
  }
}

function revealQuestion(question) {
  state.open.add(question);
  const index = qIndexOf(question);
  renderQuestions(`[data-q="${index}"] .q-toggle`);
  $("questions").querySelector(`[data-q="${index}"]`)?.scrollIntoView({ block: "start", behavior: "smooth" });
}

function renderStats() {
  const total = state.questions.length;
  const enabled = state.questions.filter(q => q.enabled).length;
  const children = new Set(state.questions.flatMap(q => branchLinks(q).map(([, target]) => target)));
  const entries = state.questions.filter(q => q.enabled && !children.has(q)).length;
  $("question-stats").textContent = total
    ? `共 ${total} 个问题 · ${enabled} 个启用 · ${entries} 个入口`
    : "";
  const allOpen = total > 0 && state.questions.every(q => state.open.has(q));
  const toggle = $("toggle-all");
  toggle.hidden = !total;
  toggle.textContent = allOpen ? "全部收起" : "全部展开";
}

// ---------- 问题卡片 ----------

function renderQuestions(focus) {
  const root = $("questions");
  if (!state.questions.length) {
    root.replaceChildren(h("li", { class: "questions-empty" },
      h("b", null, "还没有问题"),
      h("span", null, "没有问题时，群聊和冷场都保持沉默，私聊仍直接回复。可以从下方添加问题，或恢复默认问题。")));
  } else {
    root.replaceChildren(...state.questions.map(renderQuestion));
  }
  state.fresh = null;
  renderFlow();
  renderStats();
  refreshChangeMarks();
  if (NEEDS_AUTOSIZE) root.querySelectorAll("textarea.autosize").forEach(autosize);
  if (focus) root.querySelector(focus)?.focus();
}

function toggleQuestion(question) {
  if (state.open.has(question)) state.open.delete(question);
  else state.open.add(question);
  renderQuestions(`[data-q="${qIndexOf(question)}"] .q-toggle`);
}

function renderQuestion(question, qIndex) {
  const open = state.open.has(question);
  const issue = questionIssue(question);
  const card = h("li", {
    class: cls("question", !question.enabled && "is-disabled", open && "is-open",
      state.fresh === question && "is-new", question.stage === "post_reply" && "is-post"),
    dataset: { q: qIndex, role: question.role },
  });

  const enabledText = h("span", null, question.enabled ? "启用" : "已停用");
  const enabledToggle = h("label", { class: "switch", title: "停用后不会问这个问题，也会阻断经过它的「接着问」" },
    h("input", {
      type: "checkbox",
      checked: question.enabled,
      "aria-label": `启用问题 ${qName(question)}`,
      onchange: (event) => {
        question.enabled = event.target.checked;
        enabledText.textContent = question.enabled ? "启用" : "已停用";
        card.classList.toggle("is-disabled", !question.enabled);
        renderStats();
      },
    }),
    enabledText);

  const parents = parentsOf(question);
  const toggle = h("button", {
    type: "button",
    class: "q-toggle",
    "aria-expanded": String(open),
    "aria-controls": `q-body-${qIndex}`,
    onclick: () => toggleQuestion(question),
  },
    icon("chevron"),
    h("span", { class: "q-index" }, qIndex + 1),
    h("span", { class: "q-name mono", dataset: { nameOf: qIndex } }, qName(question)),
    question.role !== "answer" && h("span", { class: "q-tag is-role" }, roleLabel(question)),
    h("span", { class: "q-tag" }, TYPE_LABELS[question.type]),
    question.decision_mode !== "inherit" && h("span", { class: "q-tag" }, question.decision_mode === "random" ? "随机" : "模型"),
    question.stage === "post_reply" && h("span", { class: "q-tag is-post" }, "回复后"),
    !open && issue && h("span", { class: "q-tag is-issue", title: issue.message }, "待完善"),
    h("span", { class: "q-change", hidden: true }),
  );

  const tools = h("div", { class: "q-tools" },
    question.role === "answer" && iconButton("copy", "复制这个问题", () => duplicateQuestion(question)),
    iconButton("up", "上移问题", () => moveQuestion(qIndex, -1, "up"), { disabled: qIndex === 0, act: "up" }),
    iconButton("down", "下移问题", () => moveQuestion(qIndex, 1, "down"), {
      disabled: qIndex === state.questions.length - 1, act: "down",
    }),
    confirmButton(`删除问题 ${qName(question)}`, () => deleteQuestion(question), {
      onArm: () => {
        if (parents.length) setSaveState("notice", `「${qName(question)}」被 ${parents.length} 个答案接着问，删除后这些分支会直接结束`);
      },
      onDisarm: () => {
        if (parents.length && $("savebar").dataset.state === "notice") setSaveState(isDirty() ? "dirty" : "clean");
      },
    }),
  );

  card.append(h("div", { class: "q-head" }, toggle, h("div", { class: "q-controls" }, enabledToggle, tools)));

  if (!open) {
    card.append(renderSummary(question, parents));
    return card;
  }
  card.append(h("div", { class: "q-body", id: `q-body-${qIndex}` },
    renderEntryLine(question, parents),
    renderPrompt(question, qIndex),
    renderSettings(question, qIndex),
    renderRoleNote(question),
    renderAnswers(question, qIndex),
  ));
  return card;
}

function renderEntry(question, parents) {
  if (!parents.length) {
    return h("span", { class: "q-entry is-root", title: "没有其他答案指向它，在本阶段开始时直接判断" },
      question.stage === "post_reply" ? "入口 · 回复写完后判断" : "入口 · 每轮开始时判断");
  }
  return h("span", { class: "q-entry" }, "当 ",
    parents.map(([parent, key], i) => [i ? " 或 " : "",
      h("b", null, nameRef(parent, "span"), ` = ${answerLabel(parent, key)}`)]),
    " 时才问");
}

// 改选项 ID 或「接着问」时，其他卡片里的进入条件随之更新；只替换文字，不动输入框。
function refreshEntries() {
  for (const card of $("questions").querySelectorAll(".question[data-q]")) {
    const question = state.questions[Number(card.dataset.q)];
    if (!question) continue;
    const parents = parentsOf(question);
    for (const el of card.querySelectorAll(".q-entry")) el.replaceWith(renderEntry(question, parents));
  }
}

function renderEntryLine(question, parents) {
  return h("p", { class: "q-entry-line" }, renderEntry(question, parents));
}

function renderSummary(question, parents) {
  const post = question.stage === "post_reply";
  const preview = question.instructions;
  const chips = answerSlots(question).map((slot) => {
    const weight = slot.owner[slot.weightKey];
    const next = slot.owner[slot.nextKey];
    const linked = next && state.questions.includes(next);
    const hint = !post && String(slot.owner[slot.hintKey] || "").trim();
    const tool = !post && slot.owner[slot.toolKey];
    return h("span", { class: cls("ans-chip", weight === 0 && "is-zero") },
      h("b", null, answerLabel(question, slot.key)),
      Number.isFinite(weight) && weight !== 1 && h("small", null, `×${weight}`),
      hint && h("span", { class: "ans-mark is-hint", title: `会话提示：${hint}` }, icon("hint")),
      tool && h("span", { class: "ans-mark is-tool", title: `调用工具：${state.tools[tool] || tool}` }, icon("zap")),
      linked && [h("span", { class: "ans-next", "aria-label": "接着问" }, icon("arrow")), nameRef(next)],
    );
  });
  return h("div", { class: "q-summary", onclick: () => toggleQuestion(question) },
    h("p", { class: cls("q-preview", !preview.trim() && "is-empty") },
      preview.trim() || "还没有填写要问判断模型的问题"),
    h("div", { class: "q-chips" }, renderEntry(question, parents), h("span", { class: "q-chips-sep", "aria-hidden": "true" }), chips),
  );
}

function promptField(question, key, label, placeholder, qIndex) {
  const id = `q${qIndex}-${key}`;
  const input = h("textarea", {
    id, class: "autosize", rows: 2, value: question[key],
    placeholder, dataset: { field: key },
    oninput: () => { question[key] = input.value; },
  });
  const tokens = h("span", { class: "token-bar" }, "插入",
    PROMPT_TOKENS.map(name => h("button", {
      type: "button", class: "token",
      title: `判断模型会看到本轮的实际${name}`,
      "aria-label": `在${label}中插入${name}`,
      onclick: () => {
        input.setRangeText(`{${name}}`, input.selectionStart, input.selectionEnd, "end");
        input.focus();
        input.dispatchEvent(new Event("input", { bubbles: true }));
      },
    }, `{${name}}`)));
  return h("div", { class: "q-prompt" },
    h("div", { class: "field-row" }, h("label", { class: "field-label is-judge", for: id }, label), tokens),
    input);
}

function segmented(label, choices, selected, onPick, { disabled = {}, extra = "" } = {}) {
  return h("div", { class: cls("seg", extra), role: "group", "aria-label": label },
    choices.map(([value, text]) => h("button", {
      type: "button",
      "aria-pressed": String(value === selected),
      disabled: !!disabled[value],
      title: disabled[value] || undefined,
      onclick: () => { if (value !== selected) onPick(value); },
    }, text)));
}

function renderPrompt(question, qIndex) {
  if (question.role !== "action") {
    const example = question.stage === "post_reply"
      ? "例如：实际回复是否控制在两句话以内？"
      : "例如：这轮回复应该用什么语气？";
    return promptField(question, "instructions", "问判断模型", example, qIndex);
  }
  const proactive = question._scene === "proactive";
  const scene = segmented("编辑哪个场景", [["passive", "收到消息时"], ["proactive", "冷场时"]],
    proactive ? "proactive" : "passive", (value) => {
      question._scene = value;
      renderQuestions(`[data-q="${qIndex}"] .scene-seg [aria-pressed="true"]`);
    }, { extra: "scene-seg" });
  return h("div", { class: "q-scene" },
    h("div", { class: "scene-head" }, scene,
      h("span", { class: "hint" }, proactive
        ? "会话沉默一段时间后判断要不要主动开口，会自动补充当前时间和冷场时长。"
        : "收到群聊消息时判断要不要发言；私聊默认直接回复。")),
    proactive
      ? promptField(question, "proactive_instructions", "冷场时问判断模型", "留空则沿用「收到消息时」的问题", qIndex)
      : promptField(question, "instructions", "收到消息时问判断模型", "例如：现在适合接话吗？", qIndex));
}

function fieldSelect(label, choices, selected, onChange, { field, id } = {}) {
  const input = h("select", { id, dataset: field ? { field } : undefined, onchange: (e) => onChange(e.target.value) },
    Object.entries(choices).map(([key, text]) => h("option", { value: key }, text)));
  input.value = selected;
  return h("label", { class: "setting" }, h("span", { class: "field-label" }, label), input);
}

function switchField(label, checked, onChange, note) {
  return h("label", { class: "switch switch-row" },
    h("input", { type: "checkbox", checked, onchange: (e) => onChange(e.target.checked) }),
    h("span", null, label, note && h("small", null, note)));
}

function renderSettings(question, qIndex) {
  const isAnswer = question.role === "answer";
  const post = question.stage === "post_reply";
  const idInput = h("input", {
    type: "text", class: "mono", value: question.id, placeholder: "如 tone",
    spellcheck: "false", autocomplete: "off", dataset: { field: "id" },
    oninput: (event) => {
      question.id = event.target.value;
      for (const el of document.querySelectorAll(`[data-name-of="${qIndex}"]`)) {
        el.textContent = el.tagName === "OPTION" ? targetTitle(question) : qName(question);
      }
    },
  });
  const roleSelect = fieldSelect("用途", post ? { answer: state.roles.answer } : state.roles, question.role, (value) => {
    question.role = value;
    if (value !== "answer") {
      const preset = state.defaults.find(q => q.role === value);
      question.type = "choice";
      question.stage = "pre_reply";
      question.options = preset.options.map(o => ({ ...(question.options.find(old => old.id === o.id) || o) }));
      if (value === "action" && !question.proactive_instructions) {
        Object.assign(question, {
          proactive_instructions: preset.proactive_instructions || "",
          proactive_none_criteria: preset.proactive_none_criteria || "",
        });
      }
    }
    renderQuestions(`[data-q="${qIndex}"] [data-field="role"]`);
    updateDirty();
  }, { field: "role" });

  const typeSeg = segmented("题型", [["choice", "选择题"], ["boolean", "是否题"]], question.type, (type) => {
    question.type = type;
    if (type === "choice") ensureTwoOptions(question);
    renderQuestions(`[data-q="${qIndex}"] .type-seg [aria-pressed="true"]`);
    updateDirty();
  }, { disabled: isAnswer ? {} : { boolean: "这个用途只支持选择题" }, extra: "type-seg" });

  const stageSeg = segmented("判断时机", [["pre_reply", "回复前"], ["post_reply", "回复写完后"]], question.stage, (stage) => {
    question.stage = stage;
    renderQuestions(`[data-q="${qIndex}"] .stage-seg [aria-pressed="true"]`);
    updateDirty();
  }, { disabled: isAnswer ? {} : { post_reply: "只有「记录答案」可以在回复写完后判断" }, extra: "stage-seg" });

  const modeSelect = fieldSelect("本题决策", {
    inherit: "继承默认模式", model: "模型判断", random: "按权重随机",
  }, question.decision_mode, (mode) => {
    question.decision_mode = mode;
    renderQuestions(`[data-q="${qIndex}"] [data-field="decision-mode"]`);
    updateDirty();
  }, { field: "decision-mode" });

  const thresholdNote = question.type === "boolean"
    ? "仅模型判断。两侧权重都是 1 时，「是」的概率达到阈值才判为是；调整权重后取加权概率较高的一侧。不按温度抽样。"
    : "仅模型判断。选中答案的原始概率低于插件判断阈值时，按没有选中处理。";

  return h("div", { class: "q-settings" },
    h("div", { class: "settings-grid" },
      h("label", { class: "setting" }, h("span", { class: "field-label" }, "问题 ID"), idInput),
      roleSelect,
      h("div", { class: "setting" }, h("span", { class: "field-label" }, "题型"), typeSeg),
      h("div", { class: "setting" }, h("span", { class: "field-label" }, "判断时机"), stageSeg),
      modeSelect,
    ),
    h("div", { class: "settings-switches" },
      switchField("使用插件判断阈值", question.use_threshold, (v) => { question.use_threshold = v; }, thresholdNote),
      question.role === "action" && switchField("私聊也判断是否发言", question.ask_private,
        (v) => { question.ask_private = v; }, "关闭时私聊消息直接回复，不问这道题。"),
    ),
    post && h("p", { class: "note" }, "回复写完后判断：模型会读到本轮回复正文和此前的答案。这里只记录答案并决定接着问什么，不再给会话模型提示，也不调用工具。"),
  );
}

function renderRoleNote(question) {
  const notes = {
    target: "只在群聊收到消息时判断。「*」会展开成本轮每条候选消息，选择标准里可以用 {描述} 引用消息摘要。",
    meme: "「*」会展开成图库里每张带描述的表情，选择标准里可以用 {描述} 引用表情描述。",
  };
  return notes[question.role] ? h("p", { class: "note" }, notes[question.role]) : null;
}

function renderAnswers(question, qIndex) {
  const post = question.stage === "post_reply";
  const fixedRows = question.role !== "answer" || question.type === "boolean";
  const wrap = h("div", { class: cls("answers", fixedRows && "is-fixed") },
    h("div", { class: "ans-row ans-head", "aria-hidden": "true" },
      h("span", { class: "ans-col-judge" }, question.type === "boolean" ? "判断模型的回答" : "选项 · 判断模型看到"),
      h("span", { class: "ans-col-weight", title: "默认 1。调高更偏向这一项，0 表示不选" }, "权重"),
      h("span", { class: "ans-col-effect" }, post ? "选中后" : "选中后 · 会话模型收到")));
  const shares = showsShares(question) ? weightShares(question) : null;
  if (question.type === "boolean") {
    ["true", "false"].forEach((side, i) => wrap.append(booleanRow(question, qIndex, side, shares?.[i])));
    return wrap;
  }
  const proactive = question.role === "action" && question._scene === "proactive";
  const editable = question.role === "answer";
  question.options.forEach((option, oIndex) => {
    if (question.role === "action" && option.id === (proactive ? "reply" : "proactive")) return;
    wrap.append(choiceRow(question, qIndex, option, oIndex, shares?.[oIndex], proactive));
  });
  if (question.role === "action") {
    wrap.append(h("p", { class: "note" }, "「none」的选中后效果和权重在两个场景共用。"));
  }
  if (editable) {
    wrap.append(h("button", {
      type: "button",
      class: "btn btn-add-opt",
      onclick: () => {
        const used = new Set(question.options.map((item) => item.id));
        question.options.push({ id: nextId("option_", used), criteria: "", session_hint: "", weight: 1, tool: "" });
        renderQuestions(`[data-q="${qIndex}"] [data-o="${question.options.length - 1}"] [data-field="opt-id"]`);
        updateDirty();
      },
    }, icon("plus"), "添加选项"));
  }
  return wrap;
}

function weightCell(question, qIndex, owner, key, label, share) {
  return h("div", { class: "ans-weight" },
    h("input", {
      type: "number", min: 0, max: 100, step: "any", value: owner[key] ?? 1,
      "aria-label": label, dataset: { field: "weight" },
      oninput: (event) => {
        owner[key] = event.target.valueAsNumber;
        refreshShares(question, qIndex);
      },
    }),
    share !== undefined && h("small", { class: "ans-share", dataset: { share: "" } }, shareText(share)));
}

function refreshShares(question, qIndex) {
  if (!showsShares(question)) return;
  const shares = weightShares(question);
  const slots = answerSlots(question);
  const card = $("questions").querySelector(`[data-q="${qIndex}"]`);
  slots.forEach((slot, i) => {
    const cell = card?.querySelector(`[data-o="${slot.index ?? slot.key}"] [data-share]`);
    if (cell) cell.textContent = shareText(shares[i]);
  });
}

function effectsCell(question, qIndex, owner, keys, rowKey, label) {
  const post = question.stage === "post_reply";
  const toolChoices = { "": "不调用", ...state.tools };
  return h("div", { class: "ans-effects" },
    !post && h("textarea", {
      class: "ans-hint autosize", rows: 1, value: owner[keys.hint] || "",
      placeholder: "给会话模型的提示（可选）",
      "aria-label": `${label}时给会话模型的提示`,
      oninput: (event) => { owner[keys.hint] = event.target.value; },
    }),
    h("div", { class: "effect-row" },
      branchField(question, qIndex, owner, keys.next, rowKey),
      !post && Object.keys(state.tools).length > 0 && h("label", { class: "effect-field" },
        h("span", null, "调用工具"),
        (() => {
          const select = h("select", { onchange: (e) => { owner[keys.tool] = e.target.value; } },
            Object.entries(toolChoices).map(([value, text]) => h("option", { value }, text)));
          select.value = owner[keys.tool] || "";
          return select;
        })()),
    ));
}

function branchField(question, qIndex, owner, key, rowKey) {
  const post = question.stage === "post_reply";
  const select = h("select", {
    dataset: { field: "next" },
    onchange: (event) => {
      const value = event.target.value;
      if (value === "__new") {
        createSuccessor(question, owner, key);
        return;
      }
      owner[key] = value === "" ? undefined : state.questions[Number(value)];
      renderQuestions(`[data-q="${qIndex}"] [data-o="${rowKey}"] [data-field="next"]`);
      updateDirty();
    },
  }, h("option", { value: "" }, "不再追问"));
  state.questions.forEach((target, index) => {
    if (target === question) return;
    select.append(h("option", {
      value: String(index), dataset: { nameOf: index },
      disabled: post && target.stage !== "post_reply",
    }, targetTitle(target)));
  });
  select.append(h("option", { value: "__new" }, "＋ 新建一个问题…"));
  const selected = state.questions.indexOf(owner[key]);
  select.value = selected < 0 ? "" : String(selected);
  return h("label", { class: cls("effect-field", selected >= 0 && "is-linked") }, h("span", null, "接着问"), select);
}

function choiceRow(question, qIndex, option, oIndex, share, proactiveScene) {
  const fixed = question.role !== "answer";
  const usesProactiveNone = proactiveScene && option.id === "none";
  const label = `选项「${option.id || oIndex + 1}」`;
  const idCell = fixed
    ? h("span", { class: "ans-fixed", title: FIXED_OPTION_LABELS[question.role]?.[option.id] || "" },
        h("code", null, option.id),
        h("small", null, FIXED_OPTION_LABELS[question.role]?.[option.id] || ""))
    : h("input", {
        type: "text", class: "opt-id mono", value: option.id, placeholder: "选项 ID",
        spellcheck: "false", autocomplete: "off", "aria-label": `选项 ${oIndex + 1} 的 ID`,
        dataset: { field: "opt-id" },
        oninput: (event) => { option.id = event.target.value; },
      });
  const criteria = usesProactiveNone
    ? h("textarea", {
        class: "opt-criteria autosize", rows: 1, value: question.proactive_none_criteria,
        placeholder: `留空沿用：${option.criteria}`, "aria-label": "冷场时选择 none 的标准",
        dataset: { field: "proactive-none" },
        oninput: (event) => { question.proactive_none_criteria = event.target.value; },
      })
    : h("textarea", {
        class: "opt-criteria autosize", rows: 1, value: option.criteria,
        placeholder: option.id === "*" ? "候选说明，{描述} 会换成每个候选的内容" : "什么情况下选它",
        "aria-label": `${label}的选择标准`, dataset: { field: "opt-criteria" },
        oninput: (event) => { option.criteria = event.target.value; },
      });
  const count = question.options.length;
  return h("div", { class: "ans-row", dataset: { o: oIndex } },
    h("div", { class: cls("ans-judge", fixed && "is-fixed") }, idCell, criteria),
    weightCell(question, qIndex, option, "weight", `${label}的权重`, share),
    h("span", { class: "ans-arrow", "aria-hidden": "true" }, icon("arrow")),
    effectsCell(question, qIndex, option, { hint: "session_hint", tool: "tool", next: "next" }, oIndex, `选中${label}`),
    h("div", { class: "ans-tools" }, !fixed && [
      iconButton("up", `上移${label}`, () => moveOption(question, qIndex, oIndex, -1), { disabled: oIndex === 0 }),
      iconButton("down", `下移${label}`, () => moveOption(question, qIndex, oIndex, 1), { disabled: oIndex === count - 1 }),
      iconButton("trash", count <= 2 ? "至少保留两个选项" : `删除${label}`, () => {
        if (question.options.length <= 2) return;
        question.options.splice(oIndex, 1);
        renderQuestions();
        updateDirty();
      }, { disabled: count <= 2, danger: true }),
    ]),
  );
}

function booleanRow(question, qIndex, side, share) {
  const yes = side === "true";
  const text = yes ? "是" : "否";
  return h("div", { class: "ans-row", dataset: { o: side } },
    h("div", { class: "ans-judge is-fixed" },
      h("span", { class: "ans-fixed is-bool" }, h("code", null, text)),
      h("span", { class: "ans-fixed-text" }, `判断模型回答「${text}」`)),
    weightCell(question, qIndex, question, `${side}_weight`, `「${text}」的权重`, share),
    h("span", { class: "ans-arrow", "aria-hidden": "true" }, icon("arrow")),
    effectsCell(question, qIndex, question,
      { hint: `${side}_session_hint`, tool: `${side}_tool`, next: `${side}_next` }, side, `判断为「${text}」`),
    h("div", { class: "ans-tools" }),
  );
}

// ---------- 问题操作 ----------

function moveQuestion(qIndex, delta, act) {
  if (!moveItem(state.questions, qIndex, delta)) return;
  const target = qIndex + delta;
  const edge = target === 0 || target === state.questions.length - 1;
  const fallback = act === "up" ? "down" : "up";
  renderQuestions(`[data-q="${target}"] .q-tools [data-act="${edge ? fallback : act}"]`);
  updateDirty();
}

function moveOption(question, qIndex, oIndex, delta) {
  if (!moveItem(question.options, oIndex, delta)) return;
  renderQuestions(`[data-q="${qIndex}"] [data-o="${oIndex + delta}"] [data-field="opt-criteria"]`);
  updateDirty();
}

function deleteQuestion(question) {
  const index = qIndexOf(question);
  state.questions.splice(index, 1);
  state.open.delete(question);
  const focusIndex = Math.min(index, state.questions.length - 1);
  renderQuestions(focusIndex >= 0 ? `[data-q="${focusIndex}"] .q-toggle` : undefined);
  if (isDirty()) setSaveState("dirty", `已删除「${qName(question)}」，保存后生效；撤销更改可以找回`);
  else setSaveState("clean");
}

function insertQuestion(question, index) {
  state.questions.splice(index, 0, question);
  state.open.add(question);
  state.fresh = question;
  renderQuestions(`[data-q="${index}"] [data-field="instructions"]`);
  $("questions").querySelector(`[data-q="${index}"]`)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  updateDirty();
}

function blankQuestion(template) {
  const used = new Set(state.questions.map((item) => item.id.trim()));
  const question = normalizeQuestion({
    id: nextId(template === "post" ? "check_" : "question_", used),
    type: template === "choice" ? "choice" : "boolean",
    stage: template === "post" ? "post_reply" : "pre_reply",
    options: [],
  });
  return question;
}

function createSuccessor(parent, owner, key) {
  const question = blankQuestion(parent.stage === "post_reply" ? "post" : "choice");
  owner[key] = question;
  insertQuestion(question, qIndexOf(parent) + 1);
}

function duplicateQuestion(question) {
  const [copy] = collectQuestionsFromForm().slice(qIndexOf(question), qIndexOf(question) + 1);
  const used = new Set(state.questions.map((item) => item.id.trim()));
  const fresh = normalizeQuestion({ ...copy, id: nextId(`${copy.id || "question"}_copy`, used) });
  // 复制的题保留原来的「接着问」，但不会被其他答案指向，是一个新的入口。
  for (const slot of answerSlots(question)) {
    const target = slot.owner[slot.nextKey];
    const freshSlot = answerSlots(fresh).find(s => s.key === slot.key);
    if (freshSlot && target) freshSlot.owner[freshSlot.nextKey] = target;
  }
  insertQuestion(fresh, qIndexOf(question) + 1);
}

function onAddQuestion(template) {
  $("add-menu").open = false;
  insertQuestion(blankQuestion(template), state.questions.length);
}

// ---------- 读取与保存 ----------

function updateContextCount() {
  const length = $("custom-context").value.trim().length;
  $("context-count").textContent = length ? `${length} 字` : "未填写";
}

function fillForm(saved) {
  setMode(saved.mode);
  $("custom-context").value = saved.context;
  updateContextCount();
  const open = new Set([...state.open].map(q => q.id.trim()));
  setQuestions(saved.questions);
  state.open = new Set(state.questions.filter(q => open.has(q.id)));
  captureBaseline();
  renderQuestions();
}

function applyConfig(data, fallbackQuestions) {
  if (![2, 3, 4].includes(data.version) || !Array.isArray(data.default_questions)) throw new Error("请重载插件并刷新页面");
  state.defaults = data.default_questions;
  state.roles = data.roles;
  state.tools = data.tools;
  const questions = data.decision_questions ?? fallbackQuestions;
  state.saved = {
    mode: data.decision_mode === "random" ? "random" : "model",
    context: data.decision_custom_context || "",
    questions: JSON.parse(JSON.stringify(questions)),
  };
  fillForm(state.saved);
  state.loaded = true;
  state.savedSnapshot = snapshot();
}

function showConfigWarning(text) {
  const box = $("config-warning");
  box.hidden = !text;
  box.textContent = text ? `当前配置有问题：${text}。修改后保存即可覆盖。` : "";
}

async function loadDecisionConfig() {
  const data = await bridge.apiGet("decision-config");
  applyConfig(data);
  showConfigWarning(data.warning);
  setSaveState("clean");
}

async function saveDecisionConfig() {
  if (state.saving || !state.loaded) return;
  const error = validate();
  if (error) {
    setSaveState("error", error.message);
    markInvalid(error);
    return;
  }
  const payload = {
    version: 4,
    decision_mode: getMode(),
    decision_custom_context: $("custom-context").value,
    decision_questions: collectQuestionsFromForm(),
  };
  state.saving = true;
  setSaveState("saving");
  try {
    const data = await bridge.apiPost("decision-config", payload);
    applyConfig(data, payload.decision_questions);
    showConfigWarning("");
    state.saving = false;
    setSaveState("saved");
  } catch (err) {
    state.saving = false;
    setSaveState("error", `保存失败：${err.message}`);
  }
}

// ---------- 状态与表情包管理器 ----------

function renderStatusChips(status) {
  const adapter = status.meme_adapter || {};
  const count = adapter.candidate_count ?? status.meme_count ?? 0;
  const chips = [
    status.enabled
      ? h("span", { class: "chip is-ok" }, "已启用")
      : h("span", { class: "chip is-off", title: "插件配置中已关闭快速决策" }, "已关闭"),
  ];
  if (status.dry_run) {
    chips.push(
      h("span", { class: "chip is-dry", title: "只记录决策，不向 QQ 发送消息" }, "试运行中"),
    );
  }
  chips.push(
    h(
      "span",
      { class: "chip is-plain" },
      icon("image"),
      adapter.available ? `${count} 张表情可用` : "只发文字",
    ),
  );
  $("status-chips").replaceChildren(...chips);
}

function renderAdapter(adapter) {
  const section = $("adapter");
  const count = adapter.candidate_count ?? 0;
  const packs = Array.isArray(adapter.packs) ? adapter.packs : [];
  const msg = $("adapter-msg");
  msg.replaceChildren();
  if (!adapter.available) {
    section.dataset.state = "error";
    $("adapter-title").textContent = "没有对接到表情包管理器，机器人只会发文字";
    msg.append(
      adapter.message ? `${adapter.message} ` : "",
      "在插件市场安装并启用「表情包管理器」（",
      h("code", null, "anka-afk/astrbot_plugin_meme_manager"),
      "）后点刷新。",
    );
    $("adapter-tips").hidden = true;
    return;
  }
  section.dataset.state = "ok";
  const version = adapter.plugin_version ? `（${adapter.plugin_version}）` : "";
  $("adapter-title").textContent = `已对接表情包管理器${version}`;
  if (count) {
    msg.append(`${packs.length || adapter.pack_count || 0} 个图包，共 ${count} 张带描述的表情供判断模型挑选。`);
  } else {
    msg.append("已对接，但还没有可用的表情。请在表情包管理器中添加图片并填写描述。");
  }
  if (packs.length) {
    msg.append(
      h("br"),
      "图包：",
      ...packs.flatMap((pack, index) => [
        index ? "、" : "",
        pack.name || pack.id,
        pack.enabled === false ? "（已停用）" : "",
        pack.present === false ? "（文件缺失）" : "",
      ]),
    );
  }
  $("adapter-tips").hidden = false;
}

async function loadStatus() {
  try {
    const status = await bridge.apiGet("status");
    renderStatusChips(status);
    renderAdapter(status.meme_adapter || {});
  } catch (error) {
    $("status-chips").replaceChildren(h("span", { class: "chip is-off" }, "状态读取失败"));
    $("adapter").dataset.state = "error";
    $("adapter-title").textContent = "读取插件状态失败";
    $("adapter-msg").textContent = `${error.message}。可以稍后点刷新重试。`;
    $("adapter-tips").hidden = true;
  }
}

// ---------- 决策日志 ----------

function splitTime(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}:\d{2}:\d{2})/.exec(String(value || ""));
  if (!match) return { day: "", clock: String(value || "—") };
  const [, year, month, date, clock] = match;
  const sameDay = (d) => Number(year) === d.getFullYear() && Number(month) === d.getMonth() + 1 && Number(date) === d.getDate();
  const now = new Date();
  const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  let day = `${Number(month)}月${Number(date)}日`;
  if (sameDay(now)) day = "今天";
  else if (sameDay(yesterday)) day = "昨天";
  else if (Number(year) !== now.getFullYear()) day = `${year}年${day}`;
  return { day, clock };
}

function formatAnswer(value) {
  if (value === true) return "是";
  if (value === false) return "否";
  if (value === null || value === undefined) return "—";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

function probabilityBar(label, value, series) {
  return h("div", { class: `prob-bar ${series}` },
    h("span", null, label),
    h("span", { class: "prob-track", "aria-hidden": "true" },
      value !== null && h("span", { style: `width: ${value * 100}%` })),
    h("span", { class: "prob-value" }, value === null ? "—" : percent.format(value)),
  );
}

function renderProbabilityChart(id, answer, isRandom) {
  const rows = probabilityRows(answer, isRandom);
  const title = h("h4", null, id);
  if (!rows.length) {
    const note = answer?.note === "private_passive_default_reply" ? "私聊固定回复，本题无抽样。" : "未记录可用的概率分布。";
    return h("section", { class: "prob-question" }, title, h("p", { class: "hint" }, note));
  }
  const isBoolean = answer.question_type === "boolean" || !answer.probabilities;
  const optionName = (key) => isBoolean ? (key === "true" ? "是" : "否") : key;
  const selected = answer.selected_choice ?? answer.choice;
  const info = [];
  if (Number.isFinite(answer.selection_temperature)) info.push(`温度 ${answer.selection_temperature}`);
  if (selected != null) {
    const label = isRandom ? "抽中" : Object.hasOwn(answer, "selected_choice") ? "调整后选中" : "模型原选";
    info.push(`${label} ${optionName(selected)}`);
    const chance = answer.selection_probabilities?.[selected];
    if (Number.isFinite(chance)) info.push(`实际抽中概率 ${percent.format(chance)}`);
  }
  if (rows[0].weighted === null) info.push("无法计算加权分布");
  return h("section", { class: "prob-question" }, title,
    info.length ? h("p", { class: "log-meta" }, info.join(" · ")) : null,
    rows.map(({ key, original, weight, weighted }) => h("div", { class: "prob-option" },
      h("div", { class: "prob-option-name" }, h("b", null, optionName(key)),
        Number.isFinite(weight) && weight !== 1 ? h("small", null, `权重 ×${weight}`) : null),
      probabilityBar(isRandom ? "等权基准" : "原始", original, "original"),
      probabilityBar("加权后", weighted, "weighted"),
    )),
  );
}

function renderLog(item) {
  const action = normalizeAction(item.action);
  const isRandom = item.decision_mode === "random";
  const isMixed = item.decision_mode === "mixed";
  const time = splitTime(item.time);
  const answers = Object.entries(item.answers || item.custom_answers || {});
  if (!answers.length && item.length) answers.push(["length", item.length]);
  const hints = Array.isArray(item.session_hints) ? item.session_hints.filter(Boolean) : [];
  const post = item.post_decision;
  const postAnswers = Object.entries(post?.answers || {});
  const calls = item.tool_calls || [];
  const statusLabels = {planned: item.dry_run ? "试运行" : "未确认执行", executed: "已执行", skipped: "已跳过", failed: "失败", running: "执行中", interrupted: "已中断"};

  const main = h(
    "div",
    { class: "log-main" },
    h("span", { class: "log-action" }, ACTION_LABELS[action] || item.action || "未知"),
    h("span", { class: "tag" }, SCENE_LABELS[item.scene] || item.scene || "未知场景"),
    isRandom ? h("span", { class: "tag" }, "纯随机") : null,
    isMixed ? h("span", { class: "tag" }, "混合决策") : null,
    item.use_meme
      ? h("span", { class: "tag", title: "配图" }, icon("image"), item.meme_id || "配图")
      : null,
    item.dry_run ? h("span", { class: "tag tag-flag", title: "只记录，未发送" }, "试运行") : null,
    item.degraded ? h("span", { class: "tag tag-flag", title: "判断失败后按降级规则处理" }, "已降级") : null,
    item.official_llm ? h("span", { class: "tag", title: "文字由官方主 Agent 生成" }, "官方主 Agent") : null,
    h("span", { class: "log-dur" }, `${item.duration_ms ?? "—"} ms`),
  );

  const sub = answers.length || hints.length || postAnswers.length || calls.length
    ? h(
        "div",
        { class: "log-sub" },
        answers.map(([key, value]) =>
          h("span", { class: "tag tag-judge", title: questionText(key) }, h("b", null, key), formatAnswer(value)),
        ),
        postAnswers.map(([key, value]) =>
          h("span", { class: "tag tag-judge tag-post", title: questionText(key) || "会话模型写完回复后判断" },
            h("small", null, "回复后"), h("b", null, key), formatAnswer(value)),
        ),
        hints.length ? h("span", { class: "tag tag-chat" }, `会话提示 ${hints.length} 条`) : null,
        calls.map(call => h("span", {
          class: cls("tag tag-tool", `is-${call.status}`),
          title: [call.sources?.length ? `来源：${call.sources.join("、")}` : "", call.reason].filter(Boolean).join(" · ") || undefined,
        }, icon("zap"), `${state.tools[call.name] || call.name} · ${statusLabels[call.status] || call.status}`)),
      )
    : null;

  const raw = {...item.raw, ...post?.raw};
  const hasRaw = Object.keys(raw).length > 0;
  const postIssues = Object.entries(post?.outcomes || {});
  const detail = calls.length || hints.length || hasRaw || item.target_message_id || item.degraded || post
    ? h(
        "details",
        { class: "log-detail" },
        h("summary", null, hasRaw ? "查看概率对比与详情" : "查看详情"),
        post && (postIssues.length || post.error) && h("section", {class: "prob-question"}, h("h4", null, "回复后判断"),
          postIssues.map(([id, o]) =>
            h("p", {class: "log-meta"}, `${id} · ${o.reason || o.status}`)),
          post.error && h("p", {class: "log-error"}, post.error)),
        item.target_message_id
          ? h("p", { class: "log-meta" }, "回复的消息：", h("code", null, item.target_message_id))
          : null,
        hints.length ? h("ul", { class: "log-hints" }, hints.map((text) => h("li", null, text))) : null,
        ...calls.map(call => h("p", {class: "log-meta"}, `${state.tools[call.name] || call.name} · ${statusLabels[call.status] || call.status}`, call.sources?.length ? `（${call.sources.join("、")}）` : "", call.reason ? ` · ${call.reason}` : "")),
        !hasRaw && item.degraded ? h("p", { class: "hint" }, "判断失败，本轮没有可展示的概率。") : null,
      )
    : null;

  if (hasRaw) detail.addEventListener("toggle", function showCharts() {
    if (!detail.open) return;
    detail.removeEventListener("toggle", showCharts);
    detail.append(
      h("p", { class: "hint" }, isRandom
        ? "等权基准表示每个候选的初始机会相同。加权后即为实际抽中概率。"
        : "加权后按本轮选项权重归一化，温度在此后生效。"),
      ...Object.entries(raw).map(([id, answer]) => renderProbabilityChart(
        id, answer, isRandom || answer?.decision_mode === "random",
      )),
      h("details", { class: "prob-raw" }, h("summary", null, "查看原始数据"),
        h("pre", { class: "log-raw" }, JSON.stringify(raw, null, 2))),
    );
  });

  return h(
    "li",
    { class: "log", dataset: { action } },
    h("div", { class: "log-time" }, h("strong", null, time.clock)),
    h(
      "div",
      { class: "log-body" },
      main,
      sub,
      item.error ? h("p", { class: "log-error" }, item.error) : null,
      item.umo ? h("p", { class: "log-umo", title: item.umo }, item.umo) : null,
      detail,
    ),
  );
}

function questionText(id) {
  return state.questions.find((q) => q.id.trim() === id)?.instructions.trim() || undefined;
}

function logHasIssue(item) {
  return !!(item.error || item.degraded || item.post_decision?.error
    || (item.tool_calls || []).some((call) => call.status === "failed" || call.status === "interrupted"));
}

const LOG_FILTERS = {
  all: () => true,
  speak: (item) => !logHasIssue(item) && ["reply", "proactive"].includes(normalizeAction(item.action)),
  silent: (item) => !logHasIssue(item) && normalizeAction(item.action) === "none",
  issue: logHasIssue,
};

function logSearchText(item) {
  const answers = { ...(item.answers || item.custom_answers), ...item.post_decision?.answers };
  return [
    item.umo, SCENE_LABELS[item.scene], ACTION_LABELS[normalizeAction(item.action)], item.meme_id,
    item.error, item.post_decision?.error, item.target_message_id,
    ...Object.entries(answers).flatMap(([key, value]) => [key, formatAnswer(value), `${key}=${formatAnswer(value)}`]),
    ...(item.session_hints || []),
    ...(item.tool_calls || []).map((call) => state.tools[call.name] || call.name),
  ].filter(Boolean).join(" ").toLowerCase();
}

function clearLogFilters() {
  state.logFilter = "all";
  $("log-search").value = "";
  renderLogs();
  $("log-search").focus();
}

function renderLogs() {
  const logs = state.logs;
  const terms = $("log-search").value.trim().toLowerCase().split(/\s+/).filter(Boolean);
  for (const button of $("log-filter").querySelectorAll("[data-filter]")) {
    const key = button.dataset.filter;
    button.dataset.label ||= button.textContent.trim();
    button.setAttribute("aria-pressed", String(key === state.logFilter));
    button.replaceChildren(button.dataset.label, h("small", null, logs.filter(LOG_FILTERS[key]).length));
  }
  const shown = logs.filter((item) => LOG_FILTERS[state.logFilter](item)
    && terms.every((term) => logSearchText(item).includes(term)));
  const items = [];
  let day = null;
  for (const item of shown) {
    const time = splitTime(item.time);
    if (time.day !== day) {
      day = time.day;
      items.push(h("li", { class: "log-day" }, day || "日期未知"));
    }
    items.push(renderLog(item));
  }
  $("logs").replaceChildren(...items);
  const filtered = shown.length !== logs.length;
  $("log-count").textContent = !logs.length ? "" : filtered
    ? `符合条件 ${shown.length} 条，共 ${logs.length} 条`
    : `共 ${logs.length} 条`;
  const empty = $("logs-empty");
  empty.hidden = shown.length > 0;
  if (!logs.length) {
    empty.textContent = "还没有决策记录。机器人收到消息并完成一次判断后，结果会出现在这里。";
  } else if (!shown.length) {
    empty.replaceChildren("没有符合条件的记录。",
      h("button", { type: "button", class: "btn btn-text", onclick: clearLogFilters }, "清除筛选"));
  }
}

async function loadLogs() {
  const limit = Number($("log-limit").value) || 20;
  try {
    const data = await bridge.apiGet("logs", { limit });
    state.logs = data.logs || [];
    renderLogs();
  } catch (error) {
    state.logs = [];
    renderLogs();
    $("logs-empty").textContent = `读取日志失败：${error.message}`;
  }
}

// ---------- 页面 ----------

let currentTab = "decision";

function setTab(name, { focus = false } = {}) {
  currentTab = name;
  for (const tab of ["decision", "logs"]) {
    const active = tab === name;
    const button = $(`tab-btn-${tab}`);
    $(`panel-${tab}`).hidden = !active;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
    if (active && focus) button.focus();
  }
  if (name === "logs") loadLogs();
}

async function reload() {
  const button = $("refresh");
  button.disabled = true;
  try {
    await loadStatus();
    if (currentTab === "logs") await loadLogs();
    if (isDirty()) {
      setSaveState("dirty", "有未保存的更改，刷新时保留了你的编辑");
      return;
    }
    try {
      await loadDecisionConfig();
    } catch (error) {
      setSaveState("error", `读取判断配置失败：${error.message}`);
    }
  } finally {
    button.disabled = false;
  }
}

function renderContext(context) {
  $("heading").textContent = bridge.t("views.meme_manager.heading", "快速决策");
  document.title = bridge.t("views.meme_manager.title", "快速决策");
  $("tab-btn-decision").textContent = bridge.t("views.meme_manager.tab_decision", "判断配置");
  $("tab-btn-logs").textContent = bridge.t("views.meme_manager.tab_logs", "决策日志");
  $("refresh-label").textContent = bridge.t("views.meme_manager.refresh", "刷新");
  document.documentElement.dataset.theme = context?.isDark ? "dark" : "light";
}

let restoreTimer = 0;

function resetRestoreButton() {
  window.clearTimeout(restoreTimer);
  const button = $("restore-default");
  button.classList.remove("is-confirm");
  button.textContent = "恢复默认问题";
}

function onRestoreDefault() {
  const button = $("restore-default");
  if (!button.classList.contains("is-confirm")) {
    button.classList.add("is-confirm");
    button.textContent = "再点一次确认恢复";
    setSaveState("notice", "确认后问题列表会换成默认问题，背景信息不受影响");
    restoreTimer = window.setTimeout(() => {
      resetRestoreButton();
      setSaveState(isDirty() ? "dirty" : "clean");
    }, 4000);
    return;
  }
  resetRestoreButton();
  setQuestions(state.defaults);
  rebindBaseline();
  state.open.clear();
  renderQuestions();
  if (isDirty()) setSaveState("dirty", "已换成默认问题，保存后生效");
  else setSaveState("clean");
}

function onToggleAll() {
  const allOpen = state.questions.every(q => state.open.has(q));
  state.open = allOpen ? new Set() : new Set(state.questions);
  renderQuestions();
}

function onDiscard() {
  fillForm(state.saved);
  setSaveState("clean");
}

function onTabKey(event) {
  if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
  event.preventDefault();
  setTab(currentTab === "decision" ? "logs" : "decision", { focus: true });
}

const context = await bridge.ready();
renderContext(context);
bridge.onContext(renderContext);

$("refresh").addEventListener("click", reload);
$("tab-btn-decision").addEventListener("click", () => setTab("decision"));
$("tab-btn-logs").addEventListener("click", () => setTab("logs"));
$("tab-btn-decision").addEventListener("keydown", onTabKey);
$("tab-btn-logs").addEventListener("keydown", onTabKey);
$("log-limit").addEventListener("change", loadLogs);
$("log-search").addEventListener("input", renderLogs);
for (const button of $("log-filter").querySelectorAll("[data-filter]")) {
  button.addEventListener("click", () => {
    state.logFilter = button.dataset.filter;
    renderLogs();
  });
}
$("toggle-all").addEventListener("click", onToggleAll);
$("restore-default").addEventListener("click", onRestoreDefault);
$("save-config").addEventListener("click", saveDecisionConfig);
$("discard").addEventListener("click", onDiscard);
$("custom-context").addEventListener("input", updateContextCount);
for (const item of document.querySelectorAll("#add-menu [data-template]")) {
  item.addEventListener("click", () => onAddQuestion(item.dataset.template));
}
for (const input of document.querySelectorAll('input[name="decision-mode"]')) {
  // 随机模式下显示各答案的抽中比例，切换模式时重画问题。
  input.addEventListener("change", () => renderQuestions());
}

// 点击菜单外或按 Esc 时收起「添加问题」菜单。
document.addEventListener("click", (event) => {
  const menu = $("add-menu");
  if (menu.open && !menu.contains(event.target)) menu.open = false;
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && $("add-menu").open) {
    $("add-menu").open = false;
    $("add-menu").querySelector("summary").focus();
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s" && currentTab === "decision") {
    event.preventDefault();
    saveDecisionConfig();
  }
});

// 表单里任何输入都会刷新「未保存」状态，并清掉对应输入框的错误标记。
for (const type of ["input", "change"]) {
  $("panel-decision").addEventListener(type, (event) => {
    event.target.classList?.remove("is-invalid");
    autosize(event.target);
    updateDirty();
    if (event.target.closest("#questions")) {
      renderFlow();
      refreshEntries();
      renderStats();
      refreshChangeMarks();
    }
  });
}

window.addEventListener("beforeunload", (event) => {
  if (isDirty()) event.preventDefault();
});

await reload();
