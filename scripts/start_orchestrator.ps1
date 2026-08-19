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

Every launch starts a fresh session — no --resume flag — so context never
accumulates across restarts. Each restart (including those triggered by
watchdog_telegram_health.ps1 or watchdog_scheduler_health.ps1) begins with a
clean slate. Each launch gets a unique display/Remote Control name of the
form "Scherbring-Family-Bot-yyyyMMdd-HHmmss" (never reused, never resumed)
so a specific run is identifiable in the prompt box, /resume picker, and
terminal title. --remote-control is still passed so the in-session
CronCreate/CronList tools are available.

2026-08-11: a force-kill (Stop-Process -Force) doesn't give claude a chance
to run its normal exit cleanup, which includes disabling the xterm mouse-
tracking mode it turns on for its TUI. Left enabled, every subsequent mouse
move/click in this window gets translated into raw escape codes (e.g.
"ESC[<51;65;30M") and fed into the console as if typed - which can flood the
console with garbage and, per the 2026-08-10 ~10hr outage, apparently wedge
this loop badly enough to block the next restart. So after every claude exit
(for any reason), reset the terminal's mouse-tracking/cursor state before
looping.


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

2026-08-19: discovered the orchestrator was running under an org-managed
Claude account (Remote Control disabled by org policy) because auth is a
single shared ~/.claude/.credentials.json for the whole Windows profile -
whichever account last ran `claude auth login` wins, everywhere, including
here. Tried loading a personal-account long-lived token (`claude setup-token`)
into CLAUDE_CODE_OAUTH_TOKEN to decouple the orchestrator's identity from
whatever's logged in interactively elsewhere - but long-lived tokens are
inference-only and Remote Control refuses to enable under one at all
("Remote Control requires a full-scope login token"). Reverted: Remote
Control matters more here than account isolation, so the orchestrator once
again just inherits whatever account is currently logged in via
`claude auth login` on this machine. Practical effect: mainly use the
personal account for interactive logins on this machine when Remote Control
on the orchestrator is needed.

2026-08-19: every launch now passes --permission-mode auto so the
orchestrator always starts in auto mode instead of whatever mode was left
over interactively.
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

while ($true) {
    $SessionName = "Scherbring-Family-Bot-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
    claude --debug --remote-control $SessionName --name $SessionName --permission-mode auto --channels plugin:telegram@claude-plugins-official
    Reset-TerminalMouseTracking
    Write-Host ""
    Write-Host "Orchestrator exited (exit code $LASTEXITCODE). Restarting in 10 seconds... (Ctrl+C to stop)"
    Start-Sleep -Seconds 10
}
