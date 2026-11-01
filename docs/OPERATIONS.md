# Operations runbook

Day-to-day care of the assistant on its WSL2 Ubuntu 24.04 host. For first-time setup see
`GETTING_STARTED.md` and `README.md`. Open follow-ups are in `docs/TODO.md`.

Everything runs as **systemd user units** (`systemctl --user ...`). The repo is
`~/projects/scherbring-family-assistant`; secrets are in `~/.config/scherbring-assistant/`.

## What runs

| Unit | Job |
|---|---|
| `assistant-bridge.service` | Telegram bridge (**live, production bot**): polls Telegram into a durable inbox, delivers replies from the outbox. See "Telegram bridge" below |
| `assistant-agent-worker.service` | agent worker (**live**): runs Claude turns for inbox messages via the Agent SDK, resumes its session on restart; systemd watchdog 30 s |
| `assistant-scheduler.timer` | every 2 min: run due scheduled tasks (`scripts/scheduler_dispatch.py`) |
| `assistant-watchdog.timer` | every 2 min: scheduler-health check, log pruning |
| `assistant-ha-mcp.service` | local Home Assistant MCP server, 127.0.0.1:8086 |
| `assistant-system-health.timer` | every 2 h: integration health checks, shown on the dashboard's System status page (`python scripts/system_health_store.py status`) |
| `assistant-insights-refresh.timer` | every 30 min, 6am-10pm: refreshes COROS sleep/recovery, weather, habits and journal answers for My Hub, day rating and Wellness (`python scripts/dayrating_store.py insights refresh --days 3 --source coros_health,weather,habits,journal`) |
| `assistant-hub-refresh.timer` | every 15 min, 6am-10pm: copies Home Assistant calendar events and dated Todoist tasks into My Hub and, once cut over, sends the hub notify-check message (`python scripts/hub_refresh.py --notify`; preview with `--dry-run`). Needs `TODOIST_API_TOKEN` in the secrets `.env`. Notifications stay muted while the `myhub-refresh` scheduled task is enabled |
| `assistant-money-sync.timer` | every 30 min, 6am-11pm: copies Monarch budget, transactions, balances, net worth and bills (plus investment holdings on the daily full sync) into the `money_*` tables for the Money page and the `finance` agent (`vendor/monarch-mcp-server/.venv/bin/python scripts/money_sync.py --mode auto`). Status: `python scripts/money_store.py sync-status` |
| `assistant-credential-check.timer` | daily 09:30: warn before Hy-Vee, Drive, Monarch logins expire |
| `assistant-backup.timer` | daily 03:15: back up the database, memory notes and secrets |
| `assistant-dashboard.service` | optional web dashboard (`dashboard/README.md`): five tabs (Today, Me, Family, Home, Money) plus System behind the gear; old pre-redesign pages under `/archive` until removed (`docs/TODO.md`). Writes go through the shared services (habits, chores, check-in, recipes, dismissals) and the Today page's one-shot "ask" box queues a message for the agent, answered in Telegram; the iOS Shortcut posts to `POST /api/ask` (`text` field, tagged `via="ios"`); `DASHBOARD_ASK_CHAT_ID` overrides the chat, default is the first `BRIDGE_ALLOWED_CHAT_IDS`. Binds `DASHBOARD_HOST` (127.0.0.1 unless a drop-in overrides it). Restart after dashboard changes: `systemctl --user restart assistant-dashboard` |

## Is it healthy?

```
systemctl --user list-timers 'assistant-*'          # every timer should have a NEXT time
scripts/agent_ctl.py status                         # bridge and worker up, queue, current turn
python scripts/credential_check.py --status         # login ages and probes, sends nothing
python scripts/scheduler_store.py list              # the scheduled tasks
```

Quick end-to-end check: send the bot a message. If there is no reply within a minute, run
`scripts/agent_ctl.py status` and look at `journalctl --user -u assistant-bridge -u assistant-agent-worker -n 100`.

## Logs

