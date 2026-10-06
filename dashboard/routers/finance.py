"""Finance section: tagging activity and report/planning status (summaries only)."""
from fastapi import APIRouter, Request

from ..data import finance
from ..web import render

router = APIRouter()


@router.get("/finance", include_in_schema=False)
def tagging(request: Request):
    return render(request, "finance/tagging.html", "finance", "tagging", "Transaction Tagging",
                  data=finance.get_tagging())


@router.get("/finance/reports", include_in_schema=False)
def reports(request: Request):
    return render(request, "finance/reports.html", "finance", "reports", "Reports & Planning",
                  data=finance.get_reports())


@router.get("/api/finance/tagging")
def api_tagging():
    return finance.get_tagging()


@router.get("/api/finance/reports")
def api_reports():
    return finance.get_reports()
