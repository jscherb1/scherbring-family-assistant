"""Weekly review section: the latest findings, with accept / dismiss / done.

Every read and write goes through the shared service in scripts/review.py (via data/review.py), the same code
the `review` subagent's CLI uses. This router only does web plumbing. Writes are guarded by the same-origin
check the ask box uses; access control is the network (Tailscale-only bind).
"""
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ..data import review as data
from ..web import render
from .ask import _same_origin

router = APIRouter()
service = data.service


@router.get("/review", include_in_schema=False)
def review_page(request: Request, show: str = "open", error: str = ""):
    return render(request, "review/index.html", "review", "review", "Weekly review",
                  data=data.get_review(show), error=error)


@router.get("/api/review")
def api_review(show: str = "open"):
    return data.get_review(show)


@router.post("/review/{finding_id}/status", include_in_schema=False)
def set_status(request: Request, finding_id: str, status: str = Form("")):
    if not _same_origin(request):
        return JSONResponse({"error": "Blocked: the request didn't come from this site."}, status_code=403)
    result = data.set_status(finding_id, status)
    if result.get("error"):
        return RedirectResponse(f"/review?{urlencode({'error': result['error']})}", status_code=303)
    return RedirectResponse("/review", status_code=303)
