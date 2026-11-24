import test from "node:test";
import assert from "node:assert/strict";
import { changedValues, describeChanges, fieldError, formatDuration } from "../views/meme_manager/settings-model.mjs";

const fields = [
  { key: "enabled", type: "bool", label: "启用快速决策" },
  { key: "choice_threshold", type: "float", label: "判断选项生效阈值", min: 0, max: 1 },
  { key: "group.silence_seconds", type: "int", label: "群聊冷场时长（秒）", min: 30, max: 21600 },
  { key: "group.target_quote_mode", type: "string", label: "指定消息的引用方式",
    options: [{ value: "first" }, { value: "all" }, { value: "none" }] },
  { key: "decision_provider_id", type: "string", label: "判断提供商 ID", max_length: 10 },
];
const byKey = Object.fromEntries(fields.map((field) => [field.key, field]));

test("按字段类型和取值范围校验设置", () => {
  assert.equal(fieldError(byKey.enabled, false), "");
  assert.equal(fieldError(byKey.enabled, "false"), "需要是开关值");
  assert.equal(fieldError(byKey.choice_threshold, 0.35), "");
  assert.equal(fieldError(byKey.choice_threshold, 1.2), "需在 0 到 1 之间");
  assert.equal(fieldError(byKey.choice_threshold, null), "请填写数字");
  assert.equal(fieldError(byKey["group.silence_seconds"], 45.5), "需要是整数");
  assert.equal(fieldError(byKey["group.silence_seconds"], 10), "需在 30 到 21600 之间");
  assert.equal(fieldError(byKey["group.target_quote_mode"], "all"), "");
  assert.equal(fieldError(byKey["group.target_quote_mode"], "both"), "不是可选的值");
  assert.equal(fieldError(byKey.decision_provider_id, "  abc  "), "");
  assert.equal(fieldError(byKey.decision_provider_id, "x".repeat(11)), "不能超过 10 个字符");
  assert.equal(fieldError({ type: "int", min: 0 }, -1), "不能小于 0");
});

test("只提交改动过的设置，文本首尾空白不算改动", () => {
  const saved = { enabled: true, choice_threshold: 0.5, "group.silence_seconds": 600,
    "group.target_quote_mode": "first", decision_provider_id: "judge" };
  assert.deepEqual(changedValues(fields, { ...saved, decision_provider_id: " judge " }, saved), {});
  const changes = changedValues(fields, { ...saved, enabled: false, "group.silence_seconds": 900 }, saved);
  assert.deepEqual(changes, { enabled: false, "group.silence_seconds": 900 });
  assert.equal(describeChanges(fields, changes), "未保存：启用快速决策、群聊冷场时长（秒）");
  const many = changedValues(fields, { enabled: false, choice_threshold: 0.6, "group.silence_seconds": 900,
    "group.target_quote_mode": "all", decision_provider_id: "" }, saved);
  assert.equal(describeChanges(fields, many), "未保存：启用快速决策、判断选项生效阈值、群聊冷场时长（秒） 等 5 项");
  assert.equal(describeChanges(fields, {}), "");
});

test("秒数换算成易读时长", () => {
  assert.equal(formatDuration(45), "");
  assert.equal(formatDuration(600), "10 分钟");
  assert.equal(formatDuration(1830), "30 分钟 30 秒");
  assert.equal(formatDuration(21600), "6 小时");
  assert.equal(formatDuration(3660), "1 小时 1 分钟");
  assert.equal(formatDuration(null), "");
});
