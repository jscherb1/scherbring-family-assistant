"""Training page: running volume and heart-rate-zone time from COROS.

Read-only. Every number comes from the shared service in scripts/training.py (via data/training.py), the same
code the `training` subagent's CLI runs; this module only lays that output out as charts (dashboard/charts.py).
The sync runs from the CLI/scheduler, so there are no write routes. The /api/training* twins return the
service output unchanged.
"""
from datetime import date, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from .. import charts
from ..data import training as data
from ..web import render

router = APIRouter()

RANGES = (30, 90, 180, 365)
BUCKETS = ("day", "week", "month")
METRICS = ("distance", "time")
ZONE_CLS = [f"tz tz{i}" for i in range(6)]
SYNC_COMMAND = "python scripts/training_store.py sync"


def _api(result):
    if isinstance(result, dict) and "error" in result:
        return JSONResponse(result, status_code=400 if result.get("code") == 2 else 503)
    return result


def _split(values):
    """sports=running&sports=cycling or sports=running,cycling -> ['running', 'cycling'] (None when empty)."""
    out = []
    for v in values or []:
        out += [p.strip() for p in str(v).split(",") if p.strip()]
    return out or None


# ---------------------------------------------------------------------------
# formatting (presentation only)
# ---------------------------------------------------------------------------


def _dur(seconds):
    if seconds is None:
        return "-"
    m = round(seconds / 60)
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def _hours(v):
    return f"{v:.1f}h".replace(".0h", "h")


def _pace(sec_per_mi):
    if not sec_per_mi:
        return "-"
    m, s = divmod(round(sec_per_mi), 60)
    return f"{m}:{s:02d}"


def _mi(v):
    return f"{v:.1f}".rstrip("0").rstrip(".") if v is not None else "-"


def _pct(v):
    return "-" if v is None else f"{v:.0f}%"


def _bpm(z):
    lo, hi = z.get("low"), z.get("high")
    if lo is not None and hi is not None:
        return f"{lo}-{hi} bpm"
    if lo is not None:
        return f"{lo}+ bpm"
    if hi is not None:
        return f"under {hi} bpm"
    return ""


def _zone_legend(defs):
    """Z0..Z5 with bpm ranges, e.g. {"name": "Z2", "text": "Z2 155-162 bpm", "cls": "tz tz2"}."""
    zones = defs["zones"] if isinstance(defs, dict) and "zones" in defs else [{"zone": i} for i in range(6)]
    return [{"name": f"Z{z['zone']}", "range": _bpm(z), "cls": ZONE_CLS[z["zone"]],
             "text": f"Z{z['zone']} {_bpm(z)}".strip()} for z in zones]


# ---------------------------------------------------------------------------
# query state -> links
# ---------------------------------------------------------------------------


class Query_:
    """The page's query state, with helpers to build links that keep it."""

    def __init__(self, metric, range_, start, end, bucket, sports, default_sports):
        self.metric, self.range, self.start, self.end, self.bucket = metric, range_, start, end, bucket
        self.sports = sports                      # explicit selection from the URL, or None
        self.selected = sports or default_sports  # what the chips show

    def href(self, **over):
        d = {"metric": self.metric, "range": self.range, "start": self.start, "end": self.end,
             "bucket": self.bucket, "sports": self.sports}
        d.update(over)
        if d["start"] or d["end"]:
            d["range"] = ""
        pairs = [(k, v) for k, v in d.items() if v and k != "sports"]
        pairs += [("sports", s) for s in (d["sports"] or [])]
        defaults = {("metric", "distance"), ("bucket", "week")}
        pairs = [p for p in pairs if p not in defaults]
        return "/training" + ("?" + urlencode(pairs) if pairs else "")


def _window(range_, start, end, today):
    """(start, end, error) to pass to the service; range presets are rolling windows ending today."""
    if start or end:
        return start or None, end or None, None
    if range_:
        try:
            n = int(range_)
        except ValueError:
            n = 0
        if n not in RANGES:
            return None, None, f"Range must be one of {', '.join(map(str, RANGES))} days."
        return (today - timedelta(days=n - 1)).isoformat(), today.isoformat(), None
    return None, None, None


