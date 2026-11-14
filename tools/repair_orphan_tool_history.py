#!/usr/bin/env python3
"""离线审计并按哈希比较交换更新一条群会话里的未配对工具调用。

默认只打印结构统计，不写数据库，不输出消息正文、参数或 call id。
真正写回需要同时给出 --apply 和 dry-run 打印的 history_sha256。
只处理 umo 类型为 group / GroupMessage 的当前会话，拒绝私聊。
不新建会话，不改其它列或其它会话，不补造工具结果。

备份使用 SQLite 的 online backup API，会带上已提交的 WAL，而不是直接复制 db 文件。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tool_history import load_history_records, repair_tool_history

GROUP_KINDS = {"group", "groupmessage"}
PRIVATE_KINDS = {"private", "friend", "friendmessage", "privatemessage"}


def _session_kind(umo: str) -> str:
    parts = umo.split(":")
    if len(parts) < 3 or not all(parts):
        raise SystemExit("umo 必须是 platform:type:session 三段完整值")
    return parts[1].lower()


def _connect(path: Path, *, readonly: bool) -> sqlite3.Connection:
    if readonly:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    return sqlite3.connect(path)


def _raw_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _resolve_conversation_id(conn: sqlite3.Connection, umo: str) -> str:
    row = conn.execute(
        """
        SELECT value FROM preferences
        WHERE scope = 'umo' AND scope_id = ? AND key = 'sel_conv_id'
        """,
        (umo,),
    ).fetchone()
    if row is None:
        raise SystemExit("该 umo 没有 sel_conv_id，未扫描其它会话")
    payload = json.loads(_raw_text(row[0]))
    if not isinstance(payload, dict):
        raise SystemExit("sel_conv_id 的存储不是对象")
    cid = payload.get("val")
    if not isinstance(cid, str) or not cid:
        raise SystemExit("sel_conv_id 没有字符串 val")
    return cid


def _load_content(conn: sqlite3.Connection, conversation_id: str) -> tuple[str, list]:
    row = conn.execute(
        "SELECT content FROM conversations WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchone()
    if row is None:
        raise SystemExit("sel_conv_id 指向的会话不存在")
    if row[0] is None:
        raise SystemExit("该会话 content 为空，不写回")
    raw = _raw_text(row[0])
    records = load_history_records(raw)
    return raw, records


def _backup(source: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = source.with_name(f"{source.name}.orphan-tool-bak-{stamp}")
    if target.exists():
        raise SystemExit(f"备份目标已存在：{target.name}")
    src = sqlite3.connect(source)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


def _report(payload: dict) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path, help="AstrBot data_v4.db 路径")
    parser.add_argument("--umo", required=True, help="完整 unified_msg_origin，必须是群会话")
    parser.add_argument(
        "--expect-sha256",
        default="",
        help="dry-run 输出的 history_sha256；只有 --apply 时使用",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="按哈希比较交换写回。缺省时只做 dry-run",
    )
    args = parser.parse_args(argv)

    kind = _session_kind(args.umo)
    if kind in PRIVATE_KINDS:
        raise SystemExit("私聊历史本次不持久修改")
    if kind not in GROUP_KINDS:
        raise SystemExit(f"只接受群会话，当前类型是 {kind}")
    if not args.db.is_file():
        raise SystemExit("数据库文件不存在")
    if args.apply and not args.expect_sha256:
        raise SystemExit("写回必须同时提供 --expect-sha256")

    conn = _connect(args.db, readonly=True)
    try:
        conversation_id = _resolve_conversation_id(conn, args.umo)
        raw, records = _load_content(conn, conversation_id)
        digest = _sha256(raw)
        repaired, stats = repair_tool_history(records, preserve_open_tail=False)
        payload = {
            "mode": "apply" if args.apply else "dry-run",
            "umo_kind": kind,
            "conversation_id": conversation_id,
            "history_sha256": digest,
            "message_count": len(records),
            "role_counts_before": _role_counts(records),
            "role_counts_after": stats.role_counts,
            "kept_tool_exchanges": stats.kept_exchanges,
            "removed_unpaired_calls": stats.removed_calls,
            "dropped_tool_results": stats.dropped_results,
            "dropped_empty_assistants": stats.dropped_empty_assistants,
            "changed": stats.changed,
            "applied": False,
        }
        if not args.apply:
            _report(payload)
            return 0
        if digest != args.expect_sha256.lower():
            payload["error"] = "history_sha256 与当前内容不一致，未写回"
            _report(payload)
            return 2
        if not stats.changed:
            payload["applied"] = False
            payload["note"] = "历史无需修改"
            _report(payload)
            return 0
    finally:
        conn.close()

    backup = _backup(args.db)
    new_text = json.dumps(repaired, ensure_ascii=False)
    conn = _connect(args.db, readonly=False)
    try:
        conn.execute("BEGIN IMMEDIATE")
        current_raw, _current_records = _load_content(conn, conversation_id)
        if _sha256(current_raw) != digest or current_raw != raw:
            conn.rollback()
            payload["error"] = "写回前内容已变化，未修改"
            payload["backup"] = backup.name
            _report(payload)
            return 2
        cursor = conn.execute(
            """
            UPDATE conversations
            SET content = ?
            WHERE conversation_id = ? AND content = ?
            """,
            (new_text, conversation_id, raw),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            payload["error"] = "比较交换未命中唯一行，已回滚"
            payload["backup"] = backup.name
            _report(payload)
            return 2
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    payload["applied"] = True
    payload["backup"] = backup.name
    payload["history_sha256_after"] = _sha256(new_text)
    _report(payload)
    return 0


def _role_counts(records: list) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in records:
        if isinstance(item, dict):
            role = str(item.get("role") or "")
            counts[role] = counts.get(role, 0) + 1
    return counts


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit as exc:
        if isinstance(exc.code, str):
            sys.stderr.write(exc.code + "\n")
            raise SystemExit(2) from exc
        raise
