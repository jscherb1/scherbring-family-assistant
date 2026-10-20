"""Home page: Today (the merged Summary + My Hub page), the Week page, their /api twins, and dismissing attention items.

Under /archive, "/" still renders the old Summary page. Dismiss/restore are plain form POSTs guarded by the same
same-origin check as the ask box; dismissals are the one shared store (see dismissals.py).
"""
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import dismissals
from ..data import summary, today, week
from ..web import ARCHIVE, render
from .ask import _same_origin

router = APIRouter()


def _home():
    return "/" if ARCHIVE.get() else "/#attention"


@router.get("/", include_in_schema=False)
def landing(request: Request, sent: int = 0, ask_error: str = ""):
    if ARCHIVE.get():
        return render(request, "summary/index.html", "summary", "overview", "Summary",
                      data=summary.get_summary(), sent=bool(sent), ask_error=ask_error)
    return render(request, "today/index.html", "today", "today", "Today",
                  data=today.get_today(), sent=bool(sent), ask_error=ask_error)


@router.get("/api/today")
def api_today():
    return today.get_today()


@router.get("/week", include_in_schema=False)
def week_page(request: Request):
    return render(request, "today/week.html", "today", "week", "Week", data=week.get_week())


@router.get("/api/week")
def api_week():
    return week.get_week()


@router.get("/api/summary")
def api_summary():
    return summary.get_summary()


@router.post("/attention/dismiss", include_in_schema=False)
def dismiss(request: Request, key: str = Form(...)):
    if not _same_origin(request):
        raise HTTPException(403, "cross-origin request blocked")
    if ARCHIVE.get():
        items = summary.get_summary()
        text = next((a["text"] for a in items["attention"] + items["dismissed"] if a["key"] == key), "")
    else:
        text = (today.find_item(key) or {}).get("text", "")
    dismissals.dismiss(key, text)
    return RedirectResponse(_home(), status_code=303)


@router.post("/attention/restore", include_in_schema=False)
def restore(request: Request, key: str = Form("")):
    if not _same_origin(request):
        raise HTTPException(403, "cross-origin request blocked")
    dismissals.restore(key or None)
    return RedirectResponse(_home(), status_code=303)
