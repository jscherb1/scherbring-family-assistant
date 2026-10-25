"""Landing-page summary: one headline per section plus a short 'needs attention' list.

Pure composition of the existing readers; no queries of its own. Everything here is
already safe to show (no finance amounts, no memory text).
"""
from datetime import timedelta

from . import (activity, agent, chores, common, family, golf, habits, health, home, money, ops, schedule, shopping_fitness,
               wellness)
from .. import dismissals
from ..web import fmt_dt

MAX_ATTENTION = 5


def _flag(attention, key, text, href, tile=None):
    """Record a needs-attention item. The key identifies this specific event so a dismissal
    sticks to it (and a new event of the same kind still shows up)."""
    attention.append({"key": key, "text": text, "href": href, "tile": tile})


def _ok(d) -> bool:
    return isinstance(d, dict) and "error" not in d


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _agent_tile(attention):
    st = agent.get_agent_status()
    usage = agent.get_usage()
    if not _ok(st):
        _flag(attention, "agent:unreadable", "Agent status unavailable", "/agent", "Agent")
        return {"title": "Agent", "href": "/agent", "headline": "Unknown", "sub": "Status unreadable", "state": "warn"}
    up = bool(st.get("bridge_up") and st.get("worker_up") and st.get("polling_ok"))
    if not up:
        _flag(attention, "agent:down", "The agent bridge or worker is not running", "/agent", "Agent")
    if st.get("paused"):
        _flag(attention, "agent:paused", "The agent is paused", "/agent", "Agent")
    if st.get("mcp_down"):
        _flag(attention, "agent:mcp:" + ",".join(sorted(st["mcp_down"])),
              f"Tools offline: {', '.join(st['mcp_down'])}", "/agent", "Agent")
    days = usage.get("days") or []
    today = days[0] if days else None
    sub = f"{_plural(today['turns'], 'turn')} today · ${today['cost_usd']:.2f}" if today else "No activity today"
    queued = (st.get("inbox_pending") or 0) + (st.get("inbox_processing") or 0)
    if queued:
        sub = f"{queued} in queue · {sub}"
    headline = "Online" if up and not st.get("paused") else ("Paused" if st.get("paused") else "Offline")
    return {"title": "Agent", "href": "/agent", "headline": headline, "sub": sub,
            "state": "ok" if headline == "Online" else "warn"}


def _schedule_tile(attention):
    status = health.get_status()
    runs = status.get("task_runs") if _ok(status) else None
    sched = schedule.list_tasks()
    tasks = [t for t in (sched.get("tasks") or []) if t.get("enabled")] if _ok(sched) else []
    nxt = min((t for t in tasks if t.get("next_run")), key=lambda t: t["next_run"], default=None)
    if not runs or runs.get("error"):
        return {"title": "Scheduled tasks", "href": "/health/schedule", "headline": "Unknown", "sub": "", "state": "warn"}
    cutoff = common.now() - timedelta(hours=24)
    for f in runs.get("recent_failures") or []:
        ts = common.parse_ts(f["run_at"])
        if ts and ts >= cutoff:
            _flag(attention, f"sched:{f['task_name']}:{f['run_at']}",
                  f"{f['task_name']} failed ({fmt_dt(f['run_at'])})", "/health/schedule", "Scheduled tasks")
    fails = runs.get("fail_24h") or 0
    rate = runs.get("success_rate_24h")
    headline = f"{rate:.0f}% ok" if rate is not None else "No runs yet"
    sub = f"Next: {nxt['name']}" if nxt else f"{_plural(len(tasks), 'task')} enabled"
    return {"title": "Scheduled tasks", "href": "/health/schedule", "headline": headline, "sub": sub,
            "state": "warn" if fails else "ok", "next_run": nxt["next_run"] if nxt else None}


def _credentials(attention):
    sysd = ops.get_system()
    systems = (sysd.get("credentials") or {}).get("systems", []) if _ok(sysd) else []
    for s in systems:
        left = s.get("days_left")
        if s.get("state") == "expired" or (left is not None and left <= 3):
            word = "expired" if s.get("state") == "expired" or (left is not None and left <= 0) else f"expires in {left:.0f}d"
            _flag(attention, f"cred:{s['key']}:{s.get('authed_at')}", f"{s['label']} login {word}",
                  "/health/system", "Hy-Vee" if s["key"] == "hyvee" else None)
    return systems


