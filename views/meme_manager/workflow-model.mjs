// 选项动作编辑器的数据层：读入、序列化、校验、级联清理和流程计算，不依赖 DOM。
// 编辑期间题目、选项和动作来源都用对象引用，保存时才写成 ID，改名和排序不会断开引用。

export const SCENE_KEYS = ["group_passive", "private_passive", "group_proactive", "private_proactive"];

// 事件可能出现在哪些场景。正文生成完成可以发生在任何场景。
export const EVENT_SCENES = {
  message_received: ["group_passive", "private_passive"],
  silence_elapsed: ["group_proactive", "private_proactive"],
  timer_elapsed: ["group_proactive", "private_proactive"],
  reply_generated: SCENE_KEYS,
};

export const POST_EVENT = "reply_generated";
const PRE_EVENTS = new Set(["message_received", "silence_elapsed", "timer_elapsed"]);

// 与后端 send_meme 的缺省时机一致，省略 when 即为判断后立即发送。
const DEFAULT_WHEN = "on_match";

const CANDIDATE_CATALOGS = {
  options: { label: "固定选项" }, recent_messages: { label: "最近用户消息" },
  meme_categories: { label: "图库分类", children: "memes" }, memes: { label: "全部图库图片" },
};
export function configureCandidateCatalogs(catalogs) {
  Object.assign(CANDIDATE_CATALOGS, catalogs || {});
}
export function candidateType(q, child = false, seen = new Set()) {
  if (!q || seen.has(q)) return "";
  seen.add(q);
  const type = q.candidate_source === "question" ? candidateType(q.candidate_from, true, seen) : q.candidate_source;
  return child ? CANDIDATE_CATALOGS[type]?.children || "" : type;
}
export function candidateSourceChoices(questions, q) {
  return questions.filter((parent) => parent !== q && parent.enabled && candidateType(parent, true)
    && !(q.stage === "pre_reply" && parent.stage === "post_reply") && !wouldCycle(questions, q, parent));
}
export function candidateSourceIssue(q, questions) {
  if (q.candidate_source !== "question" || !q.enabled) return null;
  if (!q.candidate_from || !questions.includes(q.candidate_from)) return "候选来源已删除或尚未设置，请重新选择来源问题";
  if (!q.candidate_from.enabled) return "候选来源问题已停用";
  if (q.stage === "pre_reply" && q.candidate_from.stage === "post_reply") return "不能读取正文生成后才有的候选";
  if (!candidateType(q.candidate_from, true)) return "来源问题不能提供下级候选";
  if (wouldCycle(questions, q, q.candidate_from)) return "候选来源与触发条件会形成循环";
  return null;
}

export function previewCandidateItems(q, catalogs, choices, seen = new Set()) {
  if (!q || !q.enabled || seen.has(q)) return [];
  seen.add(q);
  if (q.candidate_source !== "question") return catalogs[q.candidate_source] || [];
  const parent = q.candidate_from;
  if (!parent?.enabled) return [];
  const items = previewCandidateItems(parent, catalogs, choices, seen);
  const first = slotsOf(parent).find((ref) => ref.weight > 0);
  const key = choices.get(parent) ?? (first ? keyOf(first) : "");
  return (key === "*" ? items[0] : items.find((item) => item.id === key))?.children || [];
}

// ---------- 选项 ----------

export function keyOf(ref) {
  return ref.side ?? String(ref.id ?? "").trim();
}

export function slotsOf(q) {
  return q.type === "boolean" ? [q.sides.true, q.sides.false] : q.options;
}

export function findSlot(q, key) {
  return slotsOf(q).find((ref) => keyOf(ref) === key) ?? null;
}

// 校验和定位使用的选项编号：是否题用 true/false，选择题用下标。
export function slotIndex(q, ref) {
  return ref.side ?? q.options.indexOf(ref);
}

export function isDynamic(q) {
  return q.candidate_source !== "options";
}

export function isWildcard(ref) {
  return ref.side === undefined && String(ref.id).trim() === "*";
}

export function effectiveMode(q, globalMode) {
  return q.decision_mode === "inherit" ? globalMode : q.decision_mode;
}

function weightOf(value) {
  return value === undefined || value === null ? 1 : Number(value);
}

