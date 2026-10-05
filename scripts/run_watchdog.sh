#!/usr/bin/env bash
# One watchdog pass: Telegram health, scheduler health, context-log prune, and
# log pruning. Invoked every 2 minutes by systemd/assistant-watchdog.timer;
# safe to run by hand for an immediate check.
#
# Output goes to a per-day file under state/logs/ (as the PowerShell version
# did) and, because it is also stdout, to journald.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PY="$REPO_ROOT/.venv/bin/python"
LOG_DIR="$REPO_ROOT/state/logs"
LOG_FILE="$LOG_DIR/watchdog_$(date +%Y-%m-%d).log"
RETENTION_DAYS="${LOG_RETENTION_DAYS:-14}"
mkdir -p "$LOG_DIR"

{
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] --- watchdog pass ---"
    "$PY" scripts/watchdog_telegram_health.py 2>&1
    "$PY" scripts/watchdog_scheduler_health.py 2>&1
    "$PY" scripts/telegram_context_bridge.py prune 2>&1
} | tee -a "$LOG_FILE"

# Prune old per-day and per-run logs so state/logs doesn't grow without bound.
find "$LOG_DIR" -type f -name '*.log' -mtime "+$RETENTION_DAYS" \
    ! -name 'orchestrator_debug.log' ! -name 'orchestrator_exits.log' -delete