def _home_tile(attention):
    m = home.get_maintenance()
    if not _ok(m):
        return {"title": "Home & yard", "href": "/home", "headline": "Unknown", "sub": "", "state": "warn"}
    items = [i for i in m.get("items", []) if i.get("active", 1)]
    overdue = [i for i in items if i.get("bucket") == "overdue"]
    soon = [i for i in items if i.get("bucket") == "soon"]
    for i in overdue:
        _flag(attention, f"home:{i['id']}:{i.get('due_date')}", f"{i['name']} is overdue", "/home", "Home & yard")
    if overdue:
        headline, state = f"{len(overdue)} overdue", "warn"
    elif soon:
        headline, state = f"{len(soon)} due soon", "ok"
    else:
        headline, state = "All caught up", "ok"
    sub = f"{_plural(len(soon), 'due soon item')}" if overdue and soon else (soon[0]["name"] if soon else f"{len(items)} items tracked")
    return {"title": "Home & yard", "href": "/home", "headline": headline, "sub": sub, "state": state}


def _golf_tile():
    g = golf.get_rounds(limit=1)
    href = "/golf"
    if not _ok(g):
        return {"title": "Golf", "href": href, "headline": "Unknown", "sub": "", "state": "warn"}
    h = g["handicap"]
    if h["handicap"] is None:
        return {"title": "Golf", "href": href, "headline": "No rounds yet", "sub": "Log one to get a handicap", "state": "ok"}
    last = h["last_round"]
    return {"title": "Golf", "href": href, "headline": f"Handicap {h['handicap']}",
            "sub": f"Last: {last['score']} at {last['course']}", "state": "ok"}


def _habits_tile(attention):
    h = habits.get_habits(include_paused=False)
    if not _ok(h):
        return {"title": "Habits", "href": "/habits", "headline": "Unknown", "sub": "", "state": "warn"}
    items = h["habits"]
    if not items:
        return {"title": "Habits", "href": "/habits", "headline": "None yet", "sub": "Add one to start a streak", "state": "ok"}
    at_risk = [i for i in items if i["at_risk"]]
    for i in at_risk:
        _flag(attention, f"habits:{i['id']}:{i['period_start']}",
              f"{i['name']} streak ({i['current_streak']}) is at risk", "/habits", "Habits")
    best = max((i for i in items if i["has_goal"]), key=lambda i: i["current_streak"], default=None)
    if at_risk:
        headline, state = f"{len(at_risk)} at risk", "warn"
    else:
        headline, state = f"{_plural(len(items), 'habit')} on track", "ok"
    sub = f"Best streak: {best['name']} ({best['current_streak']})" if best and best["current_streak"] else "No active streaks yet"
    return {"title": "Habits", "href": "/habits", "headline": headline, "sub": sub, "state": state}


def _wellness_tile():
    w = wellness.get_today()
    href = "/wellness"
    if not _ok(w):
        return {"title": "Wellness", "href": href, "headline": "Unknown", "sub": "", "state": "warn"}
    rec, sleep = w["rings"]["recovery"], w["sleep"]
    if rec["value"] is not None:
        headline = f"{rec['value']}% recovery" + (" (est.)" if rec["source"] == "estimate" else "")
    else:
        headline = "Waiting for last night"
    parts = []
    if sleep["day"]:
        parts.append(f"{sleep['display']['sleep_hours']} sleep" + ("" if sleep["status"] == "final" else " (earlier night)"))
    if w["checkin_open"] and not w["checkin"]["complete"]:
        parts.append("check-in open")
    if w["out_of_range"]:
        parts.append(f"{', '.join(w['out_of_range'])} off normal")
    return {"title": "Wellness", "href": href, "headline": headline, "sub": " · ".join(parts),
            "state": "warn" if rec["zone"] == "bad" else "ok"}


def _chores_tile():
    o = chores.get_overview()
    href = "/chores"
    if not _ok(o):
        return {"title": "Chores", "href": href, "headline": "Unknown", "sub": "", "state": "warn"}
    if not o["kids"]:
        return {"title": "Chores", "href": href, "headline": "Not set up", "sub": "Add the kids and their chores", "state": "ok"}
    if not o["total_count"]:
        return {"title": "Chores", "href": href, "headline": "Nothing due today", "sub": f"{o['owed_total']} owed", "state": "ok"}
    return {"title": "Chores", "href": href, "headline": f"{o['done_count']}/{o['total_count']} done today",
            "sub": f"{o['owed_total']} owed to the kids", "state": "ok"}


def _meals_tile():
    ins = activity.get_meal_insights()
    if not _ok(ins):
        return {"title": "Dinner", "href": "/meals/plan", "headline": "Unknown", "sub": "", "state": "warn"}
    menu = ins.get("menu") or []
    if menu:
        first = menu[0]
        return {"title": "Dinner", "href": "/meals/plan", "headline": first["title"],
                "sub": f"{first['date']} · {_plural(ins.get('recipe_count', 0), 'recipe')} saved", "state": "ok",
                "small": True}
    return {"title": "Dinner", "href": "/meals/plan", "headline": "Nothing planned",
            "sub": f"{_plural(ins.get('recipe_count', 0), 'recipe')} saved", "state": "ok"}