function freshSides(weights = {}) {
  return {
    true: { side: "true", weight: weightOf(weights.true), actions: [] },
    false: { side: "false", weight: weightOf(weights.false), actions: [] },
  };
}

export function newOption(id, criteria = "", weight = 1) {
  return { id, criteria, weight, actions: [] };
}

// ---------- 读入与序列化 ----------

export function normalizeQuestions(rawList) {
  const list = Array.isArray(rawList) ? rawList : [];
  const questions = list.map((raw) => {
    const type = raw?.type === "boolean" ? "boolean" : "choice";
    const q = {
      id: String(raw?.id ?? "").trim(),
      enabled: raw?.enabled !== false,
      type,
      instructions: String(raw?.instructions ?? ""),
      use_threshold: !!raw?.use_threshold,
      decision_mode: raw?.decision_mode === "model" || raw?.decision_mode === "random" ? raw.decision_mode : "inherit",
      stage: raw?.stage === "post_reply" ? "post_reply" : "pre_reply",
      candidate_source: raw?.candidate_source?.question ? "question"
        : raw?.candidate_source?.catalog || raw?.candidate_source || "options",
      candidate_from: null,
      candidate_from_id: raw?.candidate_source?.question || "",
      requires_candidates: typeof raw?.requires_candidates === "string" ? raw.requires_candidates : "",
      scenes: Array.isArray(raw?.scenes) ? SCENE_KEYS.filter((s) => raw.scenes.includes(s)) : [],
      options: [],
      sides: null,
      triggers: [],
      default_option: null,
      private_default: null,
      aliases: [],
    };
    if (type === "boolean") {
      q.sides = freshSides({ true: raw?.true_weight, false: raw?.false_weight });
    } else {
      q.options = (Array.isArray(raw?.options) ? raw.options : []).map((o) =>
        newOption(String(o?.id ?? "").trim(), String(o?.criteria ?? ""), weightOf(o?.weight)));
    }
    return q;
  });
  const byId = new Map(questions.map((q) => [q.id, q]));
  questions.forEach((q, index) => {
    const raw = list[index] || {};
    q.candidate_from = byId.get(q.candidate_from_id) || null;
    q.triggers = (Array.isArray(raw.triggers) ? raw.triggers : []).flatMap((t) => {
      if (typeof t?.event === "string") return [{ ...t }];
      const parent = byId.get(t?.question);
      const option = parent ? findSlot(parent, String(t?.option)) : null;
      return option ? [{ parent, option, ask_with_parent: !!t.ask_with_parent }] : [];
    });
    for (const [key, actions] of Object.entries(raw.actions && typeof raw.actions === "object" ? raw.actions : {})) {
      const ref = findSlot(q, key);
      if (!ref || !Array.isArray(actions)) continue;
      ref.actions = actions.map((a) => normalizeAction(a, q, byId)).filter(Boolean);
    }
    for (const field of ["default_option", "private_default"]) {
      q[field] = typeof raw[field] === "string" && raw[field] ? findSlot(q, raw[field]) : null;
    }
    q.aliases = Object.entries(raw.aliases && typeof raw.aliases === "object" ? raw.aliases : {}).flatMap(([alias, key]) => {
      const option = findSlot(q, String(key));
      return option ? [{ alias, option }] : [];
    });
  });
  return questions;
}

function normalizeAction(raw, owner, byId) {
  if (!raw || typeof raw.tool !== "string") return null;
  const action = { tool: raw.tool, from: null };
  if (typeof raw.from === "string") {
    const source = byId.get(raw.from);
    // 读入的配置已由后端校验，找不到来源只可能是手工改坏，丢掉比改成取本题答案安全。
    if (!source) return null;
    action.from = source === owner ? null : source;
  }
  if (typeof raw.text === "string") action.text = raw.text;
  if (typeof raw.label === "string") action.label = raw.label;
  if (typeof raw.when === "string") action.when = raw.when;
  if (raw.tool === "send_meme" && !action.when) action.when = DEFAULT_WHEN;
  return action;
}

