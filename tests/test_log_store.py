"""决策日志存储：内存读取、合并落盘与后台写入。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from test_v11_config import _install_astrbot_stubs  # isort: skip

_install_astrbot_stubs()

from test_official_inbound_handoff import _make_plugin  # noqa: E402

from astrbot_plugin_quick_decision.session_store import SessionStore  # noqa: E402


class LogStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def reopen(self, limit=20) -> SessionStore:
        return SessionStore(self.dir, log_limit=limit)

    async def test_rows_and_updates_survive_reopen(self) -> None:
        store = self.reopen()
        await store.append_log({"round_id": "a", "action": "reply"})
        await store.append_log({"round_id": "b", "action": "none"})
        await store.update_round_log("a", reply_sent=True, reply_text="你好")
        await store.flush_logs()

        rows = self.reopen().recent_logs()
        self.assertEqual([row["round_id"] for row in rows], ["b", "a"])
        self.assertEqual(rows[1]["reply_text"], "你好")
        self.assertTrue(rows[1]["reply_sent"])
        # 文件仍是每行一条完整记录，旧版本可以直接读取。
        lines = (self.dir / "decision_logs.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(line)["round_id"] for line in lines], ["a", "b"])

    async def test_a_round_of_writes_is_saved_in_one_background_write(self) -> None:
        store = self.reopen()
        threads = []
        original = store._write_log_file

        def record(*args):
            threads.append(threading.current_thread())
            return original(*args)

        with patch.object(store, "_write_log_file", side_effect=record):
            await store.append_log({"round_id": "a"})
            await store.update_round_log("a", reply_sent=True)
            await store.update_tool_log("a", [{"status": "sent"}])
            await store.update_round_log("a", post_decision={"answers": {}})
            await store.flush_logs()

        self.assertEqual(len(threads), 1)
        self.assertIsNot(threads[0], threading.main_thread())
        row, = self.reopen().recent_logs()
        self.assertEqual(row["tool_calls"], [{"status": "sent"}])
        self.assertEqual(row["post_decision"], {"answers": {}})

    async def test_writes_are_saved_without_an_explicit_flush(self) -> None:
        store = SessionStore(self.dir, log_limit=20, flush_delay=0.01)
        await store.append_log({"round_id": "a"})
        for _ in range(50):
            await asyncio.sleep(0.01)
            if (self.dir / "decision_logs.jsonl").exists():
                break
        self.assertEqual([row["round_id"] for row in self.reopen().recent_logs()], ["a"])

    async def test_reads_come_from_memory_after_first_load(self) -> None:
        store = self.reopen()
        await store.append_log({"round_id": "a"})
        await store.flush_logs()
        reader = self.reopen()
        self.assertEqual(reader.recent_logs()[0]["round_id"], "a")
        with patch.object(Path, "read_text", side_effect=AssertionError("不应再读文件")):
            for _ in range(3):
                self.assertEqual(reader.recent_logs(limit=1)[0]["round_id"], "a")
                self.assertEqual(reader.find_log("a")["round_id"], "a")
            await reader.append_log({"round_id": "b"})
            await reader.update_round_log("a", reply_sent=True)
            self.assertTrue(reader.find_log("a")["reply_sent"])

    async def test_existing_file_is_loaded_trimmed_and_cleaned(self) -> None:
        rows = [json.dumps({"round_id": str(i)}) for i in range(5)]
        (self.dir / "decision_logs.jsonl").write_text(
            "\n".join(rows[:2] + ["不是 JSON", "[1, 2]", ""] + rows[2:]) + "\n", encoding="utf-8")
        store = self.reopen(limit=3)
        self.assertEqual([row["round_id"] for row in store.recent_logs()], ["4", "3", "2"])
        self.assertEqual([row["round_id"] for row in store.recent_logs(umo=None, limit=2)], ["4", "3"])
        await store.append_log({"round_id": "5"})
        await store.flush_logs()
        lines = (self.dir / "decision_logs.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(line)["round_id"] for line in lines], ["3", "4", "5"])

    async def test_stored_rows_are_isolated_from_callers(self) -> None:
        store = self.reopen()
        record = {"round_id": "a", "batches": [{"asked": ["speak"]}], "session_name": ""}
        await store.append_log(record)
        # 记录在写入时定格，调用方之后修改原对象不影响日志。
        record["batches"].append({"asked": ["later"]})
        # 读取方修改顶层字段（如日志页补充会话名）也不会写回缓存。
        store.recent_logs()[0]["session_name"] = "临时名称"
        store.find_log("a")["action"] = "reply"
        row = store.find_log("a")
        self.assertEqual(row["batches"], [{"asked": ["speak"]}])
        self.assertEqual(row["session_name"], "")
        self.assertNotIn("action", row)

    async def test_non_json_record_is_rejected_without_corrupting_the_log(self) -> None:
        store = self.reopen()
        await store.append_log({"round_id": "a"})
        with self.assertRaises(TypeError):
            await store.append_log({"round_id": "b", "bad": object()})
        with self.assertRaises(TypeError):
            await store.update_round_log("a", bad=object())
        await store.flush_logs()
        rows = self.reopen().recent_logs()
        self.assertEqual(rows, [{"round_id": "a"}])

    async def test_write_failure_is_logged_and_retried_on_next_change(self) -> None:
        store = self.reopen()
        calls = []
        original = store._write_log_file

        def flaky(*args):
            calls.append(args)
            if len(calls) == 1:
                raise OSError("disk full")
            return original(*args)

        with patch.object(store, "_write_log_file", side_effect=flaky):
            await store.append_log({"round_id": "a"})
            await store.flush_logs()
            self.assertFalse((self.dir / "decision_logs.jsonl").exists())
            await store.append_log({"round_id": "b"})
            await store.flush_logs()
        self.assertEqual([row["round_id"] for row in self.reopen().recent_logs()], ["b", "a"])

    async def test_plugin_unload_saves_pending_logs(self) -> None:
        plugin, _ = _make_plugin(self.tmp.name)
        await plugin.sessions.append_log({"round_id": "unsaved"})
        await plugin.terminate()
        self.assertEqual(self.reopen().recent_logs()[0]["round_id"], "unsaved")


if __name__ == "__main__":
    unittest.main()
