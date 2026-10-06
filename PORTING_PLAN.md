# PORTING_PLAN.md: Windows to WSL2 Ubuntu 24.04

Audit and plan only. No code has been changed. Dated 2026-10-05.

## 0. Audit summary

**State of this checkout:** a fresh clone. No `.env`, `.mcp.json`, `state/*.db`, `vendor/`, `bun` or `sqlite3`. Present: Python 3.12 (`python3` only, no `python`), node/npm/uv, `claude`, git, tailscale. `~/.claude/channels/telegram/` does not exist yet.

### Windows-specific inventory
| Area | Where | Linux impact |
|---|---|---|
| Orchestrator launcher | `scripts/start_orchestrator.ps1` (single-instance via `Get-CimInstance Win32_Process`, 10 s restart loop, restart/exit logs, mouse-tracking reset) | Rewrite as bash |
| Watchdogs | `run_watchdog.ps1`, `watchdog_telegram_health.ps1`, `watchdog_scheduler_health.ps1`, `orchestrator_status.ps1` | Rewrite (Python preferred) |
| HA-MCP launcher | `start_ha_mcp.ps1`, `ha-mcp-web.exe` in `.venv\Scripts` | Rewrite; `.venv/bin/ha-mcp-web` |
| Task Scheduler | `register_{orchestrator,scheduler,watchdog,ha_mcp}_task.ps1`, tasks `PersonalAssistant{Orchestrator,Scheduler,Watchdog,HaMcp}` | systemd user units and timers |
| Hidden-window launchers | 3 `.vbs` files (`wscript.exe`) | Delete; systemd needs none |
| Env vars | `scheduler_dispatch.py:68,91` (`APPDATA`, `USERPROFILE`), `telegram_send.py:35`, `watchdog_scheduler_health.ps1:35` | **Silent failure:** the Telegram token resolves to a relative path and is never found, so alerts are dropped. Use `Path.home()`. |
| `claude` lookup | `scheduler_dispatch.py:_resolve_claude` raises at import if `claude` is not on PATH | Make the PATH explicit in the unit |
| `python` vs `python3` | `.claude/settings.json` hooks, allow rules, all agent Bash examples | Hooks fail silently (the bridge is best-effort). Fix with `python-is-python3` or a venv. |
| Dead rules | about 30 `Bash(cd "C:\\Users\\<old-user>\\...")` and `Edit(//c/Users/...)` entries in `.claude/settings.json`; `.claude/agents/todoist.md:21` | Remove |
| Credential store | Monarch MCP session in Windows Credential Manager (keyring) | Needs a keyring backend |
| Registry, pywin32, COM | none | n/a |
| Line endings | 96 tracked files all LF, no `.gitattributes` | OK. Add `.gitattributes` as a guard. |
| Exec bits | all files 100644; everything is run as `python x.py` | OK |
| Docs | README and GETTING_STARTED are PowerShell-centric; "Windows" is listed as a prerequisite | Update |

Not found: `winreg`, `windll`, `sys.platform` branches, `shell=True`. Paths in Python use `Path(__file__)`, so there are no drive letters in code. No real case-sensitivity bugs.

### Dependencies
- **Python, undeclared:** no root requirements file. `flask` (dashboard), `playwright` (hyvee, fitness), `numpy`, `xlsxwriter`, `google-api-python-client`, `google-auth`, `google-auth-oauthlib`, `google-auth-httplib2`, `pytest`.
- **CLIs:** `claude`, `bun` (required by the Telegram channel plugin), `python3`, Playwright Chromium (`playwright install --with-deps chromium`).
- **MCP servers:**
  - `todoist`: hosted HTTP, browser OAuth.
  - `ha`: local `http://127.0.0.1:8086/mcp`, PyPI `ha-mcp` in `vendor/ha-mcp/.venv`.
  - `monarch`: vendored `vendor/monarch-mcp-server`, keyring session. Neither vendored server is in git.
  - Telegram plugin: `telegram@claude-plugins-official`.
  - claude.ai connectors (Calendar, Drive, Home Assistant, Monarch): tied to the Claude account login.
