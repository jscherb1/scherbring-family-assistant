"""Rivian section: the R2's live status and its trends, under Home > Home Assistant. View-only.

Every number comes from the shared service in scripts/rivian.py (via data/rivian.py), the same code the
`ha-overview` subagent's CLI runs; this module only lays that output out as charts (dashboard/charts.py).
The /api/ha/rivian twin returns the service output unchanged. There are no write routes: nothing here
controls the car.
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from .. import charts
from ..data import rivian as data
from ..web import render

router = APIRouter()
service = data.service


def _n(v):
    return f"{v:.1f}".rstrip("0").rstrip(".")


def _charts(t: dict) -> dict:
    """SVG charts from trends(). Presentation only."""
    dates, soc = t["dates"], t["soc"]
    pct = lambda v: f"{_n(v)}%"  # noqa: E731
    mi = lambda v: f"{_n(v)} mi"  # noqa: E731
    out = {
        "soc": charts.timeseries(dates, [
            {"kind": "band", "low": soc["min"], "high": soc["max"], "cls": "s-soc", "label": "Daily range", "fmt": pct},
            {"kind": "line", "values": soc["mean"], "cls": "s-soc", "label": "Average", "fmt": pct, "dots": True},
            {"kind": "hline", "y": t["limit"], "cls": "target", "label": "Charge limit", "fmt": pct},
        ], y_min=0, y_max=100, y_fmt=lambda v: f"{int(v)}%", label="Battery state of charge", sync="rivian"),
        "miles": charts.timeseries(dates, [
            {"kind": "bars", "values": t["miles"], "cls": "s-miles", "label": "Miles driven", "fmt": mi},
            {"kind": "info", "values": t["kwh_used"], "label": "Battery used", "fmt": lambda v: f"{_n(v)} kWh"},
            {"kind": "info", "values": t["efficiency"], "label": "Efficiency (est.)", "fmt": lambda v: f"{_n(v)} mi/kWh"},
        ], height=150, label="Miles driven per day", sync="rivian"),
        "range": charts.timeseries(dates, [
            {"kind": "line", "values": t["range_mi"], "cls": "s-range", "label": "Estimated range", "fmt": mi,
             "dots": True},
        ], height=150, y_min=0, label="Estimated range", sync="rivian"),
        "efficiency": charts.spark([{"date": d, "value": t["efficiency"].get(d)} for d in dates], cls="s-eff",
                                   fmt=lambda v: f"{_n(v)} mi/kWh", label="Efficiency (est.)"),
    }
    return out


@router.get("/home/ha/rivian", include_in_schema=False)
def rivian_page(request: Request, days: int = 7):
    days = days if days in service.TREND_DAYS else 7
    status = data.get_status()
    trends = data.get_trends(days) if "error" not in status else {"error": status["error"]}
    ring = None
    if "error" not in status:
        e = status["energy"]
        ring = charts.ring(e["soc"], zone=e["zone"], center=e["soc_display"],
                           sub=f"limit {e['limit']}%" if e["limit"] is not None else "charge",
                           label="State of charge", marker=e["limit"])
    return render(request, "rivian/status.html", "rivian", "status", "Rivian R2", data=status, trends=trends,
                  days=days, ring=ring, charts=_charts(trends) if "error" not in trends else None)


@router.get("/home/ha/rivian/image/{view}", include_in_schema=False)
def rivian_image(view: str):
    try:
        body, content_type = service.image(view)
    except service.RivianError as exc:
        return JSONResponse({"error": exc.message}, status_code=404 if exc.code == 2 else 503)
    return Response(body, media_type=content_type or "image/png", headers={"Cache-Control": "max-age=3600"})


@router.get("/api/ha/rivian")
def api_rivian(days: int = 30):
    status = data.get_status()
    if "error" in status:
        return JSONResponse(status, status_code=503 if status.get("code") == 1 else 400)
    trends = data.get_trends(days)
    if "error" in trends:
        return JSONResponse(trends, status_code=400 if trends.get("code") == 2 else 503)
    return {"status": status, "trends": trends}