export function serializeAction(action) {
  const out = { tool: action.tool };
  if (action.from) out.from = action.from.id.trim();
  if (action.text !== undefined) out.text = action.text;
  if (action.label !== undefined && action.label.trim()) out.label = action.label;
  if (action.when !== undefined && action.when !== DEFAULT_WHEN) out.when = action.when;
  return out;
}

export function serializeQuestion(q) {
  const data = {
    id: q.id.trim(),
    enabled: q.enabled,
    type: q.type,
    instructions: q.instructions,
    use_threshold: q.use_threshold,
  };
  if (q.type === "choice") {
    data.options = q.options.map((o) => ({ id: o.id.trim(), criteria: o.criteria, weight: o.weight }));
  } else {
    data.true_weight = q.sides.true.weight;
    data.false_weight = q.sides.false.weight;
  }
  data.triggers = q.triggers.map((t) => t.event
    ? { ...t }
    : { question: t.parent.id.trim(), option: keyOf(t.option), ...(t.ask_with_parent ? { ask_with_parent: true } : {}) });
  data.actions = {};
  for (const ref of slotsOf(q)) {
    if (ref.actions.length) data.actions[keyOf(ref)] = ref.actions.map(serializeAction);
  }
  if (q.stage === "post_reply") data.stage = "post_reply";
  if (q.decision_mode !== "inherit") data.decision_mode = q.decision_mode;
  if (q.candidate_source !== "options") data.candidate_source = q.candidate_source === "question"
    ? { question: q.candidate_from?.id.trim() || q.candidate_from_id || "" } : q.candidate_source;
  if (q.requires_candidates) data.requires_candidates = q.requires_candidates;
  if (q.scenes.length) data.scenes = SCENE_KEYS.filter((s) => q.scenes.includes(s));
  if (q.default_option) data.default_option = keyOf(q.default_option);
  if (q.private_default) data.private_default = keyOf(q.private_default);
  if (q.aliases.length) data.aliases = Object.fromEntries(q.aliases.map((a) => [a.alias.trim(), keyOf(a.option)]));
  return data;
}

// ---------- 引用关系 ----------

export function childLinks(questions, parent) {
  return questions.flatMap((child) => child.triggers
    .filter((t) => t.parent === parent)
    .map((trigger) => ({ child, trigger, option: trigger.option })));
}

export function isPending(q) {
  return q.triggers.length === 0;
}

// child 已经是 parent 的祖先（或就是它）时，再让 parent 触发 child 会成环。
export function wouldCycle(questions, child, parent) {
  const seen = new Set();
  const stack = [child];
  while (stack.length) {
    const q = stack.pop();
    if (q === parent) return true;
    if (seen.has(q)) continue;
    seen.add(q);
    for (const child of questions) {
      if (child.candidate_from === q || child.triggers.some((t) => t.parent === q)) stack.push(child);
    }
  }
  return false;
}

export function findCycle(questions) {
  const visiting = new Set();
  const done = new Set();
  const path = [];
  function walk(q) {
    if (done.has(q)) return null;
    if (visiting.has(q)) return [...path.slice(path.indexOf(q)), q];
    visiting.add(q);
    path.push(q);
    for (const child of questions.filter((item) => item.candidate_from === q || item.triggers.some((t) => t.parent === q))) {
      const cycle = walk(child);
      if (cycle) return cycle;
    }
    path.pop();
    visiting.delete(q);
    done.add(q);
    return null;
  }
  for (const q of questions) {
    const cycle = walk(q);
    if (cycle) return cycle;
  }
  return null;
}

export function eventAllowed(q, event) {
  return q.stage === "post_reply" ? event === POST_EVENT : PRE_EVENTS.has(event);
}

// 本题会在哪些场景被提问：事件给出场景，上游来源继承父题的场景，最后与本题的场景限制取交集。
export function possibleScenes(q, memo = new Map()) {
  if (memo.has(q)) return memo.get(q);
  memo.set(q, new Set());
  const found = new Set();
  for (const t of q.triggers) {
    const scenes = t.event ? EVENT_SCENES[t.event] || [] : t.parent.enabled ? possibleScenes(t.parent, memo) : [];
    for (const scene of scenes) found.add(scene);
  }
  const sourceScenes = q.candidate_source === "question"
    ? q.candidate_from?.enabled ? possibleScenes(q.candidate_from, memo) : new Set() : found;
  const result = new Set([...found].filter((s) => sourceScenes.has(s) && (!q.scenes.length || q.scenes.includes(s))));
  memo.set(q, result);
  return result;
}

