#!/usr/bin/env python3
"""Renders a self-contained HTML spending-summary report for the
finance-reporter subagent (Phase 2 of the personal-finance program).

Zero-dependency (Python stdlib only), matching the rest of scripts/. This
script does NOT talk to Monarch — the agent gathers data via the `monarch`
MCP tools and hands it in as a JSON payload; this script only shapes that
payload into a consistent HTML report. Reused as-is by Phase 4 (annual
review) at a 12-month rollup.

Usage:
    python scripts/finance_report.py --period weekly|monthly --data-file <path.json> [--out <path.html>]

Payload shape (fields not applicable to a period may be omitted):
{
  "period_label": "Week of Jul 13-19, 2026",
  "date_range": {"start": "2026-07-13", "end": "2026-07-19"},
  "prior_range": {"start": "2026-07-06", "end": "2026-07-12"},
  "totals": {"income": 0, "expenses": 0, "savings": 0, "savings_rate": 0,
             "prior_expenses": 0},
  "by_category": [{"name": "...", "amount": 0, "prior_amount": 0}],
  "by_who": [{"tag": "WHO - Justin", "amount": 0, "prior_amount": 0}],
  "budget": [{"name": "...", "planned": 0, "actual": 0, "remaining": 0}],
  "net_worth": {"current": 0, "prior": 0}
}

Prints JSON to stdout: {"html_path": "...", "telegram_summary": "..."}
"""

from __future__ import annotations

import argparse
import json
import sys
from html import escape
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = REPO_ROOT / "state" / "finance_reports"

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


def _err(msg: str) -> int:
    print(json.dumps({"error": msg}), file=sys.stderr)
    return 2


def _fmt_money(value) -> str:
    if value is None:
        return "-"
    return f"${value:,.2f}"


def _fmt_pct(value) -> str:
    if value is None:
        return "-"
    return f"{value:.1f}%"


def _delta_class(current, prior, invert: bool = False) -> str:
    """CSS class for a delta. By default 'up' (increase) is flagged red — right
    for spend, wrong for net worth/income, where invert=True swaps the colors."""
    if current is None or prior is None or current == prior:
        return ""
    increased = current > prior
    if invert:
        increased = not increased
    return "up" if increased else "down"


def _delta_text(current, prior) -> str:
    if current is None or prior is None or current == prior:
        return ""
    diff = current - prior
    sign = "+" if diff >= 0 else "-"
    return f"{sign}{_fmt_money(abs(diff))} vs prior period"


def _bar_row(label: str, amount, max_amount, prior_amount=None) -> str:
    pct = 0.0
    if max_amount and max_amount > 0 and amount is not None:
        pct = max(0.0, min(100.0, (amount / max_amount) * 100))
    delta_cls = _delta_class(amount, prior_amount)
    delta_txt = _delta_text(amount, prior_amount) if prior_amount is not None else ""
    return f"""
    <div class="bar-row">
      <div class="bar-label">{escape(label)}</div>
      <div class="bar-track"><div class="bar-fill" style="width:{pct:.1f}%"></div></div>
      <div class="bar-amount">{_fmt_money(amount)}
        {f'<span class="delta {delta_cls}">{escape(delta_txt)}</span>' if delta_txt else ''}
      </div>
    </div>"""


def _section_by_category(rows: list) -> str:
    if not rows:
        return ""
    amounts = [r.get("amount") or 0 for r in rows]
    max_amount = max(amounts) if amounts else 0
    body = "\n".join(
        _bar_row(r.get("name", "Unknown"), r.get("amount"), max_amount, r.get("prior_amount"))
        for r in sorted(rows, key=lambda r: r.get("amount") or 0, reverse=True)
    )
    return f"""
    <section>
      <h2>Spending by category</h2>
      {body}
    </section>"""


def _section_by_who(rows: list) -> str:
    if not rows:
        return ""
    amounts = [r.get("amount") or 0 for r in rows]
    max_amount = max(amounts) if amounts else 0
    body = "\n".join(
        _bar_row(r.get("tag", "Unknown"), r.get("amount"), max_amount, r.get("prior_amount"))
        for r in sorted(rows, key=lambda r: r.get("amount") or 0, reverse=True)
    )
    return f"""
    <section>
      <h2>Spending by who</h2>
      {body}
    </section>"""


