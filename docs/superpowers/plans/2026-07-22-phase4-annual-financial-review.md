# Phase 4 — Annual Financial Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `annual` report mode to the existing Phase 2 spending-summary
infrastructure, so the `finance-reporter` subagent can produce a year-to-date review
with month-by-month spending, year-over-year and 3-year trend comparisons, and an
agent-authored narrative (executive summary, income/spending explanation, net-worth
narrative, forward-looking commentary, and explicit data-gap callouts) — delivered as
a Telegram brief plus a full HTML report in Drive, on a once-a-year schedule.

**Architecture:** Reuse, don't duplicate. `scripts/finance_report.py` gets a third
`--period annual` mode (new rendering functions for narrative/month/trend sections,
alongside the existing category/who/budget/net-worth ones it already has).
`.claude/agents/finance-reporter.md` gets a new "Annual review" branch in its existing
report workflow — same Drive-upload and report-log steps, different data-gathering and
date-range logic. `scripts/finance_store.py`'s `report` subcommand gains `"annual"` as
a third `--period` choice (no schema change — the column is already free-text). One
new scheduled task fires once a year.

**Tech Stack:** Python 3 stdlib only (no new dependencies), SQLite (existing
`state/agent_results.db` / `state/schema.sql`), Markdown agent-definition files,
`scripts/scheduler_store.py` for cron registration.

## Global Constraints

- Every script invocation shown in agent-facing docs must be the **bare command**,
  nothing prepended (no `cd ... &&`, no env vars) — matches the existing convention in
  `.claude/agents/finance-reporter.md` and `.claude/agents/finance.md`.
- No new third-party dependencies — `scripts/finance_report.py` and
  `scripts/finance_store.py` are zero-dependency stdlib scripts and must stay that way.
- All Python scripts must `reconfigure(encoding="utf-8")` stdout/stderr at import time
  (already present in both files being modified — do not remove it).
- Uploaded report HTML must always be created with
  `disableConversionToGoogleType: true`, or Drive silently converts it to a Google Doc.
- Subagent frontmatter must set `model: sonnet` (repo-wide policy from `CLAUDE.md`) —
  `finance-reporter.md` already has this; do not change it.

---

### Task 1: Extend `finance_report.py` with an `annual` period

**Files:**
- Modify: `scripts/finance_report.py:11-27` (module docstring), `:170-198` (new
  section functions + CSS), `:201-246` (`render_html`), `:249-297`
  (`build_telegram_summary`), `:302` (argparse `--period` choices)

**Interfaces:**
- Consumes: existing `_bar_row(label, amount, max_amount, prior_amount=None)`,
  `_fmt_money`, `_fmt_pct`, `_delta_class`, `_delta_text`, `_section_by_category`,
  `_section_by_who`, `_section_budget`, `_section_net_worth` — all unchanged, all
  reused for the `annual` branch.
- Produces: `_section_by_month(rows: list) -> str`, `_section_trends(trends: list) ->
  str`, `_section_narrative_block(title: str, text: str) -> str`,
  `_section_data_gaps(gaps: list) -> str` — new functions Task 3 (the agent doc) will
  reference by the payload field names they consume (`by_month`, `trends`,
  `narrative.*`). `render_html` and `build_telegram_summary` both accept
  `period="annual"` as a valid third value alongside `"weekly"`/`"monthly"`.

- [ ] **Step 1: Write a hand-built annual test payload fixture**

Create `state/finance_reports/_smoke-annual.json` (this is a throwaway smoke-test
fixture, not committed — Step 8 deletes it):