// ---------- 动作 ----------

// 动作可以取答案的题：null 表示本题。带候选类型要求的动作只能取对应来源的题，取本题时限于「每条候选」行。
export function sourceChoices(questions, catalog, q, ref, toolName) {
  const tool = catalog[toolName];
  if (!tool) return [];
  const ok = (source) => !(q.stage === "pre_reply" && source.stage === "post_reply")
    && (!tool.source || (candidateType(source) === tool.source && (source !== q || isWildcard(ref))));
  const list = ok(q) ? [null] : [];
  for (const other of questions) if (other !== q && ok(other)) list.push(other);
  return list;
}

export function actionAvailability(questions, catalog, q, ref, toolName) {
  const tool = catalog[toolName];
  if (!tool) return "未知动作";
  if (!tool.stages.includes(q.stage)) return q.stage === "post_reply" ? "正文已生成，不能再使用" : "只能在正文生成后使用";
  if (toolName === "request_reply" && ref.actions.some((a) => a.tool === toolName)) return "已添加";
  if (tool.source && !sourceChoices(questions, catalog, q, ref, toolName).length) {
    if (isDynamic(q) && candidateType(q) === tool.source && !isWildcard(ref)) return "固定选项没有选中的候选";
    return tool.source === "recent_messages" ? "需要一道从最近用户消息中选择的题" : "需要一道从图库表情中选择的题";
  }
  return null;
}

export function newAction(questions, catalog, q, ref, toolName) {
  const tool = catalog[toolName];
  const action = { tool: toolName, from: null };
  if (tool?.parameters?.includes("from")) action.from = sourceChoices(questions, catalog, q, ref, toolName)[0] ?? null;
  if (tool?.when?.length) action.when = tool.when.includes("after_send") ? "after_send" : tool.when[0];
  return action;
}

// 引用失效、阶段或候选类型不符：这些会随结构修改出现，由级联清理处理。
function structuralActionIssue(questions, catalog, q, ref, action) {
  const tool = catalog[action.tool];
  if (!tool) return "未知动作";
  if (!tool.stages.includes(q.stage)) return q.stage === "post_reply" ? "正文已生成，不能使用这个动作" : "这个动作只能在正文生成后使用";
  const source = action.from ?? q;
  if (!questions.includes(source)) return "引用的题目已删除";
  if (q.stage === "pre_reply" && source.stage === "post_reply") return "不能引用正文生成后才有的答案";
  const unresolvedDisabled = !source.enabled && source.candidate_source === "question" && !candidateType(source);
  if (tool.source && ((!unresolvedDisabled && candidateType(source) !== tool.source) || (source === q && !isWildcard(ref)))) {
    return tool.source === "recent_messages" ? "需要从最近用户消息中选择的题" : "需要从图库表情中选择的题";
  }
  return null;
}

export function actionIssue(questions, catalog, q, ref, action) {
  const structural = structuralActionIssue(questions, catalog, q, ref, action);
  if (structural) return structural;
  const tool = catalog[action.tool];
  const data = serializeAction(action);
  for (const key of Object.keys(data)) {
    if (key !== "tool" && !tool.parameters.includes(key)) return "动作参数无效";
  }
  if (action.text !== undefined && action.from) return "内容来源和固定文本只能选一个";
  if (action.text !== undefined && !action.text.trim()) return "固定文本还没有填写";
  if (action.when !== undefined && !(tool.when || []).includes(action.when)) return "执行时机无效";
  return null;
}

export function moveTargets(questions, catalog, q, from, action) {
  return slotsOf(q).filter((ref) => ref !== from
    && !structuralActionIssue(questions, catalog, q, ref, action)
    && !(action.tool === "request_reply" && ref.actions.some((a) => a.tool === "request_reply")));
}

// ---------- 校验 ----------

function weightError(value) {
  return !Number.isFinite(value) || value < 0 || value > 100;
}

function triggerKey(t) {
  return t.event ? `event:${t.event}` : t.option;
}

