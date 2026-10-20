"""Server-rendered SVG charts for the dashboard (Wellness first).

Presentation only: these functions draw numbers they're given and hold no business rules. Colours come
only from CSS classes (static/wellness.css), so every chart follows the light/dark theme. Interactivity is
progressive: each day/point carries a `data-tip` JSON payload and a `<title>` fallback, and
static/wellness.js turns those into hover tooltips, a shared crosshair and tap-to-pin readouts on phones.

Time-series charts are built from layers drawn in order over one shared date axis:
    {"kind": "bars",  "values": {day: v}, "cls": "s-sleep", "label": "Sleep", "fmt": f, "status": {day: st},
                      "class_of": f(day, v) -> extra class}
    {"kind": "line",  "values": {day: v}, "cls": ..., "label": ..., "fmt": f, "dots": bool, "dashed": bool,
                      "status": {...}, "class_of": ...}
    {"kind": "band",  "low": {day: v}, "high": {day: v}, "cls": ..., "label": ..., "fmt": f}
    {"kind": "range", "low": {day: v}, "high": {day: v}, "cls": ..., "label": ..., "fmt_low": f, "fmt_high": f}
    {"kind": "stack", "parts": [(label, {day: v}, cls)], "fmt": f}
    {"kind": "hline", "y": v, "cls": ..., "label": ...}
    {"kind": "zones", "zones": [(low, high, cls, label)]}
    {"kind": "info",  "values": {day: v}, "label": ..., "fmt": f}   (tooltip row only, nothing drawn)
The first layer with a "status" map decides how pending/partial days are marked.
"""
from __future__ import annotations

import json
import math
from datetime import date
from html import escape

from markupsafe import Markup

W = 480                                     # viewBox width shared by every time-series chart
PAD_L, PAD_R, PAD_T, PAD_B = 38, 8, 10, 22
NOTES = {"pending": "Not synced yet", "partial": "Day in progress", "missing": "No data"}


def _n(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


def _tip(obj) -> str:
    """The data-tip attribute: compact JSON in single quotes (far smaller than escaping every double quote)."""
    body = escape(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), quote=False).replace("'", "&#39;")
    return "data-tip='" + body + "'"


def _day_label(d: str, long: bool = False) -> str:
    dt = date.fromisoformat(d)
    return dt.strftime("%a, %b %-d" if long else "%b %-d")


def _nice_ticks(lo: float, hi: float, n: int = 4) -> list[float]:
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw)
    start = math.floor(lo / step) * step
    ticks, v = [], start
    while v <= hi + step * 0.001:
        ticks.append(round(v, 6))
        v += step
    if ticks[-1] < hi:
        ticks.append(round(ticks[-1] + step, 6))
    return ticks


def _domain(layers, y_min, y_max):
    vals = []
    for ly in layers:
        k = ly["kind"]
        if k in ("bars", "line"):
            vals += [v for v in ly["values"].values() if v is not None]
        elif k in ("band", "range"):
            vals += [v for v in list(ly["low"].values()) + list(ly["high"].values()) if v is not None]
        elif k == "hline" and ly.get("y") is not None:
            vals.append(ly["y"])
        elif k == "stack":
            vals.append(100)
    if not vals:
        vals = [0, 1]
    lo = min(vals) if y_min is None else y_min
    hi = max(vals) if y_max is None else y_max
    if y_min is None and any(ly["kind"] in ("bars", "stack") for ly in layers):
        lo = min(lo, 0)
    if y_min is None and lo == hi:
        lo -= 1
    pad = (hi - lo) * 0.06
    lo2 = lo if y_min is not None or lo == 0 else lo - pad
    ticks = _nice_ticks(lo2, hi + (0 if y_max is not None else pad))
    if y_min is not None:
        ticks = [t for t in ticks if t >= y_min] or [y_min]
    if y_max is not None:
        ticks = [t for t in ticks if t <= y_max]
        if ticks[-1] < y_max:
            ticks.append(y_max)
    return ticks[0], ticks[-1], ticks


