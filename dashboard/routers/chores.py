"""Adult chore-chart configuration: kids, chores, payouts and record fixes.

Every read and write goes through the shared service in scripts/chores.py (via data/chores.py),
the same code the `chores` subagent's CLI uses. This router only does web plumbing: forms in,
303 redirect back with a one-line result or error. Writes are guarded by the same-origin
check the ask box uses; access control is the network (Tailscale-only bind). There is no login:
the kid page links nowhere near this section, and every payout is in the ledger.
"""
from urllib.parse import quote_plus

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ..data import chores as data, common
from ..web import render
from .ask import _same_origin

router = APIRouter()
service = data.service


def _blocked(request: Request):
    if not _same_origin(request):
        return JSONResponse({"error": "Blocked: the request didn't come from this site."}, status_code=403)
    return None


def _back(path: str, error: str | None = None, ok: str | None = None):
    qs = f"?error={quote_plus(error)}" if error else (f"?ok={quote_plus(ok)}" if ok else "")
    return RedirectResponse(f"{path}{qs}", status_code=303)


def _int(text: str) -> int | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        raise service.ChoreError("That needs to be a whole number") from None


def _run(path: str, fn, ok: str):
    """Call a service write, turning ChoreError into a message on the redirect."""
    try:
        fn()
    except service.ChoreError as exc:
        return _back(path, error=exc.message)
    return _back(path, ok=ok)


def _page(request, template, page, title, getter, error, ok):
    return render(request, template, "chores", page, title, data=getter(), error=error, ok=ok)


@router.get("/chores", include_in_schema=False)
def overview_page(request: Request, error: str = "", ok: str = ""):
    return _page(request, "chores/overview.html", "overview", "Chores", data.get_overview, error, ok)


@router.get("/chores/manage", include_in_schema=False)
def manage_page(request: Request, error: str = "", ok: str = ""):
    return _page(request, "chores/manage.html", "manage", "Manage chores", data.get_manage, error, ok)


@router.get("/chores/ledger", include_in_schema=False)
def ledger_page(request: Request, error: str = "", ok: str = ""):
    return _page(request, "chores/ledger.html", "ledger", "Money", data.get_ledger_page, error, ok)


# ---- writes: kids ----

@router.post("/chores/kids", include_in_schema=False)
def add_kid(request: Request, name: str = Form(""), avatar: str = Form("")):
    if (resp := _blocked(request)):
        return resp
    return _run("/chores/manage", lambda: service.add_kid(name, avatar=avatar.strip() or None,
                                                          db_path=common.db_path()), "Kid added")


@router.post("/chores/kids/{kid_id}/update", include_in_schema=False)
def update_kid(request: Request, kid_id: str, name: str = Form(""), avatar: str = Form(""),
               active: str = Form("")):
    if (resp := _blocked(request)):
        return resp
    changes = {}
    if name.strip():
        changes["name"] = name
    if avatar.strip():
        changes["avatar"] = avatar.strip()
    if active in ("0", "1"):
        changes["active"] = active == "1"
    return _run("/chores/manage", lambda: service.update_kid(kid_id, db_path=common.db_path(), **changes),
                "Kid updated")


# ---- writes: chores ----

@router.post("/chores/chores", include_in_schema=False)
def create_chore(request: Request, name: str = Form(""), icon: str = Form(""), frequency: str = Form("daily"),
                 target: str = Form(""), days: list[str] = Form([]), value: str = Form("0"),
                 description: str = Form(""), kids: list[str] = Form([])):
    if (resp := _blocked(request)):
        return resp

    def go():
        service.create_chore(name, frequency, _int(target), value_cents=service.parse_money(value or "0"),
                             icon=icon.strip() or None, description=description.strip() or None,
                             days=days or None, kids=kids, db_path=common.db_path())
    return _run("/chores/manage", go, "Chore added")


@router.post("/chores/chores/{chore_id}/update", include_in_schema=False)
def update_chore(request: Request, chore_id: str, name: str = Form(""), icon: str = Form(""),
                 value: str = Form(""), active: str = Form(""), frequency: str = Form(""),
                 target: str = Form(""), days: list[str] = Form([]), edit_schedule: str = Form(""),
                 kids: list[str] = Form([]), edit_kids: str = Form("")):
    """One form per chore card: basic fields, optional schedule edit, optional kid assignments."""
    if (resp := _blocked(request)):
        return resp

    def go():
        db = common.db_path()
        changes: dict = {}
        if name.strip():
            changes["name"] = name
        if icon.strip():
            changes["icon"] = icon.strip()
        if value.strip():
            changes["value_cents"] = service.parse_money(value)
        if active in ("0", "1"):
            changes["active"] = active == "1"
        if edit_schedule:
            changes["frequency"] = frequency
            changes["days"] = days
            if not days:
                changes["target_count"] = _int(target) or 1
        if changes:
            service.update_chore(chore_id, db_path=db, **changes)
        if edit_kids:
            current = {k["id"] for k in service.list_kids(include_inactive=True, db_path=db)
                       if k["name"] in service.get_chore(chore_id, db_path=db)["kids"]}
            for kid in service.list_kids(include_inactive=True, db_path=db):
                want = kid["id"] in kids
                if want != (kid["id"] in current):
                    service.assign(chore_id, kid["id"], assigned=want, db_path=db)
    return _run("/chores/manage", go, "Chore saved")


# ---- writes: money and backfill ----

@router.post("/chores/payout", include_in_schema=False)
def payout(request: Request, kid_id: str = Form(""), amount: str = Form(""), note: str = Form("")):
    if (resp := _blocked(request)):
        return resp
    return _run("/chores/ledger", lambda: service.payout(
        kid_id, service.parse_money(amount or "0"), note=note.strip() or None, db_path=common.db_path()),
        "Payout recorded")


@router.post("/chores/backfill", include_in_schema=False)
def backfill(request: Request, kid_id: str = Form(""), chore_id: str = Form(""), log_date: str = Form("")):
    if (resp := _blocked(request)):
        return resp
    return _run("/chores/ledger", lambda: service.complete(
        kid_id, chore_id, log_date=log_date or None, db_path=common.db_path()), "Completion recorded")


@router.post("/chores/undo", include_in_schema=False)
def undo(request: Request, kid_id: str = Form(""), chore_id: str = Form("")):
    if (resp := _blocked(request)):
        return resp
    return _run("/chores/ledger", lambda: service.undo(kid_id, chore_id, any_date=True, db_path=common.db_path()),
                "Latest completion removed")


# ---- JSON twins (same service output) ----

@router.get("/api/chores")
def api_chores():
    return data.get_overview()


@router.get("/api/chores/kids/{kid_id}")
def api_kid(kid_id: str):
    view = data.get_kid(kid_id)
    if view.get("error", "").startswith("no kid"):
        raise HTTPException(404, view["error"])
    return view


@router.get("/api/chores/ledger")
def api_ledger():
    return data.get_ledger_page()