def _section_budget(rows: list) -> str:
    if not rows:
        return ""
    trs = []
    for r in rows:
        planned = r.get("planned")
        actual = r.get("actual")
        remaining = r.get("remaining")
        over = planned is not None and actual is not None and actual > planned
        trs.append(f"""
        <tr class="{'over' if over else ''}">
          <td>{escape(r.get('name', 'Unknown'))}</td>
          <td>{_fmt_money(planned)}</td>
          <td>{_fmt_money(actual)}</td>
          <td>{_fmt_money(remaining)}</td>
        </tr>""")
    return f"""
    <section>
      <h2>Budget vs. actual</h2>
      <table>
        <thead><tr><th>Category</th><th>Planned</th><th>Actual</th><th>Remaining</th></tr></thead>
        <tbody>{''.join(trs)}</tbody>
      </table>
    </section>"""


def _section_net_worth(nw: dict) -> str:
    if not nw:
        return ""
    current = nw.get("current")
    prior = nw.get("prior")
    delta_cls = _delta_class(current, prior, invert=True)
    delta_txt = _delta_text(current, prior) if prior is not None else ""
    return f"""
    <section>
      <h2>Net worth</h2>
      <div class="stat">
        <div class="stat-value">{_fmt_money(current)}</div>
        {f'<div class="delta {delta_cls}">{escape(delta_txt)}</div>' if delta_txt else ''}
      </div>
    </section>"""


CSS = """
body { font-family: -apple-system, Segoe UI, Helvetica, Arial, sans-serif; background:#f7f7f8; color:#1c1c1e; margin:0; padding:32px; }
.report { max-width:720px; margin:0 auto; background:#fff; border-radius:12px; padding:32px; box-shadow:0 1px 3px rgba(0,0,0,0.08); }
h1 { font-size:22px; margin:0 0 4px; }
.subtitle { color:#6b6b70; font-size:14px; margin:0 0 24px; }
.totals { display:flex; gap:16px; flex-wrap:wrap; margin-bottom:24px; }
.totals .stat { flex:1; min-width:140px; background:#f2f2f5; border-radius:8px; padding:12px 16px; }
.totals .stat-label { font-size:12px; color:#6b6b70; text-transform:uppercase; letter-spacing:0.04em; }
.totals .stat-value { font-size:20px; font-weight:600; margin-top:2px; }
section { margin-bottom:28px; }
h2 { font-size:15px; text-transform:uppercase; letter-spacing:0.03em; color:#3a3a3c; border-bottom:1px solid #e5e5ea; padding-bottom:6px; margin-bottom:12px; }
.bar-row { display:flex; align-items:center; gap:10px; margin-bottom:8px; }
.bar-label { flex:0 0 140px; font-size:13px; color:#3a3a3c; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.bar-track { flex:1; background:#eceef1; border-radius:4px; height:10px; overflow:hidden; }
.bar-fill { background:#5a67f2; height:100%; border-radius:4px; }
.bar-amount { flex:0 0 auto; font-size:13px; font-weight:600; text-align:right; min-width:90px; }
.delta { display:block; font-size:11px; font-weight:400; color:#6b6b70; }
.delta.up { color:#c0392b; }
.delta.down { color:#1e8e5a; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th, td { text-align:left; padding:6px 8px; border-bottom:1px solid #eceef1; }
tr.over td { color:#c0392b; }
.stat { background:#f2f2f5; border-radius:8px; padding:12px 16px; display:inline-block; }
.stat-value { font-size:20px; font-weight:600; }
footer { margin-top:24px; font-size:11px; color:#9a9a9e; }
"""


