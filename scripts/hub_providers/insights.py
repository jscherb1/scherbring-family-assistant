"""Insights: the latest written story and the strongest findings behind it."""
import json

PROVIDER = {"name": "insights", "title": "Insights"}


def collect(conn, today):
    r = conn.execute("SELECT created_at, narrative, stats_json FROM insight_reports "
                     "ORDER BY created_at DESC LIMIT 1").fetchone()
    if not r:
        return {"card": {"report": None}}
    try:
        findings = (json.loads(r["stats_json"]).get("findings") or [])[:3]
    except ValueError:
        findings = []
    text = (r["narrative"] or "").strip()
    return {"card": {"report": {"created_at": r["created_at"], "excerpt": text[:420], "findings": [
        {k: f.get(k) for k in ("metric", "label", "confidence", "r", "effect", "n") if k in f} for f in findings]}}}
