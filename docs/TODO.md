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
15. **Unmerged work:** the `wsl-port` branch is in pull request #2 (https://github.com/jscherb1/scherbring-family-assistant/pull/2) and is not merged to `main`. Merging
    is the user's decision.

## Not started (ideas, from README "Backlog")

See the Backlog section of `README.md`.
