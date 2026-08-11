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
existing scheduler poller's Task Scheduler cadence via run_scheduler_poll.ps1)
so detection-to-heal is minutes, not hours.

Detection: inspect the tail of the most-recently-modified debug log under
~/.claude/debug/*.txt (expected to be the live orchestrator's log) for
whichever of these signals appears LAST:
  - unhealthy: "Tool mcp__plugin_telegram_telegram__reply not found in
    render-time tools" or "Filtering out tool_reference for unavailable tool:
    mcp__plugin_telegram_telegram__reply"
  - healthy:   MCP server "plugin:telegram:telegram": Successfully connected,
               or ...: Tool 'reply' completed successfully

If neither signal is present (fresh session, or the wrong log file got picked),
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
#>

$RepoRoot = Split-Path -Parent $PSScriptRoot
$StateFile = Join-Path $RepoRoot "state\telegram_watchdog.json"
$DebugDir = Join-Path $env:USERPROFILE ".claude\debug"
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

$logFile = Get-ChildItem $DebugDir -Filter *.txt -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $logFile) { exit 0 }

$tail = Get-Content $logFile.FullName -Tail 3000 -ErrorAction SilentlyContinue
if (-not $tail) { exit 0 }

$unhealthyPattern = 'Tool mcp__plugin_telegram_telegram__reply not found in render-time tools|Filtering out tool_reference for unavailable tool: mcp__plugin_telegram_telegram__reply'
$healthyPattern = 'MCP server "plugin:telegram:telegram": Successfully connected|MCP server "plugin:telegram:telegram": Tool .reply. completed successfully'

$lastUnhealthy = $tail | Select-String -Pattern $unhealthyPattern | Select-Object -Last 1
$lastHealthy = $tail | Select-String -Pattern $healthyPattern | Select-Object -Last 1

$state = Get-WatchdogState
$now = Get-Date

$isUnhealthy = $lastUnhealthy -and (-not $lastHealthy -or $lastUnhealthy.LineNumber -gt $lastHealthy.LineNumber)

if ($isUnhealthy) {
    if (-not $state.first_unhealthy_at) {
        $state.first_unhealthy_at = $now.ToString("o")
        Save-WatchdogState $state
        Write-Host "watchdog: unhealthy signal seen, starting 3-minute confirmation window"
        exit 0
    }

    $firstSeen = [datetime]$state.first_unhealthy_at
    if (($now - $firstSeen).TotalMinutes -ge 3) {
        Write-Host "watchdog: persistent broken Telegram connection confirmed - restarting orchestrator (pid $($proc.ProcessId))"
        Stop-Process -Id $proc.ProcessId -Force
        $state.first_unhealthy_at = $null
        $state.last_restart_at = $now.ToString("o")
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