// 与 config_util._validate_triggers、workflow_actions.validate_actions 对齐，并定位到字段。
export function questionIssues(q, questions, catalog, labelOf = keyOf) {
  const issues = [];
  const name = q.id.trim() ? `「${q.id.trim()}」` : "这道题";
  const slots = slotsOf(q);
  if (!q.id.trim()) issues.push({ field: "id", message: "还没有填写题目 ID" });
  if (!q.instructions.trim()) issues.push({ field: "instructions", message: `${name}还没有填写要问判断模型的问题` });
  if (q.type === "choice") {
    const seen = new Set();
    q.options.forEach((option, o) => {
      const id = option.id.trim();
      if (!id) issues.push({ o, field: "opt-id", message: `${name}的选项 ${o + 1} 还没有填写 ID` });
      else if (seen.has(id)) issues.push({ o, field: "opt-id", message: `${name}的选项 ID「${id}」重复了` });
      else if (id === "*" && !isDynamic(q)) issues.push({ o, field: "opt-id", message: `${name}使用固定选项，选项 ID 不能是 *` });
      else if (q.aliases.some((a) => a.alias.trim() === id)) issues.push({ o, field: "opt-id", message: `${name}的选项 ID「${id}」与别名重名` });
      seen.add(id);
      if (!option.criteria.trim()) issues.push({ o, field: "opt-criteria", message: `${name}的选项「${id || o + 1}」还没有填写说明` });
    });
    if (q.options.length < 2) issues.push({ field: "options", message: `${name}至少需要两个选项` });
  }
  for (const ref of slots) {
    if (weightError(ref.weight)) {
      issues.push({ o: slotIndex(q, ref), field: "weight", message: `${name}的选项「${labelOf(ref)}」权重必须是 0 到 100 之间的数字` });
    }
  }
  if (slots.length && slots.every((ref) => !weightError(ref.weight)) && !slots.some((ref) => ref.weight > 0)) {
    issues.push({ o: slotIndex(q, slots[0]), field: "weight", message: `${name}至少需要一个权重大于 0 的选项` });
  }
  if (isDynamic(q) && (q.type !== "choice" || !q.options.some(isWildcard))) {
    issues.push({ field: "source", message: `${name}使用动态候选，需要选择题和「每条候选」行` });
  }
  const sourceIssue = candidateSourceIssue(q, questions);
  if (sourceIssue) issues.push({ field: "source", message: `${name}：${sourceIssue}` });
  const seenTriggers = new Set();
  q.triggers.forEach((t, i) => {
    if (q.candidate_source === "question" && t.ask_with_parent) {
      issues.push({ t: i, field: "timing", message: `${name}需要等待候选来源的答案，不能同批提前提问` });
    }
    const key = triggerKey(t);
    if (seenTriggers.has(key)) issues.push({ t: i, field: "trigger", message: `${name}有重复的触发来源` });
    seenTriggers.add(key);
    if (t.event) {
      if (t.event === "timer_elapsed") {
        const interval = Object.hasOwn(t, "interval_seconds"), daily = Object.hasOwn(t, "daily_time");
        if (interval === daily || (interval && (!Number.isInteger(t.interval_seconds) || t.interval_seconds < 15 || t.interval_seconds > 2147483647))
          || (daily && (typeof t.daily_time !== "string" || t.daily_time.length !== 5 || !/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(t.daily_time)))) {
          issues.push({ t: i, field: "trigger", message: `${name}的定时触发需要 15～2147483647 秒的整数间隔，或 HH:MM 格式的每日时刻` });
        }
      }
      if (!eventAllowed(q, t.event)) {
        issues.push({ t: i, field: "trigger", message: q.stage === "post_reply"
          ? `${name}在正文生成后提问，事件只能选「本轮正文生成完成」`
          : `${name}在正文生成前提问，不能订阅「本轮正文生成完成」` });
      }
      return;
    }
    if (!questions.includes(t.parent) || !slotsOf(t.parent).includes(t.option)) {
      issues.push({ t: i, field: "trigger", message: `${name}的上游选项已不存在，请重新选择` });
    } else if (t.parent.stage === "post_reply" && q.stage === "pre_reply") {
      issues.push({ t: i, field: "trigger", message: `${name}在正文生成前提问，不能由正文生成后的题触发` });
    } else if (t.ask_with_parent && t.parent.stage !== q.stage) {
      issues.push({ t: i, field: "timing", message: `${name}与上游不在同一阶段，只能命中后再问` });
    }
  });
  for (const ref of slots) {
    ref.actions.forEach((action, a) => {
      const issue = actionIssue(questions, catalog, q, ref, action);
      if (issue) issues.push({ o: slotIndex(q, ref), a, field: "action", message: `${name}的选项「${labelOf(ref)}」：${issue}` });
    });
  }
  for (const field of ["default_option", "private_default"]) {
    const ref = q[field];
    if (ref && (!slots.includes(ref) || isWildcard(ref))) {
      issues.push({ field, message: `${name}的${field === "default_option" ? "兜底选项" : "私聊默认选项"}无效` });
    }
  }
  const aliasSeen = new Set();
  q.aliases.forEach((alias, i) => {
    const key = alias.alias.trim();
    if (!key || aliasSeen.has(key) || !slots.includes(alias.option) || slots.some((ref) => keyOf(ref) === key)) {
      issues.push({ field: "alias", i, message: `${name}的别名「${key || i + 1}」无效` });
    }
    aliasSeen.add(key);
  });
  if (q.scenes.some((s) => !SCENE_KEYS.includes(s))) issues.push({ field: "scenes", message: `${name}的场景无效` });
  return issues;
}