```json
{
  "period_label": "2026 Year to Date",
  "date_range": {"start": "2026-01-01", "end": "2026-11-21"},
  "prior_range": {"start": "2025-01-01", "end": "2025-11-21"},
  "totals": {"income": 118000.0, "expenses": 78400.55, "savings": 39599.45, "savings_rate": 33.6, "prior_expenses": 82100.20},
  "by_category": [
    {"name": "Groceries", "amount": 14200.00, "prior_amount": 13100.00},
    {"name": "Mortgage", "amount": 26400.00, "prior_amount": 26400.00},
    {"name": "Restaurants", "amount": 3900.00, "prior_amount": 5200.00}
  ],
  "by_who": [
    {"tag": "WHO - Justin", "amount": 21000.00, "prior_amount": 19500.00},
    {"tag": "WHO - Caroline", "amount": 18500.00, "prior_amount": 20200.00},
    {"tag": "WHO - Family", "amount": 38900.55, "prior_amount": 42400.20}
  ],
  "by_month": [
    {"name": "Jan", "amount": 7200.10}, {"name": "Feb", "amount": 6800.40},
    {"name": "Mar", "amount": 7100.00}, {"name": "Apr", "amount": 6950.25},
    {"name": "May", "amount": 7300.00}, {"name": "Jun", "amount": 7050.80},
    {"name": "Jul", "amount": 7400.00}, {"name": "Aug", "amount": 7150.00},
    {"name": "Sep", "amount": 7000.00}, {"name": "Oct", "amount": 7249.00},
    {"name": "Nov", "amount": 3200.00}
  ],
  "trends": [
    {"label": "Net worth by year", "points": [
      {"year": "2024", "value": 398000.00}, {"year": "2025", "value": 410300.00}, {"year": "2026", "value": 452000.00}
    ]},
    {"label": "Total spending by year", "points": [
      {"year": "2024", "value": 95200.00}, {"year": "2025", "value": 82100.20}, {"year": "2026", "value": 78400.55}
    ]}
  ],
  "budget": [
    {"name": "Groceries", "planned": 13500.00, "actual": 14200.00, "remaining": -700.00},
    {"name": "Mortgage", "planned": 26400.00, "actual": 26400.00, "remaining": 0.0}
  ],
  "net_worth": {"current": 452000.00, "prior": 410300.00},
  "narrative": {
    "executive_summary": "Spending is down 4.5% year-to-date while income grew 9%, pushing the savings rate to 33.6% from prior-year levels.",
    "income_and_spending": "Restaurant spending fell 25% ($5,200 to $3,900), the single largest category shift this year.\nGroceries rose 8% ($13,100 to $14,200), tracking with grocery inflation.",
    "net_worth_narrative": "Net worth grew $41,700 (10.2%) this year, driven primarily by the higher savings rate rather than market returns.",
    "looking_ahead": "At the current savings rate, discretionary capacity for a larger purchase next year has meaningfully increased versus 2025 - this is a qualitative read on cash flow, not a funded plan.",
    "data_gaps": [
      "No retirement model yet (Phase 3) - the looking-ahead statement above is qualitative, not a simulated projection.",
      "No stored financial goals to compare this year's progress against."
    ],
    "telegram_highlights": [
      "Spending down 4.5% YTD, savings rate up to 33.6%",
      "Net worth +$41,700 (10.2%) this year"
    ]
  }
}
```

- [ ] **Step 2: Run the current script against the fixture and confirm it fails**

