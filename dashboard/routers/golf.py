"""Golf section: current handicap, recent rounds, log a round, saved courses.

Every read and write goes through the shared service in scripts/golf.py (via data/golf.py),
the same code the `golf` subagent's CLI uses. This router only does web plumbing. Writes are
guarded by the same-origin check the ask box uses; access control is the network (Tailscale-only bind).
"""
from datetime import date
from urllib.parse import quote_plus

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ..data import common, golf as data
from ..web import render
from .ask import _same_origin

router = APIRouter()
service = data.service


def _blocked(request: Request):
    if not _same_origin(request):
        return JSONResponse({"error": "Blocked: the request didn't come from this site."}, status_code=403)
    return None


def _back(path: str, error: str | None = None, notice: str | None = None):
    qs = f"?error={quote_plus(error)}" if error else (f"?notice={quote_plus(notice)}" if notice else "")
    return RedirectResponse(f"{path}{qs}", status_code=303)


@router.get("/golf", include_in_schema=False)
def golf_page(request: Request, error: str = "", notice: str = ""):
    return render(request, "golf/index.html", "golf", "rounds", "Golf", data=data.get_rounds(),
                  error=error, notice=notice, today=date.today().isoformat())


@router.get("/golf/courses", include_in_schema=False)
def courses_page(request: Request, error: str = "", notice: str = ""):
    return render(request, "golf/courses.html", "golf", "courses", "Golf courses", data=data.get_courses(),
                  error=error, notice=notice)


@router.post("/golf/rounds", include_in_schema=False)
def add_round(request: Request, played_on: str = Form(""), course: str = Form(""), score: str = Form(""),
              rating: str = Form(""), slope: str = Form(""), notes: str = Form("")):
    if (resp := _blocked(request)):
        return resp
    try:
        res = service.add_round(played_on, course, score, rating=rating.strip() or None,
                                slope=slope.strip() or None, notes=notes or None, db_path=common.db_path())
    except service.GolfError as exc:
        return _back("/golf", error=exc.message)
    msg = f"Logged {res['round']['score']} at {res['round']['course']}. Handicap {res['handicap_before']} → {res['handicap_after']}."
    if not res["sheet"].get("synced"):
        msg += " (Not yet in the Google Sheet; use 'Sync to sheet' on this page to retry.)"
    return _back("/golf", notice=msg)


@router.post("/golf/rounds/undo", include_in_schema=False)
def undo(request: Request):
    if (resp := _blocked(request)):
        return resp
    try:
        res = service.undo_last(db_path=common.db_path())
    except service.GolfError as exc:
        return _back("/golf", error=exc.message)
    return _back("/golf", notice=f"Removed {res['deleted']['course']} ({res['deleted']['score']}). Handicap now {res['handicap']}.")


@router.post("/golf/sync", include_in_schema=False)
def sync(request: Request):
    if (resp := _blocked(request)):
        return resp
    res = service.sync_pending(db_path=common.db_path())
    if res["pending"] and res["synced"] < res["pending"]:
        err = next((r["error"] for r in res["results"] if not r["synced"]), "unknown error")
        return _back("/golf", error=f"Sheet sync failed: {err}")
    return _back("/golf", notice=f"Synced {res['synced']} round(s) to the sheet.")


@router.post("/golf/courses", include_in_schema=False)
def save_course(request: Request, name: str = Form(""), rating: str = Form(""), slope: str = Form(""),
                tee: str = Form("")):
    if (resp := _blocked(request)):
        return resp
    try:
        service.save_course(name, rating, slope, tee=tee or None, db_path=common.db_path())
    except service.GolfError as exc:
        return _back("/golf/courses", error=exc.message)
    return _back("/golf/courses", notice="Course saved.")


@router.get("/api/golf")
def api_golf(limit: int = 30):
    return data.get_rounds(limit)


@router.get("/api/golf/courses")
def api_courses():
    return data.get_courses()
