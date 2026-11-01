#!/usr/bin/env python3
"""Operate and observe the Telegram bridge + agent worker.

    agent_ctl.py status [--json]      liveness, queue depth, current turn, MCP problems
    agent_ctl.py pause | resume       worker finishes its turn then stops/starts taking messages
                                      (pause before `claude --resume <session_id>` by hand)
    agent_ctl.py say "text" [--as CHAT_ID] [--timeout 120]
                                      send a message from the "terminal" channel and print the reply
    agent_ctl.py tail                 follow the live agent event stream (read-only)
    agent_ctl.py sessions             session ids the worker resumes
    agent_ctl.py drill worker-kill|bridge-kill|hang|worker-stop|worker-start
                                      induce a failure to verify detection and recovery
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bridge_store import BridgeStore  # noqa: E402
from paths import REPO_ROOT, get_env  # noqa: E402

DB = REPO_ROOT / "state" / "inbox.db"
STREAM_LOG = REPO_ROOT / "state" / "logs" / "agent_stream.jsonl"
BRIDGE_UNIT = "assistant-bridge"
WORKER_UNIT = "assistant-agent-worker"


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def cmd_status(args) -> int:
    store = BridgeStore(DB)
    snap = store.health_snapshot()
    snap["units"] = {u: _systemctl("is-active", u).stdout.strip() or "unknown" for u in (BRIDGE_UNIT, WORKER_UNIT)}
    if args.json:
        print(json.dumps(snap, indent=2))
        return 0
    mark = lambda ok: "up" if ok else "DOWN"  # noqa: E731
    print(f"bridge : {mark(snap['bridge_up'])} (heartbeat {snap['bridge_heartbeat_age_s']}s ago, unit {snap['units'][BRIDGE_UNIT]})")
    if snap["bridge_up"] and not snap["polling_ok"]:
        print("         (bridge is alive but Telegram polling is failing)")
    print(f"worker : {mark(snap['worker_up'])}{' [PAUSED]' if snap['paused'] else ''} (heartbeat {snap['worker_heartbeat_age_s']}s ago, unit {snap['units'][WORKER_UNIT]})")
    print(f"queue  : {snap['inbox_pending']} pending, {snap['inbox_processing']} in progress, oldest {snap['oldest_pending_age_s']}")
    print(f"outbox : {snap['outbox_pending']} pending, {snap['outbox_failed']} failed")
    if snap["current_turn"]:
        print(f"turn   : message {snap['current_turn']['inbox_id']} running {int(time.time() - snap['current_turn']['started'])}s")
    token_env = _effective_token_env()
    print(f"token  : {token_env} ({'PRODUCTION bot' if token_env == 'TELEGRAM_BOT_TOKEN' else 'test bot'})")
    if snap["mcp_down"]:
        print(f"mcp    : not connected: {', '.join(snap['mcp_down'])}")
    return 0 if snap["bridge_up"] and snap["worker_up"] else 1


def cmd_pause(args) -> int:
    BridgeStore(DB).kv_set("paused", "1" if args.cmd == "pause" else "0")
    print("worker paused (finishes current turn)" if args.cmd == "pause" else "worker resumed")
    return 0


def cmd_say(args) -> int:
    store = BridgeStore(DB)
    chat = args.chat or (get_env("BRIDGE_ALLOWED_CHAT_IDS") or "").split(",")[0].strip()
    if not chat:
        print("no --as CHAT_ID and BRIDGE_ALLOWED_CHAT_IDS is empty", file=sys.stderr)
        return 2
    inbox_id = store.add_inbound("terminal", "terminal", args.text, external_id=f"say-{time.time_ns()}", session_key=chat, payload={"user": "terminal", "message_id": ""})
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        row = store._conn.execute("SELECT * FROM outbox WHERE channel='terminal' AND status='pending' AND in_reply_to=? ORDER BY id LIMIT 1", (inbox_id,)).fetchone()
        if row:
            print(row["text"])
            store.mark_sent(row["id"])
            if "On it" in row["text"] and row["text"].startswith("⏳"):
                continue  # interim notice; keep waiting for the real reply
            return 0
        time.sleep(0.5)
    print("(timed out waiting for a reply)", file=sys.stderr)
    return 1


def cmd_tail(_args) -> int:
    STREAM_LOG.parent.mkdir(parents=True, exist_ok=True)
    STREAM_LOG.touch()
    with STREAM_LOG.open() as f:
        f.seek(0, 2)
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.3)
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            body = ev.get("text") or ev.get("name") or ev.get("error") or ""
            if ev.get("type") == "tool_use":
                body = f"{ev['name']} {json.dumps(ev.get('input'))[:200]}"
            print(f"{ev.get('t', '')[11:]} #{ev.get('inbox_id')} {ev.get('type'):<12} {str(body)[:300]}", flush=True)


def cmd_sessions(_args) -> int:
    store = BridgeStore(DB)
    for r in store._conn.execute("SELECT key, value FROM kv WHERE key LIKE 'session_id:%'"):
        print(f"{r['key'].split(':', 1)[1]}  {r['value']}")
    return 0


def _effective_token_env() -> str:
    """What the bridge unit really uses: .env override, else the unit's own Environment default."""
    override = get_env("BRIDGE_TOKEN_ENV")
    if override:
        return override
    env = _systemctl("show", BRIDGE_UNIT, "-p", "Environment", "--value").stdout
    for part in env.split():
        if part.startswith("BRIDGE_TOKEN_ENV="):
            return part.split("=", 1)[1]
    return "TELEGRAM_TEST_BOT_TOKEN"


def cmd_drill(args) -> int:
    d = args.drill
    if d == "worker-kill":
        _systemctl("kill", "-s", "KILL", WORKER_UNIT)
    elif d == "bridge-kill":
        _systemctl("kill", "-s", "KILL", BRIDGE_UNIT)
    elif d == "hang":
        _systemctl("kill", "-s", "STOP", WORKER_UNIT)  # systemd WatchdogSec should kill+restart it
    elif d == "worker-stop":
        _systemctl("stop", WORKER_UNIT)
    elif d == "worker-start":
        _systemctl("start", WORKER_UNIT)
    print(f"drill '{d}' triggered at {time.strftime('%H:%M:%S')}; watch `agent_ctl.py status` and the dashboard")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)
    for name in ("pause", "resume"):
        sub.add_parser(name).set_defaults(fn=cmd_pause)
    p = sub.add_parser("say")
    p.add_argument("text")
    p.add_argument("--as", dest="chat")
    p.add_argument("--timeout", type=float, default=120)
    p.set_defaults(fn=cmd_say)
    sub.add_parser("tail").set_defaults(fn=cmd_tail)
    sub.add_parser("sessions").set_defaults(fn=cmd_sessions)
    p = sub.add_parser("drill")
    p.add_argument("drill", choices=["worker-kill", "bridge-kill", "hang", "worker-stop", "worker-start"])
    p.set_defaults(fn=cmd_drill)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
