#!/usr/bin/env python3
"""Agent worker: claims inbox messages, runs a Claude turn, queues the reply.

Restartable by design. All durable state lives in the bridge store (inbox/outbox)
and in the Claude session transcript; on start each session is resumed by id, so
a restart loses neither queued messages nor conversation context.

Usage:
    python scripts/agent_worker.py [--db state/inbox.db] [--model sonnet]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import socket
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fastlane  # noqa: E402
from bridge_store import BridgeStore  # noqa: E402
from bridge_store import session_key_for  # noqa: E402
from paths import REPO_ROOT, dev_chat_ids, household_chat_id  # noqa: E402

log = logging.getLogger("worker")

STREAM_LOG = REPO_ROOT / "state" / "logs" / "agent_stream.jsonl"
USAGE_LOG = REPO_ROOT / "state" / "usage.jsonl"
TURN_TIMEOUT_S = 900
SLOW_ACK_AFTER_S = 20
MAX_ATTEMPTS = 3
MAX_CONCURRENT_SESSIONS = 4  # each live session is a claude subprocess; bound memory and API fan-out
STOP_GRACE_S = 10  # systemd TimeoutStopSec is 20: 10 s for turns + <=3 s cancel + <=4 s closing clients; cancelled turns are redelivered
STOP_CANCEL_WAIT_S = 3
STOP_CLOSE_WAIT_S = 4
RETRY_BACKOFF_S = 1.0  # pause after an unexpected failure so a locked database does not make a loop spin
MCP_READY_TIMEOUT_S = 90
HEARTBEAT_EVERY_S = 2
PING_EVERY_S = 30
MODEL_CANARY_EVERY_S = 1800
PING_TIMEOUT_S = 20
FASTLANE_POLL_S = 0.1          # how often new messages are looked at; Tier 0 answers within about this long
FASTLANE_BEAT_EVERY_S = 1.0    # liveness stamp that lets claim_next hold messages for triage (it ignores it after 5 s)
FASTLANE_EXEC_TIMEOUT_S = 30   # an action's service calls (HA, Todoist) have their own 8 s timeouts
MODEL_CANARY_TIMEOUT_S = 90
ROTATE_CONTEXT_TOKENS = 150_000
ROTATE_AGE_S = 7 * 86400
COMPACT_MIN_TOKENS = 40_000
MAINTENANCE_HOUR = 3  # local time; quiet-hours compaction
COMPACT_INSTRUCTIONS = ("/compact Preserve: open tasks and pending questions, the user's preferences and corrections, "
                        "outcomes of scheduled tasks, and anything the user asked to be remembered.")

CHANNEL_PROMPT = """\
You are running behind a message bridge, not the Telegram plugin. Overrides to anything in CLAUDE.md about Telegram:
- Your final assistant message is delivered to the user automatically on the channel the message came from. Do NOT call any Telegram reply/react/edit tool and do NOT run scripts/telegram_send.py for normal replies.
- Acknowledgements (reaction, typing, a "working on it" notice for slow turns) are sent by the bridge automatically. Do not send your own.
- Never use AskUserQuestion; if you need an answer, just ask in your final message and the user's reply will arrive as the next message.
- Messages arrive wrapped in a <channel ...> tag with chat/user/time metadata; "[Earlier notes]" blocks are context (e.g. results of scheduled tasks and alerts already delivered to the user), not requests.
- A <replying_to ...> block means the user used Telegram's reply on an earlier message; it holds that message's text and who sent it (e.g. source="scheduled:finance-transaction-review" or "credential-check"). Read the user's message as being about that one.
- A message with via="web" was typed into the family dashboard's ask box instead of Telegram. Treat it like any other message: your reply still lands in the Telegram chat.
- A message with via="ios" was sent from the user's iPhone Shortcut. Same handling: your reply lands in Telegram, where it will likely be read on a phone, so keep it concise.
- If a message is marked as a retry, a previous attempt may have partially executed: check current state before repeating any side effect.
"""

DEV_PROMPT = """\
This is the DEVELOPMENT chat (not the household chat). For any code or config change:
- This session starts in the main checkout, which scheduled runs share. For ANY code or config change, create the worktree FIRST (`git worktree add`, new branch from origin/main) before editing anything. Never leave the main checkout dirty or on a branch other than main, and never switch its branch.
- A worktree has no `state/` directory, so scripts run there do not see the live data. Never test against the live database (state/*.db) without saying so explicitly.
- Follow CLAUDE.md's development process: present a plan and wait for approval, work on a branch, open a PR, merge only when the user says to.
"""


def is_dev_session(session_key: str) -> bool:
    """True when this session belongs to a chat listed in BRIDGE_DEV_CHAT_IDS (directly or via BRIDGE_SESSION_MAP)."""
    dev = dev_chat_ids()
    return session_key in dev or any(session_key_for(c) == session_key for c in dev)


@dataclass
class TurnResult:
    text: str
    session_id: str | None = None
    cost_usd: float | None = None
    usage: dict = field(default_factory=dict)


class AgentSession(Protocol):
    async def ensure_ready(self) -> None: ...
    async def run_turn(self, prompt: str, on_event: Callable[[dict], None]) -> TurnResult: ...
    async def close(self) -> None: ...


def format_turn_prompt(row: dict) -> str:
    """Wrap an inbox row (plus folded context notes) as the user turn."""
    p = row["payload"]
    parts = []
    if row.get("notes"):
        lines = []
        for n in row["notes"]:
            when = datetime.fromtimestamp(n["received_at"]).strftime("%Y-%m-%d %H:%M")
            lines.append(f"- [{when}] {n['text']}")
        if row.get("notes_hidden"):
            lines.insert(0, f"- (+{row['notes_hidden']} older notes not shown)")
        parts.append("[Earlier notes]\n" + "\n".join(lines) + "\n[/Earlier notes]")
    if row["attempts"] > 1:
        parts.append(f"[Retry #{row['attempts']}: a previous attempt at this message did not finish; it may have partially executed.]")
    ts = datetime.fromtimestamp(p.get("date") or row["received_at"]).isoformat(timespec="seconds")
    via = f' via="{p["via"]}"' if p.get("via") else ""
    attrs = f'source="{row["channel"]}" chat_id="{row["chat_ref"]}" message_id="{p.get("message_id", "")}" user="{p.get("user", "")}" ts="{ts}"{via}'
    body = row["text"]
    for att in p.get("attachments", []):
        if att.get("path"):
            body += f"\n[attachment {att['kind']}: {att['path']}]"
    rt = p.get("reply_to")
    if rt:
        if rt.get("source"):
            src = rt["source"]
        else:
            src = "assistant" if rt.get("from_bot") else "user"
        sent = f' sent="{datetime.fromtimestamp(rt["at"]).isoformat(timespec="seconds")}"' if rt.get("at") else ""
        parts.append(f'<replying_to source="{src}"{sent}>\n{rt.get("text", "")}\n</replying_to>')
    parts.append(f"<channel {attrs}>\n{body}\n</channel>")
    return "\n\n".join(parts)


def resolve_reply_to(store: BridgeStore, row: dict) -> None:
    """Swap Telegram's quoted copy of a replied-to message for our record of it (full text, sender)."""
    rt = row["payload"].get("reply_to")
    if not rt or rt.get("message_id") is None:
        return
    try:
        hit = store.find_sent(row["chat_ref"], rt["message_id"])
    except Exception:  # noqa: BLE001
        hit = None
    if hit:
        rt.update(text=hit["text"], source=hit["source"] or "assistant", at=hit["at"])


def format_seed(rows: list[dict]) -> str:
    """Rebuild context from the durable message log when a session can't be resumed."""
    if not rows:
        return ""
    lines = []
    for r in rows:
        who = {"in": "User", "out": "Assistant", "note": "Sent outside this chat"}.get(r["dir"], "Assistant")
        when = datetime.fromtimestamp(r["at"]).strftime("%Y-%m-%d %H:%M")
        lines.append(f"[{when}] {who}: {r['text']}")
    return ("[Conversation history restored from the message log because the previous session could not be "
            "resumed. Treat it as your memory of the recent conversation.]\n" + "\n".join(lines) + "\n[/history]\n\n")


def sd_notify(msg: str) -> None:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode())
    except OSError:
        pass


def _append_jsonl(path: Path, obj: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, default=str) + "\n")
    except OSError as e:
        log.debug("jsonl write failed: %s", e)


class SdkSession:
    """One long-lived Claude SDK client for a session key, resumed by id."""

    def __init__(self, session_key: str, store: BridgeStore, model: str):
        self.key = session_key
        self.store = store
        self.model = model
        self._client: Any = None
        self.mcp_ready = False
        self.seed: str = ""  # history to prepend to the next turn after a fresh (non-resumed) start

    def _options(self):
        from claude_agent_sdk import ClaudeAgentOptions

        resume = self.store.kv_get(f"session_id:{self.key}")
        return ClaudeAgentOptions(
            model=self.model,
            cwd=str(REPO_ROOT),
            setting_sources=["project"],
            permission_mode="auto",
            # The dev note lives in the system prompt (fixed per session), not per turn, so the cached prefix stays stable.
            system_prompt={"type": "preset", "preset": "claude_code", "append": CHANNEL_PROMPT + ("\n" + DEV_PROMPT if is_dev_session(self.key) else "")},
            resume=resume or None,
            # A background task outlives its turn and its completion has nowhere to be
            # delivered here; make the agent run things in the foreground instead.
            env={"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"},
        )

    async def ensure_ready(self) -> None:
        from claude_agent_sdk import ClaudeSDKClient

        if self._client is None:
            try:
                self._client = ClaudeSDKClient(options=self._options())
                await self._client.connect()
            except Exception as e:  # noqa: BLE001
                if not self.store.kv_get(f"session_id:{self.key}"):
                    raise
                log.error("could not resume session for %s (%r); starting fresh from the message log", self.key, e)
                await self.close()
                self._start_fresh()
                self._client = ClaudeSDKClient(options=self._options())
                await self._client.connect()
            self.mcp_ready = False
        if not self.store.kv_get(f"session_id:{self.key}") and not self.seed:
            # A brand-new session (first run, or after a rotation): give it the recent conversation.
            self.seed = format_seed(self.store.recent_conversation(self.key))
        deadline = time.time() + MCP_READY_TIMEOUT_S
        while time.time() < deadline:
            st = await self._client.get_mcp_status()
            servers = st.get("mcpServers", []) if isinstance(st, dict) else []
            pending = [s.get("name") for s in servers if s.get("status") == "pending"]
            if not pending:
                bad = [s.get("name") for s in servers if s.get("status") not in ("connected", "disabled")]
                self.store.set_mcp_down(self.key, bad)
                self.mcp_ready = True
                return
            await asyncio.sleep(1)
        log.warning("MCP servers still pending after %ss: proceeding anyway", MCP_READY_TIMEOUT_S)
        self.store.set_mcp_down(self.key, ["timeout"])

    async def run_turn(self, prompt: str, on_event: Callable[[dict], None], *, require_assistant: bool = True) -> TurnResult:
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock, UserMessage

        await self.ensure_ready()
        await self._client.query(self.seed + prompt)
        result = TurnResult(text="")
        seen_assistant = False
        # receive_messages (not receive_response): the latter stops at the FIRST result,
        # which can be a stale one from earlier out-of-band work (e.g. a background task
        # finishing) and would end this turn unanswered.
        async for m in self._client.receive_messages():
            if isinstance(m, AssistantMessage):
                seen_assistant = True
                for b in m.content:
                    if isinstance(b, TextBlock):
                        on_event({"type": "text", "text": b.text})
                    elif isinstance(b, ThinkingBlock):
                        on_event({"type": "thinking", "text": b.thinking})
                    elif isinstance(b, ToolUseBlock):
                        on_event({"type": "tool_use", "name": b.name, "input": b.input})
            elif isinstance(m, UserMessage) and isinstance(m.content, list):
                for b in m.content:
                    if isinstance(b, ToolResultBlock):
                        on_event({"type": "tool_result", "is_error": b.is_error, "content": str(b.content)[:2000]})
            elif isinstance(m, ResultMessage):
                if require_assistant and not seen_assistant:
                    on_event({"type": "stale_result_ignored", "text": (m.result or "")[:200]})
                    continue
                result = TurnResult(text=m.result or "", session_id=m.session_id, cost_usd=m.total_cost_usd, usage=m.usage or {})
                if m.session_id:
                    if self.store.kv_get(f"session_id:{self.key}") != m.session_id:
                        self.store.kv_set(f"session_started:{self.key}", str(time.time()))
                    self.store.kv_set(f"session_id:{self.key}", m.session_id)
                u = result.usage
                self.store.kv_set(f"ctx_tokens:{self.key}", str((u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)))
                self.seed = ""
                break
        return result

    def _start_fresh(self) -> None:
        self.store.kv_set(f"session_id:{self.key}", "")
        self.store.kv_set(f"ctx_tokens:{self.key}", "0")
        self.seed = format_seed(self.store.recent_conversation(self.key))

    def should_rotate(self, now: float | None = None) -> bool:
        ts = now if now is not None else time.time()
        ctx = int(self.store.kv_get(f"ctx_tokens:{self.key}", "0") or 0)
        started = float(self.store.kv_get(f"session_started:{self.key}", "0") or 0)
        return ctx > ROTATE_CONTEXT_TOKENS or (started > 0 and ts - started > ROTATE_AGE_S)

    async def rotate(self) -> None:
        log.warning("rotating session for %s", self.key)
        await self.close()
        self._start_fresh()

    async def compact(self) -> TurnResult:
        # /compact legitimately ends with a result and no assistant message.
        return await self.run_turn(COMPACT_INSTRUCTIONS, lambda ev: None, require_assistant=False)

    async def ping(self) -> list[str]:
        """Zero-token liveness check of the live agent process; returns MCP servers not connected."""
        if self._client is None:
            return []
        st = await self._client.get_mcp_status()
        servers = st.get("mcpServers", []) if isinstance(st, dict) else []
        return [s.get("name") for s in servers if s.get("status") not in ("connected", "disabled")]

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as e:  # noqa: BLE001
                log.debug("disconnect: %s", e)
            self._client = None


async def model_canary(model: str) -> None:
    """A real, minimal model round trip in a throwaway session (no resume, no MCP, no project
    settings): verifies auth, network and the API without touching the user's context."""
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

    opts = ClaudeAgentOptions(model=model, setting_sources=[], max_turns=1, allowed_tools=[], extra_args={"strict-mcp-config": None}, env={"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"})
    result = None
    async for m in query(prompt="Reply with exactly: OK", options=opts):  # drain fully; early exit leaves the generator open
        if isinstance(m, ResultMessage):
            result = m
    if result is None:
        raise RuntimeError("canary produced no result")
    if result.is_error or "OK" not in (result.result or ""):
        raise RuntimeError(f"unexpected canary result: {(result.result or '')[:100]!r}")


class Worker:
    def __init__(self, store: BridgeStore, session_factory: Callable[[str], AgentSession], *, turn_timeout: float = TURN_TIMEOUT_S, slow_ack_after: float = SLOW_ACK_AFTER_S, max_sessions: int = MAX_CONCURRENT_SESSIONS,
                 fastlane_router: Any = None, fastlane_db: Any = None):
        self.store = store
        self.fastlane_router = fastlane_router   # Tier 1 (fastlane_model.HaikuRouter); None = Tier 0 only
        self.fastlane_db = fastlane_db           # agent_results.db for the fast-lane services (None = the default)
        self._fl_seen: set[int] = set()          # inbox ids being triaged right now
        self._fl_locks: dict[str, asyncio.Lock] = {}  # per session: triage runs in message order
        self._fl_tasks: set[asyncio.Task] = set()
        self._fl_beat = 0.0
        self._fl_warm = False
        self.session_factory = session_factory
        self.sessions: dict[str, AgentSession] = {}
        self.turn_timeout = turn_timeout
        self.slow_ack_after = slow_ack_after
        self.stop = asyncio.Event()
        self._ping_failures: dict[str, int] = {}
        self.max_sessions = max_sessions
        self._loops: dict[str, asyncio.Task] = {}  # session_key -> its running turn loop
        self._locks: dict[str, asyncio.Lock] = {}  # session_key -> held while its client is in use

    def _lock(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    def _drop_session(self, key: str) -> None:
        self.sessions.pop(key, None)
        self.store.clear_mcp_down(key)  # a rebuilt client re-reports; don't leave a dead session's state in the union

    def _session(self, key: str) -> AgentSession:
        if key not in self.sessions:
            self.sessions[key] = self.session_factory(key)
        return self.sessions[key]

    def _reply(self, row: dict, text: str, *, kind: str = "reply", note: bool = False) -> None:
        source = None if kind == "reply" else "worker"
        self.store.enqueue_outbound(row["channel"], row["chat_ref"], text, in_reply_to=row["id"], source=source, kind=kind, note=note)

    async def process_one(self, session_key: str | None = None) -> bool:
        """Claim and run one message (of `session_key`, or the oldest of any session). False if none."""
        row = self.store.claim_next(session_key)
        if row is None:
            return False
        try:
            return await self._run_claimed(row)
        except asyncio.CancelledError:
            self._safe_requeue(row)
            raise
        except Exception as e:  # noqa: BLE001
            # Anything unexpected between claim and finish: never strand the row as 'processing'
            # (claim_next only takes 'pending'). attempts is kept, so MAX_ATTEMPTS still bounds retries.
            log.exception("message %s failed unexpectedly: %r", row["id"], e)
            self._safe_requeue(row)
            await asyncio.sleep(RETRY_BACKOFF_S)
            return True

    def _safe_requeue(self, row: dict) -> None:
        try:
            self.store.requeue(row["id"])
        except Exception as e:  # noqa: BLE001
            log.error("could not requeue message %s: %r (recover_in_flight will on restart)", row["id"], e)

    async def _run_claimed(self, row: dict) -> bool:
        key = row["session_key"]
        if row["attempts"] > MAX_ATTEMPTS:
            self.store.fail(row["id"])
            self._reply(row, "⚠️ I couldn't process that message after several attempts. Please resend it or rephrase.", kind="notice", note=True)
            return True
        resolve_reply_to(self.store, row)
        started = time.time()
        events: list[dict] = []

        def on_event(ev: dict) -> None:
            ev = {"t": datetime.now().isoformat(timespec="seconds"), "inbox_id": row["id"], "session": key, **ev}
            events.append(ev)
            _append_jsonl(STREAM_LOG, ev)

        _append_jsonl(STREAM_LOG, {"t": datetime.now().isoformat(timespec="seconds"), "inbox_id": row["id"], "session": key, "type": "turn_start", "text": row["text"][:500]})
        slow_task = asyncio.create_task(self._slow_ack(row))  # also covers waiting behind compaction
        session = None
        lock, marked, running = self._lock(key), False, False
        try:
            try:
                await asyncio.wait_for(lock.acquire(), timeout=self.turn_timeout)
            except asyncio.TimeoutError:
                raise RuntimeError(f"session {key} stayed busy for {self.turn_timeout}s") from None  # requeued by the caller
            try:
                # Marker set only now: the lock holder before us (maintenance) clears its own marker on exit.
                self.store.set_current_turn(key, row["id"], started)
                marked = True
                session = self._session(key)
                should_rotate = getattr(session, "should_rotate", None)
                if should_rotate and should_rotate():
                    await session.rotate()
                running = True
                result = await asyncio.wait_for(session.run_turn(format_turn_prompt(row), on_event), timeout=self.turn_timeout)
            finally:
                lock.release()
        except asyncio.TimeoutError:
            log.error("turn %s timed out", row["id"])
            if session is not None:
                await session.close()
            self._drop_session(key)
            self.store.complete(row["id"])
            self._reply(row, "⚠️ That took too long and I had to stop. Try again or break it into smaller steps.", kind="notice", note=True)
            _append_jsonl(STREAM_LOG, {"t": datetime.now().isoformat(timespec="seconds"), "inbox_id": row["id"], "type": "turn_timeout"})
            return True
        except asyncio.CancelledError:
            # Worker is stopping mid-turn: hand the message back for redelivery (as recover_in_flight would).
            self.store.requeue(row["id"])
            raise
        except Exception as e:  # noqa: BLE001
            # Leave the row 'processing'->pending for redelivery; rebuild the client.
            log.exception("turn %s failed: %s", row["id"], e)
            if session is not None:
                await session.close()
            self._drop_session(key)
            self.store.requeue(row["id"])
            _append_jsonl(STREAM_LOG, {"t": datetime.now().isoformat(timespec="seconds"), "inbox_id": row["id"], "type": "turn_error", "error": repr(e)})
            if not running:
                await asyncio.sleep(RETRY_BACKOFF_S)  # failed before the turn ran (e.g. database locked): don't spin
            return True
        finally:
            slow_task.cancel()
            if marked:
                self.store.clear_current_turn(key)

        # Never close a message without telling the user something.
        final = result.text if result.text.strip() else "✅ Done (I had nothing further to report)."
        self._reply(row, final)
        self.store.complete(row["id"])
        self._track_daily_cost(result.cost_usd or 0.0)
        _append_jsonl(USAGE_LOG, {"t": datetime.now().isoformat(timespec="seconds"), "session": key, "inbox_id": row["id"], "duration_s": round(time.time() - started, 1), "cost_usd": result.cost_usd, "usage": result.usage,
                                  "fastlane": {"tier": 2, "latency_ms": int((time.time() - row["received_at"]) * 1000), "reason": row.get("triage")}})
        _append_jsonl(STREAM_LOG, {"t": datetime.now().isoformat(timespec="seconds"), "inbox_id": row["id"], "type": "turn_end", "duration_s": round(time.time() - started, 1)})
        return True

    async def _session_loop(self, key: str) -> None:
        """Drain one session's queue in order, then end (the dispatcher respawns it when more arrives).
        Other sessions have their own loops, so a long turn here never blocks them."""
        try:
            while not self.stop.is_set() and self.store.kv_get("paused") != "1":
                if not await self.process_one(key):
                    return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("session loop %s crashed: %r", key, e)
            await asyncio.sleep(1)  # backoff before the dispatcher respawns it

    def _spawn_session_loops(self) -> None:
        for key in self.store.pending_session_keys():
            if len(self._loops) >= self.max_sessions:
                break
            if key in self._loops:
                continue
            task = asyncio.create_task(self._session_loop(key), name=f"session:{key}")
            self._loops[key] = task
            task.add_done_callback(lambda t, k=key: self._loops.pop(k, None) if self._loops.get(k) is t else None)

    def _track_daily_cost(self, cost: float) -> None:
        """Optional guard rail: alert once per day when API-rate cost passes BRIDGE_DAILY_COST_ALERT_USD."""
        today = datetime.now().strftime("%Y-%m-%d")
        if self.store.kv_get("cost_day") != today:
            self.store.kv_set("cost_day", today)
            self.store.kv_set("cost_today", "0")
        total = float(self.store.kv_get("cost_today", "0") or 0) + cost
        self.store.kv_set("cost_today", str(total))
        limit = float(os.environ.get("BRIDGE_DAILY_COST_ALERT_USD") or 0)
        if limit and total > limit and self.store.kv_get("cost_alerted_day") != today:
            self.store.kv_set("cost_alerted_day", today)
            self._alert(f"💸 Agent usage today is about ${total:.2f} at API rates (alert threshold ${limit:.2f}).")

    async def maintenance_once(self, now: datetime | None = None) -> bool:
        """Quiet-hours compaction with instructions that keep what matters. Compacts each big session
        that is idle (no queued or running message, nobody holding its client); a busy one is retried
        next minute while still inside the hour. Returns True once the day's pass is complete."""
        now = now or datetime.now()
        today = now.strftime("%Y-%m-%d")
        if now.hour != MAINTENANCE_HOUR or self.store.kv_get("last_maintenance") == today:
            return False
        active = self.store.active_session_keys() | set(self.store.current_turns())
        deferred = False
        for key, session in list(self.sessions.items()):
            compact = getattr(session, "compact", None)
            if compact is None or self.store.kv_get(f"last_maintenance:{key}") == today:
                continue
            if int(self.store.kv_get(f"ctx_tokens:{key}", "0") or 0) < COMPACT_MIN_TOKENS:
                continue
            if key in active or self._lock(key).locked():
                deferred = True
                continue
            self.store.kv_set(f"last_maintenance:{key}", today)
            self.store.set_current_turn(key, "maintenance", time.time())
            try:
                async with self._lock(key):
                    await asyncio.wait_for(compact(), timeout=self.turn_timeout)
                _append_jsonl(STREAM_LOG, {"t": datetime.now().isoformat(timespec="seconds"), "type": "compacted", "session": key})
            except Exception as e:  # noqa: BLE001
                log.error("maintenance compaction failed for %s: %r", key, e)
            finally:
                self.store.clear_current_turn(key)
        if deferred:
            return False
        self.store.kv_set("last_maintenance", today)
        return True

    async def maintenance_loop(self) -> None:
        while not self.stop.is_set():
            await asyncio.sleep(60)
            try:
                await self.maintenance_once()
            except Exception as e:  # noqa: BLE001
                log.error("maintenance error: %r", e)

    # ---- fast lane: answer clear short commands without an agent turn (see scripts/fastlane.py) ----
    def _fl_pass(self, row: dict, why: str) -> None:
        self.store.set_triage(row["id"], "pass:" + why)
        _append_jsonl(STREAM_LOG, {"t": datetime.now().isoformat(timespec="seconds"), "inbox_id": row["id"], "type": "fastlane_pass", "reason": why})

    async def fastlane_once(self) -> int:
        """Look at messages nobody has triaged yet. Returns how many were started. Does nothing unless the kill
        switch is on (`agent_ctl.py fastlane on`); while on, a stamp tells claim_next to wait (up to 10 s) for it."""
        on = self.store.kv_get("fastlane") == "on"
        if not on:
            if self._fl_warm and self.fastlane_router is not None:
                self._fl_warm = False
                await self.fastlane_router.close()
            return 0
        now = time.time()
        if now - self._fl_beat >= FASTLANE_BEAT_EVERY_S:
            self.store.kv_set("fastlane_beat", str(now))
            self._fl_beat = now
        if not self._fl_warm and self.fastlane_router is not None:
            self._fl_warm = True
            t = asyncio.create_task(self.fastlane_router.prewarm())
            self._fl_tasks.add(t)
            t.add_done_callback(self._fl_tasks.discard)
        if self.store.kv_get("paused") == "1":
            return 0
        started = 0
        for row in self.store.untriaged():
            if row["id"] in self._fl_seen:
                continue
            self._fl_seen.add(row["id"])
            t = asyncio.create_task(self._fastlane_row(row))
            self._fl_tasks.add(t)
            t.add_done_callback(self._fl_tasks.discard)
            started += 1
        return started

    async def fastlane_loop(self) -> None:
        while not self.stop.is_set():
            try:
                await self.fastlane_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.error("fast lane loop error: %r", e)  # never fatal: claim_next fails open after 10 s
            await asyncio.sleep(FASTLANE_POLL_S)

    async def _fastlane_row(self, row: dict) -> None:
        lock = self._fl_locks.setdefault(row["session_key"], asyncio.Lock())
        try:
            async with lock:   # one message at a time per session, in order
                try:
                    await self._fastlane_triage(row)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    log.exception("fast lane triage of %s failed: %r", row["id"], e)
                    self._fl_pass(row, "error")
        finally:
            self._fl_seen.discard(row["id"])

    async def _fastlane_triage(self, row: dict) -> None:
        now = time.time()
        reason = fastlane.eligibility(row, self.store, now=now)
        if reason:
            return self._fl_pass(row, reason)
        text = row["text"]
        ctx = fastlane.Context.load(self.fastlane_db)
        match, cost = fastlane.tier0(text, ctx), None
        if match is None:
            if self.fastlane_router is None or not fastlane.worth_model(text, ctx):
                return self._fl_pass(row, "no_match")
            res = await self.fastlane_router.route(text, ctx)
            if res.obj is None:
                return self._fl_pass(row, "model_" + (res.error or "none"))
            match, why = fastlane.check_model_output(res.obj, text, ctx)
            if match is None:
                return self._fl_pass(row, why)
            cost = res.cost_usd
            if time.time() - row["received_at"] > fastlane.MAX_AGE_S:
                return self._fl_pass(row, "late")
        why = fastlane.ordering_conflict(row, match, self.store) or fastlane.checkin_block(match, self.store, row, ctx, now)
        if why:
            return self._fl_pass(row, why)
        if not self.store.fastlane_claim(row["id"]):
            return   # the agent took it (10 s passed or the gate was off): nothing to do
        key = row["session_key"]
        started = time.time()
        stamp = datetime.now().isoformat(timespec="seconds")
        _append_jsonl(STREAM_LOG, {"t": stamp, "inbox_id": row["id"], "session": key, "type": "turn_start", "text": text[:500], "fastlane": match.action})
        try:
            reply = await asyncio.wait_for(asyncio.to_thread(fastlane.execute, match, ctx), timeout=FASTLANE_EXEC_TIMEOUT_S)
        except fastlane.PassUp as e:
            self.store.fastlane_release(row["id"], e.reason, side_effects_possible=False)
            return
        except asyncio.TimeoutError:
            reply = fastlane.Reply("That took longer than expected and I can't tell whether it went through. Please check it.", wrote=True)
        except asyncio.CancelledError:
            self.store.requeue(row["id"])   # worker stopping; the agent's redelivery carries the retry marker
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("fast action %s for %s failed: %r", match.action, row["id"], e)
            self.store.fastlane_release(row["id"], "error", side_effects_possible=True)
            return
        self.store.record_fast_exchange(row, reply.text)
        self.store.complete(row["id"])
        latency_ms = int((time.time() - row["received_at"]) * 1000)
        _append_jsonl(USAGE_LOG, fastlane.usage_record(row, tier=match.tier, action=match.action, latency_ms=latency_ms, outcome="done", cost_usd=cost))
        _append_jsonl(STREAM_LOG, {"t": datetime.now().isoformat(timespec="seconds"), "inbox_id": row["id"], "type": "turn_end", "duration_s": round(time.time() - started, 2), "fastlane": match.action})

    async def _slow_ack(self, row: dict) -> None:
        await asyncio.sleep(self.slow_ack_after)
        self._reply(row, "⏳ On it. This is taking a bit; I'll send the result when it's ready.", kind="notice")

    def _alert(self, text: str) -> None:
        # Alerts go to the household chat, never a dev chat (household_chat_id skips BRIDGE_DEV_CHAT_IDS).
        chat = os.environ.get("TELEGRAM_ALERT_CHAT_ID") or household_chat_id()
        if chat in dev_chat_ids():
            chat = household_chat_id()
        if chat:
            self.store.enqueue_outbound("telegram", chat, text, source="worker", kind="alert", note=True)

    def _record_canary(self, kind: str, ok: bool, detail: str = "") -> None:
        self.store.kv_set("last_canary", json.dumps({"t": time.time(), "kind": kind, "ok": ok, "detail": detail[:300]}))
        failing = self.store.kv_get(f"canary_failing:{kind}") == "1"
        if not ok and not failing:
            self.store.kv_set(f"canary_failing:{kind}", "1")
            self._alert(f"⚠️ Agent health check failed ({kind}): {detail[:200]}")
        elif ok and failing:
            self.store.kv_set(f"canary_failing:{kind}", "0")
            self._alert(f"✅ Agent health check recovered ({kind}).")

    async def ping_once(self) -> None:
        """Ping every live session that isn't mid-turn (a busy one is skipped, so a long turn is never
        misread as a hung client). Two straight failures rebuild that session's client."""
        busy = set(self.store.current_turns())
        failures: list[str] = []
        notes: list[str] = []
        pinged = False
        for key, session in list(self.sessions.items()):
            ping = getattr(session, "ping", None)
            lock = self._locks.get(key)
            if ping is None or key in busy or "legacy" in busy or (lock is not None and lock.locked()):
                continue
            pinged = True
            try:
                down = await asyncio.wait_for(ping(), timeout=PING_TIMEOUT_S)
                self.store.set_mcp_down(key, down)
                self._ping_failures[key] = 0
                if down:
                    notes.append(f"mcp not connected: {down}")
            except Exception as e:  # noqa: BLE001
                self._ping_failures[key] = self._ping_failures.get(key, 0) + 1
                failures.append(repr(e))
                if self._ping_failures[key] >= 2:
                    log.error("session %s unresponsive; rebuilding client", key)
                    await session.close()
                    self._drop_session(key)
                    self._ping_failures[key] = 0
        if pinged:
            # One result per pass: a hung session and a healthy one must not flip failed/recovered alerts.
            if failures:
                self._record_canary("ping", False, "; ".join(failures))
            else:
                self._record_canary("ping", True, "; ".join(notes))

    async def model_canary_once(self, fn: Callable[[], Awaitable[None]]) -> None:
        # No busy gate: the canary is a throwaway session that touches no live client, so it must keep
        # running (and can catch an auth/API outage) while a long dev or household turn is in progress.
        try:
            await asyncio.wait_for(fn(), timeout=MODEL_CANARY_TIMEOUT_S)
            self._record_canary("model", True)
        except Exception as e:  # noqa: BLE001
            self._record_canary("model", False, repr(e))

    async def canary_loop(self, model: str) -> None:
        cstore = BridgeStore(self.store.path)
        last_model = 0.0
        while not self.stop.is_set():
            await asyncio.sleep(PING_EVERY_S)
            probe = Worker(cstore, self.session_factory)  # own connection; shares live sessions below
            probe.sessions, probe._ping_failures, probe._locks = self.sessions, self._ping_failures, self._locks
            await probe.ping_once()
            if time.time() - last_model >= MODEL_CANARY_EVERY_S:
                last_model = time.time()
                await probe.model_canary_once(lambda: model_canary(model))

    async def heartbeat_loop(self) -> None:
        # Separate task and connection so long turns never look like a dead worker,
        # but a wedged event loop does.
        hb_store = BridgeStore(self.store.path)
        while not self.stop.is_set():
            hb_store.kv_set("worker_heartbeat", str(time.time()))
            sd_notify("WATCHDOG=1")
            await asyncio.sleep(HEARTBEAT_EVERY_S)

    async def run(self, model: str = "sonnet") -> None:
        recovered = self.store.recover_in_flight()
        if recovered:
            log.warning("recovered %d in-flight message(s) from a previous crash", recovered)
        self.store.clear_all_current_turns()  # also drops the pre-concurrency single `current_turn` value
        self.store.clear_mcp_down()  # sessions re-report as they connect; stale keys must not pin the union
        hb = asyncio.create_task(self.heartbeat_loop())
        canary = asyncio.create_task(self.canary_loop(model))
        maint = asyncio.create_task(self.maintenance_loop())
        fl = asyncio.create_task(self.fastlane_loop())
        sd_notify("READY=1")
        try:
            # Dispatcher: a loop per session with pending work, so a long turn in one session never
            # blocks another. Idle sessions have no task at all; nothing spins.
            while not self.stop.is_set():
                if self.store.kv_get("paused") != "1":
                    self._spawn_session_loops()
                await asyncio.sleep(0.3)
        finally:
            hb.cancel()
            canary.cancel()
            maint.cancel()
            fl.cancel()
            for t in list(self._fl_tasks):
                t.cancel()
            if self.fastlane_router is not None:
                await self.fastlane_router.close()
            loops = list(self._loops.values())
            if loops:
                _, pending = await asyncio.wait(loops, timeout=STOP_GRACE_S)  # let running turns finish briefly
                for t in pending:
                    t.cancel()  # process_one requeues its message, so it is redelivered on restart
                if pending:
                    await asyncio.wait(pending, timeout=STOP_CANCEL_WAIT_S)
            if self.sessions:
                await asyncio.wait([asyncio.ensure_future(s.close()) for s in self.sessions.values()], timeout=STOP_CLOSE_WAIT_S)
            for key in list(self.sessions):
                self.store.clear_mcp_down(key)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(REPO_ROOT / "state" / "inbox.db"))
    ap.add_argument("--model", default="sonnet")  # CLAUDE.md model policy: Sonnet by default
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    store = BridgeStore(args.db)
    import fastlane_model  # noqa: E402  (Tier 1 router; only connects when the fast lane is switched on)

    worker = Worker(store, lambda key: SdkSession(key, store, args.model), fastlane_router=fastlane_model.HaikuRouter())

    async def runner() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, worker.stop.set)
        await worker.run(args.model)

    asyncio.run(runner())
    return 0


if __name__ == "__main__":
    sys.exit(main())
