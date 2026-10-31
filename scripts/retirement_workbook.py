#!/usr/bin/env python3
"""Builds the retirement modeling Excel workbook (Phase 3 of the personal-finance
program). Takes the `retirement_model.py` engine output plus assumption metadata
and emits a self-contained, user-editable `.xlsx`:

  - Summary        headline, success %, ending balances, narrative, chart
  - Assumptions    master editable inputs + a source column (Monarch/user/default)
  - Base Case      year-by-year projection as LIVE Excel formulas referencing the
                   Assumptions cells — edit an assumption, Excel recalculates. The
                   per-year median return column comes from the engine (it follows the
                   allocation's glide path) and is editable too.
  - Monte Carlo    static p10/p50/p90 trajectory table (nominal and today's dollars) + chart
  - Accounts       each retirement account's bucket, balance and allocation
  - <scenario>     one tab per what-if, each with its own local (live) inputs
  - Data Sources   which values came from Monarch, when pulled, whether overridden

The Base Case end balance matches retirement_model.deterministic_projection exactly:
    out    = spending - social security + other outflows + taxes
    growth = (start - out) * r + contribution * (SQRT(1 + r) - 1)
    end    = MAX(0, start - out + growth + contribution)
(outflows at the start of the year, contributions mid-year, r = that year's median return).
Contribution and spending are live formulas; Social Security, other outflows, taxes and r are
the engine's values for the median path, so the sheet and the engine agree on it.

Usage (CLI):
    python scripts/retirement_workbook.py --data-file <payload.json> --out <out.xlsx>
(The retirement agent builds the payload with `retirement_store.py workbook` instead.)

Payload shape:
{
  "generated_date": "2026-07-23",
  "assumptions": { ...engine assumption keys..., "scenarios": [...] },
  "assumption_sources": {"current_balance": "Monarch get_accounts (2026-07-23)", ...},
  "result": <retirement_model.run_all output: {"base": {...}, "scenarios": [...]}>,
  "result_file": "state/retirement/result-2026-07-23.json"   (use instead of "result";
                 the engine output is ~140KB, too big to write inline)
  "accounts": [{"name", "bucket", "balance", "allocation": {...}, "allocation_source"}],  (optional)
  "narrative": {"headline": "...", "body": "..."},
  "data_sources": [{"item": "...", "value": "...", "source": "...",
                    "pulled": "2026-07-23", "overridden": "No"}]
}

Prints JSON to stdout: {"workbook_path": "..."}.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import xlsxwriter


for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

# Ordered assumption fields: (key, label, number-format). The fixed order gives
# each value a stable cell so Base Case formulas can reference it.
FIELDS = [
    ("current_age", "Current age", "0"),
    ("retirement_age", "Retirement age", "0"),
    ("plan_through_age", "Plan through age", "0"),
    ("current_balance", "Current portfolio balance", "$#,##0"),
    ("annual_contribution", "Annual contribution (today's $)", "$#,##0"),
    ("contribution_real_growth", "Contribution growth above inflation", "0.0%"),
    ("retirement_spend", "Retirement spending / yr (today's $, after tax)", "$#,##0"),
    ("inflation", "Inflation", "0.0%"),
]

PROJ_HEADERS = ["Age", "Year", "Start Balance", "Contribution", "Median Return", "Growth",
                "Spending", "Social Security", "Other Outflows", "Taxes", "End Balance", "End (today's $)"]
END_COL = 10
REAL_COL = 11


def _sanitize_sheet_name(name: str) -> str:
    for ch in "[]:*?/\\":
        name = name.replace(ch, " ")
    return name.strip()[:31] or "Scenario"


def _fmts(wb):
    return {
        "title": wb.add_format({"bold": True, "font_size": 15}),
        "h2": wb.add_format({"bold": True, "font_size": 12}),
        "hdr": wb.add_format({"bold": True, "bottom": 1, "bg_color": "#F2F2F2"}),
        "label": wb.add_format({"bold": False}),
        "input": wb.add_format({"bg_color": "#FFF6DC", "border": 1}),  # editable = shaded
        "note": wb.add_format({"italic": True, "font_color": "#666666", "text_wrap": True}),
        "wrap": wb.add_format({"text_wrap": True, "valign": "top"}),
        "money": wb.add_format({"num_format": "$#,##0"}),
        "money_eng": wb.add_format({"num_format": "$#,##0", "font_color": "#555555", "bg_color": "#EEF3F8"}),
        "money_in": wb.add_format({"num_format": "$#,##0", "bg_color": "#FFF6DC", "border": 1}),
        "pct": wb.add_format({"num_format": "0.0%"}),
        "pct_in": wb.add_format({"num_format": "0.0%", "bg_color": "#FFF6DC", "border": 1}),
        "int": wb.add_format({"num_format": "0"}),
        "int_in": wb.add_format({"num_format": "0", "bg_color": "#FFF6DC", "border": 1}),
        "big": wb.add_format({"bold": True, "font_size": 22, "font_color": "#1F6F43"}),
    }


def _input_fmt(number_format, fmts):
    if number_format.startswith("$"):
        return fmts["money_in"]
    if number_format.endswith("%"):
        return fmts["pct_in"]
    return fmts["int_in"]


def _write_assumption_block(ws, assumptions, sources, fmts, sheet_name, title, top=0):
    """Write a label/value/source block. Returns (cell_map, next_free_row).

    cell_map maps each key to a bare cell ref like "$B$5" (no sheet prefix)."""
    ws.write(top, 0, title, fmts["title"])
    hdr_row = top + 2
    ws.write(hdr_row, 0, "Assumption", fmts["hdr"])
    ws.write(hdr_row, 1, "Value", fmts["hdr"])
    ws.write(hdr_row, 2, "Source", fmts["hdr"])
    cell_map = {}
    data_start = hdr_row + 1
    for i, (key, label, numfmt) in enumerate(FIELDS):
        r = data_start + i
        ws.write(r, 0, label, fmts["label"])
        ws.write_number(r, 1, float(assumptions.get(key) or 0), _input_fmt(numfmt, fmts))
        if sources:
            ws.write(r, 2, sources.get(key, "default"))
        cell_map[key] = f"$B${r + 1}"
    r = data_start + len(FIELDS)
    alloc = assumptions.get("allocation")
    if alloc:
        ws.write(r + 1, 0, "Returns (from the allocation; informational)", fmts["h2"])
        rows = [("Stocks today", alloc["us_equity"] + alloc["intl_equity"]),
                ("  of which international", alloc["intl_equity"]),
                ("Bonds today", alloc["bonds"]), ("Cash today", alloc["cash"])]
        glide = assumptions.get("glide_path") or {}
        if "retirement_equity" in glide:
            rows.append(("Stocks at retirement (glide path)", glide["retirement_equity"]))
        if "end_equity" in glide:
            rows.append(("Stocks at plan end (glide path)", glide["end_equity"]))
        if assumptions.get("fee") is not None:
            rows.append(("Fund fees", assumptions["fee"]))
        for i, (lab, val) in enumerate(rows):
            ws.write(r + 2 + i, 0, lab, fmts["label"])
            ws.write_number(r + 2 + i, 1, float(val), fmts["pct"])
        r = r + 2 + len(rows)
    elif assumptions.get("expected_return") is not None:
        ws.write(r + 1, 0, "Expected return (nominal, arithmetic)", fmts["label"])
        ws.write_number(r + 1, 1, float(assumptions["expected_return"]), fmts["pct"])
        ws.write(r + 2, 0, "Return volatility", fmts["label"])
        ws.write_number(r + 2, 1, float(assumptions.get("volatility", 0)), fmts["pct"])
        r += 3
    r = _write_household_block(ws, assumptions, fmts, r)
    ws.set_column(0, 0, 36)
    ws.set_column(1, 1, 16)
    ws.set_column(2, 2, 38)
    return cell_map, r


def _write_household_block(ws, a, fmts, r):
    """Household inputs the engine uses (informational; edit them in config and re-run)."""
    if not a.get("buckets") and not a.get("people"):
        return r
    ws.write(r + 1, 0, "Household (informational; the engine's inputs)", fmts["h2"])
    r += 2
    rows = []
    for b, v in (a.get("buckets") or {}).items():
        rows.append((f"Balance: {b}", v, "money"))
    for b, v in (a.get("contributions") or {}).items():
        rows.append((f"Contribution / yr: {b}", v, "money"))
    if a.get("kids_balance") is not None:
        rows.append(("Kids' accounts (529s etc.)", a["kids_balance"], "money"))
    if a.get("taxable_gain_fraction") is not None:
        rows.append(("Taxable account: share that is gains", a["taxable_gain_fraction"], "pct"))
    for p in a.get("people") or []:
        if p.get("ss_pia"):
            rows.append((f"{p['name']}: Social Security at 67 / yr (today's $)", p["ss_pia"], "money"))
            rows.append((f"{p['name']}: claims at age", p.get("ss_claim_age", 67), "int"))
    for lab, val, f in rows:
        ws.write(r, 0, lab, fmts["label"])
        ws.write_number(r, 1, float(val), fmts[f])
        r += 1
    events = a.get("events") or []
    if events:
        ws.write(r + 1, 0, "Cash-flow events", fmts["h2"])
        ws.write(r + 2, 0, "Event", fmts["hdr"])
        ws.write(r + 2, 1, "Amount / yr", fmts["hdr"])
        ws.write(r + 2, 2, "Ages and basis", fmts["hdr"])
        r += 3
        for e in events:
            ws.write(r, 0, e.get("name", ""))
            ws.write_number(r, 1, float(e["amount"]), fmts["money"])
            basis = "today's $" if e.get("real", True) else "fixed $"
            if e.get("real_growth"):
                basis += f", +{e['real_growth'] * 100:.1f}%/yr above inflation"
            if e.get("funding") == "kids":
                basis += ", paid from kids' accounts first"
            start = e["start_age"] if e.get("min_age") is None else f"later of {e['start_age']} and {e['min_age']}"
            ws.write(r, 2, f"{start} to {e.get('end_age', e['start_age'])}; {basis}")
            r += 1
    return r


def _write_projection(ws, wb, assumptions, cell_map, prefix, fmts, base_year, header_row, det):
    """Write the projection table. `prefix` is prepended to every cell_map ref (e.g.
    "'Assumptions'!" for Base Case, "" for on-sheet scenario blocks). Contribution and
    spending are live formulas; the median return, Social Security, other outflows and taxes
    come from the engine's median path `det` (editable values, not recalculated).
    Returns (first_data_row0, last_data_row0) as 0-based indices."""
    for c, h in enumerate(PROJ_HEADERS):
        ws.write(header_row, c, h, fmts["hdr"])

    def P(key):
        return prefix + cell_map[key]

    ca = int(assumptions["current_age"])
    pa = int(assumptions["plan_through_age"])
    n = pa - ca
    first = header_row + 1
    for i in range(n):
        r = first + i          # 0-based row index
        R = r + 1              # Excel 1-based row number
        age = f"A{R}"
        infl = f"(1+{P('inflation')})^({age}-{P('current_age')})"
        # Age
        if i == 0:
            ws.write_formula(r, 0, f"={P('current_age')}", fmts["int"])
        else:
            ws.write_formula(r, 0, f"=A{R - 1}+1", fmts["int"])
        # Year (static reference point)
        ws.write_number(r, 1, base_year + i, fmts["int"])
        # Start balance
        if i == 0:
            ws.write_formula(r, 2, f"={P('current_balance')}", fmts["money"])
        else:
            ws.write_formula(r, 2, f"=K{R - 1}", fmts["money"])
        row = det[i] if i < len(det) else {}
        # Contribution (working years only, arrives mid-year)
        ws.write_formula(r, 3,
            f"=IF({age}<{P('retirement_age')},{P('annual_contribution')}*{infl}"
            f"*(1+{P('contribution_real_growth')})^({age}-{P('current_age')}),0)",
            fmts["money"])
        # Median return for the year (from the engine's glide path; editable)
        ws.write_number(r, 4, float(row.get("median_return", 0.0)), fmts["pct_in"])
        # Growth: outflows leave at the start of the year, half a year of growth on contributions
        net = f"(G{R}-H{R}+I{R}+J{R})"
        ws.write_formula(r, 5, f"=(C{R}-{net})*E{R}+D{R}*(SQRT(1+E{R})-1)", fmts["money"])
        # Spending (retirement only, after tax)
        ws.write_formula(r, 6,
            f"=IF({age}>={P('retirement_age')},{P('retirement_spend')}*{infl},0)",
            fmts["money"])
        # Engine values: Social Security, other outflows (mortgage, healthcare, college beyond the
        # kids' accounts, survivor spending change), taxes on withdrawals
        ws.write_number(r, 7, float(row.get("ss", 0.0)), fmts["money_eng"])
        ws.write_number(r, 8, float(row.get("other_outflows", 0.0)), fmts["money_eng"])
        ws.write_number(r, 9, float(row.get("taxes", 0.0)), fmts["money_eng"])
        # End balance, nominal and in today's dollars
        ws.write_formula(r, 10, f"=MAX(0,C{R}-{net}+F{R}+D{R})", fmts["money"])
        ws.write_formula(r, 11, f"=K{R}/(1+{P('inflation')})^({age}-{P('current_age')}+1)", fmts["money"])
    ws.set_column(0, 1, 8)
    ws.set_column(2, 11, 15)
    return first, first + n - 1


def _add_balance_chart(wb, data_sheet, first, last, value_col, title):
    chart = wb.add_chart({"type": "line"})
    chart.add_series({
        "name": title,
        "categories": [data_sheet, first, 0, last, 0],       # Age
        "values": [data_sheet, first, value_col, last, value_col],
        "line": {"color": "#1F6F43", "width": 2.0},
    })
    chart.set_title({"name": title})
    chart.set_x_axis({"name": "Age"})
    chart.set_y_axis({"name": "Balance ($)", "num_format": "$#,##0"})
    chart.set_legend({"none": True})
    chart.set_size({"width": 640, "height": 360})
    return chart


def build_workbook(payload: dict, out_path: str) -> str:
    assumptions = payload["assumptions"]
    sources = payload.get("assumption_sources", {})
    result = payload["result"]
    base = result["base"]
    narrative = payload.get("narrative", {})
    gen_date = payload.get("generated_date", "")
    base_year = int(gen_date[:4]) if gen_date[:4].isdigit() else 2026

    wb = xlsxwriter.Workbook(out_path, {"in_memory": True})
    fmts = _fmts(wb)

    # --- create sheets up front so cross-sheet formulas/charts resolve ---
    ws_summary = wb.add_worksheet("Summary")
    ws_assum = wb.add_worksheet("Assumptions")
    ws_base = wb.add_worksheet("Base Case")
    ws_mc = wb.add_worksheet("Monte Carlo")

    # --- Assumptions (master, referenced by Base Case) ---
    cell_map, _ = _write_assumption_block(
        ws_assum, assumptions, sources, fmts, "Assumptions",
        "Retirement Model — Assumptions", top=0)
    ws_assum.write(1, 0,
        "Shaded cells are editable — change them and the Base Case tab recalculates.",
        fmts["note"])

    # --- Base Case (live formulas) ---
    ws_base.write(0, 0, "Base Case — deterministic projection", fmts["title"])
    ws_base.write(1, 0,
        "Contribution and spending are live formulas on the Assumptions tab. Blue-shaded columns "
        "(Social Security, other outflows, taxes) and the median return come from the engine's run "
        "and do not recalculate; re-run the retirement agent to refresh them and the Monte Carlo.",
        fmts["note"])
    first, last = _write_projection(
        ws_base, wb, assumptions, cell_map, "'Assumptions'!", fmts, base_year, header_row=3,
        det=base["deterministic"])
    ws_base.insert_chart(3, 13,
        _add_balance_chart(wb, "Base Case", first, last, REAL_COL, "Projected balance, today's dollars (base case)"))

    # --- Monte Carlo (static engine output + confidence bands) ---
    ws_mc.write(0, 0, "Monte Carlo simulation", fmts["title"])
    ws_mc.write(1, 0, "Success probability", fmts["h2"])
    ws_mc.write(1, 2, base["success_probability"] / 100.0, fmts["pct"])
    se = base.get("success_se")
    ws_mc.write(2, 0,
        f"Share of {base.get('n_paths', 'the')} simulated futures that fund spending through the "
        f"plan-through age{f' (plus or minus {se:.1f} points of simulation noise)' if se else ''}. "
        "Balances are at the start of each age. Static snapshot from the last run.",
        fmts["note"])
    mc_hdr = 4
    real = base.get("percentiles_real") or []
    for c, h in enumerate(["Age", "10th percentile", "Median", "90th percentile",
                           "10th (today's $)", "Median (today's $)", "90th (today's $)"][:4 + 3 * bool(real)]):
        ws_mc.write(mc_hdr, c, h, fmts["hdr"])
    for i, row in enumerate(base["percentiles"]):
        r = mc_hdr + 1 + i
        ws_mc.write_number(r, 0, row["age"], fmts["int"])
        ws_mc.write_number(r, 1, row["p10"], fmts["money"])
        ws_mc.write_number(r, 2, row["p50"], fmts["money"])
        ws_mc.write_number(r, 3, row["p90"], fmts["money"])
        if real:
            ws_mc.write_number(r, 4, real[i]["p10"], fmts["money"])
            ws_mc.write_number(r, 5, real[i]["p50"], fmts["money"])
            ws_mc.write_number(r, 6, real[i]["p90"], fmts["money"])
    mc_first = mc_hdr + 1
    mc_last = mc_hdr + len(base["percentiles"])
    band = wb.add_chart({"type": "line"})
    for col, (name, color) in enumerate(
            [("10th percentile", "#C0504D"), ("Median", "#1F6F43"), ("90th percentile", "#4F81BD")], start=1):
        band.add_series({
            "name": name,
            "categories": ["Monte Carlo", mc_first, 0, mc_last, 0],
            "values": ["Monte Carlo", mc_first, col, mc_last, col],
            "line": {"color": color, "width": 1.75},
        })
    band.set_title({"name": "Portfolio balance — confidence bands"})
    band.set_x_axis({"name": "Age"})
    band.set_y_axis({"name": "Balance ($)", "num_format": "$#,##0"})
    band.set_size({"width": 720, "height": 400})
    ws_mc.insert_chart(mc_hdr, 8, band)
    ws_mc.set_column(0, 6, 16)

    # --- Scenario tabs (each self-contained + live) ---
    for sc in result.get("scenarios", []):
        name = _sanitize_sheet_name(sc.get("name", "Scenario"))
        ws = wb.add_worksheet(name)
        sc_assum = sc["assumptions"]
        smap, next_row = _write_assumption_block(
            ws, sc_assum, None, fmts, name, f"Scenario — {sc.get('name', name)}", top=0)
        ws.write(1, 0, "Editable inputs for this scenario; the table below is live.", fmts["note"])
        ws.write(next_row + 1, 0, "Monte Carlo success probability", fmts["h2"])
        ws.write(next_row + 1, 2, sc["success_probability"] / 100.0, fmts["pct"])
        proj_hdr = next_row + 3
        f0, l0 = _write_projection(ws, wb, sc_assum, smap, "", fmts, base_year, header_row=proj_hdr,
                                   det=sc["deterministic"])
        ws.insert_chart(proj_hdr, 13,
            _add_balance_chart(wb, name, f0, l0, REAL_COL, f"Projected balance, today's $ ({sc.get('name', name)})"))

    # --- Accounts ---
    accounts = payload.get("accounts") or []
    if accounts:
        ws_ac = wb.add_worksheet("Accounts")
        ws_ac.write(0, 0, "Retirement accounts", fmts["title"])
        ws_ac.write(1, 0, "Kids' (529/UTMA) and cash accounts are excluded from the projection.", fmts["note"])
        hdrs = ["Account", "Bucket", "Balance", "US stocks", "Intl stocks", "Bonds", "Cash", "Allocation source"]
        for c, h in enumerate(hdrs):
            ws_ac.write(3, c, h, fmts["hdr"])
        for i, acct in enumerate(accounts):
            r = 4 + i
            al = acct.get("allocation") or {}
            ws_ac.write(r, 0, acct.get("name", ""))
            ws_ac.write(r, 1, acct.get("bucket", ""))
            ws_ac.write_number(r, 2, float(acct.get("balance") or 0), fmts["money"])
            for c, k in enumerate(("us_equity", "intl_equity", "bonds", "cash"), start=3):
                ws_ac.write_number(r, c, float(al.get(k, 0.0)), fmts["pct"])
            ws_ac.write(r, 7, acct.get("allocation_source", ""))
        ws_ac.set_column(0, 0, 40)
        ws_ac.set_column(1, 6, 13)
        ws_ac.set_column(7, 7, 36)

    # --- Data Sources ---
    ws_ds = wb.add_worksheet("Data Sources")
    ws_ds.write(0, 0, "Data Sources", fmts["title"])
    for c, h in enumerate(["Item", "Value", "Source", "Pulled", "Overridden"]):
        ws_ds.write(2, c, h, fmts["hdr"])
    for i, row in enumerate(payload.get("data_sources", [])):
        r = 3 + i
        ws_ds.write(r, 0, row.get("item", ""))
        ws_ds.write(r, 1, row.get("value", ""))
        ws_ds.write(r, 2, row.get("source", ""))
        ws_ds.write(r, 3, row.get("pulled", ""))
        ws_ds.write(r, 4, row.get("overridden", ""))
    ws_ds.set_column(0, 1, 24)
    ws_ds.set_column(2, 2, 90)
    ws_ds.set_column(3, 4, 12)

    # --- Summary (author-facing headline; built last, references others) ---
    ws_summary.write(0, 0, "Retirement Plan — Summary", fmts["title"])
    if gen_date:
        ws_summary.write(1, 0, f"Generated {gen_date}", fmts["note"])
    ws_summary.write(3, 0, "Probability of success", fmts["h2"])
    ws_summary.write(4, 0, base["success_probability"] / 100.0, fmts["big"])
    if base.get("success_se") is not None:
        ws_summary.write(5, 0, f"plus or minus {base['success_se']:.1f} points (simulation noise)", fmts["note"])
    ws_summary.write(6, 0, "Ending balance (age {})".format(assumptions["plan_through_age"]), fmts["h2"])
    ending_real = base.get("ending_balance_real")
    if ending_real:
        ws_summary.write(6, 1, "Today's $", fmts["hdr"])
        ws_summary.write(6, 2, "Nominal", fmts["hdr"])
    for i, (lab, key) in enumerate([("10th percentile", "p10"), ("Median", "p50"), ("90th percentile", "p90")]):
        ws_summary.write(7 + i, 0, lab, fmts["label"])
        if ending_real:
            ws_summary.write_number(7 + i, 1, ending_real[key], fmts["money"])
            ws_summary.write_number(7 + i, 2, base["ending_balance"][key], fmts["money"])
        else:
            ws_summary.write_number(7 + i, 1, base["ending_balance"][key], fmts["money"])
    if narrative.get("headline"):
        ws_summary.write(11, 0, narrative["headline"], fmts["h2"])
    if narrative.get("body"):
        ws_summary.merge_range(12, 0, 16, 5, narrative["body"], fmts["wrap"])
    # confidence-band chart on the summary too
    summary_chart = wb.add_chart({"type": "line"})
    for col, (nm, color) in enumerate(
            [("10th", "#C0504D"), ("Median", "#1F6F43"), ("90th", "#4F81BD")], start=4 if real else 1):
        summary_chart.add_series({
            "name": nm,
            "categories": ["Monte Carlo", mc_first, 0, mc_last, 0],
            "values": ["Monte Carlo", mc_first, col, mc_last, col],
            "line": {"color": color, "width": 1.5},
        })
    summary_chart.set_title({"name": "Projected balance — confidence bands" + (" (today's $)" if real else "")})
    summary_chart.set_y_axis({"num_format": "$#,##0"})
    summary_chart.set_size({"width": 560, "height": 320})
    ws_summary.insert_chart(3, 7, summary_chart)
    ws_summary.set_column(0, 0, 22)
    ws_summary.set_column(1, 2, 16)

    wb.close()
    return out_path


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build the retirement modeling workbook")
    ap.add_argument("--data-file", required=True, help="payload JSON path")
    ap.add_argument("--out", required=True, help="output .xlsx path")
    args = ap.parse_args(argv)
    payload = json.loads(Path(args.data_file).read_text(encoding="utf-8"))
    if "result" not in payload and payload.get("result_file"):
        payload["result"] = json.loads(Path(payload["result_file"]).read_text(encoding="utf-8"))
    path = build_workbook(payload, args.out)
    print(json.dumps({"workbook_path": path}))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
