"""会话缓冲、冷却、主动发言配额和决策日志。"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .models import BufferedMessage, DecisionResult, SessionProfile

# 合并一轮决策内的多次日志变更后再写盘。
LOG_FLUSH_DELAY = 1.0


def _skip_key(row: dict) -> tuple | None:
    """跳过记录的合并依据；不是跳过记录时为 None。"""
    raw = row.get("raw")
    reason = raw.get("skip_reason") if isinstance(raw, dict) else None
    if not reason:
        return None
    return reason, row.get("scene"), row.get("trigger_event"), tuple(row.get("timer_roots") or ())


@dataclass
class SessionRuntime:
    profile: SessionProfile
    messages: deque[BufferedMessage] = field(default_factory=lambda: deque(maxlen=80))
    # 最近分配的消息序号，以及被动判断已经看过的最大序号；之间的用户消息尚待判断。
    seq: int = 0
    consumed_seq: int = 0
    # 仍在 on_chat_message 里（判断中或排队等待）的入站消息处理器数。
    handlers: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    deciding: bool = False
    last_user_ts: float = 0.0
    last_bot_ts: float = 0.0
    silence_evaluated_at_user_ts: float = 0.0
    timer_due: dict[tuple, float] = field(default_factory=dict)
    proactive_times: deque[float] = field(default_factory=deque)
    last_decision: DecisionResult | None = None
    pending_pipeline: DecisionResult | None = None
    # 占用会话的官方事件，以及开始占用、最后一次看到官方 Agent 仍在运行的时刻（monotonic）。
    pipeline_event: Any = None
    pipeline_started_at: float = 0.0
    pipeline_active_at: float = 0.0
    pipeline_released: asyncio.Event = field(default_factory=asyncio.Event)
    proactive_running: bool = False
    proactive_released: asyncio.Event = field(default_factory=asyncio.Event)
    display_name: str = ""
    name_checked_at: float = 0.0
    known: bool = True
    # 处理器仍在运行的入站事件，按消息序号；合并回合要从各条消息的事件上收附件，处理器结束时移除。
    events: dict[int, Any] = field(default_factory=dict)

    @property
    def last_activity(self) -> float:
        return max(self.last_user_ts, self.last_bot_ts)

    def unconsumed_user_messages(self, upto_seq: int | None = None) -> list[BufferedMessage]:
        """上一轮被动判断之后到达的用户消息，按到达顺序；给了 upto_seq 时只取到该序号为止。"""
        upto = self.seq if upto_seq is None else upto_seq
        return [m for m in self.messages if m.role == "user" and self.consumed_seq < m.seq <= upto]

    def has_user_messages_after(self, seq: int) -> bool:
        return any(m.role == "user" and m.seq > seq for m in self.messages)


class SessionStore:
    """内存会话状态。重启后聊天缓冲会丢失，表情库不受影响。

    决策日志首次使用时载入内存，此后读写都不碰文件；变更合并后在后台线程整份写回，
    文件仍是每行一条完整记录。
    """

    def __init__(
        self, data_dir: Path, log_limit: int = 200, *, flush_delay: float = LOG_FLUSH_DELAY
    ) -> None:
        self.data_dir = data_dir
        self.log_path = data_dir / "decision_logs.jsonl"
        self.log_limit = log_limit
        self._sessions: dict[str, SessionRuntime] = {}
        # 每项为（序列化后的行, 解析后的记录），按写入顺序排列。
        self._log_entries: list[tuple[str, dict]] | None = None
        self._log_dirty = False
        self._log_version = 0
        self._flush_delay = flush_delay
        self._flush_task: asyncio.Task[None] | None = None
        self._flush_now = asyncio.Event()
        self._write_lock = asyncio.Lock()
        # 写盘线程可能在协程被取消后仍在运行，用版本号保证旧快照不会覆盖新快照。
        self._file_lock = threading.Lock()
        self._written_version = 0

    def get(self, umo: str) -> SessionRuntime | None:
        return self._sessions.get(umo)

    def ensure(self, profile: SessionProfile) -> SessionRuntime:
        runtime = self._sessions.get(profile.umo)
        if runtime is None:
            runtime = SessionRuntime(profile=profile)
            self._sessions[profile.umo] = runtime
        else:
            runtime.profile = profile
        return runtime

    def all_sessions(self) -> list[SessionRuntime]:
        return list(self._sessions.values())

    def prune_idle(self, max_idle_seconds: int = 86400) -> int:
        """丢弃一天没有活动且没有进行中任务或定时订阅的会话。"""
        cutoff = time.time() - max(max_idle_seconds, 1)
        removed = 0
        for umo, runtime in list(self._sessions.items()):
            if (
                runtime.deciding
                or runtime.pending_pipeline is not None
                or runtime.handlers
                or runtime.timer_due
                or runtime.lock.locked()
            ):
                continue
            last_activity = runtime.last_activity
            if last_activity and last_activity < cutoff:
                self._sessions.pop(umo, None)
                removed += 1
        return removed

    def append_message(
        self, profile: SessionProfile, message: BufferedMessage, limit: int
    ) -> SessionRuntime:
        runtime = self.ensure(profile)
        if runtime.messages.maxlen != max(limit * 2, 20):
            runtime.messages = deque(runtime.messages, maxlen=max(limit * 2, 20))
        runtime.seq += 1
        message.seq = runtime.seq
        runtime.messages.append(message)
        received_at = time.time()
        timestamp = min(received_at, message.timestamp or received_at)
        if message.role == "user":
            # 迟到的消息仍进缓冲区；冷场计时只随更新的发言推进。
            runtime.last_user_ts = max(runtime.last_user_ts, timestamp)
        else:
            runtime.last_bot_ts = max(runtime.last_bot_ts, timestamp)
        return runtime

    def recent_messages(self, umo: str, limit: int) -> list[BufferedMessage]:
        runtime = self._sessions.get(umo)
        if not runtime:
            return []
        return list(runtime.messages)[-limit:]

    def in_cooldown(self, umo: str, cooldown_seconds: int) -> bool:
        if cooldown_seconds <= 0:
            return False
        runtime = self._sessions.get(umo)
        if not runtime or not runtime.last_bot_ts:
            return False
        return time.time() - runtime.last_bot_ts < cooldown_seconds

    def mark_bot_sent(self, umo: str) -> None:
        runtime = self._sessions.get(umo)
        if runtime:
            runtime.last_bot_ts = time.time()

    def can_proactive(self, umo: str, hourly_limit: int) -> bool:
        if hourly_limit <= 0:
            return False
        runtime = self._sessions.get(umo)
        if not runtime:
            return True
        cutoff = time.time() - 3600
        while runtime.proactive_times and runtime.proactive_times[0] < cutoff:
            runtime.proactive_times.popleft()
        return len(runtime.proactive_times) < hourly_limit

    def mark_proactive(self, umo: str) -> None:
        runtime = self._sessions.get(umo)
        if runtime:
            runtime.proactive_times.append(time.time())

    def mark_silence_evaluated(self, umo: str) -> None:
        runtime = self._sessions.get(umo)
        if runtime:
            runtime.silence_evaluated_at_user_ts = runtime.last_user_ts or time.time()

    def silence_already_evaluated(self, umo: str) -> bool:
        runtime = self._sessions.get(umo)
        if not runtime or not runtime.last_user_ts:
            return True
        return runtime.silence_evaluated_at_user_ts == runtime.last_user_ts

    # --- 决策日志 ---

    def _entries(self) -> list[tuple[str, dict]]:
        if self._log_entries is None:
            entries: list[tuple[str, dict]] = []
            try:
                text = self.log_path.read_text(encoding="utf-8", errors="replace") if self.log_path.exists() else ""
            except OSError as exc:
                logger.warning("无法读取决策日志：%s", exc)
                text = ""
            for line in text.splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    entries.append((line, row))
            self._log_entries = entries
            self._trim_logs()
        return self._log_entries

    def _trim_logs(self) -> None:
        overflow = len(self._log_entries) - max(self.log_limit, 1)
        if overflow > 0:
            del self._log_entries[:overflow]

    async def append_log(self, record: dict) -> None:
        # 先序列化再解析：不合法的记录在改动缓存前报错，缓存也不与调用方共用对象。
        line = json.dumps(record, ensure_ascii=False)
        self._entries().append((line, json.loads(line)))
        self._trim_logs()
        self._schedule_flush()

    async def append_skip_log(self, record: dict) -> None:
        """记录一次跳过判断。同一会话连续因同一原因跳过时并成一条，免得冷却期内每条消息各占一条日志、
        把真正的判断挤出保留条数。

        并入的记录移到末尾，保留最新一次的触发消息和上下文；skip_count 记连续次数，first_time 记第一次的时间。
        """
        key = _skip_key(record)
        entries = self._entries()
        for i in range(len(entries) - 1, -1, -1):
            row = entries[i][1]
            if row.get("umo") != record.get("umo"):
                continue
            if key is not None and _skip_key(row) == key:
                record = {**record, "skip_count": int(row.get("skip_count") or 1) + 1,
                          "first_time": row.get("first_time") or row.get("time")}
                del entries[i]
            break
        await self.append_log(record)

    async def update_tool_log(self, round_id: str, calls: list[dict]) -> None:
        await self.update_round_log(round_id, tool_calls=calls)

    async def update_round_log(self, round_id: str, **fields) -> None:
        entries = self._entries()
        for i in range(len(entries) - 1, -1, -1):
            if entries[i][1].get("round_id") == round_id:
                line = json.dumps({**entries[i][1], **fields}, ensure_ascii=False)
                entries[i] = (line, json.loads(line))
                self._schedule_flush()
                return

    def recent_logs(self, limit: int = 20, umo: str | None = None) -> list[dict]:
        """按新到旧返回日志。顶层字典是副本，嵌套内容与缓存共用，只读。"""
        items: list[dict] = []
        for _, row in reversed(self._entries()):
            if umo and row.get("umo") != umo:
                continue
            items.append(dict(row))
            if len(items) >= limit:
                break
        return items

    def find_log(self, round_id: str) -> dict | None:
        """返回指定回合的日志，规则同 recent_logs。"""
        if not round_id:
            return None
        return next((dict(row) for _, row in reversed(self._entries())
                     if row.get("round_id") == round_id), None)

    def _schedule_flush(self) -> None:
        self._log_dirty = True
        self._log_version += 1
        if self._flush_task is None or self._flush_task.done():
            self._flush_now.clear()
            self._flush_task = asyncio.get_running_loop().create_task(self._flush_later())

    async def _flush_later(self) -> None:
        try:
            await asyncio.wait_for(self._flush_now.wait(), self._flush_delay)
        except TimeoutError:
            pass
        await self._write_pending()

    async def _write_pending(self) -> None:
        async with self._write_lock:
            while self._log_dirty:
                self._log_dirty = False
                version = self._log_version
                text = "".join(line + "\n" for line, _ in self._entries())
                try:
                    await asyncio.to_thread(self._write_log_file, version, text)
                except Exception as exc:  # noqa: BLE001
                    # 保留待写标记：没有新变更时，卸载前的 flush_logs 也要再写一次。
                    self._log_dirty = True
                    logger.warning("决策日志写入失败，下次变更或卸载时重试：%s", exc)
                    return

    def _write_log_file(self, version: int, text: str) -> None:
        with self._file_lock:
            if version <= self._written_version:
                return
            tmp = self.log_path.with_suffix(".jsonl.tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self.log_path)
            self._written_version = version

    async def flush_logs(self) -> None:
        """立即写入尚未落盘的日志，插件卸载时调用。"""
        task = self._flush_task
        if task is not None and not task.done():
            self._flush_now.set()
            # wait 不会因本协程被取消而取消写盘任务，也不抛出任务内的异常。
            await asyncio.wait({task})
        await self._write_pending()