| Where | What |
|---|---|
| `journalctl --user -u assistant-scheduler -n 50` | dispatcher ticks (also `-u assistant-watchdog`, `-u assistant-backup`, ...) |
| `journalctl --user -u assistant-bridge -u assistant-agent-worker` | bridge and worker logs |
| `state/logs/agent_stream.jsonl` | the worker's live event stream (`agent_ctl.py tail`) |
| `state/logs/watchdog_YYYY-MM-DD.log` | each watchdog pass |
| `state/logs/scheduler_dispatch_debug/*.log` | one debug log per scheduled run |
| `scheduled_task_runs` table | per-run status and summary (`scheduler_store.py`) |

Logs older than 14 days are pruned by the watchdog.

An `ok` status only means the run exited cleanly and any message it had was delivered; a reply that says
it could not finish is still `ok` when no command was denied, so read the Telegram message. The dispatcher
reads the run as JSON, so it knows the exact commands that were denied. A run with denials is **degraded**
(no alert; the summary and the System status page name the denied commands) only if a marked message was
delivered and it does not report the task itself failing ("sync failed", "did not complete", ...). A run is
**failed** (and alerts) if it exits non-zero, the JSON result is an error, delivery fails, it was silent
despite denials (a blocked check may have hidden something), or its delivered message reports a failure
while commands were denied. Fix degraded runs by correcting the prompt so each Bash call is one plain
documented command, not by widening the allow list.

## Start, stop, restart

```
systemctl --user restart assistant-agent-worker     # picks up agent/prompt/settings changes; the session is resumed
systemctl --user restart assistant-bridge           # picks up telegram_bridge.py changes; queued messages are kept
systemctl --user stop assistant-agent-worker        # stop; it will NOT come back until started
systemctl --user start assistant-agent-worker
systemctl --user restart assistant-scheduler.timer
```

After editing a unit in `systemd/`, run `scripts/install_systemd.sh` (it re-templates the
paths and reloads), then restart the unit. Never run a second bridge by hand while the unit is
up: two pollers on one bot token fight over Telegram updates.

## Telegram bridge

The bridge splits Telegram handling in
two: `scripts/telegram_bridge.py` owns Telegram (saves every message to `state/inbox.db` *before*
acknowledging it, delivers replies from an outbox with retry) and `scripts/agent_worker.py` runs the
Claude turns and can be killed or restarted at any moment without losing a message or the
conversation (the Claude session is resumed by id; if that fails it is rebuilt from the message log).

The production bot is served by the bridge (cut over 2026-10-06). The test bot
(`BRIDGE_TOKEN_ENV=TELEGRAM_TEST_BOT_TOKEN` in `.env`) is still there for experiments.

**Never enable the Telegram Claude plugin** (`telegram@claude-plugins-official`). It is a poller: any
Claude session that loads it, even a pre-warmed spare from the Claude daemon, polls the same bot token and
blocks the bridge with `getUpdates: Conflict`. If polling is blocked for more than a minute the bridge messages
the user ("I'm not receiving your messages") and sends a recovery message afterwards; find the culprit with
`ps -eo pid,ppid,etime,cmd | grep "bun run.*telegram"`.

```
scripts/agent_ctl.py status            # up/down, queue, current turn, MCP problems (exit 1 if unhealthy)
scripts/agent_ctl.py tail              # live, full, read-only stream of what the agent is doing/thinking
scripts/agent_ctl.py say "hello"       # talk to it from the terminal (replies print here)
scripts/agent_ctl.py pause | resume    # worker stops/starts taking messages (finishes the current turn)
scripts/agent_ctl.py sessions          # session ids; take over by hand: pause, `claude --resume <id>`, resume
scripts/agent_ctl.py drill worker-kill|bridge-kill|hang|worker-stop|worker-start
```

Telegram commands answered by the bridge itself (work even if the agent is down): `/status`.
If a message waits >10 s with the worker down, the user gets "agent is restarting, your message is
saved". Dashboard: **Agent** section (`/agent`, `/agent/usage`).

