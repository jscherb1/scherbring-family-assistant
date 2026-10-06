#!/usr/bin/env python3
"""Durable inbox/outbox for the Telegram bridge (stdlib only).

The bridge process writes every inbound update here BEFORE acknowledging it to
Telegram, so a crash never loses a message. The agent worker claims rows,
runs a turn, and only then marks them done (at-least-once). Replies go through
the outbox so a Bot API outage never loses an answer either.

Rows carry `channel` + `chat_ref` (telegram chat id, "dashboard", "terminal"...)
and a `session_key` (defaults to the chat ref; map several chats to one key via
`session_key_for` to share a session later).
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "state" / "inbox.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    channel      TEXT NOT NULL,
    chat_ref     TEXT NOT NULL,
    session_key  TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'message',   -- message | context_note
    external_id  TEXT,                              -- telegram update_id; dedupe key
    text         TEXT NOT NULL DEFAULT '',
    payload      TEXT NOT NULL DEFAULT '{}',        -- json: user, message_id, attachments...
    received_at  REAL NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending | processing | done | failed
    attempts     INTEGER NOT NULL DEFAULT 0,
    picked_at    REAL,
    done_at      REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS inbox_dedupe ON inbox(channel, external_id) WHERE external_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS inbox_status ON inbox(status, id);

CREATE TABLE IF NOT EXISTS outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    channel         TEXT NOT NULL,
    chat_ref        TEXT NOT NULL,
    text            TEXT NOT NULL,
    files           TEXT NOT NULL DEFAULT '[]',
    in_reply_to     INTEGER,
    created_at      REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending', -- pending | sent | failed
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    sent_at         REAL,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS outbox_status ON outbox(status, next_attempt_at);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

MAX_OUTBOX_ATTEMPTS = 8


def backoff_seconds(attempts: int) -> float:
    """1s, 2s, 4s ... capped at 30s (attempts is the count already made)."""
    return float(min(30, 2 ** max(0, attempts - 1)))


class BridgeStore:
    def __init__(self, path: Path | str = DEFAULT_DB, *, read_only: bool = False):
        self.path = Path(path)
        if read_only:  # dashboard: never create, migrate or write
            self._conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=10, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            return
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        # Several connections can open a fresh DB at once; the WAL switch and schema
        # creation need a lock that the busy timeout doesn't always cover, so retry.
        for attempt in range(50):
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=FULL")  # durability over speed
                self._conn.executescript(SCHEMA)
                break
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) or attempt == 49:
                    raise
                time.sleep(0.1)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # ---- key/value (poll offset, heartbeats, session ids) ----
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # ---- inbox ----
    def add_inbound(
        self,
        channel: str,
        chat_ref: str,
        text: str,
        *,
        external_id: str | None = None,
        session_key: str | None = None,
        kind: str = "message",
        payload: dict | None = None,
        now: float | None = None,
    ) -> int | None:
        """Insert an inbound row. Returns the id, or None if it's a duplicate."""
        try:
            cur = self._conn.execute(
                "INSERT INTO inbox(channel,chat_ref,session_key,kind,external_id,text,payload,received_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    channel,
                    str(chat_ref),
                    session_key or str(chat_ref),
                    kind,
                    external_id,
                    text,
                    json.dumps(payload or {}),
                    now if now is not None else time.time(),
                ),
            )
        except sqlite3.IntegrityError:
            return None
        return cur.lastrowid

    def recover_in_flight(self) -> int:
        """Called on worker start: rows left 'processing' by a crash go back to pending."""
        cur = self._conn.execute(
            "UPDATE inbox SET status='pending' WHERE status='processing' AND kind='message'"
        )
        return cur.rowcount

    def claim_next(self, session_key: str | None = None, now: float | None = None) -> dict | None:
        """Atomically claim the oldest pending message.

        Pending context notes for the same session are folded into the claimed
        row's `notes` list (and marked done) so the worker can prepend them to
        the turn without a model call per note.
        """
        with self._tx() as c:
            where = "status='pending' AND kind='message'"
            args: tuple = ()
            if session_key is not None:
                where += " AND session_key=?"
                args = (session_key,)
            row = c.execute(f"SELECT * FROM inbox WHERE {where} ORDER BY id LIMIT 1", args).fetchone()
            if row is None:
                return None
            ts = now if now is not None else time.time()
            c.execute(
                "UPDATE inbox SET status='processing', attempts=attempts+1, picked_at=? WHERE id=?",
                (ts, row["id"]),
            )
            note_rows = c.execute(
                "SELECT id, text, received_at FROM inbox WHERE status='pending' AND kind='context_note'"
                " AND session_key=? AND id<? ORDER BY id",
                (row["session_key"], row["id"]),
            ).fetchall()
            for n in note_rows:
                c.execute("UPDATE inbox SET status='done', done_at=? WHERE id=?", (ts, n["id"]))
        out = dict(row)
        out["attempts"] += 1
        out["payload"] = json.loads(out["payload"])
        out["notes"] = [{"text": n["text"], "received_at": n["received_at"]} for n in note_rows]
        return out

    def complete(self, inbox_id: int, now: float | None = None) -> None:
        self._conn.execute(
            "UPDATE inbox SET status='done', done_at=? WHERE id=?",
            (now if now is not None else time.time(), inbox_id),
        )

    def requeue(self, inbox_id: int) -> None:
        """Put a claimed message back to pending (attempts is kept, so retries are bounded)."""
        self._conn.execute("UPDATE inbox SET status='pending' WHERE id=? AND status='processing'", (inbox_id,))

    def fail(self, inbox_id: int) -> None:
        self._conn.execute("UPDATE inbox SET status='failed' WHERE id=?", (inbox_id,))

    # ---- outbox ----
    def enqueue_outbound(
        self,
        channel: str,
        chat_ref: str,
        text: str,
        *,
        files: list[str] | None = None,
        in_reply_to: int | None = None,
        now: float | None = None,
    ) -> int:
        ts = now if now is not None else time.time()
        cur = self._conn.execute(
            "INSERT INTO outbox(channel,chat_ref,text,files,in_reply_to,created_at,next_attempt_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (channel, str(chat_ref), text, json.dumps(files or []), in_reply_to, ts, ts),
        )
        return cur.lastrowid

    def next_outbound(self, channel: str | None = None, now: float | None = None) -> dict | None:
        ts = now if now is not None else time.time()
        sql = "SELECT * FROM outbox WHERE status='pending' AND next_attempt_at<=?"
        args: list = [ts]
        if channel:
            sql += " AND channel=?"
            args.append(channel)
        row = self._conn.execute(sql + " ORDER BY id LIMIT 1", args).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["files"] = json.loads(out["files"])
        return out

    def mark_sent(self, outbox_id: int, now: float | None = None) -> None:
        self._conn.execute(
            "UPDATE outbox SET status='sent', sent_at=?, error=NULL WHERE id=?",
            (now if now is not None else time.time(), outbox_id),
        )

    def mark_send_failed(self, outbox_id: int, error: str, now: float | None = None) -> str:
        """Record a failed send; reschedule with backoff or give up. Returns new status."""
        ts = now if now is not None else time.time()
        row = self._conn.execute("SELECT attempts FROM outbox WHERE id=?", (outbox_id,)).fetchone()
        attempts = (row["attempts"] if row else 0) + 1
        if attempts >= MAX_OUTBOX_ATTEMPTS:
            self._conn.execute(
                "UPDATE outbox SET status='failed', attempts=?, error=? WHERE id=?",
                (attempts, error[:500], outbox_id),
            )
            return "failed"
        self._conn.execute(
            "UPDATE outbox SET attempts=?, error=?, next_attempt_at=? WHERE id=?",
            (attempts, error[:500], ts + backoff_seconds(attempts), outbox_id),
        )
        return "pending"

    # ---- health / introspection ----
    def stats(self, now: float | None = None) -> dict:
        ts = now if now is not None else time.time()
        c = self._conn
        pending = c.execute("SELECT COUNT(*) n FROM inbox WHERE status='pending' AND kind='message'").fetchone()
        oldest = c.execute("SELECT MIN(received_at) oldest FROM inbox WHERE status IN ('pending','processing') AND kind='message'").fetchone()["oldest"]
        processing = c.execute("SELECT COUNT(*) n FROM inbox WHERE status='processing'").fetchone()["n"]
        out_pending = c.execute("SELECT COUNT(*) n FROM outbox WHERE status='pending'").fetchone()["n"]
        out_failed = c.execute("SELECT COUNT(*) n FROM outbox WHERE status='failed'").fetchone()["n"]
        return {
            "inbox_pending": pending["n"],
            "oldest_pending_age_s": (ts - oldest) if oldest else None,  # includes in-progress
            "inbox_processing": processing,
            "outbox_pending": out_pending,
            "outbox_failed": out_failed,
        }

    def health_snapshot(self, now: float | None = None, stale_s: float = 15) -> dict:
        """Everything the dashboard/agent_ctl needs to show liveness, from one place."""
        ts = now if now is not None else time.time()

        def age(key: str) -> float | None:
            v = self.kv_get(key)
            return round(ts - float(v), 1) if v else None

        bridge_age, worker_age, poll_age = age("bridge_heartbeat"), age("worker_heartbeat"), age("last_poll_ok")
        turn = self.kv_get("current_turn") or ""
        return {
            "bridge_up": bridge_age is not None and bridge_age <= stale_s,
            "bridge_heartbeat_age_s": bridge_age,
            "polling_ok": poll_age is not None and poll_age <= 60,  # Telegram reachable and long-polling
            "last_poll_ok_age_s": poll_age,
            "worker_up": worker_age is not None and worker_age <= stale_s,
            "worker_heartbeat_age_s": worker_age,
            "paused": self.kv_get("paused") == "1",
            "mcp_down": json.loads(self.kv_get("mcp_down") or "[]"),
            "current_turn": json.loads(turn) if turn else None,
            "last_canary": json.loads(self.kv_get("last_canary") or "null"),
            **self.stats(ts),
        }

    def recent_conversation(self, session_key: str, *, max_age_s: float = 48 * 3600, min_messages: int = 40,
                            max_bytes: int = 300_000, now: float | None = None) -> list[dict]:
        """Both directions, oldest first: everything newer than max_age_s, but never fewer than
        min_messages regardless of age; max_bytes only trims beyond that floor."""
        ts = now if now is not None else time.time()
        rows: list[dict] = []
        for r in self._conn.execute(
            "SELECT 'in' dir, received_at at, text FROM inbox WHERE session_key=? AND kind IN ('message','context_note')",
            (session_key,),
        ):
            rows.append(dict(r))
        chat_refs = {r["chat_ref"] for r in self._conn.execute("SELECT DISTINCT chat_ref FROM inbox WHERE session_key=?", (session_key,))}
        for ref in chat_refs:
            for r in self._conn.execute("SELECT 'out' dir, created_at at, text FROM outbox WHERE chat_ref=?", (ref,)):
                rows.append(dict(r))
        rows.sort(key=lambda r: r["at"])
        keep_from = len(rows)
        size = 0
        for i in range(len(rows) - 1, -1, -1):
            within_floor = (len(rows) - i) <= min_messages
            within_age = (ts - rows[i]["at"]) <= max_age_s
            size += len(rows[i]["text"].encode())
            if not within_floor and (not within_age or size > max_bytes):
                break
            keep_from = i
        return rows[keep_from:]
