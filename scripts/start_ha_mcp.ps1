<#
.SYNOPSIS
Starts the local ha-mcp HTTP server (Home Assistant MCP) and keeps it running.

.DESCRIPTION
Safe to run any time - by hand, or as the action of the "PersonalAssistantHaMcp"
scheduled task set up by register_ha_mcp_task.ps1. It first checks whether an
instance is already running and exits immediately if so. Otherwise it loads
HOMEASSISTANT_URL / HOMEASSISTANT_TOKEN from the repo's .env (gitignored,
never committed), binds loopback-only (MCP_HOST=127.0.0.1) so the default
/mcp path doesn't need a high-entropy secret, and launches
vendor/ha-mcp/.venv/Scripts/ha-mcp-web.exe on port 8086. If it ever exits
(crash, update, etc.) it's restarted after a short delay.

Claude Code connects to it via the "ha" entry in .mcp.json:
http://127.0.0.1:8086/mcp

Run once to verify manually:
    powershell -ExecutionPolicy Bypass -File scripts\start_ha_mcp.ps1
#>

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$existing = Get-Process -Name "ha-mcp-web" -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "ha-mcp already running (pid $($existing[0].Id)) - not starting a second instance."
    exit 0
}

$EnvFile = Join-Path $RepoRoot ".env"
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$' -and $_ -notmatch '^\s*#') {
            [System.Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], "Process")
        }
    }
}

if (-not $env:HOMEASSISTANT_URL -or -not $env:HOMEASSISTANT_TOKEN) {
    Write-Host "HOMEASSISTANT_URL / HOMEASSISTANT_TOKEN not set in .env - see .env.example."
    exit 1
}

$env:MCP_HOST = "127.0.0.1"
$HaMcpWeb = Join-Path $RepoRoot "vendor\ha-mcp\.venv\Scripts\ha-mcp-web.exe"

Write-Host "Starting ha-mcp HTTP server (loopback-only, http://127.0.0.1:8086/mcp)"

while ($true) {
    & $HaMcpWeb
    Write-Host ""
    Write-Host "ha-mcp exited (exit code $LASTEXITCODE). Restarting in 10 seconds... (Ctrl+C to stop)"
    Start-Sleep -Seconds 10
}
