# Phase 5 — Proactive recommendations engine (`finance-advisor`) — design

## Context

Phases 1–4 of the personal-finance program are built: WHO-tagging (`finance`),
weekly/monthly/annual spending reports (`finance-reporter`), and the Monte Carlo
retirement model (`retirement` + `scripts/retirement_model.py`). The data-backed
"can we afford *this specific thing*" what-if routing already works: `finance`
hands retirement-relevant questions to `retirement`, which runs them through the
model and answers from the success-probability delta.

Phase 5 is the remaining piece from the backlog
(`docs/superpowers/specs/2026-07-22-personal-finance-agent-backlog.md`): the
**creative recommendations layer** — grounded, data-backed ideas for what to do
with the household's surplus, weighed against the retirement model's headroom
("what we can actually afford"), rather than generic advice. This is the *proactive
synthesis* on top of the existing Q&A plumbing, not the plumbing itself.

## Decisions locked in with the user (brainstorming)

- **New dedicated subagent `finance-advisor`**, not an extension of
  `retirement`/`finance`. It is a distinct forward-looking synthesis concern
  spanning accounts, holdings, cashflow, budget headroom, and the retirement
  model, and needs a wider read toolset than any existing agent. Keeping it
  separate preserves the clean single-concern boundaries of Phases 1–4
  (`retirement` = the model; `finance` = tagging + factual Q&A; `finance-reporter`
  = backward-looking reports).
- **On-demand only** — no scheduled firing. A backlog item captures folding an
  advisor section into the annual review report later, so the proactive layer
  also lands once a year without a separate cron task to nag.
- **Chat/Telegram answer only** — no Drive artifact, no HTML renderer. Unlike a
  spending report or the retirement workbook, recommendations are discursive; a
  well-structured ranked text answer is the deliverable.
- **Pure advice** — surfaces ranked, cited recommendations and stops. No Monarch
  writes, no Todoist/goal creation, no handoff actions. Read-only, like
  `retirement` and `finance-reporter`.
- **Stored advisor profile** for the facts Monarch cannot provide (tax bracket,
  HSA eligibility, insurance/estate coverage, liquidity target, risk tolerance):
  read → use what's present → ask only what's missing/stale → offer to persist.
  Mirrors how `retirement` stores its assumptions; keeps grounded recommendations
  sharp over time without re-interrogating the user each run.

## Design

### 1. New agent — `.claude/agents/finance-advisor.md`

Structured like `.claude/agents/retirement.md`. Frontmatter: `name:
finance-advisor`; a description with **no unquoted colon** (see the
agent-registry-YAML gap — an unquoted colon can drop the agent from the registry
until restart); `model: sonnet` (per `CLAUDE.md`); tools:

```
mcp__monarch__get_accounts, mcp__monarch__get_account_holdings,
mcp__monarch__get_net_worth, mcp__monarch__get_net_worth_by_account_type,
mcp__monarch__get_cashflow, mcp__monarch__get_budgets,
mcp__monarch__get_recurring_transactions, Bash
```

Body sections (reusing the house boilerplate from the other finance agents):

- **Data source note** — local `monarch` MCP, official paused, re-auth guidance.
- **Invoking scripts (mandatory form)** — bare `python scripts/...`, no `cd`/env.
- **Continuity** — `state_store.py query --agent finance-advisor --limit 10`
  before; `state_store.py write --agent finance-advisor ...` after, so a re-run
  references prior recommendations instead of blindly repeating them.
- **The advisor profile** — stored in `finance_config` key `advisor_profile`
  (`finance_store.py config get/set --key advisor_profile`). Fields: filing
  status, estimated marginal tax bracket, HSA-eligible HDHP (y/n) + current
  HSA/401k/IRA contributions & room, existing term-life / umbrella / will &
  beneficiaries, emergency-fund / liquidity target (months or $), risk tolerance.
  Read → use present values → ask only for missing/stale ones (bounded, not
  open-ended) → offer to persist with `config set`. Never invent one silently; if
  unanswerable this run, flag the dependent recommendation as assumption-based and
  say what's missing.
