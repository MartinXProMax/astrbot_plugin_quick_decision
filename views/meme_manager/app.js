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
      },
      {
        id: "long",
        criteria: "更完整、稍详细的回复",
        session_hint: "本轮回复策略：请给出稍详细、完整的回复，但仍保持自然。",
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

function cloneDefaultQuestions() {
  return JSON.parse(JSON.stringify(DEFAULT_QUESTIONS));
}

function nextId(prefix, used) {
  let index = 1;
  while (used.has(`${prefix}${index}`)) index += 1;
  return `${prefix}${index}`;
}

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
  };
  if (type === "choice") {
    const options = Array.isArray(raw?.options) ? raw.options : [];
    question.options = options.map((option) => ({
      id: String(option?.id || "").trim(),
      criteria: String(option?.criteria || ""),
      session_hint: String(option?.session_hint || ""),
    }));
    if (question.options.length < 2) {
      question.options.push(
        { id: "option_1", criteria: "", session_hint: "" },
        { id: "option_2", criteria: "", session_hint: "" },
      );
    }
  }
  return question;
}

function collectQuestionsFromForm() {
  return questionsState.map((question) => {
    if (question.type === "boolean") {
      return {
        id: question.id.trim(),
        enabled: Boolean(question.enabled),
        type: "boolean",
        instructions: question.instructions,
        true_session_hint: question.true_session_hint,
        false_session_hint: question.false_session_hint,
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
      })),
    };
  });
}

function moveItem(list, index, delta) {
  const next = index + delta;
  if (next < 0 || next >= list.length) return;
  const [item] = list.splice(index, 1);
  list.splice(next, 0, item);
}

function renderQuestions() {
  const root = $("questions");
  root.innerHTML = "";
  if (!questionsState.length) {
    const empty = document.createElement("p");
    empty.className = "hint";
    empty.textContent = "当前没有自定义判断问题。判断模型将只决定是否发言、回哪一条以及是否配图。";
    root.append(empty);
    return;
  }
  questionsState.forEach((question, qIndex) => {
    root.append(renderQuestionCard(question, qIndex));
  });
}