Health checks: bridge and worker heartbeats (`last_poll_ok` separates "bridge alive" from "Telegram
reachable"); the worker pings its agent process every 30 s (no tokens) and makes a real minimal model
call every 30 min; failures alert via Telegram once and send a recovery notice. systemd restarts a
killed bridge in ~1 s, a killed worker in ~2 s, and a wedged worker within 30 s.

Context hygiene (worker): every message sent outside a turn (scheduled-task results, credential,
system-health, task-failure, scheduler-watchdog and backup alerts, `telegram_send.py`) goes through
`scripts/notify.py`, which sends via the Bot API and records it in `inbox.db`: a sent `outbox` row
(`source`, `kind`), its Telegram message ids (`sent_ids`), and a context note shown to the agent on
the user's next message. When the user replies to a message in Telegram, the bridge keeps
`reply_to_message` and the worker looks it up in `sent_ids`, so the turn carries a
`<replying_to source=...>` block with the full original text. Sessions rotate above 150k context tokens or after 7 days (rebuilt from
the last 48 h / 40 messages); a quiet-hours (03:xx) `/compact` runs if context > 40k tokens. Set
`BRIDGE_DAILY_COST_ALERT_USD` in `.env` for a once-a-day usage alert. Per-turn usage is in
`state/usage.jsonl`.

Config (in `~/.config/scherbring-assistant/.env`): `TELEGRAM_TEST_BOT_TOKEN`,
`BRIDGE_ALLOWED_CHAT_IDS` (comma-separated; empty = pairing mode, nobody is served and the bot
replies with the sender's chat id), optional `BRIDGE_SESSION_MAP` (`{"chat_id":"shared-key"}` to
put several chats in one session; default is one session per chat; do not map a dev chat into a household
session), `BRIDGE_DEV_CHAT_IDS` (comma-separated dev chats, see above), `TELEGRAM_ALERT_CHAT_ID` and
`DASHBOARD_ASK_CHAT_ID` (household chat id; required in practice once a dev chat exists).

**Concurrent sessions and the dev chat.** The worker runs one loop per session (by default one session per
chat), so a long turn in one chat (a 500 s Hy-Vee cart, a dev task) never blocks another; messages inside a
session are still answered in order. At most 4 sessions run at once (`MAX_CONCURRENT_SESSIONS`); loops exist only
while a session has queued work. `paused`, the health canaries, cost tracking and crash recovery are global;
`current_turn` is per session (`current_turn:<session_key>` in the `kv` table; `agent_ctl.py status` and the
dashboard list every running turn); the global MCP-down list is the union over sessions. The quiet-hours compaction
only compacts a session that is idle, and the 30 s ping skips a session that is mid-turn. On SIGTERM the worker waits
10 s for running turns, then cancels them (3 s) and closes clients (4 s), inside systemd's 20 s stop timeout, and puts
cancelled messages back in the queue (redelivered as a retry). A turn that fails before it starts (for example a locked
database) is requeued after a 1 s pause, bounded by the 3-attempt limit.

**Fast lane.** The worker also runs a fast-lane task (`scripts/fastlane.py`, `scripts/fastlane_model.py`) that answers
short, clear commands (lights, thermostat, closing a garage door, a habit, a day rating, a chore, adding to a Todoist
list, "what's for dinner") without a full agent turn. It is **off by default**: `python scripts/agent_ctl.py fastlane on`
turns it on within a second, `off` stops it (no restart; the switch is `kv` key `fastlane`), `status` shows it with the last
24 h of counts. While on, the task stamps `fastlane_beat` every second and `claim_next` holds a new message for it for at most
10 s; if the stamp is older than 5 s, the fast lane is off, or a message is older than 10 s, the agent takes it as usual. The
Tier 1 model call keeps one pre-started, primed Haiku client and uses it for exactly one message (so no message sees
another's text); the replacement is built in the background, and while none is ready `route()` returns at once and the
message goes to the agent. Disconnects are bounded (3 s), so a hung one cannot hold up a restart. It is only started while
the lane is on, and closed when it is switched off. A failure or timeout pauses Tier 1 for 30 s. Each fast message logs `fastlane: {tier, action, latency_ms}` to `state/usage.jsonl`, an answer
leaves one context note ("You asked: ... I replied: ...") and a `source="fastlane"` outbox reply, and `inbox.triage`
records the decision (`fast`, or `pass:<reason>` such as `pass:question`, `pass:no_match`, `pass:order_conflict`).
Debug a message with `python scripts/fastlane_store.py test "<text>"` (dry run, never writes; Tier 0 only, `--model` adds the live call). The worker needs a restart
to pick up the code (`systemctl --user restart assistant-agent-worker`), then switch it on.

The Telegram group on the same bot is the dev chat. Put its id in `BRIDGE_ALLOWED_CHAT_IDS` (the allow list is
per chat; nobody but the owner is in the group) and in `BRIDGE_DEV_CHAT_IDS`. Its session gets a dev note in its
system prompt: use `git worktree add` for code changes, never switch branches in the main checkout (scheduled runs
share it), follow the CLAUDE.md development process. The household session does not get it, and context notes
(scheduled results, alerts) follow the session key, so household notes never appear in dev. Because alerts, the
dashboard ask box and `agent_ctl.py say` default to "the first allowed chat", set `TELEGRAM_ALERT_CHAT_ID` and
`DASHBOARD_ASK_CHAT_ID` explicitly to the household chat; `agent_ctl.py status` warns if a dev chat is configured and
they are not. The fallback (first allowed chat) skips dev chats, and `say` only reaches a dev chat with `--as`.
Dev and household sessions share the Claude memory directory and fixed-name temp files such as `state/hyvee_tmp`
and `state/myhub_tmp`, so don't run the same flow (a Hy-Vee build, a hub refresh) from both at once. A dev worktree has
no `state/`, so scripts run there don't see live data; the dev prompt says so. Outage alerts from the bridge go to
`TELEGRAM_ALERT_CHAT_ID`, else the first non-dev allowed chat, never the group. `/status@botname` works in groups. If Telegram upgrades the basic group to a supergroup its chat id changes: the
bridge logs `chat <old> was upgraded to a supergroup with NEW chat id <new>` and answers nothing from the new id
(it replies "Not authorized. Your chat id is ..."); replace the old id in `BRIDGE_ALLOWED_CHAT_IDS` and
`BRIDGE_DEV_CHAT_IDS` and restart the bridge and worker. Config is never rewritten automatically.

Hard rules learned the hard way: the worker disables Claude background tasks (their completion has
nowhere to be delivered) and ignores stale result events; never close a message without a reply.

## Keeping WSL alive (Windows side)

WSL2 only runs while its VM is up. Three pieces keep it up:

1. `/etc/wsl.conf` has `[boot] systemd=true`.
2. `C:\Users\<you>\.wslconfig` has `vmIdleTimeout=-1` under `[wsl2]` (apply with `wsl --shutdown`).
3. A Windows startup task, `WSL-Keepalive-Assistant`, runs
   `wsl.exe -d Ubuntu-24.04 -u jscherb1 -- sleep infinity`.

Plus `sudo loginctl enable-linger $USER` so user units run with no login session.

Windows sleep is not the problem on this machine: standby is set to never, and the clock-jump
warnings in the debug log are about a second long, not sleep-sized. If replies are ever
delayed, note when you sent and when it answered, then check `journalctl --user -u assistant-bridge -u assistant-agent-worker` around those times.

## Logins that expire

Two systems need a manual login now and then. `assistant-credential-check.timer` probes each
daily with a **real request** (Drive: a read-only file list; Monarch: an account listing). Only a probe that
comes back `dead` sends a Telegram message ("EXPIRED", naming the desktop shortcut); `ok` and `unknown` (network
trouble) stay silent. The age timer is only a secondary note and `--status` column; there is no advance warning.
COROS is not on the list: it logs in again by itself.

**Hy-Vee is separate and has exactly one path.** The only way to sign in is the **Hy-Vee Login** desktop shortcut
(`scripts/hyvee/Hy-Vee Login.bat`, a copy lives on the Windows desktop): remote in, double-click, solve the CAPTCHA.
It runs `login_test.py --login-only --manual-wait 300`, saves the session and re-checks the connection. The only check is
`scripts/hyvee/hyvee_connection.py` (order-history API: `ok` / `expired` on a clear 401-403 confirmed by a re-test /
`unknown` for Cloudflare, 429, 5xx, timeouts), run every 2 hours by `assistant-system-health.timer` through
`system_health`. `unknown` never changes the status or sends a message; `expired` sends one Telegram message that says why
and how to fix it, and one when it recovers. A passing check re-saves the session file, which keeps rotated cookies alive.
Every check is logged to `state/hyvee_checks.jsonl` (status, HTTP status, reason, login age) so the real session lifetime
is visible. `python scripts/hyvee/hyvee_connection.py status|check` shows or runs it by hand. No advance-expiry warning.

**Desktop shortcuts** (copies live on the Windows desktop, sources in `scripts/`): `Monarch Login.bat` and
`Google Drive Login.bat` run `scripts/connection_refresh.py`, which does a real check first ("already working" if it
passes), logs in only if it is down, then re-checks and shows the result. Monarch still needs the cookie header pasted;
Drive still needs the approval click.

| System | Lasts about | Re-login |
|---|---|---|
| Google Drive | 7 days while the OAuth app is in *Testing* | `.venv/bin/python scripts/drive_upload.py auth` (open the printed URL, approve). See `docs/retirement-drive-setup.md` for publishing the app so this stops being weekly. |
| Monarch | about 2 weeks | `cd vendor/monarch-mcp-server && .venv/bin/python login_setup.py`, choose option 1, paste the cookie header from app.monarch.com |

Things that look like bugs but are not:
- The Monarch tool `check_auth_status` says "not authenticated" even when the login works. Judge by
  a real read such as `get_accounts`.
- COROS allows one web session per account. A fresh automated login signs you out of COROS elsewhere.
- Monarch's official connector (`https://api.monarch.com/mcp`, claude.ai connector "Monarch Money", read +
  write, authorized 2026-10-21) is only a **fallback for the `finance` agent** when the vendored login has
  expired. It can't see Finicity-connected accounts (all Fidelity and Alliance Bernstein), and the Core plan
  caps weekly usage. So reports, retirement, the advisor and the money snapshot stay on the vendored server.
  Headless scheduled runs do load it. If its authorization lapses, the bridge health shows it under MCP down;
  re-authorize in claude.ai → Settings → Connectors. Revisit a full switch when Monarch adds Finicity data.

## Scheduled tasks

```
python scripts/scheduler_store.py list
python scripts/scheduler_run_now.py --list
python scripts/scheduler_run_now.py --name <task> [--ignore-gates]   # run one now, for testing
```

`scheduler_run_now.py` goes through the real dispatch path and posts a `[TEST]` message to
Telegram, but does not advance the task's schedule. Tasks that write (finance reports, the
retirement workbook) create real Drive files and database rows, so clean those up after a test.
Cron times are local time, so `timedatectl` must show America/Chicago.