def _weekly_default_start(today):
    return (today - timedelta(days=today.weekday()) - timedelta(weeks=11)).isoformat()


# ---------------------------------------------------------------------------
# chart composition
# ---------------------------------------------------------------------------


def _labels(rows):
    return {r["bucket_start"]: r["label"] for r in rows}


def _volume_chart(v, metric):
    rows = v["rows"]
    dates = [r["bucket_start"] for r in rows]
    if metric == "distance":
        vals = {r["bucket_start"]: r["distance_mi"] for r in rows}
        avg = {r["bucket_start"]: r["rolling_avg"] for r in rows}
        f, yf, name = (lambda x: f"{_mi(x)} mi"), (lambda x: f"{x:g}"), "Miles per week"
    else:
        vals = {r["bucket_start"]: r["time_s"] / 3600 for r in rows}
        avg = {r["bucket_start"]: r["rolling_avg"] / 3600 for r in rows}
        f, yf, name = _hours, (lambda x: f"{x:g}h"), "Hours per week"
    labels = {r["bucket_start"]: f"Wk of {charts._day_label(r['bucket_start'])}" for r in rows}
    return charts.timeseries(dates, [
        {"kind": "bars", "values": vals, "cls": "s-load", "label": name.split(" per")[0], "fmt": f},
        {"kind": "line", "values": avg, "cls": "avg", "label": "4-week avg", "fmt": f},
    ], y_min=0, y_fmt=yf, label=f"{name}, with 4-week rolling average", labels=labels, sync="tr-volume")


def _nice_top(max_value):
    ticks = charts._nice_ticks(0, max_value or 1)
    return ticks[-1]


def _zone_time_chart(z, unit_hours):
    rows = z["rows"]
    dates = [r["bucket_start"] for r in rows]
    div = 3600 if unit_hours else 60
    f = (lambda x: _hours(x)) if unit_hours else (lambda x: f"{x:.0f} min")
    top = _nice_top(max((r["total_s"] / div for r in rows), default=0))
    parts = [(f"Z{i}", {r["bucket_start"]: r["seconds"][i] / div for r in rows}, ZONE_CLS[i]) for i in range(6)]
    return charts.timeseries(dates, [{"kind": "stack", "parts": parts, "fmt": f}], y_min=0, y_max=top,
                             y_fmt=(lambda x: f"{x:g}h") if unit_hours else (lambda x: f"{x:g}m"),
                             label=f"Time in each heart-rate zone per {z['bucket']}", labels=_labels(rows),
                             sync="tr-zones")


def _share_chart(s):
    rows = s["rows"]
    dates = [r["bucket_start"] for r in rows]
    parts = [(f"Z{i}", {r["bucket_start"]: r["percent"][i] for r in rows if r["percent"]}, ZONE_CLS[i])
             for i in range(6)]
    status = {r["bucket_start"]: "final" if r["percent"] else "nodata" for r in rows}
    return charts.timeseries(dates, [
        {"kind": "info", "values": {}, "status": status},
        {"kind": "stack", "parts": parts, "fmt": lambda x: f"{x:.0f}%"},
    ], y_min=0, y_max=100, y_fmt=lambda x: f"{x:g}%", label=f"Share of time in each zone per {s['bucket']}",
        labels=_labels(rows), sync="tr-share")


def _easy_chart(e):
    rows = e["rows"]
    dates = [r["bucket_start"] for r in rows]
    status = {r["bucket_start"]: "final" if r["easy_pct"] is not None else "nodata" for r in rows}
    pf = lambda x: f"{x:.0f}%"  # noqa: E731
    return charts.timeseries(dates, [
        {"kind": "info", "values": {}, "status": status},
        {"kind": "line", "values": {r["bucket_start"]: r["easy_pct"] for r in rows if r["easy_pct"] is not None},
         "cls": "s-ratio", "dots": True, "label": "Easy (Z0-Z2)", "fmt": pf},
        {"kind": "line", "values": {r["bucket_start"]: r["z1_z2_pct"] for r in rows if r["z1_z2_pct"] is not None},
         "cls": "s-ati", "dots": True, "dashed": True, "label": "Z1-Z2 only", "fmt": pf},
    ], y_min=0, y_max=100, y_fmt=lambda x: f"{x:g}%", label="Easy share of heart-rate time per week",
        labels={r["bucket_start"]: f"Wk of {charts._day_label(r['bucket_start'])}" for r in rows}, sync="tr-easy")