function renderQuestionCard(question, qIndex) {
  const card = document.createElement("article");
  card.className = "question-card";

  const toolbar = document.createElement("div");
  toolbar.className = "question-toolbar";
  const enabledLabel = document.createElement("label");
  enabledLabel.className = "enabled-flag";
  const enabled = document.createElement("input");
  enabled.type = "checkbox";
  enabled.checked = question.enabled !== false;
  enabled.addEventListener("change", () => {
    question.enabled = enabled.checked;
  });
  enabledLabel.append(enabled, document.createTextNode("启用此问题"));
  const up = document.createElement("button");
  up.type = "button";
  up.className = "secondary small";
  up.textContent = "上移";
  up.addEventListener("click", () => {
    moveItem(questionsState, qIndex, -1);
    renderQuestions();
  });
  const down = document.createElement("button");
  down.type = "button";
  down.className = "secondary small";
  down.textContent = "下移";
  down.addEventListener("click", () => {
    moveItem(questionsState, qIndex, 1);
    renderQuestions();
  });
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "danger small";
  remove.textContent = "删除问题";
  remove.addEventListener("click", () => {
    questionsState.splice(qIndex, 1);
    renderQuestions();
  });
  toolbar.append(enabledLabel, up, down, remove);

  const grid = document.createElement("div");
  grid.className = "field-grid";

  const idInput = document.createElement("input");
  idInput.type = "text";
  idInput.value = question.id;
  idInput.placeholder = "例如 length 或 tone";
  idInput.addEventListener("input", () => {
    question.id = idInput.value;
  });
  const idLabel = document.createElement("label");
  idLabel.textContent = "问题 ID";
  grid.append(idLabel, idInput);

  const typeSelect = document.createElement("select");
  const choiceOpt = document.createElement("option");
  choiceOpt.value = "choice";
  choiceOpt.textContent = "选择题";
  const boolOpt = document.createElement("option");
  boolOpt.value = "boolean";
  boolOpt.textContent = "是否题";
  typeSelect.append(choiceOpt, boolOpt);
  typeSelect.value = question.type;
  typeSelect.addEventListener("change", () => {
    question.type = typeSelect.value;
    if (question.type === "choice" && (!question.options || question.options.length < 2)) {
      question.options = [
        { id: "option_1", criteria: "", session_hint: "" },
        { id: "option_2", criteria: "", session_hint: "" },
      ];
    }
    renderQuestions();
  });
  const typeLabel = document.createElement("label");
  typeLabel.textContent = "问题类型";
  grid.append(typeLabel, typeSelect);

  const instructions = document.createElement("textarea");
  instructions.rows = 2;
  instructions.value = question.instructions;
  instructions.placeholder = "写给判断模型看的问题说明";
  instructions.addEventListener("input", () => {
    question.instructions = instructions.value;
  });
  const instLabel = document.createElement("label");
  instLabel.textContent = "判断标准";
  grid.append(instLabel, instructions);

  card.append(toolbar, grid);

  if (question.type === "boolean") {
    const boolGrid = document.createElement("div");
    boolGrid.className = "bool-grid";
    const trueHint = document.createElement("textarea");
    trueHint.rows = 2;
    trueHint.value = question.true_session_hint || "";
    trueHint.placeholder = "判断为「是」时告诉会话模型的内容，可留空";
    trueHint.addEventListener("input", () => {
      question.true_session_hint = trueHint.value;
    });
    const trueLabel = document.createElement("label");
    trueLabel.textContent = "为是时的会话提示";
    const falseHint = document.createElement("textarea");
    falseHint.rows = 2;
    falseHint.value = question.false_session_hint || "";
    falseHint.placeholder = "判断为「否」时告诉会话模型的内容，可留空";
    falseHint.addEventListener("input", () => {
      question.false_session_hint = falseHint.value;
    });
    const falseLabel = document.createElement("label");
    falseLabel.textContent = "为否时的会话提示";
    boolGrid.append(trueLabel, trueHint, falseLabel, falseHint);
    card.append(boolGrid);
  } else {
    const optionsWrap = document.createElement("div");
    optionsWrap.className = "questions";
    (question.options || []).forEach((option, oIndex) => {
      optionsWrap.append(renderOptionCard(question, oIndex));
    });
    const addOption = document.createElement("button");
    addOption.type = "button";
    addOption.className = "secondary small";
    addOption.textContent = "添加选项";
    addOption.addEventListener("click", () => {
      const used = new Set((question.options || []).map((item) => item.id));
      question.options = question.options || [];
      question.options.push({
        id: nextId("option_", used),
        criteria: "",
        session_hint: "",
      });
      renderQuestions();
    });
    card.append(optionsWrap, addOption);
  }
  return card;
}

function renderOptionCard(question, oIndex) {
  const option = question.options[oIndex];
  const card = document.createElement("div");
  card.className = "option-card";
  const toolbar = document.createElement("div");
  toolbar.className = "option-toolbar";
  const title = document.createElement("strong");
  title.textContent = `选项 ${oIndex + 1}`;
  const up = document.createElement("button");
  up.type = "button";
  up.className = "secondary small";
  up.textContent = "上移";
  up.addEventListener("click", () => {
    moveItem(question.options, oIndex, -1);
    renderQuestions();
  });
  const down = document.createElement("button");
  down.type = "button";
  down.className = "secondary small";
  down.textContent = "下移";
  down.addEventListener("click", () => {
    moveItem(question.options, oIndex, 1);
    renderQuestions();
  });
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "danger small";
  remove.textContent = "删除选项";
  remove.disabled = question.options.length <= 2;
  remove.addEventListener("click", () => {
    if (question.options.length <= 2) return;
    question.options.splice(oIndex, 1);
    renderQuestions();
  });
  toolbar.append(title, up, down, remove);

  const grid = document.createElement("div");
  grid.className = "option-grid";
  const idInput = document.createElement("input");
  idInput.type = "text";
  idInput.value = option.id;
  idInput.placeholder = "例如 short";
  idInput.addEventListener("input", () => {
    option.id = idInput.value;
  });
  const idLabel = document.createElement("label");
  idLabel.textContent = "选项 ID";
  const criteria = document.createElement("input");
  criteria.type = "text";
  criteria.value = option.criteria;
  criteria.placeholder = "写给判断模型看的选项说明";
  criteria.addEventListener("input", () => {
    option.criteria = criteria.value;
  });
  const criteriaLabel = document.createElement("label");
  criteriaLabel.textContent = "判断标准";
  const hint = document.createElement("textarea");
  hint.rows = 2;
  hint.value = option.session_hint || "";
  hint.placeholder = "选中后告诉会话模型的内容，可留空";
  hint.addEventListener("input", () => {
    option.session_hint = hint.value;
  });
  const hintLabel = document.createElement("label");
  hintLabel.textContent = "告诉会话模型";
  grid.append(idLabel, idInput, criteriaLabel, criteria, hintLabel, hint);
  card.append(toolbar, grid);
  return card;
}

