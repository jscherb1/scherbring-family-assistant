# Personal finance agent — backlog (Phases 2–6)

Phase 0 (Monarch connectivity via the local `monarch` MCP server) and Phase 1
(transaction review + WHO tagging, the `finance` subagent) are built — see
`.claude/agents/finance.md`, `scripts/finance_store.py`, and the `monarch` entry
in `.mcp.json`. This doc captures the rest of the program the user described, so
it isn't lost, with a recommended approach for each. None of this is built yet —
each item should get its own brainstorm/spec session before implementation,
same as Phase 1 got.

## Data source note (read this first in any follow-on session)

All financial data comes from Monarch Money. The **official** MCP
(`mcp__claude_ai_Monarch_Money__*`) has been paused by Monarch since before this
work started ("data portability question raised by one of our partners").
Phases 2–6 should use the same **local `monarch` MCP server**
(`vendor/monarch-mcp-server/`, tools `mcp__monarch__*`) that Phase 1 uses. If the
official MCP comes back online, re-evaluate switching — its tool surface is
close enough that most of these phases would only need a tool-name swap.

## Phase 2 — Weekly & monthly spending summaries — BUILT 2026-07-22

Built as a dedicated `finance-reporter` subagent (`.claude/agents/finance-reporter.md`),
separate from the transaction-tagging `finance` agent and strictly read-only against
Monarch. The report is rendered by `scripts/finance_report.py` (self-contained HTML,
inline CSS, no external assets) from a JSON payload the agent assembles from
`get_spending_summary`/`get_budgets`/`get_cashflow`/`get_net_worth`/per-WHO-tag
`get_transactions`; history is tracked in the new `finance_report_log` table via
`finance_store.py report add/list/update`. Reports upload as raw `.html` (never
converted to a Google Doc) into a `Reports` subfolder of the shared finance Drive
folder, cached in `finance_config` as `reports_drive_folder_id`. Scheduled tasks
`weekly-spending-summary` (`0 8 * * 0`) and `monthly-spending-summary` (`0 9 * * 6`,
self-gated to the last Saturday, same pattern as `kids-memory`'s monthly recap) are
registered in the scheduler.

**Email delivery was decided out of scope for this build** — the user chose Telegram +
Drive link only, deferring email until a send-capable Gmail tool (not just
`create_draft`) is available. See "Open item" below, now resolved as "not doing it yet."

**Original spec** (kept for reference / to inform Phase 4, which reuses the same report
generator at a 12-month rollup):

**Cadence:** weekly every Sunday morning (e.g. `0 8 * * 0`); monthly on the
**last Saturday of the month**. Cron can't express "last Saturday" directly —
follow the `kids-memory` self-gating pattern (`.claude/agents/kids-memory.md`,
"Scheduled: monthly recap"): fire every Saturday via the `scheduler` subagent,
and have the prompt/agent logic check whether today is within the last 7 days
of the month before actually running the monthly version; otherwise reply with
nothing (silent-when-healthy convention used throughout this repo).

**Output, per the user's spec:**
- A **brief summary sent over Telegram** by the orchestrator.
- A **formal HTML report** saved to the Drive working folder
  (https://drive.google.com/drive/folders/1He_dqUQj4pVra0kldmWO3d2cRqwrihAU —
  suggest a `Reports/` subfolder, mirroring the `kids-memory` per-child-subfolder
  pattern) **and** emailed to the user.

**Content:** budget vs. actual (`get_budgets` with `include_actuals`/actuals
support), comparison to the previous period (prior week/month via
`get_cashflow`/`get_spending_summary` over the prior date range), and **who
spent what** — aggregate by WHO tag (`WHO:*`, `Adults`, `Kids`, `Family`) using
`get_transactions`/`search_transactions` filtered by `tag_ids`.

**Open item — email delivery isn't built yet.** The Gmail MCP available in this
environment (`mcp__claude_ai_Gmail__*`) only exposes `create_draft`, not send.
Options to resolve before building Phase 2: (a) create a draft addressed to the
user and rely on them sending it (defeats "automatic" delivery), (b) add an SMTP
step (needs credentials/config), (c) re-check whether a send-capable Gmail tool
becomes available. Decide with the user before committing to an approach.

## Phase 3 — Retirement modeling & planning — BUILT 2026-07-23

Built as a dedicated `retirement` subagent (`.claude/agents/retirement.md`, `model:
sonnet`, read-only against Monarch) on two new stdlib+numpy/xlsxwriter scripts:
- `scripts/retirement_model.py` — the Monte Carlo engine (numpy, lognormal annual
  returns, accumulation → decumulation): returns `success_probability`, per-year
  p10/p50/p90 percentile trajectories, a deterministic median projection, ending-balance
  percentiles, and a depletion summary; runs base case + what-if scenarios via `run_all`.
- `scripts/retirement_workbook.py` — builds the deliverable **user-editable `.xlsx`
  modeling workbook** (xlsxwriter): Summary, an editable **Assumptions** tab, a
  **Base Case** with LIVE Excel formulas referencing Assumptions (edit an input → Excel
  recalculates), a **Monte Carlo** confidence-band tab, one tab per **scenario**, and a
  **Data Sources** tab.

Instead of the spec's Google Sheets workbook (no Sheets API tooling exists in this
environment — only Drive), the workbook is a native `.xlsx` (openpyxl/xlsxwriter both
available; numpy available, scipy not). **Delivery to Drive uses a dedicated CLI**,
`scripts/drive_upload.py` (its own Google OAuth token; see `docs/retirement-drive-setup.md`)
rather than the Drive MCP tool — the MCP path only accepts content inline and a ~70KB
binary workbook is ~93KB of base64 as a single tool argument, which is impractical and
breaks the unattended run. The CLI uploads straight from the file path and does an
**in-place `files.update`** on one canonical `Retirement-Model.xlsx` in a `Retirement/`
subfolder, so Drive's native revision history is the archive (id/url cached in
`finance_config` as `retirement_workbook_file_id`/`_url`). Assumptions live in
`finance_config` key `retirement_assumptions` (retire age
62, plan-through 95, spend $120k, inflation 3%, SS start 67 with configurable amount);
current balance, annual contribution, and allocation-derived return/volatility are
**data-driven from Monarch each run** and overridable in the workbook. The scheduled task
`retirement-quarterly-review` (cron `0 9 * 1,4,7,10 6` — every Saturday in
Jan/Apr/Jul/Oct, plus an in-agent last-Saturday self-gate) runs the plan-vs-actual
review. Engine + workbook covered by `tests/test_retirement_model.py` and
`tests/test_retirement_workbook.py`.

