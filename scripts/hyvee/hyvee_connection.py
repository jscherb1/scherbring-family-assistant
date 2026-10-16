"""The one Hy-Vee connection: how we know we're signed in, what we record, and what happens after a login.

Everything that asks "is Hy-Vee connected?" goes through here:
  - check()          one read-only test of the saved session (account-only order-history API). Three outcomes:
                       ok       200 with order data
                       expired  a clear 401/403, or no saved session
                       unknown  anything else (Cloudflare, 429, 5xx, timeout, browser trouble): never a verdict
                     Every result carries a reason (status, page, title) so an alert can say why.
  - probe_page(page) the same test on a page you already have open (cart_ops uses it).
  - on_login_saved() call after ANY sign-in saves the session: records the login time, verifies, updates health.
  - A passing check re-saves the session file, so cookies Hy-Vee rotates are kept (a free keep-alive).

system_health.check_hyvee() runs check() every 2 hours and is the only thing that raises or clears the alert.
The only way to sign in is scripts/hyvee/login_test.py (the "Hy-Vee Login" desktop shortcut).

Usage:
    python scripts/hyvee/hyvee_connection.py check     # run the check, record it, print JSON
    python scripts/hyvee/hyvee_connection.py status    # what we last recorded, no browser
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
for p in (str(REPO_ROOT / "scripts"), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import hyvee_web  # noqa: E402

SESSION_FILE = REPO_ROOT / "state" / "hyvee_session.json"
STATE_FILE = REPO_ROOT / "state" / "hyvee_connection.json"  # {"authed_at": iso, "source": str}
CHECK_LOG = REPO_ROOT / "state" / "hyvee_checks.jsonl"  # one line per check: evidence for how long a login really lasts
CHECK_LOG_MAX = 600
CHALLENGE_WORDS = ("just a moment", "attention required", "verify you are", "challenge")
CONFIRM_DELAY_S = 20  # an "expired" verdict is re-tested once before it counts

LOGIN_FIX = ("Double-click \"Hy-Vee Login\" on the Windows desktop (or run: cd {repo} && .venv/bin/python "
             "scripts/hyvee/login_test.py --login-only --manual-wait 300), solve the CAPTCHA, and it re-checks itself.")


def _now() -> datetime:
    return datetime.now().astimezone()


def result(status: str, reason: str, **extra) -> dict:
    return {"status": status, "reason": reason, "checked_at": _now().isoformat(timespec="seconds"), **extra}


def probe_page(page) -> dict:
    """Test an already-open page's browser context. Read-only. Never raises."""
    try:
        page.goto(hyvee_web.HOME_URL, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1500)
        title = (page.title() or "")[:80]
        url = page.url.split("?")[0]
        resp = page.request.get(hyvee_web.PURCHASE_HISTORY_API.format(page=1))
    except Exception as exc:  # noqa: BLE001 - a flaky probe is "unknown", never "expired"
        return result("unknown", f"probe failed to run ({type(exc).__name__})")
    facts = {"http_status": resp.status, "url": url, "title": title}
    where = f"order-history API {resp.status}, page '{title}'"
    if any(w in title.lower() for w in CHALLENGE_WORDS):
        return result("unknown", f"Cloudflare bot check in the way ({where})", **facts)
    if resp.status == 200:
        try:
            if "purchaseGroups" in (resp.json() or {}):
                return result("ok", "signed in (order history readable)", **facts)
        except Exception:  # noqa: BLE001
            pass
        return result("unknown", f"order-history answered 200 without order data ({where})", **facts)
    if resp.status in (401, 403):
        return result("expired", f"Hy-Vee rejected the saved session ({where})", **facts)
    return result("unknown", f"unexpected answer ({where})", **facts)


def _persist_session(context, session_file: Path) -> None:
    """Keep cookies Hy-Vee rotated during a passing check. Atomic, owner-only."""
    tmp = session_file.with_suffix(session_file.suffix + ".tmp")
    context.storage_state(path=str(tmp))
    os.chmod(tmp, 0o600)
    tmp.replace(session_file)