- **Web services:** Hy-Vee and COROS (Playwright and web API), Google Drive OAuth, Telegram Bot API, Home Assistant.

### Secrets: where they live now
| Secret | Location (old machine) | Git |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | `%USERPROFILE%\.claude\channels\telegram\.env` | outside the repo |
| `HYVEE_USERNAME` / `HYVEE_PASSWORD`, `COROS_USERNAME` / `COROS_PASSWORD`, `HOMEASSISTANT_URL` / `HOMEASSISTANT_TOKEN` | repo `.env` | ignored |
| Hy-Vee and COROS session cookies | `state/hyvee_session.json`, `state/coros_session.json` | ignored, regenerable |
| Google Drive OAuth | `state/google/drive_client_secret.json`, `drive_token.json` (override env vars `GOOGLE_DRIVE_CLIENT_SECRET`, `GOOGLE_DRIVE_TOKEN`) | ignored |
| Monarch session | Windows Credential Manager | not on disk |
| Claude login | `~/.claude/.credentials.json` (shared by the whole profile) | outside the repo |
| Todoist OAuth | held by Claude Code | outside the repo |
| `alert_chat_id` | `scripts/scheduler.config.json` | **tracked**; a personal id, not a secret |

### Startup, scheduling, logging, recovery (today)
- **Start:** the `PersonalAssistantOrchestrator` task runs at logon and starts `start_orchestrator.ps1`, which loops `claude --debug-file ... --name ... --permission-mode auto --channels plugin:telegram@claude-plugins-official` with a 10 s restart. There is no `--resume`, so every start is a fresh session. The hook `telegram_context_bridge.py` replays the last 60 minutes of conversation.
- **Scheduling:** two layers.
  - OS layer: scheduler dispatch every 2 min, watchdog every 2 min, HA-MCP at logon.
  - App layer: cron rows in SQLite `scheduled_tasks`. `scheduler_dispatch.py` runs due rows as `claude --print` with a 300 s timeout. Cron uses naive local time, so `/etc/localtime` must be America/Chicago.
- **Jobs (from docs; confirm with `scheduler_store.py list` on the old machine):**
  - Lawn check, Sat 08:00.
  - Weather, daily 06:30.
  - Home maintenance, Sun 09:00.
  - Weekly spending, Sun 08:00.
  - Monthly spending, last Sat 09:00.
  - Retirement quarterly review, Jan/Apr/Jul/Oct.
  - Annual financial review, Nov.
  - Kids-memory monthly recap, first Sun 19:30.
  - Kids-memory weekly check-in, Sun 20:00.
  - Weekly fitness plan, Sun 18:00.
- **Logging:** `state/logs/` holds `orchestrator_debug.log`, `orchestrator_exits.log`, `watchdog_YYYY-MM-DD.log` and `scheduler_dispatch_debug/*.log`. Also `state/orchestrator_restarts.jsonl` and the `scheduled_task_runs` table. No rotation. The dispatcher's stdout is lost because it runs hidden. Timestamps are mixed: UTC with `Z` in some files, naive local time in others.
- **Recovery:**
  - The wrapper loop restarts `claude` after 10 s.
  - The Telegram watchdog force-kills the process on any of three signals, debounced 3 min: reply-tool errors in the debug log, a fallback-used marker from `telegram_send.py`, or 60 min of silence plus an SSE reconnect.
  - The scheduler watchdog checks that `last_tick` in `scheduler_loop_state.json` is under 20 min old, debounced 5 min. It re-runs the dispatcher, restarts the task, and alerts via Telegram only if the machine was not asleep.
  - HA-MCP has its own 10 s restart loop.
  - The scheduler marks a task dispatched before running it, so an interrupted run is not duplicated. Failed runs are not retried; they send an alert.

