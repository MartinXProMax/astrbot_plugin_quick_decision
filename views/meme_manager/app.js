import { probabilityRows } from "./log-probabilities.mjs";

const bridge = window.AstrBotPluginView;

const DEFAULT_QUESTIONS = [
  {
    id: "length",
    enabled: true,
    type: "choice",
    instructions: "如果发言，回复应该简短还是稍详细？",
    options: [
      {
        id: "short",
        criteria: "简短回复，一两句即可",
        session_hint: "本轮回复策略：请给出简短、自然的回复，一两句即可。",
        weight: 1,
      },
      {
        id: "long",
        criteria: "更完整、稍详细的回复",
        session_hint: "本轮回复策略：请给出稍详细、完整的回复，但仍保持自然。",
        weight: 1,
      },
    ],
  },
];

const RESERVED_IDS = new Set([
  "action",
  "target_message_id",
  "use_meme",
  "meme_id",
]);

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

const ICONS = {
  up: '<path d="m6 15 6-6 6 6"/>',
  down: '<path d="m6 9 6 6 6-6"/>',
  trash:
    '<path d="M4 7h16"/><path d="M10 11v6M14 11v6"/><path d="M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12"/><path d="M9 7V4h6v3"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  arrow: '<path d="M4 12h15"/><path d="m13 6 6 6-6 6"/>',
  image:
    '<rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="m21 16-5-5-9 8"/>',
};

// ---------- 小工具 ----------

function $(id) {
  return document.getElementById(id);
}

const PROPERTY_KEYS = new Set(["value", "checked", "disabled", "hidden", "rows"]);

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
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
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
      class: `icon-btn${danger ? " is-danger" : ""}`,
      title: label,
      "aria-label": label,
      disabled,
      dataset: act ? { act } : undefined,
      onclick: onClick,
    },
    icon(name),
  );
}

const NEEDS_AUTOSIZE = !globalThis.CSS?.supports?.("field-sizing", "content");

function autosize(el) {
  if (!NEEDS_AUTOSIZE || !el?.matches?.("textarea.autosize")) return;
  el.style.height = "auto";
  el.style.height = `${el.scrollHeight + 2}px`;
}