- **Deriving the Monarch-backed picture** — each run: cash & debt balances + APRs
  (`get_accounts`); allocation / holdings incl. unrealized gains/losses in taxable
  accounts (`get_account_holdings`, `get_net_worth_by_account_type`); trailing
  surplus / savings rate (`get_cashflow` — same large-payload caveat as
  `retirement`: parse `summary` from the saved file with `python`, don't load the
  whole file into context; figure at `summary[0].summary.savings`); budget
  headroom (`get_budgets`); recurring premiums/subscriptions
  (`get_recurring_transactions`). Household/dependents via
  `profile_store.py person list`.
- **Retirement headroom** — for retirement-relevant items, reuse `retirement`'s
  exact approach: load `retirement_assumptions` from `finance_config`, fill the
  Monarch-derived fields, write the assumptions/scenario JSON with the **Write
  tool** (never a throwaway script), run `python scripts/retirement_model.py
  --data-file <f>.json --paths 10000 --seed 42 --out <r>.json`, and cite the
  **success-probability delta** (base vs. what-if). Be explicit about
  nominal-vs-today's-dollars and that Monte Carlo results are probabilistic.
- **Recommendation taxonomy (grounded first):**
  - **Tier 1 — grounded (must cite real numbers):** idle-cash-vs-invest (cash
    beyond the liquidity target); debt-payoff-vs-expected-return (debt APRs vs.
    the allocation's expected return); tax-advantaged optimization (HSA / 401k /
    backdoor Roth / 529, using the advisor profile); tax-loss harvesting
    (unrealized losers in taxable holdings); insurance / estate gaps (coverage vs.
    net worth + dependents).
  - **Tier 2 — directional (clearly framed):** rental property, business,
    big trip, new car — each weighed against the model's headroom, labeled
    directional, surfaced only after the grounded tier.
- **Output** — ranked text, grounded before directional, each item labeled with
  its tier and the numbers it cites, plus a compact top-picks Telegram brief.
  Skip recommendations that don't apply (no idle cash → say so briefly, don't
  manufacture one).
- **Guardrails** — read-only Monarch; pure advice (no writes / Todoist / goals /
  handoff actions); never invent a profile fact silently; every grounded claim
  cites a computed number; Tier 2 always directional; honest about assumptions
  and gaps.

### 2. Routing edits

- `.claude/agents/finance.md` "Data-backed questions": open-ended "what should we
  do with our money / any recommendations" → `finance-advisor` (parallel to the
  existing pointer sending retirement-relevant what-ifs to `retirement`).
- `.claude/agents/retirement.md` "Phase 5 entry point": note the *creative
  recommendations* layer is now `finance-advisor`; `retirement` keeps the formal
  "can we afford *X*" what-if.

### 3. Backlog update

`docs/superpowers/specs/2026-07-22-personal-finance-agent-backlog.md` Phase 5
section: mark the recommendations engine **BUILT** as `finance-advisor`
(on-demand, chat-only, pure advice, stored `advisor_profile`), and add a
follow-up item: fold an advisor recommendations section into the annual review
report (`finance-reporter` annual mode).

## Reuse (do not rebuild)

- `scripts/retirement_model.py` — headroom what-ifs (CLI-invokable).
- `scripts/finance_store.py config get/set` — `advisor_profile` storage.
- `scripts/state_store.py` — continuity records.
- `scripts/profile_store.py person list` — household/dependents.

## Verification (end-to-end, on-demand)

No new scripts, so no new unit tests. Verify by invoking `finance-advisor`:

1. First run with empty `advisor_profile`: pulls Monarch data, detects missing
   profile facts, asks a bounded set of questions, persists answers via
   `config set --key advisor_profile`.
2. Grounded tier cites real numbers (actual cash vs. liquidity target, real debt
   APRs, actual unrealized-loss positions).
3. A Tier-2 item runs `retirement_model.py` and reports a success-probability
   **delta** vs. base, framed as directional.
4. Output is ranked (grounded before directional), chat/Telegram only, no Drive
   write attempted, no Monarch write attempted.
5. A `state_store` record is written; a re-run reads prior context and doesn't
   blindly repeat.
6. Registry check: `finance-advisor` appears in the agent registry (no unquoted
   colon in the description).
