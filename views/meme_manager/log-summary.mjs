export function logHasIssue(item) {
  return !!(item.error || item.degraded || item.post_decision?.error
    || (item.tool_calls || []).some((call) => ["failed", "interrupted"].includes(call.status)));
}

export function sessionLabel(umo, name = "") {
  const parts = String(umo || "").split(":");
  const id = parts.slice(2).join(":") || umo || "未知";
  return name || `${parts[1] === "FriendMessage" ? "私聊" : "群聊"} ${id}`;
}

// 跳过原因；带上被跳过的定时题。同一会话连续同因跳过时，后端并成一条，这里再带上次数和第一次的时间。
export function skipText(item) {
  const reason = item?.raw?.skip_reason;
  if (!reason) return "";
  const notes = [];
  if (Array.isArray(item.timer_roots) && item.timer_roots.length) notes.push(`定时题 ${item.timer_roots.join("、")}`);
  const count = Number(item.skip_count) || 1;
  if (count >= 2) {
    const first = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}:\d{2}:\d{2})/.exec(String(item.first_time || ""));
    if (first) {
      const [, year, month, date, clock] = first;
      const sameDay = String(item.time || "").startsWith(`${year}-${month}-${date}T`);
      notes.push(`连续 ${count} 次，自 ${sameDay ? "" : `${Number(month)}月${Number(date)}日 `}${clock} 起`);
    } else {
      notes.push(`连续 ${count} 次`);
    }
  }
  return notes.length ? `${reason}（${notes.join("；")}）` : reason;
}

// 一条记录代表的回合数：连续跳过并成的一条按跳过次数算，发言率才不会因合并而虚高。
function rounds(item) {
  return item.raw?.skip_reason ? Math.max(1, Math.floor(Number(item.skip_count) || 1)) : 1;
}

export function summarizeLogs(logs) {
  const count = logs.length;
  const total = logs.reduce((sum, item) => sum + rounds(item), 0);
  const weigh = (keep) => logs.reduce((sum, item) => sum + (keep(item) ? rounds(item) : 0), 0);
  const measured = logs.filter((item) => !item.raw?.skip_reason && Number.isFinite(item.duration_ms));
  return {
    count,
    speakingRate: total ? weigh((item) => !logHasIssue(item) && ["reply", "join", "proactive"].includes(item.action)) / total : 0,
    failureRate: total ? weigh(logHasIssue) / total : 0,
    averageMs: measured.length ? Math.round(measured.reduce((sum, item) => sum + item.duration_ms, 0) / measured.length) : null,
  };
}
