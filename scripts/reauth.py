"""Remote re-authentication service: refresh an expired login from any device, no terminal needed.

Stdlib + google-auth-oauthlib (already required by drive_upload). The Telegram agent (via reauth_store.py) and
the dashboard "Reconnect" page both call this module, so they follow one path.

Google Drive is a two-step paste flow (the same one used for COROS):
  start("drive")            -> a Google consent URL. Open it on any device and approve.
  complete("drive", pasted) -> the browser lands on a localhost page that fails to load; paste that whole
                               address back. The one-time code in it is exchanged for a token and saved.
The PKCE verifier needed between the two steps is kept in state/reauth_pending.json (git-ignored, 15 min TTL).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

PENDING_FILE = REPO_ROOT / "state" / "reauth_pending.json"
PENDING_TTL = 15 * 60
REDIRECT_URI = "http://localhost/callback"  # Desktop-app clients accept any localhost redirect
SYSTEMS = ("drive",)


class ReauthError(Exception):
    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.message = message
        self.code = code


def _load_pending(path: Path = PENDING_FILE) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    now = time.time()
    return {k: v for k, v in data.items() if now - v.get("created", 0) < PENDING_TTL}


def _save_pending(data: dict, path: Path = PENDING_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _drive_flow():
    import drive_upload as du
    from google_auth_oauthlib.flow import Flow

    secret = du._client_secret_path()
    if not secret.exists():
        raise ReauthError(f"OAuth client secret not found at {secret}", 1)
    flow = Flow.from_client_secrets_file(str(secret), du.SCOPES)
    flow.redirect_uri = REDIRECT_URI
    return flow, du


def start(system: str, *, pending_path: Path = PENDING_FILE) -> dict:
    if system not in SYSTEMS:
        raise ReauthError(f"unknown system {system!r}; remote sign-in supports: {', '.join(SYSTEMS)}")
    flow, _ = _drive_flow()
    url, state = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="true")
    pending = _load_pending(pending_path)
    pending[system] = {"state": state, "verifier": flow.code_verifier, "created": time.time()}
    _save_pending(pending, pending_path)
    return {
        "system": system,
        "url": url,
        "steps": [
            "Open the link on any device and approve access (tap Advanced > Go to app if Google warns it is unverified).",
            "Your browser then goes to a localhost/callback page that fails to load. That's expected.",
            "Copy the full address from the address bar and send it back.",
        ],
        "expires_minutes": PENDING_TTL // 60,
    }


def complete(system: str, pasted: str, *, pending_path: Path = PENDING_FILE) -> dict:
    if system not in SYSTEMS:
        raise ReauthError(f"unknown system {system!r}; remote sign-in supports: {', '.join(SYSTEMS)}")
    pasted = (pasted or "").strip()
    if "code=" not in pasted:
        raise ReauthError("That doesn't look like the callback address (it should contain code=...).")
    pending = _load_pending(pending_path)
    entry = pending.get(system)
    if not entry:
        raise ReauthError("No sign-in is in progress (or it expired). Start again to get a fresh link.")
    flow, du = _drive_flow()
    flow.code_verifier = entry["verifier"]
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")  # the redirect is plain http://localhost
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    try:
        flow.fetch_token(authorization_response=pasted)
    except Exception as exc:  # noqa: BLE001 - oauthlib raises many types; surface the reason
        raise ReauthError(f"Google rejected the code ({type(exc).__name__}: {exc}). Start again for a fresh link.", 1)
    token_path = du._token_path()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(flow.credentials.to_json(), encoding="utf-8")
    pending.pop(system, None)
    _save_pending(pending, pending_path)
    return {"system": system, "saved": str(token_path), "has_refresh_token": bool(flow.credentials.refresh_token)}


def verify(system: str) -> dict:
    """Prove the saved login works (a real token load/refresh); returns {ok, detail}."""
    import credential_check as cc
    res = cc.probe_drive()
    return {"system": system, "ok": res == "ok", "detail": res}