Run: `python scripts/finance_report.py --period annual --data-file state/finance_reports/_smoke-annual.json`
Expected: FAIL — `argument --period: invalid choice: 'annual'` (the `annual` choice
doesn't exist yet).

- [ ] **Step 3: Update the module docstring**

Modify `scripts/finance_report.py:11-27`, replacing:

```python
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
```

with:

```python
Usage:
    python scripts/finance_report.py --period weekly|monthly|annual --data-file <path.json> [--out <path.html>]

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
  "net_worth": {"current": 0, "prior": 0},
  "by_month": [{"name": "Jan", "amount": 0}],
  "trends": [
    {"label": "Net worth by year", "points": [{"year": "2024", "value": 0}]},
    {"label": "Total spending by year", "points": [{"year": "2024", "value": 0}]}
  ],
  "narrative": {
    "executive_summary": "2-4 sentences: the year in a nutshell.",
    "income_and_spending": "Paragraph(s), one per newline, on what income/spending did and why.",
    "net_worth_narrative": "Paragraph on what drove net worth change this year.",
    "looking_ahead": "Softer, explicitly-directional commentary - observations, not projections.",
    "data_gaps": ["Specific things that would make this more data-driven."],
    "telegram_highlights": ["2-4 short lines for the Telegram brief."]
  }
}

Prints JSON to stdout: {"html_path": "...", "telegram_summary": "..."}
"""
```

- [ ] **Step 4: Add the new section-rendering functions**

Insert immediately after `_section_net_worth` (`scripts/finance_report.py:170`, right
before the `CSS = """` line at 173):

```python
def _section_by_month(rows: list) -> str:
    if not rows:
        return ""
    amounts = [r.get("amount") or 0 for r in rows]
    max_amount = max(amounts) if amounts else 0
    body = "\n".join(
        _bar_row(r.get("name", "Unknown"), r.get("amount"), max_amount)
        for r in rows
    )
    return f"""
    <section>
      <h2>Spending by month</h2>
      {body}
    </section>"""


def _section_trends(trends: list) -> str:
    if not trends:
        return ""
    blocks = []
    for trend in trends:
        points = trend.get("points", [])
        if not points:
            continue
        values = [p.get("value") or 0 for p in points]
        max_value = max(values) if values else 0
        rows_html = "\n".join(
            _bar_row(p.get("year", "Unknown"), p.get("value"), max_value)
            for p in points
        )
        blocks.append(f"""
    <section>
      <h2>{escape(trend.get('label', 'Trend'))}</h2>
      {rows_html}
    </section>""")
    return "".join(blocks)


def _narrative_paragraphs(text: str) -> str:
    """Render one <p> per newline-separated paragraph. Plain text only, escaped."""
    if not text:
        return ""
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    return "\n".join(f"<p>{escape(p)}</p>" for p in paragraphs)


def _section_narrative_block(title: str, text: str) -> str:
    if not text:
        return ""
    return f"""
    <section class="narrative">
      <h2>{escape(title)}</h2>
      {_narrative_paragraphs(text)}
    </section>"""


def _section_data_gaps(gaps: list) -> str:
    if not gaps:
        return ""
    items = "\n".join(f"<li>{escape(g)}</li>" for g in gaps)
    return f"""
    <section class="callout">
      <h2>What would sharpen this</h2>
      <ul>{items}</ul>
    </section>"""
```

- [ ] **Step 5: Add narrative/callout CSS**

Modify `scripts/finance_report.py:173-198`, replacing the closing lines of the `CSS`
string:

```python
tr.over td { color:#c0392b; }
.stat { background:#f2f2f5; border-radius:8px; padding:12px 16px; display:inline-block; }
.stat-value { font-size:20px; font-weight:600; }
footer { margin-top:24px; font-size:11px; color:#9a9a9e; }
"""
```

with:

```python
tr.over td { color:#c0392b; }
.stat { background:#f2f2f5; border-radius:8px; padding:12px 16px; display:inline-block; }
.stat-value { font-size:20px; font-weight:600; }
.narrative p { font-size:14px; line-height:1.5; margin:0 0 10px; color:#2c2c2e; }
.narrative p:last-child { margin-bottom:0; }
.callout { background:#fff8e6; border:1px solid #f0dca0; border-radius:8px; padding:16px 20px; }
.callout h2 { border-bottom:none; margin-bottom:8px; color:#8a6d1d; }
.callout ul { margin:0; padding-left:20px; font-size:13px; color:#5c4a13; }
footer { margin-top:24px; font-size:11px; color:#9a9a9e; }
"""
```

- [ ] **Step 6: Add the `annual` branch to `render_html`**

Modify `scripts/finance_report.py:201-246`. Replace the `sections = [...]` block:

```python
    sections = [
        _section_by_who(payload.get("by_who", [])),
        _section_by_category(payload.get("by_category", [])),
    ]
    if period == "monthly":
        sections.append(_section_budget(payload.get("budget", [])))
        sections.append(_section_net_worth(payload.get("net_worth", {})))
```

with:

```python
    narrative = payload.get("narrative", {}) or {}

    if period == "annual":
        sections = [
            _section_narrative_block("Executive summary", narrative.get("executive_summary")),
            _section_by_who(payload.get("by_who", [])),
            _section_by_category(payload.get("by_category", [])),
            _section_narrative_block("Income & spending", narrative.get("income_and_spending")),
            _section_by_month(payload.get("by_month", [])),
            _section_trends(payload.get("trends", [])),
            _section_budget(payload.get("budget", [])),
            _section_net_worth(payload.get("net_worth", {})),
            _section_narrative_block("What drove net worth", narrative.get("net_worth_narrative")),
            _section_narrative_block("Looking ahead", narrative.get("looking_ahead")),
            _section_data_gaps(narrative.get("data_gaps", [])),
        ]
    else:
        sections = [
            _section_by_who(payload.get("by_who", [])),
            _section_by_category(payload.get("by_category", [])),
        ]
        if period == "monthly":
            sections.append(_section_budget(payload.get("budget", [])))
            sections.append(_section_net_worth(payload.get("net_worth", {})))
```

- [ ] **Step 7: Add the `annual` branch to `build_telegram_summary`**

Modify `scripts/finance_report.py:249-297`. Replace the function's opening (through
the `lines = [...]` line):

```python
def build_telegram_summary(payload: dict, period: str) -> str:
    period_label = payload.get("period_label", period.capitalize())
    totals = payload.get("totals", {}) or {}
    expenses = totals.get("expenses")
    prior_expenses = totals.get("prior_expenses")
    income = totals.get("income")
    savings_rate = totals.get("savings_rate")

    lines = [f"📊 {period_label}"]
    if expenses is not None:
```

with:

```python
def build_telegram_summary(payload: dict, period: str) -> str:
    period_label = payload.get("period_label", period.capitalize())
    totals = payload.get("totals", {}) or {}
    expenses = totals.get("expenses")
    prior_expenses = totals.get("prior_expenses")
    income = totals.get("income")
    savings_rate = totals.get("savings_rate")
    narrative = payload.get("narrative", {}) or {}

    lines = [f"📊 {period_label}"]
    if period == "annual":
        lines.extend(narrative.get("telegram_highlights", []))
    if expenses is not None:
```

Then, further down in the same function, replace:

```python
    if period == "monthly":
        nw = payload.get("net_worth", {}) or {}
```

with:

```python
    if period in ("monthly", "annual"):
        nw = payload.get("net_worth", {}) or {}
```

(The rest of that `if` block — the over-budget check — is unchanged; it now also
applies to `annual` since annual reports include a `budget` section.)

- [ ] **Step 8: Add `"annual"` to the argparse `--period` choices**

Modify `scripts/finance_report.py:302`, replacing:

```python
    parser.add_argument("--period", required=True, choices=["weekly", "monthly"])
```

with:

```python
    parser.add_argument("--period", required=True, choices=["weekly", "monthly", "annual"])
```

- [ ] **Step 9: Re-run the fixture and verify success**

