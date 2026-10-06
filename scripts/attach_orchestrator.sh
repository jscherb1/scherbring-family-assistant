#!/usr/bin/env bash
# Show the orchestrator's tmux session in this terminal, and keep showing it.
#
# The assistant itself runs headless under systemd (assistant-orchestrator.service).
# This only *views* it: closing the window or pressing Ctrl-b d detaches and the
# assistant keeps running. If the session restarts, this re-attaches by itself.
SESSION="${ORCH_TMUX_SESSION:-assistant}"
while true; do
    if tmux has-session -t "$SESSION" 2>/dev/null; then
        tmux attach -t "$SESSION"
    else
        echo "Waiting for tmux session '$SESSION' (systemctl --user status assistant-orchestrator)..."
    fi
    sleep 3
done
