import test from "node:test";
import assert from "node:assert/strict";
import { sessionLabel, summarizeLogs } from "../views/meme_manager/log-summary.mjs";

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
