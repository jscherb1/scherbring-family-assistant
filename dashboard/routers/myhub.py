"""My Hub: one visual page for everything that matters to the person. Merged into Today ("/") in the 2026-10
redesign: /me now redirects there and the old page lives on under /archive/me.

Read-only except dismissing/restoring reviewed attention items (plain same-origin form POSTs). The page and its /api/me twin render the same service output (scripts/myhub.py).
"""
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from ..data import myhub as data
from ..web import ARCHIVE, render
from .ask import _same_origin

router = APIRouter()


@router.get("/me", include_in_schema=False)
def hub_page(request: Request):
    if not ARCHIVE.get():
        return RedirectResponse("/", status_code=302)
    return render(request, "myhub/index.html", "myhub", "hub", "My Hub", data=data.get_hub())


@router.get("/api/me")
def hub_api():
    return data.get_hub()


@router.post("/me/attention/dismiss", include_in_schema=False)
def dismiss(request: Request, key: str = Form(...)):
    if not _same_origin(request):
        raise HTTPException(403, "cross-origin request blocked")
    try:
        data.dismiss(key)
    except data.service.HubError as exc:
        if exc.code != 1:  # already gone: nothing to hide
            raise HTTPException(400, exc.message)
    return RedirectResponse("/me#attention", status_code=303)


@router.post("/me/attention/restore", include_in_schema=False)
def restore(request: Request, key: str = Form("")):
    if not _same_origin(request):
        raise HTTPException(403, "cross-origin request blocked")
    data.restore(key or None)
    return RedirectResponse("/me#attention", status_code=303)


@router.post("/me/refresh", include_in_schema=False)
def refresh(request: Request):
    if not _same_origin(request):
        raise HTTPException(403, "cross-origin request blocked")
    data.refresh_live()
    return RedirectResponse("/me", status_code=303)
