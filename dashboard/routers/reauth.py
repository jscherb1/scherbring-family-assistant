"""Reconnect: re-authenticate an expired login (Google Drive, Hy-Vee) from any device, phone included.

Drive uses the paste-the-callback flow; Hy-Vee shows a server-side browser you tap and type into to clear the
CAPTCHA. All logic is in the shared services (see ..data.reauth). Writes use the same-origin guard from ask.py.
"""
from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

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


# ---- Hy-Vee remote browser ----

@router.get("/reconnect/hyvee", include_in_schema=False)
def hyvee_page(request: Request):
    return render(request, "reauth/hyvee.html", "health", "reconnect", "Reconnect Hy-Vee",
                  status=data.hyvee_session().status())


@router.get("/reconnect/hyvee/state", include_in_schema=False)
def hyvee_state():
    return data.hyvee_session().status()


@router.get("/reconnect/hyvee/shot.jpg", include_in_schema=False)
def hyvee_shot():
    try:
        img = data.hyvee_session().screenshot()
    except data.RemoteLoginError:
        img = None
    if not img:
        return Response(status_code=204)
    return Response(img, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


def _act(request: Request, fn):
    if (b := _blocked(request)):
        return b
    try:
        fn()
    except data.RemoteLoginError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return data.hyvee_session().status()


@router.post("/reconnect/hyvee/start", include_in_schema=False)
def hyvee_start(request: Request):
    if (b := _blocked(request)):
        return b
    return data.hyvee_session().start()


@router.post("/reconnect/hyvee/click", include_in_schema=False)
def hyvee_click(request: Request, x: float = Form(...), y: float = Form(...)):
    return _act(request, lambda: data.hyvee_session().click(x, y))


@router.post("/reconnect/hyvee/type", include_in_schema=False)
def hyvee_type(request: Request, text: str = Form("")):
    return _act(request, lambda: data.hyvee_session().type_text(text))


@router.post("/reconnect/hyvee/key", include_in_schema=False)
def hyvee_key(request: Request, key: str = Form(...)):
    return _act(request, lambda: data.hyvee_session().press(key))


@router.post("/reconnect/hyvee/fill", include_in_schema=False)
def hyvee_fill(request: Request):
    return _act(request, lambda: data.hyvee_session().fill_saved_credentials())


@router.post("/reconnect/hyvee/finish", include_in_schema=False)
def hyvee_finish(request: Request):
    return _act(request, lambda: data.hyvee_session().finish())


@router.post("/reconnect/hyvee/stop", include_in_schema=False)
def hyvee_stop(request: Request):
    if (b := _blocked(request)):
        return b
    data.hyvee_session().stop()
    return RedirectResponse("/reconnect", status_code=303)