def timeseries(dates: list[str], layers: list[dict], *, height: int = 170, y_min=None, y_max=None,
               y_fmt=_n, label: str = "", sync: str = "trend", y_ticks: list[float] | None = None) -> Markup:
    """A date-axis chart from layers (see module docstring). Returns an inline <svg>. `y_ticks` fixes the
    gridlines (and the domain to their span) for axes that need round values, like clock times."""
    n = len(dates)
    plot_w, plot_h = W - PAD_L - PAD_R, height - PAD_T - PAD_B
    lo, hi, ticks = (y_ticks[0], y_ticks[-1], y_ticks) if y_ticks else _domain(layers, y_min, y_max)
    slot = plot_w / max(n, 1)

    def x(i):
        return PAD_L + (i + 0.5) * slot

    def y(v):
        v = max(lo, min(hi, v))
        return PAD_T + plot_h - (v - lo) / (hi - lo) * plot_h

    out = [f'<svg class="wchart" viewBox="0 0 {W} {height}" role="img" aria-label="{escape(label)}" '
           f'data-sync="{escape(sync)}" preserveAspectRatio="xMidYMid meet">']
    for t in ticks:
        out.append(f'<line class="wgl" x1="{PAD_L}" x2="{W - PAD_R}" y1="{y(t):.1f}" y2="{y(t):.1f}"/>'
                   f'<text class="ylab" x="{PAD_L - 5}" y="{y(t) + 4:.1f}" text-anchor="end">{escape(y_fmt(t))}</text>')
    step = max(1, round(n / 6))
    for j, i in enumerate(range(n - 1, -1, -step)):
        out.append(f'<text class="xlab{" xl-alt" if j % 2 else ""}" x="{x(i):.1f}" y="{height - 6}" '
                   f'text-anchor="middle">{_day_label(dates[i])}</text>')

    status = next((ly["status"] for ly in layers if ly.get("status")), {})
    bar_w = max(1.0, slot * 0.72)
    for ly in layers:
        k, cls = ly["kind"], ly.get("cls", "")
        if k == "zones":
            for zlo, zhi, zcls, _ in ly["zones"]:
                if zhi < lo or zlo > hi:
                    continue
                y1, y2 = y(min(zhi, hi)), y(max(zlo, lo))
                out.append(f'<rect class="zone {zcls}" x="{PAD_L}" y="{y1:.1f}" width="{plot_w}" height="{y2 - y1:.1f}"/>')
        elif k == "hline" and ly.get("y") is not None:
            out.append(f'<line class="hline {cls}" x1="{PAD_L}" x2="{W - PAD_R}" y1="{y(ly["y"]):.1f}" '
                       f'y2="{y(ly["y"]):.1f}"/>')
        elif k == "band":
            for seg in _segments(dates, lambda d: (ly["low"].get(d), ly["high"].get(d))):
                top = " ".join(f"{x(i):.1f},{y(h):.1f}" for i, (_, h) in seg)
                bot = " ".join(f"{x(i):.1f},{y(lw):.1f}" for i, (lw, _) in reversed(seg))
                out.append(f'<polygon class="band {cls}" points="{top} {bot}"/>')
        elif k == "bars":
            for i, d in enumerate(dates):
                v = ly["values"].get(d)
                if v is None:
                    continue
                extra = ly["class_of"](d, v) if ly.get("class_of") else ""
                st = (ly.get("status") or {}).get(d, "final")
                y0, y1 = y(max(lo, 0)), y(v)
                out.append(f'<rect class="wbar {cls} {extra} {st}" x="{x(i) - bar_w / 2:.1f}" y="{min(y0, y1):.1f}" '
                           f'width="{bar_w:.1f}" height="{max(abs(y0 - y1), 0.5):.1f}" rx="{min(2, bar_w / 3):.1f}"/>')
        elif k == "range":
            for i, d in enumerate(dates):
                a, b = ly["low"].get(d), ly["high"].get(d)
                if a is None or b is None:
                    continue
                y1, y2 = y(max(a, b)), y(min(a, b))
                out.append(f'<rect class="wbar {cls}" x="{x(i) - bar_w / 2:.1f}" y="{y1:.1f}" width="{bar_w:.1f}" '
                           f'height="{max(y2 - y1, 0.5):.1f}" rx="{min(2, bar_w / 3):.1f}"/>')
        elif k == "stack":
            for i, d in enumerate(dates):
                acc = 0.0
                for _, vals, pcls in ly["parts"]:
                    v = vals.get(d)
                    if not v:
                        continue
                    y1, y2 = y(acc + v), y(acc)
                    out.append(f'<rect class="wbar {pcls}" x="{x(i) - bar_w / 2:.1f}" y="{y1:.1f}" width="{bar_w:.1f}" '
                               f'height="{max(y2 - y1, 0.3):.1f}"/>')
                    acc += v
        elif k == "line":
            for seg in _segments(dates, lambda d: ly["values"].get(d)):
                pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in seg)
                if len(seg) > 1:
                    out.append(f'<polyline class="line {cls}{" dashed" if ly.get("dashed") else ""}" points="{pts}"/>')
                if ly.get("dots") or len(seg) == 1:
                    for i, v in seg:
                        extra = ly["class_of"](dates[i], v) if ly.get("class_of") else ""
                        st = (ly.get("status") or {}).get(dates[i], "final")
                        out.append(f'<circle class="dot {cls} {extra} {st}" cx="{x(i):.1f}" cy="{y(v):.1f}" r="2.6"/>')

    # Not-yet-in days: a dashed stub on the baseline, so "waiting" never reads as zero.
    for i, d in enumerate(dates):
        if status.get(d) == "pending":
            out.append(f'<line class="pending-mark" x1="{x(i):.1f}" x2="{x(i):.1f}" y1="{PAD_T + plot_h:.1f}" '
                       f'y2="{PAD_T + plot_h - 14:.1f}"/>')

    out.append(f'<line class="xhair" x1="0" x2="0" y1="{PAD_T}" y2="{PAD_T + plot_h}"/>')
    for i, d in enumerate(dates):
        rows = _tip_rows(layers, d)
        note = NOTES.get(status.get(d, "final"), "") if status else ""
        tip = {"d": _day_label(d, True), "rows": rows, "note": note}
        title = "; ".join(f"{r[0]} {r[1]}" for r in rows) or note
        out.append(f'<rect class="hit" x="{PAD_L + i * slot:.1f}" y="{PAD_T}" width="{slot:.2f}" height="{plot_h}" '
                   f'data-i="{i}" data-x="{x(i):.1f}" {_tip(tip)}><title>{escape(tip["d"])}: '
                   f'{escape(title)}</title></rect>')
    out.append("</svg>")
    return Markup("".join(out))


