"""工具历史配对、冷场写回和离线脚本。不访问网络，不连接 QQ，不调用模型。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import test_v11_config  # noqa: E402,F401  pylint: disable=unused-import

from astrbot_plugin_quick_decision.models import DecisionResult
from astrbot_plugin_quick_decision.reply_generator import generate_text
from astrbot_plugin_quick_decision.tool_history import (
    pending_model_violations,
    repair_tool_history,
    sanitize_provider_request,
)


def _call(cid: str, name: str = "astrbot_file_read_tool") -> dict:
    return {
        "id": cid,
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


def _tool(cid: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": cid, "content": content}


def _group_prefix() -> list[dict]:
    return [
        {"role": "user", "content": [{"type": "text", "text": "u0"}, {"type": "text", "text": "u1"}]},
        {
            "role": "assistant",
            "content": [{"type": "think", "think": "", "encrypted": "opaque-reasoning"}],
            "tool_calls": [_call("call-g")],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "plain answer"}]},
    ]


class RepairTests(unittest.TestCase):
    def test_minimal_repro_strips_orphan_and_keeps_both_assistant_texts(self) -> None:
        history = _group_prefix()
        before = pending_model_violations(history)
        self.assertEqual(before[0]["input_index"], 2)
        self.assertEqual(before[0]["pending"], 1)

        repaired, stats = repair_tool_history(history)
        self.assertEqual(stats.removed_calls, 1)
        self.assertEqual(stats.dropped_empty_assistants, 0)
        self.assertEqual(repaired[1]["content"][0]["encrypted"], "opaque-reasoning")
        self.assertNotIn("tool_calls", repaired[1])
        self.assertEqual(repaired[2]["content"][0]["text"], "plain answer")
        self.assertEqual(pending_model_violations(repaired), [])
        self.assertIn("tool_calls", history[1])

    def test_legal_multi_tool_keeps_order_ids_and_failed_output(self) -> None:
        history = [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "working",
                "tool_calls": [_call("a", "astrbot_grep_tool"), _call("b", "astrbot_execute_shell")],
            },
            _tool("b", "error: exit 1"),
            _tool("a", "match"),
            {"role": "assistant", "content": "done"},
        ]
        repaired, stats = repair_tool_history(history)
        self.assertFalse(stats.changed)
        self.assertEqual(stats.kept_exchanges, 1)
        self.assertIs(repaired[2], history[2])
        self.assertEqual(repaired[2]["content"], "error: exit 1")
        self.assertEqual(repaired[3]["tool_call_id"], "a")
        self.assertEqual(pending_model_violations(repaired), [])

    def test_partial_results_drop_only_the_unmatched_call(self) -> None:
        history = [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "keep"}, {"type": "think", "think": "reason"}],
                "tool_calls": [_call("kept"), _call("missing")],
            },
            _tool("kept", "body"),
            {"role": "user", "content": "next"},
        ]
        repaired, stats = repair_tool_history(history)
        self.assertEqual(stats.removed_calls, 1)
        self.assertEqual(stats.dropped_results, 0)
        self.assertEqual(
            [call["id"] for call in repaired[0]["tool_calls"]],
            ["kept"],
        )
        self.assertEqual(repaired[0]["content"][0]["text"], "keep")
        self.assertEqual(repaired[0]["content"][1]["think"], "reason")
        self.assertEqual(repaired[1]["content"], "body")
        self.assertEqual(pending_model_violations(repaired), [])

    def test_duplicate_ids_are_not_guessed(self) -> None:
        history = [
            {
                "role": "assistant",
                "content": "visible",
                "tool_calls": [_call("dup"), _call("dup")],
            },
            _tool("dup", "one"),
            _tool("dup", "two"),
        ]
        repaired, stats = repair_tool_history(history)
        self.assertEqual(stats.removed_calls, 2)
        self.assertEqual(stats.dropped_results, 2)
        self.assertEqual(repaired, [{"role": "assistant", "content": "visible"}])
        self.assertEqual(pending_model_violations(repaired), [])

    def test_result_after_user_is_not_reattached(self) -> None:
        history = [
            {"role": "assistant", "content": None, "tool_calls": [_call("late")]},
            {"role": "user", "content": "human"},
            _tool("late", "too late"),
            {"role": "assistant", "content": "still here"},
        ]
        repaired, stats = repair_tool_history(history)
        self.assertEqual(stats.removed_calls, 1)
        self.assertEqual(stats.dropped_empty_assistants, 1)
        self.assertEqual(stats.dropped_results, 1)
        self.assertEqual(
            repaired,
            [
                {"role": "user", "content": "human"},
                {"role": "assistant", "content": "still here"},
            ],
        )

    def test_empty_orphan_is_dropped_but_encrypted_think_is_kept(self) -> None:
        encrypted = json.dumps(
            {"type": "openai_responses_reasoning", "items": [{"type": "reasoning"}]}
        )
        history = [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": None, "tool_calls": [_call("none")]},
            {
                "role": "assistant",
                "content": [{"type": "think", "think": "", "encrypted": encrypted}],
                "tool_calls": [_call("think-only")],
            },
            {"role": "assistant", "content": "after"},
        ]
        repaired, stats = repair_tool_history(history)
        self.assertEqual(stats.dropped_empty_assistants, 1)
        self.assertEqual(stats.removed_calls, 2)
        self.assertEqual(repaired[1]["content"][0]["encrypted"], encrypted)
        self.assertNotIn("tool_calls", repaired[1])
        self.assertEqual(repaired[2]["content"], "after")
        self.assertEqual(pending_model_violations(repaired), [])

    def test_open_tail_is_preserved_only_when_requested(self) -> None:
        history = [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "doing", "tool_calls": [_call("wait")]},
        ]
        kept, kept_stats = repair_tool_history(history, preserve_open_tail=True)
        self.assertFalse(kept_stats.changed)
        self.assertEqual(kept[1]["tool_calls"][0]["id"], "wait")
        sent, sent_stats = repair_tool_history(history, preserve_open_tail=False)
        self.assertEqual(sent_stats.removed_calls, 1)
        self.assertNotIn("tool_calls", sent[1])
        self.assertEqual(sent[1]["content"], "doing")

    def test_checkpoint_between_a_legal_pair_stays(self) -> None:
        history = [
            {"role": "assistant", "tool_calls": [_call("c")], "content": "x"},
            {"role": "_checkpoint", "content": {"id": "cp"}},
            _tool("c", "ok"),
        ]
        repaired, stats = repair_tool_history(history)
        self.assertFalse(stats.changed)
        self.assertEqual(repaired[1]["role"], "_checkpoint")

    def test_tail_orphan_inside_a_long_history_is_repaired_without_dropping_text(self) -> None:
        history = _group_prefix()
        history.extend(
            [
                {"role": "user", "content": "later user"},
                {"role": "assistant", "content": "later answer"},
            ]
            * 6
        )
        history.append(
            {
                "role": "assistant",
                "content": "tail text",
                "tool_calls": [_call("tail")],
            }
        )
        history.append({"role": "user", "content": "after tail"})
        repaired, stats = repair_tool_history(history)
        self.assertEqual(stats.removed_calls, 2)
        texts = [
            item.get("content")
            for item in repaired
            if isinstance(item.get("content"), str)
        ]
        self.assertIn("later user", texts)
        self.assertIn("tail text", texts)
        self.assertIn("after tail", texts)
        self.assertEqual(pending_model_violations(repaired), [])

    def test_sanitize_request_does_not_require_a_decision(self) -> None:
        req = SimpleNamespace(contexts=_group_prefix())
        stats = sanitize_provider_request(req)
        self.assertEqual(stats.removed_calls, 1)
        self.assertEqual(pending_model_violations(req.contexts), [])
        untouched = SimpleNamespace(contexts=[{"role": "user", "content": "hi"}])
        original = untouched.contexts
        sanitize_provider_request(untouched)
        self.assertIs(untouched.contexts, original)


class ColdStartWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_generate_text_keeps_tool_chain_and_newer_history(self) -> None:
        legal = [
            {"role": "user", "content": "old"},
            {
                "role": "assistant",
                "content": [{"type": "think", "think": "", "encrypted": "opaque"}],
                "tool_calls": [_call("orphan")],
            },
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "run", "tool_calls": [_call("ok")]},
            _tool("ok", "error: tool failed"),
            {"role": "assistant", "content": "finished"},
        ]
        state = {"history": json.dumps(legal), "seen_request": None}

        class Manager:
            async def get_curr_conversation_id(self, umo):  # noqa: ARG002
                return "cid"

            async def get_conversation(self, umo, cid):  # noqa: ARG002
                return SimpleNamespace(history=state["history"], cid=cid, persona_id=None)

            async def update_conversation(self, umo, cid, history=None, token_usage=None):  # noqa: ARG002
                state["history"] = json.dumps(history)
                state["written"] = history

        async def llm_generate(**kwargs):
            state["seen_request"] = kwargs["contexts"]
            newer = json.loads(state["history"])
            newer.extend(
                [
                    {"role": "user", "content": "arrived while generating"},
                    {"role": "assistant", "content": "official reply"},
                ]
            )
            state["history"] = json.dumps(newer)
            return SimpleNamespace(completion_text="cold hello")

        context = SimpleNamespace(
            get_current_chat_provider_id=_async_return("provider"),
            conversation_manager=Manager(),
            persona_manager=SimpleNamespace(
                get_persona=_async_return(SimpleNamespace(system_prompt="")),
                get_default_persona_v3=_async_return({"prompt": ""}),
            ),
            llm_generate=llm_generate,
        )
        import astrbot_plugin_quick_decision.reply_generator as reply_generator

        @asynccontextmanager
        async def no_lock(_umo):
            yield

        original_lock = reply_generator.acquire_official_session_lock
        reply_generator.acquire_official_session_lock = no_lock
        try:
            text = await generate_text(
                context,
                umo="test:group:1",
                decision=DecisionResult(action="proactive", scene="group_proactive"),
                user_prompt="ignored for proactive",
            )
        finally:
            reply_generator.acquire_official_session_lock = original_lock

        self.assertEqual(text, "cold hello")
        sent_roles = [item["role"] for item in state["seen_request"]]
        self.assertIn("tool", sent_roles)
        self.assertFalse(any(item.get("tool_calls") and item["tool_calls"][0]["id"] == "orphan" for item in state["seen_request"]))
        self.assertTrue(any(item.get("tool_calls") and item["tool_calls"][0]["id"] == "ok" for item in state["seen_request"]))
        written = state["written"]
        self.assertEqual(written[-1], {"role": "assistant", "content": "cold hello"})
        self.assertIn({"role": "user", "content": "arrived while generating"}, written)
        self.assertTrue(any(item.get("role") == "tool" and item.get("content") == "error: tool failed" for item in written))
        self.assertTrue(any(item.get("tool_calls") and item["tool_calls"][0]["id"] == "orphan" for item in written))

    async def test_lock_reread_sees_history_written_by_the_official_holder(self) -> None:
        state = {
            "history": json.dumps([{"role": "user", "content": "base"}]),
            "held": False,
            "reads": 0,
        }

        class Manager:
            async def get_curr_conversation_id(self, umo):  # noqa: ARG002
                return "cid"

            async def get_conversation(self, umo, cid):  # noqa: ARG002
                state["reads"] += 1
                if state["reads"] > 1 and not state["held"]:
                    raise AssertionError("写回前的重读发生在锁外")
                return SimpleNamespace(history=state["history"], cid=cid, persona_id=None)

            async def update_conversation(self, umo, cid, history=None, token_usage=None):  # noqa: ARG002
                if not state["held"]:
                    raise AssertionError("写回发生在锁外")
                state["written"] = history

        async def llm_generate(**kwargs):  # noqa: ARG001
            return SimpleNamespace(completion_text="after lock")

        context = SimpleNamespace(
            get_current_chat_provider_id=_async_return("provider"),
            conversation_manager=Manager(),
            persona_manager=SimpleNamespace(
                get_default_persona_v3=_async_return(""),
            ),
            llm_generate=llm_generate,
        )
        import astrbot_plugin_quick_decision.reply_generator as reply_generator

        @asynccontextmanager
        async def locked(_umo):
            state["history"] = json.dumps(
                [
                    {"role": "user", "content": "base"},
                    {"role": "assistant", "content": "saved by official", "tool_calls": [_call("live")]},
                    _tool("live", "done"),
                ]
            )
            state["held"] = True
            try:
                yield
            finally:
                state["held"] = False

        original_lock = reply_generator.acquire_official_session_lock
        reply_generator.acquire_official_session_lock = locked
        try:
            await generate_text(
                context,
                umo="test:group:2",
                decision=DecisionResult(action="proactive", scene="group_proactive"),
                user_prompt="",
            )
        finally:
            reply_generator.acquire_official_session_lock = original_lock

        written = state["written"]
        self.assertEqual(written[-1]["content"], "after lock")
        self.assertEqual(written[-3]["tool_calls"][0]["id"], "live")
        self.assertEqual(written[-2]["content"], "done")


def _async_return(value):
    async def _inner(*_args, **_kwargs):
        return value

    return _inner


class RepairScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "data_v4.db"
        self.script = Path(__file__).resolve().parents[1] / "tools" / "repair_orphan_tool_history.py"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _init_db(self) -> str:
        raw = json.dumps(_group_prefix(), ensure_ascii=False)
        other = json.dumps([{"role": "user", "content": "private stays"}], ensure_ascii=False)
        conn = sqlite3.connect(self.db)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE conversations (
                inner_conversation_id INTEGER PRIMARY KEY,
                conversation_id TEXT UNIQUE,
                platform_id TEXT,
                user_id TEXT,
                content TEXT,
                title TEXT,
                persona_id TEXT,
                token_usage INTEGER DEFAULT 0,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE preferences (
                id INTEGER PRIMARY KEY,
                scope TEXT,
                scope_id TEXT,
                key TEXT,
                value TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO conversations VALUES (1, 'group-cid', 'qq', 'u', ?, 'title', 'persona', 7, 't', 't')",
            (raw,),
        )
        conn.execute(
            "INSERT INTO conversations VALUES (2, 'private-cid', 'qq', 'u', ?, 'keep', 'persona', 3, 't', 't')",
            (other,),
        )
        conn.execute(
            "INSERT INTO preferences VALUES (1, 'umo', 'test:group:97', 'sel_conv_id', ?, 't', 't')",
            (json.dumps({"val": "group-cid"}),),
        )
        conn.execute(
            "INSERT INTO preferences VALUES (2, 'umo', 'test:private:13', 'sel_conv_id', ?, 't', 't')",
            (json.dumps({"val": "private-cid"}),),
        )
        conn.commit()
        conn.close()
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def test_dry_run_is_structural_and_does_not_write(self) -> None:
        digest = self._init_db()
        before = self.db.read_bytes()
        code = self._capture(["--db", str(self.db), "--umo", "test:group:97"])
        self.assertEqual(code[0], 0)
        payload = json.loads(code[1])
        self.assertEqual(payload["mode"], "dry-run")
        self.assertEqual(payload["history_sha256"], digest)
        self.assertEqual(payload["removed_unpaired_calls"], 1)
        self.assertFalse(payload["applied"])
        self.assertNotIn("plain answer", code[1])
        self.assertNotIn("opaque-reasoning", code[1])
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(list(Path(self.tmp.name).glob("*.orphan-tool-bak-*")), [])

    def test_apply_requires_hash_and_refuses_private(self) -> None:
        self._init_db()
        missing = self._capture(["--db", str(self.db), "--umo", "test:group:97", "--apply"])
        self.assertEqual(missing[0], 2)
        private = self._capture(
            ["--db", str(self.db), "--umo", "test:private:13", "--apply", "--expect-sha256", "abc"]
        )
        self.assertEqual(private[0], 2)
        self.assertIn("私聊", private[2])

    def test_apply_cas_updates_only_group_content(self) -> None:
        digest = self._init_db()
        wrong = self._capture(
            [
                "--db",
                str(self.db),
                "--umo",
                "test:group:97",
                "--apply",
                "--expect-sha256",
                "0" * 64,
            ]
        )
        self.assertEqual(wrong[0], 2)
        conn = sqlite3.connect(self.db)
        unchanged = conn.execute("SELECT content FROM conversations WHERE conversation_id='group-cid'").fetchone()[0]
        conn.close()
        self.assertEqual(hashlib.sha256(unchanged.encode()).hexdigest(), digest)

        code, out, err = self._capture(
            [
                "--db",
                str(self.db),
                "--umo",
                "test:group:97",
                "--apply",
                "--expect-sha256",
                digest,
            ]
        )
        self.assertEqual(err, "")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["applied"])
        backup = Path(self.tmp.name) / payload["backup"]
        self.assertTrue(backup.is_file())
        conn = sqlite3.connect(self.db)
        group = conn.execute(
            "SELECT content, title, token_usage, persona_id FROM conversations WHERE conversation_id='group-cid'"
        ).fetchone()
        private = conn.execute(
            "SELECT content, title FROM conversations WHERE conversation_id='private-cid'"
        ).fetchone()
        conn.close()
        repaired = json.loads(group[0])
        self.assertEqual(pending_model_violations(repaired), [])
        self.assertEqual(repaired[2]["content"][0]["text"], "plain answer")
        self.assertNotIn("tool_calls", repaired[1])
        self.assertEqual(group[1], "title")
        self.assertEqual(group[2], 7)
        self.assertEqual(group[3], "persona")
        self.assertIn("private stays", private[0])
        self.assertEqual(private[1], "keep")
        saved = sqlite3.connect(backup)
        old = json.loads(saved.execute("SELECT content FROM conversations WHERE conversation_id='group-cid'").fetchone()[0])
        saved.close()
        self.assertIn("tool_calls", old[1])

    def _capture(self, args: list[str]) -> tuple[int, str, str]:
        spec = importlib.util.spec_from_file_location("repair_orphan_tool_history", self.script)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        from io import StringIO
        from contextlib import redirect_stderr, redirect_stdout

        stdout = StringIO()
        stderr = StringIO()
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = module.main(args)
        except SystemExit as exc:
            if isinstance(exc.code, str):
                return 2, stdout.getvalue(), exc.code
            return int(exc.code or 0), stdout.getvalue(), stderr.getvalue()
        return int(code or 0), stdout.getvalue(), stderr.getvalue()


if __name__ == "__main__":
    unittest.main()
