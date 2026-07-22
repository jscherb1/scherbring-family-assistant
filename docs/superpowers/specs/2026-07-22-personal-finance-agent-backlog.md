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

## Phase 3 — Retirement modeling & planning

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

## Phase 4 — Annual family financial review

**Cadence:** last Saturday in November (same self-gating approach as Phase 2's
monthly summary — fire weekly on Saturdays, self-gate to the specific week).
Can also be generated on-demand at any time, not just on the scheduled date.

**Content:** effectively Phase 2's reporting logic at a 12-month scale — spend
by category, by WHO tag, by time (month-over-month within the year), compared
to budget and to previous years (multi-year `get_net_worth`/`get_cashflow`
history). Reuse the Phase 2 HTML report generator/template rather than building
a second one — this is the same report shape at a different rollup.

## Phase 5 — Proactive recommendations & general Q&A

**Data-backed Q&A** ("can we afford a new car", "what if we did a $X home
renovation"): answer from real Monarch data (accounts, cashflow, budget
headroom) plus, once Phase 3 exists, the retirement model — running a what-if
through the Monte Carlo engine is the most rigorous way to answer "can we
afford X" for anything retirement-relevant; smaller near-term purchases can be
answered from cashflow/budget headroom alone.

**Creative recommendations** (the user's ask: "be creative... rental houses,
invest in a business, plan a trip, get a new car, etc."): this is genuinely
open-ended and worth its own brainstorm rather than a prescriptive checklist
here. Starting angles to research/consider: rental property ROI vs. the user's
liquidity and risk tolerance, opportunity cost of large cash balances sitting
idle vs. investing, debt payoff vs. investment return comparisons, tax-loss
harvesting opportunities, HSA/529/retirement account contribution optimization,
insurance/estate-planning gaps, and only then more speculative ideas like a
business investment or a big trip — weighed against the retirement model's
headroom once Phase 3 exists so recommendations are grounded in "what we can
actually afford" rather than generic advice.

## Suggested build order

1. Phase 2 (summaries) — BUILT, see above. Builds directly on Phase 1's WHO-tagging investment
   (spend-by-person reporting only works once tagging is solid) and is
   self-contained infra (HTML report + Drive + Telegram) reusable by Phase 4.
2. Phase 4 (annual review) — cheap once Phase 2's report generator exists.
3. Phase 3 (retirement modeling) — the largest lift; deserves a dedicated
   brainstorm + input interview with the user before any code.
4. Phase 5 (Q&A + recommendations) — layers on top of whichever of Phase 2/3
   exists at the time; can start as data-backed Q&A immediately and grow richer
   as Phase 3 lands.