def _longest_chart(lg, metric):
    rows = lg["rows"]
    dates = [r["bucket_start"] for r in rows]
    key = "longest_distance" if metric == "distance" else "longest_time"
    vals = {}
    for r in rows:
        b = r[key]
        if b:
            vals[r["bucket_start"]] = b["distance_mi"] if metric == "distance" else (b["moving_s"] or 0) / 3600
    f = (lambda x: f"{_mi(x)} mi") if metric == "distance" else _hours
    return charts.timeseries(dates, [
        {"kind": "bars", "values": vals, "cls": "s-ati", "label": "Longest run", "fmt": f}],
        y_min=0, y_fmt=(lambda x: f"{x:g}") if metric == "distance" else (lambda x: f"{x:g}h"),
        label=f"Longest run per week by {'distance' if metric == 'distance' else 'time'}",
        labels={r["bucket_start"]: f"Wk of {charts._day_label(r['bucket_start'])}" for r in rows}, sync="tr-longest")


def _summary_text(parts):
    return " ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# page
# ---------------------------------------------------------------------------


def _first_error(*results):
    return next((r for r in results if isinstance(r, dict) and "error" in r), None)


@router.get("/training", include_in_schema=False)
def training_page(request: Request, metric: str = "distance", range: str = "", start: str = "", end: str = "",
                  bucket: str = "week", sports: list[str] = Query(default=[])):
    today = date.today()
    sports_list = _split(sports)
    avail = data.get_sports()
    if isinstance(avail, dict):  # database/service failure
        return render(request, "training/overview.html", "training", "overview", "Training", fatal=avail["error"])
    total_count = sum(s["count"] for s in avail)
    defaults = [s["group"] for s in avail if s["default"]]
    q = Query_(metric, range, start, end, bucket, sports_list, defaults)

    ctx = dict(avail=avail, q=q, sync_command=SYNC_COMMAND, empty=total_count == 0, input_error=None, fatal=None,
               ranges=[], metrics=[], buckets=[], chips=[], custom=False)
    # controls are always rendered, even when the input is bad, so it can be fixed in place
    ctx["ranges"] = [{"label": f"{n} days", "href": q.href(range=str(n), start="", end=""),
                      "active": (q.range == str(n)) or (not q.range and not (start or end) and n == 30)}
                     for n in RANGES]
    ctx["custom"] = bool(start or end)
    ctx["metrics"] = [{"label": lab, "href": q.href(metric=m), "active": q.metric == m}
                      for m, lab in (("distance", "Miles"), ("time", "Time"))]
    ctx["buckets"] = [{"label": b.capitalize() + "s" if b != "day" else "Days", "href": q.href(bucket=b),
                       "active": q.bucket == b} for b in BUCKETS]
    ctx["chips"] = [{"label": s["label"], "count": s["count"], "on": s["group"] in q.selected,
                     "href": q.href(sports=[g for g in [x["group"] for x in avail]
                                            if (g in q.selected) != (g == s["group"])])} for s in avail]

    if ctx["empty"]:
        return render(request, "training/overview.html", "training", "overview", "Training", **ctx)
    if metric not in METRICS or bucket not in BUCKETS:
        ctx["input_error"] = f"Unknown {'metric' if metric not in METRICS else 'bucket'}; use the links above."
        return render(request, "training/overview.html", "training", "overview", "Training", **ctx)
    s_, e_, err = _window(range, start, end, today)
    if err:
        ctx["input_error"] = err
        return render(request, "training/overview.html", "training", "overview", "Training", **ctx)

    chosen = bool(s_ or e_)
    vol_start = s_ if chosen else None
    weekly_start = s_ if chosen else _weekly_default_start(today)
    sp = sports_list
    summ = data.get_summary(sp, s_, e_)
    vol = data.get_volume(metric, sp, vol_start, e_)
    tot = data.get_zone_time("total", sp, s_, e_)
    zt = data.get_zone_time(bucket, sp, s_, e_)
    zs = data.get_zone_share(bucket, sp, s_, e_)
    easy = data.get_easy_share("week", sp, weekly_start, e_)
    longest = data.get_longest(sp, vol_start, e_)
    acts = data.get_activities(sp, s_, e_, limit=15)
    defs = data.get_zone_definitions()
    err = _first_error(summ, vol, tot, zt, zs, easy, longest, acts, defs)
    if err:
        if err.get("code") == 2:
            ctx["input_error"] = err["error"]
        else:
            ctx["fatal"] = err["error"]
        return render(request, "training/overview.html", "training", "overview", "Training", **ctx)

    legend = _zone_legend(defs)
    total_s = tot["rows"][0]["total_s"] if tot["rows"] else 0
    zone_rows = [{"zone": legend[i], "seconds": sec, "minutes": round(sec / 60),
                  "pct": (sec / total_s * 100) if total_s else 0}
                 for i, sec in enumerate(tot["rows"][0]["seconds"] if tot["rows"] else [0] * 6)]
    unit_hours = bucket != "day" and (max((r["total_s"] for r in zt["rows"]), default=0) >= 6 * 3600)
    no_zone_buckets = sum(1 for r in zs["rows"] if not r["percent"])
    ctx.update(
        summary=summ, volume=vol, legend=legend, zone_rows=zone_rows, zone_total_s=total_s, activities=acts,
        zone_window=f"{tot['start']} to {tot['end']}", no_zone_buckets=no_zone_buckets,
        charts={"volume": _volume_chart(vol, metric), "zones": _zone_time_chart(zt, unit_hours),
                "share": _share_chart(zs), "easy": _easy_chart(easy), "longest": _longest_chart(longest, metric)},
        easy=easy, unit_hours=unit_hours, chosen=chosen,
        fmt=dict(dur=_dur, mi=_mi, pct=_pct, pace=_pace),
        sel_label=summ["sports"] if isinstance(summ["sports"], str) else ", ".join(summ["sports"]),
    )
    return render(request, "training/overview.html", "training", "overview", "Training", **ctx)


