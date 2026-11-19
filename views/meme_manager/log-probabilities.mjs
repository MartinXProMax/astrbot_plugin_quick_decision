// 历史日志未启用权重时省略 option_weights，等同于所有选项权重为 1。
export function probabilityRows(answer, isRandom = false) {
  const valid = (p) => Number.isFinite(p) && p >= 0 && p <= 1;
  if (!answer || typeof answer !== "object") return [];
  let entries;
  if (answer.probabilities && typeof answer.probabilities === "object" && !Array.isArray(answer.probabilities)) {
    entries = Object.entries(answer.probabilities);
  } else if (!("choice" in answer) && valid(answer.probability)) {
    entries = [["true", answer.probability], ["false", 1 - answer.probability]];
  } else {
    return [];
  }
  if (!entries.length || entries.some(([, p]) => !valid(p))) return [];
  const weights = answer.option_weights ?? {};
  const rows = entries.map(([key, original]) => ({
    key, original, weight: Object.hasOwn(weights, key) ? weights[key] : 1,
  }));
  if (isRandom) {
    return rows.map((row) => {
      const chance = answer.selection_probabilities?.[row.key];
      return { ...row, weighted: valid(chance) ? chance : null };
    });
  }
  const validWeights = rows.every(({ weight }) => Number.isFinite(weight) && weight >= 0 && weight <= 100);
  const total = validWeights ? rows.reduce((sum, { original, weight }) => sum + original * weight, 0) : 0;
  return rows.map((row) => ({
    ...row,
    weighted: total > 0 ? row.original * row.weight / total : null,
  }));
}
