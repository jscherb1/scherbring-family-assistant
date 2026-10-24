"""Home Assistant section: the Family Board (replica of the HA dashboard) and HA Health.

Every read and write goes through the shared service in scripts/ha.py (via data/ha.py), the same code
the `ha-overview` subagent's CLI uses. Writes are limited to that service's allow-list and are guarded
by the same-origin check the ask box uses; access control is the network (Tailscale-only bind).
"""
from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from ..data import ha as data
from ..web import render
from .ask import _same_origin

router = APIRouter()
service = data.service


@router.get("/home/ha", include_in_schema=False)
def board_page(request: Request, error: str = ""):
    return render(request, "ha/board.html", "ha", "board", "Family Board", data=data.get_board(), error=error)


@router.get("/home/ha/health", include_in_schema=False)
def health_page(request: Request):
    return render(request, "ha/health.html", "ha", "health", "Home Assistant health", data=data.get_health())


@router.post("/home/ha/act", include_in_schema=False)
def act(request: Request, entity_id: str = Form(""), action: str = Form(""), value: str = Form("")):
    if not _same_origin(request):
        return JSONResponse({"error": "Blocked: the request didn't come from this site."}, status_code=403)
    try:
        service.act(entity_id, action, value or None)
    except service.HAError as exc:
        return RedirectResponse(f"/home/ha?error={exc.message.replace(' ', '+')}", status_code=303)
    return RedirectResponse("/home/ha", status_code=303)


@router.get("/home/ha/camera/{entity_id}", include_in_schema=False)
def camera(entity_id: str):
    try:
        body, content_type = service.camera_image(entity_id)
    except service.HAError as exc:
        return JSONResponse({"error": exc.message}, status_code=404)
    return Response(body, media_type=content_type or "image/jpeg")


@router.get("/api/ha/board")
def api_board():
    return data.get_board()


@router.get("/api/ha/health")
def api_health():
    return data.get_health()