def _segments(dates, get):
    """Runs of consecutive days that have a value (lines break across gaps)."""
    seg, out = [], []
    for i, d in enumerate(dates):
        v = get(d)
        if v is None or (isinstance(v, tuple) and None in v):
            if seg:
                out.append(seg)
            seg = []
        else:
            seg.append((i, v))
    if seg:
        out.append(seg)
    return out


def _tip_rows(layers, d):
    rows = []
    for ly in layers:
        if ly.get("tip") is False or not ly.get("label"):
            continue
        k = ly["kind"]
        if k in ("bars", "line", "info"):
            v = ly["values"].get(d)
            if v is not None:
                rows.append([ly["label"], ly.get("fmt", _n)(v), ly.get("cls", "")])
        elif k == "band":
            a, b = ly["low"].get(d), ly["high"].get(d)
            if a is not None and b is not None:
                f = ly.get("fmt", _n)
                rows.append([ly["label"], f"{f(a)} – {f(b)}", ly.get("cls", "")])
        elif k == "range":
            a, b = ly["low"].get(d), ly["high"].get(d)
            if a is not None and b is not None:
                rows.append([ly["label"], f"{ly['fmt_low'](a)} → {ly['fmt_high'](b)}", ly.get("cls", "")])
        elif k == "stack":
            for label, vals, pcls in ly["parts"]:
                if vals.get(d) is not None:
                    rows.append([label, ly.get("fmt", _n)(vals[d]), pcls])
        elif k == "hline" and ly.get("y") is not None:
            rows.append([ly["label"], ly.get("fmt", _n)(ly["y"]), ly.get("cls", "")])
    return rows


