#!/usr/bin/env bash
# Linux port of start_orchestrator.ps1: run the Claude Code orchestrator in a
# loop, restarting it 10 seconds after every exit.
#
# Run it inside a terminal (tmux): claude is an interactive TUI and needs a
# pty. systemd/assistant-orchestrator.service starts it in a tmux session.
#
# - Single instance: a flock on state/orchestrator.lock. A second copy exits 0.
# - Every launch is recorded via orchestrator_restart_log.py
#   (wrapper-start first, loop-restart afterwards).
# - Every exit is appended to state/logs/orchestrator_exits.log.
# - The debug log is pinned to state/logs/orchestrator_debug.log, which the
#   Telegram watchdog reads.
# - Every launch is a fresh session; telegram_context_bridge.py replays the
#   last 60 minutes of conversation.
#
# ORCH_RESTART_DELAY (seconds, default 10) and CLAUDE_BIN (default: claude) exist
# so tests can shorten the wait and substitute a stub.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# The venv first on PATH makes the bare `python scripts/...` commands used by
# the agents and permission rules resolve to the venv interpreter.
export PATH="$REPO_ROOT/.venv/bin:$HOME/.local/bin:$HOME/.bun/bin:$PATH"

SECRETS_ENV="${SCHERBRING_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/scherbring-assistant}/.env"
if [[ -f "$SECRETS_ENV" ]]; then
    set -a
    # shellcheck disable=SC1090
    . "$SECRETS_ENV"
    set +a
fi

LOG_DIR="$REPO_ROOT/state/logs"
DEBUG_LOG="$LOG_DIR/orchestrator_debug.log"
EXIT_LOG="$LOG_DIR/orchestrator_exits.log"
mkdir -p "$LOG_DIR"

exec 9>"$REPO_ROOT/state/orchestrator.lock"
if ! flock -n 9; then
    echo "Orchestrator already running - not starting a second instance."
    exit 0
fi

reset_terminal_mouse_tracking() {
    # Disable mouse tracking (1000/1002/1003/1006) and bracketed paste, and
    # make sure the cursor is visible, in case claude died mid-TUI.
    printf '\033[?1000l\033[?1002l\033[?1003l\033[?1006l\033[?2004l\033[?25h'
}

echo "Starting personal-assistant orchestrator from $REPO_ROOT"

reason="wrapper-start"
while true; do
    session_name="Scherbring-Family-Bot-$(date +%Y%m%d-%H%M%S)"
    python "$REPO_ROOT/scripts/orchestrator_restart_log.py" record \
        --session-name "$session_name" --reason "$reason" >/dev/null || true
    reason="loop-restart"

    "${CLAUDE_BIN:-claude}" --debug-file "$DEBUG_LOG" --name "$session_name" \
        --permission-mode auto \
        --channels plugin:telegram@claude-plugins-official
    exit_code=$?

    reset_terminal_mouse_tracking
    echo
    echo "Orchestrator exited (exit code $exit_code). Restarting in ${ORCH_RESTART_DELAY:-10} seconds... (Ctrl+C to stop)"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] session=$session_name exit_code=$exit_code" >>"$EXIT_LOG"

    sleep "${ORCH_RESTART_DELAY:-10}"
done