function cloneDefaultQuestions() {
  return JSON.parse(JSON.stringify(DEFAULT_QUESTIONS));
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

// ---------- 状态 ----------

const state = {
  questions: [],
  reservedIds: new Set(RESERVED_IDS),
  saved: { context: "", questions: cloneDefaultQuestions() },
  savedSnapshot: "",
  saving: false,
  freshIndex: -1,
  loaded: false,
};

function normalizeQuestion(raw) {
  const type = raw?.type === "boolean" ? "boolean" : "choice";
  const question = {
    id: String(raw?.id || "").trim(),
    enabled: raw?.enabled !== false,
    type,
    instructions: String(raw?.instructions || ""),
    options: [],
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
      weight: option?.weight ?? 1,
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
    question.options.push({ id, criteria: "", session_hint: "", weight: 1 });
  }
}

function collectQuestionsFromForm() {
  return state.questions.map((question) => {
    if (question.type === "boolean") {
      return {
        id: question.id.trim(),
        enabled: Boolean(question.enabled),
        type: "boolean",
        instructions: question.instructions,
        true_session_hint: question.true_session_hint,
        false_session_hint: question.false_session_hint,
        true_weight: question.true_weight,
        false_weight: question.false_weight,
      };
    }
    return {
      id: question.id.trim(),
      enabled: Boolean(question.enabled),
      type: "choice",
      instructions: question.instructions,
      options: (question.options || []).map((option) => ({
        id: String(option.id || "").trim(),
        criteria: option.criteria,
        session_hint: option.session_hint,
        weight: option.weight,
      })),
    };
  });
}

function snapshot() {
  return JSON.stringify({
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
    if (current !== "dirty") setSaveState("dirty");
  } else if (current === "dirty" || current === "error") {
    setSaveState("clean");
  }
}

// ---------- 判断问题 ----------

function renderBuiltinQuestions(questions) {
  const root = $("builtin-questions");
  if (!Array.isArray(questions) || !questions.length) {
    root.replaceChildren(h("p", { class: "hint" }, "未能读取内置题面，请重载插件后刷新。"));
    return;
  }
  root.replaceChildren(...questions.map(({ id, title, variants }) =>
    h("details", null,
      h("summary", null, title, " ", h("code", null, id)),
      ...variants.map(({ when, prompt, options, note }) => h("div", { class: "builtin-variant" },
        h("strong", null, when),
        prompt && h("p", null, prompt),
        options && h("ul", null, Object.entries(options).map(([key, label]) =>
          h("li", null, h("code", null, key), "：", label))),
        note && h("p", { class: "hint" }, note),
      )),
    )
  ));
}

function renderQuestions(focus) {
  const root = $("questions");
  root.replaceChildren();
  if (!state.questions.length) {
    root.append(
      h(
        "li",
        { class: "questions-empty" },
        "现在没有自定义问题，判断模型只回答上面的内置问题。可以点「添加问题」，比如加一个语气或要不要追问。",
      ),
    );
  } else {
    state.questions.forEach((question, index) => {
      root.append(renderQuestion(question, index));
    });
  }
  state.freshIndex = -1;
  if (NEEDS_AUTOSIZE) root.querySelectorAll("textarea.autosize").forEach(autosize);
  if (focus) root.querySelector(focus)?.focus();
}

function renderQuestion(question, qIndex) {
  const total = state.questions.length;
  const card = h("li", {
    class: [
      "question",
      question.enabled ? "" : "is-disabled",
      state.freshIndex === qIndex ? "is-new" : "",
    ]
      .filter(Boolean)
      .join(" "),
    dataset: { q: qIndex },
  });

  const enabledText = h("span", null, question.enabled ? "启用" : "已停用");
  const enabledToggle = h(
    "label",
    { class: "switch", title: "停用后判断模型不会回答这个问题" },
    h("input", {
      type: "checkbox",
      checked: question.enabled,
      "aria-label": "启用此问题",
      onchange: (event) => {
        question.enabled = event.target.checked;
        enabledText.textContent = question.enabled ? "启用" : "已停用";
        card.classList.toggle("is-disabled", !question.enabled);
      },
    }),
    enabledText,
  );

  const typeSwitch = h(
    "div",
    { class: "seg", role: "group", "aria-label": "问题类型" },
    ["choice", "boolean"].map((type) =>
      h(
        "button",
        {
          type: "button",
          "aria-pressed": String(question.type === type),
          onclick: () => {
            if (question.type === type) return;
            question.type = type;
            if (type === "choice") ensureTwoOptions(question);
            renderQuestions(`[data-q="${qIndex}"] .seg [aria-pressed="true"]`);
            updateDirty();
          },
        },
        type === "choice" ? "选择题" : "是否题",
      ),
    ),
  );

  const head = h(
    "div",
    { class: "q-head" },
    h("span", { class: "q-index", title: "会话提示按这个顺序拼接" }, String(qIndex + 1)),
    h(
      "label",
      { class: "q-id" },
      h("span", { class: "sr-only" }, "问题 ID"),
      h("input", {
        type: "text",
        class: "mono",
        value: question.id,
        placeholder: "问题 ID，如 tone",
        spellcheck: "false",
        autocomplete: "off",
        dataset: { field: "id" },
        oninput: (event) => {
          question.id = event.target.value;
        },
      }),
    ),
    typeSwitch,
    h("span", { class: "q-spacer" }),
    enabledToggle,
    h(
      "div",
      { class: "q-tools" },
      iconButton("up", "上移问题", () => moveQuestion(qIndex, -1, "up"), {
        disabled: qIndex === 0,
        act: "up",
      }),
      iconButton("down", "下移问题", () => moveQuestion(qIndex, 1, "down"), {
        disabled: qIndex === total - 1,
        act: "down",
      }),
      iconButton(
        "trash",
        "删除问题",
        () => {
          state.questions.splice(qIndex, 1);
          renderQuestions();
          updateDirty();
        },
        { danger: true },
      ),
    ),
  );

  const instructions = h(
    "label",
    { class: "q-instr" },
    h("span", { class: "field-label" }, "判断标准"),
    h("textarea", {
      class: "autosize",
      rows: 2,
      value: question.instructions,
      placeholder: "写给判断模型的问题，例如：这轮回复应该用什么语气？",
      dataset: { field: "instructions" },
      oninput: (event) => {
        question.instructions = event.target.value;
      },
    }),
  );

  const body = h("div", { class: "q-body" }, instructions);
  if (question.type === "boolean") {
    body.append(renderBooleanLanes(question));
  } else {
    body.append(renderChoiceLanes(question, qIndex));
  }
  card.append(head, body);
  return card;
}

function moveQuestion(qIndex, delta, act) {
  if (!moveItem(state.questions, qIndex, delta)) return;
  const target = qIndex + delta;
  const edge = target === 0 || target === state.questions.length - 1;
  const fallback = act === "up" ? "down" : "up";
  renderQuestions(`[data-q="${target}"] .q-tools [data-act="${edge ? fallback : act}"]`);
  updateDirty();
}

function laneHead(judgeText) {
  return h(
    "div",
    { class: "lane-row lanes-head", "aria-hidden": "true" },
    h("span", { class: "lane-title is-judge" }, judgeText),
    h("span", { class: "lane-title is-chat" }, "会话模型收到"),
  );
}

function chatHint(value, label, onInput) {
  return h("textarea", {
    class: "opt-chat autosize",
    rows: 1,
    value,
    "aria-label": label,
    placeholder: "留空则不告诉会话模型",
    oninput: (event) => onInput(event.target.value),
  });
}

function arrow() {
  return h("span", { class: "opt-arrow", "aria-hidden": "true" }, icon("arrow"));
}

function optionWeight(value, label, field, onInput) {
  return h(
    "label",
    { class: "opt-weight" },
    "权重",
    h("input", {
      type: "number",
      min: 0,
      max: 100,
      step: "any",
      value: value ?? 1,
      "aria-label": label,
      dataset: { field },
      oninput: (event) => onInput(event.target.valueAsNumber),
    }),
  );
}

function renderBooleanLanes(question) {
  const row = (fixed, text, key, weightKey) =>
    h(
      "div",
      { class: "lane-row opt" },
      h(
        "div",
        { class: "opt-judge" },
        h("span", { class: "opt-fixed" }, fixed),
        h("span", { class: "opt-fixed-text" }, text),
      ),
      arrow(),
      chatHint(question[key] || "", `判断为「${fixed}」时告诉会话模型`, (value) => {
        question[key] = value;
      }),
      h("div", { class: "opt-tools" }),
      optionWeight(question[weightKey], `「${fixed}」的选择权重`, weightKey, (value) => {
        question[weightKey] = value;
      }),
    );
  return h(
    "div",
    { class: "lanes" },
    laneHead("判断模型的回答"),
    row("是", "判断为「是」时", "true_session_hint", "true_weight"),
    row("否", "判断为「否」时", "false_session_hint", "false_weight"),
  );
}

function renderChoiceLanes(question, qIndex) {
  const options = question.options || [];
  const lanes = h("div", { class: "lanes" }, laneHead("判断模型看到：选项 ID 和判断标准"));
  options.forEach((option, oIndex) => {
    const n = oIndex + 1;
    lanes.append(
      h(
        "div",
        { class: "lane-row opt", dataset: { o: oIndex } },
        h(
          "div",
          { class: "opt-judge" },
          h("input", {
            type: "text",
            class: "opt-id mono",
            value: option.id,
            placeholder: "选项 ID",
            spellcheck: "false",
            autocomplete: "off",
            "aria-label": `选项 ${n} 的 ID`,
            dataset: { field: "opt-id" },
            oninput: (event) => {
              option.id = event.target.value;
            },
          }),
          h("textarea", {
            class: "opt-criteria autosize",
            rows: 1,
            value: option.criteria,
            placeholder: "什么情况下选它",
            "aria-label": `选项 ${n} 的判断标准`,
            dataset: { field: "opt-criteria" },
            oninput: (event) => {
              option.criteria = event.target.value;
            },
          }),
        ),
        arrow(),
        chatHint(option.session_hint || "", `选中选项 ${n} 时告诉会话模型`, (value) => {
          option.session_hint = value;
        }),
        h(
          "div",
          { class: "opt-tools" },
          iconButton("up", `上移选项 ${n}`, () => moveOption(question, qIndex, oIndex, -1), {
            disabled: oIndex === 0,
          }),
          iconButton("down", `下移选项 ${n}`, () => moveOption(question, qIndex, oIndex, 1), {
            disabled: oIndex === options.length - 1,
          }),
          iconButton(
            "trash",
            options.length <= 2 ? "至少保留两个选项" : `删除选项 ${n}`,
            () => {
              if (question.options.length <= 2) return;
              question.options.splice(oIndex, 1);
              renderQuestions();
              updateDirty();
            },
            { disabled: options.length <= 2, danger: true },
          ),
        ),
        optionWeight(option.weight, `选项 ${n} 的选择权重`, "opt-weight", (value) => {
          option.weight = value;
        }),
      ),
    );
  });

  const addOption = h(
    "button",
    {
      type: "button",
      class: "btn btn-add-opt",
      onclick: () => {
        const used = new Set(question.options.map((item) => item.id));
        question.options.push({ id: nextId("option_", used), criteria: "", session_hint: "", weight: 1 });
        const last = question.options.length - 1;
        renderQuestions(`[data-q="${qIndex}"] [data-o="${last}"] [data-field="opt-criteria"]`);
        updateDirty();
      },
    },
    icon("plus"),
    "添加选项",
  );
  return h("div", null, lanes, h("div", { class: "q-foot" }, addOption));
}

function moveOption(question, qIndex, oIndex, delta) {
  if (!moveItem(question.options, oIndex, delta)) return;
  renderQuestions(`[data-q="${qIndex}"] [data-o="${oIndex + delta}"] [data-field="opt-criteria"]`);
  updateDirty();
}

// ---------- 校验（与后端 strict 规则一致） ----------

function validate(questions) {
  const used = new Set();
  for (const [q, question] of questions.entries()) {
    const label = question.id || `第 ${q + 1} 个问题`;
    if (!question.id) {
      return { q, field: "id", message: `第 ${q + 1} 个问题还没有填写 ID` };
    }
    if (state.reservedIds.has(question.id) || RESERVED_IDS.has(question.id)) {
      return { q, field: "id", message: `「${question.id}」是内置问题的 ID，请换一个` };
    }
    if (used.has(question.id)) {
      return { q, field: "id", message: `问题 ID「${question.id}」重复了` };
    }
    used.add(question.id);
    if (!question.instructions.trim()) {
      return { q, field: "instructions", message: `问题「${label}」还没有填写判断标准` };
    }
    if (question.type === "boolean") {
      for (const field of ["true_weight", "false_weight"]) {
        if (!Number.isFinite(question[field]) || question[field] < 0 || question[field] > 100) {
          return { q, field, message: `问题「${label}」的权重必须是 0 到 100 之间的有限数字` };
        }
      }
      if (question.true_weight === 0 && question.false_weight === 0) {
        return { q, field: "true_weight", message: `问题「${label}」至少需要一个权重大于 0 的选项` };
      }
      continue;
    }
    const optionIds = new Set();
    for (const [o, option] of question.options.entries()) {
      if (!option.id) {
        return { q, o, field: "opt-id", message: `问题「${label}」的选项 ${o + 1} 还没有填写 ID` };
      }
      if (optionIds.has(option.id)) {
        return { q, o, field: "opt-id", message: `问题「${label}」的选项 ID「${option.id}」重复了` };
      }
      optionIds.add(option.id);
      if (!option.criteria.trim()) {
        return {
          q,
          o,
          field: "opt-criteria",
          message: `问题「${label}」的选项「${option.id}」还没有填写判断标准`,
        };
      }
      if (!Number.isFinite(option.weight) || option.weight < 0 || option.weight > 100) {
        return { q, o, field: "opt-weight", message: `问题「${label}」的选项「${option.id}」权重必须是 0 到 100 之间的有限数字` };
      }
    }
    if (!question.options.some((option) => option.weight > 0)) {
      return { q, o: 0, field: "opt-weight", message: `问题「${label}」至少需要一个权重大于 0 的选项` };
    }
  }
  return null;
}

function markInvalid(error) {
  const card = $("questions").querySelector(`[data-q="${error.q}"]`);
  const scope = error.o === undefined ? card : card?.querySelector(`[data-o="${error.o}"]`);
  const field = scope?.querySelector(`[data-field="${error.field}"]`);
  if (!field) return;
  field.classList.add("is-invalid");
  field.scrollIntoView({ block: "center", behavior: "smooth" });
  field.focus({ preventScroll: true });
}

// ---------- 读取与保存 ----------

function updateContextCount() {
  const length = $("custom-context").value.trim().length;
  $("context-count").textContent = length ? `${length} 字` : "未填写";
}

function fillForm(saved) {
  $("custom-context").value = saved.context;
  updateContextCount();
  state.questions = saved.questions.map(normalizeQuestion);
  renderQuestions();
}

function applyConfig(data, fallbackQuestions) {
  renderBuiltinQuestions(data.builtin_questions);
  if (Array.isArray(data.reserved_question_ids)) {
    state.reservedIds = new Set(data.reserved_question_ids);
  }
  const questions = Array.isArray(data.decision_questions)
    ? data.decision_questions
    : fallbackQuestions || cloneDefaultQuestions();
  state.saved = {
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
  const payload = {
    decision_custom_context: $("custom-context").value,
    decision_questions: collectQuestionsFromForm(),
  };
  const error = validate(payload.decision_questions);
  if (error) {
    setSaveState("error", error.message);
    markInvalid(error);
    return;
  }
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
  const now = new Date();
  const today =
    Number(year) === now.getFullYear() &&
    Number(month) === now.getMonth() + 1 &&
    Number(date) === now.getDate();
  return { day: today ? "今天" : `${month}-${date}`, clock };
}

function formatAnswer(value) {
  if (value === true) return "是";
  if (value === false) return "否";
  if (value === null || value === undefined) return "—";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

const percent = new Intl.NumberFormat("zh-CN", { style: "percent", maximumFractionDigits: 2 });

function probabilityBar(label, value, series) {
  return h("div", { class: `prob-bar ${series}` },
    h("span", null, label),
    h("span", { class: "prob-track", "aria-hidden": "true" },
      value !== null && h("span", { style: `width: ${value * 100}%` })),
    h("span", { class: "prob-value" }, value === null ? "—" : percent.format(value)),
  );
}

function renderProbabilityChart(id, answer) {
  const rows = probabilityRows(answer);
  const title = h("h4", null, id);
  if (!rows.length) {
    const note = answer?.note === "private_passive_default_reply" ? "私聊固定回复，无模型概率。" : "未记录可用的概率分布。";
    return h("section", { class: "prob-question" }, title, h("p", { class: "hint" }, note));
  }
  const isBoolean = !answer.probabilities;
  const optionName = (key) => isBoolean ? (key === "true" ? "是" : "否") : key;
  const selected = answer.selected_choice ?? answer.choice;
  const info = [];
  if (Number.isFinite(answer.selection_temperature)) info.push(`温度 ${answer.selection_temperature}`);
  if (selected != null) {
    info.push(`${Object.hasOwn(answer, "selected_choice") ? "调整后选中" : "模型原选"} ${optionName(selected)}`);
    const chance = answer.selection_probabilities?.[selected];
    if (Number.isFinite(chance)) info.push(`实际抽中概率 ${percent.format(chance)}`);
  }
  if (rows[0].weighted === null) info.push("无法计算加权分布");
  return h("section", { class: "prob-question" }, title,
    info.length ? h("p", { class: "log-meta" }, info.join(" · ")) : null,
    rows.map(({ key, original, weight, weighted }) => h("div", { class: "prob-option" },
      h("div", { class: "prob-option-name" }, h("b", null, optionName(key)),
        Number.isFinite(weight) && weight !== 1 ? h("small", null, `权重 ×${weight}`) : null),
      probabilityBar("原始", original, "original"),
      probabilityBar("加权后", weighted, "weighted"),
    )),
  );
}

function renderLog(item) {
  const action = normalizeAction(item.action);
  const time = splitTime(item.time);
  const answers = item.custom_answers && typeof item.custom_answers === "object"
    ? Object.entries(item.custom_answers)
    : [];
  if (!answers.length && item.length) answers.push(["length", item.length]);
  const hints = Array.isArray(item.session_hints) ? item.session_hints.filter(Boolean) : [];

  const main = h(
    "div",
    { class: "log-main" },
    h("span", { class: "log-action" }, ACTION_LABELS[action] || item.action || "未知"),
    h("span", { class: "tag" }, SCENE_LABELS[item.scene] || item.scene || "未知场景"),
    item.use_meme
      ? h("span", { class: "tag", title: "配图" }, icon("image"), item.meme_id || "配图")
      : null,
    item.dry_run ? h("span", { class: "tag tag-flag", title: "只记录，未发送" }, "试运行") : null,
    item.degraded ? h("span", { class: "tag tag-flag", title: "判断失败后按降级规则处理" }, "已降级") : null,
    item.official_llm ? h("span", { class: "tag", title: "文字由官方主 Agent 生成" }, "官方主 Agent") : null,
    h("span", { class: "log-dur" }, `${item.duration_ms ?? "—"} ms`),
  );

  const sub = answers.length || hints.length
    ? h(
        "div",
        { class: "log-sub" },
        answers.map(([key, value]) =>
          h("span", { class: "tag tag-judge" }, h("b", null, key), formatAnswer(value)),
        ),
        hints.length ? h("span", { class: "tag tag-chat" }, `会话提示 ${hints.length} 条`) : null,
      )
    : null;

  const raw = item.raw && typeof item.raw === "object" && Object.keys(item.raw).length
    ? item.raw
    : null;
  const detail = hints.length || raw || item.target_message_id || item.degraded
    ? h(
        "details",
        { class: "log-detail" },
        h("summary", null, raw ? "查看概率对比与详情" : "查看详情"),
        item.target_message_id
          ? h("p", { class: "log-meta" }, "回复的消息：", h("code", null, item.target_message_id))
          : null,
        hints.length ? h("ul", { class: "log-hints" }, hints.map((text) => h("li", null, text))) : null,
        !raw && item.degraded ? h("p", { class: "hint" }, "判断失败，本轮没有可展示的概率。") : null,
      )
    : null;

  if (raw) detail.addEventListener("toggle", function showCharts() {
    if (!detail.open) return;
    detail.removeEventListener("toggle", showCharts);
    detail.append(
      h("p", { class: "hint" }, "加权后按本轮选项权重归一化，温度在此后生效。"),
      ...Object.entries(raw).map(([id, answer]) => renderProbabilityChart(id, answer)),
      h("details", { class: "prob-raw" }, h("summary", null, "查看原始数据"),
        h("pre", { class: "log-raw" }, JSON.stringify(raw, null, 2))),
    );
  });

  return h(
    "li",
    { class: "log", dataset: { action } },
    h("div", { class: "log-time" }, h("strong", null, time.clock), time.day),
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

async function loadLogs() {
  const limit = Number($("log-limit").value) || 20;
  const list = $("logs");
  const empty = $("logs-empty");
  try {
    const data = await bridge.apiGet("logs", { limit });
    const logs = data.logs || [];
    list.replaceChildren(...logs.map(renderLog));
    empty.hidden = logs.length > 0;
    empty.textContent = "还没有决策记录。机器人收到消息并完成一次判断后，结果会出现在这里。";
  } catch (error) {
    list.replaceChildren();
    empty.hidden = false;
    empty.textContent = `读取日志失败：${error.message}`;
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
      if (!state.loaded) renderBuiltinQuestions(null);
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
  button.textContent = "恢复默认长短问题";
}

function onRestoreDefault() {
  const button = $("restore-default");
  if (!button.classList.contains("is-confirm")) {
    button.classList.add("is-confirm");
    button.textContent = "再点一次确认恢复";
    setSaveState("notice", "确认后问题列表会换成默认长短问题，背景信息不受影响");
    restoreTimer = window.setTimeout(() => {
      resetRestoreButton();
      setSaveState(isDirty() ? "dirty" : "clean");
    }, 4000);
    return;
  }
  resetRestoreButton();
  state.questions = cloneDefaultQuestions().map(normalizeQuestion);
  renderQuestions();
  if (isDirty()) setSaveState("dirty", "已换成默认长短问题，保存后生效");
  else setSaveState("clean");
}

function onAddQuestion() {
  const used = new Set(state.questions.map((item) => item.id));
  state.questions.push(
    normalizeQuestion({
      id: nextId("custom_", used),
      enabled: true,
      type: "choice",
      instructions: "",
      options: [],
    }),
  );
  state.freshIndex = state.questions.length - 1;
  renderQuestions(`[data-q="${state.freshIndex}"] [data-field="instructions"]`);
  $("questions").lastElementChild?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  updateDirty();
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
$("add-question").addEventListener("click", onAddQuestion);
$("restore-default").addEventListener("click", onRestoreDefault);
$("save-config").addEventListener("click", saveDecisionConfig);
$("discard").addEventListener("click", onDiscard);
$("custom-context").addEventListener("input", updateContextCount);

// 表单里任何输入都会刷新「未保存」状态，并清掉对应输入框的错误标记。
for (const type of ["input", "change"]) {
  $("panel-decision").addEventListener(type, (event) => {
    event.target.classList?.remove("is-invalid");
    autosize(event.target);
    updateDirty();
  });
}

window.addEventListener("beforeunload", (event) => {
  if (isDirty()) event.preventDefault();
});

await reload();
