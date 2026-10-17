"""Landing page: ask box, one status line, a short needs-attention list, glance tiles.

Attention items can be dismissed (and restored); see dismissals.py. Like the ask box these are
plain form POSTs guarded by the same same-origin check.
"""
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import dismissals
from ..data import summary
from ..web import render
from .ask import _same_origin

router = APIRouter()


@router.get("/", include_in_schema=False)
def landing(request: Request, sent: int = 0, ask_error: str = ""):
    return render(request, "summary/index.html", "summary", "overview", "Summary",
                  data=summary.get_summary(), sent=bool(sent), ask_error=ask_error)


@router.get("/api/summary")
def api_summary():
    return summary.get_summary()


@router.post("/attention/dismiss", include_in_schema=False)
def dismiss(request: Request, key: str = Form(...)):
    if not _same_origin(request):
        raise HTTPException(403, "cross-origin request blocked")
    items = summary.get_summary()
    text = next((a["text"] for a in items["attention"] + items["dismissed"] if a["key"] == key), "")
    dismissals.dismiss(key, text)
    return RedirectResponse("/", status_code=303)


@router.post("/attention/restore", include_in_schema=False)
def restore(request: Request, key: str = Form("")):
    if not _same_origin(request):
        raise HTTPException(403, "cross-origin request blocked")
    dismissals.restore(key or None)
    return RedirectResponse("/", status_code=303)