def render_html(payload: dict, period: str) -> str:
    period_label = payload.get("period_label", period.capitalize())
    totals = payload.get("totals", {}) or {}
    income = totals.get("income")
    expenses = totals.get("expenses")
    savings = totals.get("savings")
    savings_rate = totals.get("savings_rate")
    prior_expenses = totals.get("prior_expenses")
    delta_cls = _delta_class(expenses, prior_expenses)
    delta_txt = _delta_text(expenses, prior_expenses) if prior_expenses is not None else ""

    totals_html = f"""
    <div class="totals">
      <div class="stat"><div class="stat-label">Income</div><div class="stat-value">{_fmt_money(income)}</div></div>
      <div class="stat"><div class="stat-label">Expenses</div><div class="stat-value">{_fmt_money(expenses)}</div>
        {f'<div class="delta {delta_cls}">{escape(delta_txt)}</div>' if delta_txt else ''}
      </div>
      <div class="stat"><div class="stat-label">Savings</div><div class="stat-value">{_fmt_money(savings)}</div></div>
      <div class="stat"><div class="stat-label">Savings rate</div><div class="stat-value">{_fmt_pct(savings_rate)}</div></div>
    </div>"""

    sections = [
        _section_by_who(payload.get("by_who", [])),
        _section_by_category(payload.get("by_category", [])),
    ]
    if period == "monthly":
        sections.append(_section_budget(payload.get("budget", [])))
        sections.append(_section_net_worth(payload.get("net_worth", {})))

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{escape(period_label)} — Spending Summary</title>
<style>{CSS}</style>
</head>
<body>
  <div class="report">
    <h1>{escape(period_label)}</h1>
    <p class="subtitle">Spending summary &middot; {escape(period)}</p>
    {totals_html}
    {''.join(sections)}
    <footer>Generated by the finance-reporter subagent.</footer>
  </div>
</body>
</html>"""


def build_telegram_summary(payload: dict, period: str) -> str:
    period_label = payload.get("period_label", period.capitalize())
    totals = payload.get("totals", {}) or {}
    expenses = totals.get("expenses")
    prior_expenses = totals.get("prior_expenses")
    income = totals.get("income")
    savings_rate = totals.get("savings_rate")

    lines = [f"📊 {period_label}"]
    if expenses is not None:
        delta = ""
        if prior_expenses is not None:
            diff = expenses - prior_expenses
            arrow = "▲" if diff > 0 else ("▼" if diff < 0 else "→")
            delta = f" ({arrow} {_fmt_money(abs(diff))} vs prior)"
        lines.append(f"Spent {_fmt_money(expenses)}{delta}")
    if income is not None:
        lines.append(f"Income {_fmt_money(income)}")
    if savings_rate is not None:
        lines.append(f"Savings rate {_fmt_pct(savings_rate)}")

    by_who = payload.get("by_who", [])
    if by_who:
        top = sorted(by_who, key=lambda r: r.get("amount") or 0, reverse=True)[:4]
        who_line = ", ".join(f"{r.get('tag')}: {_fmt_money(r.get('amount'))}" for r in top)
        lines.append(f"By who — {who_line}")

    by_category = payload.get("by_category", [])
    if by_category:
        top = sorted(by_category, key=lambda r: r.get("amount") or 0, reverse=True)[:3]
        cat_line = ", ".join(f"{r.get('name')}: {_fmt_money(r.get('amount'))}" for r in top)
        lines.append(f"Top categories — {cat_line}")

    if period == "monthly":
        nw = payload.get("net_worth", {}) or {}
        if nw.get("current") is not None:
            nw_line = f"Net worth {_fmt_money(nw.get('current'))}"
            if nw.get("prior") is not None:
                diff = nw["current"] - nw["prior"]
                arrow = "▲" if diff > 0 else ("▼" if diff < 0 else "→")
                nw_line += f" ({arrow} {_fmt_money(abs(diff))})"
            lines.append(nw_line)
        budget = payload.get("budget", [])
        over = [b for b in budget if b.get("planned") is not None and b.get("actual") is not None and b["actual"] > b["planned"]]
        if over:
            over_line = ", ".join(b.get("name", "Unknown") for b in over[:3])
            lines.append(f"Over budget — {over_line}")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render a finance spending-summary HTML report.")
    parser.add_argument("--period", required=True, choices=["weekly", "monthly"])
    parser.add_argument("--data-file", required=True, help="Path to a JSON payload file.")
    parser.add_argument("--out", default=None, help="Output .html path (default: state/finance_reports/<period>-<end date>.html)")
    args = parser.parse_args(argv)

    data_path = Path(args.data_file)
    if not data_path.exists():
        return _err(f"data file not found: {data_path}")
    try:
        payload = json.loads(data_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return _err(f"invalid JSON in data file: {exc}")

    html = render_html(payload, args.period)
    telegram_summary = build_telegram_summary(payload, args.period)

    if args.out:
        out_path = Path(args.out)
    else:
        end_date = (payload.get("date_range") or {}).get("end", "unknown-date")
        DEFAULT_OUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = DEFAULT_OUT_DIR / f"{args.period}-{end_date}.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")

    print(json.dumps({"html_path": str(out_path), "telegram_summary": telegram_summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
