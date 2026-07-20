<#
.SYNOPSIS
Reports whether the personal-assistant orchestrator
(claude --channels plugin:telegram@claude-plugins-official) is currently running.

.DESCRIPTION
Read-only check - starts nothing. Useful before manually opening a new terminal to
launch the orchestrator, so you don't end up with two instances polling Telegram at
once. Exit code 0 = running, 1 = not running.
#>

$MatchPattern = '*channels*plugin:telegram*'

$procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like $MatchPattern }

if ($procs) {
    foreach ($p in $procs) {
        Write-Host "RUNNING  pid=$($p.ProcessId)  started=$($p.CreationDate)"
    }
    exit 0
}
else {
    Write-Host "NOT RUNNING"
    exit 1
}
