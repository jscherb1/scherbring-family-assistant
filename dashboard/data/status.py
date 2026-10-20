"""System Status reader: one "is everything OK?" board for the System tab.

Pure composition of the existing readers (health, agent, ops, schedule); each check below only restates a flag
those readers already compute, so the deep pages (Scheduled tasks, Restarts, Logins & backups, Agent) stay the
source of detail.
"""
from . import agent, health, ops, schedule


def _check(label, ok, detail, href, unknown=False):
    return {"label": label, "state": "unknown" if unknown else ("ok" if ok else "bad"), "detail": detail, "href": href}


def get_status_board():
    st = health.get_status()
    ag = agent.get_agent_status()
    sysd = ops.get_system()
    integ = ops.get_integrations()
    sched = schedule.list_tasks()

    checks = []
    if "error" in ag:
        checks.append(_check("Agent", False, ag["error"], "/agent", unknown=True))
    else:
        checks += [
            _check("Telegram bridge", ag.get("bridge_up") and ag.get("polling_ok"),
                   "polling OK" if ag.get("polling_ok") else "not polling Telegram", "/agent"),
            _check("Agent worker", ag.get("worker_up") and not ag.get("paused"),
                   "paused" if ag.get("paused") else ("running" if ag.get("worker_up") else "not running"), "/agent"),
            _check("MCP servers", not ag.get("mcp_down"),
                   "all connected" if not ag.get("mcp_down") else "offline: " + ", ".join(ag["mcp_down"]), "/agent"),
            _check("Health check", (ag.get("last_canary") or {}).get("ok", False),
                   "passing" if (ag.get("last_canary") or {}).get("ok") else "failing or not run yet", "/agent",
                   unknown=not ag.get("last_canary")),
            _check("Reply delivery", not ag.get("outbox_failed"),
                   f"{ag.get('outbox_failed')} undeliverable" if ag.get("outbox_failed") else "all delivered", "/agent"),
        ]
    hb = sysd.get("heartbeat", {})
    checks.append(_check("Scheduler", not hb.get("stale"), hb.get("error") or f"last tick {hb.get('age_minutes')} min ago",
                         "/health/schedule", unknown="error" in hb))
    bk = sysd.get("backups", {})
    checks.append(_check("Backups", not bk.get("stale"),
                         bk.get("error") or (f"newest {bk.get('newest_age_hours')} h ago" if bk.get("files") else "none found"),
                         "/health/system", unknown="error" in bk or not bk.get("files")))

    logins = [s for s in (sysd.get("credentials") or {}).get("systems", [])]
    integrations = integ.get("systems", []) if "error" not in integ else []
    problems = (sum(1 for c in checks if c["state"] == "bad")
                + sum(1 for s in integrations if s.get("status") == "down")
                + sum(1 for s in logins if s.get("state") in ("warn", "expired")))
    upcoming = [t for t in (sched.get("tasks") or []) if t.get("enabled") and t.get("next_run")][:4] if "error" not in sched else []
    return {
        "problems": problems,
        "checks": checks,
        "integrations": integrations,
        "logins": logins,
        "runs": st.get("task_runs") if "error" not in st else None,
        "uptime": st.get("uptime") if "error" not in st else None,
        "upcoming": upcoming,
        "generated_at": st.get("generated_at"),
    }