def category_bars(items: list[dict], *, height: int = 140, cls: str = "", fmt=_n, label: str = "",
                  y_min=None, y_max=None) -> Markup:
    """Vertical bars for labelled categories: items are {"label", "value", "tip": "...", "cls": optional}."""
    n = len(items)
    plot_w, plot_h = W - PAD_L - PAD_R, height - PAD_T - PAD_B
    vals = [i["value"] for i in items if i["value"] is not None] or [0, 1]
    lo = 0 if y_min is None else y_min
    hi = max(vals) if y_max is None else y_max
    ticks = _nice_ticks(lo, hi)
    lo, hi = ticks[0], ticks[-1]
    slot = plot_w / max(n, 1)
    bw = slot * 0.62

    def y(v):
        return PAD_T + plot_h - (max(lo, min(hi, v)) - lo) / (hi - lo) * plot_h

    out = [f'<svg class="wchart" viewBox="0 0 {W} {height}" role="img" aria-label="{escape(label)}">']
    for t in ticks:
        out.append(f'<line class="wgl" x1="{PAD_L}" x2="{W - PAD_R}" y1="{y(t):.1f}" y2="{y(t):.1f}"/>'
                   f'<text class="ylab" x="{PAD_L - 5}" y="{y(t) + 4:.1f}" text-anchor="end">{escape(fmt(t))}</text>')
    for i, it in enumerate(items):
        cx = PAD_L + (i + 0.5) * slot
        if n <= 8 or (n - 1 - i) % 2 == 0:
            out.append(f'<text class="xlab{" xl-alt" if n > 8 and (n - 1 - i) % 4 else ""}" x="{cx:.1f}" '
                       f'y="{height - 6}" text-anchor="middle">{escape(it["label"])}</text>')
        if it["value"] is None:
            continue
        y1 = y(it["value"])
        tip = {"d": it.get("title", it["label"]), "rows": [[label, fmt(it["value"]), cls]], "note": it.get("tip", "")}
        out.append(f'<rect class="wbar {cls} {it.get("cls", "")}" x="{cx - bw / 2:.1f}" y="{y1:.1f}" width="{bw:.1f}" '
                   f'height="{max(y(lo) - y1, 0.5):.1f}" rx="2" {_tip(tip)}>'
                   f'<title>{escape(tip["d"])}: {escape(fmt(it["value"]))}</title></rect>')
    out.append("</svg>")
    return Markup("".join(out))


def ring(value, *, max_value: float = 100, zone: str | None = None, center: str = "", sub: str = "",
         label: str = "", pending: bool = False, tip: dict | None = None) -> Markup:
    """A Whoop-style ring gauge. `zone` (good/mid/bad/low/ok/high) picks the arc colour."""
    frac = 0 if value is None else max(0.0, min(1.0, value / max_value))
    cls = f"ring z-{zone or 'none'}{' pending' if pending or value is None else ''}"
    tip_attr = f' {_tip(tip)}' if tip else ""
    return Markup(
        f'<svg class="{cls}" viewBox="0 0 120 120" role="img" aria-label="{escape(label)}: {escape(center)}"{tip_attr}>'
        f'<circle class="ring-track" cx="60" cy="60" r="50" pathLength="100"/>'
        f'<circle class="ring-arc" cx="60" cy="60" r="50" pathLength="100" '
        f'stroke-dasharray="{frac * 100:.1f} 100" transform="rotate(-90 60 60)"/>'
        f'<text class="ring-val" x="60" y="62" text-anchor="middle">{escape(center or "—")}</text>'
        f'<text class="ring-sub" x="60" y="82" text-anchor="middle">{escape(sub)}</text>'
        f'<title>{escape(label)}: {escape(center or "no data yet")}</title></svg>')


def spark(points: list[dict], *, cls: str = "", band: dict | None = None, fmt=_n, label: str = "") -> Markup:
    """A 14-ish day sparkline (points are {"date", "value"}) with an optional baseline band {low, high}."""
    vals = [p["value"] for p in points if p["value"] is not None]
    if len(vals) < 2:
        return Markup("")
    w, h, pad = 120, 32, 3
    lo, hi = min(vals), max(vals)
    if band and band.get("low") is not None:
        lo, hi = min(lo, band["low"]), max(hi, band["high"])
    hi = hi if hi > lo else lo + 1
    n = len(points)

    def x(i):
        return pad + i * (w - 2 * pad) / max(n - 1, 1)

    def y(v):
        return h - pad - (v - lo) / (hi - lo) * (h - 2 * pad)

    out = [f'<svg class="wspk {cls}" viewBox="0 0 {w} {h}" role="img" aria-label="{escape(label)}">']
    if band and band.get("low") is not None:
        out.append(f'<rect class="band" x="0" y="{y(band["high"]):.1f}" width="{w}" '
                   f'height="{y(band["low"]) - y(band["high"]):.1f}"/>')
    for seg in _segments([p["date"] for p in points], lambda d: next(p["value"] for p in points if p["date"] == d)):
        out.append(f'<polyline class="line" points="{" ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in seg)}"/>')
    last = max(i for i, p in enumerate(points) if p["value"] is not None)
    tip = {"d": label, "rows": [[_day_label(p["date"]), fmt(p["value"]), ""] for p in points
                                if p["value"] is not None][-7:], "note": "last 7 readings"}
    out.append(f'<circle class="dot" cx="{x(last):.1f}" cy="{y(points[last]["value"]):.1f}" r="2.5"/>'
               f'<rect class="hit" x="0" y="0" width="{w}" height="{h}" {_tip(tip)}/></svg>')
    return Markup("".join(out))