export function validate(questions, catalog, labelOf) {
  const used = new Set();
  for (const [index, q] of questions.entries()) {
    const [issue] = questionIssues(q, questions, catalog, labelOf);
    if (issue) return { q: index, ...issue, message: issue.message.replace(/^还没有/, `第 ${index + 1} 道题还没有`) };
    const id = q.id.trim();
    if (used.has(id)) return { q: index, field: "id", message: `题目 ID「${id}」重复了` };
    used.add(id);
  }
  const cycle = findCycle(questions);
  if (cycle) {
    return { q: questions.indexOf(cycle[0]), field: "trigger",
      message: `触发来源形成了循环：${cycle.map((q) => q.id.trim() || "未命名").join(" → ")}` };
  }
  return null;
}

// ---------- 级联清理 ----------

export function structuralRemovals(questions, catalog) {
  const out = [];
  for (const q of questions) {
    const slots = slotsOf(q);
    for (const t of q.triggers) {
      const broken = t.event
        ? !eventAllowed(q, t.event)
        : !questions.includes(t.parent) || !slotsOf(t.parent).includes(t.option)
          || (t.parent.stage === "post_reply" && q.stage === "pre_reply");
      if (broken) out.push({ kind: "trigger", q, item: t });
    }
    for (const ref of slots) {
      for (const action of ref.actions) {
        if (structuralActionIssue(questions, catalog, q, ref, action)) out.push({ kind: "action", q, ref, item: action });
      }
    }
    for (const field of ["default_option", "private_default"]) {
      if (q[field] && (!slots.includes(q[field]) || isWildcard(q[field]))) out.push({ kind: "default", q, field });
    }
    for (const alias of q.aliases) if (!slots.includes(alias.option)) out.push({ kind: "alias", q, item: alias });
  }
  return out;
}

function applyRemovals(removals) {
  for (const r of removals) {
    if (r.kind === "trigger") r.q.triggers = r.q.triggers.filter((t) => t !== r.item);
    else if (r.kind === "action") r.ref.actions = r.ref.actions.filter((a) => a !== r.item);
    else if (r.kind === "default") r.q[r.field] = null;
    else if (r.kind === "alias") r.q.aliases = r.q.aliases.filter((a) => a !== r.item);
  }
}

// 跨阶段的上游只能命中后再问。这是调整，不算移除。
function fixTiming(questions) {
  for (const q of questions) {
    for (const t of q.triggers) if (q.candidate_source === "question" || (t.parent && t.parent.stage !== q.stage)) t.ask_with_parent = false;
  }
}