async function loadDecisionConfig() {
  const data = await bridge.apiGet("decision-config");
  $("custom-context").value = data.decision_custom_context || "";
  reservedIds = new Set(data.reserved_question_ids || [...RESERVED_IDS]);
  questionsState = Array.isArray(data.decision_questions)
    ? data.decision_questions.map(normalizeQuestion)
    : cloneDefaultQuestions();
  renderQuestions();
  if (data.warning) {
    showMessage($("config-msg"), data.warning, false);
  } else {
    showMessage($("config-msg"), "", true);
  }
}

async function saveDecisionConfig() {
  const payload = {
    decision_custom_context: $("custom-context").value,
    decision_questions: collectQuestionsFromForm(),
  };
  const used = new Set();
  for (const question of payload.decision_questions) {
    if (!question.id) {
      showMessage($("config-msg"), "每个问题都需要填写 ID", false);
      return;
    }
    if (reservedIds.has(question.id) || RESERVED_IDS.has(question.id)) {
      showMessage(
        $("config-msg"),
        `问题 id「${question.id}」为内置保留项，请更换`,
        false,
      );
      return;
    }
    if (used.has(question.id)) {
      showMessage($("config-msg"), `问题 id 重复：${question.id}`, false);
      return;
    }
    used.add(question.id);
  }
  $("save-config").disabled = true;
  try {
    const data = await bridge.apiPost("decision-config", payload);
    $("custom-context").value = data.decision_custom_context || "";
    questionsState = Array.isArray(data.decision_questions)
      ? data.decision_questions.map(normalizeQuestion)
      : payload.decision_questions.map(normalizeQuestion);
    renderQuestions();
    showMessage($("config-msg"), "已保存到官方插件配置。重载插件后也会继续生效。", true);
  } catch (error) {
    showMessage($("config-msg"), error.message, false);
  } finally {
    $("save-config").disabled = false;
  }
}


let questionsState = [];
let reservedIds = new Set(RESERVED_IDS);
let currentTab = "decision";

function $(id) {
  return document.getElementById(id);
}

function showMessage(el, text, ok) {
  el.hidden = !text;
  el.textContent = text || "";
  el.className = `msg ${ok ? "ok" : "error"}`;
}

function setTab(name) {
  currentTab = name;
  $("panel-decision").hidden = name !== "decision";
  $("panel-logs").hidden = name !== "logs";
  $("tab-btn-decision").classList.toggle("active", name === "decision");
  $("tab-btn-logs").classList.toggle("active", name === "logs");
}

