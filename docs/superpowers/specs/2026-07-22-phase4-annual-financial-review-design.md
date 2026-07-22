# Phase 4 — Annual family financial review — design

## Context

Phase 2 (weekly/monthly spending summaries) is built: a `finance-reporter` subagent
gathers Monarch data, renders a self-contained HTML report via
`scripts/finance_report.py`, uploads it to a Drive `Reports` folder, and replies into
Telegram with a compact brief plus the Drive link. Phase 4, per the original backlog
(`docs/superpowers/specs/2026-07-22-personal-finance-agent-backlog.md`), reuses this
same generator and agent at an annual rollup rather than building a second report
pipeline.

Unlike the weekly/monthly reports (which are purely numeric rollups), the user wants
the annual review to also answer *why* the numbers look the way they do and *what it
means going forward* — an executive summary, a narrative explanation of income/spending
and net-worth movement, and forward-looking commentary. This can't come from a
deterministic script; it requires synthesis, so the **agent** authors that narrative
text and hands it to the script as plain-text payload fields the script renders
verbatim, alongside the numeric sections it already knows how to build.

## Decisions locked in with the user

- **Delivery:** same pattern as Phase 2 — brief highlights in the Telegram message,
  full report in Drive, link in the Telegram message. No email (unchanged from Phase 2).
- **Year comparisons:** two separate comparisons, not one — (a) year-over-year vs. the
  immediately prior year, and (b) a 3-year trend (net worth and total spending).
- **Date range:** calendar **year-to-date** (Jan 1 → the review date), not a trailing
  365-day window. This matches the report's purpose as a year-end planning checkpoint
  fired before the year is actually over. Prior-year comparisons use the **same Jan
  1 → same calendar date** window last year, so YoY is apples-to-apples. An on-demand
  request for a fully completed past year (e.g. "the 2025 annual review") uses the
  full Jan 1–Dec 31 window for that year instead.
- **Narrative scope:** the agent may include softer, non-modeled forward-looking
  commentary (explicitly framed as directional, not a projection) — not just
  strictly-data-grounded statements. In exchange, the narrative must always call out
  where it's limited by missing data/capability (starting with: no retirement model
  yet — that's Phase 3), so the report itself becomes the running list of what future
  phases should add. Every narrative claim must cite a specific number the agent
  computed (a delta, a percentage, a category name) — never a vague generality.
- **Scheduling:** register the recurring task as part of this build, same as Phase 2.

## Design

### 1. Report generator (`scripts/finance_report.py`) — new `annual` period

Add `"annual"` to the `--period` choices. New payload fields on top of what
weekly/monthly already use:

```json
{
  "by_month": [{"name": "Jan", "amount": 0}, "... chronological, not sorted by size"],
  "trends": [
    {"label": "Net worth by year", "points": [{"year": "2024", "value": 0}, {"year": "2025", "value": 0}, {"year": "2026", "value": 0}]},
    {"label": "Total spending by year", "points": [{"year": "2024", "value": 0}, {"year": "2025", "value": 0}, {"year": "2026", "value": 0}]}
  ],
  "narrative": {
    "executive_summary": "2-4 sentences: the year in a nutshell.",
    "income_and_spending": "Paragraph(s) on what income/spending did and why (category shifts, one-time events).",
    "net_worth_narrative": "Paragraph on what drove net worth change this year.",
    "looking_ahead": "Softer, explicitly-directional commentary on what this signals for next year and beyond - framed as observations, not projections.",
    "data_gaps": ["Specific things that would make this more data-driven, e.g. 'No retirement model yet (Phase 3) - feasibility statements below are qualitative, not simulated.'"],
    "telegram_highlights": ["2-4 short lines for the Telegram brief, e.g. 'Saved 34% of income, up from 29% last year.'"]
  }
}
```

`by_category`, `by_who`, `budget`, and `net_worth` are **reused as-is** from the
weekly/monthly shape — their existing `amount`/`prior_amount` fields already express
"this YTD vs. same YTD window last year," no schema change needed.