def heatmap(weeks: list[list[dict]], *, label: str = "") -> Markup:
    """GitHub-style calendar: one column per Monday-start week; cells carry level 0-5, status and display."""
    cell, gap, top, left = 13, 3, 16, 20
    w = left + len(weeks) * (cell + gap)
    h = top + 7 * (cell + gap)
    out = [f'<svg class="wheat" viewBox="0 0 {w} {h}" role="img" aria-label="{escape(label)}">']
    for r, name in ((0, "M"), (2, "W"), (4, "F")):
        out.append(f'<text class="ylab" x="{left - 6}" y="{top + r * (cell + gap) + 10}" text-anchor="end">{name}</text>')
    last_month, last_col = None, -9
    for c, week in enumerate(weeks):
        m = date.fromisoformat(week[-1]["date"]).strftime("%b")
        if m != last_month:
            if c - last_col >= 3:
                out.append(f'<text class="xlab" x="{left + c * (cell + gap)}" y="11">{m}</text>')
                last_col = c
            last_month = m
        for r, p in enumerate(week):
            note = NOTES.get(p["status"], "") if p["status"] != "final" else ""
            tip = {"d": _day_label(p["date"], True), "rows": [[label, p["display"], ""]] if p["value"] is not None
                   else [], "note": note}
            out.append(f'<rect class="hm l{p["level"]} {p["status"]}" x="{left + c * (cell + gap)}" '
                       f'y="{top + r * (cell + gap)}" width="{cell}" height="{cell}" rx="2.5" {_tip(tip)}>'
                       f'<title>{escape(tip["d"])}: {escape(p["display"])}</title></rect>')
    out.append("</svg>")
    return Markup("".join(out))


def scatter(points: list[dict], *, fit: dict | None, x_fmt=_n, y_fmt=_n, x_label: str = "", y_label: str = "",
            height: int = 300) -> Markup:
    """Dots for paired days ({"date", "x", "y"}) plus an optional least-squares line {slope, intercept}."""
    if not points:
        return Markup("")
    plot_w, plot_h = W - PAD_L - PAD_R, height - PAD_T - PAD_B - 12
    xs, ys = [p["x"] for p in points], [p["y"] for p in points]
    xt, yt = _nice_ticks(min(xs), max(xs)), _nice_ticks(min(ys), max(ys))
    x0, x1, y0, y1 = xt[0], xt[-1], yt[0], yt[-1]

    def X(v):
        return PAD_L + (v - x0) / (x1 - x0) * plot_w

    def Y(v):
        return PAD_T + plot_h - (v - y0) / (y1 - y0) * plot_h

    out = [f'<svg class="wchart" viewBox="0 0 {W} {height}" role="img" '
           f'aria-label="{escape(y_label)} against {escape(x_label)}">']
    for t in yt:
        out.append(f'<line class="wgl" x1="{PAD_L}" x2="{W - PAD_R}" y1="{Y(t):.1f}" y2="{Y(t):.1f}"/>'
                   f'<text class="ylab" x="{PAD_L - 5}" y="{Y(t) + 4:.1f}" text-anchor="end">{escape(y_fmt(t))}</text>')
    for t in xt:
        out.append(f'<text class="xlab" x="{X(t):.1f}" y="{PAD_T + plot_h + 16:.1f}" text-anchor="middle">'
                   f'{escape(x_fmt(t))}</text>')
    out.append(f'<text class="axis-title" x="{PAD_L + plot_w / 2:.1f}" y="{height - 2}" text-anchor="middle">'
               f'{escape(x_label)}</text>')
    if fit:
        a, b = fit["slope"], fit["intercept"]
        out.append(f'<line class="fit" x1="{X(x0):.1f}" y1="{Y(max(y0, min(y1, a * x0 + b))):.1f}" '
                   f'x2="{X(x1):.1f}" y2="{Y(max(y0, min(y1, a * x1 + b))):.1f}"/>')
    for p in points:
        tip = {"d": _day_label(p["date"], True), "rows": [[x_label, x_fmt(p["x"]), ""], [y_label, y_fmt(p["y"]), ""]],
               "note": ""}
        out.append(f'<circle class="sdot" cx="{X(p["x"]):.1f}" cy="{Y(p["y"]):.1f}" r="4" {_tip(tip)}>'
                   f'<title>{escape(tip["d"])}: {escape(x_fmt(p["x"]))}, {escape(y_fmt(p["y"]))}</title></circle>')
    out.append("</svg>")
    return Markup("".join(out))
