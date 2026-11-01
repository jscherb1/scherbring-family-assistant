# Open to-dos

Things still open after the WSL2 migration (finished 2026-10-06). Agents: read this first, and
update it when you finish or add something. Dates are absolute.

## Needs the user

0. **Golf: optional tidy-up.** Imported courses keep the sheet's labels (e.g. "Oaks - Kasey and Tyler");
   `/golf/courses` can add/update but not delete. (Sheets mirror and scorecard-photo flow both verified live
   2026-10-24.)

1. **Save the backup key, then delete it from WSL.** `scripts/backup.py init-key` wrote the private
   key to `~/backup-age-key.txt` (2026-10-06). Copy it into the password manager, then
   `shred -u ~/backup-age-key.txt`. Until then the encrypted secrets backups cannot be restored
   anywhere else. See `docs/OPERATIONS.md`, "The encryption key".
2. **Shred `~/migration/`.** It still holds the migrated credentials, sessions and old state. Everything
   in it is in place and in the first backup. The agent cannot do this itself because `~/migration` is
   on the project's deny list. Run: `find ~/migration -type f -exec shred -u {} + && rm -rf ~/migration`
3. **Google OAuth app stays in Testing (decided 2026-10-14).** Publishing to production needs a privacy
   policy link on a domain you own and have verified in Search Console, so we did not publish. Drive's login
   expires every 7 days; refresh it from any device with the dashboard **Reconnect** page or by telling the
   agent "reconnect drive" (`scripts/reauth.py`). Note: `drive_upload.py` uses the full `drive` scope, not
   `drive.file`.
4. **Old Windows machine.** Before it pulls anything it must run `git fetch && git reset --hard
   origin/main` (or re-clone), because history was rewritten on 2026-10-05. Its four scheduled tasks
   (`PersonalAssistant{Orchestrator,Scheduler,Watchdog,HaMcp}`) must stay disabled, or two pollers will
   fight over one Telegram bot token.
