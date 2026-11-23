export function logHasIssue(item) {
  return !!(item.error || item.degraded || item.post_decision?.error
    || (item.tool_calls || []).some((call) => ["failed", "interrupted"].includes(call.status)));
}

export function sessionLabel(umo, name = "") {
  const parts = String(umo || "").split(":");
  const id = parts.slice(2).join(":") || umo || "未知";
  return name || `${parts[1] === "FriendMessage" ? "私聊" : "群聊"} ${id}`;
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
