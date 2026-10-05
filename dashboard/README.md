# Orchestrator Dashboard (PoC)

A small read-only Flask dashboard that shows orchestrator health signals already
tracked in `../state/`: uptime/restarts, scheduled-task success/failure, watchdog
health, and Telegram fallback usage.

This is a proof-of-concept for one specific question: can a small local web app be
run on this machine and reached from another device over Tailscale, without
exposing anything to the public internet.

## Run it

From the repo root, using the shared virtualenv (`scripts/setup_wsl.sh`):

```
.venv/bin/python dashboard/app.py
```

It binds to `127.0.0.1:5151` by default. Environment variables:

- `DASHBOARD_HOST` - address to bind. Use this machine's Tailscale IP
  (`tailscale ip -4`) to reach it from other devices; avoid `0.0.0.0`.
- `DASHBOARD_PORT` - port (default 5151).

To keep it running, install and enable the user unit:

```
scripts/install_systemd.sh assistant-dashboard.service
```

(Edit `Environment=DASHBOARD_HOST=...` in the installed unit to change the bind
address, then `systemctl --user daemon-reload && systemctl --user restart assistant-dashboard`.)

## Access it

- On this machine: http://localhost:5151
- From another device on the same Tailscale network, once `DASHBOARD_HOST` is set to
  the Tailscale IP: `http://<tailscale-ip>:5151`.

## Notes

- Read-only: the app opens `state/agent_results.db` in SQLite read-only mode and only
  reads the JSON/JSONL files under `state/`. It never writes to any orchestrator state.
- The page auto-refreshes every 30 seconds via `/api/status`.
- Stop with Ctrl+C.
