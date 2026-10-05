# Orchestrator Dashboard (PoC)

A small read-only Flask dashboard that shows orchestrator health signals already
tracked in `../state/`: uptime/restarts, scheduled-task success/failure, watchdog
health, and Telegram fallback usage.

This is a proof-of-concept for one specific question: can a small local web app be
run on this machine and reached from another device over Tailscale, without
exposing anything to the public internet. It is not a service — there's no Windows
Scheduled Task wired up to keep it running; start/stop it manually.

## Run it

```
cd dashboard
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

This binds to `0.0.0.0:5151` so it's reachable on the Tailscale interface, not just
`localhost`.

## Access it

- On this machine: http://localhost:5151
- From another device on the same Tailscale network: `http://<this-machine's-tailscale-ip>:5151`
  (find the IP with `tailscale ip -4` on this machine, or look it up in the Tailscale
  admin console).

## Notes

- Read-only: the app opens `state/agent_results.db` in SQLite read-only mode and only
  reads the JSON/JSONL files under `state/`. It never writes to any orchestrator state.
- The page auto-refreshes every 30 seconds via `/api/status`.
- Stop with Ctrl+C.
