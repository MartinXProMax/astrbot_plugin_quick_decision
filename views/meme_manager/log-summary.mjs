export function logHasIssue(item) {
  return !!(item.error || item.degraded || item.post_decision?.error
    || (item.tool_calls || []).some((call) => ["failed", "interrupted"].includes(call.status)));
}

export function sessionLabel(umo, name = "") {
  const parts = String(umo || "").split(":");
  const id = parts.slice(2).join(":") || umo || "未知";
  return name || `${parts[1] === "FriendMessage" ? "私聊" : "群聊"} ${id}`;
}

// 跳过原因；同一会话连续同因跳过时，后端并成一条，这里带上次数和第一次的时间。
export function skipText(item) {
  const reason = item?.raw?.skip_reason;
  if (!reason) return "";
  const count = Number(item.skip_count) || 1;
  if (count < 2) return reason;
  const first = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}:\d{2}:\d{2})/.exec(String(item.first_time || ""));
  if (!first) return `${reason}（连续 ${count} 次）`;
  const [, year, month, date, clock] = first;
  const sameDay = String(item.time || "").startsWith(`${year}-${month}-${date}T`);
  const day = sameDay ? "" : `${Number(month)}月${Number(date)}日 `;
  return `${reason}（连续 ${count} 次，自 ${day}${clock} 起）`;
}

export function summarizeLogs(logs) {
  const count = logs.length;
  const measured = logs.filter((item) => !item.raw?.skip_reason && Number.isFinite(item.duration_ms));
  return {
    count,
    speakingRate: count ? logs.filter((item) => !logHasIssue(item) && ["reply", "join", "proactive"].includes(item.action)).length / count : 0,
    failureRate: count ? logs.filter(logHasIssue).length / count : 0,
    averageMs: measured.length ? Math.round(measured.reduce((sum, item) => sum + item.duration_ms, 0) / measured.length) : null,
  };
}