# ---------------------------------------------------------------------------
# JSON twins
# ---------------------------------------------------------------------------


@router.get("/api/training")
def api_training(sports: list[str] = Query(default=[]), start: str = "", end: str = ""):
    sp = _split(sports)
    out = {"summary": data.get_summary(sp, start or None, end or None), "sports": data.get_sports(),
           "config": data.get_config(), "zones": data.get_zone_definitions()}
    return _api(_first_error(*out.values()) or out)


@router.get("/api/training/volume")
def api_volume(metric: str = "distance", sports: list[str] = Query(default=[]), start: str = "", end: str = ""):
    return _api(data.get_volume(metric, _split(sports), start or None, end or None))


@router.get("/api/training/zones")
def api_zones(bucket: str = "total", share: int = 0, sports: list[str] = Query(default=[]), start: str = "",
              end: str = ""):
    fn = data.get_zone_share if share else data.get_zone_time
    return _api(fn(bucket, _split(sports), start or None, end or None))


@router.get("/api/training/share")
def api_share(bucket: str = "week", sports: list[str] = Query(default=[]), start: str = "", end: str = ""):
    return _api(data.get_zone_share(bucket, _split(sports), start or None, end or None))


@router.get("/api/training/easy")
def api_easy(bucket: str = "week", sports: list[str] = Query(default=[]), start: str = "", end: str = ""):
    return _api(data.get_easy_share(bucket, _split(sports), start or None, end or None))


@router.get("/api/training/longest")
def api_longest(sports: list[str] = Query(default=[]), start: str = "", end: str = ""):
    return _api(data.get_longest(_split(sports), start or None, end or None))


@router.get("/api/training/activities")
def api_activities(sports: list[str] = Query(default=[]), start: str = "", end: str = "", limit: int = 50):
    return _api(data.get_activities(_split(sports), start or None, end or None, limit=max(1, min(limit, 500))))
