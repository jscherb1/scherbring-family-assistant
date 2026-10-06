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

CHANNEL_PROMPT = """\
You are running behind a message bridge, not the Telegram plugin. Overrides to anything in CLAUDE.md about Telegram:
- Your final assistant message is delivered to the user automatically on the channel the message came from. Do NOT call any Telegram reply/react/edit tool and do NOT run scripts/telegram_send.py for normal replies.
- Acknowledgements (reaction, typing, a "working on it" notice for slow turns) are sent by the bridge automatically. Do not send your own.
- Never use AskUserQuestion; if you need an answer, just ask in your final message and the user's reply will arrive as the next message.
- Messages arrive wrapped in a <channel ...> tag with chat/user/time metadata; "[Earlier notes]" blocks are context (e.g. results of scheduled tasks already delivered to the user), not requests.
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
        parts.append("[Earlier notes]\n" + "\n".join(lines) + "\n[/Earlier notes]")
    if row["attempts"] > 1:
        parts.append(f"[Retry #{row['attempts']}: a previous attempt at this message did not finish; it may have partially executed.]")
    ts = datetime.fromtimestamp(p.get("date") or row["received_at"]).isoformat(timespec="seconds")
    attrs = f'source="{row["channel"]}" chat_id="{row["chat_ref"]}" message_id="{p.get("message_id", "")}" user="{p.get("user", "")}" ts="{ts}"'
    body = row["text"]
    for att in p.get("attachments", []):
        if att.get("path"):
            body += f"\n[attachment {att['kind']}: {att['path']}]"
    parts.append(f"<channel {attrs}>\n{body}\n</channel>")
    return "\n\n".join(parts)


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

    def _options(self):
        from claude_agent_sdk import ClaudeAgentOptions

        resume = self.store.kv_get(f"session_id:{self.key}")
        return ClaudeAgentOptions(
            model=self.model,
            cwd=str(REPO_ROOT),
            setting_sources=["project"],
            permission_mode="auto",
            system_prompt={"type": "preset", "preset": "claude_code", "append": CHANNEL_PROMPT},
            resume=resume,
            # A background task outlives its turn and its completion has nowhere to be
            # delivered here; make the agent run things in the foreground instead.
            env={"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1", "BRIDGE_WORKER": "1"},
        )

    async def ensure_ready(self) -> None:
        from claude_agent_sdk import ClaudeSDKClient

        if self._client is None:
            self._client = ClaudeSDKClient(options=self._options())
            await self._client.connect()
            self.mcp_ready = False
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

    async def run_turn(self, prompt: str, on_event: Callable[[dict], None]) -> TurnResult:
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock, UserMessage

        await self.ensure_ready()
        await self._client.query(prompt)
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
                if not seen_assistant:
                    on_event({"type": "stale_result_ignored", "text": (m.result or "")[:200]})
                    continue
                result = TurnResult(text=m.result or "", session_id=m.session_id, cost_usd=m.total_cost_usd, usage=m.usage or {})
                if m.session_id:
                    self.store.kv_set(f"session_id:{self.key}", m.session_id)
                break
        return result

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as e:  # noqa: BLE001
                log.debug("disconnect: %s", e)
            self._client = None


class Worker:
    def __init__(self, store: BridgeStore, session_factory: Callable[[str], AgentSession], *, turn_timeout: float = TURN_TIMEOUT_S, slow_ack_after: float = SLOW_ACK_AFTER_S):
        self.store = store
        self.session_factory = session_factory
        self.sessions: dict[str, AgentSession] = {}
        self.turn_timeout = turn_timeout
        self.slow_ack_after = slow_ack_after
        self.stop = asyncio.Event()

    def _session(self, key: str) -> AgentSession:
        if key not in self.sessions:
            self.sessions[key] = self.session_factory(key)
        return self.sessions[key]

    def _reply(self, row: dict, text: str) -> None:
        self.store.enqueue_outbound(row["channel"], row["chat_ref"], text, in_reply_to=row["id"])

    async def process_one(self) -> bool:
        row = self.store.claim_next()
        if row is None:
            return False
        if row["attempts"] > MAX_ATTEMPTS:
            self.store.fail(row["id"])
            self._reply(row, "⚠️ I couldn't process that message after several attempts. Please resend it or rephrase.")
            return True
        session = self._session(row["session_key"])
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
            self._reply(row, "⚠️ That took too long and I had to stop. Try again or break it into smaller steps.")
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
        self._reply(row, result.text if result.text.strip() else "✅ Done (I had nothing further to report).")
        self.store.complete(row["id"])
        _append_jsonl(USAGE_LOG, {"t": datetime.now().isoformat(timespec="seconds"), "session": row["session_key"], "inbox_id": row["id"], "duration_s": round(time.time() - started, 1), "cost_usd": result.cost_usd, "usage": result.usage})
        _append_jsonl(STREAM_LOG, {"t": datetime.now().isoformat(timespec="seconds"), "inbox_id": row["id"], "type": "turn_end", "duration_s": round(time.time() - started, 1)})
        return True

    async def _slow_ack(self, row: dict) -> None:
        await asyncio.sleep(self.slow_ack_after)
        self._reply(row, "⏳ On it. This is taking a bit; I'll send the result when it's ready.")

    async def heartbeat_loop(self) -> None:
        # Separate task and connection so long turns never look like a dead worker,
        # but a wedged event loop does.
        hb_store = BridgeStore(self.store.path)
        while not self.stop.is_set():
            hb_store.kv_set("worker_heartbeat", str(time.time()))
            sd_notify("WATCHDOG=1")
            await asyncio.sleep(HEARTBEAT_EVERY_S)

    async def run(self) -> None:
        recovered = self.store.recover_in_flight()
        if recovered:
            log.warning("recovered %d in-flight message(s) from a previous crash", recovered)
        hb = asyncio.create_task(self.heartbeat_loop())
        sd_notify("READY=1")
        try:
            while not self.stop.is_set():
                if self.store.kv_get("paused") == "1" or not await self.process_one():
                    await asyncio.sleep(0.3)
        finally:
            hb.cancel()
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
        await worker.run()

    asyncio.run(runner())
    return 0


if __name__ == "__main__":
    sys.exit(main())
