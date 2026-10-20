# Open to-dos

Things still open after the WSL2 migration (finished 2026-10-06). Agents: read this first, and
update it when you finish or add something. Dates are absolute.

## Needs the user

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
   `~/.config/scherbring-assistant/.env` and the channel plugin's own copy), and revoke old Google/Monarch
   sessions. Timing is the user's call.
6. **Decide whether to ask GitHub support to purge cached old commits.** The repo is public, and
   history was scrubbed with `git filter-repo`, but the old commits can stay reachable through cached views
   and PR refs.
7. **Delete the leftovers that still contain pre-scrub data** once the above is settled:
   `~/backups/scherbring-mirror-20261005.git` (pre-scrub mirror: it still holds the personal data that
   was scrubbed), `~/work/scrub`, `~/.config/scherbring-assistant/.env.bak`, `the .wslconfig.bak next to your Windows .wslconfig`.
8. **Check the Windows backup folder is covered.** Backups land in `C:\Users\<you>\Backups\assistant`.
   Make sure File History or OneDrive includes it, and consider a periodic `wsl --export` of the distro.

## Telegram bridge (branch `feat/telegram-bridge`, cut over 2026-10-06)

`telegram_bridge.py` + `agent_worker.py` now serve the production bot (design and runbook:
`docs/OPERATIONS.md`, "Telegram bridge"). Roll back any time with
`scripts/agent_ctl.py mode plugin --yes` (restarts the old orchestrator; the bridge keeps the old
conversation log current so context survives). The user is soak-testing it for real use.

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

After the soak (about a week of normal use): remove the plugin path (`start_orchestrator.sh`,
`assistant-orchestrator.service`, the log-grep watchdog, the context-bridge hooks in
`.claude/settings.json`), merge `feat/telegram-bridge`, and rotate the bot token (item 5 above).
Known small gap: the dashboard's context-size figure reads 0 until the first turn after a compaction.

## Agent follow-ups

9. **Test the two scheduled tasks that have never run on WSL.** Dates are when each next fires on its own:
   - `retirement-quarterly-review`: next real run **2026-10-31**. Test before then.
   - `annual-financial-review`: next real run **2026-11-28**.

   ```
   python scripts/scheduler_run_now.py --name retirement-quarterly-review --ignore-gates
   python scripts/scheduler_run_now.py --name annual-financial-review --ignore-gates
   ```
   Judge by the Telegram message and the printed debug log, not the `ok` status: look for "did NOT
   complete", permission denials, and a Drive link. Watch the retirement run's duration against the 300 s
   limit (`TASK_TIMEOUT_SECONDS` in `scripts/scheduler_dispatch.py`; raise it if close). Each run writes a
   real Drive file (annual: Reports folder; retirement: Retirement folder) and database rows
   (`finance_report_log`, `retirement_*` config), so trash the Drive files and delete the rows afterwards, as
   was done for the weekly and monthly tests on 2026-10-06. Both earlier attempts failed only on the account
   session limit; the permission fixes (commit `dc61b97`) are unproven for these two.
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
    - Restart causes are inferred by matching the exit log to the watchdog logs, and those logs are pruned
      after 14 days. Recording a structured cause (for example a `cause` field in
      `state/orchestrator_restarts.jsonl` written by the watchdog) would make the history exact and permanent.
    - Every dashboard page is view-only (insights for home, family, finance, Hy-Vee, fitness, meals, activity
      added 2026-10-06). Recipes can now be added, edited and
      deleted (2026-10-12); the other sections are still view-only (habits and chores aside).
    - Sensitive areas (kids memory text, profile fact values, finance amounts) are deliberately hidden until
      auth exists; revisit what to reveal once there is a login.
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

- [ ] Registered `myhub-refresh` (every 2 h, 7am-9pm; replaced the morning/evening pair); it has not fired yet. Until the
      first run the page says calendar/tasks are "not synced yet". Check `scheduler_store.py runs`.
- [ ] The look (JARVIS theme) and the phone layout have not been viewed in a browser; only tested by route tests.
- [ ] Not on the hub yet (phase 2): finance items waiting on a decision (e.g. WHO tags; Monarch is live-only, so it
      needs a snapshot like calendar/tasks), Hy-Vee cart state, upcoming birthdays, Costco/shopping list counts, an
      expiring-login warning before it dies (only dead ones show now), per-area detail tabs.
- [ ] Older features (home maintenance, system health) have their rules re-read inside `hub_providers/` rather than shared;
      migrate with the rest of the old-feature duplication above.

## System health (PR 1 built 2026-10-14)

- [ ] Hub notify-check (2026-10-17): verify live. The 7am refresh should send one digest, later refreshes stay silent unless something is new/escalated, urgent repeats every 6 h, nothing after 8pm except a new urgent item; dismissing on Summary or My Hub must stop the message. Check `hub_notified` and the run log after a day.
- [x] PR 2 built 2026-10-14: `myhub` agent pings Todoist, Calendar, Gmail, Home Assistant (`system-ping-morning` 6:35am,
      `system-ping-evening` 5:05pm); a Telegram alert fires once when a system goes down and once on recovery; an agent
      ping older than 30 h shows amber. Gmail/Home Assistant pings failed twice in the long-lived worker session because its cached `myhub` definition lacks the new tools; verify on the first scheduled (fresh-session) run, restarting the agent worker if still missing. Also unchecked: the `myhub` agent can call
      the Gmail `list_labels` / Home Assistant `GetDateTime` tools under its new tool list.
- [x] PR 3 built 2026-10-14: `workout_sync` now reads activities from the health MCP (`querySportRecords`, matched the web
      API's 16 activity days 9/11-10/12 exactly) and falls back to the web API if the MCP fails. MCP history only starts
      ~2026-09-05, so deeper backfills need `--source web`. Day-rating metrics (`coros_wellness`) deliberately stay on
      web: the MCP has no fatigue or daily training load, its resting HR is a different measure (53 vs 48 bpm), and its
      load numbers differ, so switching would break the stored series. HRV matches, so it could move later. Verify the
      6am `workouts-habit-sync` run reports `"source": "mcp"`.
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
- [ ] HRV and load are only as fresh as the weekly COROS web pull. Moving them to the official MCP
      (`querySleepHrv`, `queryTrainingLoadAssessment`) would let the 30-minute timer keep them current.
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

## Habits tracker (built 2026-10-12; agent and dashboard verified live)

- `habits-daily-nudge` is registered (daily 7 PM) and live-tested 2026-10-12: silent path verified (nothing at risk).
  The message path (a real at-risk habit) is not yet seen end to end; watch the first real nudge.
- The habits page is the dashboard's second write surface and still has no login (see the auth note under
  Agent follow-ups); real auth is needed before other family members use it.
- Later ideas: history/heatmap view, per-habit reminder times, more frequent proactive nudges, monthly goals.
- Migrate older features (home-maintenance, lawn-garden, ...) to the shared-service pattern in `CLAUDE.md`
  so their CLI and dashboard reader stop duplicating logic.

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
- [ ] Faster refresh: confirm `assistant-insights-refresh.timer` keeps firing (`systemctl --user list-timers`) and that the first
      `myhub-refresh` runs (7am onward) land; tune the intervals if the footer shows amber sources.

- [ ] Scheduled runs share the development checkout (decided 2026-10-19: no separate worktree). A failed run now carries a `[possible cause: checkout is on branch ...]` note, but keep dev work off the main checkout (use `git worktree add`) where possible. Verify `dayrating-weekly-insights` on its next Sunday 19:30 run (should be `ok`, no denials).
