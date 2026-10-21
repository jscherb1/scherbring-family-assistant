"""Money section: the overview (budget, spending, net worth, review queue), tagging, reports & planning.

The overview and the live review counts come from the shared service in scripts/money.py (via
data/money.py) over the local Monarch snapshot; the tagging-history and reports summaries come from
data/finance.py. The only write is Refresh, which starts a quick snapshot sync; it is guarded by the
same-origin check the ask box uses.
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ..data import finance
from ..data import money as money_data
from ..web import render
from .ask import _same_origin

router = APIRouter()


@router.get("/finance", include_in_schema=False)
def overview(request: Request, refreshing: str = ""):
    return render(request, "finance/overview.html", "finance", "home", "Money",
                  data=money_data.get_overview(), refreshing=bool(refreshing))


@router.get("/finance/tagging", include_in_schema=False)
def tagging(request: Request):
    return render(request, "finance/tagging.html", "finance", "tagging", "Transaction Tagging",
                  data=finance.get_tagging(), queue=money_data.get_review_queue())


@router.get("/finance/reports", include_in_schema=False)
def reports(request: Request):
    return render(request, "finance/reports.html", "finance", "reports", "Reports & Planning",
                  data=finance.get_reports())


@router.post("/finance/refresh", include_in_schema=False)
def refresh(request: Request):
    if not _same_origin(request):
        return JSONResponse({"error": "Blocked: the request didn't come from this site."}, status_code=403)
    started = money_data.start_refresh()
    return RedirectResponse("/finance?refreshing=1" if started else "/finance", status_code=303)


@router.get("/api/finance/overview")
def api_overview():
    return money_data.get_overview()


@router.get("/api/finance/tagging")
def api_tagging():
    return {**finance.get_tagging(), "queue": money_data.get_review_queue()}


@router.get("/api/finance/reports")
def api_reports():
    return finance.get_reports()
