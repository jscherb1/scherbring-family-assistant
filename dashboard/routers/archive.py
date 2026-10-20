"""Index of the archived (pre-redesign) pages. The pages themselves are served by the /archive middleware in
app.py through the normal routes; this just lists them. Remove with the archive (docs/TODO.md)."""
from fastapi import APIRouter, Request

from ..web import LEGACY_SECTIONS, render

router = APIRouter()


@router.get("/archive", include_in_schema=False)
def archive_index(request: Request):
    return render(request, "system/archive.html", "system", "archive", "Archive", sections_old=LEGACY_SECTIONS)