def _check_once(session_file: Path, keep_alive: bool) -> dict:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        try:
            context = browser.new_context(storage_state=str(session_file), viewport={"width": 1366, "height": 900})
            res = probe_page(context.new_page())
            if res["status"] == "ok" and keep_alive:
                try:
                    _persist_session(context, session_file)
                except Exception:  # noqa: BLE001 - failing to refresh the file must not fail the check
                    pass
            return res
        finally:
            browser.close()


def check(session_file: Path = SESSION_FILE, keep_alive: bool = True, probe=None, sleep=time.sleep) -> dict:
    """The one check. `probe(session_file, keep_alive)` is injectable for tests."""
    if not session_file.exists():
        return result("expired", "no saved session; sign in once with the Hy-Vee Login shortcut")
    run = probe or _check_once
    try:
        res = run(session_file, keep_alive)
        if res["status"] == "expired":  # one 401 has been a false alarm before: confirm before believing it
            sleep(CONFIRM_DELAY_S)
            again = run(session_file, keep_alive)
            if again["status"] != "expired":
                res = result("unknown", f"first test said expired but the re-test said {again['status']}: {again['reason']}")
    except Exception as exc:  # noqa: BLE001
        res = result("unknown", f"check could not run ({type(exc).__name__}: {str(exc)[:100]})")
    res["login_age_days"] = login_age_days()
    log_check(res)
    return res


def login_time() -> datetime | None:
    try:
        return datetime.fromisoformat(json.loads(STATE_FILE.read_text(encoding="utf-8"))["authed_at"]).astimezone()
    except (OSError, ValueError, KeyError):
        return None


def login_age_days() -> float | None:
    t = login_time()
    return round((_now() - t).total_seconds() / 86400, 2) if t else None


def log_check(res: dict) -> None:
    try:
        line = json.dumps({k: res.get(k) for k in ("checked_at", "status", "http_status", "reason", "login_age_days")})
        lines = (CHECK_LOG.read_text(encoding="utf-8").splitlines() if CHECK_LOG.exists() else []) + [line]
        CHECK_LOG.parent.mkdir(parents=True, exist_ok=True)
        CHECK_LOG.write_text("\n".join(lines[-CHECK_LOG_MAX:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def health_detail(res: dict) -> tuple[str, str]:
    """Map a check result to (system_health status, one-line detail that says why)."""
    age = res.get("login_age_days")
    age_s = f", signed in {age:g} days ago" if age is not None else ""
    if res["status"] == "ok":
        return "ok", f"order history readable{age_s}"
    if res["status"] == "expired":
        return "down", f"{res['reason']}{age_s}. {LOGIN_FIX.format(repo=REPO_ROOT)}"
    return "unknown", f"couldn't tell: {res['reason']}"


def on_login_saved(source: str, notify=None, db_path=None) -> dict:
    """Call right after any sign-in writes the session file. Records the login, verifies it, updates health."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({"authed_at": _now().isoformat(timespec="seconds"), "source": source}))
    except OSError:
        pass
    import system_health
    out = system_health.run_checks(["hyvee"], db_path=db_path, notify=notify or system_health.telegram_notify)
    return out[0]


def status() -> dict:
    import system_health
    row = next((s for s in system_health.get_status() if s["key"] == "hyvee"), {})
    t = login_time()
    return {"status": row.get("status", "unknown"), "detail": row.get("detail", ""), "checked_at": row.get("checked_at"),
            "last_ok_at": row.get("last_ok_at"), "login_at": t.isoformat(timespec="seconds") if t else None,
            "login_age_days": login_age_days()}


def main(argv=None) -> int:
    cmd = (argv or sys.argv[1:] or ["status"])[0]
    if cmd == "check":
        import system_health
        out = system_health.run_checks(["hyvee"], notify=system_health.telegram_notify)[0]
    elif cmd == "status":
        out = status()
    else:
        print(json.dumps({"error": "usage: hyvee_connection.py check|status"}), file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