function measure(q) {
  return {
    triggers: q.triggers.length,
    actions: slotsOf(q).reduce((n, ref) => n + ref.actions.length, 0),
    defaults: (q.default_option ? 1 : 0) + (q.private_default ? 1 : 0) + q.aliases.length,
  };
}

// 对仍然保留的题统计修改会丢掉多少触发来源、动作和默认设置。被删除的题自身的内容不计入。
export function runChange(questions, catalog, apply, { commit = true } = {}) {
  const before = new Map(questions.map((q) => [q, measure(q)]));
  const undo = apply();
  const removals = structuralRemovals(questions, catalog);
  const lost = { triggers: 0, actions: 0, defaults: 0 };
  for (const q of questions) {
    const was = before.get(q);
    if (!was) continue;
    const now = measure(q);
    for (const key of Object.keys(lost)) lost[key] += Math.max(0, was[key] - now[key]);
  }
  for (const r of removals) lost[{ trigger: "triggers", action: "actions", default: "defaults", alias: "defaults" }[r.kind]] += 1;
  if (commit) {
    applyRemovals(removals);
    fixTiming(questions);
  } else {
    undo();
  }
  lost.total = lost.triggers + lost.actions + lost.defaults;
  return lost;
}

export function describeLoss(lost) {
  const parts = [];
  if (lost.triggers) parts.push(`${lost.triggers} 条触发来源`);
  if (lost.actions) parts.push(`${lost.actions} 个动作`);
  if (lost.defaults) parts.push(`${lost.defaults} 项默认设置`);
  return parts.join("、");
}

// 以下修改都返回撤销函数，供 runChange 先试算再决定是否提交。

export function deleteQuestion(questions, q) {
  return () => {
    const index = questions.indexOf(q);
    questions.splice(index, 1);
    return () => questions.splice(index, 0, q);
  };
}

export function deleteOption(q, ref) {
  return () => {
    const index = q.options.indexOf(ref);
    q.options.splice(index, 1);
    return () => q.options.splice(index, 0, ref);
  };
}

// 是否题转为选择题时原地改造两侧对象，触发来源、动作和默认值都跟着走。
function toChoice(q) {
  const sides = q.sides;
  const labels = { true: ["是", "判断为是"], false: ["否", "判断为否"] };
  for (const key of ["true", "false"]) {
    const ref = sides[key];
    delete ref.side;
    ref.id = labels[key][0];
    ref.criteria = labels[key][1];
  }
  q.type = "choice";
  q.options = [sides.true, sides.false];
  q.sides = null;
  return () => {
    for (const key of ["true", "false"]) {
      const ref = sides[key];
      delete ref.id;
      delete ref.criteria;
      ref.side = key;
    }
    q.type = "boolean";
    q.sides = sides;
    q.options = [];
  };
}

export function changeType(q, type) {
  return () => {
    if (type === q.type) return () => {};
    if (type === "choice") return toChoice(q);
    const options = q.options;
    q.type = "boolean";
    q.sides = freshSides();
    q.options = [];
    return () => {
      q.type = "choice";
      q.sides = null;
      q.options = options;
    };
  };
}

export function changeSource(q, source) {
  return () => {
    const saved = { source: q.candidate_source, requires: q.requires_candidates, options: q.options, from: q.candidate_from, fromId: q.candidate_from_id };
    let undoType = () => {};
    if (source !== "options") {
      if (q.type === "boolean") undoType = toChoice(q);
      if (!q.options.some(isWildcard)) q.options = [...q.options, newOption("*", "{描述}")];
      if (!q.requires_candidates || q.requires_candidates === saved.source) q.requires_candidates = source === "question" ? "" : source;
    } else {
      q.options = q.options.filter((o) => !isWildcard(o));
      if (q.requires_candidates === saved.source) q.requires_candidates = "";
    }
    q.candidate_source = source;
    if (source !== "question") { q.candidate_from = null; q.candidate_from_id = ""; }
    return () => {
      q.candidate_source = saved.source;
      q.candidate_from = saved.from;
      q.candidate_from_id = saved.fromId;
      q.requires_candidates = saved.requires;
      q.options = saved.options;
      undoType();
    };
  };
}

