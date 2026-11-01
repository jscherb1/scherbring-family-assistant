"""Weekly review reader for the dashboard.

No logic of its own: runs, findings and counts all come from the shared service in scripts/review.py, the same
code the `review` subagent's CLI runs.
"""
import sys

from . import common

sys.path.insert(0, str(common.SCRIPTS_DIR))
import review as service  # noqa: E402

STATUS_ORDER = ("new", "accepted", "done", "dismissed")


@common.guarded
def get_review(show: str = "open"):
    """The latest run plus findings. show: open (new + accepted), all, or one status."""
    path = common.db_path()
    try:
        latest = service.latest_run(db_path=path)
    except service.ReviewError:
        latest = None
    if show == "open":
        findings = [f for s in ("new", "accepted") for f in service.list_findings(status=s, db_path=path)]
    elif show in service.STATUSES:
        findings = service.list_findings(status=show, db_path=path)
    else:
        show = "all"
        findings = service.list_findings(db_path=path, limit=200)
    findings.sort(key=lambda f: f["updated_at"], reverse=True)
    findings.sort(key=lambda f: STATUS_ORDER.index(f["status"]))
    return {"latest": ({k: v for k, v in latest.items() if k != "findings"} if latest else None),
            "findings": findings, "counts": service.counts(db_path=path), "show": show,
            "statuses": list(service.STATUSES)}


@common.guarded
def get_new_findings():
    """New findings (newest first), for the Summary tile."""
    return service.list_findings(status="new", db_path=common.db_path())


def set_status(finding_id: str, status: str) -> dict:
    """Write path used by the routes: straight to the service."""
    try:
        return service.set_status(finding_id, status, db_path=common.db_path())
    except service.ReviewError as exc:
        return {"error": exc.message, "code": exc.code}
