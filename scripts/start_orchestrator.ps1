<#
.SYNOPSIS
Starts the personal-assistant orchestrator (claude --channels plugin:telegram@...)
and keeps it running.

.DESCRIPTION
Safe to run any time - by hand, or as the action of the "PersonalAssistantOrchestrator"
scheduled task set up by register_orchestrator_task.ps1. It first checks whether an
instance is already running and exits immediately if so, so you never end up with two
copies polling Telegram at the same time. Otherwise it launches the orchestrator and,
if it ever exits (crash, update, etc.), restarts it after a short delay. Minimize this
window to get it out of the way; closing it stops the assistant.

Every launch resumes the orchestrator's own named conversation
("Scherbring-Family-Bot-Task-v1", via --resume) instead of starting a blank
one, including a restart triggered by watchdog_telegram_health.ps1 or
watchdog_scheduler_health.ps1 force-killing this process.

2026-08-11: a force-kill (Stop-Process -Force) doesn't give claude a chance
to run its normal exit cleanup, which includes disabling the xterm mouse-
tracking mode it turns on for its TUI. Left enabled, every subsequent mouse
move/click in this window gets translated into raw escape codes (e.g.
"ESC[<51;65;30M") and fed into the console as if typed - which can flood the
console with garbage and, per the 2026-08-10 ~10hr outage, apparently wedge
this loop badly enough to block the next restart. So after every claude exit
(for any reason), reset the terminal's mouse-tracking/cursor state before
looping.

2026-08-11: the remote-control app identifies sessions by the name passed to
--remote-control, NOT --resume. --resume expects a session ID and only falls
back to fuzzy name-matching, which becomes ambiguous the moment more than one
past session shares this name - confirmed this had regressed to hitting an
interactive "multiple sessions match" disambiguation picker on every restart,
a second unhandled prompt on top of the (now-retired, see below) dev-channels
warning, which is almost certainly the main reason past restarts silently
went missing (the picker sits waiting for a keypress no differently than the
warning did).

2026-08-12: switching to plain --continue (dropping --resume $SessionName
entirely) turned out to be the wrong fix - --continue resumes "the most
recent conversation in this directory," which is whatever session was last
active there, not necessarily the orchestrator's own dedicated conversation
(e.g. it could grab an ad-hoc dev/implementation session run in this same
repo). The named --resume is intentional: it guarantees this always resumes
specifically the orchestrator's own tagged conversation, not just whatever
happened to run here most recently.

The actual root cause of the ambiguous "multiple sessions match" picker was
several past sessions all carrying the same custom title
"Scherbring-Family-Bot-Task-v1" (each restart that hit the picker in the past
apparently forked a fresh untitled session rather than truly resuming,
compounding over time). Cleaned up by renaming the stale duplicate(s) out of
the way (via the same custom-title mechanism /rename uses) so exactly one
session carries the name - see git history / conversation log around
2026-08-12 for the cleanup. With only one match, --resume $SessionName
resolves unambiguously and no picker should appear. If it ever does again,
that means a duplicate has reappeared and needs the same cleanup - check
`grep -h '"type":"custom-title"' ~/.claude/projects/<this-project-hash>/*.jsonl`
for more than one session ending on this name.
--remote-control is still passed on every launch to label the session for the
remote-control app.

2026-08-11: --dangerously-load-development-channels (needed for the local
scheduler channel, which is a hand-written script, not a marketplace plugin,
so it can never be loaded via the ordinary --channels allowlist path) made
every launch show an interactive "WARNING: Loading development channels"
confirmation that needs a keypress, and this had been given up on as
unautomatable in this environment after two failed approaches (a hidden
helper process writing a synthetic Enter key event into the console input
buffer, and redirecting claude's stdin to a controlled pipe - both reverted;
see git history for details if this is ever revisited elsewhere).

2026-08-12: retired the local scheduler channel entirely rather than continue
working around its launch-flag prompt. The scheduled-tasks poll loop is now
armed by the orchestrator itself, in-session, via CronCreate (see the
"Scheduler self-arming" section of CLAUDE.md and
scripts/watchdog_scheduler_health.ps1 for the OS-level backstop that verifies
it's actually ticking). No dev channel, no --dangerously-load-development-channels
flag, no launch-time confirmation dialog - this should make unattended
auto-restart actually unattended.
#>

function Reset-TerminalMouseTracking {
    $esc = [char]27
    # Disable mouse tracking modes (1000/1002/1003 press/drag/motion, 1006 SGR
    # extended coords), disable bracketed paste, and make sure the cursor is visible.
    Write-Host -NoNewline "$esc[?1000l$esc[?1002l$esc[?1003l$esc[?1006l$esc[?2004l$esc[?25h"
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$MatchPattern = '*channels*plugin:telegram*'
$existing = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like $MatchPattern }
if ($existing) {
    Write-Host "Orchestrator already running (pid $($existing[0].ProcessId)) - not starting a second instance."
    exit 0
}

Write-Host "Starting personal-assistant orchestrator from $RepoRoot"
Write-Host "Minimize this window to keep it running in the background; closing it stops the assistant."

$SessionName = "Scherbring-Family-Bot-Task-v1"

while ($true) {
    claude --resume $SessionName --debug --remote-control --channels plugin:telegram@claude-plugins-official
    Reset-TerminalMouseTracking
    Write-Host ""
    Write-Host "Orchestrator exited (exit code $LASTEXITCODE). Restarting in 10 seconds... (Ctrl+C to stop)"
    Start-Sleep -Seconds 10
}