def _finance_tile():
    o = money.get_overview()
    if not _ok(o):
        return {"title": "Money", "href": "/finance", "headline": "Unknown", "sub": "", "state": "warn"}
    if not o["sync"]["has_data"]:
        return {"title": "Money", "href": "/finance", "headline": "Not synced", "sub": "waiting for the first Monarch sync",
                "state": "warn"}
    flex = next((b for b in o["budget"] if b["kind"] == "flexible"), None)
    if flex:
        left = flex["left"]
        headline = f"${abs(round(left)):,} {'left' if left >= 0 else 'over'}"
        sub = "flexible budget"
    else:
        headline, sub = f"${round(o['spending']['mtd']):,}", "spent this month"
    r = o["review"]
    if r["needs_review"] or r["no_who"]:
        sub += f" · {r['needs_review']} to review, {r['no_who']} no WHO"
    red = (flex and flex["status"] == "red") or r["needs_review_status"] == "red" or o["sync"]["stale"]
    return {"title": "Money", "href": "/finance", "headline": headline, "sub": sub, "state": "warn" if red else "ok"}


def _fitness_tile():
    f = shopping_fitness.get_fitness()
    if not _ok(f):
        return {"title": "Fitness", "href": "/fitness", "headline": "Unknown", "sub": "", "state": "warn"}
    weeks = f.get("weeks") or []
    if f.get("is_current_week") and f.get("week"):
        n = len(f["week"])
        return {"title": "Fitness", "href": "/fitness", "headline": f"{n} workouts", "sub": "planned this week", "state": "ok"}
    return {"title": "Fitness", "href": "/fitness", "headline": "No plan this week",
            "sub": f"Last plan: week of {f['week_start']}" if f.get("week_start") else "", "state": "ok" if weeks else "warn"}


def _family_tile():
    p = family.get_profile()
    m = family.get_memories()
    if not _ok(p):
        return {"title": "Family", "href": "/family", "headline": "Unknown", "sub": "", "state": "warn"}
    upcoming = sorted((x for x in p.get("people", []) if x.get("days_until_birthday") is not None),
                      key=lambda x: x["days_until_birthday"])
    if upcoming:
        n = upcoming[0]
        d = n["days_until_birthday"]
        when = "today" if d == 0 else f"in {_plural(d, 'day')}"
        headline, sub = f"{n['name'].split()[0]}'s birthday", when
    else:
        headline, sub = f"{p.get('total', 0)} people", "on file"
    if _ok(m) and m.get("total"):
        sub += f" · {_plural(m['total'], 'memory')} saved".replace("memorys", "memories")
    return {"title": "Family", "href": "/family", "headline": headline, "sub": sub, "state": "ok", "small": True}


def _hyvee_tile(systems):
    h = shopping_fitness.get_hyvee()
    login = next((s for s in systems if s.get("key") == "hyvee"), None)
    if not _ok(h):
        return {"title": "Hy-Vee", "href": "/hyvee", "headline": "Unknown", "sub": "", "state": "warn"}
    expired = bool(login and login.get("state") == "expired")
    hist = h.get("history") or {}
    headline = "Login expired" if expired else "Ready"
    sub = f"{_plural(hist.get('orders', 0), 'past order')} on file"
    return {"title": "Hy-Vee", "href": "/hyvee", "headline": headline, "sub": sub, "state": "warn" if expired else "ok"}


def get_summary():
    attention: list[dict] = []
    agent_tile = _agent_tile(attention)
    schedule_tile = _schedule_tile(attention)
    systems = _credentials(attention)
    tiles = [
        agent_tile, schedule_tile, _home_tile(attention), _habits_tile(attention), _golf_tile(), _wellness_tile(), _chores_tile(),
        _meals_tile(), _finance_tile(),
        _fitness_tile(), _family_tile(), _hyvee_tile(systems),
    ]
    dismissed = dismissals.load()
    live = [a for a in attention if a["key"] not in dismissed]
    hidden = [a for a in attention if a["key"] in dismissed]
    # A tile stops glowing red once everything flagged against it has been dismissed.
    still_flagged = {a["tile"] for a in live}
    for t in tiles:
        if t["state"] == "warn" and t["title"] not in still_flagged and any(a["tile"] == t["title"] for a in hidden):
            t["state"] = "ok"
    return {
        "status": "attention" if live else "ok",
        "attention_count": len(live),
        "attention": live[:MAX_ATTENTION],
        "all_attention": live,
        "more": max(0, len(live) - MAX_ATTENTION),
        "dismissed": hidden,
        "active_keys": [a["key"] for a in attention],
        "tiles": tiles,
    }