## Backups

`assistant-backup.timer` runs `scripts/backup.py` daily at 03:15 into
`/mnt/c/Users/<you>/Backups/assistant`, a Windows-side folder so File History or OneDrive can cover it.
The folder comes from `ASSISTANT_BACKUP_DIR`, which is set in a local systemd drop-in that is not in the
repo (`~/.config/systemd/user/assistant-backup.service.d/override.conf`, with
`[Service]` and `Environment=ASSISTANT_BACKUP_DIR=...`). Without it the script uses `~/backups/assistant`.
On a new machine, recreate the drop-in, then `systemctl --user daemon-reload`. Each run writes two files:

- `data-YYYYMMDD-HHMMSS.tar.gz`: a consistent SQLite snapshot of `state/agent_results.db`, the Claude
  memory notes, `state/credential_check.json`, `state/schema.sql`, `scripts/scheduler.config.json`. **Not encrypted.**
- `secrets-YYYYMMDD-HHMMSS.tar.age`: credentials and sessions, encrypted with [age](https://age-encryption.org).

Retention: the newest 14 days, plus the newest backup of each of the 8 weeks before that.
A failed backup sends a Telegram alert.

### The encryption key

Encryption uses a public key kept at `~/.config/scherbring-assistant/backup_age_recipient.txt`. The
**private key is needed only to restore** and is deliberately not kept in the config directory.
`python scripts/backup.py init-key` writes it to `~/backup-age-key.txt`. Copy it into your password
manager, then delete the file from WSL. **Without the private key the encrypted secrets cannot be
recovered.** If it is lost, make a new key (delete the recipient file, run `init-key` again) and the
next backup uses it; older secrets archives stay unreadable.

### Check a backup (restore drill)

```
ASSISTANT_BACKUP_DIR=/mnt/c/Users/<you>/Backups/assistant python scripts/backup.py verify
```

It unpacks the newest data archive into a temp folder, runs SQLite `integrity_check`, and counts tables,
scheduled tasks and memory notes. Do this after any change to the backup and every few months.

### Restore

1. Stop the assistant: `systemctl --user stop assistant-bridge assistant-agent-worker assistant-scheduler.timer`
2. Unpack the data archive somewhere temporary: `mkdir /tmp/restore && tar -xzf data-STAMP.tar.gz -C /tmp/restore`
3. Put things back:
   - `/tmp/restore/state/agent_results.db` to `state/agent_results.db`
   - `/tmp/restore/claude-memory/` to `~/.claude/projects/-home-<user>-projects-scherbring-family-assistant/memory/`
   - `/tmp/restore/state/credential_check.json` and `schema.sql` to `state/`
   - `/tmp/restore/scripts/scheduler.config.json` to `scripts/` (only if present)
4. Secrets: `age -d -i ~/backup-age-key.txt secrets-STAMP.tar.age | tar -x -C /tmp/restore-secrets` (private key first;
   keep `/tmp/restore-secrets` private and delete it afterwards). Folders inside map back like this, then set
   modes (`chmod 700` directories, `chmod 600` files):

   | In the archive | Goes to |
   |---|---|
   | `config/scherbring-assistant/` | `~/.config/scherbring-assistant/` |
   | `monarch-mcp-server/` | `~/.monarch-mcp-server/` |
   | `claude-channels-telegram/` | `~/.claude/channels/telegram/` |
   | `state/hyvee_session.json`, `state/coros_session.json`, `state/google/` | the same names under the repo's `state/` |
   | `repo/.mcp.json`, `repo/settings.local.json` | repo `.mcp.json`, and repo `.claude/settings.local.json` |

5. Start again: `systemctl --user start assistant-bridge assistant-agent-worker assistant-scheduler.timer`, then run
   `python scripts/scheduler_store.py list` and send the bot a message.

Not in the backup because it can be rebuilt: the venv and `vendor/` (run `scripts/setup_wsl.sh`),
Playwright's browser, generated reports (already in Drive), and logs. The Claude login itself
(`claude auth login`) and the Todoist sign-in are redone by hand.

Also worth doing now and then: a `wsl --export` of the whole distro to a Windows drive.

## Permissions

The agent worker runs Claude in `--permission-mode auto` with the allow/deny rules in
`.claude/settings.json`; scheduled runs are one-shot `claude --print` processes (`scripts/scheduler_dispatch.py`) under the same rules. The deny rules stop the agent from reading or editing credential
files (`~/.config/scherbring-assistant`, `~/.ssh`, `~/.monarch-mcp-server`, the Telegram channel
folder, `.env`, the session files), and from running shell commands that name them. They apply to
every agent run, including scheduled ones. Scripts you run yourself are not affected.
One side effect: an agent shell command containing the text `.env` is refused, even a harmless one.

Headless scheduled runs have nobody to approve a prompt, so any shell command that does not match
an allow rule is denied. When you add an agent that needs a new script, add an allow rule for it.

## Moving to another machine

1. `git clone` the repo, then `scripts/setup_wsl.sh`.
2. Restore from a backup (see **Restore**), or copy `~/.config/scherbring-assistant/` by hand.
3. `claude auth login`, set `BRIDGE_ALLOWED_CHAT_IDS` for the new bot if the token changed, and redo the logins above.
4. `sudo loginctl enable-linger $USER` and `scripts/install_systemd.sh <units>` (see `README.md`).

Only one machine may run the bridge at a time (one bot token, one poller). The Windows
keep-alive task is the only part that is specific to WSL.
