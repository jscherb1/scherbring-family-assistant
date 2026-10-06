# Operations runbook

Day-to-day care of the assistant on its WSL2 Ubuntu 24.04 host. For first-time setup see
`GETTING_STARTED.md` and `README.md`. Open follow-ups are in `docs/TODO.md`.

Everything runs as **systemd user units** (`systemctl --user ...`). The repo is
`~/projects/scherbring-family-assistant`; secrets are in `~/.config/scherbring-assistant/`.

## What runs

| Unit | Job |
|---|---|
| `assistant-orchestrator.service` | Claude Code with the Telegram channel, in a tmux session named `assistant` |
| `assistant-scheduler.timer` | every 2 min: run due scheduled tasks (`scripts/scheduler_dispatch.py`) |
| `assistant-watchdog.timer` | every 2 min: Telegram-health and scheduler-health checks, log pruning |
| `assistant-ha-mcp.service` | local Home Assistant MCP server, 127.0.0.1:8086 |
| `assistant-credential-check.timer` | daily 09:30: warn before Hy-Vee, Drive, Monarch logins expire |
| `assistant-backup.timer` | daily 03:15: back up the database, memory notes and secrets |
| `assistant-dashboard.service` | optional dashboard, 127.0.0.1 only (not enabled by default) |

## Is it healthy?

```
systemctl --user list-timers 'assistant-*'          # every timer should have a NEXT time
systemctl --user status assistant-orchestrator      # active (running)
tmux attach -t assistant                            # look at it; Ctrl-b d to leave
python scripts/credential_check.py --status         # login ages and probes, sends nothing
python scripts/scheduler_store.py list              # the 12 scheduled tasks
```

Quick end-to-end check: send the bot a message. If there is no reply within a minute, look at
`state/orchestrator_restarts.jsonl` (restart history) and `state/logs/orchestrator_debug.log`.

## Logs

| Where | What |
|---|---|
| `journalctl --user -u assistant-scheduler -n 50` | dispatcher ticks (also `-u assistant-watchdog`, `-u assistant-backup`, ...) |
| `state/logs/orchestrator_debug.log` | the orchestrator's Claude debug log (read by the Telegram watchdog) |
| `state/logs/orchestrator_exits.log` | one line per orchestrator exit |
| `state/logs/watchdog_YYYY-MM-DD.log` | each watchdog pass |
| `state/logs/scheduler_dispatch_debug/*.log` | one debug log per scheduled run |
| `scheduled_task_runs` table | per-run status and summary (`scheduler_store.py`) |

Logs older than 14 days are pruned by the watchdog.

An `ok` status only means the run exited cleanly and its message was delivered. The dispatcher
now marks a run **failed** if it shows any tool-permission denial, but still read the Telegram
message: a reply that says it could not finish is a failure even if the status looks fine.

## Start, stop, restart

```
systemctl --user restart assistant-orchestrator     # fresh Claude session; last hour of chat is replayed
systemctl --user stop assistant-orchestrator        # stop; it will NOT come back until started
systemctl --user start assistant-orchestrator
systemctl --user restart assistant-scheduler.timer
```

After editing a unit in `systemd/`, run `scripts/install_systemd.sh` (it re-templates the
paths and reloads), then restart the unit. Never run a second orchestrator by hand while the
unit is up: two pollers on one bot token fight over Telegram updates. (The launcher refuses a
second copy with a lock, but check `tmux ls` first.)

## Keeping WSL alive (Windows side)

WSL2 only runs while its VM is up. Three pieces keep it up:

1. `/etc/wsl.conf` has `[boot] systemd=true`.
2. `C:\Users\<you>\.wslconfig` has `vmIdleTimeout=-1` under `[wsl2]` (apply with `wsl --shutdown`).
3. A Windows startup task, `WSL-Keepalive-Assistant`, runs
   `wsl.exe -d Ubuntu-24.04 -u jscherb1 -- sleep infinity`.

Plus `sudo loginctl enable-linger $USER` so user units run with no login session.

Optional: `scripts/register_attach_window.ps1` registers a logon task, `WSL-Assistant-Window`,
that opens a window showing the live orchestrator. It only views the session; closing it never
stops the assistant. Remove with `Unregister-ScheduledTask -TaskName WSL-Assistant-Window -Confirm:$false`.

Windows sleep is not the problem on this machine: standby is set to never, and the clock-jump
warnings in the debug log are about a second long, not sleep-sized. If replies are ever
delayed, note when you sent and when it answered, then check `state/orchestrator_restarts.jsonl`.

## Logins that expire

Three systems need a manual login now and then. `assistant-credential-check.timer` probes each
daily and sends a Telegram message two days before the expected expiry, with the exact command.
COROS is not on the list: it logs in again by itself.

| System | Lasts about | Re-login |
|---|---|---|
| Hy-Vee | 11-12 days | `.venv/bin/python scripts/hyvee/login_test.py --login-only --manual-wait 180` (a browser window opens; solve the CAPTCHA) |
| Google Drive | 7 days while the OAuth app is in *Testing* | `.venv/bin/python scripts/drive_upload.py auth` (open the printed URL, approve). See `docs/retirement-drive-setup.md` for publishing the app so this stops being weekly. |
| Monarch | about 2 weeks | `cd vendor/monarch-mcp-server && .venv/bin/python login_setup.py`, choose option 1, paste the cookie header from app.monarch.com |

Things that look like bugs but are not:
- The Monarch tool `check_auth_status` says "not authenticated" even when the login works. Judge by
  a real read such as `get_accounts`.
- COROS allows one web session per account. A fresh automated login signs you out of COROS elsewhere.
- Monarch's official claude.ai connector is paused by Monarch; this setup uses the vendored server.

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

1. Stop the assistant: `systemctl --user stop assistant-orchestrator assistant-scheduler.timer`
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

5. Start again: `systemctl --user start assistant-orchestrator assistant-scheduler.timer`, then run
   `python scripts/scheduler_store.py list` and send the bot a message.

Not in the backup because it can be rebuilt: the venv and `vendor/` (run `scripts/setup_wsl.sh`),
Playwright's browser, generated reports (already in Drive), and logs. The Claude login itself
(`claude auth login`) and the Todoist sign-in are redone by hand.

Also worth doing now and then: a `wsl --export` of the whole distro to a Windows drive.

## Permissions

The orchestrator runs with `--permission-mode auto` and the allow/deny rules in
`.claude/settings.json`. The deny rules stop the agent from reading or editing credential
files (`~/.config/scherbring-assistant`, `~/.ssh`, `~/.monarch-mcp-server`, the Telegram channel
folder, `.env`, the session files), and from running shell commands that name them. They apply to
every agent run, including scheduled ones. Scripts you run yourself are not affected.
One side effect: an agent shell command containing the text `.env` is refused, even a harmless one.

Headless scheduled runs have nobody to approve a prompt, so any shell command that does not match
an allow rule is denied. When you add an agent that needs a new script, add an allow rule for it.

## Moving to another machine

1. `git clone` the repo, then `scripts/setup_wsl.sh`.
2. Restore from a backup (see **Restore**), or copy `~/.config/scherbring-assistant/` by hand.
3. `claude auth login`, re-pair Telegram if the bot token changed, and redo the logins above.
4. `sudo loginctl enable-linger $USER` and `scripts/install_systemd.sh <units>` (see `README.md`).

Only one machine may run the orchestrator at a time (one bot token, one poller). The Windows
keep-alive task is the only part that is specific to WSL.
