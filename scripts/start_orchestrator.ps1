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

Restarts pass --continue so a restart (including one triggered by
watchdog_telegram_health.ps1, which force-kills this process on a persistent
broken Telegram MCP connection) resumes the same conversation instead of
starting a blank one.

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
a second unhandled prompt on top of the dev-channels warning below, which is
almost certainly the main reason past restarts silently went missing (the
picker sits waiting for a keypress no differently than the warning did).
--remote-control is now passed on every launch (labels whatever session is
starting), and --continue (unambiguous: most recent conversation in this
directory) is what actually provides restart continuity, not name-based
--resume. This alone should make restarts need a manual keypress far less
often than before.

2026-08-11: --dangerously-load-development-channels (needed for the local
scheduler channel, which is a hand-written script, not a marketplace plugin,
so it can never be loaded via the ordinary --channels allowlist path) makes
every launch show an interactive "WARNING: Loading development channels"
confirmation that needs a keypress, and this has been given up on as
unautomatable in this environment after two failed approaches:
  1. A hidden helper process writing an Enter key event into the console's
     input buffer (WriteConsoleInput/AttachConsole). The Win32 calls report
     success (AttachConsole ok, WriteConsoleInput ok, 2 events written) but
     the prompt is unaffected - strongly suggests Windows Terminal's ConPTY
     layer routes real input through a different pipe than the classic
     console input buffer this API writes to, so the events land somewhere
     the TUI never reads.
  2. Redirecting claude's stdin to a pipe we control, since a non-TTY stdin
     makes claude silently *skip* the confirmation entirely (verified). But
     the interactive TUI then never mounts at all - the session just hangs
     showing nothing, and worse, a human can no longer unstick it by typing
     into the window either, since real keyboard input no longer reaches a
     redirected pipe. Reverted same-day.
Both were reverted. This script now just launches claude directly against a
real console with real stdin - if the warning appears, type Enter into the
window yourself. Should be rare given the --continue fix above; if it starts
happening on every restart again, the --remote-control-name disambiguation
bug (or something like it) has likely regressed.
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
Write-Host "If a 'WARNING: Loading development channels' prompt appears, press Enter to accept it - this is not automated (see script header)."

$SessionName = "Scherbring-Family-Bot-Task-v1"

$first = $true
while ($true) {
    if ($first) {
        claude --debug --remote-control --resume $SessionName --channels plugin:telegram@claude-plugins-official --dangerously-load-development-channels server:scheduler
        $first = $false
    }
    else {
        claude --continue --debug --remote-control --resume $SessionName --channels plugin:telegram@claude-plugins-official --dangerously-load-development-channels server:scheduler
    }
    Reset-TerminalMouseTracking
    Write-Host ""
    Write-Host "Orchestrator exited (exit code $LASTEXITCODE). Restarting in 10 seconds... (Ctrl+C to stop)"
    Start-Sleep -Seconds 10
}
