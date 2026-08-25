<#
.SYNOPSIS
Detects a stuck/broken Telegram MCP connection in the running orchestrator and
force-restarts it so start_orchestrator.ps1's loop (now with --continue) can
bring it back up with a clean connection.

.DESCRIPTION
Background: 2026-08-10 investigation found that when ~/.claude/settings.json
gets rewritten on disk (e.g. by a Claude desktop app auto-update restarting its
VM service), the running orchestrator hot-reloads permissions but does not
reconnect already-connected stdio MCP servers. The Telegram channel's tool
binding goes stale ("Tool mcp__plugin_telegram_telegram__reply not found in
render-time tools") and never recovers on its own - only a full process
restart has reliably fixed it.

This script is meant to be called every ~2 minutes (piggybacked on the
watchdog Scheduled Task's cadence via run_watchdog.ps1) so detection-to-heal
is minutes, not hours.

Detection: inspect the tail of the orchestrator's dedicated debug log
(state/logs/orchestrator_debug.log - see 2026-08-23 note below) for three
independent unhealthy signals, any one of which is sufficient:
  - reply-tool grep: "Tool mcp__plugin_telegram_telegram__reply not found in
    render-time tools" or "Filtering out tool_reference for unavailable tool:
    mcp__plugin_telegram_telegram__reply" appearing more recently than the
    last healthy connection line
  - fallback self-report: state/telegram_fallback_used.json stamped more
    recently than the last restart
  - heartbeat staleness: no "CCRClient: Heartbeat sent" line in 10+ minutes

If none of these fire (fresh session, or the log file doesn't exist yet),
treat it as unknown and take NO action - absence of data never triggers a
restart, only an explicit unhealthy signal does.

To avoid restarting on a single transient blip, an unhealthy reading must
persist across two checks at least 3 minutes apart (tracked in
state/telegram_watchdog.json) before this script kills the process. Killing
is safe here only because start_orchestrator.ps1's wrapper loop immediately
restarts it with --continue, resuming the same conversation.

2026-08-11: this force-kill leaves the terminal's mouse-tracking mode stuck
on (claude never gets to run its exit cleanup), which flooded the console
with garbage input and likely contributed to a ~10hr outage. The actual
terminal-reset mitigation lives in start_orchestrator.ps1 (the process that
owns that console) - this script runs headless via Task Scheduler with its
own console, so it can't reach the visible window directly.

2026-08-21: the debug-log grep above only fires AFTER something actually
tries to call the reply tool and fails - a stale binding that nothing has
attempted to use yet produces no error line at all, so it can sit broken
silently (this is exactly what happened: SSE reconnects dropped the tool
mid-day and it went undetected because no reply attempt had occurred since).
scripts/telegram_send.py is now the orchestrator's instructed fallback
whenever the reply tool errors or isn't found - it delivers the message
directly via the Bot API AND stamps state/telegram_fallback_used.json,
since being invoked at all means the real tool was unreachable. That file
is now a second, more reliable unhealthy signal alongside the log grep:
an explicit self-report instead of a heuristic over log text.

2026-08-24: found the reply tool had gone stale silently for ~13 hours
(23:05 restart -> 12:09 next day) with zero Telegram traffic in between to
trip either existing signal - 13 SSE reconnects happened in that window, any
one of which could have dropped the binding per the 2026-08-23 SSE-reconnect
finding below, but nothing had tried to reply so nothing errored. Justin
noticed before the watchdog did. All three existing signals require an
*attempted* Telegram send to ever fire; none of them proactively probe.
Fixed by adding a fourth, proactive signal: track the last time a reply was
*verified* to work (either "Tool 'reply' completed successfully" or the
"Successfully connected" line right after a restart), and if at least one
SSE reconnect/liveness-timeout has happened since that last-verified point
AND $StaleSilentThresholdMinutes (60) have elapsed with no fresh
verification, treat it as unhealthy and let the existing 3-minute
confirmation + restart flow handle it - same as the other signals. This
only fires during genuinely quiet windows (no Telegram traffic to reset the
"last verified" clock), which is exactly when a restart is cheapest: no
in-progress conversation to lose. A real message succeeding at any point
resets the clock and skips the restart entirely.

2026-08-23: found the orchestrator had gone fully silent for 40+ hours
(no "CCRClient: Heartbeat sent" lines at all since 2026-08-21T19:04, right
after a tool-not-found error and an event-loop-stall warning) while the OS
process stayed alive/"Responding", and neither existing signal caught it -
nobody had tried to message it, so the reply tool was never exercised and
never errored. Two compounding bugs, both fixed:
  (a) Log-file selection here used to pick "whatever debug log in
      ~/.claude/debug was most recently modified", which silently breaks
      the moment ANY other Claude Code session on the machine (e.g. an
      interactive terminal session used to investigate the very outage)
      writes to its own debug log more recently than the orchestrator's -
      the watchdog ends up grepping the wrong session's log entirely and
      finds nothing wrong because there's nothing wrong with IT. Fixed at
      the source: start_orchestrator.ps1 now launches with
      `--debug-file state\logs\orchestrator_debug.log`, a fixed path
      dedicated to the orchestrator, so this script just reads that file
      directly - no guessing, no correlation heuristics, no ambiguity even
      if another session starts within the same second.
  (b) The only "unhealthy" signals both require an attempted Telegram send
      to ever fire. A fully wedged orchestrator that isn't crashing but has
      simply stopped pumping its event loop produces neither. Fixed by
      adding a third, generic liveness signal: "CCRClient: Heartbeat sent"
      lines appear roughly every ~1-5 min under normal operation (observed
      max gap 326s across a full session); no heartbeat for 10+ minutes
      while the process is still running means the loop is wedged,
      independent of whether Telegram is involved at all.
#>

$RepoRoot = Split-Path -Parent $PSScriptRoot
$StateFile = Join-Path $RepoRoot "state\telegram_watchdog.json"
$FallbackUsedFile = Join-Path $RepoRoot "state\telegram_fallback_used.json"
$DebugLogFile = Join-Path $RepoRoot "state\logs\orchestrator_debug.log"
$MatchPattern = '*channels*plugin:telegram*'

function Get-WatchdogState {
    if (Test-Path $StateFile) {
        try { return Get-Content $StateFile -Raw | ConvertFrom-Json } catch { }
    }
    return [PSCustomObject]@{ first_unhealthy_at = $null; last_restart_at = $null }
}

function Save-WatchdogState($state) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $StateFile) | Out-Null
    $state | ConvertTo-Json | Set-Content -Path $StateFile -Encoding utf8
}

# Nothing to heal if the orchestrator isn't even running.
$proc = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like $MatchPattern } | Select-Object -First 1
if (-not $proc) {
    exit 0
}

