"""Reconnect page data: calls the shared re-auth service (scripts/reauth.py) and the system health service.

No rules of its own; the Telegram agent's `reauth_store.py` runs the same Drive code. Hy-Vee has no in-app
sign-in: its status comes from the one connection check (scripts/hyvee/hyvee_connection.py).
"""
import sys

from . import common

sys.path.insert(0, str(common.SCRIPTS_DIR))
sys.path.insert(0, str(common.SCRIPTS_DIR / "hyvee"))
import hyvee_connection  # noqa: E402
import reauth as service  # noqa: E402
import system_health  # noqa: E402

ReauthError = service.ReauthError


@common.guarded
def get_status():
    health = {s["key"]: s for s in system_health.get_status(db_path=common.db_path())}
    return {"systems": [health[k] for k in ("drive", "hyvee") if k in health],
            "hyvee_login_age_days": hyvee_connection.login_age_days(),
            "hyvee_fix": hyvee_connection.LOGIN_FIX.format(repo="<repo>")}
