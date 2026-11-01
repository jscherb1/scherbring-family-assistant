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

# Claude's own account-level connectors that nothing here uses; their auth state is not our problem.
# (The official Monarch connector was removed from this set on 2026-10-21: the finance agent now uses
# it as a fallback, so an expired authorization should show up.)
IGNORED_MCP: set[str] = set()

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

-- Telegram message ids of every delivered chunk, so a reply-to can be traced to what was sent.
CREATE TABLE IF NOT EXISTS sent_ids (
    chat_ref       TEXT NOT NULL,
    tg_message_id  INTEGER NOT NULL,
    outbox_id      INTEGER NOT NULL,
    PRIMARY KEY (chat_ref, tg_message_id)
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Columns added after the first release; created on open for older databases.
OUTBOX_MIGRATIONS = {
    "source": "TEXT",                         # who sent it: worker, bridge, scheduled:<task>, credential-check...
    "kind": "TEXT NOT NULL DEFAULT 'reply'",  # reply | notice | alert | scheduled
    "noted": "INTEGER NOT NULL DEFAULT 0",    # 1 = also recorded as a context note (the seed shows the note instead)
}

# Inbox columns added after the first release.
INBOX_MIGRATIONS = {
    "triage": "TEXT",   # fast lane: NULL = not looked at yet, 'fast' = handled by the fast lane, 'pass:<why>' = for the agent
}

FASTLANE_MAX_WAIT_S = 10      # a message the fast lane has not triaged by now is claimed anyway (fail open)
FASTLANE_BEAT_STALE_S = 5     # ...and the gate is off entirely when the fast-lane task isn't beating
MAX_OUTBOX_ATTEMPTS = 8
MAX_FOLDED_NOTES = 20


def backoff_seconds(attempts: int) -> float:
    """1s, 2s, 4s ... capped at 30s (attempts is the count already made)."""
    return float(min(30, 2 ** max(0, attempts - 1)))


def session_key_for(chat_ref: str) -> str:
    """Session a chat maps to: its own id unless BRIDGE_SESSION_MAP (JSON) says otherwise."""
    from paths import get_env

    try:
        mapping = json.loads(get_env("BRIDGE_SESSION_MAP") or "{}")
    except ValueError:
        mapping = {}
    return mapping.get(str(chat_ref), str(chat_ref))


def _note_text(source: str | None, text: str) -> str:
    return f"[{source or 'system'}] sent you: {text}"[:4000]


def _exchange_text(source: str, request: str, result: str) -> str:
    """Context note for something answered without an agent turn: what was asked and what was done."""
    return f"[{source}] You asked: {request}\n[{source}] Handled directly without an agent turn, I replied: {result}"[:4000]


def record_context_note(chat_ref: str, text: str, source: str, *, db: Path | str = DEFAULT_DB) -> None:
    """Make something delivered to the user outside a conversation turn (a scheduled task result)
    visible to the main session on its next turn. Best effort: never raises."""
    try:
        store = BridgeStore(db)
        try:
            store.add_inbound(source, str(chat_ref), text[:4000], kind="context_note",
                              session_key=session_key_for(str(chat_ref)))
        finally:
            store.close()
    except Exception:  # noqa: BLE001
        pass


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
                self._migrate()
                break
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) or attempt == 49:
                    raise
                time.sleep(0.1)

    def _migrate(self) -> None:
        for table, migrations in (("outbox", OUTBOX_MIGRATIONS), ("inbox", INBOX_MIGRATIONS)):
            have = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            for col, decl in migrations.items():
                if col not in have:
                    try:
                        self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                    except sqlite3.OperationalError as e:  # another connection added it first
                        if "duplicate column" not in str(e):
                            raise

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

    # ---- per-session turn state (several sessions can run turns at once) ----
    def set_current_turn(self, session_key: str, inbox_id, started: float) -> None:
        self.kv_set(f"current_turn:{session_key}", json.dumps({"inbox_id": inbox_id, "started": started}))

    def clear_current_turn(self, session_key: str) -> None:
        self.kv_set(f"current_turn:{session_key}", "")

    def clear_all_current_turns(self) -> None:
        """Worker start: nothing is running yet, so any leftover marker (incl. the old single-turn key) is stale."""
        self._conn.execute("DELETE FROM kv WHERE key='current_turn' OR key LIKE 'current_turn:%'")

    def current_turns(self) -> dict[str, dict]:
        """{session_key: {inbox_id, started}} for turns running now. Tolerates the pre-concurrency
        single `current_turn` value (shown as session 'legacy') and unparseable values."""
        out: dict[str, dict] = {}
        for r in self._conn.execute("SELECT key, value FROM kv WHERE (key='current_turn' OR key LIKE 'current_turn:%') AND value<>''"):
            try:
                v = json.loads(r["value"])
            except ValueError:
                continue
            if not isinstance(v, dict):
                continue
            key = r["key"].split(":", 1)[1] if ":" in r["key"] else "legacy"
            out[key] = {"inbox_id": v.get("inbox_id"), "started": v.get("started")}
        return out

    def clear_mcp_down(self, session_key: str | None = None) -> None:
        """Forget one session's MCP state (closed/removed), or every session's (worker start), so a stale
        key can't pin the global list."""
        if session_key is None:
            self._conn.execute("DELETE FROM kv WHERE key LIKE 'mcp_down:%'")
        else:
            self._conn.execute("DELETE FROM kv WHERE key=?", (f"mcp_down:{session_key}",))
        self._recompute_mcp_down()

    def set_mcp_down(self, session_key: str, names: list[str]) -> None:
        """Record one session's disconnected MCP servers; the global `mcp_down` is the union over
        sessions, so two sessions with momentarily different views don't flip it back and forth."""
        self.kv_set(f"mcp_down:{session_key}", json.dumps(names))
        self._recompute_mcp_down()

    def _recompute_mcp_down(self) -> None:
        union: set[str] = set()
        for r in self._conn.execute("SELECT value FROM kv WHERE key LIKE 'mcp_down:%'"):
            try:
                union.update(json.loads(r["value"]))
            except ValueError:
                pass
        self.kv_set("mcp_down", json.dumps(sorted(union)))

    # ---- fast lane gate ----
    def fastlane_gate_active(self, now: float | None = None) -> bool:
        """True when the fast lane is switched on AND its task is alive. Only then does claim_next hold back
        messages the fast lane has not looked at yet, so a dead or disabled fast lane can never stall anything."""
        if self.kv_get("fastlane") != "on":
            return False
        ts = now if now is not None else time.time()
        beat = self.kv_get("fastlane_beat")
        try:
            return bool(beat) and ts - float(beat) <= FASTLANE_BEAT_STALE_S
        except ValueError:
            return False

    def _claimable(self, now: float | None) -> tuple[str, tuple]:
        """SQL condition (and args) for pending messages the agent may take now."""
        ts = now if now is not None else time.time()
        where = "status='pending' AND kind='message'"
        if self.fastlane_gate_active(ts):
            return where + " AND (triage IS NOT NULL OR received_at <= ?)", (ts - FASTLANE_MAX_WAIT_S,)
        return where, ()

    def pending_session_keys(self, now: float | None = None) -> list[str]:
        """Sessions with a claimable message, oldest waiting first."""
        where, args = self._claimable(now)
        return [r["session_key"] for r in self._conn.execute(
            f"SELECT session_key FROM inbox WHERE {where} GROUP BY session_key ORDER BY MIN(id)", args)]

    def untriaged(self, limit: int = 20) -> list[dict]:
        """New messages the fast lane has not looked at, oldest first."""
        rows = self._conn.execute(
            "SELECT * FROM inbox WHERE status='pending' AND kind='message' AND triage IS NULL ORDER BY id LIMIT ?",
            (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d["payload"])
            except ValueError:
                d["payload"] = {}
            out.append(d)
        return out

    def set_triage(self, inbox_id: int, value: str) -> bool:
        """Record a pass decision ('pass:<why>') on a message nobody has taken yet."""
        cur = self._conn.execute(
            "UPDATE inbox SET triage=? WHERE id=? AND status='pending' AND triage IS NULL", (value[:80], inbox_id))
        return cur.rowcount == 1

    def fastlane_claim(self, inbox_id: int, now: float | None = None) -> bool:
        """Take an untriaged pending message for the fast lane (atomic against claim_next). attempts becomes 1,
        so if the worker dies mid-action the agent's redelivery carries the "may have partially executed" marker."""
        ts = now if now is not None else time.time()
        cur = self._conn.execute(
            "UPDATE inbox SET status='processing', triage='fast', attempts=attempts+1, picked_at=?"
            " WHERE id=? AND status='pending' AND triage IS NULL", (ts, inbox_id))
        return cur.rowcount == 1

    def fastlane_release(self, inbox_id: int, why: str, *, side_effects_possible: bool) -> None:
        """Hand a fast-lane claim to the agent after all. With no possible side effect the attempt does not
        count; otherwise it does, and the agent is told to check state before repeating anything."""
        self._conn.execute(
            "UPDATE inbox SET status='pending', triage=?, attempts=CASE WHEN ? THEN attempts ELSE 0 END"
            " WHERE id=? AND status='processing' AND triage='fast'",
            (("pass:" + why)[:80], int(side_effects_possible), inbox_id))

    def earlier_messages(self, session_key: str, before_id: int) -> list[dict]:
        """Messages of this session queued or running ahead of `before_id` (for the fast lane's ordering rule)."""
        return [dict(r) for r in self._conn.execute(
            "SELECT id, text, status, triage FROM inbox WHERE session_key=? AND kind='message' AND id<?"
            " AND status IN ('pending','processing') ORDER BY id", (session_key, before_id))]

    def recent_assistant_question(self, session_key: str, *, within_s: float = 600, scheduled_within_s: float = 4 * 3600,
                                  now: float | None = None, exclude_id: int | None = None) -> str | None:
        """Why the next message may be an answer: the assistant's last outbound for this session asked something in
        the last `within_s`, or a scheduled task asked something (a check-in) in the last `scheduled_within_s` that
        no later message has answered. Returns a short reason or None."""
        ts = now if now is not None else time.time()
        refs = [r["chat_ref"] for r in self._conn.execute("SELECT DISTINCT chat_ref FROM inbox WHERE session_key=?", (session_key,))]
        if not refs:
            return None
        marks = ",".join("?" * len(refs))
        asks = lambda text: "?" in ((text or "").strip().splitlines() or [""])[-1]  # noqa: E731
        last = self._conn.execute(
            f"SELECT text, created_at FROM outbox WHERE chat_ref IN ({marks}) AND status<>'failed' ORDER BY id DESC LIMIT 1",
            refs).fetchone()
        if last and ts - last["created_at"] <= within_s and asks(last["text"]):
            return "question"
        sched = self._conn.execute(
            f"SELECT text, created_at FROM outbox WHERE chat_ref IN ({marks}) AND source LIKE 'scheduled:%' AND status<>'failed'"
            " ORDER BY id DESC LIMIT 1", refs).fetchone()
        if sched and ts - sched["created_at"] <= scheduled_within_s and "?" in (sched["text"] or ""):
            answered = self._conn.execute(
                "SELECT 1 FROM inbox WHERE session_key=? AND kind='message' AND received_at>? AND id<>? LIMIT 1",
                (session_key, sched["created_at"], exclude_id or 0)).fetchone()
            if not answered:
                return "scheduled_question"
        return None

    def active_session_keys(self) -> set[str]:
        """Sessions with a message pending or in progress."""
        return {r["session_key"] for r in self._conn.execute(
            "SELECT DISTINCT session_key FROM inbox WHERE status IN ('pending','processing') AND kind='message'")}

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
            where, args = self._claimable(now)
            if session_key is not None:
                where += " AND session_key=?"
                args = args + (session_key,)
            row = c.execute(f"SELECT * FROM inbox WHERE {where} ORDER BY id LIMIT 1", args).fetchone()
            if row is None:
                return None
            ts = now if now is not None else time.time()
            c.execute(
                "UPDATE inbox SET status='processing', attempts=attempts+1, picked_at=? WHERE id=?",
                (ts, row["id"]),
            )
            # Notes are 'folded' into this message (picked_at holds the message id) and only become
            # 'done' when it completes, so a redelivered message gets the same notes again.
            c.execute(
                "UPDATE inbox SET status='folded', picked_at=? WHERE status='pending' AND kind='context_note'"
                " AND session_key=? AND id<?",
                (row["id"], row["session_key"], row["id"]),
            )
            all_notes = c.execute(
                "SELECT text, received_at FROM inbox WHERE status='folded' AND kind='context_note' AND picked_at=?"
                " ORDER BY id",
                (row["id"],),
            ).fetchall()
        note_rows = all_notes[-MAX_FOLDED_NOTES:]  # older ones stay folded (and are closed on complete) but aren't shown
        out = dict(row)
        out["attempts"] += 1
        out["payload"] = json.loads(out["payload"])
        out["notes"] = [{"text": n["text"], "received_at": n["received_at"]} for n in note_rows]
        out["notes_hidden"] = len(all_notes) - len(note_rows)
        return out

    def complete(self, inbox_id: int, now: float | None = None) -> None:
        ts = now if now is not None else time.time()
        self._conn.execute("UPDATE inbox SET status='done', done_at=? WHERE id=?", (ts, inbox_id))
        self._conn.execute(
            "UPDATE inbox SET status='done', done_at=? WHERE status='folded' AND kind='context_note' AND picked_at=?",
            (ts, inbox_id),
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
        source: str | None = None,
        kind: str = "reply",
        note: bool = False,
        note_text: str | None = None,
        now: float | None = None,
    ) -> int:
        """Queue a message for the bridge to send. `note=True` also records it as a context note, for
        messages the live session didn't write itself (alerts, notices), so it can discuss them later.
        `note_text` replaces the default "sent you: ..." wording of that note."""
        ts = now if now is not None else time.time()
        cur = self._conn.execute(
            "INSERT INTO outbox(channel,chat_ref,text,files,in_reply_to,created_at,next_attempt_at,source,kind,noted)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (channel, str(chat_ref), text, json.dumps(files or []), in_reply_to, ts, ts, source, kind, int(note)),
        )
        if note:
            self.add_inbound(source or channel, str(chat_ref), note_text or _note_text(source, text), kind="context_note",
                             session_key=session_key_for(str(chat_ref)), now=ts)
        return cur.lastrowid

    def record_fast_exchange(self, row: dict, reply: str, *, source: str = "fastlane", now: float | None = None) -> int:
        """An action the fast lane handled: queue the reply (source=fastlane, so replying to it can be traced) and
        leave ONE context note with both the request and the result for the agent's next turn. The request row is
        then hidden from recent_conversation (triage='fast'), so nothing shows twice."""
        return self.enqueue_outbound(
            row["channel"], row["chat_ref"], reply, in_reply_to=row["id"], source=source, kind="reply", note=True,
            note_text=_exchange_text(source, row["text"], reply), now=now)

    def record_sent(self, channel: str, chat_ref: str, text: str, tg_message_ids: list[int], *,
                    source: str, kind: str = "alert", note: bool = True, now: float | None = None) -> int:
        """Log a message something else already delivered (direct Bot API send) as a sent outbox row,
        with its Telegram ids, plus a context note so the conversation knows about it."""
        ts = now if now is not None else time.time()
        cur = self._conn.execute(
            "INSERT INTO outbox(channel,chat_ref,text,created_at,next_attempt_at,status,sent_at,attempts,source,kind,noted)"
            " VALUES(?,?,?,?,?,'sent',?,1,?,?,?)",
            (channel, str(chat_ref), text, ts, ts, ts, source, kind, int(note)),
        )
        self.add_sent_ids(cur.lastrowid, str(chat_ref), tg_message_ids)
        if note:
            self.add_inbound(source, str(chat_ref), _note_text(source, text), kind="context_note",
                             session_key=session_key_for(str(chat_ref)), now=ts)
        return cur.lastrowid

    def add_sent_ids(self, outbox_id: int, chat_ref: str, tg_message_ids: list[int]) -> None:
        for mid in tg_message_ids:
            self._conn.execute(
                "INSERT OR REPLACE INTO sent_ids(chat_ref,tg_message_id,outbox_id) VALUES(?,?,?)",
                (str(chat_ref), int(mid), outbox_id),
            )

    def find_sent(self, chat_ref: str, tg_message_id: int) -> dict | None:
        """The outbox row a Telegram message id belongs to (for "replying to" context), or None."""
        row = self._conn.execute(
            "SELECT o.id, o.text, o.source, o.kind, COALESCE(o.sent_at, o.created_at) AS at FROM sent_ids s"
            " JOIN outbox o ON o.id = s.outbox_id WHERE s.chat_ref=? AND s.tg_message_id=?",
            (str(chat_ref), int(tg_message_id)),
        ).fetchone()
        return dict(row) if row else None

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

    def mark_sent(self, outbox_id: int, now: float | None = None, tg_message_ids: list[int] | None = None) -> None:
        self._conn.execute(
            "UPDATE outbox SET status='sent', sent_at=?, error=NULL WHERE id=?",
            (now if now is not None else time.time(), outbox_id),
        )
        if tg_message_ids:
            row = self._conn.execute("SELECT chat_ref FROM outbox WHERE id=?", (outbox_id,)).fetchone()
            if row:
                self.add_sent_ids(outbox_id, row["chat_ref"], tg_message_ids)

    def mark_send_abandoned(self, outbox_id: int, error: str) -> str:
        """Give up on a send without retrying (a timed-out send may already have been delivered)."""
        self._conn.execute(
            "UPDATE outbox SET status='failed', attempts=attempts+1, error=? WHERE id=?",
            (error[:500], outbox_id),
        )
        return "failed"

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
        turns = self.current_turns()
        oldest = min(turns.items(), key=lambda kv: kv[1].get("started") or ts, default=None)
        return {
            "bridge_up": bridge_age is not None and bridge_age <= stale_s,
            "bridge_heartbeat_age_s": bridge_age,
            "polling_ok": poll_age is not None and poll_age <= 60,  # Telegram reachable and long-polling
            "last_poll_ok_age_s": poll_age,
            "worker_up": worker_age is not None and worker_age <= stale_s,
            "worker_heartbeat_age_s": worker_age,
            "paused": self.kv_get("paused") == "1",
            "mcp_down": [n for n in json.loads(self.kv_get("mcp_down") or "[]") if n not in IGNORED_MCP],
            "current_turns": [{"session": k, **v} for k, v in sorted(turns.items())],
            "current_turn": {"session": oldest[0], **oldest[1]} if oldest else None,  # longest-running one
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
            "SELECT CASE kind WHEN 'context_note' THEN 'note' ELSE 'in' END dir, received_at at, text FROM inbox"
            " WHERE session_key=? AND kind IN ('message','context_note') AND COALESCE(triage,'')<>'fast'",
            (session_key,),
        ):
            rows.append(dict(r))
        chat_refs = {r["chat_ref"] for r in self._conn.execute("SELECT DISTINCT chat_ref FROM inbox WHERE session_key=?", (session_key,))}
        for ref in chat_refs:
            # Rows that were also recorded as a note appear once, as the note.
            for r in self._conn.execute("SELECT 'out' dir, created_at at, text FROM outbox WHERE chat_ref=? AND noted=0", (ref,)):
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