### GUI, browser, desktop needs
- **Playwright Chromium (hyvee, fitness):** no persistent browser profile, only `storage_state` JSON. Hy-Vee defaults to headless. The COROS scripts default to headed, so always pass `--headless` (the fitness agent already does).
- **Headed use:** needed for first logins and the COROS CAPTCHA. WSLg can handle this, or log in elsewhere and copy the session JSON.
- **One-time browser OAuth:** Todoist, Google Drive (`flow.run_local_server`), and Monarch (`login_setup.py` cookie paste).
- **`claude` is an interactive TUI:** the orchestrator needs a persistent pty. A bare systemd service has no TTY, so use tmux (see section 4).

## 1. Run-in-WSL vs run-native recommendation
**Recommendation: run in WSL2.** This matches your stated plan, and nothing in the repo requires Windows.
- The Python code is already portable. The Windows-only surface is about 10 `.ps1` files, 3 `.vbs` files and 3 small env-var spots.
- Playwright, SQLite and systemd behave better on ext4 than on NTFS. The repo is already on ext4, so keep it off `/mnt/c`.
- Windows PowerShell is currently needed only for scheduling and process control, which systemd covers.

Costs to accept:
- WSL2 is alive only while its VM runs. Section 4 covers keeping it running.
- WSL2 needs systemd enabled (`/etc/wsl.conf`: `[boot] systemd=true`).
- `localhost` forwarding matters if the dashboard or ha-mcp must be reached from Windows.
- Native Windows would mean keeping the whole PowerShell/VBS stack, with no benefit once the repo moves.

## 2. Ordered change list (one commit each)
Each item lists a smoke test (S) and a rollback (R). Global rollback: every change is its own commit on a `wsl-port` branch, so `git revert <sha>` undoes it. Nothing is deleted until item 19.

