import assert from "node:assert/strict";
import test from "node:test";
import { probabilityRows } from "../views/meme_manager/log-probabilities.mjs";

test("只归一化日志中的权重，温度为零的抽样结果独立于加权条", () => {
  const rows = probabilityRows({
    probabilities: { short: 0.7, long: 0.3 }, option_weights: { long: 3 },
    selection_temperature: 0, selection_probabilities: { short: 0, long: 1 },
  });
  assert.deepEqual(rows.map(r => r.original), [0.7, 0.3]);
  assert.ok(Math.abs(rows[1].weighted - 0.5625) < 1e-12);
  assert.ok(Math.abs(rows[0].weighted - 0.4375) < 1e-12);
});

test("旧是否题可恢复两侧概率，零权重选项仍可对照", () => {
  const old = probabilityRows({ probability: 0.25 });
  assert.deepEqual(old.map(r => [r.key, r.original, r.weighted]), [["true", 0.25, 0.25], ["false", 0.75, 0.75]]);
  const weighted = probabilityRows({ probability: 0.25, option_weights: { false: 0 } });
  assert.deepEqual(weighted.map(r => r.weighted), [1, 0]);
});

test("纯随机显示等权基准和实际抽中概率，包括零权重选项", () => {
  const rows = probabilityRows({
    question_type: "boolean",
    probabilities: { true: 0.5, false: 0.5 },
    option_weights: { true: 0, false: 1 },
    selection_probabilities: { true: 0, false: 1 },
  }, true);
  assert.deepEqual(rows.map(r => [r.key, r.original, r.weighted]), [
    ["true", 0.5, 0], ["false", 0.5, 1],
  ]);
});

test("没有分布时不生成虚构的百分比", () => {
  for (const raw of [{}, { choice: "reply", note: "private_passive_default_reply" }, { probability: null }]) {
    assert.deepEqual(probabilityRows(raw), []);
  }
  const rows = probabilityRows({ probabilities: { a: 1, b: 0 }, option_weights: { a: 0 } });
  assert.deepEqual(rows.map(r => r.weighted), [null, null]);
});
