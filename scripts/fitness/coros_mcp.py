"""Client for COROS's official MCP server (https://mcpus.coros.com/mcp), used for sleep, steps and stress.

Auth is OAuth 2.1 (public client, PKCE). One browser sign-in stores a long-lived refresh token in
state/coros_mcp_token.json (git-ignored); this module renews the access token on its own. Set it up with:

    python scripts/fitness/coros_mcp.py auth-url                  # open the printed link, approve, copy the
    python scripts/fitness/coros_mcp.py auth-finish "<callback>"  # localhost/callback?code=... address

Responses are text reports, not JSON; the parse_* helpers turn them into {YYYY-MM-DD: {field: number}}.
Stdlib only. Raises CorosMcpError (code 1 = needs sign-in, 2 = request failed).
"""
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKEN_FILE = REPO_ROOT / "state" / "coros_mcp_token.json"
PENDING_FILE = REPO_ROOT / "state" / "coros_mcp_pending.json"
BASE = "https://mcpus.coros.com"
MCP_URL = f"{BASE}/mcp"
REDIRECT_URI = "http://localhost/callback"
SCOPE = "openid mcp.tools offline_access"
REFRESH_MARGIN = 24 * 3600  # renew when less than a day is left


class CorosMcpError(Exception):
    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------

def _post_form(url: str, data: dict) -> dict:
    req = urllib.request.Request(url, urllib.parse.urlencode(data).encode(),
                                 {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise CorosMcpError(f"{url} -> HTTP {e.code}: {e.read().decode()[:200]}", 1 if e.code in (400, 401) else 2)
    except urllib.error.URLError as e:
        raise CorosMcpError(f"{url} unreachable: {e.reason}", 2)


def _load_token() -> dict:
    if not TOKEN_FILE.exists():
        raise CorosMcpError("COROS MCP not signed in; run: python scripts/fitness/coros_mcp.py auth-url", 1)
    return json.loads(TOKEN_FILE.read_text())


def _save_token(tok: dict) -> None:
    TOKEN_FILE.write_text(json.dumps(tok))
    TOKEN_FILE.chmod(0o600)


def access_token() -> str:
    tok = _load_token()
    if time.time() > tok["obtained_at"] + tok["expires_in"] - REFRESH_MARGIN:
        if not tok.get("refresh_token"):
            raise CorosMcpError("COROS MCP token expired and there is no refresh token; sign in again", 1)
        new = _post_form(f"{BASE}/oauth2/token", {"grant_type": "refresh_token", "client_id": tok["client_id"],
                                                  "refresh_token": tok["refresh_token"]})
        tok = {**tok, **new, "obtained_at": time.time()}
        tok.setdefault("refresh_token", new.get("refresh_token") or tok.get("refresh_token"))
        _save_token(tok)
    return tok["access_token"]


def auth_url() -> str:
    """Register a client, store PKCE state, and return the browser sign-in link."""
    import base64
    import hashlib
    import secrets
    req = urllib.request.Request(f"{BASE}/oauth2/register", json.dumps({
        "client_name": "scherbring-family-assistant", "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "token_endpoint_auth_method": "none", "scope": SCOPE}).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        client_id = json.loads(r.read())["client_id"]
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    PENDING_FILE.write_text(json.dumps({"verifier": verifier, "state": state, "client_id": client_id,
                                        "redirect_uri": REDIRECT_URI}))
    PENDING_FILE.chmod(0o600)
    return f"{BASE}/oauth2/authorize?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT_URI, "scope": SCOPE,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state})


def auth_finish(callback_url: str) -> None:
    pending = json.loads(PENDING_FILE.read_text())
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(callback_url).query)
    if qs.get("state", [None])[0] != pending["state"] or "code" not in qs:
        raise CorosMcpError("callback address has no code or the state does not match", 2)
    tok = _post_form(f"{BASE}/oauth2/token", {
        "grant_type": "authorization_code", "code": qs["code"][0], "redirect_uri": pending["redirect_uri"],
        "client_id": pending["client_id"], "code_verifier": pending["verifier"]})
    _save_token({**tok, "obtained_at": time.time(), "client_id": pending["client_id"]})


# ---------------------------------------------------------------------------
# MCP calls
# ---------------------------------------------------------------------------

def _rpc(token: str, body: dict, session: str | None = None):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if session:
        headers["Mcp-Session-Id"] = session
    try:
        with urllib.request.urlopen(urllib.request.Request(MCP_URL, json.dumps(body).encode(), headers),
                                    timeout=60) as r:
            session = r.headers.get("Mcp-Session-Id", session)
            text = r.read().decode()
    except urllib.error.HTTPError as e:
        raise CorosMcpError(f"COROS MCP HTTP {e.code}: {e.read().decode()[:200]}", 1 if e.code == 401 else 2)
    except urllib.error.URLError as e:
        raise CorosMcpError(f"COROS MCP unreachable: {e.reason}", 2)
    for line in text.splitlines():  # reply is plain JSON or an SSE "data:" line
        if line.startswith("data:"):
            text = line[5:].strip()
    return (json.loads(text) if text.strip() else {}), session


def call_tools(calls: list[tuple[str, dict]]) -> list[str]:
    """Run several tool calls in one MCP session; return each tool's text output."""
    token = access_token()
    _, session = _rpc(token, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "scherbring-family-assistant", "version": "1"}}})
    _rpc(token, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
    out = []
    for i, (name, args) in enumerate(calls, start=2):
        reply, _ = _rpc(token, {"jsonrpc": "2.0", "id": i, "method": "tools/call",
                                "params": {"name": name, "arguments": args}}, session)
        if "error" in reply or reply.get("result", {}).get("isError"):
            raise CorosMcpError(f"{name} failed: {json.dumps(reply)[:300]}", 2)
        text = "\n".join(c.get("text", "") for c in reply["result"]["content"])
        if text.startswith('"'):  # the server returns the report as a JSON-encoded string
            text = json.loads(text)
        out.append(text)
    return out


# ---------------------------------------------------------------------------
# parsers (text report -> {YYYY-MM-DD: {field: number}})
# ---------------------------------------------------------------------------

def _minutes(text: str) -> float | None:
    m = re.search(r"(?:(\d+)h)?\s*(?:(\d+)\s*min)?", text.strip())
    if not m or not (m.group(1) or m.group(2)):
        return None
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


def _clock(hhmm: str) -> float:
    h, m = hhmm.split(":")
    return int(h) + int(m) / 60


def parse_sleep_overview(text: str) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for block in re.split(r"\n(?=\d{4}-\d{2}-\d{2}\n)", text):
        head = re.match(r"(\d{4}-\d{2}-\d{2})\n", block)
        if not head:
            continue
        f = dict(re.findall(r"^([^:\n]+): (.+)$", block, flags=re.M))
        row: dict[str, float] = {}
        if "Sleep Score" in f:
            row["sleep_score"] = float(f["Sleep Score"])
        mins = _minutes(f.get("Main Sleep (asleep)", ""))
        if mins:
            row["sleep_hours"] = round(mins / 60, 2)
        for key, name in (("Deep Sleep Ratio", "deep_pct"), ("REM Ratio", "rem_pct"), ("Light Sleep Ratio", "light_pct")):
            m = re.match(r"(\d+)%", f.get(key, ""))
            if m and row.get("sleep_hours"):
                row[name] = float(m.group(1))
        awake = _minutes(f.get("Awake Time", ""))
        if awake is not None and row.get("sleep_hours"):
            row["awake_min"] = float(awake)
        if f.get("Awake Count (>5 min)", "").isdigit() and row.get("sleep_hours"):
            row["wakeups"] = float(f["Awake Count (>5 min)"])
        win = re.match(r"\d{4}-\d{2}-\d{2} (\d{2}:\d{2}) - \d{4}-\d{2}-\d{2} (\d{2}:\d{2})", f.get("Main Sleep Window", ""))
        if win:
            row["bedtime_after_noon"] = (_clock(win.group(1)) - 12) % 24  # 23:07 -> 11.1, 00:30 -> 12.5
            row["wake_hour"] = _clock(win.group(2))
        if row:
            out[head.group(1)] = row
    return out


def parse_daily_health(text: str) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for m in re.finditer(r"--- (\d{8}) ---\n(.*?)(?=\n--- \d{8} ---|\Z)", text, flags=re.S):
        d, body = m.group(1), m.group(2)
        day = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        row: dict[str, float] = {}
        if s := re.search(r"Steps: ([\d,]+)", body):
            steps = int(s.group(1).replace(",", ""))
            if steps:
                row["steps"] = float(steps)
        if s := re.search(r"Exercise: (\d+) min", body):
            row["exercise_min"] = float(s.group(1))
        if s := re.search(r"Stress: Avg (\d+)", body):
            row["stress_avg"] = float(s.group(1))
        if s := re.search(r"Sleep HR: Avg (\d+) bpm", body):
            row["sleep_hr_avg"] = float(s.group(1))
        if row:
            out[day] = row
    return out


def parse_stress_level(text: str) -> dict[str, dict[str, float]]:
    return {m.group(1): {"stress_avg": float(m.group(2))}
            for m in re.finditer(r"(\d{4}-\d{2}-\d{2}):\nAverage Stress: (\d+)", text)}


def parse_wellness_check(text: str) -> dict[str, dict[str, float]]:
    """One wellness check (the watch's on-demand reading) -> {day: {wellness_hr, wellness_hrv, ...}}.
    Uses the mean of each reading list; days with no check come back as 'No complete ... data' and are skipped."""
    out: dict[str, dict[str, float]] = {}
    for block in re.split(r"\n(?=Date: \d{4}-\d{2}-\d{2}\n)", text):
        head = re.match(r"Date: (\d{4}-\d{2}-\d{2})\n", block)
        if not head:
            continue
        row: dict[str, float] = {}
        for label, name in (("Heart Rate", "wellness_hr"), ("HRV", "wellness_hrv"), ("Stress", "wellness_stress"),
                            ("Respiration Rate", "wellness_resp"), ("SpO2", "wellness_spo2")):
            m = re.search(rf"^{label} List: \[(.*?)\]$", block, flags=re.M)
            vals = [float(v) for v in re.findall(r"=(\d+(?:\.\d+)?)", m.group(1))] if m else []
            if vals:
                row[name] = round(sum(vals) / len(vals), 1)
        if row:
            out[head.group(1)] = row
    return out


def parse_recovery(text: str) -> dict[str, float]:
    """Current recovery snapshot -> {recovery_pct, recovery_hours_left}. The server has no history for this."""
    out: dict[str, float] = {}
    if m := re.search(r"Recovery: (\d+)%", text):
        out["recovery_pct"] = float(m.group(1))
    if m := re.search(r"Estimated Full Recovery: (\d+)h", text):
        out["recovery_hours_left"] = float(m.group(1))
    return out


def parse_sleep_hrv(text: str) -> dict[str, dict[str, float]]:
    """querySleepHrv -> {day: {hrv_ms, hrv_base_low, hrv_base_high, hrv_baseline}} from the official daily
    assessment only. The raw time-series section that follows it is ignored (the tool says not to average it).
    Days with no official value are skipped. Dated by wake-up day, like the web series."""
    text = re.split(r"\nSleep HRV Time Series", text, maxsplit=1)[0]
    out: dict[str, dict[str, float]] = {}
    for m in re.finditer(r"^(\d{4}-\d{2}-\d{2}):\n((?:[ \t]+.*\n?)+)", text, flags=re.M):
        body = m.group(2)
        row: dict[str, float] = {}
        if s := re.search(r"HRV Avg: (\d+(?:\.\d+)?) ms", body):
            row["hrv_ms"] = float(s.group(1))
        if s := re.search(r"Normal Range: (\d+(?:\.\d+)?) - (\d+(?:\.\d+)?) ms", body):
            row["hrv_base_low"], row["hrv_base_high"] = float(s.group(1)), float(s.group(2))
        if s := re.search(r"Baseline: (\d+(?:\.\d+)?) ms", body):
            row["hrv_baseline"] = float(s.group(1))
        if row.get("hrv_ms"):
            out[m.group(1)] = row
    return out


def parse_training_load(text: str) -> dict[str, dict[str, float]]:
    """queryTrainingLoadAssessment -> {day: {load_short, load_long, load_ratio}}. COROS's short-term load is a
    7-day figure and long-term a 28-day one, but they are not on the same scale as the web series."""
    out: dict[str, dict[str, float]] = {}
    for block in re.split(r"\n(?=\d{4}-\d{2}-\d{2}\n)", text):
        head = re.match(r"(\d{4}-\d{2}-\d{2})\n", block)
        if not head:
            continue
        row: dict[str, float] = {}
        for label, name in (("Short-Term Load", "load_short"), ("Long-Term Load", "load_long"), ("Load Ratio", "load_ratio")):
            if s := re.search(rf"^{label}: (\d+(?:\.\d+)?)\s*$", block, flags=re.M):
                row[name] = float(s.group(1))
        if row:
            out[head.group(1)] = row
    return out


def main(argv: list[str]) -> int:
    try:
        if argv[:1] == ["auth-url"]:
            print(auth_url())
        elif argv[:1] == ["auth-finish"] and len(argv) == 2:
            auth_finish(argv[1])
            print("signed in; token saved to", TOKEN_FILE)
        elif argv[:1] == ["status"]:
            access_token()
            print("ok")
        else:
            print(__doc__, file=sys.stderr)
            return 2
    except CorosMcpError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.code
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