async function loadStatus() {
  try {
    const status = await bridge.apiGet("status");
    const dry = status.dry_run ? "试运行" : "正常";
    const enabled = status.enabled ? "已启用" : "已关闭";
    const adapter = status.meme_adapter || {};
    const count = adapter.candidate_count ?? status.meme_count ?? 0;
    const source = adapter.available
      ? `meme_manager ${count} 张`
      : "仅文字";
    $("status-pill").textContent = `${enabled} · ${dry} · ${source}`;
    const box = $("adapter-msg");
    if (!adapter.available) {
      box.className = "msg error";
      box.textContent = adapter.message || "未检测到表情包管理器，将只发送文字，不会使用旧自制图库。";
    } else {
      box.className = "hint";
      const ver = adapter.plugin_version ? `（${adapter.plugin_version}）` : "";
      box.textContent =
        `已对接市场插件「表情包管理器」${ver}。请在该插件页面管理图库和逐图描述；本插件只按描述选图，不会再提供自制图库上传或删除。` +
        ` 当前可用 ${count} 张。请在表情包管理器中关闭主动发送表情工具，并把表情发送概率设为 0，避免与本插件各发一张。`;
    }
  } catch (error) {
    $("status-pill").textContent = "状态读取失败";
    $("adapter-msg").className = "msg error";
    $("adapter-msg").textContent = `无法读取状态：${error.message}`;
  }
}

async function loadLogs() {
  try {
    const data = await bridge.apiGet("logs", { limit: 8 });
    const logs = data.logs || [];
    if (!logs.length) {
      $("logs").textContent = "（暂无日志）";
      return;
    }
    $("logs").textContent = logs
      .map((item) => {
        const meme = item.use_meme ? item.meme_id : "无图";
        const err = item.error ? ` 错误=${item.error}` : "";
        const answers = item.custom_answers
          ? ` 判断=${JSON.stringify(item.custom_answers)}`
          : "";
        const hints = Array.isArray(item.session_hints) && item.session_hints.length
          ? ` 提示=${item.session_hints.length}`
          : "";
        const length = item.length || "-";
        return `${item.time}  ${item.scene}  ${item.action}  ${length}  ${meme}  ${item.duration_ms}ms${answers}${hints}${err}`;
      })
      .join("\n");
  } catch (error) {
    $("logs").textContent = `读取日志失败：${error.message}`;
  }
}

async function reload() {
  await loadStatus();
  await loadLogs();
  try {
    await loadDecisionConfig();
  } catch (error) {
    showMessage($("config-msg"), `加载判断配置失败：${error.message}`, false);
  }
}

function renderContext(context) {
  $("heading").textContent = bridge.t("views.meme_manager.heading", "快速决策");
  document.title = bridge.t("views.meme_manager.title", "快速决策");
  $("tab-btn-decision").textContent = bridge.t(
    "views.meme_manager.tab_decision",
    "判断配置",
  );
  $("tab-btn-logs").textContent = bridge.t("views.meme_manager.tab_logs", "决策日志");
  document.documentElement.dataset.theme = context?.isDark ? "dark" : "light";
}


const context = await bridge.ready();
renderContext(context);
bridge.onContext(renderContext);
$("refresh").addEventListener("click", reload);
$("tab-btn-decision").addEventListener("click", () => setTab("decision"));
$("tab-btn-logs").addEventListener("click", () => setTab("logs"));
$("add-question").addEventListener("click", () => {
  const used = new Set(questionsState.map((item) => item.id));
  questionsState.push(
    normalizeQuestion({
      id: nextId("custom_", used),
      enabled: true,
      type: "choice",
      instructions: "",
      options: [
        { id: "option_1", criteria: "", session_hint: "" },
        { id: "option_2", criteria: "", session_hint: "" },
      ],
    }),
  );
  renderQuestions();
});
let restorePending = false;
let restoreTimer = 0;
$("restore-default").addEventListener("click", () => {
  if (!restorePending) {
    restorePending = true;
    showMessage($("config-msg"), "再点一次以确认恢复默认长短问题（自定义上下文不会被清除）", true);
    restoreTimer = window.setTimeout(() => {
      restorePending = false;
    }, 4000);
    return;
  }
  restorePending = false;
  window.clearTimeout(restoreTimer);
  questionsState = cloneDefaultQuestions().map(normalizeQuestion);
  renderQuestions();
  showMessage($("config-msg"), "已在表单中恢复默认长短问题，请点击保存后生效。", true);
});
$("save-config").addEventListener("click", saveDecisionConfig);
await reload();
