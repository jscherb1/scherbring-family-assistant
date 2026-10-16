"""Reconnect: re-authenticate an expired Google Drive login from any device, phone included (paste-the-callback
flow). Hy-Vee is shown read-only here: it signs in only through the desktop "Hy-Vee Login" shortcut.
All logic is in the shared services (see ..data.reauth). Writes use the same-origin guard from ask.py.
"""
from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse

from ..data import reauth as data
from ..web import render
from .ask import _same_origin

router = APIRouter()


def _blocked(request: Request):
    if not _same_origin(request):
        return JSONResponse({"error": "Blocked: the request didn't come from this site."}, status_code=403)
    return None


@router.get("/reconnect", include_in_schema=False)
def reconnect_page(request: Request):
    return render(request, "reauth/index.html", "health", "reconnect", "Reconnect", data=data.get_status(),
                  drive=None, error=None, ok=None)


@router.post("/reconnect/drive/start", include_in_schema=False)
def drive_start(request: Request):
    if (b := _blocked(request)):
        return b
    try:
        drive = data.service.start("drive")
        err = None
    except data.ReauthError as e:
        drive, err = None, e.message
    return render(request, "reauth/index.html", "health", "reconnect", "Reconnect", data=data.get_status(),
                  drive=drive, error=err, ok=None)


@router.post("/reconnect/drive/complete", include_in_schema=False)
def drive_complete(request: Request, pasted: str = Form("")):
    if (b := _blocked(request)):
        return b
    try:
        data.service.complete("drive", pasted)
        res = data.service.verify("drive", db_path=data.common.db_path())
        ok = "Google Drive is reconnected." if res["ok"] else "Saved, but Drive still looks unhealthy. Try again."
        err = None if res["ok"] else ok
        ok = ok if res["ok"] else None
    except data.ReauthError as e:
        ok, err = None, e.message
    return render(request, "reauth/index.html", "health", "reconnect", "Reconnect", data=data.get_status(),
                  drive=None, error=err, ok=ok)
