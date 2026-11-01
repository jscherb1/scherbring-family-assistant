# Retirement model review and improvement plan (2026-10-31)

Household-specific figures (balances, account names, results) are deliberately left out of
this repo document. They live in the run files under `state/retirement/` and the Drive
workbooks.

## Context

An expert review of the retirement model and process. The system as of the review:
- `scripts/retirement_model.py`: a numpy Monte Carlo over one portfolio with iid lognormal returns.
- `scripts/retirement_workbook.py`: an xlsx with a live deterministic Base Case.
- `.claude/agents/retirement.md`: the LLM derived the inputs from Monarch by hand.
- Assumptions in `finance_config.retirement_assumptions`.

The headline success probability looked reassuring, but it rested on inputs that were wrong
or missing. Correcting Phase 1's issues lowered it materially.

**Decisions:**
- Kids' accounts are excluded from retirement money.
- The base case uses forward-looking returns.
- The retirement-spend basis is derived from Monarch spending in Phase 2.
- The work ships in three phases, each its own PR.

## Review findings (ranked by impact on accuracy)

**A. Inputs**
1. **Kids' money counted as retirement money.** 529s and a kid-earmarked brokerage account
   were included in the balance, and kids' future costs weren't modeled.
2. **No taxes.** Most of the investable money is pre-tax (403b/401a/IRA), yet the model
   withdrew the spending target with no federal or MN tax, no Social Security taxation and no
   RMDs. If the target is take-home spending, gross withdrawals need to be roughly 15–25%
   higher. This is the single biggest optimism bias.
3. **Wrong contribution figure.** Trailing-12 "savings" (income − expenses) misses payroll
   deferrals and the employer match, counts cash piling up in checking as invested, and
   treats mortgage principal as spending.
4. **Optimistic, bucketed returns.**
   - A flat historical-average return with no fees.
   - The stock share snapped to the nearest bucket, although the agent doc said "interpolate".
   - No glide path.
5. **Social Security is one combined placeholder.** No per-spouse split, no claiming-age
   math (62 = 70%, 67 = 100%, 70 = 124% of the full benefit), no survivor benefit. A
   trust-fund shortfall (about 77–80% of scheduled benefits from the mid-2030s) is a more
   realistic stress than "no Social Security".
6. **Spending is one flat inflating number.** It has no:
   - mortgage handling (a fixed nominal payment that stops at payoff)
   - pre-Medicare healthcare (ages 62–65)
   - college costs
   - lumpy events
   - guardrails
7. **Checking/savings cash ignored.** Cash above an emergency reserve is a real resource and
   an advice signal.

**B. Model mechanics**
8. **The "median" deterministic path used the arithmetic mean.** The real median compounds
   at about μ − σ²/2.
9. **Year-end withdrawals** understated sequence risk.
10. **Single-factor risk.** Fixed inflation, thin-tailed returns, no deterministic stress tests.
11. **Thin validation.** No Monte Carlo standard error, so moves of about 0.1–0.4 points
    (pure noise at 10k paths) read as signal.

**C. Outputs and process**
12. **Nominal-dollar headlines.** The most useful outputs weren't produced: max sustainable
    spend, earliest retirement age, and p10 in real dollars.
13. **Plan-vs-actual compared net worth to last quarter's net worth.** That isn't a pinned
    plan.
14. **The LLM hand-derived inputs.** That made runs unreproducible. Holdings came back empty
    for some employer plans, so the allocation was a guess.
15. **Workbook gaps.** No per-account, tax, cash-flow or sensitivity views.
16. **No dashboard surface.** The CLAUDE.md agent + UI standard isn't met.

## Implementation

### Phase 1: correctness and deterministic inputs (built)

- **Shared service `scripts/retirement.py` and CLI `scripts/retirement_store.py`.**
  - They classify Monarch-snapshot accounts by subtype into `pretax | roth | hsa | taxable |
    cash | kids | exclude`, with per-account overrides in `finance_config.retirement_accounts`.
  - They derive the retirement balance, the allocation from holdings (per-ticker overrides in
    `retirement_securities`, plus name heuristics and target-date handling), and trailing
    savings.
  - They resolve assumptions, run the engine, and build the workbook payload.
  - Runs and workbooks never overwrite earlier ones.
- **Holdings sync:** `money_sync.py` stores holdings in `money_holdings` on full syncs.
- **Engine:**
  - Capital-market assumptions (`scripts/retirement_data/cma.json`, refreshed yearly) priced
    by allocation weights, net of fees, with a glide path.
  - True-median deterministic path; start-of-year withdrawals and mid-year contributions.
  - Real-dollar outputs, standard error, and stricter validation.
- **Workbook:** a per-year median-return column, today's-dollar columns, an Accounts tab, and
  a formula-parity test against the engine.
- **Agents:** `retirement` and `finance-advisor` run everything through the CLI. Every
  quarterly run uploads a new date-stamped workbook.

### Phase 2: household realism

**Inputs needed from the user:**
- both spouses' SSA estimates at 67
- deferral % and employer match
- IRA and taxable investing amounts
- any pension
- mortgage rate, P&I and payoff date
- each spouse's retirement age
- college targets
- emergency-reserve size

**Engine:**
- **Tax buckets and withdrawals:** taxable / pre-tax / Roth buckets with a configurable
  withdrawal order; RMDs from 75.
- **Tax module:** federal MFJ brackets indexed to inflation, Social Security taxation, MN
  tax, capital gains on taxable withdrawals, and a vectorized gross-up so spending is
  after-tax.
- **Two-person Social Security:** claiming-age math, a survivor step-up, and a trust-fund
  haircut scenario.
- **Cash-flow events:** mortgage P&I to payoff (nominal), ACA premiums 62–65 with a
  healthcare inflation premium, college funded first from the kids bucket, one-offs and
  part-time income.
- **Contributions and cash:** per-account contributions with real wage growth; Monarch
  savings becomes a cross-check. Excess cash counts as a cash bucket.
- **Spending:** after-tax, excluding the mortgage and kids' costs, with optional guardrails
  and a late-life real decline.
- **Risk:** optional stochastic inflation, fat-tailed equity draws, and stress scenarios.
- **Spending baseline:** a draft retirement budget built from Monarch categories, confirmed
  with the user.

### Phase 3: outputs, plan-vs-actual, dashboard

- **Solver metrics:** max safe spend at 85% and 90%, and the earliest retirement age at 90%.
- **Richer results:** real-dollar headlines, guardrail-cut stats, and a sensitivity tornado.
- **Workbook tabs:** Accounts, Cash-flow events, Taxes, Sensitivity.
- **Pinned baseline:** a `retirement_baseline` table, so the quarterly review compares the
  investable balance and actual contributions against the baseline band.
- **Dashboard section:**
  - Service-backed, with an `/api` twin and a guarded write route.
  - It hides dollar balances by default, matching `RETIREMENT_SAFE_KEYS`.

## Verification

- `pytest tests/test_retirement*.py tests/test_money.py tests/test_dashboard.py` for each phase.
- **Regression attribution** of each change's effect on success, step by step, on the same
  inputs.
- **Hand-check:** one tax gross-up year by hand.
- **Workbook formula parity** with the engine within $1.
- **End to end:** `scheduler_run_now.py --name retirement-quarterly-review --ignore-gates`
  after merging.
