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
#>

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
    claude --channels plugin:telegram@claude-plugins-official --dangerously-load-development-channels server:scheduler
    Write-Host ""
    Write-Host "Orchestrator exited (exit code $LASTEXITCODE). Restarting in 10 seconds... (Ctrl+C to stop)"
    Start-Sleep -Seconds 10
}