# start_orchestrator.ps1 launches with --debug-file pointed at this fixed
# path, so there's no ambiguity about which log belongs to the orchestrator
# (see 2026-08-23 note above for why dir-wide "most recently modified" used
# to silently pick the wrong session's log).
if (-not (Test-Path $DebugLogFile)) { exit 0 }

$tail = Get-Content $DebugLogFile -Tail 3000 -ErrorAction SilentlyContinue
if (-not $tail) { exit 0 }

$unhealthyPattern = 'Tool mcp__plugin_telegram_telegram__reply not found in render-time tools|Filtering out tool_reference for unavailable tool: mcp__plugin_telegram_telegram__reply'
$healthyPattern = 'MCP server "plugin:telegram:telegram": Successfully connected|MCP server "plugin:telegram:telegram": Tool .reply. completed successfully'

$lastUnhealthy = $tail | Select-String -Pattern $unhealthyPattern | Select-Object -Last 1
$lastHealthy = $tail | Select-String -Pattern $healthyPattern | Select-Object -Last 1

$state = Get-WatchdogState
$now = Get-Date

$logSignalUnhealthy = $lastUnhealthy -and (-not $lastHealthy -or $lastUnhealthy.LineNumber -gt $lastHealthy.LineNumber)

# Second, independent unhealthy signal: scripts/telegram_send.py stamps this
# file every time it's invoked, and it's only ever invoked as a fallback when
# the real reply tool was unreachable - so any timestamp newer than our last
# restart means the binding was (recently) broken, regardless of whether the
# log grep above happened to catch a matching error line.
$fallbackSignalUnhealthy = $false
$fallbackReason = $null
if (Test-Path $FallbackUsedFile) {
    try {
        $fallbackState = Get-Content $FallbackUsedFile -Raw | ConvertFrom-Json
        $fallbackUsedAt = [datetime]$fallbackState.last_used_at
        $sinceLastRestart = -not $state.last_restart_at -or $fallbackUsedAt -gt [datetime]$state.last_restart_at
        if ($sinceLastRestart) {
            $fallbackSignalUnhealthy = $true
            $fallbackReason = $fallbackState.reason
        }
    } catch { }
}

