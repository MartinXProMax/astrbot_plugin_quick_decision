// 「插件设置」页的纯逻辑：校验、改动对比和时长显示。字段描述由后端按 _conf_schema.json 生成。

function isNumberField(field) {
  return field.type === "int" || field.type === "float";
}

// 与后端 validate_setting 的规则一致；返回空字符串表示有效。
export function fieldError(field, value) {
  if (field.type === "bool") return typeof value === "boolean" ? "" : "需要是开关值";
  if (isNumberField(field)) {
    if (typeof value !== "number" || !Number.isFinite(value)) return "请填写数字";
    if (field.type === "int" && !Number.isInteger(value)) return "需要是整数";
    const low = field.min ?? -Infinity;
    const high = field.max ?? Infinity;
    if (value < low || value > high) {
      if (field.min == null) return `不能大于 ${field.max}`;
      if (field.max == null) return `不能小于 ${field.min}`;
      return `需在 ${field.min} 到 ${field.max} 之间`;
    }
    return "";
  }
  if (typeof value !== "string") return "需要是文本";
  const text = value.trim();
  if (field.options && !field.options.some((option) => option.value === text)) return "不是可选的值";
  if (field.max_length && text.length > field.max_length) return `不能超过 ${field.max_length} 个字符`;
  return "";
}

function normalize(field, value) {
  return field.type === "string" && typeof value === "string" ? value.trim() : value;
}

// 只提交改过的项，避免覆盖在官方配置页里同时改动的其他设置。
export function changedValues(fields, values, saved) {
  const changes = {};
  for (const field of fields) {
    const value = normalize(field, values[field.key]);
    if (value !== saved[field.key]) changes[field.key] = value;
  }
  return changes;
}

export function describeChanges(fields, changes) {
  const labels = fields.filter((field) => Object.hasOwn(changes, field.key)).map((field) => field.label);
  if (!labels.length) return "";
  const shown = labels.slice(0, 3).join("、");
  return `未保存：${labels.length > 3 ? `${shown} 等 ${labels.length} 项` : shown}`;
}

// 秒数换算成易读时长，不足一分钟时不额外显示。
export function formatDuration(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 60) return "";
  const total = Math.round(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const rest = total % 60;
  return [hours && `${hours} 小时`, minutes && `${minutes} 分钟`, rest && `${rest} 秒`].filter(Boolean).join(" ");
}
