"""Wellness section: today's readiness, trends over time and what drives them.

Read-only. Every number comes from the shared service in scripts/wellness.py (via data/wellness.py), the
same code the `wellness` subagent's CLI runs; this module only lays that output out as charts
(dashboard/charts.py). The /api/wellness* twins return the service output unchanged.
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import charts
from ..data import wellness as data
from ..web import render

router = APIRouter()
service = data.service
fmt = service.fmt


def _f(key):
    return lambda v: fmt(key, v)


def _vals(points):
    return {p["date"]: p["value"] for p in points if p["value"] is not None}


def _status(points):
    return {p["date"]: p["status"] for p in points}


def _api(result):
    if isinstance(result, dict) and "error" in result:
        return JSONResponse(result, status_code=400 if result.get("code") == 2 else 503)
    return result


# ---------------------------------------------------------------------------
# chart composition
# ---------------------------------------------------------------------------


def _today_charts(d):
    r = d["rings"]
    rec, sl, st = r["recovery"], r["sleep"], r["strain"]
    rings = {
        "recovery": charts.ring(rec["value"], zone=rec["zone"], center=f'{rec["value"]}%' if rec["value"] is not None
                                else "", sub="estimate" if rec["source"] == "estimate" else "recovery",
                                label="Recovery", tip={"d": "Recovery", "rows": [], "note": rec["note"]}),
        "sleep": charts.ring(sl["value"], zone=sl["zone"], center=fmt("sleep_score", sl["value"]) if sl["value"]
                             is not None else "", sub=fmt("sleep_hours", sl["hours"]) if sl["hours"] else "sleep",
                             label="Sleep score", pending=sl["status"] != "final",
                             tip={"d": "Sleep score", "rows": [["Asleep", fmt("sleep_hours", sl["hours"]), ""]],
                                  "note": "" if sl["status"] == "final" else
                                  f"Last night not synced yet; showing {sl['day']}"}),
    }
    if st["value"] is None:
        rings["strain"] = charts.ring(None, label="7-day load")
    else:
        top = st["high"] * 1.25 if st["high"] else st["value"]
        rng = f'{fmt("load", st["low"])}–{fmt("load", st["high"])}' if st["high"] else ""
        rings["strain"] = charts.ring(
            st["value"], max_value=top, zone=st["zone"], center=fmt("load", st["value"]), sub="7-day load",
            label="7-day training load",
            tip={"d": "7-day training load", "rows": [["Load", fmt("load", st["value"]), ""]] +
                 ([["Recommended", rng, ""]] if rng else []),
                 "note": {"low": "Below COROS's recommended range", "high": "Above COROS's recommended range",
                          "ok": "Inside COROS's recommended range"}.get(st["zone"], f"As of {st['day']}")})
    sparks = {v["key"]: charts.spark(v.get("spark") or [], band=v.get("baseline"), fmt=_f(v["key"]),
                                     label=v["label"], cls=f"s-{v['key']}") for v in d["vitals"]}
    sparks["rating"] = charts.spark(d["mood"]["spark"], fmt=_f("rating"), label="Day rating", cls="s-rating")
    return {"rings": rings, "sparks": sparks}


def _trend_charts(t):
    s, dates = t["series"], t["dates"]
    pts = {k: v["points"] for k, v in s.items()}
    avg = {k: _vals(v["avg7"]) for k, v in s.items()}
    out = {}
    out["sleep"] = charts.timeseries(dates, [
        {"kind": "bars", "values": _vals(pts["sleep_hours"]), "status": _status(pts["sleep_hours"]), "cls": "s-sleep",
         "label": "Asleep", "fmt": _f("sleep_hours"),
         "class_of": lambda day, v, sc=_vals(pts["sleep_score"]): f"z-{service.zone(sc.get(day)) or 'none'}"},
        {"kind": "info", "values": _vals(pts["sleep_score"]), "label": "Score", "fmt": _f("sleep_score")},
        {"kind": "hline", "y": t["sleep"]["target_hours"], "cls": "target", "label": "Target", "fmt": _f("sleep_hours"),
         "tip": False},
        {"kind": "line", "values": avg["sleep_hours"], "cls": "avg", "label": "7-day avg", "fmt": _f("sleep_hours")},
    ], y_min=0, label="Hours asleep per night", y_fmt=lambda v: f"{v:g}h")
    # Sleep window: bedtime and wake time as hours after noon, so one night is one floating bar.
    bed, wake = _vals(pts["bedtime"]), {k: v + 12 for k, v in _vals(pts["wake_time"]).items()}
    out["window"] = charts.timeseries(dates, [
        {"kind": "range", "low": bed, "high": wake, "cls": "s-window", "label": "In bed", "fmt_low": _f("bedtime"),
         "fmt_high": lambda v: fmt("wake_time", v - 12)},
    ], height=150, label="Bedtime to wake time", y_fmt=lambda v: fmt("bedtime", v).replace(":00", ""),
        y_ticks=[8, 10, 12, 14, 16, 18, 20])  # 8 PM to 8 AM, every 2 hours
    st = t["stages"]
    out["stages"] = charts.timeseries(dates, [
        {"kind": "stack", "fmt": lambda v: f"{v:.0f}%", "parts": [
            ("Deep", {x["date"]: x["deep"] for x in st}, "s-deep"), ("REM", {x["date"]: x["rem"] for x in st}, "s-rem"),
            ("Light", {x["date"]: x["light"] for x in st}, "s-light"),
            ("Awake", {x["date"]: x["awake"] for x in st}, "s-awake")]},
    ], y_min=0, y_max=100, height=150, label="Sleep stages per night", y_fmt=lambda v: f"{v:g}%")
    out["score"] = charts.timeseries(dates, [
        {"kind": "zones", "zones": [(67, 100, "zone-ok", "Good"), (34, 67, "zone-low", "Fair"), (0, 34, "zone-high", "Poor")]},
        {"kind": "line", "values": avg["sleep_score"], "cls": "avg", "label": "7-day avg", "fmt": _f("sleep_score")},
        {"kind": "line", "values": _vals(pts["sleep_score"]), "status": _status(pts["sleep_score"]), "cls": "s-score",
         "dots": True, "label": "Score", "fmt": _f("sleep_score"),
         "class_of": lambda day, v: f"z-{service.zone(v)}"},
    ], y_max=100, label="Sleep score per night with 7-day average", y_fmt=lambda v: f"{v:g}")
    band = t["hrv_band"]
    hrv_band_side = {p["date"]: p.get("band") for p in pts["hrv"]}
    out["hrv"] = charts.timeseries(dates, [
        {"kind": "band", "low": {b["date"]: b["low"] for b in band}, "high": {b["date"]: b["high"] for b in band},
         "cls": "s-hrv", "label": "Normal range", "fmt": _f("hrv")},
        {"kind": "line", "values": avg["hrv"], "cls": "avg", "label": "7-day avg", "fmt": _f("hrv"), "dashed": True},
        {"kind": "line", "values": _vals(pts["hrv"]), "status": _status(pts["hrv"]), "cls": "s-hrv", "dots": True,
         "label": "HRV", "fmt": _f("hrv"), "class_of": lambda day, v: f"b-{hrv_band_side.get(day) or 'none'}"},
    ], label="Overnight HRV with normal range", y_fmt=lambda v: f"{v:g}")
    rb = t["rhr_band"]
    out["rhr"] = charts.timeseries(dates, [
        {"kind": "band", "low": {b["date"]: b["low"] for b in rb}, "high": {b["date"]: b["high"] for b in rb},
         "cls": "s-rhr", "label": "30-day normal", "fmt": _f("resting_hr")},
        {"kind": "line", "values": _vals(pts["resting_hr"]), "status": _status(pts["resting_hr"]), "cls": "s-rhr",
         "dots": True, "label": "Resting HR", "fmt": _f("resting_hr")},
        {"kind": "line", "values": _vals(pts["sleep_hr"]), "cls": "s-sleephr", "label": "Sleeping HR",
         "fmt": _f("sleep_hr"), "dashed": True},
    ], label="Resting heart rate with 30-day normal range", y_fmt=lambda v: f"{v:g}")
    out["load"] = charts.timeseries(dates, [
        {"kind": "bars", "values": _vals(pts["load"]), "status": _status(pts["load"]), "cls": "s-load",
         "label": "Daily load", "fmt": _f("load")},
        {"kind": "line", "values": _vals(pts["ati"]), "cls": "s-ati", "label": "Acute (ATI)", "fmt": _f("ati")},
        {"kind": "line", "values": _vals(pts["cti"]), "cls": "s-cti", "label": "Fitness (CTI)", "fmt": _f("cti")},
    ], y_min=0, label="Daily training load with acute and chronic load")
    out["ratio"] = charts.timeseries(dates, [
        {"kind": "zones", "zones": [(z["low"], z["high"], f"zone-{z['zone']}", z["label"])
                                    for z in t["load_ratio_zones"]]},
        {"kind": "line", "values": _vals(pts["load_ratio"]), "status": _status(pts["load_ratio"]), "cls": "s-ratio",
         "dots": True, "label": "Load ratio", "fmt": _f("load_ratio")},
    ], y_min=0, y_max=2, height=120, label="Acute to chronic load ratio", y_fmt=lambda v: f"{v:g}")
    out["mood"] = charts.timeseries(dates, [
        {"kind": "line", "values": avg["rating"], "cls": "avg", "label": "7-day avg", "fmt": _f("rating")},
        {"kind": "line", "values": _vals(pts["rating"]), "status": _status(pts["rating"]), "cls": "s-rating",
         "dots": True, "label": "Rating", "fmt": _f("rating"),
         "class_of": lambda day, v: "r-hi" if v >= 8 else "r-lo" if v <= 4 else ""},
    ], y_min=1, y_max=10, label="Day rating with 7-day average", y_fmt=lambda v: f"{v:g}")
    wd = t["rating_by_weekday"]
    out["weekday"] = charts.category_bars(
        [{"label": w["day"], "value": w["avg"], "tip": f"{w['n']} rated days"} for w in wd],
        cls="s-rating", fmt=lambda v: f"{v:.1f}", label="Average rating", y_max=10, height=130)
    aw = t["activity_weeks"]
    out["steps"] = charts.category_bars(
        [{"label": charts._day_label(w["week"]), "value": w["value"], "title": f"Week of {charts._day_label(w['week'])}",
          "tip": f"{w['days']} days"} for w in aw["steps"]][-13:],
        cls="s-steps", fmt=lambda v: f"{v / 1000:.1f}k" if v >= 1000 else f"{v:.0f}", label="Steps per day",
        height=130)
    out["exercise"] = charts.category_bars(
        [{"label": charts._day_label(w["week"]), "value": w["value"], "title": f"Week of {charts._day_label(w['week'])}",
          "tip": f"{w['days']} days"} for w in aw["exercise"]][-13:],
        cls="s-exercise", fmt=lambda v: f"{v:.0f} min", label="Exercise per week", height=130)
    return out


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------


@router.get("/wellness", include_in_schema=False)
def today_page(request: Request):
    d = data.get_today()
    return render(request, "wellness/today.html", "wellness", "today", "Wellness",
                  data=d, charts=_today_charts(d) if "error" not in d else None, fmt=fmt)


@router.get("/wellness/trends", include_in_schema=False)
def trends_page(request: Request, range: str = "90", heat: str = "rating"):
    t = data.get_trends(range)
    h = data.get_heatmap(heat)
    return render(request, "wellness/trends.html", "wellness", "trends", "Wellness trends",
                  data=t, heat=h, charts=_trend_charts(t) if "error" not in t else None,
                  heatmap=charts.heatmap(h["weeks"], label=h["label"]) if "error" not in h else None, fmt=fmt)


@router.get("/wellness/drivers", include_in_schema=False)
def drivers_page(request: Request, target: str = "rating", x: str = "sleep_hours", y: str = "rating",
                 days: int = 90):
    d = data.get_drivers(target, days)
    sc = data.get_scatter(x, y, days)
    plot = None
    if "error" not in sc:
        plot = charts.scatter(sc["points"], fit=sc["fit"], x_fmt=_f(x), y_fmt=_f(y), x_label=sc["x"]["label"],
                              y_label=sc["y"]["label"])
    top = max((abs(f["correlation"]) for f in d.get("findings", [])), default=1) or 1
    return render(request, "wellness/drivers.html", "wellness", "drivers", "Wellness drivers",
                  data=d, scatter=sc, plot=plot, top=top, fmt=fmt)


# ---------------------------------------------------------------------------
# JSON twins
# ---------------------------------------------------------------------------


@router.get("/api/wellness")
def api_today():
    return _api(data.get_today())


@router.get("/api/wellness/trends")
def api_trends(range: str = "90"):
    return _api(data.get_trends(range))


@router.get("/api/wellness/heatmap")
def api_heatmap(metric: str = "rating"):
    return _api(data.get_heatmap(metric))


@router.get("/api/wellness/drivers")
def api_drivers(target: str = "rating", days: int = 90):
    return _api(data.get_drivers(target, days))


@router.get("/api/wellness/scatter")
def api_scatter(x: str = "sleep_hours", y: str = "rating", days: int = 90):
    return _api(data.get_scatter(x, y, days))
