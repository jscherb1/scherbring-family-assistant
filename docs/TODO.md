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
3. **Switch the Google OAuth app to production** (Google Cloud console, OAuth consent screen,
   Publish app; no verification needed for single-user use). This ends the weekly Drive re-login.
   Then run `python scripts/drive_upload.py auth` once and raise Drive's `lifetime_days` in
   `state/credential_check.json` (7 by default) or let the live probe re-learn it. Steps are in
   `docs/retirement-drive-setup.md`.
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

- [ ] Registered `myhub-refresh-morning` (6:30 AM) and `myhub-refresh-evening` (5 PM); neither has fired yet. Until the
      first run the page says calendar/tasks are "not synced yet". Check `scheduler_store.py runs`.
- [ ] The look (JARVIS theme) and the phone layout have not been viewed in a browser; only tested by route tests.
- [ ] Not on the hub yet (phase 2): finance items waiting on a decision (e.g. WHO tags; Monarch is live-only, so it
      needs a snapshot like calendar/tasks), Hy-Vee cart state, upcoming birthdays, Costco/shopping list counts, an
      expiring-login warning before it dies (only dead ones show now), per-area detail tabs.
- [ ] Older features (home maintenance, system health) have their rules re-read inside `hub_providers/` rather than shared;
      migrate with the rest of the old-feature duplication above.

## System health (PR 1 built 2026-10-14)

- [ ] PR 2: agent pings for Todoist, Calendar, Gmail and Home Assistant (`system_health_store.py record`), and a
      Telegram alert when a system goes down (state change only). Until then Todoist/Calendar are inferred from the hub
      sync and Gmail/Home Assistant show "not checked yet".
- [ ] PR 3: move the COROS web reads (activities for `workout_sync`, HRV/load for day rating) to the health MCP; the web
      login stays only for planned workouts and the library.
- [ ] Timer `assistant-system-health.timer` installed but unobserved; check `systemctl --user list-timers` and the
      `system_health` rows' `checked_at` after a few hours. Tune the 2 h interval as needed.
- [ ] Hy-Vee's verdict is the daily credential check's, not a live probe (a headless browser every 2 h is too heavy).

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
