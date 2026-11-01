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

from bridge_store import BridgeStore  # noqa: E402
from paths import REPO_ROOT  # noqa: E402

log = logging.getLogger("worker")

STREAM_LOG = REPO_ROOT / "state" / "logs" / "agent_stream.jsonl"
USAGE_LOG = REPO_ROOT / "state" / "usage.jsonl"
TURN_TIMEOUT_S = 900
SLOW_ACK_AFTER_S = 20
MAX_ATTEMPTS = 3
MCP_READY_TIMEOUT_S = 90
HEARTBEAT_EVERY_S = 2
PING_EVERY_S = 30
MODEL_CANARY_EVERY_S = 1800
PING_TIMEOUT_S = 20
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
            system_prompt={"type": "preset", "preset": "claude_code", "append": CHANNEL_PROMPT},
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
                self.store.kv_set("mcp_down", json.dumps(bad))
                self.mcp_ready = True
                return
            await asyncio.sleep(1)
        log.warning("MCP servers still pending after %ss: proceeding anyway", MCP_READY_TIMEOUT_S)
        self.store.kv_set("mcp_down", json.dumps(["timeout"]))

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
    def __init__(self, store: BridgeStore, session_factory: Callable[[str], AgentSession], *, turn_timeout: float = TURN_TIMEOUT_S, slow_ack_after: float = SLOW_ACK_AFTER_S):
        self.store = store
        self.session_factory = session_factory
        self.sessions: dict[str, AgentSession] = {}
        self.turn_timeout = turn_timeout
        self.slow_ack_after = slow_ack_after
        self.stop = asyncio.Event()
        self._ping_failures: dict[str, int] = {}

    def _session(self, key: str) -> AgentSession:
        if key not in self.sessions:
            self.sessions[key] = self.session_factory(key)
        return self.sessions[key]

    def _reply(self, row: dict, text: str, *, kind: str = "reply", note: bool = False) -> None:
        source = None if kind == "reply" else "worker"
        self.store.enqueue_outbound(row["channel"], row["chat_ref"], text, in_reply_to=row["id"], source=source, kind=kind, note=note)

    async def process_one(self) -> bool:
        row = self.store.claim_next()
        if row is None:
            return False
        if row["attempts"] > MAX_ATTEMPTS:
            self.store.fail(row["id"])
            self._reply(row, "⚠️ I couldn't process that message after several attempts. Please resend it or rephrase.", kind="notice", note=True)
            return True
        resolve_reply_to(self.store, row)
        session = self._session(row["session_key"])
        should_rotate = getattr(session, "should_rotate", None)
        if should_rotate and should_rotate():
            await session.rotate()
        started = time.time()
        events: list[dict] = []

        def on_event(ev: dict) -> None:
            ev = {"t": datetime.now().isoformat(timespec="seconds"), "inbox_id": row["id"], "session": row["session_key"], **ev}
            events.append(ev)
            _append_jsonl(STREAM_LOG, ev)

        _append_jsonl(STREAM_LOG, {"t": datetime.now().isoformat(timespec="seconds"), "inbox_id": row["id"], "type": "turn_start", "text": row["text"][:500]})
        self.store.kv_set("current_turn", json.dumps({"inbox_id": row["id"], "started": started}))
        slow_task = asyncio.create_task(self._slow_ack(row))
        try:
            result = await asyncio.wait_for(session.run_turn(format_turn_prompt(row), on_event), timeout=self.turn_timeout)
        except asyncio.TimeoutError:
            log.error("turn %s timed out", row["id"])
            await session.close()
            self.sessions.pop(row["session_key"], None)
            self.store.complete(row["id"])
            self._reply(row, "⚠️ That took too long and I had to stop. Try again or break it into smaller steps.", kind="notice", note=True)
            _append_jsonl(STREAM_LOG, {"t": datetime.now().isoformat(timespec="seconds"), "inbox_id": row["id"], "type": "turn_timeout"})
            return True
        except Exception as e:  # noqa: BLE001
            # Leave the row 'processing'->pending for redelivery; rebuild the client.
            log.exception("turn %s failed: %s", row["id"], e)
            await session.close()
            self.sessions.pop(row["session_key"], None)
            self.store.requeue(row["id"])
            _append_jsonl(STREAM_LOG, {"t": datetime.now().isoformat(timespec="seconds"), "inbox_id": row["id"], "type": "turn_error", "error": repr(e)})
            return True
        finally:
            slow_task.cancel()
            self.store.kv_set("current_turn", "")

        # Never close a message without telling the user something.
        final = result.text if result.text.strip() else "✅ Done (I had nothing further to report)."
        self._reply(row, final)
        self.store.complete(row["id"])
        self._track_daily_cost(result.cost_usd or 0.0)
        _append_jsonl(USAGE_LOG, {"t": datetime.now().isoformat(timespec="seconds"), "session": row["session_key"], "inbox_id": row["id"], "duration_s": round(time.time() - started, 1), "cost_usd": result.cost_usd, "usage": result.usage})
        _append_jsonl(STREAM_LOG, {"t": datetime.now().isoformat(timespec="seconds"), "inbox_id": row["id"], "type": "turn_end", "duration_s": round(time.time() - started, 1)})
        return True

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
        """Quiet-hours compaction with instructions that keep what matters. Returns True if it ran."""
        now = now or datetime.now()
        today = now.strftime("%Y-%m-%d")
        if now.hour != MAINTENANCE_HOUR or self.store.kv_get("last_maintenance") == today:
            return False
        stats = self.store.stats()
        if stats["inbox_pending"] or stats["inbox_processing"] or self.store.kv_get("current_turn"):
            return False  # try again next minute while still inside the hour
        self.store.kv_set("last_maintenance", today)
        for key, session in list(self.sessions.items()):
            compact = getattr(session, "compact", None)
            if compact is None or int(self.store.kv_get(f"ctx_tokens:{key}", "0") or 0) < COMPACT_MIN_TOKENS:
                continue
            self.store.kv_set("current_turn", json.dumps({"inbox_id": "maintenance", "started": time.time()}))
            try:
                await asyncio.wait_for(compact(), timeout=self.turn_timeout)
                _append_jsonl(STREAM_LOG, {"t": datetime.now().isoformat(timespec="seconds"), "type": "compacted", "session": key})
            except Exception as e:  # noqa: BLE001
                log.error("maintenance compaction failed for %s: %r", key, e)
            finally:
                self.store.kv_set("current_turn", "")
        return True

    async def maintenance_loop(self) -> None:
        while not self.stop.is_set():
            await asyncio.sleep(60)
            try:
                await self.maintenance_once()
            except Exception as e:  # noqa: BLE001
                log.error("maintenance error: %r", e)

    async def _slow_ack(self, row: dict) -> None:
        await asyncio.sleep(self.slow_ack_after)
        self._reply(row, "⏳ On it. This is taking a bit; I'll send the result when it's ready.", kind="notice")

    def _alert(self, text: str) -> None:
        chat = os.environ.get("TELEGRAM_ALERT_CHAT_ID") or (os.environ.get("BRIDGE_ALLOWED_CHAT_IDS", "").split(",")[0].strip())
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
        """Ping every live session that isn't mid-turn. Two straight failures rebuild the client."""
        if self.store.kv_get("current_turn"):
            return
        for key, session in list(self.sessions.items()):
            ping = getattr(session, "ping", None)
            if ping is None:
                continue
            try:
                down = await asyncio.wait_for(ping(), timeout=PING_TIMEOUT_S)
                self.store.kv_set("mcp_down", json.dumps(down))
                self._ping_failures[key] = 0
                self._record_canary("ping", True, f"mcp not connected: {down}" if down else "")
            except Exception as e:  # noqa: BLE001
                self._ping_failures[key] = self._ping_failures.get(key, 0) + 1
                self._record_canary("ping", False, repr(e))
                if self._ping_failures[key] >= 2:
                    log.error("session %s unresponsive; rebuilding client", key)
                    await session.close()
                    self.sessions.pop(key, None)
                    self._ping_failures[key] = 0

    async def model_canary_once(self, fn: Callable[[], Awaitable[None]]) -> None:
        if self.store.kv_get("current_turn"):
            return
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
            probe.sessions, probe._ping_failures = self.sessions, self._ping_failures
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
        hb = asyncio.create_task(self.heartbeat_loop())
        canary = asyncio.create_task(self.canary_loop(model))
        maint = asyncio.create_task(self.maintenance_loop())
        sd_notify("READY=1")
        try:
            while not self.stop.is_set():
                if self.store.kv_get("paused") == "1" or not await self.process_one():
                    await asyncio.sleep(0.3)
        finally:
            hb.cancel()
            canary.cancel()
            maint.cancel()
            for s in self.sessions.values():
                await s.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(REPO_ROOT / "state" / "inbox.db"))
    ap.add_argument("--model", default="sonnet")  # CLAUDE.md model policy: Sonnet by default
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    store = BridgeStore(args.db)
    worker = Worker(store, lambda key: SdkSession(key, store, args.model))

    async def runner() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, worker.stop.set)
        await worker.run(args.model)

    asyncio.run(runner())
    return 0


if __name__ == "__main__":
    sys.exit(main())
