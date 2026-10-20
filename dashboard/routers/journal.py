"""Journal + nightly check-in: the check-in card (also on Wellness > Today), the Journal page and its writes.

Every read and write goes through the shared service in scripts/journal.py (via data/journal.py), the same code
the `journal` subagent's CLI runs; this router only does web plumbing. Writes are guarded by the same-origin
check the ask box uses.
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ..data import common, journal as data
from ..web import render
from .ask import _same_origin

router = APIRouter()
service = data.service
NEXT_PAGES = {"/wellness", "/wellness/journal"}  # where a plain (non-htmx) form post may return to


def _blocked(request: Request):
    if not _same_origin(request):
        return JSONResponse({"error": "Blocked: the request didn't come from this site."}, status_code=403)
    return None


def _back(next_page: str, error: str | None = None, day: str | None = None):
    target = next_page if next_page in NEXT_PAGES else "/wellness/journal"
    params = [f"error={error.replace(' ', '+')}"] if error else []
    if day and target == "/wellness/journal":
        params.append(f"date={day}")
    return RedirectResponse(target + ("?" + "&".join(params) if params else ""), status_code=303)


@router.get("/wellness/journal", include_in_schema=False)
def journal_page(request: Request, date: str = "", days: int = 28, error: str = ""):
    return render(request, "wellness/journal.html", "wellness", "journal", "Journal",
                  checkin=data.get_checkin(date or None), history=data.get_history(days),
                  questions=data.get_questions(), error=error)


@router.post("/wellness/checkin", include_in_schema=False)
async def checkin(request: Request):
    """The check-in form: rating + note, a yes/no per question (q_<slug>), and daily habits done (habit)."""
    if (resp := _blocked(request)):
        return resp
    form = await request.form()
    day = form.get("date") or None
    yes = [k[2:] for k, v in form.items() if k.startswith("q_") and v == "yes"]
    no = [k[2:] for k, v in form.items() if k.startswith("q_") and v == "no"]
    error = None
    try:
        service.record(day=day, score=form.get("score") or None, note=(form.get("note") or "").strip() or None,
                       yes=yes, no=no, habits_done=form.getlist("habit"), source="dashboard", db_path=common.db_path())
    except service.JournalError as exc:
        error = exc.message
    if request.headers.get("hx-request"):
        # 200 even on error: htmx only swaps 2xx, and the card shows the message inline.
        return render(request, "wellness/_checkin.html", "wellness", "journal", "Check-in",
                      c=data.get_checkin(day), error=error, saved=not error, open=True)
    return _back(form.get("next", ""), error, day)


@router.post("/journal/questions", include_in_schema=False)
async def add_question(request: Request):
    if (resp := _blocked(request)):
        return resp
    form = await request.form()
    try:
        service.add_question(form.get("text", ""), category=form.get("category") or None, db_path=common.db_path())
    except service.JournalError as exc:
        return _back("/wellness/journal", exc.message)
    return _back("/wellness/journal")


@router.post("/journal/questions/{slug}/update", include_in_schema=False)
async def update_question(request: Request, slug: str):
    if (resp := _blocked(request)):
        return resp
    form = await request.form()
    try:
        service.update_question(slug, active=form.get("active") == "1", db_path=common.db_path())
    except service.JournalError as exc:
        return _back("/wellness/journal", exc.message)
    return _back("/wellness/journal")


def _api(result):
    if isinstance(result, dict) and "error" in result:
        return JSONResponse(result, status_code=404 if result.get("code") == 1 else 400 if "code" in result else 503)
    return result


@router.get("/api/journal/checkin")
def api_checkin(date: str = ""):
    return _api(data.get_checkin(date or None))


@router.get("/api/journal/history")
def api_history(days: int = 28):
    return _api(data.get_history(days))


@router.get("/api/journal/questions")
def api_questions():
    return _api(data.get_questions())


@router.get("/api/journal/impact")
def api_impact(days: int = 90):
    return _api(data.get_impact(days))
