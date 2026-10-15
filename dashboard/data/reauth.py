"""Reconnect page data: calls the shared remote re-auth services (scripts/reauth.py, scripts/hyvee/remote_login.py).

No rules of its own; the Telegram agent's `reauth_store.py` runs the same Drive code.
"""
import sys

from . import common

sys.path.insert(0, str(common.SCRIPTS_DIR))
sys.path.insert(0, str(common.SCRIPTS_DIR / "hyvee"))
import reauth as service  # noqa: E402
import remote_login  # noqa: E402
import system_health  # noqa: E402

ReauthError = service.ReauthError
RemoteLoginError = remote_login.RemoteLoginError


def hyvee_session() -> remote_login.RemoteLogin:
    return remote_login.get_session()


@common.guarded
def get_status():
    health = {s["key"]: s for s in system_health.get_status(db_path=common.db_path())}
    return {"systems": [health[k] for k in ("drive", "hyvee") if k in health],
            "hyvee_session": hyvee_session().status()}
