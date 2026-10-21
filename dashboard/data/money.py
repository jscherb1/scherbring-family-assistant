"""Money reader for the dashboard (the Money home page and the live tagging counts).

No logic of its own: every number comes from the shared service in scripts/money.py, the same code
the finance agent's money_store.py CLI runs. The data is the local Monarch snapshot that
scripts/money_sync.py refreshes.
"""
import subprocess
import sys

from . import common

sys.path.insert(0, str(common.SCRIPTS_DIR))
import money as service  # noqa: E402

SYNC_PYTHON = common.BASE_DIR / "vendor" / "monarch-mcp-server" / ".venv" / "bin" / "python"


def _spark(series, width=240, height=48):
    """SVG polyline points for the net-worth sparkline."""
    values = [p["value"] for p in series]
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    step = width / (len(values) - 1)
    return " ".join(f"{i * step:.1f},{height - (v - lo) / span * height:.1f}" for i, v in enumerate(values))


@common.guarded
def get_overview():
    data = service.overview(db_path=common.db_path())
    if data.get("networth"):
        data["networth"]["spark"] = _spark(data["networth"]["series"])
    return data


@common.guarded
def get_review_queue():
    return {**service.review_queue(db_path=common.db_path()), "sync": service.sync_status(db_path=common.db_path())}


def start_refresh() -> bool:
    """Kick off a quick sync in the background (the page reloads to show it). False if it can't start."""
    if not SYNC_PYTHON.exists():
        return False
    subprocess.Popen([str(SYNC_PYTHON), str(common.SCRIPTS_DIR / "money_sync.py"), "--mode", "quick",
                      "--db", str(common.db_path())],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return True