5. **Rotate secrets after wiping the old machine.** The Telegram bot token (BotFather, then update
   `~/.config/scherbring-assistant/.env` and the old channel plugin's copy in `~/.claude/channels/telegram/`, which `backup.py` and `paths.telegram_env_path` still read until the token is rotated), and revoke old Google/Monarch
   sessions. Timing is the user's call.
6. **Decide whether to ask GitHub support to purge cached old commits.** The repo is public, and
   history was scrubbed with `git filter-repo`, but the old commits can stay reachable through cached views
   and PR refs.
7. **Delete the leftovers that still contain pre-scrub data** once the above is settled:
   `~/backups/scherbring-mirror-20261005.git` (pre-scrub mirror: it still holds the personal data that
   was scrubbed), `~/work/scrub`, `~/.config/scherbring-assistant/.env.bak`, `the .wslconfig.bak next to your Windows .wslconfig`.
8. **Check the Windows backup folder is covered.** Backups land in `C:\Users\<you>\Backups\assistant`.
   Make sure File History or OneDrive includes it, and consider a periodic `wsl --export` of the distro.

## Scheduler `degraded` status and HA tool names (PRs merged 2026-10-31 and 2026-11-01)

- [ ] `degraded` status has not been seen on a real run yet; when one appears, check that `scheduler_store.py runs` shows the
  denied command. `permission_denials` was confirmed from the bundled CLI schema only.
- HA connector tools were verified in a Telegram turn for a read (`GetLiveContext`). The porch light is not exposed to the
  connector, so it goes through the local HA server (`ha_call_service`).
- [ ] Agent on/off through the `Hass` intents is still unverified.
- [ ] Watch whether `daily-weather-check` and `workouts-habit-sync` still produce denials after the prompt rule (one plain
  command per Bash call).

## Plugin path removed (2026-10-31, branch `chore/remove-plugin-path`)

`telegram_bridge.py` + `agent_worker.py` serve the production bot (runbook: `docs/OPERATIONS.md`, "Telegram
bridge"). The soak ended and the plugin-era path is removed from the repo: orchestrator launch scripts and unit,
the log-grep Telegram watchdog, the context-bridge hooks, `agent_ctl.py mode`, and the dashboard restart history
(so the "record a structured restart cause" dashboard follow-up is moot).

Host steps done 2026-11-01: the orchestrator unit was disabled and deleted, the gitignored `state/` leftovers were
removed, and the worker and dashboard were restarted on the merged code. Still open (besides rotating the bot token, item 5):
- [ ] Optional, Windows: `Unregister-ScheduledTask -TaskName WSL-Assistant-Window -Confirm:$false` (the attach window).

Deferred by the user (do later, none urgent):
- **Send files/photos back through the bridge** (the plugin could; the bridge only receives them
  today and replies text-only). Also message edits and reactions from the agent side.
- **External dead-man's switch** for whole-host outages (e.g. healthchecks.io ping from the canary;
  user would create the free check and put `HEALTHCHECK_PING_URL` in `.env`). Not wanted for now.
- **Full reboot drill** with the new units: `wsl --shutdown`, confirm bridge and worker come back and
  the session resumes. Briefly takes everything down, so do it with the user present.
- Dashboard full-content stream view: would need a token-protected page (the dashboard has no
  login). The one-shot "ask" box shipped 2026-10-07 (see `dashboard/README.md`). `agent_ctl say` /
  `tail` cover the rest for now.

Rotate the bot token when ready (item 5 above).
Known small gap: the dashboard's context-size figure reads 0 until the first turn after a compaction.

## Agent follow-ups

9. **Test `annual-financial-review`** (never run on WSL; next real run **2026-11-28**).
   `retirement-quarterly-review` ran on 2026-10-31 (it hit a `sed` denial, since fixed by a prompt rule; PR #52 fixed the
   likely cause).

   ```
   python scripts/scheduler_run_now.py --name annual-financial-review --ignore-gates
   ```
   Judge by the Telegram message and the printed debug log, not the `ok` status: look for "did NOT
   complete", permission denials, and a Drive link. Each run writes a real Drive file (Reports folder) and database
   rows (`finance_report_log`), so trash the Drive file and delete the rows afterwards, as was done for the weekly and
   monthly tests on 2026-10-06.
9a. **Retirement model improvements (Phases 1-3).** Plan: `docs/superpowers/specs/2026-10-31-retirement-model-review.md`
   (review findings + phased build). Built and merged: Phase 1 (snapshot-derived inputs, capital-market returns,
   glide path, median-path and timing fixes; PR #53) and Phase 2a (taxes, buckets, RMDs, two-person Social
   Security, mortgage / healthcare / college cash flows, emergency reserve, after-tax spending; PR #54). Latest
   workbook: `Retirement-Model-2026-10-31-phase2a.xlsx` in the Drive Retirement folder. Open:
   - [ ] **User inputs** (placeholders in `retirement_assumptions.household`, flagged in every run):
     - [ ] actual gross salaries (estimated from take-home pay; the 10% deferral is capped at the IRS limit)
     - [ ] employer retirement contributions / match (assumed 0)
     - [ ] the second spouse's SSA estimate at 67 (assumed)
     - [ ] fund mix of the employer plans whose provider returns no holdings (the run's `warnings` name them; set
       each with `retirement_store.py accounts classify --account "<name>" --allocation '{...}'`)
     - [ ] then re-run and publish a new dated workbook
   - [ ] **Decide on the idle retirement cash** (cash sweeps and money market inside the IRAs). The model flags it
     every run; the "Idle cash invested" scenario shows the effect.
   - [ ] **Phase 2b:** spending guardrails, variable (stochastic) inflation, fat-tailed returns, stress tests (bad
     first year of retirement, high-inflation decade).
   - [ ] **Roth-conversion what-if:** large pre-tax balances make RMD-era taxes the biggest lifetime cost; model
     converting up to a bracket between retirement and 75.
   - [ ] **Early-retirement access rules:** the rule of 55 and 72(t) payments aren't modeled, so retire-before-60
     results are conservative (10% penalty assumed).
   - [ ] **Phase 3:** solver metrics (max safe spend, earliest retirement age), pinned baseline for real
     plan-vs-actual, sensitivity tab, dashboard section (hide dollar balances by default).
   - [ ] **Yearly:** refresh `scripts/retirement_data/cma.json` after J.P. Morgan's LTCMA (Oct) and Vanguard's
     outlook (Dec), and `scripts/retirement_data/tax_<year>.json` after the IRS (Oct/Nov) and Minnesota (Dec)
     publish the next year's figures (the Minnesota single-filer figures are estimated).
   - [ ] **Untested live:** the quarterly task on the new workflow (next real run 2027-01-30; new dated workbook per
     run). Test with `scheduler_run_now.py --name retirement-quarterly-review --ignore-gates`, then trash the test
     Drive file and its `finance_report_log` row.
10. **Reboot test: done** by the user on 2026-10-06 (the assistant came back on its own). Nothing left.
11. **Live-test the deny rules.** `tests/test_settings_deny.py` checks the rule list, but a real headless
    `claude --print` run that tries to read a secret has not been done (the harness blocked the attempt).
    Do it once from a terminal.
12. **The `Bash(*.env*)` deny rule is broad.** It also refuses harmless agent commands that merely mention
    `.env` (such as `.env.example`). Narrow it if it gets in the way; the Read/Edit rules protect the
    real files regardless.
13. **Optional hardening:** turn off WSL interop (`/etc/wsl.conf`, `[interop] enabled=false`) so the agent
    cannot reach `powershell.exe` or `/mnt/c`. This also stops the backup job writing to
    `/mnt/c/...`, so move the backup target first.
14. **Known limitation:** COROS allows one web session per account, so each automated login signs the user
    out of COROS elsewhere.

15. **Dashboard follow-ups** (the FastAPI dashboard shipped 2026-10-06, see `dashboard/README.md`):
    - Every dashboard page is view-only (insights for home, family, finance, Hy-Vee, fitness, meals, activity
      added 2026-10-06). Recipes can now be added, edited and
      deleted (2026-10-12); the other sections are still view-only (habits and chores aside).
    - Sensitive areas (kids memory text, profile fact values, finance report text/profile values) are deliberately
      hidden until auth exists. Money amounts were opened up on 2026-10-21 (Tailscale-only); revisit with a login.
    - Fitness shows planned workouts only: `completion_status` is never set by any automation.
    - The meal menu is read from `agent_results.detail_json`; a dedicated meal-plan table would be sturdier.
    - The dashboard has no login; it relies on the bind address (localhost or Tailscale). The Summary
      page's ask box (2026-10-07) is the first write route and assumes a single user (Justin). Before
      other family members use it, add real auth plus a per-user chat-id mapping.

## Kid chore chart (built 2026-10-12; service, CLI, dashboard and kid page tested, not yet used live)

- **User actions:** add the kids and chores (dashboard Chores → Chores & kids, or ask the `chores` agent); set a
  set up a tablet/old phone on the tailnet and add `/kids` to its home screen.
- **Scheduled tasks not registered yet** (need the user's chat id; commands in README "Chores agent"):
  `chores-weekly-payday` (Sun 5 PM) and optionally `chores-afternoon-check` (weekdays 4:30 PM). Neither has run
  end to end; test the payday summary once real data exists. Restart the agent worker if the new `chores`
  subagent isn't picked up.
- **Visual check pending:** the kids' page was verified by HTML/route tests and a live request, not eyeballed on a
  tablet. Check layout, tap targets, confetti/chime on the real device. (Read-aloud of chore names was removed 2026-10-12; add it back with the
  browser's `speechSynthesis` if the kids use the screen on their own and need to know the chore.)
  Web Audio needs one tap before sound plays (normal browser autoplay rule).
- No login: a kid could browse to `/chores` and edit money. Every payout is in the ledger; add a grown-up PIN
  on the `/chores` write routes if that becomes a problem (see the auth note under Agent follow-ups).
- **Deliberately left out of v1 (2026-10-12) to keep it simple; add back if wanted:** reward shop (cash balance in for
  something), savings goals, bonuses (perfect week, streak milestones, mystery surprise) and manual balance adjustments.
  The first version of all of these is in git history on `feat/chore-chart` (commits before the simplify commit).
- Later ideas: per-kid themes and reading level, family team quest, streak freeze, Home Assistant speaker nudge
  (`HassBroadcast`) when chores are still open.

## My Hub (built 2026-10-14)

- **Hub refresh script cutover DONE 2026-11-01:** `assistant-hub-refresh.timer` installed and the `myhub-refresh` task
  disabled after 21 clean timer runs (a day earlier than planned). Rollback:
  `python scripts/scheduler_store.py enable --name myhub-refresh`. Still to verify: the first script-sent digest (see
  System health, 2026-11-02 7am).

- [ ] The look (JARVIS theme) and the phone layout have not been viewed in a browser; only tested by route tests.
- [ ] Not on the hub yet (phase 2): finance items waiting on a decision (e.g. WHO tags; Monarch is live-only, so it
      needs a snapshot like calendar/tasks), Hy-Vee cart state, upcoming birthdays, Costco/shopping list counts, an
      expiring-login warning before it dies (only dead ones show now), per-area detail tabs.
- [ ] Older features (home maintenance, system health) have their rules re-read inside `hub_providers/` rather than shared;
      migrate with the rest of the old-feature duplication above.

## System health (PR 1 built 2026-10-14)

- [ ] Hub notify-check: verify the first script-sent digest at 7am on 2026-11-02 (`hub_notify` system-health key, `hub_notified` table, run log). Later refreshes stay silent unless something is new/escalated, urgent repeats every 6 h, nothing after 8pm except a new urgent item; dismissing on Summary or My Hub must stop the message.
- [x] PR 2 built 2026-10-14: `myhub` agent pings Todoist, Calendar, Gmail, Home Assistant (`system-ping-morning` 6:35am,
      `system-ping-evening` 5:05pm); a Telegram alert fires once when a system goes down and once on recovery; an agent
      ping older than 30 h shows amber. Gmail/Home Assistant pings failed twice in the long-lived worker session because its cached `myhub` definition lacks the new tools; verify on the first scheduled (fresh-session) run, restarting the agent worker if still missing. Also unchecked: the `myhub` agent can call
      the Gmail `list_labels` / Home Assistant `GetDateTime` tools under its new tool list.
- [x] PR 3 built 2026-10-14: `workout_sync` now reads activities from the health MCP (`querySportRecords`, matched the web
      API's 16 activity days 9/11-10/12 exactly) and falls back to the web API if the MCP fails. MCP history only starts
      ~2026-09-05, so deeper backfills need `--source web`. Day-rating metrics (`coros_wellness`) stay on web:
      the MCP has no fatigue or daily training load, its resting HR is a different measure (53 vs 48 bpm), and its
      load numbers differ. Verify the 6am `workouts-habit-sync` run reports `"source": "mcp"`.
- [x] Phase 5 (2026-10-31, branch `feat/wellness-mcp-freshness`): HRV moved to the MCP. Over the 54 days both existed
      (2026-09-01 to 10-25) `querySleepHrv` equalled the stored web `coros:hrv_ms`, `hrv_vs_baseline` and the normal band
      exactly (mean and max difference 0), so `coros_health` (30-minute timer) now writes `coros:hrv_ms`,
      `hrv_vs_baseline`, `hrv_base_low/high` and `coros_wellness` no longer does; older web rows stay as history.
      `queryTrainingLoadAssessment` is stored as new `coros:mcp_load_short/long/ratio` (kept out of the insights
      analysis, shown as "Live" load freshness on Wellness); the weekly web load, fatigue, ATI/CTI and recommended range
      are unchanged.
      The backfill to 2026-09-01 was run and the live timer verified 2026-11-01 (1,094 values). A daily-load history
      before that is not available from the MCP.
- [ ] Timer `assistant-system-health.timer` installed but unobserved; check `systemctl --user list-timers` and the
      `system_health` rows' `checked_at` after a few hours. Tune the 2 h interval as needed.

## Dashboard UX overhaul (started 2026-10-19, plan in 3 PRs)

- [x] PR 1: 6-tab navigation (System behind the gear, bottom tab bar on phones), one dark HUD theme, and `/archive`
      serving every pre-redesign page through the same routes (`dashboard/README.md`, "Navigation and look").
- [ ] Check it on the phone: bottom tab bar, safe areas on the home-screen app, and the sub-nav scrolling sideways.
- [x] PR 2: merged **Today** page (`/`, Summary + My Hub), **Check-in** page (`/checkin`: rating + journal + daily
      habits in one place) and the **System status** board (`/health`). `/me`, `/rating` and `/wellness/journal?date=`
      redirect; the old pages are at `/archive/`, `/archive/me`, `/archive/rating`, `/archive/health`.
- [ ] Use Today for a few days and decide whether anything from the old My Hub is missed (vitals detail, sleep stages,
      insight story and the Systems list now live on Wellness, Insights and System status).
- [x] PR 3: long tables paged and filterable (Groceries, Activity, Agent Live, People, Lawn, Finance, Menu,
      Maintenance), habit cards tidied (Done + date on one row, Undo / Pause / Calendar below), denser recipe list on
      phones, a **Week** page (`/week`) and **jump-to search** (`/` key or the magnifier).
- [ ] Not done from the plan: one shared page-header macro (title, freshness, primary action) across every page.
      Headers are already consistent in style; revisit only if pages start to drift.
- [ ] Delete the archive about 30 days after PR 2 merges (steps in `dashboard/README.md`, "Archive").

## Wellness dashboard (built 2026-10-19, branch `feat/wellness-dashboard`)

- [x] Nightly check-in + yes/no journal built 2026-10-19 (`scripts/journal.py`, `journal` agent, check-in card on
      Wellness → Today, Wellness → Journal page, impact table on Drivers). `nightly-checkin` registered (8:30 PM) and
      the old `dayrating-nightly-nudge` disabled. Alcohol, THC, Sex and Date night moved from habits into the journal
      (6 yes days imported, habits paused).
- [ ] Watch the first `nightly-checkin` run (2026-10-19 20:30) and the first reply: check `scheduler_store.py runs`
      and that the reply landed (`journal_store.py day`). Not yet seen end to end over Telegram.
- [ ] The 7 PM `habits-daily-nudge` still runs separately (it's a "do it now" reminder for at-risk streaks); fold it
      into the check-in if two evening messages feel like too many.
- [x] Backfill the new COROS fields (acute/chronic load, recommended 7-day load range, HRV normal band, state codes):
      `python scripts/dayrating_store.py insights refresh --days 90 --source coros_wellness`. Run 2026-10-19
      (1,340 values; ATI/CTI and the recommended range from 7/20, COROS HRV band from 8/22). The weekly insights run
      keeps them current.
- [ ] Not yet seen by a real phone: the tap-to-pin readouts were checked in a Playwright mobile emulation only.
- [ ] The `wellness` and `journal` subagents are untested over Telegram.
- [ ] `insights.analyze` still counts COROS sleep scores of 0 (nights COROS didn't score) as real values; the
      wellness service treats them as missing. Decide whether the insights source should drop them too.
- Later phases (suggested): Performance tab from COROS `/analyse/query` + `/dashboard/query` (race predictor, EvoLab
  scores, threshold HR/pace, time in HR zones); MCP `queryFitnessAssessmentOverview` for VO2max;
  sleep need / sleep debt; energy and stress self-ratings; weight; Caroline's data if she wears a COROS.

## Day rating + insights (built 2026-10-14)

- [ ] Registered `dayrating-nightly-nudge` (8:30 PM) and `dayrating-weekly-insights` (Sun 7:30 PM); neither has fired yet.
      Check the first runs in `scheduler_store.py runs`. 
- [ ] The AI narrative path (agent writes and saves a story) is untested end to end until there are 10+ rated days.
- [x] Calendar load source built 2026-10-14 (`calendar_load` table, `dayrating_store.py calendar sync`; agent syncs
      before each refresh). The scheduled nightly/weekly runs haven't exercised the sync yet.
- [x] COROS sleep, steps and stress built 2026-10-14 as the `coros_health` source, via COROS's official MCP server
      (`scripts/fitness/coros_mcp.py`; the Training Hub web API has no sleep). One-time browser sign-in done; the
      refresh token renews itself (~30 day access tokens). If it ever fails, re-run `coros_mcp.py auth-url` /
      `auth-finish`. The nightly/weekly `insights refresh` pulls it, so no separate sync task. Not yet used:
      naps. Wellness-check readings (`coros:wellness_*`, one query per day, last 60 days; ~3 days of data so far)
      and recovery (`coros:recovery_pct`; the server only gives a live snapshot, so it is stamped on each refresh day and
      builds up over time) were added 2026-10-14. Sparse-data guard: a metric needs 10 paired rated days (14 for
      wellness) before it is analysed; below that it is listed in `not_enough_data` with its day count. Regular
      wellness checks would help. A full refresh now takes ~50s because of the per-day wellness calls.
- [ ] Phase 2 sources not built: Monarch spending (skip or discount the
      latest day, charges post late), chores/kids activity.
- [ ] Later: optional second score (energy/stress), quick manual questions in the nightly nudge, tags from notes.

## Home Assistant pages (built 2026-10-24, branch `feat/ha-section`)

Home > Home Assistant has **Family board** (`/home/ha`) and **HA health** (`/home/ha/health`), both over
`scripts/ha.py`; the `ha-overview` agent uses `scripts/ha_store.py`. Verified live against HA (reads);
writes are covered by tests with HA stubbed.

- [ ] Try a write from the app once and put it back: Kid Door Alerts toggle, one thermostat setpoint.
- [ ] Camera snapshots show "unavailable" today because the `tapo_control` integration is failing in HA
      (HA health lists it as the top recommendation). Re-check after fixing it.
- [ ] Not built, only suggested on the health page: a scheduled "HA unreachable" / low-battery alert to
      Telegram. If wanted, register `ha_store.py health --brief` with `scheduler_store.py add`.
- [ ] Pre-existing, unrelated: `tests/test_journal.py::test_cli` fails on `main` too.

## Habits tracker (built 2026-10-12; agent and dashboard verified live)

- `habits-daily-nudge` is registered (daily 7 PM) and live-tested 2026-10-12: silent path verified (nothing at risk).
  The message path (a real at-risk habit) is not yet seen end to end; watch the first real nudge.
- The habits page is the dashboard's second write surface and still has no login (see the auth note under
  Agent follow-ups); real auth is needed before other family members use it.
- Later ideas: history/heatmap view, per-habit reminder times, more frequent proactive nudges, monthly goals.
- Migrate older features (home-maintenance, lawn-garden, ...) to the shared-service pattern in `CLAUDE.md`
  so their CLI and dashboard reader stop duplicating logic.

## Official Monarch connector (evaluated 2026-10-21)

- [x] Authorized (read + write); headless runs load it; tools cover every finance-agent write (tags, reviewed,
      rules, notes). Wired in as the `finance` agent's fallback only.
- [ ] Not yet exercised live: the fallback path (needs the vendored login down, or a test with the user present).
- [ ] Revisit a full switch (retire the vendored server and cookie logins) once Monarch serves Finicity data
      through MCP, and check the Core weekly cap in Monarch → Settings → MCP Connector after a normal week.

## Money home page + Monarch snapshot (built 2026-10-21)

- [ ] Install and watch the timer: `scripts/install_systemd.sh assistant-money-sync.timer`, then check
      `python scripts/money_store.py sync-status` after a few runs (the first auto run is a full pull, ~20 s).
- [ ] Look at `/finance` on the phone; tune the red/yellow/green thresholds after a month of use
      (`money_store.py config get|set`).
- [ ] Transactions older than 120 days that get a WHO tag in Monarch show up in the no-WHO count until the next
      daily full pull. Fine for now; run `money_sync.py --mode full` by hand if it matters.
- [ ] The savings rate is cash flow (take-home pay minus spending): payroll 401k/403b contributions aren't in
      Monarch's income, so it understates real saving.
- [ ] Seasonal tiles (e.g. a "Christmas 2026" tag total): deliberately left out; add a tile builder in `money.py`
      when wanted (the snapshot already stores tags).
- [ ] 7 visible accounts show "update required" in Monarch (bank connections tile); re-link them in Monarch.

## Not started (ideas, from README "Backlog")

See the Backlog section of `README.md`.

- [ ] `workouts-habit-sync` (daily 6am, registered 2026-10-14): script live-tested and backfilled by hand, but the scheduled dispatch has not fired yet. Check the 10/15 run in `scheduler_store.py runs`. Note COROS allows one web session per account, so a fresh login signs Justin out of COROS elsewhere.

## Remote re-auth (Reconnect), added 2026-10-14

- [ ] Live-test the Drive paste flow end to end from a phone (start, approve, paste, verify green on the hub).
- [x] Hy-Vee remote browser and cookie import: removed 2026-10-16 (neither ever worked); the desktop shortcut is the only
      sign-in, `scripts/hyvee/hyvee_connection.py` the only check.
- [ ] Hy-Vee: after a few days, read `state/hyvee_checks.jsonl` to see how long a session really lasts and whether the
      keep-alive (each passing check re-saves the session) extends it. If sessions still die in about a day, add a
      scheduled prompt asking Justin to run the shortcut.
- [ ] Faster refresh: confirm `assistant-insights-refresh.timer` keeps firing (`systemctl --user list-timers`); tune the intervals if the footer shows amber sources.

- [ ] Scheduled runs share the development checkout (decided 2026-10-19: no separate worktree). A failed run now carries a `[possible cause: checkout is on branch ...]` note, but keep dev work off the main checkout (use `git worktree add`) where possible. Verify `dayrating-weekly-insights` on its next Sunday 19:30 run (should be `ok`, no denials).

- [ ] Live-test the **Monarch Login** and **Google Drive Login** desktop shortcuts through a real expiry (only the "already working" path was exercised). The `.bat` copies are already on the Windows desktop.

## Weekly step-back review (built 2026-11-01, branch `feat/weekly-review`)

- Registered (Saturdays 10:00, `--timeout-seconds 1200`). First live test run 2026-11-01 worked: 5 findings, 128 s, no
  denials. The first scheduled run is 2026-11-07.
- [ ] **Record the cost** of the first 2-3 runs here (Opus; `python scripts/scheduler_store.py runs`, `cost_usd` and
  `duration_s`): date, cost, findings kept. Drop back to Sonnet or trim the digest if the value does not justify it. The
  Opus exception in `CLAUDE.md` is for this agent only.
- [ ] **Reinstall the scheduler unit** (`scripts/install_systemd.sh`): `TimeoutStartSec` went from 360 to 3600 so a
  1200 s task is not killed, and the dispatcher now refreshes its heartbeat each minute during a task. Until
  reinstalled, a long task would be killed at 6 minutes. While a long review runs, other due tasks wait (ticks are
  serial); a Saturday 10 AM run can delay other tasks due in that window by up to 20 minutes.
- [ ] **Known gap, not redesigned:** `scheduler_dispatch.main()` marks a task dispatched before running it. If the
  dispatcher process is killed mid-task (systemd timeout, reboot, OOM), that run is lost silently: no `ok` or
  `failed` row is logged, only the `dispatched` one, and the task does not retry until its next cron time. The
  heartbeat and the larger `TimeoutStartSec` reduce the chance for long tasks; the missed-run detection gap in
  the scheduler notes still applies.
- [ ] `home` and `lawn` and `profile_dates` digest sections read their tables directly (those features' rules live
  in their CLIs/dashboard readers, not a service). Migrate them to services as part of the older-features item,
  then point `review.py` at the services.
- [ ] Finding titles show on the Today/Summary page, so they must not carry sensitive amounts. The agent file tells
  it to keep them generic; check the first runs.

## Concurrent sessions and dev chat (built 2026-11-01, branch `feat/concurrent-sessions`)

Live-verified 2026-11-01: the household chat answered in about 3 s during a 60-second dev turn; the dev group is configured.
- [ ] Not checked live: the dev group's worktree note and the `/status@<botname>` reply, `agent_ctl.py sessions` / SDK memory
  with two live `claude` processes (cap is 4, `MAX_CONCURRENT_SESSIONS`), and a basic group upgrading to a supergroup (the
  id changes, see OPERATIONS.md).
- Not checked: whether anything shared on disk (for example one browser profile) misbehaves when two sessions use the
  same tool at once; SQLite stores are fine.

## Fast lane, Tier 0 and 1 (built 2026-11-01, branch `feat/fast-lane`)

ON since 2026-11-01 (`agent_ctl.py fastlane on`). Live-verified 2026-11-01: reads, garage status, the "already closed"
path, porch on (no change), a list add (a real add) and a Tier 1 read.
- [ ] Not verified live: habit log, day rating, a device actually moving, close garage actually closing, thermostat.
- [ ] Tier 1 median measured 3.4-4.7 s, borderline around the 4 s target; no API key for now. Revisit with
  `agent_ctl.py fastlane status` (and the Agent page Fast lane card, `state/usage.jsonl`) after a week: share of messages
  handled, median per tier, any `pass:model_*` reasons, and any message the lane took that it should not have. Options if
  too slow: an API key (faster, no idle process) or Tier 0 only.
- Known point: "Costco List" and "Costco Staples" are separate lists, and the duplicate check only looks at the target
  list. That is correct behaviour.
- Known window: the worker can crash after a fast action's reply is queued but before the row is completed. The row is
  then redelivered to the agent with the "Retry" marker, so the user may get a second reply (the action itself is not
  repeated by the lane; the agent is told to check state).
- Possible follow-ups: undo ("undo that") through the fast lane, "also add ..." style follow-ups, and wider Tier 1
  coverage once the history shows which phrasings are common.