**Refinement backlog (not yet done — make the analysis more robust):** the first
build ships two deliberate placeholders that should become genuinely data-driven
before the model is leaned on for real decisions:
- **Asset allocation** is assumed (85/15 → return/volatility) rather than derived
  from actual holdings. Pull the real stock/bond split per account via
  `get_account_holdings` and map it to return/vol instead of the fallback.
- **Social Security** is a placeholder ($40k/yr combined at 67). Replace with a
  real estimate (from earnings history / SSA statement, or a documented
  methodology), keeping it a configurable lever.
Both are low-risk to iterate on since every value is already an editable
Assumptions cell + config field; this is a "make the numbers trustworthy" pass,
not new architecture. Logged 2026-07-23.

**Original spec** (kept for reference):

**Recommended architecture:** a **Monte Carlo simulation engine**, not
spreadsheet formulas — the user explicitly wants confidence levels and
capital-burn-down charts, which plain deterministic Sheets formulas can't
produce. Build a small Python module (stdlib + maybe `numpy` if available) that:
- Simulates N market-return paths (drawn from historical or assumed
  distributions) across the accumulation phase (today → retirement age) and the
  decumulation phase (retirement age → death age), applying the retirement
  budget as an annual withdrawal.
- Reports a **success probability** (% of simulated paths that don't run out of
  money before the death age) and **confidence bands** (e.g. 10th/50th/90th
  percentile net-worth trajectories).
- Produces a **burn-down chart** of capital post-retirement.
- Writes the results — assumptions, projections, charts — into a **Google
  Sheets** workbook (one tab for assumptions/inputs, one for the base-case
  projection + chart, one per **what-if scenario**).

