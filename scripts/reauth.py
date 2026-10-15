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
HYVEE_SESSION_FILE = REPO_ROOT / "state" / "hyvee_session.json"
HYVEE_DOMAIN = "hy-vee.com"


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


def verify(system: str, *, db_path=None) -> dict:
    """Prove the saved login works (a real token load/refresh); returns {ok, detail}.

    Also records the result in system_health so the Systems tile and Reconnect page flip to green right away."""
    import credential_check as cc
    import system_health
    res = cc.probe_drive()
    if res == "ok":
        system_health.record(system, "ok", "reconnected; live read worked", db_path=db_path,
                             notify=system_health.telegram_notify)
    return {"system": system, "ok": res == "ok", "detail": res}


# ---- Hy-Vee cookie import ----

def _hyvee_cookie_domain(domain: str) -> bool:
    d = (domain or "").lstrip(".").lower()
    return d == HYVEE_DOMAIN or d.endswith("." + HYVEE_DOMAIN)


def _same_site(v) -> str:
    return {"strict": "Strict", "lax": "Lax", "none": "None", "no_restriction": "None"}.get(str(v or "").lower(), "Lax")


def parse_cookies(text: str) -> list[dict]:
    """Turn pasted cookie-exporter output into Playwright cookies, keeping only Hy-Vee domains.

    Accepts a JSON array (Cookie-Editor / EditThisCookie), {"cookies": [...]} (Playwright state), or a Netscape
    cookies.txt. Values are never included in error messages."""
    text = (text or "").strip()
    if not text:
        raise ReauthError("Paste the exported cookies first.")
    raw: list[dict] = []
    if text[0] in "[{":
        try:
            data = json.loads(text)
        except ValueError:
            raise ReauthError("That looks like JSON but couldn't be read. Re-export and paste the whole thing.")
        raw = data.get("cookies", []) if isinstance(data, dict) else data
        if not isinstance(raw, list):
            raise ReauthError("Unrecognised cookie JSON (expected a list of cookies).")
    else:
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
                http_only = True
            elif not line or line.startswith("#"):
                continue
            else:
                http_only = False
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            raw.append({"domain": parts[0], "path": parts[2], "secure": parts[3].upper() == "TRUE",
                        "expires": parts[4], "name": parts[5], "value": parts[6], "httpOnly": http_only})
    out = []
    for c in raw:
        if not isinstance(c, dict) or not c.get("name") or not _hyvee_cookie_domain(c.get("domain", "")):
            continue
        exp = c.get("expires", c.get("expirationDate", -1))
        try:
            exp = float(exp)
        except (TypeError, ValueError):
            exp = -1
        if c.get("session") or exp <= 0:
            exp = -1
        out.append({"name": str(c["name"]), "value": str(c.get("value", "")), "domain": c["domain"],
                    "path": c.get("path") or "/", "expires": exp,
                    "httpOnly": bool(c.get("httpOnly", False)), "secure": bool(c.get("secure", True)),
                    "sameSite": _same_site(c.get("sameSite"))})
    if not out:
        raise ReauthError("No hy-vee.com cookies found in that paste. Export while on a signed-in hy-vee.com tab.")
    return out


def _validate_hyvee_state(state: dict) -> bool:
    """Open a headless browser with these cookies and check it's signed in (read-only)."""
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "hyvee"))
    from playwright.sync_api import sync_playwright
    import login_test as lt
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        try:
            page = browser.new_context(storage_state=state).new_page()
            return bool(lt.is_logged_in(page))
        finally:
            browser.close()


def import_hyvee_cookies(pasted: str, *, session_file: Path = HYVEE_SESSION_FILE, validate=None) -> dict:
    """Validate pasted cookies against Hy-Vee and, only if signed in, save them as the session.

    The previous session file is kept as <file>.bak. `validate` is injectable for tests."""
    cookies = parse_cookies(pasted)
    state = {"cookies": cookies, "origins": []}
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "hyvee"))
    try:
        ok = (validate or _validate_hyvee_state)(state)
    except Exception as exc:  # noqa: BLE001 - browser tooling failures should read as a message
        raise ReauthError(f"Couldn't test the cookies ({type(exc).__name__}). Nothing was saved.", 1)
    if not ok:
        raise ReauthError("Hy-Vee did not treat those cookies as signed in (expired, or tied to your browser). "
                          "Nothing was saved. Try again right after signing in, or use the on-server browser.", 1)
    session_file.parent.mkdir(parents=True, exist_ok=True)
    if session_file.exists():
        session_file.replace(session_file.with_suffix(session_file.suffix + ".bak"))
    session_file.write_text(json.dumps(state), encoding="utf-8")
    try:
        os.chmod(session_file, 0o600)
    except OSError:
        pass
    if validate is not None:  # tests inject a validator; don't touch real login bookkeeping
        return {"system": "hyvee", "saved": True, "cookies": len(cookies), "names": sorted({c["name"] for c in cookies})}
    try:
        from credential_check import record_login
        record_login("hyvee")
        import system_health
        system_health.record("hyvee", "ok", "reconnected via cookie import", notify=system_health.telegram_notify)
    except Exception:  # noqa: BLE001 - bookkeeping must not undo a good login
        pass
    return {"system": "hyvee", "saved": True, "cookies": len(cookies),
            "names": sorted({c["name"] for c in cookies})}