# Third, independent signal: generic process-liveness via heartbeat recency.
# "CCRClient: Heartbeat sent" lines appear every ~1-5 min under normal
# operation regardless of any Telegram activity, so their absence catches a
# fully wedged event loop even when nobody ever tried to send a message
# (the failure mode the two signals above both miss - see 2026-08-23 note).
$heartbeatSignalUnhealthy = $false
$lastHeartbeatLine = $tail | Select-String -Pattern 'CCRClient: Heartbeat sent' | Select-Object -Last 1
if ($lastHeartbeatLine -and $lastHeartbeatLine.Line -match '^(\S+)Z') {
    $lastHeartbeatAt = [datetime]::Parse($Matches[1], $null, [System.Globalization.DateTimeStyles]::RoundtripKind)
    $minutesSinceHeartbeat = ($now.ToUniversalTime() - $lastHeartbeatAt).TotalMinutes
    if ($minutesSinceHeartbeat -ge 10) {
        $heartbeatSignalUnhealthy = $true
    }
}

# Fourth, proactive signal: catch a stale binding during a quiet window, before
# anyone notices. Find the last point the reply tool was *verified* to work -
# either a successful reply completion, or the "Successfully connected" line
# right after a restart (also proof-of-life). If an SSE reconnect/liveness-
# timeout has happened since that point AND it's been $StaleSilentThresholdMinutes
# with no fresh verification, the binding may have silently dropped with nobody
# the wiser - see 2026-08-24 note above. A real successful reply at any time
# resets this clock, so this only ever fires in windows with zero Telegram
# traffic, which is also when a restart is cheapest (nothing mid-conversation
# to lose).
$StaleSilentThresholdMinutes = 60
$staleSilentUnhealthy = $false
$verifiedPattern = "Tool 'reply' completed successfully|plugin:telegram:telegram`": Successfully connected"
$reconnectPattern = 'SSETransport: Stream read error|SSETransport: Liveness timeout, reconnecting'

$lastVerified = $tail | Select-String -Pattern $verifiedPattern | Select-Object -Last 1
if ($lastVerified -and $lastVerified.Line -match '^(\S+)Z') {
    $lastVerifiedAt = [datetime]::Parse($Matches[1], $null, [System.Globalization.DateTimeStyles]::RoundtripKind)
    $minutesSinceVerified = ($now.ToUniversalTime() - $lastVerifiedAt).TotalMinutes
    if ($minutesSinceVerified -ge $StaleSilentThresholdMinutes) {
        $reconnectSinceVerified = $tail | Select-String -Pattern $reconnectPattern |
            Where-Object {
                $_.Line -match '^(\S+)Z' -and
                ([datetime]::Parse($Matches[1], $null, [System.Globalization.DateTimeStyles]::RoundtripKind)) -gt $lastVerifiedAt
            }
        if ($reconnectSinceVerified) {
            $staleSilentUnhealthy = $true
        }
    }
}

$isUnhealthy = $logSignalUnhealthy -or $fallbackSignalUnhealthy -or $heartbeatSignalUnhealthy -or $staleSilentUnhealthy

if ($isUnhealthy) {
    if (-not $state.first_unhealthy_at) {
        $state.first_unhealthy_at = $now.ToString("o")
        Save-WatchdogState $state
        $source = if ($fallbackSignalUnhealthy) { "fallback-script signal ($fallbackReason)" } elseif ($heartbeatSignalUnhealthy) { "no heartbeat for $([math]::Round($minutesSinceHeartbeat,1)) min" } elseif ($staleSilentUnhealthy) { "no verified reply for $([math]::Round($minutesSinceVerified,1)) min with a reconnect since" } else { "log grep" }
        Write-Host "watchdog: unhealthy signal seen via $source, starting 3-minute confirmation window"
        exit 0
    }

    $firstSeen = [datetime]$state.first_unhealthy_at
    if (($now - $firstSeen).TotalMinutes -ge 3) {
        Write-Host "watchdog: persistent broken Telegram connection confirmed - restarting orchestrator (pid $($proc.ProcessId))"
        # 2026-08-25: no pre-restart heads-up message anymore, by design. The
        # goal is a restart the user never notices - context_bridge.py's
        # SessionStart hook replays the rolling conversation log into the
        # fresh session, so there's nothing to warn about losing. A visible
        # "restarting..." ping was itself the tell that undermined seamlessness.
        Stop-Process -Id $proc.ProcessId -Force
        $state.first_unhealthy_at = $null
        $state.last_restart_at = (Get-Date).ToString("o")
        Save-WatchdogState $state
    }
}
else {
    if ($state.first_unhealthy_at) {
        Write-Host "watchdog: Telegram connection recovered on its own - clearing tracking"
        $state.first_unhealthy_at = $null
        Save-WatchdogState $state
    }
}