export function changeStage(q, stage) {
  return () => {
    const saved = { stage: q.stage, triggers: q.triggers };
    if (stage === "post_reply") {
      // 收到消息、冷场在正文生成后没有意义，统一换成正文生成完成。
      const kept = q.triggers.filter((t) => !t.event || !PRE_EVENTS.has(t.event));
      const hadEvent = kept.length !== q.triggers.length;
      q.triggers = hadEvent && !kept.some((t) => t.event === POST_EVENT) ? [{ event: POST_EVENT }, ...kept] : kept;
    }
    q.stage = stage;
    return () => {
      q.stage = saved.stage;
      q.triggers = saved.triggers;
    };
  };
}

// ---------- 流程 ----------

export function reachable(questions, roots) {
  const seen = new Set();
  const stack = [...roots];
  while (stack.length) {
    const q = stack.pop();
    if (seen.has(q) || !q.enabled) continue;
    seen.add(q);
    for (const { child } of childLinks(questions, q)) stack.push(child);
  }
  return seen;
}

export function replySources(questions, roots) {
  const out = [];
  const live = reachable(questions, roots);
  for (const q of questions) {
    if (!live.has(q)) continue;
    for (const ref of slotsOf(q)) {
      if (ref.actions.some((a) => a.tool === "request_reply")) out.push({ q, ref });
    }
  }
  return out;
}

export function allReplySources(questions) {
  return questions.flatMap((q) => slotsOf(q)
    .filter((ref) => ref.actions.some((a) => a.tool === "request_reply"))
    .map((ref) => ({ q, ref })));
}

export function roots(questions, event) {
  return questions.filter((q) => q.triggers.some((t) => t.event === event));
}

// 试算可设定答案的题：在该场景下可能被提问的启用题。
export function questionsForScene(questions, scene, event = "") {
  const live = event ? reachable(questions, [...roots(questions, event), ...roots(questions, POST_EVENT)]) : null;
  return questions.filter((q) => q.enabled && possibleScenes(q).has(scene) && (!live || live.has(q)));
}

// ---------- 模板 ----------

export function nextId(prefix, used) {
  let index = 1;
  while (used.has(`${prefix}${index}`)) index += 1;
  return `${prefix}${index}`;
}

const TEMPLATES = {
  boolean: { prefix: "question_", raw: { type: "boolean", instructions: "" } },
  choice: {
    prefix: "question_",
    raw: { type: "choice", instructions: "", options: [{ id: "option_1", criteria: "" }, { id: "option_2", criteria: "" }] },
  },
  message: {
    prefix: "pick_message_",
    raw: {
      type: "choice", candidate_source: "recent_messages", requires_candidates: "recent_messages",
      instructions: "最近哪条消息最需要回应？没有需要单独回应的消息时选「不针对」。\n本轮判断时间：{当前时间}",
      options: [{ id: "*", criteria: "{描述}" }, { id: "不针对", criteria: "没有需要单独回应的消息，自然接话即可" }],
      actions: { "*": [{ tool: "add_context" }, { tool: "quote_message" }] },
      default_option: "不针对",
    },
  },
  meme: {
    prefix: "pick_meme_",
    raw: {
      type: "choice", candidate_source: "memes",
      instructions: "从图库里选一张最适合这轮回复的表情；都不合适时选「不配图」。只根据描述选择。",
      options: [{ id: "*", criteria: "{描述}" }, { id: "不配图", criteria: "没有合适的表情" }],
      actions: { "*": [{ tool: "send_meme", when: "after_send" }] },
      default_option: "不配图",
    },
  },
  post: {
    prefix: "check_",
    raw: { type: "boolean", stage: "post_reply", instructions: "", triggers: [{ event: POST_EVENT }] },
  },
};

export const TEMPLATE_KINDS = Object.keys(TEMPLATES);

// 生成一道普通题。模板只预填内容，生成后与手工新建的题没有区别。
export function templateQuestion(kind, questions, { stage } = {}) {
  const template = TEMPLATES[kind] || TEMPLATES.boolean;
  const used = new Set(questions.map((q) => q.id.trim()));
  const raw = JSON.parse(JSON.stringify(template.raw));
  raw.id = nextId(template.prefix, used);
  if (stage) raw.stage = stage;
  const [q] = normalizeQuestions([raw]);
  return q;
}
