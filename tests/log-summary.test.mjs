import test from "node:test";
import assert from "node:assert/strict";
import { sessionLabel, skipText, summarizeLogs } from "../views/meme_manager/log-summary.mjs";

test("连续跳过并成一条时显示次数和第一次的时间，跨天带上日期", () => {
  assert.equal(skipText({ action: "none" }), "");
  assert.equal(skipText({ raw: { skip_reason: "冷却中跳过" } }), "冷却中跳过");
  const time = "2026-11-26T10:05:00+0800";
  assert.equal(skipText({ raw: { skip_reason: "冷却中跳过" }, skip_count: 4, time, first_time: "2026-11-26T10:01:30+0800" }),
    "冷却中跳过（连续 4 次，自 10:01:30 起）");
  assert.equal(skipText({ raw: { skip_reason: "冷却中跳过" }, skip_count: 2, time, first_time: "2026-11-25T23:59:10+0800" }),
    "冷却中跳过（连续 2 次，自 11月25日 23:59:10 起）");
  assert.equal(skipText({ raw: { skip_reason: "冷却中跳过" }, skip_count: 3 }), "冷却中跳过（连续 3 次）");
});

test("统计区分失败与沉默，跳过的回合不稀释平均判断耗时", () => {
  const stats = summarizeLogs([
    { action: "reply", duration_ms: 100 },
    { action: "none", duration_ms: 200, error: "provider failure" },
    { action: "none", duration_ms: 0, raw: { skip_reason: "冷却" } },
    { action: "none", duration_ms: 300 },
  ]);
  assert.deepEqual(stats, { count: 4, speakingRate: 0.25, failureRate: 0.25, averageMs: 200 });
  assert.equal(summarizeLogs([]).averageMs, null);
});

test("会话优先显示群名，缺失群名时回退为易读会话编号", () => {
  assert.equal(sessionLabel("qq:GroupMessage:123", "测试群"), "测试群");
  assert.equal(sessionLabel("qq:GroupMessage:123"), "群聊 123");
  assert.equal(sessionLabel("qq:FriendMessage:456"), "私聊 456");
});