Run: `python scripts/finance_report.py --period annual --data-file state/finance_reports/_smoke-annual.json`
Expected: PASS — prints JSON with `html_path` and `telegram_summary`. In
`telegram_summary`, confirm it contains `"Spending down 4.5% YTD"` (a
`telegram_highlights` line) and `"Net worth"`.

Then check the generated HTML file (path from the printed `html_path`) contains all
of: `Executive summary`, `Spending by month`, `Net worth by year`, `Total spending by
year`, `What drove net worth`, `Looking ahead`, `What would sharpen this`, and does
**not** contain any `<script`, `http://`, or `https://` reference (confirming no
external assets) other than inside escaped narrative text.

- [ ] **Step 10: Confirm weekly/monthly still work (regression check)**

Run the existing weekly and monthly smoke payloads from Phase 2 (recreate them inline
if not still present — see the payload shapes documented in the module docstring) and
confirm both still produce valid output with no `annual`-only sections appearing.

- [ ] **Step 11: Clean up the smoke-test fixture and output**

```bash
rm -f state/finance_reports/_smoke-annual.json state/finance_reports/annual-*.html
```

- [ ] **Step 12: Commit**

```bash
git add scripts/finance_report.py
git commit -m "$(cat <<'EOF'
feat(finance): add annual report period to finance_report.py

Adds month-by-month, multi-year trend, and agent-authored narrative
sections (executive summary, income/spending, net worth, looking ahead,
data gaps) for the Phase 4 annual financial review, reusing the existing
category/who/budget/net-worth renderers rather than duplicating them.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Extend `finance_store.py`'s `report` subcommand with `annual`

**Files:**
- Modify: `scripts/finance_store.py:43-46` (docstring), `:308-310`
  (`cmd_report_add` validation), `:493`, `:502` (argparse choices)

**Interfaces:**
- Consumes: existing `finance_report_log` table (`state/schema.sql`) — `period` column
  is already free-text `TEXT`, no migration needed.
- Produces: `finance_store.py report add --period annual ...` and
  `report list --period annual` now valid — Task 4's scheduled-task prompt and any
  future `finance-reporter` annual workflow rely on this.

- [ ] **Step 1: Confirm the current behavior fails for `annual`**

Run: `python scripts/finance_store.py report add --period annual --range-start 2026-01-01 --range-end 2026-11-21`
Expected: FAIL — argparse rejects `annual` with "invalid choice."

- [ ] **Step 2: Update the module docstring**

Modify `scripts/finance_store.py:43-46`, replacing:

```python
    python scripts/finance_store.py report add --period weekly|monthly \
        --range-start 2026-07-13 --range-end 2026-07-19 \
        [--drive-file-id <id>] [--drive-url "..."] [--summary "..."]
    python scripts/finance_store.py report list [--period weekly|monthly] [--limit 10]
```

with:

```python
    python scripts/finance_store.py report add --period weekly|monthly|annual \
        --range-start 2026-07-13 --range-end 2026-07-19 \
        [--drive-file-id <id>] [--drive-url "..."] [--summary "..."]
    python scripts/finance_store.py report list [--period weekly|monthly|annual] [--limit 10]
```

- [ ] **Step 3: Update `cmd_report_add`'s validation**

Modify `scripts/finance_store.py:308-310`, replacing:

```python
def cmd_report_add(args: argparse.Namespace) -> int:
    if args.period not in ("weekly", "monthly"):
        return _err("--period must be 'weekly' or 'monthly'")
```

with:

```python
def cmd_report_add(args: argparse.Namespace) -> int:
    if args.period not in ("weekly", "monthly", "annual"):
        return _err("--period must be 'weekly', 'monthly', or 'annual'")
```

- [ ] **Step 4: Update the argparse choices**

Modify `scripts/finance_store.py:493`, replacing:

```python
    rpt_add.add_argument("--period", required=True, choices=["weekly", "monthly"])
```

with:

```python
    rpt_add.add_argument("--period", required=True, choices=["weekly", "monthly", "annual"])
```

Modify `scripts/finance_store.py:502`, replacing:

```python
    rpt_list.add_argument("--period", default=None, choices=["weekly", "monthly"])
```

with:

```python
    rpt_list.add_argument("--period", default=None, choices=["weekly", "monthly", "annual"])