**What-if capability:** each scenario (e.g. "buy a second home", "retire 3 years
early", "add a pension") is a variant input set run through the same engine,
written to its own tab/section for side-by-side comparison against the base
case — don't overwrite the base case when exploring a what-if.

**Required inputs (will need a structured interview with the user before the
first run):** retirement age, target death age (or a distribution/assumption),
annual retirement budget/spending target, current account balances (pull real
numbers via `get_accounts`/`get_account_holdings` rather than asking), current
savings rate, expected Social Security/pension income and start age, expected
investment return and inflation assumptions (offer reasonable defaults, let the
user override), risk tolerance/asset allocation. Treat this as its own
brainstorm — don't guess these into an agent file without the user's input.

**Quarterly (per the user's text — confirm exact cadence; the request mixed
"Quarterly Review" and "send monthly reviews from a retirement perspective") plan-
vs-actual review:** compare actual net worth/savings trajectory
(`get_net_worth`) against the plan's projected path, and flag if
assumptions/allocation/savings rate need revisiting.

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
scheduler.
Reports are delivered the same way as Phase 2: brief Telegram highlights plus
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

## Tooling backlog — reduce approval friction for report generation

**Logged 2026-07-22, during Phase 4 live testing.** Generating a report (weekly,
monthly, or annual) via the `finance-reporter` subagent required the user to approve
20+ tool calls in one session. Root cause diagnosed: the agent was writing a one-off
Python script (via `Bash` heredoc) purely to *compute* the payload — delta
percentages, rounding, extra fields the renderer doesn't even read — then executing
it. That's two approval-gated actions (write the script, run it) for work that needed
neither computation nor code execution, since `scripts/finance_report.py` already
derives deltas from raw `amount`/`prior_amount` pairs itself.

**First fix already applied** (same session, before this backlog note): tightened
`.claude/agents/finance-reporter.md`'s payload-assembly instructions to mandate
writing the payload JSON directly via the `Write` tool with literal values — never
generating and executing a throwaway script — and to supply only the raw numbers the
schema documents, not invented derived fields.

**Still open — worth a follow-up session:**
- Confirm in practice that `Write` calls to the scratchpad temp directory are actually
  low/no-friction as designed, now that the agent isn't reaching for `Bash`+Python at
  all for payload construction. If `Write` itself still prompts per-call, that's a
  different problem than the one just fixed and needs its own investigation (possibly
  a permission rule, possibly a harness-level question).
- Consider whether the Drive-upload and `finance_store.py report add` steps (both
  already real actions, not just data assembly) can be reduced to fewer/narrower
  approval points without loosening what actually needs a human's eyes.
- If friction persists after the `Write`-tool fix, reassess whether `finance_report.py`
  should accept the payload some other way (e.g., piped directly rather than via a
  file) to remove the intermediate scratchpad step entirely.
- Goal: enable routine report generation (especially the recurring scheduled
  weekly/monthly/annual firings, which run unattended) without requiring the user to
  approve anything at all, while keeping genuinely consequential actions (Drive
  writes, DB writes) appropriately visible.

## Phase 5 — Proactive recommendations & general Q&A

**Data-backed Q&A** ("can we afford a new car", "what if we did a $X home
renovation") — **BUILT** as part of Phase 3: the `retirement` subagent runs
retirement-relevant what-ifs through the Monte Carlo engine and answers from the
success-probability delta, and `finance` routes such questions to it. Smaller
near-term purchases are answered from cashflow/budget headroom by `finance`.

**Creative recommendations** — **BUILT 2026-07-23** as a dedicated read-only
`finance-advisor` subagent (`.claude/agents/finance-advisor.md`, `model: sonnet`).
See `docs/superpowers/specs/2026-07-23-finance-advisor-design.md` for the design.
It turns real Monarch data (cash, debt + APRs, holdings/allocation incl. taxable
unrealized losses, trailing cashflow surplus, budget headroom, recurring premiums)
plus the Phase 3 retirement model's headroom into **grounded, ranked, data-backed
recommendations** — a grounded tier first (idle-cash-vs-invest, debt-payoff-vs-
expected-return, tax-advantaged optimization HSA/401k/backdoor-Roth/529, tax-loss
harvesting, insurance/estate gaps), then directional/speculative ideas (rental,
business, big trip, new car) each framed against the model's headroom. Key
decisions: **on-demand only** (no scheduled firing), **chat/Telegram output only**
(no Drive artifact), **pure advice** (read-only, never acts — no writes, Todoist
tasks, or goals), and the facts Monarch can't provide (tax bracket, HSA
eligibility, insurance/estate coverage, liquidity target, risk tolerance) live in
a stored `advisor_profile` (`finance_config`) — asked once, persisted, never
invented silently.

**Follow-up (not yet built):** fold an advisor recommendations section into the
**annual review report** (Phase 4 / `finance-reporter` annual mode) so the
proactive layer also lands once a year, in addition to the on-demand agent.

## Suggested build order

1. Phase 2 (summaries) — BUILT, see above. Builds directly on Phase 1's WHO-tagging investment
   (spend-by-person reporting only works once tagging is solid) and is
   self-contained infra (HTML report + Drive + Telegram) reusable by Phase 4.
2. Phase 4 (annual review) — BUILT, see above. Was cheap once Phase 2's report generator existed.
3. Phase 3 (retirement modeling) — BUILT 2026-07-23, see above.
4. Phase 5 (Q&A + recommendations) — **BUILT 2026-07-23.** The data-backed "can we
   afford X" Q&A landed with Phase 3 (`retirement` runs what-ifs; `finance` routes
   to it). The creative-recommendations engine is now the read-only `finance-advisor`
   subagent (on-demand, chat-only, pure advice, stored `advisor_profile`). Only
   follow-up left: fold an advisor section into the annual review report.
