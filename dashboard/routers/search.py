"""Jump-to search: the index static/ui.js filters in the browser (pages, actions, recipes, habits, people)."""
from fastapi import APIRouter

from ..data import navindex
from ..web import SECTIONS

router = APIRouter()


@router.get("/api/nav-index")
def nav_index():
    return navindex.get_index(SECTIONS)