**New rendering functions:**
- `_section_narrative(narrative)` — renders `executive_summary` near the top (under
  the totals row), `income_and_spending` near the category/who breakdown,
  `net_worth_narrative` near the net-worth stat, `looking_ahead` near the end, and
  `data_gaps` as a visually distinct callout box (different background, labeled "What
  would sharpen this") so it reads as transparency, not filler. Plain paragraphs,
  HTML-escaped — no markdown parsing.
- `_section_by_month(rows)` — like `_section_by_category` but chronological order
  (not sorted by amount).
- `_section_trends(trends)` — renders each trend block (net worth by year, spending by
  year) as its own mini bar-chart using the existing bar-row CSS, one row per year.

**`build_telegram_summary()`** gets an `annual` branch: leads with
`narrative.telegram_highlights`, then the existing hard numbers (spend, income,
savings rate, net worth, over-budget flags) in the same format as monthly. The Drive
link is still appended by the agent after upload, not by the script (unchanged
convention).

### 2. Agent workflow (`.claude/agents/finance-reporter.md`) — annual branch

New case in the existing report workflow:

- **Date range:** Jan 1 of the current year → today, for the scheduled/default
  on-demand case. An on-demand request naming a completed past year uses that year's
  full Jan 1–Dec 31. Prior-year comparison window: Jan 1 → same calendar date, prior
  year.
- **Data gathering** — all via existing `monarch` tools already in this agent's
  toolset, no new MCP tools needed:
  - `get_spending_summary` for the YTD window and the matching prior-year window →
    `totals` + `by_category`.
  - Per-WHO-tag `get_transactions` loop (same pattern as weekly/monthly) for both
    windows → `by_who`.
  - `get_spending_summary` called once per month within the YTD window → `by_month`.
  - `get_net_worth` for (a) current vs. same-date-last-year, and (b) year-end (or
    latest available) net worth for each of the last 3 years → feeds the net-worth
    `trends` block.
  - Three `get_spending_summary` annual-total calls (current + 2 prior years) → feeds
    the spending `trends` block.
  - `get_budgets` summed across the YTD window's months → `budget`.
- **Narrative authoring:** after gathering the data above, the agent writes the
  `narrative` fields itself, grounded in the specific numbers just computed. This is
  the agent's own reasoning — no script/tool call produces it. `data_gaps` must always
  include the Phase 3 retirement-model gap plus anything else the agent notices missing
  (e.g. no stored financial goals to compare progress against).
- **No in-agent self-gate needed** — unlike the monthly report (which fires weekly and
  must self-select "last Saturday"), the annual task's cron is already constrained to
  fire once a year (see scheduling below).
- Everything else matches the weekly/monthly path exactly: assemble payload →
  `finance_report.py --period annual` → find/create the (already-cached) Drive
  `Reports` folder → upload HTML (`disableConversionToGoogleType: true`) →
  `finance_store.py report add --period annual ...` → reply with `telegram_summary` +
  Drive link.

### 3. Scheduling

New scheduled task `annual-financial-review`. Cron can't express "last Saturday of
November" directly. The day-of-month-range trick (day-of-month 22-28 AND Saturday)
would work under *AND* semantics for restricted day-of-month + day-of-week fields, but
this repo's cron matcher (`scripts/scheduler_store.py`) implements standard Vixie-cron
*OR* semantics instead: when both fields are restricted, a date matches if *either*
matches. Under that matcher, `0 9 22-28 11 6` fires on every day 22-28 of November
*plus* every Saturday of November — about 10 times, not once. So instead this follows
the same pattern as the monthly report: cron restricted to Saturday only, scoped to
November (`0 9 * 11 6` — day-of-month wildcard means only the day-of-week restriction
applies, no OR ambiguity), plus an in-agent self-gate that checks "is today the last
Saturday of November" and no-ops otherwise. See the "Annual self-gate" section of
`.claude/agents/finance-reporter.md` for the exact check, which mirrors the existing
monthly self-gate.

### 4. `finance_store.py` — `report` subcommand

`--period` choices extended from `["weekly", "monthly"]` to
`["weekly", "monthly", "annual"]`. No schema change — `finance_report_log.period` is
already a free-form `TEXT` column.

## Files

- **Edit:** `scripts/finance_report.py` — `annual` period choice; `_section_narrative`,
  `_section_by_month`, `_section_trends`; `build_telegram_summary` annual branch.
- **Edit:** `.claude/agents/finance-reporter.md` — annual branch in the report
  workflow.
- **Edit:** `scripts/finance_store.py` — add `"annual"` to the `report` subcommand's
  `--period` choices.
- **Edit:** `docs/superpowers/specs/2026-07-22-personal-finance-agent-backlog.md` —
  mark Phase 4 built once implemented.
- **Runtime:** one `scheduler_store.py add` call for `annual-financial-review`.

## Verification

1. Hand-write an annual test payload (including narrative fields) and run
   `finance_report.py --period annual --data-file ...`; confirm the executive summary,
   income/spending narrative, month-by-month bars, YoY comparisons (via existing
   `by_category`/`by_who`/`net_worth` prior-amount fields), 3-year trends, budget
   table, net-worth narrative, and the data-gaps callout all render correctly with no
   external asset references.
2. `finance_store.py report add --period annual ...` / `report list --period annual`
   round-trip.
3. Live run via the `finance-reporter` agent for the current YTD window — confirm real
   Monarch numbers, a narrative grounded in those specific numbers (not generic
   filler), and a successful Drive upload as raw HTML.
4. `scheduler_store.py list` shows `annual-financial-review` enabled with cron
   `0 9 * 11 6`.
