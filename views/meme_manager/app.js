import { probabilityRows } from "./log-probabilities.mjs";
import * as wf from "./workflow-model.mjs";

const bridge = window.AstrBotPluginView;

const SCENE_LABELS = {
  group_passive: "群聊",
  private_passive: "私聊",
  group_proactive: "群聊主动",
  private_proactive: "私聊主动",
};

const PREVIEW_SCENES = {
  group_passive: "群聊收到消息",
  private_passive: "私聊收到消息",
  group_proactive: "群聊主动判断",
  private_proactive: "私聊主动判断",
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
const STAGE_LABELS = { pre_reply: "正文生成前", post_reply: "正文生成后" };
const SOURCE_TAGS = { recent_messages: "选消息", memes: "选表情" };
const PROMPT_TOKENS = ["当前时间", "冷场时长"];

// 动作目录只给出参数名和时机取值，界面文案先在这里补齐；目录里出现新动作时退回原名。
const ACTION_UI = {
  request_reply: { icon: "reply", group: "chat" },
  add_context: { icon: "context", group: "chat" },
  quote_message: { icon: "quote", group: "send" },
  send_meme: { icon: "image", group: "send" },
};
const WHEN_LABELS = { on_match: "判断后立即发送", after_send: "正文发送后配图" };
const TOOL_STATUS = {
  planned: "计划中", executed: "已执行", skipped: "已跳过", failed: "失败", running: "执行中", interrupted: "已中断",
};
const MARK_LABELS = { used: "采用", discarded: "丢弃", unasked: "未提问" };

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
  reply: '<path d="M21 15a2 2 0 0 1-2 2H8l-5 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
  context: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/><path d="M8 13h8M8 17h5"/>',
  quote: '<path d="M6 7h5v5c0 3-2 5-5 6"/><path d="M14 7h5v5c0 3-2 5-5 6"/>',
  zap: '<path d="M13 2 4 14h7l-1 8 9-12h-7l1-8z"/>',
  close: '<path d="M6 6l12 12M18 6 6 18"/>',
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

function moveItem(list, index, delta) {
  const next = index + delta;
  if (next < 0 || next >= list.length) return false;
  const [item] = list.splice(index, 1);
  list.splice(next, 0, item);
  return true;
}

function normalizeLogAction(action) {
  return action === "join" ? "reply" : action;
}

const percent = new Intl.NumberFormat("zh-CN", { style: "percent", maximumFractionDigits: 2 });
const roughPercent = new Intl.NumberFormat("zh-CN", { style: "percent", maximumFractionDigits: 1 });

// ---------- 状态 ----------

const state = {
  questions: [],
  defaults: [],
  catalog: {},
  events: {},
  sources: {},
  storedVersion: null,
  saved: { mode: "model", context: "", questions: [] },
  savedSnapshot: "",
  saving: false,
  fresh: null,
  loaded: false,
  // 展开的问题和折叠区以对象记录，改名和排序后保持展开。
  open: new Set(),
  advanced: new Set(),
  // 读到的配置里每道题的序列化结果，用来标出新增和修改过的题。
  baseline: new Map(),
  baselineOrder: [],
  preview: { scene: "group_passive", trigger_event: "silence_elapsed", choices: new WeakMap(), result: null, stale: false, revision: 0 },
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

function setQuestions(raw) {
  state.questions = wf.normalizeQuestions(JSON.parse(JSON.stringify(raw)));
}

function collectQuestions() {
  return state.questions.map(wf.serializeQuestion);
}

// ---------- 改动追踪 ----------

function captureBaseline() {
  state.baseline = new Map(state.questions.map((q) => [q, { id: q.id.trim(), json: JSON.stringify(wf.serializeQuestion(q)) }]));
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
  return base.json === JSON.stringify(wf.serializeQuestion(question)) ? "" : "changed";
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
  const pending = state.questions.filter(wf.isPending).length;
  const text = parts.length ? `未保存：${parts.join("、")}` : SAVE_MESSAGES.dirty;
  const notes = [];
  if (pending) notes.push(`${pending} 道题待连接，保存后不会被提问`);
  if (Number.isInteger(state.storedVersion) && state.storedVersion < 5) notes.push("保存后配置升级为选项动作格式，旧版编辑页将无法再保存");
  return notes.length ? `${text}。${notes.join("；")}` : text;
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
    questions: collectQuestions(),
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

// 会移除其他内容的修改先试算：第一次点击只提示数量，几秒内再点同一处才执行。
let armed = null;

function disarm() {
  if (!armed) return;
  window.clearTimeout(armed.timer);
  armed.el?.classList.remove("is-armed");
  armed = null;
}

function confirmChange(key, el, apply, verb, after) {
  const loss = wf.runChange(state.questions, state.catalog, apply, { commit: false });
  if (loss.total && armed?.key !== key) {
    disarm();
    el?.classList.add("is-armed");
    armed = {
      key, el,
      timer: window.setTimeout(() => {
        disarm();
        if ($("savebar").dataset.state === "notice") setSaveState(isDirty() ? "dirty" : "clean", isDirty() ? describeChanges() : "");
      }, 4000),
    };
    setSaveState("notice", `再点一次确认${verb}：会同时移除 ${wf.describeLoss(loss)}`);
    return;
  }
  disarm();
  const lost = wf.runChange(state.questions, state.catalog, apply);
  after?.();
  refreshEditor();
  if (lost.total) setSaveState("dirty", `已${verb}，并移除 ${wf.describeLoss(lost)}；撤销更改可以找回`);
}

// ---------- 名称与标签 ----------

function qName(question) {
  return question.id.trim() || "未命名问题";
}

function qIndexOf(question) {
  return state.questions.indexOf(question);
}

function slotLabel(q, ref) {
  if (ref.side) return ref.side === "true" ? "是" : "否";
  if (wf.isWildcard(ref)) return q.candidate_source === "memes" ? "每张候选表情" : "每条候选消息";
  return ref.id.trim() || "未命名选项";
}

// 校验信息里的选项名，不依赖所属的题。
function refLabel(ref) {
  if (ref.side) return ref.side === "true" ? "是" : "否";
  return ref.id.trim() === "*" ? "每条候选" : ref.id.trim();
}

function toolLabel(name) {
  return state.catalog[name]?.label || { send_meme: "发送表情包" }[name] || name;
}

function triggerLabel(t) {
  if (t.event === "timer_elapsed") return t.daily_time !== undefined ? `每天 ${t.daily_time}` : `每 ${t.interval_seconds} 秒`;
  return t.event ? state.events[t.event] || t.event : `${qName(t.parent)} 选「${slotLabel(t.parent, t.option)}」`;
}

function timingLabel(t) {
  return t.ask_with_parent ? "同批" : "命中后问";
}

// 页面上显示问题名的地方都带 data-name-of，改 ID 时就地刷新。
function nameRef(question, tag = "code") {
  return h(tag, { dataset: { nameOf: qIndexOf(question) } }, qName(question));
}

function questionIssues(q) {
  return wf.questionIssues(q, state.questions, state.catalog, refLabel);
}

function validate() {
  return wf.validate(state.questions, state.catalog, refLabel);
}

function weightShares(question) {
  const slots = wf.slotsOf(question);
  const weights = slots.map((slot) => slot.weight);
  const valid = weights.every((w) => Number.isFinite(w) && w >= 0 && w <= 100);
  const total = valid ? weights.reduce((sum, w) => sum + w, 0) : 0;
  return weights.map((w) => (total > 0 ? w / total : null));
}

function showsShares(question) {
  // 「每条候选」会展开成多个候选，这种题不显示固定比例。
  return wf.effectiveMode(question, getMode()) === "random" && !question.options.some(wf.isWildcard);
}

function shareText(share) {
  if (share === null) return "—";
  if (share === 0) return "不会抽中";
  return `抽中约 ${roughPercent.format(share)}`;
}

function markInvalid(error) {
  const question = state.questions[error.q];
  if (!question) return;
  state.open.add(question);
  if (["default_option", "private_default", "alias", "scenes"].includes(error.field)) state.advanced.add(question);
  renderQuestions();
  const card = $("questions").querySelector(`[data-q="${error.q}"]`);
  let scope = card;
  if (error.o !== undefined) scope = scope?.querySelector(`[data-o="${error.o}"]`) || scope;
  if (error.a !== undefined) scope = scope?.querySelector(`[data-a="${error.a}"]`) || scope;
  if (error.t !== undefined) scope = scope?.querySelector(`[data-t="${error.t}"]`) || scope;
  const field = scope?.matches?.(`[data-field="${error.field}"]`) ? scope
    : scope?.querySelector(`[data-field="${error.field}"]`) || scope;
  if (!field) return;
  field.classList.add("is-invalid");
  field.scrollIntoView({ block: "center", behavior: "smooth" });
  field.focus?.({ preventScroll: true });
}

// ---------- 流程泳道 ----------

function descendants(starts) {
  const seen = new Set();
  const stack = [...starts];
  while (stack.length) {
    const q = stack.pop();
    if (seen.has(q)) continue;
    seen.add(q);
    for (const { child } of wf.childLinks(state.questions, q)) stack.push(child);
  }
  return seen;
}

function previewMarks() {
  const marks = new Map();
  const data = state.preview.result;
  if (!data || state.preview.stale) return marks;
  const asked = new Set(data.batches.flatMap((b) => b.asked || []));
  const discarded = new Set(data.batches.flatMap((b) => b.discarded || []));
  for (const q of state.questions) {
    const id = q.id.trim();
    if (discarded.has(id)) marks.set(id, "discarded");
    else if (Object.hasOwn(data.answers || {}, id)) marks.set(id, "used");
    else if (!asked.has(id) && q.enabled) marks.set(id, "unasked");
  }
  return marks;
}

function slotTools(ref) {
  return [...new Set(ref.actions.map((a) => a.tool))];
}

function actIcons(tools) {
  return tools.map((tool) => h("span", {
    class: cls("flow-act", `is-${ACTION_UI[tool]?.group || "send"}`),
    title: toolLabel(tool),
  }, icon(ACTION_UI[tool]?.icon || "zap")));
}

function flowNode(q, edge, shown, marks) {
  const repeated = shown.has(q);
  const mark = marks.get(q.id.trim());
  const links = wf.childLinks(state.questions, q);
  const outs = wf.slotsOf(q).map((ref) => {
    const tools = slotTools(ref);
    const kids = links.filter((l) => l.option === ref);
    if (!tools.length && !kids.length) return null;
    return h("span", { class: "flow-out" }, slotLabel(q, ref), actIcons(tools),
      kids.length > 0 && h("span", { class: "flow-kids" }, icon("arrow"), kids.map((l) => qIndexOf(l.child) + 1).join(" ")));
  }).filter(Boolean);
  const item = h("li", null,
    edge && h("span", { class: "flow-edge" }, edge, icon("arrow")),
    h("button", {
      type: "button",
      class: cls("flow-node", !q.enabled && "is-off", q.stage === "post_reply" && "is-post",
        { new: "is-changed is-added", changed: "is-changed" }[changeOf(q)]),
      title: q.instructions.trim() || undefined,
      onclick: () => revealQuestion(q),
    },
      h("span", { class: "flow-num" }, qIndexOf(q) + 1),
      nameRef(q),
      h("span", { class: "flow-meta" }, TYPE_LABELS[q.type]),
      !q.enabled && h("span", { class: "q-tag is-off" }, "已停用"),
      mark && h("span", { class: `flow-badge is-${mark}` }, MARK_LABELS[mark]),
      repeated ? h("span", { class: "flow-meta" }, "（同上）")
        : outs.length ? h("span", { class: "flow-outs" }, outs) : h("span", { class: "flow-meta" }, "只记录答案"),
    ));
  if (repeated) return item;
  shown.add(q);
  if (links.length) {
    item.append(h("ul", null, links.map((l) =>
      flowNode(l.child, `${slotLabel(q, l.option)} · ${timingLabel(l.trigger)}`, shown, marks))));
  }
  return item;
}

function renderFlow() {
  const qs = state.questions;
  const marks = previewMarks();
  const lanes = Object.entries(state.events).map(([event, title]) => {
    const entries = wf.roots(qs, event);
    const sources = wf.replySources(qs, entries);
    const shown = new Set();
    const speaks = event !== wf.POST_EVENT && entries.some((q) => q.enabled);
    return h("section", { class: cls("lane", !entries.length && "is-empty"), dataset: { event } },
      h("div", { class: "lane-head" },
        h("span", { class: "lane-title" }, title),
        h("span", { class: "lane-stats" }, entries.length ? `${entries.length} 道入口` : "没有题订阅",
          sources.length > 0 && ` · 请求发言：${sources.map((s) => `${qName(s.q)}=${slotLabel(s.q, s.ref)}`).join("、")}`),
        h("button", { type: "button", class: "btn btn-text lane-add", onclick: () => addEntry(event) }, icon("plus"), "添加入口题")),
      speaks && !sources.length && h("p", { class: "lane-note" }, "这个事件不会调用会话模型发言，独立动作照常执行。"),
      sources.length > 1 && h("p", { class: "lane-note is-info" }, "多处请求发言时只生成一次正文。"),
      entries.length > 0 && h("ul", { class: "flow-tree" }, entries.map((q) => flowNode(q, null, shown, marks))));
  });
  const pending = qs.filter(wf.isPending);
  if (pending.length) {
    const shown = new Set();
    lanes.push(h("section", { class: "lane is-pending" },
      h("div", { class: "lane-head" },
        h("span", { class: "lane-title" }, "待连接"),
        h("span", { class: "lane-stats" }, "没有触发来源，不会被提问")),
      h("ul", { class: "flow-tree" }, pending.map((q) => flowNode(q, null, shown, marks)))));
  }
  const covered = descendants([...Object.keys(state.events).flatMap((event) => wf.roots(qs, event)), ...pending]);
  const orphans = qs.filter((q) => !covered.has(q));
  if (orphans.length) {
    const shown = new Set();
    lanes.push(h("section", { class: "lane is-danger" },
      h("div", { class: "lane-head" }, h("span", { class: "lane-title" }, "这些题互相触发形成了循环，没有入口")),
      h("ul", { class: "flow-tree" }, orphans.map((q) => flowNode(q, null, shown, marks)))));
  }
  $("flow-body").replaceChildren(...lanes);
  if ($("decision-preview").open) previewFields();
}

// ---------- 试算 ----------

function markPreviewStale() {
  if (!state.preview.result || state.preview.stale) return;
  state.preview.stale = true;
  $("preview-stale").hidden = false;
  renderFlow();
}

function previewFields() {
  const fields = $("preview-fields");
  const scene = state.preview.scene;
  const event = scene.endsWith("_proactive") ? state.preview.trigger_event : "message_received";
  const list = wf.questionsForScene(state.questions, scene, event);
  const selects = list.map((q) => {
    const slots = wf.slotsOf(q).filter((ref) => ref.weight > 0);
    if (!slots.length) return null;
    const options = Object.fromEntries(slots.map((ref) => [wf.keyOf(ref),
      wf.isWildcard(ref) ? (q.candidate_source === "memes" ? "示例表情" : "示例消息") : slotLabel(q, ref)]));
    let key = state.preview.choices.get(q);
    if (!Object.hasOwn(options, key)) key = Object.keys(options)[0];
    state.preview.choices.set(q, key);
    return fieldSelect(qName(q), options, key, (value) => {
      state.preview.choices.set(q, value);
      markPreviewStale();
    });
  });
  fields.replaceChildren(...[
    fieldSelect("场景", PREVIEW_SCENES, scene, (value) => {
      state.preview.scene = value;
      markPreviewStale();
      previewFields();
    }),
    scene.endsWith("_proactive") && fieldSelect("触发事件", { silence_elapsed: "达到冷场时长", timer_elapsed: "定时到期（预览全部定时入口）" }, event, (value) => {
      state.preview.trigger_event = value;
      markPreviewStale();
      previewFields();
    }),
    ...selects,
    !list.length && h("p", { class: "hint" }, "这个场景下没有会被提问的题。"),
  ].filter(Boolean));
}

function formatSource(text) {
  const index = String(text).indexOf("=");
  if (index < 0) return String(text);
  const id = text.slice(0, index);
  const value = text.slice(index + 1);
  return `${id}=${formatPreviewAnswer(id, value)}`;
}

function formatPreviewAnswer(id, value) {
  const q = state.questions.find((item) => item.id.trim() === id);
  if (value === true || (q?.type === "boolean" && value === "true")) return "是";
  if (value === false || (q?.type === "boolean" && value === "false")) return "否";
  if (value === "sample_message") return "示例消息";
  if (value === "sample_meme") return "示例表情";
  return formatAnswer(value);
}

function previewRow(title, ...content) {
  return h("div", { class: "pv-row" }, h("span", { class: "pv-title" }, title), h("div", { class: "pv-value" }, content));
}

function renderPreviewResult() {
  const box = $("preview-result");
  const data = state.preview.result;
  $("preview-stale").hidden = !state.preview.stale;
  if (!data) {
    box.replaceChildren();
    return;
  }
  const context = data.context || {};
  const materials = [
    context.message && h("li", null, h("b", null, "选中的消息："), context.message),
    ...Object.entries(context.fields || {}).map(([label, value]) => h("li", null, h("b", null, `${label}：`), formatAnswer(value))),
    ...Object.entries(context.answers || {}).map(([id, value]) => h("li", null, h("b", null, `${id}：`), formatPreviewAnswer(id, value))),
    ...(context.hints || []).map((text) => h("li", null, h("b", null, "固定文本："), text)),
  ].filter(Boolean);
  const answers = Object.entries(data.answers || {});
  const hasPost = state.questions.some((q) => q.enabled && q.stage === "post_reply");
  box.replaceChildren(h("div", { class: "pv-grid" },
    previewRow("判断请求", h("b", null, `${data.batches.length} 次`),
      data.batches.map((b, i) => h("p", { class: "pv-batch" },
        `第 ${i + 1} 批 · ${b.stage === "post_reply" ? "正文后" : "正文前"} · 问 ${b.asked.join("、") || "—"}`,
        b.discarded?.length > 0 && [" · 丢弃 ", b.discarded.map((id, j) => [j ? "、" : "", h("s", null, id)])]))),
    previewRow("有效答案", answers.length
      ? answers.map(([id, value]) => h("span", { class: "tag tag-judge" }, h("b", null, id), formatPreviewAnswer(id, value)))
      : "无"),
    previewRow("会话模型", data.reply && data.reply !== "none"
      ? [h("b", null, "生成 1 次"), ` · 来源 ${(data.reply_sources || []).map(formatSource).join("、")}`]
      : ["不发言", hasPost ? h("small", { class: "pv-note" }, "正文后的题只在正文前请求了发言时才计算。") : null]),
    previewRow("交给会话模型", materials.length ? h("ul", { class: "pv-list" }, materials) : "无"),
    previewRow("引用", data.quote ? h("code", null, data.quote) : "无"),
    previewRow("动作", data.tools?.length
      ? h("ul", { class: "pv-list" }, data.tools.map((tool) => h("li", null,
        h("span", { class: "act-icon" }, icon(ACTION_UI[tool.name]?.icon || "zap")),
        h("b", null, toolLabel(tool.name)), ` · ${WHEN_LABELS[tool.when] || tool.when} · ${TOOL_STATUS[tool.status] || tool.status}`,
        tool.sources?.length ? ` · 来源 ${tool.sources.map(formatSource).join("、")}` : "",
        tool.reason ? ` · ${tool.reason}` : "")))
      : "无"),
  ));
}

async function runPreview() {
  const revision = ++state.preview.revision;
  const box = $("preview-result");
  const error = validate();
  if (error) {
    box.replaceChildren(h("p", { class: "log-error" }, `配置还有问题：${error.message}`,
      h("button", { type: "button", class: "btn btn-text", onclick: () => markInvalid(error) }, "去修改")));
    return;
  }
  box.replaceChildren(h("p", { class: "hint" }, "正在试算…"));
  try {
    const event = state.preview.scene.endsWith("_proactive") ? state.preview.trigger_event : "message_received";
    const list = wf.questionsForScene(state.questions, state.preview.scene, event);
    const data = await bridge.apiPost("decision-preview", {
      version: 7,
      scene: state.preview.scene,
      trigger_event: event,
      decision_mode: getMode(),
      decision_questions: collectQuestions(),
      answers: Object.fromEntries(list.filter((q) => state.preview.choices.has(q))
        .map((q) => [q.id.trim(), state.preview.choices.get(q)])),
    });
    if (revision !== state.preview.revision) return;
    state.preview.result = data;
    state.preview.stale = false;
    renderPreviewResult();
    renderFlow();
  } catch (err) {
    if (revision === state.preview.revision) box.replaceChildren(h("p", { class: "log-error" }, `试算失败：${err.message}`));
  }
}

$("decision-preview").addEventListener("toggle", () => { if ($("decision-preview").open) previewFields(); });
$("run-preview").addEventListener("click", runPreview);

function revealQuestion(question) {
  state.open.add(question);
  const index = qIndexOf(question);
  renderQuestions(`[data-q="${index}"] .q-toggle`);
  $("questions").querySelector(`[data-q="${index}"]`)?.scrollIntoView({ block: "start", behavior: "smooth" });
}

function renderStats() {
  const qs = state.questions;
  const total = qs.length;
  const enabled = qs.filter((q) => q.enabled).length;
  const entries = qs.filter((q) => q.enabled && q.triggers.some((t) => t.event)).length;
  const pending = qs.filter(wf.isPending).length;
  const speaks = wf.allReplySources(qs.filter((q) => q.enabled)).length;
  $("question-stats").textContent = total
    ? [`共 ${total} 个问题`, `${enabled} 个启用`, `${entries} 个入口`, pending && `${pending} 个待连接`, `${speaks} 处请求发言`]
      .filter(Boolean).join(" · ")
    : "";
  const allOpen = total > 0 && qs.every((q) => state.open.has(q));
  const toggle = $("toggle-all");
  toggle.hidden = !total;
  toggle.textContent = allOpen ? "全部收起" : "全部展开";
}

// ---------- 问题卡片 ----------

function renderQuestions(focus) {
  disarm();
  const root = $("questions");
  if (!state.questions.length) {
    root.replaceChildren(h("li", { class: "questions-empty" },
      h("b", null, "还没有题目"),
      h("span", null, "没有题目订阅事件时，插件不发起判断，也不会调用会话模型发言。"),
      h("span", { class: "questions-empty-actions" },
        h("button", { type: "button", class: "btn btn-outline", onclick: () => addEntry("message_received") },
          icon("plus"), "添加入口题（收到消息）"),
        h("button", { type: "button", class: "btn btn-text", onclick: onRestoreDefault }, "恢复默认问题"))));
  } else {
    root.replaceChildren(...state.questions.map(renderQuestion));
  }
  state.fresh = null;
  renderFlow();
  renderStats();
  refreshChangeMarks();
  if (NEEDS_AUTOSIZE) root.querySelectorAll("textarea.autosize").forEach(autosize);
  if (focus) document.querySelector(focus)?.focus();
}

function refreshEditor(focus) {
  renderQuestions(focus);
  updateDirty();
  markPreviewStale();
}

function toggleQuestion(question) {
  if (state.open.has(question)) state.open.delete(question);
  else state.open.add(question);
  renderQuestions(`[data-q="${qIndexOf(question)}"] .q-toggle`);
}

function renderQuestion(question, qIndex) {
  const open = state.open.has(question);
  const [issue] = questionIssues(question);
  const pending = wf.isPending(question);
  const card = h("li", {
    class: cls("question", !question.enabled && "is-disabled", open && "is-open",
      state.fresh === question && "is-new", question.stage === "post_reply" && "is-post", pending && "is-pending"),
    dataset: { q: qIndex },
  });

  const enabledText = h("span", null, question.enabled ? "启用" : "已停用");
  const enabledToggle = h("label", { class: "switch", title: "停用后不会问这道题，也会阻断经过它的后续题" },
    h("input", {
      type: "checkbox",
      checked: question.enabled,
      "aria-label": `启用问题 ${qName(question)}`,
      onchange: (event) => {
        question.enabled = event.target.checked;
        enabledText.textContent = question.enabled ? "启用" : "已停用";
        refreshEditor();
      },
    }),
    enabledText);

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
    h("span", { class: "q-tag" }, TYPE_LABELS[question.type]),
    wf.isDynamic(question) && h("span", { class: "q-tag is-source" }, SOURCE_TAGS[question.candidate_source] || question.candidate_source),
    question.stage === "post_reply" && h("span", { class: "q-tag is-post" }, "正文后"),
    question.decision_mode !== "inherit" && h("span", { class: "q-tag" }, question.decision_mode === "random" ? "随机" : "模型"),
    pending && h("span", { class: "q-tag is-pending" }, "待连接"),
    !open && issue && h("span", { class: "q-tag is-issue", title: issue.message }, "待完善"),
    h("span", { class: "q-change", hidden: true }),
  );

  const tools = h("div", { class: "q-tools" },
    iconButton("copy", "复制这个问题", () => duplicateQuestion(question)),
    iconButton("up", "上移问题", () => moveQuestion(qIndex, -1, "up"), { disabled: qIndex === 0, act: "up" }),
    iconButton("down", "下移问题", () => moveQuestion(qIndex, 1, "down"), {
      disabled: qIndex === state.questions.length - 1, act: "down",
    }),
    confirmButton(`删除问题 ${qName(question)}`, () => deleteQuestion(question), {
      onArm: () => {
        const loss = wf.runChange(state.questions, state.catalog, wf.deleteQuestion(state.questions, question), { commit: false });
        if (loss.total) setSaveState("notice", `删除「${qName(question)}」会同时移除 ${wf.describeLoss(loss)}，失去来源的子题进入待连接`);
      },
      onDisarm: () => {
        if ($("savebar").dataset.state === "notice") setSaveState(isDirty() ? "dirty" : "clean", isDirty() ? describeChanges() : "");
      },
    }),
  );

  card.append(h("div", { class: "q-head" }, toggle, h("div", { class: "q-controls" }, enabledToggle, tools)));

  if (!open) {
    card.append(renderSummary(question));
    return card;
  }
  card.append(h("div", { class: "q-body", id: `q-body-${qIndex}` },
    renderWhen(question, qIndex),
    renderAsk(question, qIndex),
    renderOutcomes(question, qIndex),
    renderAdvanced(question, qIndex),
  ));
  return card;
}

function renderEntry(q) {
  if (wf.isPending(q)) return h("span", { class: "q-entry is-pending" }, "待连接 · 不会被提问");
  return h("span", { class: "q-entry" }, q.triggers.map((t) => t.event ? triggerLabel(t) : `${triggerLabel(t)} · ${timingLabel(t)}`).join(" 或 "));
}

// 改选项 ID 时，其他卡片里的触发描述随之更新；只替换文字，不动输入框。
function refreshEntries() {
  for (const card of $("questions").querySelectorAll(".question[data-q]")) {
    const question = state.questions[Number(card.dataset.q)];
    if (!question) continue;
    for (const el of card.querySelectorAll(".q-entry")) el.replaceWith(renderEntry(question));
  }
}

function renderSummary(question) {
  const links = wf.childLinks(state.questions, question);
  const chips = wf.slotsOf(question).map((ref) => h("span", { class: cls("ans-chip", ref.weight === 0 && "is-zero") },
    h("b", null, slotLabel(question, ref)),
    Number.isFinite(ref.weight) && ref.weight !== 1 && h("small", null, `×${ref.weight}`),
    actIcons(slotTools(ref)),
    links.filter((l) => l.option === ref).map((l) => [icon("arrow"), nameRef(l.child)]),
  ));
  return h("div", { class: "q-summary", onclick: () => toggleQuestion(question) },
    h("p", { class: cls("q-preview", !question.instructions.trim() && "is-empty") },
      question.instructions.trim() || "还没有填写要问判断模型的问题"),
    h("div", { class: "q-chips" }, renderEntry(question), h("span", { class: "q-chips-sep", "aria-hidden": "true" }), chips),
  );
}

function segmented(label, choices, selected, onPick, { disabled = {}, extra = "", field } = {}) {
  return h("div", { class: cls("seg", extra), role: "group", "aria-label": label, dataset: field ? { field } : undefined },
    choices.map(([value, text]) => h("button", {
      type: "button",
      "aria-pressed": String(value === selected),
      disabled: !!disabled[value],
      title: disabled[value] || undefined,
      onclick: (event) => { if (value !== selected) onPick(value, event.currentTarget); },
    }, text)));
}

function fieldSelect(label, choices, selected, onChange, { field, id } = {}) {
  const input = h("select", { id, dataset: field ? { field } : undefined, onchange: (e) => onChange(e.target.value) },
    Object.entries(choices).map(([key, text]) => h("option", { value: key }, text)));
  input.value = selected;
  return h("label", { class: "setting" }, h("span", { class: "field-label" }, label), input);
}

// 带分组和置灰原因的下拉框：choices 为 {group, label, disabled, reason}。
function choiceSelect(label, choices, current, onPick, { field, placeholder } = {}) {
  const groups = new Map();
  choices.forEach((c, i) => {
    if (!groups.has(c.group)) groups.set(c.group, []);
    groups.get(c.group).push(h("option", { value: String(i), disabled: !!c.disabled },
      c.label + (c.reason ? `（${c.reason}）` : "")));
  });
  const select = h("select", {
    "aria-label": label,
    dataset: field ? { field } : undefined,
    onchange: (e) => { if (e.target.value !== "") onPick(choices[Number(e.target.value)]); },
  },
    placeholder && h("option", { value: "" }, placeholder),
    [...groups].map(([group, options]) => group ? h("optgroup", { label: group }, options) : options));
  select.value = current >= 0 ? String(current) : "";
  return select;
}

function section(title, ...children) {
  return h("div", { class: "q-section" }, h("h3", { class: "q-section-title" }, title), children);
}

function switchField(label, checked, onChange, note) {
  return h("label", { class: "switch switch-row" },
    h("input", { type: "checkbox", checked, onchange: (e) => onChange(e.target.checked) }),
    h("span", null, label, note && h("small", null, note)));
}

// ---------- 何时提问 ----------

function triggerChoices(q, current) {
  const used = new Set(q.triggers.filter((t) => t !== current).map((t) => (t.event ? `event:${t.event}` : t.option)));
  const list = [];
  for (const [event, label] of Object.entries(state.events)) {
    if (!wf.eventAllowed(q, event) || used.has(`event:${event}`)) continue;
    list.push({ group: "事件", label, match: (t) => t.event === event,
      make: () => ({ event, ...(event === "timer_elapsed" ? { interval_seconds: 600 } : {}) }) });
  }
  for (const parent of state.questions) {
    if (parent === q || (parent.stage === "post_reply" && q.stage === "pre_reply")) continue;
    const cycle = wf.wouldCycle(state.questions, q, parent);
    for (const ref of wf.slotsOf(parent)) {
      if (used.has(ref)) continue;
      list.push({
        group: "上游选项",
        label: `${qName(parent)} 选「${slotLabel(parent, ref)}」`,
        disabled: cycle,
        reason: cycle ? "会形成循环" : "",
        match: (t) => t.option === ref,
        make: () => ({
          parent, option: ref,
          ask_with_parent: parent.stage === q.stage && (current?.parent ? current.ask_with_parent : true),
        }),
      });
    }
  }
  return list;
}

function renderWhen(q, qIndex) {
  const stageSeg = segmented("提问阶段", [["pre_reply", "正文生成前"], ["post_reply", "正文生成后"]], q.stage,
    (stage, el) => confirmChange(`stage:${qIndex}:${stage}`, el, wf.changeStage(q, stage), `切换到${STAGE_LABELS[stage]}`),
    { extra: "stage-seg", field: "stage" });
  const random = wf.effectiveMode(q, getMode()) === "random";
  const rows = q.triggers.map((t, i) => {
    const choices = triggerChoices(q, t);
    const select = choiceSelect("触发来源", choices, choices.findIndex((c) => c.match(t)), (choice) => {
      q.triggers[i] = choice.make();
      refreshEditor(`[data-q="${qIndex}"] [data-t="${i}"] select`);
    }, { field: "trigger" });
    let timing = null;
    if (t.event === "timer_elapsed") {
      const daily = t.daily_time !== undefined;
      timing = h("div", { class: "timer-fields" },
        segmented("定时方式", [["interval", "固定间隔"], ["daily", "每天"]], daily ? "daily" : "interval", (mode) => {
          delete t.interval_seconds;
          delete t.daily_time;
          Object.assign(t, mode === "daily" ? { daily_time: "08:00" } : { interval_seconds: 600 });
          refreshEditor();
        }),
        h("label", { class: "inline-field" }, daily ? "时刻" : "每",
          h("input", {
            type: daily ? "time" : "number", min: daily ? undefined : 15, max: daily ? undefined : 2147483647,
            step: daily ? 60 : 1, value: daily ? t.daily_time : t.interval_seconds,
            "aria-label": daily ? "每日触发时刻" : "定时间隔（秒）", dataset: { field: "trigger" },
            oninput: (e) => {
              if (daily) t.daily_time = e.target.value;
              else t.interval_seconds = e.target.valueAsNumber;
              updateDirty();
              markPreviewStale();
            },
            onchange: () => refreshEditor(),
          }), daily ? "" : "秒"));
    }
    if (t.parent) {
      if (t.parent.stage !== q.stage) timing = h("span", { class: "note-inline" }, "需等正文生成，命中后再问");
      else if (random) timing = h("span", { class: "note-inline" }, "本地随机，无需等批");
      else {
        timing = segmented("提问时机", [["with", "随上游同批"], ["after", "命中后再问"]], t.ask_with_parent ? "with" : "after",
          (value) => { t.ask_with_parent = value === "with"; refreshEditor(); }, { extra: "timing-seg", field: "timing" });
      }
    }
    return h("div", { class: "trigger-row", dataset: { t: i } }, select, timing,
      iconButton("close", "移除这条触发来源", () => { q.triggers.splice(i, 1); refreshEditor(); }));
  });
  const free = triggerChoices(q, null).find((c) => !c.disabled);
  const addTrigger = h("button", {
    type: "button", class: "btn btn-text", disabled: !free, dataset: { act: "add-trigger" },
    title: free ? undefined : "没有可以再添加的触发来源",
    onclick: () => {
      q.triggers.push(free.make());
      refreshEditor(`[data-q="${qIndex}"] [data-t="${q.triggers.length - 1}"] select`);
    },
  }, icon("plus"), "添加触发来源");

  const sceneChips = h("div", { class: "scene-chips", role: "group", "aria-label": "限定场景" },
    wf.SCENE_KEYS.map((scene) => h("button", {
      type: "button",
      class: "chip-toggle",
      "aria-pressed": String(q.scenes.includes(scene)),
      onclick: () => {
        q.scenes = q.scenes.includes(scene) ? q.scenes.filter((s) => s !== scene)
          : wf.SCENE_KEYS.filter((s) => s === scene || q.scenes.includes(s));
        refreshEditor();
      },
    }, SCENE_LABELS[scene])));
  const scenes = wf.possibleScenes(q);
  const effective = wf.isPending(q) ? null : scenes.size
    ? `实际会在${wf.SCENE_KEYS.filter((s) => scenes.has(s)).map((s) => PREVIEW_SCENES[s]).join("、")}时问${q.stage === "post_reply" ? "（正文生成后）" : ""}`
    : "按当前触发来源和场景限制，这道题不会被提问";

  return section("何时提问",
    h("div", { class: "when-grid" },
      h("span", { class: "field-label" }, "阶段"), stageSeg,
      h("span", { class: "field-label" }, "触发来源"),
      h("div", { class: "q-triggers" },
        wf.isPending(q) && h("p", { class: "pending-note" }, "待连接：这道题不会被提问。选择一个事件或上游选项。"),
        rows,
        h("div", { class: "trigger-foot" }, addTrigger,
          q.triggers.length > 1 && h("small", { class: "hint" }, "任一来源命中即提问，同一轮只问一次。"),
          q.triggers.some((t) => t.ask_with_parent) && h("small", { class: "hint" }, "同批只是提前问，上游命中后才采用答案。")),
        q.triggers.some((t) => t.event === "timer_elapsed") && h("small", { class: "hint" },
          "定时仅作用于插件已收到过消息的会话；每天按 AstrBot 会话时区执行，未配置时用服务器时区。约每 15 秒检查一次，重启后重新计时。")),
      h("span", { class: "field-label" }, "限定"),
      h("div", { class: "when-limits" },
        sceneChips,
        h("small", { class: "hint" }, "不选即不限，对上游来源同样生效"),
        h("label", { class: "inline-field" }, "有候选时才问",
          (() => {
            const select = h("select", { onchange: (e) => { q.requires_candidates = e.target.value; refreshEditor(); } },
              h("option", { value: "" }, "不限"),
              h("option", { value: "recent_messages" }, "有最近用户消息时"),
              h("option", { value: "memes" }, "有图库表情时"));
            select.value = q.requires_candidates;
            return select;
          })()),
        effective && h("p", { class: "effective" }, effective))));
}

// ---------- 问判断模型 ----------

function renderAsk(q, qIndex) {
  const idInput = h("input", {
    type: "text", class: "mono", value: q.id, placeholder: "如 join_chat",
    spellcheck: "false", autocomplete: "off", dataset: { field: "id" },
    oninput: (event) => {
      q.id = event.target.value;
      for (const el of document.querySelectorAll(`[data-name-of="${qIndex}"]`)) el.textContent = qName(q);
    },
  });
  const typeSeg = segmented("题型", [["boolean", "是否题"], ["choice", "选择题"]], q.type,
    (type, el) => confirmChange(`type:${qIndex}:${type}`, el, wf.changeType(q, type), `改为${TYPE_LABELS[type]}`),
    { disabled: wf.isDynamic(q) ? { boolean: "动态候选需要选择题" } : {}, extra: "type-seg" });
  const sources = Object.entries(state.sources).length ? Object.entries(state.sources)
    : [["options", "固定选项"], ["recent_messages", "最近用户消息"], ["memes", "图库表情"]];
  const sourceSeg = segmented("候选来源", sources, q.candidate_source,
    (source, el) => confirmChange(`source:${qIndex}:${source}`, el, wf.changeSource(q, source), `改为「${state.sources[source] || source}」`),
    { extra: "source-seg", field: "source" });

  const id = `q${qIndex}-instructions`;
  const input = h("textarea", {
    id, class: "autosize", rows: 2, value: q.instructions,
    placeholder: "例如：现在加入讨论合适吗？", dataset: { field: "instructions" },
    oninput: () => { q.instructions = input.value; },
  });
  const tokens = h("span", { class: "token-bar" }, "插入",
    PROMPT_TOKENS.map((name) => h("button", {
      type: "button", class: "token",
      title: `判断模型会看到本轮的实际${name}`,
      "aria-label": `插入${name}`,
      onclick: () => {
        input.setRangeText(`{${name}}`, input.selectionStart, input.selectionEnd, "end");
        input.focus();
        input.dispatchEvent(new Event("input", { bubbles: true }));
      },
    }, `{${name}}`)));
  const dynamicNote = {
    recent_messages: "「每条候选消息」行会展开为本轮快照中的每条用户消息（最多 8 条），{描述} 换成发送人、时间和摘要。",
    memes: "「每张候选表情」行会展开为图库里每张带描述的表情，{描述} 换成表情描述。",
  }[q.candidate_source];

  return section("问判断模型",
    h("div", { class: "settings-grid ask-grid" },
      h("label", { class: "setting" }, h("span", { class: "field-label" }, "题目 ID"), idInput),
      h("div", { class: "setting" }, h("span", { class: "field-label" }, "题型"), typeSeg),
      h("div", { class: "setting is-wide" }, h("span", { class: "field-label" }, "候选来源"), sourceSeg)),
    h("div", { class: "q-prompt" },
      h("div", { class: "field-row" }, h("label", { class: "field-label is-judge", for: id }, "问判断模型"), tokens),
      input),
    dynamicNote && h("p", { class: "note" }, dynamicNote));
}

// ---------- 选项与结果 ----------

function renderOutcomes(q, qIndex) {
  const shares = showsShares(q) ? weightShares(q) : null;
  const wrap = h("div", { class: cls("answers", q.type === "boolean" && "is-fixed") },
    h("div", { class: "ans-row ans-head", "aria-hidden": "true" },
      h("span", { class: "ans-col-judge" }, q.type === "boolean" ? "判断模型的回答" : "选项 · 判断模型看到"),
      h("span", { class: "ans-col-weight", title: "默认 1。调高更偏向这一项，0 表示不选" }, "权重"),
      h("span", { class: "ans-col-effect" }, "选中后")));
  if (q.type === "boolean") {
    ["true", "false"].forEach((side, i) => wrap.append(booleanRow(q, qIndex, side, shares?.[i])));
  } else {
    q.options.forEach((option, oIndex) => wrap.append(choiceRow(q, qIndex, option, oIndex, shares?.[oIndex])));
    wrap.append(h("button", {
      type: "button",
      class: "btn btn-add-opt",
      onclick: () => {
        const used = new Set(q.options.map((item) => item.id.trim()));
        const fresh = wf.newOption(wf.nextId("option_", used));
        const star = q.options.findIndex(wf.isWildcard);
        // 新选项排在「每条候选」之前，候选行留在最后。
        if (star >= 0) q.options.splice(star, 0, fresh);
        else q.options.push(fresh);
        refreshEditor(`[data-q="${qIndex}"] [data-o="${q.options.indexOf(fresh)}"] [data-field="opt-id"]`);
      },
    }, icon("plus"), "添加选项"));
  }
  return section("选项与结果", wrap);
}

function weightCell(question, qIndex, ref, label, share) {
  return h("div", { class: "ans-weight" },
    h("input", {
      type: "number", min: 0, max: 100, step: "any", value: ref.weight ?? 1,
      "aria-label": label, dataset: { field: "weight" },
      oninput: (event) => {
        ref.weight = event.target.valueAsNumber;
        refreshShares(question, qIndex);
      },
    }),
    share !== undefined && h("small", { class: "ans-share", dataset: { share: "" } }, shareText(share)));
}

function refreshShares(question, qIndex) {
  if (!showsShares(question)) return;
  const shares = weightShares(question);
  const card = $("questions").querySelector(`[data-q="${qIndex}"]`);
  wf.slotsOf(question).forEach((ref, i) => {
    const cell = card?.querySelector(`[data-o="${wf.slotIndex(question, ref)}"] [data-share]`);
    if (cell) cell.textContent = shareText(shares[i]);
  });
}

function choiceRow(q, qIndex, option, oIndex, share) {
  const star = wf.isWildcard(option);
  const label = `选项「${star ? slotLabel(q, option) : option.id || oIndex + 1}」`;
  const idCell = star
    ? h("span", { class: "ans-fixed", title: "动态候选行，ID 固定为 *" }, h("code", null, "*"), h("small", null, slotLabel(q, option)))
    : h("input", {
      type: "text", class: "opt-id mono", value: option.id, placeholder: "选项 ID",
      spellcheck: "false", autocomplete: "off", "aria-label": `选项 ${oIndex + 1} 的 ID`,
      dataset: { field: "opt-id" },
      oninput: (event) => { option.id = event.target.value; },
    });
  const criteria = h("textarea", {
    class: "opt-criteria autosize", rows: 1, value: option.criteria,
    placeholder: star ? "候选说明，{描述} 会换成每个候选的内容" : "什么情况下选它",
    "aria-label": `${label}的说明`, dataset: { field: "opt-criteria" },
    oninput: (event) => { option.criteria = event.target.value; },
  });
  const count = q.options.length;
  const removeReason = star ? "动态候选需要这一行" : count <= 2 ? "至少保留两个选项" : "";
  const remove = iconButton("trash", removeReason || `删除${label}`, (event) => {
    if (removeReason) return;
    confirmChange(`option:${qIndex}:${oIndex}`, event.currentTarget, wf.deleteOption(q, option), `删除${label}`,
      () => state.preview.choices.delete(q));
  }, { disabled: !!removeReason, danger: true });
  return h("div", { class: "ans-row", dataset: { o: oIndex } },
    h("div", { class: cls("ans-judge", star && "is-fixed") }, idCell, criteria),
    weightCell(q, qIndex, option, `${label}的权重`, share),
    h("span", { class: "ans-arrow", "aria-hidden": "true" }, icon("arrow")),
    renderOutcome(q, qIndex, option),
    h("div", { class: "ans-tools" },
      iconButton("up", `上移${label}`, () => moveOption(q, qIndex, oIndex, -1), { disabled: oIndex === 0 }),
      iconButton("down", `下移${label}`, () => moveOption(q, qIndex, oIndex, 1), { disabled: oIndex === count - 1 }),
      remove),
  );
}

function booleanRow(q, qIndex, side, share) {
  const text = side === "true" ? "是" : "否";
  const ref = q.sides[side];
  return h("div", { class: "ans-row", dataset: { o: side } },
    h("div", { class: "ans-judge is-fixed" },
      h("span", { class: "ans-fixed is-bool" }, h("code", null, text)),
      h("span", { class: "ans-fixed-text" }, `判断模型回答「${text}」`)),
    weightCell(q, qIndex, ref, `「${text}」的权重`, share),
    h("span", { class: "ans-arrow", "aria-hidden": "true" }, icon("arrow")),
    renderOutcome(q, qIndex, ref),
    h("div", { class: "ans-tools" }),
  );
}

function renderOutcome(q, qIndex, ref) {
  const kids = wf.childLinks(state.questions, q).filter((l) => l.option === ref);
  return h("div", { class: "ans-effects" },
    ref.actions.length
      ? h("div", { class: "act-list" }, ref.actions.map((action, a) => renderAction(q, qIndex, ref, action, a)))
      : h("p", { class: "act-empty" }, "无动作，只记录答案"),
    h("div", { class: "effect-row" }, addActionField(q, qIndex, ref), followField(q, qIndex, ref)),
    kids.length > 0 && h("div", { class: "follow-list" }, h("span", null, "接着问"),
      kids.map((l) => h("button", { type: "button", class: "follow-link", onclick: () => revealQuestion(l.child) },
        icon("arrow"), nameRef(l.child), h("small", null, timingLabel(l.trigger))))),
  );
}

function addActionField(q, qIndex, ref) {
  const choices = Object.keys(state.catalog).map((tool) => {
    const reason = wf.actionAvailability(state.questions, state.catalog, q, ref, tool);
    return { group: "", label: toolLabel(tool), disabled: !!reason, reason: reason || "", tool };
  });
  const select = choiceSelect(`给${slotLabel(q, ref)}添加动作`, choices, -1, (choice) => {
    ref.actions.push(wf.newAction(state.questions, state.catalog, q, ref, choice.tool));
    const o = wf.slotIndex(q, ref);
    refreshEditor(`[data-q="${qIndex}"] [data-o="${o}"] [data-a="${ref.actions.length - 1}"] select, [data-q="${qIndex}"] [data-o="${o}"] [data-a="${ref.actions.length - 1}"] button`);
  }, { placeholder: "添加动作…" });
  return h("label", { class: "effect-field is-action" }, h("span", null, "动作"), select);
}

function followField(q, qIndex, ref) {
  const linked = new Set(wf.childLinks(state.questions, q).filter((l) => l.option === ref).map((l) => l.child));
  const existing = state.questions.filter((c) => c !== q && !linked.has(c)
    && !(q.stage === "post_reply" && c.stage === "pre_reply") && !wf.wouldCycle(state.questions, c, q));
  const kinds = [["boolean", "新建是否题"], ["choice", "新建选择题"], ["message", "新建选消息题"], ["meme", "新建选表情题"]]
    .filter(([kind]) => !(q.stage === "post_reply" && kind === "message"));
  const choices = [
    ...existing.map((c) => ({ group: "现有的题", label: `${qIndexOf(c) + 1}. ${qName(c)}${wf.isPending(c) ? "（待连接）" : ""}`, child: c })),
    ...kinds.map(([kind, label]) => ({ group: "新建", label, kind })),
  ];
  const select = choiceSelect(`选中${slotLabel(q, ref)}后接着问`, choices, -1, (choice) => {
    if (choice.child) {
      choice.child.triggers.push({ parent: q, option: ref, ask_with_parent: choice.child.stage === q.stage });
      refreshEditor();
      return;
    }
    const child = wf.templateQuestion(choice.kind, state.questions, { stage: q.stage });
    child.triggers = [{ parent: q, option: ref, ask_with_parent: true }];
    insertQuestion(child, qIndexOf(q) + 1);
  }, { placeholder: "接着问…" });
  return h("label", { class: "effect-field is-linked" }, h("span", null, "接着问"), select);
}

function isAncestor(source, q, seen = new Set()) {
  if (seen.has(q)) return false;
  seen.add(q);
  return q.triggers.some((t) => t.parent && (t.parent === source || isAncestor(source, t.parent, seen)));
}

function answerDescription(q, ref) {
  if (wf.isWildcard(ref)) {
    return q.candidate_source === "memes" ? "选中表情的描述" : "选中的消息（发送人、时间和原文）";
  }
  return `本题答案「${slotLabel(q, ref)}」`;
}

function sourceSelect(label, choices, current, onPick) {
  const select = h("select", { "aria-label": label, onchange: (e) => onPick(e.target.value) },
    choices.map(([value, text]) => h("option", { value }, text)));
  select.value = current;
  return h("label", { class: "act-field" }, h("span", null, label), select);
}

// 文本参数逐字输入时不重画卡片，只就地更新这张动作卡片的错误状态。
function recheckAction(input, q, ref, action) {
  const box = input.closest(".act");
  if (!box) return;
  const issue = wf.actionIssue(state.questions, state.catalog, q, ref, action);
  box.classList.toggle("is-invalid", !!issue);
  const error = box.querySelector(".act-error");
  if (!issue) error?.remove();
  else if (error) error.textContent = issue;
  else box.append(h("p", { class: "act-error" }, issue));
}

function actionParams(q, ref, action) {
  const tool = state.catalog[action.tool];
  if (!tool) return null;
  const params = tool.parameters || [];
  const fields = [];
  const fromValue = action.from ? String(qIndexOf(action.from)) : "self";
  const pick = (value) => (value === "self" ? null : state.questions[Number(value)] || null);
  if (action.tool === "add_context") {
    const sources = wf.sourceChoices(state.questions, state.catalog, q, ref, action.tool).filter(Boolean);
    const choices = [["self", answerDescription(q, ref)], ...sources.map((s) => [String(qIndexOf(s)), `「${qName(s)}」的答案`]), ["text", "固定文本"]];
    const current = action.text !== undefined ? "text" : fromValue;
    if (!choices.some(([value]) => value === current)) choices.push([current, `「${qName(action.from)}」的答案（已失效）`]);
    fields.push(sourceSelect("内容", choices, current, (value) => {
      if (value === "text") {
        action.from = null;
        action.text = action.text ?? "";
      } else {
        delete action.text;
        action.from = pick(value);
      }
      refreshEditor();
    }));
    if (action.text !== undefined) {
      fields.push(h("textarea", {
        class: "act-text autosize", rows: 1, value: action.text,
        placeholder: "例如：这轮用简短、口语化的语气回复", "aria-label": "固定文本",
        oninput: (event) => { action.text = event.target.value; recheckAction(event.target, q, ref, action); },
      }));
    }
    fields.push(h("label", { class: "act-field is-label" }, h("span", null, "字段名"),
      h("input", {
        type: "text", value: action.label || "", placeholder: "可选，如 篇幅",
        oninput: (event) => { action.label = event.target.value; recheckAction(event.target, q, ref, action); },
      })));
  } else if (params.includes("from")) {
    const noun = { recent_messages: "消息", memes: "表情" }[tool.source] || "答案";
    const sources = wf.sourceChoices(state.questions, state.catalog, q, ref, action.tool);
    const choices = sources.map((s) => (s ? [String(qIndexOf(s)), `「${qName(s)}」选中的${noun}`] : ["self", `本题选中的${noun}`]));
    if (!choices.some(([value]) => value === fromValue)) {
      choices.push([fromValue, action.from ? `「${qName(action.from)}」（已失效）` : `本题（已失效）`]);
    }
    fields.push(sourceSelect(noun, choices, fromValue, (value) => { action.from = pick(value); refreshEditor(); }));
  }
  if (params.includes("when") && tool.when?.length) {
    fields.push(segmented("时机", tool.when.map((w) => [w, WHEN_LABELS[w] || w]), action.when,
      (w) => { action.when = w; refreshEditor(); }, { extra: "when-seg", field: "when" }));
  }
  return fields.length ? h("div", { class: "act-params" }, fields) : null;
}

function quoteBindings() {
  return state.questions.flatMap((q) => wf.slotsOf(q).flatMap((ref) => ref.actions
    .filter((a) => a.tool === "quote_message").map((a) => ({ q, ref, action: a }))));
}

function actionNotes(q, ref, action) {
  const notes = [];
  if (action.tool === "request_reply") {
    // 只算可能在同一轮命中的来源：冷场和收到消息不会共用一次生成。
    const scenes = wf.possibleScenes(q);
    const others = wf.allReplySources(state.questions)
      .filter((s) => s.ref !== ref && [...wf.possibleScenes(s.q)].some((scene) => scenes.has(scene)));
    notes.push(others.length
      ? `与 ${others.slice(0, 2).map((s) => `${qName(s.q)}=${slotLabel(s.q, s.ref)}`).join("、")}${others.length > 2 ? ` 等 ${others.length} 处` : ""}共用一次生成`
      : "本轮生成一次正文");
  } else if (action.tool === "add_context") {
    if (action.label?.trim()) notes.push(`会话模型会看到「${action.label.trim()}：…」`);
    else if (action.text !== undefined) notes.push("作为本轮提示交给会话模型");
  } else if (action.tool === "quote_message") {
    notes.push("引用方式沿用群聊策略里的「指定消息的引用方式」");
    const all = quoteBindings();
    if (all.length > 1) notes.push(`多个引用同时命中时，采用排在最后的「${qName(all[all.length - 1].q)}」`);
  } else if (action.tool === "send_meme") {
    notes.push(action.when === "after_send" ? "正文确认发送后再发，本轮不发言就跳过" : "不等正文，本轮不发言也会发送");
  }
  if (action.from && !isAncestor(action.from, q)) notes.push(`「${qName(action.from)}」未提问或未命中时跳过这个动作`);
  return notes;
}

function renderAction(q, qIndex, ref, action, a) {
  const tool = state.catalog[action.tool];
  const ui = ACTION_UI[action.tool] || { icon: "zap", group: "send" };
  const issue = wf.actionIssue(state.questions, state.catalog, q, ref, action);
  const targets = wf.moveTargets(state.questions, state.catalog, q, ref, action);
  const count = ref.actions.length;
  const move = targets.length > 0 && h("select", {
    class: "act-move", "aria-label": "移到其他选项",
    onchange: (event) => {
      const target = targets[Number(event.target.value)];
      if (!target) return;
      ref.actions.splice(a, 1);
      target.actions.push(action);
      refreshEditor();
    },
  }, h("option", { value: "" }, "移到…"), targets.map((t, i) => h("option", { value: String(i) }, `选项「${slotLabel(q, t)}」`)));
  return h("div", { class: cls("act", `is-${ui.group}`, issue && "is-invalid"), dataset: { a, field: "action" } },
    h("div", { class: "act-head" },
      h("span", { class: "act-icon" }, icon(ui.icon)),
      h("b", null, tool?.label || action.tool),
      h("span", { class: "act-tools" },
        count > 1 && iconButton("up", "上移动作", () => { moveItem(ref.actions, a, -1); refreshEditor(); }, { disabled: a === 0 }),
        count > 1 && iconButton("down", "下移动作", () => { moveItem(ref.actions, a, 1); refreshEditor(); }, { disabled: a === count - 1 }),
        move,
        iconButton("close", `移除动作 ${tool?.label || action.tool}`, () => { ref.actions.splice(a, 1); refreshEditor(); }, { danger: true }))),
    actionParams(q, ref, action),
    actionNotes(q, ref, action).map((note) => h("p", { class: "act-note" }, note)),
    issue && h("p", { class: "act-error" }, issue),
  );
}

// ---------- 判断方式与兜底 ----------

function slotChoices(q) {
  return Object.fromEntries(wf.slotsOf(q).filter((ref) => !wf.isWildcard(ref))
    .map((ref) => [String(wf.slotIndex(q, ref)), slotLabel(q, ref)]));
}

function slotByIndex(q, value) {
  if (value === "") return null;
  return q.type === "boolean" ? q.sides[value] : q.options[Number(value)] || null;
}

function renderAdvanced(q, qIndex) {
  const mode = { inherit: "继承默认模式", model: "模型判断", random: "按权重随机" }[q.decision_mode];
  const privateShown = wf.possibleScenes(q).has("private_passive") || q.private_default;
  const summary = [mode, q.use_threshold ? "用阈值" : "不用阈值",
    `兜底 ${q.default_option ? slotLabel(q, q.default_option) : "无"}`,
    privateShown && `私聊${q.private_default ? `直接选「${slotLabel(q, q.private_default)}」` : "照常判断"}`,
    q.aliases.length && `${q.aliases.length} 个别名`].filter(Boolean).join(" · ");
  const thresholdNote = q.type === "boolean"
    ? "仅模型判断。两侧权重都是 1 时，「是」的概率达到阈值才判为是；调整权重后取加权概率较高的一侧。不按温度抽样。"
    : "仅模型判断。选中答案的原始概率低于插件判断阈值时，按没有选中处理。";
  const details = h("details", {
    class: "q-adv", open: state.advanced.has(q),
    ontoggle: () => (details.open ? state.advanced.add(q) : state.advanced.delete(q)),
  },
    h("summary", null, h("span", { class: "q-section-title" }, "判断方式与兜底"), h("span", { class: "q-adv-sum" }, summary)),
    h("div", { class: "q-adv-body" },
      h("div", { class: "settings-grid" },
        fieldSelect("本题决策", { inherit: "继承默认模式", model: "模型判断", random: "按权重随机" }, q.decision_mode, (value) => {
          q.decision_mode = value;
          refreshEditor(`[data-q="${qIndex}"] [data-field="decision-mode"]`);
        }, { field: "decision-mode" }),
        fieldSelect("没有有效答案时记为", { "": "不记录", ...slotChoices(q) },
          q.default_option ? String(wf.slotIndex(q, q.default_option)) : "", (value) => {
            q.default_option = slotByIndex(q, value);
            refreshEditor(`[data-q="${qIndex}"] [data-field="default_option"]`);
          }, { field: "default_option" }),
        privateShown && fieldSelect("私聊收到消息时", { "": "照常问判断模型", ...slotChoices(q) },
          q.private_default ? String(wf.slotIndex(q, q.private_default)) : "", (value) => {
            q.private_default = slotByIndex(q, value);
            refreshEditor(`[data-q="${qIndex}"] [data-field="private_default"]`);
          }, { field: "private_default" })),
      h("p", { class: "note" }, "兜底答案只写进日志，不执行该选项的动作，也不触发接着问。",
        privateShown ? "私聊直接采用的选项不问判断模型，照常执行动作并触发接着问。" : ""),
      switchField("使用插件判断阈值", q.use_threshold, (v) => { q.use_threshold = v; refreshEditor(); }, thresholdNote),
      q.aliases.length > 0 && h("div", { class: "alias-list" },
        h("span", { class: "field-label" }, "判断模型可能输出的别名"),
        q.aliases.map((alias, i) => h("div", { class: "alias-row", dataset: { i, field: "alias" } },
          h("input", {
            type: "text", class: "mono", value: alias.alias, "aria-label": "别名",
            oninput: (event) => { alias.alias = event.target.value; },
          }),
          icon("arrow"),
          (() => {
            const select = h("select", {
              "aria-label": "对应的选项",
              onchange: (e) => { alias.option = slotByIndex(q, e.target.value); refreshEditor(); },
            }, Object.entries(slotChoices(q)).map(([value, text]) => h("option", { value }, text)));
            select.value = String(wf.slotIndex(q, alias.option));
            return select;
          })(),
          iconButton("close", "移除别名", () => { q.aliases.splice(i, 1); refreshEditor(); }, { danger: true })))),
    ));
  return details;
}

// ---------- 问题操作 ----------

function moveQuestion(qIndex, delta, act) {
  if (!moveItem(state.questions, qIndex, delta)) return;
  const target = qIndex + delta;
  const edge = target === 0 || target === state.questions.length - 1;
  const fallback = act === "up" ? "down" : "up";
  refreshEditor(`[data-q="${target}"] .q-tools [data-act="${edge ? fallback : act}"]`);
}

function moveOption(question, qIndex, oIndex, delta) {
  if (!moveItem(question.options, oIndex, delta)) return;
  refreshEditor(`[data-q="${qIndex}"] [data-o="${oIndex + delta}"] [data-field="opt-criteria"]`);
}

function deleteQuestion(question) {
  const index = qIndexOf(question);
  const lost = wf.runChange(state.questions, state.catalog, wf.deleteQuestion(state.questions, question));
  state.open.delete(question);
  const focusIndex = Math.min(index, state.questions.length - 1);
  renderQuestions(focusIndex >= 0 ? `[data-q="${focusIndex}"] .q-toggle` : undefined);
  markPreviewStale();
  const extra = lost.total ? `，并移除 ${wf.describeLoss(lost)}` : "";
  if (isDirty()) setSaveState("dirty", `已删除「${qName(question)}」${extra}，保存后生效；撤销更改可以找回`);
  else setSaveState("clean");
}

function insertQuestion(question, index) {
  state.questions.splice(index, 0, question);
  state.open.add(question);
  state.fresh = question;
  const focus = wf.isPending(question) ? `[data-q="${index}"] [data-act="add-trigger"]` : `[data-q="${index}"] [data-field="instructions"]`;
  refreshEditor(focus);
  $("questions").querySelector(`[data-q="${index}"]`)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function addEntry(event) {
  const q = wf.templateQuestion(event === wf.POST_EVENT ? "post" : "boolean", state.questions);
  q.triggers = [{ event, ...(event === "timer_elapsed" ? { interval_seconds: 600 } : {}) }];
  insertQuestion(q, state.questions.length);
}

function duplicateQuestion(question) {
  const used = new Set(state.questions.map((q) => q.id.trim()));
  const raw = { ...wf.serializeQuestion(question), id: wf.nextId(`${question.id.trim()}_copy`, used), triggers: [], actions: {} };
  const [copy] = wf.normalizeQuestions([raw]);
  copy.triggers = question.triggers.map((t) => ({ ...t }));
  wf.slotsOf(question).forEach((ref, i) => {
    wf.slotsOf(copy)[i].actions = ref.actions.map((a) => ({ ...a }));
  });
  insertQuestion(copy, qIndexOf(question) + 1);
}

function onAddQuestion(template) {
  $("add-menu").open = false;
  insertQuestion(wf.templateQuestion(template, state.questions), state.questions.length);
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
  const open = new Set([...state.open].map((q) => q.id.trim()));
  setQuestions(saved.questions);
  state.open = new Set(state.questions.filter((q) => open.has(q.id)));
  state.advanced = new Set();
  captureBaseline();
  state.preview.result = null;
  state.preview.stale = false;
  renderPreviewResult();
  renderQuestions();
}

function applyConfig(data, fallbackQuestions) {
  if (data.version !== 7 || !data.actions || !Array.isArray(data.default_questions)) {
    throw new Error("插件版本与页面不匹配，请重载插件后刷新页面");
  }
  state.defaults = data.default_questions;
  state.catalog = data.actions;
  state.events = data.events;
  state.sources = data.candidate_sources || {};
  state.storedVersion = data.stored_version ?? null;
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
  box.textContent = text ? `读取配置时的提示：${text}。检查后保存即可。` : "";
}

async function loadDecisionConfig() {
  const data = await bridge.apiGet("decision-config", { version: 7 });
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
    version: 7,
    decision_mode: getMode(),
    decision_custom_context: $("custom-context").value,
    decision_questions: collectQuestions(),
  };
  state.saving = true;
  setSaveState("saving");
  try {
    const data = await bridge.apiPost("decision-config", payload);
    applyConfig(data, payload.decision_questions);
    showConfigWarning(data.warning);
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
    const note = answer?.note === "private_passive_default_reply" ? "私聊直接采用固定选项，本题无抽样。" : "未记录可用的概率分布。";
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

function callText(call, statusLabels) {
  const parts = [toolLabel(call.name)];
  if (call.when) parts.push(WHEN_LABELS[call.when] || call.when);
  parts.push(statusLabels[call.status] || call.status);
  return parts.join(" · ");
}

function renderLog(item) {
  const action = normalizeLogAction(item.action);
  const isRandom = item.decision_mode === "random";
  const isMixed = item.decision_mode === "mixed";
  const time = splitTime(item.time);
  const answers = Object.entries(item.answers || item.custom_answers || {});
  if (!answers.length && item.length) answers.push(["length", item.length]);
  const hints = Array.isArray(item.session_hints) ? item.session_hints.filter(Boolean) : [];
  const post = item.post_decision;
  const postAnswers = Object.entries(post?.answers || {});
  const calls = item.tool_calls || [];
  const sources = Array.isArray(item.reply_sources) ? item.reply_sources : [];
  const statusLabels = { ...TOOL_STATUS, planned: item.dry_run ? "试运行" : "未确认执行" };

  const main = h(
    "div",
    { class: "log-main" },
    h("span", { class: "log-action" }, ACTION_LABELS[action] || item.action || "未知"),
    h("span", { class: "tag" }, item.trigger_event === "timer_elapsed" ? `${SCENE_LABELS[item.scene]} · 定时` : SCENE_LABELS[item.scene] || item.scene || "未知场景"),
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

  const sub = answers.length || hints.length || postAnswers.length || calls.length || sources.length
    ? h(
      "div",
      { class: "log-sub" },
      sources.length > 0 && h("span", { class: "tag tag-chat", title: "调用会话模型发言的选项" },
        icon("reply"), `发言来源 ${sources.join("、")}`),
      answers.map(([key, value]) =>
        h("span", { class: "tag tag-judge", title: questionText(key) }, h("b", null, key), formatAnswer(value)),
      ),
      postAnswers.map(([key, value]) =>
        h("span", { class: "tag tag-judge tag-post", title: questionText(key) || "会话模型写完回复后判断" },
          h("small", null, "回复后"), h("b", null, key), formatAnswer(value)),
      ),
      hints.length ? h("span", { class: "tag tag-chat" }, `会话提示 ${hints.length} 条`) : null,
      calls.map((call) => h("span", {
        class: cls("tag tag-tool", `is-${call.status}`),
        title: [call.sources?.length ? `来源：${call.sources.join("、")}` : "", call.reason].filter(Boolean).join(" · ") || undefined,
      }, icon("zap"), callText(call, statusLabels))),
    )
    : null;

  const raw = { ...item.raw, ...post?.raw };
  const hasRaw = Object.keys(raw).length > 0;
  const postIssues = Object.entries(post?.outcomes || {});
  const outcomes = Object.entries(item.outcomes || {});
  const reasons = {
    branch_not_selected: "未命中触发条件", preasked_discarded: "预问后丢弃",
    not_asked: "当前场景或候选不适用", missing_answer: "缺少答案", below_threshold: "未过阈值",
    answer_type: "答案类型无效", unknown_option: "未知选项", batch_error: "请求失败",
  };
  const batches = [...(item.batches || []), ...(post?.batches || [])];
  const detail = Array.isArray(item.batches) || calls.length || hints.length || hasRaw || item.target_message_id || item.degraded || post
    ? h(
      "details",
      { class: "log-detail" },
      h("summary", null, hasRaw ? "查看概率对比与详情" : "查看详情"),
      Array.isArray(item.batches) && h("section", { class: "prob-question" }, h("h4", null, `判断请求 · ${batches.length} 次`),
        batches.map((b, i) => h("p", { class: "log-meta" }, `第 ${i + 1} 批 · ${b.duration_ms} ms · 提问 ${b.asked.join("、")}`,
          b.used?.length ? ` · 采用 ${b.used.join("、")}` : "",
          b.discarded?.length ? [" · 丢弃 ", b.discarded.map((id, j) => [j ? "、" : "", h("s", null, id)])] : "",
          b.unanswered?.length ? ` · 无有效答案 ${b.unanswered.join("、")}` : "", b.error || ""))),
      outcomes.length > 0 && h("p", { class: "log-meta" }, outcomes.map(([id, r]) => `${id} · ${reasons[r] || r}`).join("，")),
      post && (postIssues.length || post.error) && h("section", { class: "prob-question" }, h("h4", null, "回复后判断"),
        postIssues.map(([id, o]) =>
          h("p", { class: "log-meta" }, `${id} · ${o.reason || o.status}`)),
        post.error && h("p", { class: "log-error" }, post.error)),
      item.target_message_id
        ? h("p", { class: "log-meta" }, "引用的消息：", h("code", null, item.target_message_id))
        : null,
      hints.length ? h("ul", { class: "log-hints" }, hints.map((text) => h("li", null, text))) : null,
      ...calls.map((call) => h("p", { class: "log-meta" }, callText(call, statusLabels),
        call.sources?.length ? `（${call.sources.join("、")}）` : "", call.reason ? ` · ${call.reason}` : "")),
      !hasRaw && item.degraded ? h("p", { class: "hint" }, "判断失败，本轮没有可展示的概率。") : null,
    )
    : null;

  if (hasRaw) {
    detail.addEventListener("toggle", function showCharts() {
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
  }

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
  speak: (item) => !logHasIssue(item) && ["reply", "proactive"].includes(normalizeLogAction(item.action)),
  silent: (item) => !logHasIssue(item) && normalizeLogAction(item.action) === "none",
  issue: logHasIssue,
};

function logSearchText(item) {
  const answers = { ...(item.answers || item.custom_answers), ...item.post_decision?.answers };
  return [
    item.umo, SCENE_LABELS[item.scene], ACTION_LABELS[normalizeLogAction(item.action)], item.meme_id,
    item.error, item.post_decision?.error, item.target_message_id,
    ...Object.entries(answers).flatMap(([key, value]) => [key, formatAnswer(value), `${key}=${formatAnswer(value)}`]),
    ...(item.session_hints || []),
    ...(item.reply_sources || []),
    ...(item.tool_calls || []).map((call) => toolLabel(call.name)),
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
  refreshEditor();
  if (isDirty()) setSaveState("dirty", "已换成默认问题，保存后生效");
  else setSaveState("clean");
}

function onToggleAll() {
  const allOpen = state.questions.every((q) => state.open.has(q));
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
  // 随机模式下显示各答案的抽中比例和提问时机，切换模式时重画问题。
  input.addEventListener("change", () => refreshEditor());
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
    if (event.target.closest("#preview-fields")) return;
    updateDirty();
    if (event.target.closest("#questions")) {
      // 改名会影响其他卡片里的下拉选项，重画不含输入框的卡片；正在输入的卡片保持不动。
      if (type === "input" && ["id", "opt-id"].includes(event.target.dataset.field)) {
        const editing = event.target.closest(".question");
        for (const card of $("questions").querySelectorAll(".question[data-q]")) {
          if (card === editing) continue;
          const index = Number(card.dataset.q);
          card.replaceWith(renderQuestion(state.questions[index], index));
        }
      }
      markPreviewStale();
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