0. **Scrub personal data from git history (FIRST, before any other commit).** Rewrites SHAs, so it must precede the other changes. Targets: `docs/superpowers/specs/dashboard-backups/*`, the real `alert_chat_id` in `scripts/scheduler.config.json`, and anything the history grep finds (the chat id, the hardcoded Drive folder id in `retirement.md`, the old Windows username in paths if you want those gone). Steps: (a) make a full `git clone --mirror` backup; (b) grep all history for the patterns to confirm scope; (c) run `git filter-repo --invert-paths --path ... --replace-text ...` in a fresh clone; (d) review the result; (e) only after you approve, force-push. Needs approval because a force-push is outward-facing. If the remote is GitHub, old commits stay reachable through cached views and PR refs; cleaning that means GitHub support or deleting and recreating the repo. The old Windows machine (kept as the fallback) must `git fetch && git reset --hard origin/main`, or re-clone, afterwards. S: `git log --all -p | grep -c <pattern>` returns 0 and the test suite passes. R: restore from the mirror backup.
1. **Branch and guards.** Create `wsl-port`. Add `.gitattributes` (`* text=auto eol=lf`, `*.ps1 text eol=crlf`). S: `git ls-files --eol` unchanged for existing files. R: revert.
2. **Root `requirements.txt` and `requirements-dev.txt`.** Pin flask, playwright, numpy, xlsxwriter, google-*, pytest. Add `scripts/setup_wsl.sh`, which creates `.venv` and runs `playwright install --with-deps chromium`. S: `.venv/bin/python -c "import numpy, xlsxwriter, playwright, flask, googleapiclient"` and `pytest tests/` pass. R: delete `.venv`, revert.
3. **`scripts/paths.py`** (new, small). `claude_home()`, `telegram_env_path()`, `find_claude()`, `secrets_dir()`. S: unit test. R: revert.
4. **`scripts/telegram_send.py`.** Use `paths.telegram_env_path()` instead of `USERPROFILE`. Read `TELEGRAM_BOT_TOKEN` from `~/.config/scherbring-assistant/.env` first, then fall back to `~/.claude/channels/telegram/.env`. Fail loudly when no token is found. S: `python scripts/telegram_send.py "test"` arrives. R: revert.
5. **`scripts/scheduler_dispatch.py`.** Replace `_resolve_claude` and the token path (lines 68, 91) with `paths`. Remove `%APPDATA%` logic. Log to stdout/stderr for journald. S: a manual run ticks and updates `scheduler_loop_state.json`; a test task fires. R: revert.
6. **Shared `.env` loader** for `hyvee_session.py`, `coros_session.py`, the HA-MCP launcher and `drive_upload.py`. Load `~/.config/scherbring-assistant/.env` first, then repo `.env`. Warn if the file mode is looser than 600. S: `scripts/hyvee/login_test.py --headless` after secrets are placed. R: revert; the repo `.env` still works.
7. **`.claude/settings.json`.** Remove the roughly 30 `C:\Users\...` and `//c/Users/...` rules. Switch hooks and allow rules from `python` to `.venv/bin/python` (or `python3`). Add the missing allow rules for the `*_store.py` scripts. Fix `.claude/agents/todoist.md:21`. S: `claude` starts in the repo; a Telegram message makes the hook write `state/telegram_conversation_log.jsonl`. R: revert.
8. **`scripts/start_orchestrator.sh`.** Port the logic: `flock` single-instance guard, 10 s restart loop, `orchestrator_restart_log.py record`, exit log, same `claude` flags, `cd` to the repo root. S: run in tmux manually, kill `claude`, see it restart and a line in `orchestrator_exits.log`. R: keep the `.ps1` until item 19.
9. **`systemd/assistant-orchestrator.service`** (user unit). Runs the script inside a tmux session (`Type=forking`), or `script -qfc` as a pty alternative. `Restart=on-failure`, `RestartSec=10`, `EnvironmentFile=%h/.config/scherbring-assistant/.env`. Add `scripts/install_systemd.sh` to copy units into `~/.config/systemd/user/` and run `systemctl --user enable --now`. S: `systemctl --user status`, `tmux attach -t assistant`, a message to the bot gets a reply. R: `systemctl --user disable --now`, delete the unit.
10. **Scheduler timer.** `assistant-scheduler.service` (oneshot) and `.timer` (`OnBootSec=1min`, `OnUnitActiveSec=2min`, `Persistent=true`), `TimeoutStartSec=360`. S: `systemctl --user list-timers` shows it; `journalctl --user -u assistant-scheduler` has ticks. R: disable the timer.
11. **Telegram watchdog port** as `scripts/watchdog_telegram_health.py` (Python, because the logic is regex and JSON heavy). Same three signals and debounce, using `pgrep -f 'channels.*plugin:telegram'`. Restart via `systemctl --user restart assistant-orchestrator`. S: inject a fake "reply tool not found" line into a copy of the debug log and see the restart after the debounce window. R: disable the watchdog timer.
12. **Scheduler watchdog port** as `scripts/watchdog_scheduler_health.py`. Recovery is `systemctl --user restart assistant-scheduler.timer`. S: stop the timer and see it recover. R: disable.
13. **`assistant-watchdog.service` and `.timer`** (2 min) running items 11 and 12, plus pruning of logs older than 14 days. S: `list-timers`; run one pass manually. R: disable.
14. **HA-MCP (keep; decided in 8a).** Port `start_ha_mcp.ps1` to `assistant-ha-mcp.service` (`Restart=always`, `EnvironmentFile`). Install `ha-mcp` into `vendor/ha-mcp/.venv` at a pinned version in `setup_wsl.sh`. S: `curl -s localhost:8086/mcp` responds and the `home-assistant` agent lists entities. R: disable the unit.
15. **Monarch (two steps; decided in 8a).** (a) Test the official claude.ai Monarch connector against what the four finance agents need: transactions needing review, set tags, mark reviewed, notes, rules, holdings, cashflow, budgets, net worth, recurring. If it covers them, switch the agents' `tools:` lists from `mcp__monarch__*` to `mcp__claude_ai_Monarch_Money__*` (one commit per agent, with a tool-name map) and skip vendoring. This removes the keyring problem. (b) If it falls short, vendor `monarch-mcp-server` at a pinned commit, use the `keyrings.alt` file backend (mode 600), re-run `login_setup.py`, and register it in `.mcp.json`. S: the `finance` agent lists accounts and a tag round-trip works on one test transaction. R: revert the tool lists, or remove the MCP entry.
16. **Dashboard.** Add `assistant-dashboard.service`. Bind to `127.0.0.1` (or the Tailscale IP) instead of `0.0.0.0`. Update the README venv path. S: `curl localhost:5151/api/status`. R: disable.
17. **Personal data out of tracked config.** Move `alert_chat_id` out of `scripts/scheduler.config.json` into the `.env` (`TELEGRAM_ALERT_CHAT_ID`), and keep an `.example`. S: an alert is delivered. R: revert.
18. **Docs.** Rewrite README, GETTING_STARTED and `CLAUDE.md` references (PowerShell commands, `\` paths, the "Windows" prerequisite, the missing "Scheduler self-arming" section). Add `docs/OPERATIONS.md` (start, stop, logs, backup, restore). S: a fresh-shell walkthrough. R: revert.
19. **Cleanup (last, after one clean week).** Delete the `.ps1` and `.vbs` files and the register scripts. S: the test suite and a timer pass. R: `git revert`.

## 3. Secrets
- **Layout:** `~/.config/scherbring-assistant/.env`. `chmod 700` on the directory, `chmod 600` on the file. It is outside the repo, so nothing can be committed by accident. The repo `.gitignore` already covers `.env`, `state/` and `.mcp.json`. Keep `.env.example` current.
- **Keys:** `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALERT_CHAT_ID`, `HYVEE_USERNAME`, `HYVEE_PASSWORD`, `COROS_USERNAME`, `COROS_PASSWORD`, `HOMEASSISTANT_URL`, `HOMEASSISTANT_TOKEN`, optional `GOOGLE_DRIVE_CLIENT_SECRET` and `GOOGLE_DRIVE_TOKEN` paths.
- **Loaded by:** systemd `EnvironmentFile=`, plus the shared loader from item 6 for manual runs.
- **Google OAuth files** go in `~/.config/scherbring-assistant/google/`, mode 600.
- **What you must supply by hand:**
  1. `claude auth login` on WSL with your personal account.
  2. The Telegram bot token (reused), then `/telegram:configure <token>` and re-pair access.
  3. The Hy-Vee, COROS and HA credentials.
  4. Todoist OAuth in a browser (first use).
  5. Google Drive OAuth: copy `drive_client_secret.json` and the token from the old machine, or re-create and consent once.
  6. Monarch: nothing if the official connector works (item 15); otherwise `login_setup.py`.
  7. The SQLite DB and other state (section 10).
- Never paste secrets into chat. The audit printed no values.
- **Old machine:** when you decide to wipe it, first rotate the Telegram token and revoke old OAuth tokens.

## 4. Process management
- **Units (all `systemctl --user`):** `assistant-orchestrator.service` (tmux-wrapped), `assistant-scheduler.timer` and `.service`, `assistant-watchdog.timer` and `.service`, `assistant-ha-mcp.service`, `assistant-dashboard.service` (optional).
- **Restart on failure:** `Restart=on-failure`, `RestartSec=10`, `StartLimitIntervalSec=0` for the orchestrator, plus the in-script 10 s loop. The watchdog covers the stuck-but-alive case.
- **Return after a Windows reboot:**
  1. `sudo loginctl enable-linger $USER` so user units run without a login session.
  2. `/etc/wsl.conf` with `[boot] systemd=true`.
  3. `%UserProfile%\.wslconfig` with `[wsl2]` and `vmIdleTimeout=-1`, so the VM doesn't stop when idle.
  4. One Windows Task Scheduler task (trigger: at startup, run whether or not logged on, as you) running `wsl.exe -d Ubuntu-24.04 -u jscherb1 -- sleep infinity`. This is the only Windows-side piece; `docs/OPERATIONS.md` will have a PowerShell snippet to register and remove it.
  5. Verify the whole chain: reboot Windows, then check `systemctl --user list-timers` and a Telegram round trip, without opening a terminal.
- **Sleep and hibernate:** WSL pauses with Windows. The scheduler's catch-up logic (`last_dispatched_at` base) handles missed runs, and the scheduler watchdog already treats a stale tick after sleep as expected. You deferred the sleep decision; if the PC must be reachable, disable sleep on AC power.
- **Logging:** units log to journald (`journalctl --user -u assistant-*`). The existing `state/logs/` files stay. Prune after 14 days.

## 5. Permissions model
Today: `--permission-mode auto` with an allow list in `.claude/settings.json`, plus the Telegram rules in `CLAUDE.md`. Decision: keep auto, add the deny rules below, and no Hy-Vee ordering.

**May do on its own:**
- Read and write under `state/**` and the per-agent SQLite stores via the `*_store.py` scripts.
- Read-only queries against Monarch, Home Assistant, Todoist, Calendar and Drive.
- Create Todoist tasks and Calendar events in the dedicated calendars (Meal Planning, Running).
- Write memory and kid-memory files and their Drive mirrors.
- Send Telegram messages to your chat only.
- Run the scheduler's own tasks.
- Build the Hy-Vee cart (never check out).
- Basic Home Assistant control (lights, climate, media, timers).
- Tag and mark-reviewed Monarch transactions, as the `finance` agent does today.

**Requires your approval (ask on Telegram):**
- Anything outward-facing or irreversible: sending email, sharing or deleting Drive files, deleting calendar events.
- Purchases and checkout (not allowed at all for Hy-Vee).
- New Monarch auto-tagging rules (proposed, not applied).
- Home Assistant locks, garage doors, alarms, and creating or editing automations.
- Changing the schedule table for tasks you did not just ask for.
- Editing `.claude/settings.json`, systemd units, `.env` or any credential.
- Installing packages, `sudo`, `loginctl`.
- Using a model above Sonnet (per `CLAUDE.md`).
- Network calls to services outside the known list.

**Hardening to add in this port:**
- Deny rules for reading `~/.config/scherbring-assistant/**`, `~/.ssh/**`, `~/.claude/.credentials.json` and `.env`, so a prompt injection cannot read secrets.
- Run as your normal user, never root.
- Turn off WSL interop and Windows mounts (`/etc/wsl.conf`) once the port is stable, so the agent can't reach Windows files or `powershell.exe`. This will also stop `/mnt/c` backups, so do it after choosing a backup target.
- Bind the dashboard and ha-mcp to localhost.

## 6. State, logs and backup
| What | Path | Notes |
|---|---|---|
| Main DB (all agent memory) | `state/agent_results.db` | SQLite; ext4 only. Back up with `sqlite3 .backup`. |
| Scheduler and watchdog JSON | `state/*.json`, `*.jsonl` | disposable, regenerable (except the restart log) |
| Hy-Vee and COROS sessions | `state/*_session.json` | disposable; re-login |
| Reports and workbooks | `state/retirement/`, `state/finance_reports/` | regenerable; already mirrored to Drive |
| Google tokens | `~/.config/scherbring-assistant/google/` | secret; back up encrypted |
| Logs | `state/logs/` | prune after 14 days |
| Claude per-project memory | `~/.claude/projects/-home-jscherb1-projects-scherbring-family-assistant/memory/` | **not in the repo**; includes notes the scripts cite |
| Claude login, plugins, Telegram access | `~/.claude/` | re-created by login |
| Secrets | `~/.config/scherbring-assistant/` | back up separately, encrypted |

**Backup:**
- Add `scripts/backup.sh` and a daily user timer. Steps: `sqlite3 state/agent_results.db ".backup ..."`, tar the DB, the Claude memory directory and local config, and `age`/`gpg`-encrypt the secrets directory separately.
- Target: a folder on the Windows side (`/mnt/c/Users/<you>/Backups/assistant`), which File History or OneDrive can then cover. Keep 14 dailies and 8 weeklies.
- Restore drill in `docs/OPERATIONS.md`: copy the DB into `state/`, then run `scheduler_store.py list`.
- Occasional `wsl --export` of the whole distro to a Windows drive.
- Never run two orchestrators at once: two pollers on one bot token fight over Telegram updates.

## 7. Cutover order and rollback
1. Do item 0 (history scrub), then items 1 to 7, and run the test suite.
2. Stop the old machine's assistant and export its state (section 10).
3. Place secrets and state. Start the orchestrator manually (item 8) and test on Telegram.
4. Enable units (items 9 to 13). Reboot Windows and verify.
5. Enable the other units (items 14 to 16).
6. Run for a week and compare scheduled-job output to the previous week. Then do item 19.

**Rollback:** disable the WSL units (`systemctl --user disable --now 'assistant-*'`), re-enable the four Windows tasks on the old machine, and copy the DB back if needed. Only one side runs at a time.

## 8. Open questions
The original 15 questions are answered in 8a. What remains is in 8b.

## 8a. Decisions so far
1. Old machine stays as a fallback; you decide when to wipe it and rotate secrets.
2. Claude account: your personal account.
3. Telegram bot token: reuse. The old poller must be stopped first (section 10).
4. You will export the DB and state (section 10).
5. Sleep behavior: decide later. One Windows startup task to keep WSL alive is allowed.
8. HA and Monarch access must be kept; the means are my call:
   - **Home Assistant: keep `ha-mcp`.** The hosted HA connector only exposes basic intents (on/off, light, climate, media, timers). The `home-assistant` agent also edits automations, scripts, scenes, helpers and dashboards and reads history, traces and logs, and only `ha-mcp` does that. The hosted connector stays as a fallback for basic control. HA must be reachable from WSL at `HOMEASSISTANT_URL` (outbound LAN works with WSL2 NAT; verify).
   - **Monarch: try the official connector first, vendored server as the fallback** (item 15). The official connector appears to cover transactions, tags, rules, holdings, cashflow, budgets, net worth and recurring, but that is unverified until tested. It removes the vendored code and the keyring backend. Cost: a tool-name change in four agent files.
11. Scrub personal data from git history. The repo is private but should be safe to make public later (item 0).
13. COROS login: section 11.
14. No ordering. Keep `--permission-mode auto` with the deny rules in section 5.

Defaults accepted for the rest: tmux orchestrator, dashboard on localhost, America/Chicago, backups to a Windows folder.

## 8b. Remaining open questions
1. Is the git remote GitHub, and are there forks or other clones? (Decides how the history scrub is finished.)
2. Any other personal data besides the dashboard backups and the chat id that should go? The history grep (item 0) will list candidates, and I'll show them before rewriting anything.
3. Do you have WSLg (Windows 11)? Run `echo $DISPLAY $WAYLAND_DISPLAY` in WSL; any value means yes.
4. Sleep behavior (deferred): the plan covers it with `vmIdleTimeout=-1` and the keep-alive task.

## 9. Portability rules (moving to another machine later)
After the port, a move is: `git clone`, run `scripts/setup_wsl.sh`, restore the backup, copy `~/.config/scherbring-assistant/`, and redo the logins (`claude auth login`, Telegram pairing, Todoist, Drive, COROS session). About 15 to 30 minutes, with no code changes. Rules that keep it that way:
1. All paths go through `scripts/paths.py`. No hardcoded home directories.
2. All state stays in `state/`, `~/.config/scherbring-assistant/` and the Claude memory directory.
3. Pin anything vendored (`ha-mcp`, and `monarch-mcp-server` if used) by version or commit in `setup_wsl.sh`.
4. Test the restore drill once (`docs/OPERATIONS.md`) before relying on it.
5. The Windows keep-alive task is the only non-portable piece. On plain Ubuntu or a VPS it isn't needed. macOS would need a launchd port.

## 10. Step-by-step export from the old Windows machine
Do this when you are ready to cut over. The Telegram token can only be polled from one place, so the old orchestrator stops before the new one starts.
1. **Stop the old assistant.** In PowerShell on the old machine:
   - `Disable-ScheduledTask PersonalAssistantOrchestrator, PersonalAssistantScheduler, PersonalAssistantWatchdog, PersonalAssistantHaMcp`
   - Close the orchestrator window, or run `Get-CimInstance Win32_Process | ? CommandLine -like '*channels*plugin:telegram*' | % { Stop-Process -Id $_.ProcessId -Force }`.
   - Confirm nothing is running: `Get-Process claude -ErrorAction SilentlyContinue`.
2. **Record the schedule.** From the repo folder: `python scripts\scheduler_store.py list > state\scheduler_list.txt`.
3. **Take a consistent DB copy** (not a raw file copy): `python -c "import sqlite3; s=sqlite3.connect('state/agent_results.db'); d=sqlite3.connect('state/export.db'); s.backup(d); d.close()"`.
4. **Record vendored versions:** `git -C vendor\monarch-mcp-server rev-parse HEAD` and `vendor\ha-mcp\.venv\Scripts\python.exe -m pip freeze > state\ha_mcp_freeze.txt`.
5. **Collect these into one folder** (`C:\export\`):
   - `state\export.db`, `state\scheduler_list.txt`, `state\ha_mcp_freeze.txt`.
   - `state\hyvee_session.json`, `state\coros_session.json`, `state\google\` (all).
   - the repo `.env`, `.mcp.json`, `.claude\settings.local.json`.
   - `%USERPROFILE%\.claude\channels\telegram\` (`.env` and `access.json`).
   - the Claude memory folder: `%USERPROFILE%\.claude\projects\<folder-for-the-old-repo-path>\memory\`.
   - a text file with the vendored commit from step 4.
6. **Zip it** (`Compress-Archive C:\export\* C:\export.zip`). The zip holds secrets, so move it by USB stick or `tailscale file cp`. Do not use a synced cloud folder.
7. **On the WSL box:**
   - Unzip into `~/migration/`, then `chmod -R go-rwx ~/migration`.
   - Install `sqlite3`, then check the DB: `sqlite3 ~/migration/export.db "pragma integrity_check;"`. Expect `ok`.
   - Put files in place: DB to `state/agent_results.db`; the `.env` contents into `~/.config/scherbring-assistant/.env` (chmod 600); session JSON and `google/` into `state/` or the config directory; Telegram `.env` and `access.json` into `~/.claude/channels/telegram/` (chmod 600).
   - **Memory folder:** the project path differs, so the destination is `~/.claude/projects/-home-jscherb1-projects-scherbring-family-assistant/memory/`. A memory folder already exists there, so merge: copy only files that don't exist, and ask before resolving name conflicts.
   - Compare `scheduler_list.txt` with `python scripts/scheduler_store.py list` on the new box.
   - Delete `~/migration/` and the zip when done (`shred -u` for the secret files).
8. If anything fails, re-enable the four Windows tasks (`Enable-ScheduledTask ...`). Nothing on the old machine was deleted.

## 11. COROS login
You don't need to give me anything. Put `COROS_USERNAME` and `COROS_PASSWORD` in `~/.config/scherbring-assistant/.env` yourself, never in chat. Two ways to get a session:
- **Easiest (recommended): copy `state/coros_session.json` from the old machine** (it is in the export list). The scripts use that file with no browser profile, so it should work as is. If COROS rejects it, use the second way.
- **Fresh login:** if WSLg is available, run `.venv/bin/python scripts/fitness/login_test.py` without `--headless`. A Chromium window opens on your desktop. Solve the CAPTCHA if shown, and the script saves `state/coros_session.json`. Check with `.venv/bin/python scripts/fitness/library_sync.py --headless`. If no window appears, log in on the Windows machine and copy the file instead.

Hy-Vee works the same way: copy `state/hyvee_session.json`, or log in once.