```

- [ ] **Step 5: Verify add/list/update round-trip for `annual`**

```bash
python scripts/finance_store.py report add --period annual --range-start 2026-01-01 --range-end 2026-11-21 --summary "smoke test"
```
Expected: PASS — prints a JSON row with `"period": "annual"`. Copy the returned `id`
for the next commands.

```bash
python scripts/finance_store.py report list --period annual --limit 5
```
Expected: PASS — the row from above appears.

```bash
python scripts/finance_store.py report update --id <id-from-above> --drive-url "https://drive.google.com/file/d/smoketest"
```
Expected: PASS — returns the row with `drive_url` set.

- [ ] **Step 6: Remove the smoke-test row**

```bash
python -c "
import sqlite3
conn = sqlite3.connect('state/agent_results.db')
conn.execute(\"DELETE FROM finance_report_log WHERE summary = 'smoke test'\")
conn.commit()
print('deleted', conn.total_changes)
"
```
Expected: `deleted 1`.

- [ ] **Step 7: Commit**

```bash
git add scripts/finance_store.py
git commit -m "$(cat <<'EOF'
feat(finance): add annual as a valid report period in finance_store.py

Extends the report subcommand's --period choices from weekly/monthly to
include annual, for the Phase 4 annual financial review. No schema
change needed - finance_report_log.period is already free-text.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Add the annual workflow branch to `finance-reporter.md`

**Files:**
- Modify: `.claude/agents/finance-reporter.md:3` (description), `:63-88` (report
  workflow steps 1-2), `:141-155` (Scheduled firings section)

**Interfaces:**
- Consumes: `scripts/finance_report.py --period annual` (Task 1) and
  `scripts/finance_store.py report add --period annual` (Task 2) — both must be done
  first since this task's instructions reference them directly.
- Produces: agent-readable instructions only (no code); Task 4's scheduled-task prompt
  references "the finance-reporter subagent's annual review workflow," which this task
  defines.

- [ ] **Step 1: Update the frontmatter description to mention annual reviews**

Modify `.claude/agents/finance-reporter.md:3`, replacing:

```
description: Generates spending-summary reports from Monarch Money data — a brief Telegram summary plus a formal HTML report saved to Google Drive. Handles both on-demand requests ("give me last week's spending summary", "how did we do last month") and the scheduled weekly/monthly firings. Phase 2 of a larger personal-finance program; see docs/superpowers/specs/2026-07-22-personal-finance-agent-backlog.md. Read-only against Monarch — never tags transactions or marks anything reviewed (that's the `finance` subagent).
```

with:

```
description: Generates spending-summary reports from Monarch Money data — a brief Telegram summary plus a formal HTML report saved to Google Drive. Handles on-demand requests ("give me last week's spending summary", "how did we do last month", "give me this year's financial review"), the scheduled weekly/monthly firings, and the scheduled annual financial review (year-to-date narrative + trends). Phase 2/4 of a larger personal-finance program; see docs/superpowers/specs/2026-07-22-personal-finance-agent-backlog.md. Read-only against Monarch — never tags transactions or marks anything reviewed (that's the `finance` subagent).
```

- [ ] **Step 2: Add annual date-range resolution to workflow step 1**

Modify `.claude/agents/finance-reporter.md:68-74`, replacing:

```markdown
1. **Resolve the date range.**
   - Weekly: the last completed Monday–Sunday week relative to today.
   - Monthly: last calendar month (1st through last day).
   - On-demand: infer from the user's phrasing ("last week", "June", "this month so
     far") — if genuinely ambiguous, ask rather than guessing.
   - Also compute the **prior** equivalent range (previous week / previous month) for
     deltas.
```

with:

```markdown
1. **Resolve the date range.**
   - Weekly: the last completed Monday–Sunday week relative to today.
   - Monthly: last calendar month (1st through last day).
   - Annual: **calendar year-to-date** — Jan 1 of the current year through today (not
     a trailing 365-day window). This is a year-end planning checkpoint fired before
     the year is over, so "this year" means everything so far. An on-demand request
     naming a fully completed past year (e.g. "the 2025 annual review") uses that
     year's full Jan 1–Dec 31 instead.
   - On-demand: infer from the user's phrasing ("last week", "June", "this month so
     far", "this year", "the 2025 review") — if genuinely ambiguous, ask rather than
     guessing.
   - Also compute the **prior** equivalent range for deltas: previous week / previous
     month / for annual, **Jan 1 → the same calendar date last year** (so the
     year-over-year comparison is apples-to-apples against the same point in the
     year, not a full prior year vs. a partial current one).
```

- [ ] **Step 3: Add annual data-gathering to workflow step 2**

Modify `.claude/agents/finance-reporter.md:76-88`, replacing:

```markdown
2. **Gather data via `monarch` MCP tools** for both the current and prior range:
   - `get_spending_summary(start_date, end_date)` → overall income/expenses/savings/
     savings_rate and `by_category`.
   - **Spend by WHO:** cache the tag catalog once per run with `get_transaction_tags`,
     then for each WHO-prefixed tag call
     `get_transactions(start_date, end_date, tag_ids=[<tag id>])` and sum amounts. Do
     this for both the current and prior range so each WHO row can carry a delta.
   - **Monthly only:** `get_budgets(start_date, end_date)` for planned/actual/remaining
     per category, and `get_net_worth(start_date, end_date)` for current + prior
     month-end net worth.
   - If any Monarch call fails (other than an auth error, handled above), note the gap
     in the report rather than aborting the whole run — a report with an "unavailable"
     net-worth section beats no report.
```

with:

```markdown
2. **Gather data via `monarch` MCP tools** for both the current and prior range:
   - `get_spending_summary(start_date, end_date)` → overall income/expenses/savings/
     savings_rate and `by_category`.
   - **Spend by WHO:** cache the tag catalog once per run with `get_transaction_tags`,
     then for each WHO-prefixed tag call
     `get_transactions(start_date, end_date, tag_ids=[<tag id>])` and sum amounts. Do
     this for both the current and prior range so each WHO row can carry a delta.
   - **Monthly and annual:** `get_budgets(start_date, end_date)` for planned/actual/
     remaining per category (annual: summed across the YTD window's months), and
     `get_net_worth(start_date, end_date)` for current + prior net worth (annual:
     current vs. same-date-last-year).
   - **Annual only, additional calls:**
     - `get_spending_summary` once per calendar month within the YTD window → build
       `by_month` (chronological list of `{"name": "<Mon>", "amount": <total>}`).
     - `get_net_worth` for the year-end (or latest available) net worth of each of the
       last 3 years → build the `trends` entry `{"label": "Net worth by year",
       "points": [{"year": "<YYYY>", "value": <net worth>}, ...]}`.
     - `get_spending_summary` for the full-year total of each of the last 3 years (use
       full Jan 1–Dec 31 for completed years; the current year's total is its YTD
       figure) → build the `trends` entry `{"label": "Total spending by year",
       "points": [...]}`.
   - If any Monarch call fails (other than an auth error, handled above), note the gap
     in the report rather than aborting the whole run — a report with an "unavailable"
     net-worth section beats no report.

2b. **Author the narrative (annual only).** After gathering the data above, write the
    `narrative` payload fields yourself — this is your own synthesis, not something any
    tool produces:
    - `executive_summary`: 2-4 sentences, the year in a nutshell.
    - `income_and_spending`: paragraph(s) explaining what income/spending did and why
      (category shifts, one-time events) — separate paragraphs with a newline.
    - `net_worth_narrative`: paragraph on what drove the net-worth change this year.
    - `looking_ahead`: softer, forward-looking commentary on what this signals for
      next year and beyond. You may go beyond strictly-modeled statements here (e.g.
      "at this savings rate, X becomes more feasible") but frame it explicitly as
      directional observation, not a projection — there is no retirement/affordability
      model behind it yet.
    - `data_gaps`: a list of specific things that would make this more data-driven.
      **Always include** something naming the missing Phase 3 retirement model (e.g.
      "No retirement model yet (Phase 3) - the looking-ahead statement above is
      qualitative, not simulated."), plus anything else you notice missing (e.g. no
      stored financial goals to compare progress against).
    - `telegram_highlights`: 2-4 short lines for the Telegram brief, e.g. "Saved 34%
      of income, up from 29% last year."
    - **Every claim must cite a specific number you computed** (a delta, a percentage,
      a category name) — never a vague generality like "spending seems reasonable."
```

- [ ] **Step 4: Update workflow steps 3-6 to reference the annual period**

Modify `.claude/agents/finance-reporter.md:90-135`. In step 3 ("Assemble the JSON
payload"), replace:

```markdown
3. **Assemble the JSON payload** matching `scripts/finance_report.py`'s documented
   shape (`period_label`, `date_range`, `prior_range`, `totals`, `by_category`,
   `by_who`, and for monthly also `budget` and `net_worth`). Write it to a temp JSON
   file (use the scratchpad directory) — do not try to pass this inline as a shell
   argument.
```

with:

```markdown
3. **Assemble the JSON payload** matching `scripts/finance_report.py`'s documented
   shape (`period_label`, `date_range`, `prior_range`, `totals`, `by_category`,
   `by_who`, and for monthly also `budget` and `net_worth`; for annual also `budget`,
   `net_worth`, `by_month`, `trends`, and `narrative`). Write it to a temp JSON file
   (use the scratchpad directory) — do not try to pass this inline as a shell
   argument.
```

In step 4 ("Render the report"), replace:

```markdown
4. **Render the report:**
   ```
   python scripts/finance_report.py --period weekly|monthly --data-file <path>.json
   ```
```

with:

```markdown
4. **Render the report:**
   ```
   python scripts/finance_report.py --period weekly|monthly|annual --data-file <path>.json
   ```
```

In step 5 ("Upload to Google Drive"), replace the filename line:

```markdown
     create_file(
       title: "<weekly|monthly>-<range_end>-spending-summary.html",
```

with:

```markdown
     create_file(
       title: "<weekly|monthly|annual>-<range_end>-spending-summary.html",
```

In step 6 ("Record the report"), replace:

```markdown
   python scripts/finance_store.py report add --period weekly|monthly \
```

with:

```markdown
   python scripts/finance_store.py report add --period weekly|monthly|annual \
```

- [ ] **Step 5: Add the annual task to "Scheduled firings"**

Modify `.claude/agents/finance-reporter.md:141-155`, replacing:

```markdown
## Scheduled firings

Two scheduled tasks (`weekly-spending-summary`, `monthly-spending-summary`) fire this
agent via the orchestrator. Both follow the workflow above exactly, with one addition
for the monthly task:

**Monthly self-gate.** The monthly task's cron fires **every Saturday**. Before doing
any work, check whether today is the **last Saturday of the month**:
`(today + 7 days).month != today.month`. If it is not the last Saturday, **reply with
nothing and stop** — do not call any tools, do not post anything. This mirrors the
`kids-memory` subagent's monthly-recap self-gate
(`.claude/agents/kids-memory.md`, "Scheduled: monthly recap") — silent-when-not-due is
the convention throughout this repo. An **on-demand** monthly request ("how did last
month look") bypasses this gate entirely — the gate only applies to the scheduled
Saturday firing.
```

with:

```markdown
## Scheduled firings

Three scheduled tasks fire this agent via the orchestrator:
`weekly-spending-summary`, `monthly-spending-summary`, and
`annual-financial-review`. All follow the workflow above exactly, with cadence-specific
notes below.

**Monthly self-gate.** The monthly task's cron fires **every Saturday**. Before doing
any work, check whether today is the **last Saturday of the month**:
`(today + 7 days).month != today.month`. If it is not the last Saturday, **reply with
nothing and stop** — do not call any tools, do not post anything. This mirrors the
`kids-memory` subagent's monthly-recap self-gate
(`.claude/agents/kids-memory.md`, "Scheduled: monthly recap") — silent-when-not-due is
the convention throughout this repo. An **on-demand** monthly request ("how did last
month look") bypasses this gate entirely — the gate only applies to the scheduled
Saturday firing.

**Annual self-gate.** The `annual-financial-review` task's cron (`0 9 * 11 6`) fires
**every Saturday in November**. Before doing any work, check whether today is the
**last Saturday of November**: `(today + 7 days).month != 11`. If it is not the last
Saturday, **reply with nothing and stop** — do not call any tools, do not post
anything. This is the same self-gate pattern as the monthly task above (this repo's
cron matcher uses standard Vixie-cron OR semantics when both day-of-month and
day-of-week are restricted — see `scripts/scheduler_store.py`'s module docstring — so
a combined `day-of-month 22-28 AND Saturday` restriction cannot be expressed in a
single cron field set; the self-gate is what actually narrows it to one firing a
year). An **on-demand** annual request bypasses this gate entirely, same as monthly.
```

- [ ] **Step 6: Verify the edited file reads correctly**

```bash
grep -c "annual" .claude/agents/finance-reporter.md
```
Expected: a non-zero count (multiple mentions across description, workflow, and
scheduled firings).

```bash
python -c "
import re
text = open('.claude/agents/finance-reporter.md', encoding='utf-8').read()
required = ['--period annual', 'narrative', 'by_month', 'trends', 'telegram_highlights', 'annual-financial-review', 'data_gaps']
missing = [r for r in required if r not in text]
print('missing:', missing if missing else 'none')
"
```
Expected: `missing: none`.

- [ ] **Step 7: Commit**

```bash
git add .claude/agents/finance-reporter.md
git commit -m "$(cat <<'EOF'
docs(finance-reporter): add annual review workflow branch

Extends the report workflow with year-to-date date-range resolution,
the extra Monarch calls needed for month-by-month and 3-year trend
data, and instructions for the agent to author the narrative fields
itself, grounded in the numbers it just computed.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Register the scheduled task, mark Phase 4 built, end-to-end smoke test

**Files:**
- Modify: `docs/superpowers/specs/2026-07-22-personal-finance-agent-backlog.md`
  (mark Phase 4 built)
- Runtime: one `scripts/scheduler_store.py add` call (no file — a database row)

**Interfaces:**
- Consumes: `scripts/scheduler_store.py add --name --prompt --cron --target-chat-id`
  (existing CLI, unchanged) and everything from Tasks 1-3.
- Produces: a `finance_scheduled_tasks` row named `annual-financial-review` that other
  scheduled-task tooling (`scheduler_store.py list`, the poller) already knows how to
  read — no new interface.

- [ ] **Step 1: Find the existing chat_id to reuse**

```bash
python scripts/scheduler_store.py list
```
Expected: JSON list of existing tasks. Note the `target_chat_id` value shared by all
of them (as of this plan, `8150343517` — confirm it's still consistent before reusing
it; if it has changed, use the current value instead).

- [ ] **Step 2: Register `annual-financial-review`**

```bash
python scripts/scheduler_store.py add --name "annual-financial-review" --prompt "Delegate to the finance-reporter subagent to run its annual financial review workflow (calendar year-to-date: Jan 1 through today). It should gather data via the monarch MCP tools including month-by-month spending, year-over-year comparisons, and 3-year net worth and spending trends, author the narrative fields itself grounded in the numbers it computed, render the HTML report with finance_report.py --period annual, upload it to the Reports subfolder of the finance Drive folder, record it via finance_store.py report add --period annual, and reply into this Telegram chat with the compact telegram_summary text plus the Drive link." --cron "0 9 * 11 6" --target-chat-id "<chat_id from Step 1>"
```
Expected: PASS — prints the new task's JSON row with `"cron_expression": "0 9 * 11 6"`.

- [ ] **Step 3: Verify the schedule**

```bash
python scripts/scheduler_store.py list
```
Expected: `annual-financial-review` appears, `enabled: 1`, cron `0 9 * 11 6`.
Confirm `0 9 * 11 6` reads as: minute 0, hour 9, day-of-month wildcard, month 11
(November), day-of-week 6 (Saturday) — this fires on every Saturday in November
(4-5 times a year). Because this repo's cron matcher uses OR semantics when both
day-of-month and day-of-week are restricted (see `scripts/scheduler_store.py`), the
day-of-month-22-28 trick used elsewhere does NOT resolve to a single firing here — the
day-of-month wildcard avoids that ambiguity, and the agent's in-agent self-gate (see
Task 4 above, "Annual self-gate") narrows the actual work to the last Saturday only.

- [ ] **Step 4: Mark Phase 4 built in the backlog doc**

Modify `docs/superpowers/specs/2026-07-22-personal-finance-agent-backlog.md`,
replacing:

```markdown
## Phase 4 — Annual family financial review

**Cadence:** last Saturday in November (same self-gating approach as Phase 2's
monthly summary — fire weekly on Saturdays, self-gate to the specific week).
Can also be generated on-demand at any time, not just on the scheduled date.

**Content:** effectively Phase 2's reporting logic at a 12-month scale — spend
by category, by WHO tag, by time (month-over-month within the year), compared
to budget and to previous years (multi-year `get_net_worth`/`get_cashflow`
history). Reuse the Phase 2 HTML report generator/template rather than building
a second one — this is the same report shape at a different rollup.
```

with:

```markdown
## Phase 4 — Annual family financial review — BUILT 2026-07-22

Built as a new `annual` mode on the existing Phase 2 infrastructure — no new
script or agent. `scripts/finance_report.py --period annual` adds month-by-month,
3-year trend (net worth + total spending), and agent-authored narrative sections
(executive summary, income/spending explanation, net-worth narrative,
forward-looking commentary, and explicit data-gap callouts) alongside the
existing category/who/budget/net-worth renderers. The `finance-reporter`
subagent (`.claude/agents/finance-reporter.md`) gathers year-to-date data
(calendar Jan 1 → review date, with prior-year comparisons using the same Jan
1 → same-date window for an apples-to-apples YoY) and authors the narrative
itself, grounded in the specific numbers it computed — `data_gaps` always
flags the missing Phase 3 retirement model as a limitation. The scheduled task
`annual-financial-review` (cron `0 9 * 11 6` — every Saturday in November,
same pattern as the monthly task, plus an in-agent self-gate that checks for
the last Saturday of November and no-ops otherwise) is registered in the
scheduler. Reports are delivered the same way as Phase 2: brief Telegram highlights plus
a full HTML report in the Drive `Reports` folder, no email.

**Original spec** (kept for reference):

**Cadence:** last Saturday in November (same self-gating approach as Phase 2's
monthly summary — fire weekly on Saturdays, self-gate to the specific week).
Can also be generated on-demand at any time, not just on the scheduled date.

**Content:** effectively Phase 2's reporting logic at a 12-month scale — spend
by category, by WHO tag, by time (month-over-month within the year), compared
to budget and to previous years (multi-year `get_net_worth`/`get_cashflow`
history). Reuse the Phase 2 HTML report generator/template rather than building
a second one — this is the same report shape at a different rollup.
```

Then, in the "Suggested build order" section near the bottom of the file, replace:

```markdown
2. Phase 4 (annual review) — cheap once Phase 2's report generator exists.
```

with:

```markdown
2. Phase 4 (annual review) — BUILT, see above. Was cheap once Phase 2's report generator existed.
```

- [ ] **Step 5: End-to-end smoke test via the agent (manual, interactive)**

This step requires an interactive session where the `finance-reporter` subagent can
actually call `monarch` MCP tools — it cannot be run from a plan-execution context
without live tool access. Note this explicitly to the user rather than skipping
silently: ask them to invoke "give me this year's financial review so far" against the
`finance-reporter` subagent in a live session, and confirm:
- Real Monarch numbers populate `totals`, `by_category`, `by_who`, `by_month`,
  `trends`, `budget`, `net_worth`.
- The narrative is grounded in the specific numbers gathered (not generic filler) and
  `data_gaps` is non-empty.
- The HTML uploads to the Drive `Reports` folder as raw HTML (verify by opening the
  Drive link — it should render as a webpage, not open as a Google Doc).
- A `finance_report_log` row with `period: "annual"` exists:
  `python scripts/finance_store.py report list --period annual --limit 1`.

- [ ] **Step 6: Commit the backlog doc update**

```bash
git add docs/superpowers/specs/2026-07-22-personal-finance-agent-backlog.md
git commit -m "$(cat <<'EOF'
docs: mark Phase 4 (annual financial review) built

Registers the annual-financial-review scheduled task (every Saturday in
November, cron 0 9 * 11 6, with an in-agent self-gate to the last one)
and documents the built state, matching the Phase 2 backlog-doc convention.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```
