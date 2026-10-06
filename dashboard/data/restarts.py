"""Orchestrator restart history, assembled from the jsonl launch log, the exit log
and the watchdog logs (read-only).

Only coarse reasons are recorded structurally (wrapper-start / loop-restart), so the
"why" is inferred by correlating timestamps. The UI labels the cause as inferred.
"""
import json
import re
from datetime import datetime, timedelta, timezone

from . import common

EXIT_RE = re.compile(r"^\[(?P<ts>[\d\- :]+)\] session=(?P<session>\S+) exit_code=(?P<code>-?\d+)")
PASS_RE = re.compile(r"^\[(?P<ts>[\d\- :]+)\] --- watchdog pass ---")
SIGNAL_RE = re.compile(r"unhealthy signal seen via (?P<source>.+?),? starting")
CORRELATION_WINDOW = timedelta(minutes=5)

EXIT_CODE_NOTES = {137: "SIGKILL", 143: "SIGTERM", 130: "SIGINT"}


def _local_naive_to_aware(text: str):
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d %H:%M:%S").astimezone()
    except ValueError:
        return None


def _read_lines(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except FileNotFoundError:
        return []


def _load_launches():
    launches = []
    for line in _read_lines(common.state_dir() / "orchestrator_restarts.jsonl"):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = common.parse_ts(row.get("ts"))
        if ts:
            launches.append({"ts": ts, "session": row.get("session_name"),
                             "reason": row.get("reason"), "raw": line})
    launches.sort(key=lambda x: x["ts"])
    return launches


def _load_exits():
    """Map session name -> {ts, code, raw}. The latest exit line per session wins."""
    exits = {}
    for line in _read_lines(common.state_dir() / "logs" / "orchestrator_exits.log"):
        m = EXIT_RE.match(line)
        if not m:
            continue
        ts = _local_naive_to_aware(m["ts"])
        if ts:
            exits[m["session"]] = {"ts": ts, "code": int(m["code"]), "raw": line}
    return exits


def _load_watchdog_kills():
    """Each confirmed watchdog kill with the signal source and surrounding log lines.

    Log lines inside a pass carry no timestamp of their own, so they take the time of
    the most recent '--- watchdog pass ---' header."""
    kills = []
    logs_dir = common.state_dir() / "logs"
    if not logs_dir.exists():
        return kills
    for path in sorted(logs_dir.glob("watchdog_*.log")):
        pass_ts = None
        pass_lines: list[str] = []
        signal_source = None
        for line in _read_lines(path):
            m = PASS_RE.match(line)
            if m:
                pass_ts = _local_naive_to_aware(m["ts"])
                pass_lines = [line]
                continue
            pass_lines.append(line)
            s = SIGNAL_RE.search(line)
            if s:
                signal_source = s["source"]
            if "killed orchestrator" in line and pass_ts:
                kills.append({"ts": pass_ts, "source": signal_source,
                              "lines": list(pass_lines), "log": path.name})
                signal_source = None
    return kills


def _cause(launch, exit_info, kill):
    if kill:
        source = f", detected via {kill['source']}" if kill["source"] else ""
        return "watchdog", f"Watchdog killed it: Telegram connection unhealthy{source}"
    if launch["reason"] == "wrapper-start":
        return "wrapper", "Wrapper start (boot or manual restart)"
    if exit_info is None:
        return "unknown", "Unknown (no exit record found)"
    code = exit_info["code"]
    if code == 137:
        return "killed", "Killed externally (SIGKILL), no watchdog record"
    if code == 0:
        return "clean", "Clean exit (code 0)"
    return "crash", f"Crash/exit (code {code})"


def get_restart_history(limit: int = 100):
    launches = _load_launches()
    if not launches:
        return {"error": "no restart log found or it is empty", "events": [],
                "summary": {}}

    exits = _load_exits()
    kills = _load_watchdog_kills()
    events = []
    for i, launch in enumerate(launches):
        prev = launches[i - 1] if i else None
        exit_info = exits.get(prev["session"]) if prev else None
        kill = None
        if exit_info:
            near = [k for k in kills if abs(k["ts"] - exit_info["ts"]) <= CORRELATION_WINDOW]
            kill = min(near, key=lambda k: abs(k["ts"] - exit_info["ts"])) if near else None
        cause_kind, cause_text = _cause(launch, exit_info, kill)
        downtime = None
        if exit_info:
            downtime = max((launch["ts"] - exit_info["ts"]).total_seconds(), 0)
        code = exit_info["code"] if exit_info else None
        events.append({
            "ts": launch["ts"].astimezone().isoformat(timespec="seconds"),
            "session": launch["session"],
            "launch_type": launch["reason"],
            "exit_code": code,
            "exit_code_note": EXIT_CODE_NOTES.get(code) if code is not None else None,
            "cause_kind": cause_kind,
            "cause": cause_text,
            "downtime_seconds": downtime,
            "previous_session": prev["session"] if prev else None,
            "raw_launch": launch["raw"],
            "raw_exit": exit_info["raw"] if exit_info else None,
            "watchdog_lines": kill["lines"] if kill else [],
            "watchdog_log": kill["log"] if kill else None,
        })
    events.reverse()  # newest first

    current = common.now()
    in_24h = sum(1 for l in launches if current - l["ts"] <= timedelta(hours=24))
    in_7d = sum(1 for l in launches if current - l["ts"] <= timedelta(days=7))
    summary = {
        "total": len(launches),
        "last_24h": in_24h,
        "last_7d": in_7d,
        "wrapper_starts": sum(1 for l in launches if l["reason"] == "wrapper-start"),
        "loop_restarts": sum(1 for l in launches if l["reason"] == "loop-restart"),
        "watchdog_caused": sum(1 for e in events if e["cause_kind"] == "watchdog"),
    }
    return {"summary": summary, "events": events[:limit]}
