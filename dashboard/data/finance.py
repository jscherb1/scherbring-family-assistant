"""Finance readers (read-only): the finance agent's tagging history and the reports/planning status.
Report text and advisor/retirement profile values stay hidden. Live Monarch numbers (budget, review
queue) come from the snapshot via data/money.py, not from here."""
from . import common

ADVISOR_FIELDS = ["filing_status", "marginal_tax_bracket", "hsa_eligible_hdhp",
                  "hsa_contribution_ytd", "retirement_contribution_ytd", "term_life_coverage",
                  "umbrella_coverage", "estate_docs", "emergency_fund_target", "risk_tolerance"]
# Non-monetary retirement assumptions that are safe to show.
RETIREMENT_SAFE_KEYS = ["retirement_age", "plan_through_age", "inflation", "ss_start_age",
                        "data_driven", "fallback_stock_pct"]
LOW_CONFIDENCE = 0.6


def _config(conn, key):
    row = conn.execute("SELECT value FROM finance_config WHERE key = ?", (key,)).fetchone()
    return common.load_json(row["value"], None) if row else None


@common.guarded
def get_tagging():
    conn = common.connect_ro()
    try:
        actions = common.rows(conn, "SELECT action, COUNT(*) AS n FROM finance_tag_log GROUP BY action")
        decisions = common.rows(conn, "SELECT COALESCE(user_decision, 'pending') AS d, COUNT(*) AS n "
                                      "FROM finance_tag_log GROUP BY d")
        # The daily review logs the same skipped transaction again each run, so count transactions.
        awaiting = conn.execute("SELECT COUNT(DISTINCT transaction_id) FROM finance_tag_log "
                                "WHERE user_decision IS NULL AND action != 'marked_reviewed'").fetchone()[0]
        by_tag = common.rows(conn, "SELECT proposed_who_tag AS tag, COUNT(*) AS n FROM finance_tag_log "
                                   "WHERE proposed_who_tag IS NOT NULL GROUP BY tag ORDER BY n DESC")
        recent = common.rows(conn, "SELECT substr(ts, 1, 10) AS day, COUNT(*) AS n FROM finance_tag_log "
                                   "GROUP BY day ORDER BY day DESC LIMIT 14")
        wmap = common.rows(conn, "SELECT who_tag_name AS tag, signal_type, COUNT(*) AS n, "
                                 "SUM(confidence < ?) AS low FROM finance_who_map "
                                 "GROUP BY who_tag_name, signal_type ORDER BY n DESC", (LOW_CONFIDENCE,))
        proposals = common.rows(conn, "SELECT created_at, merchant_name, who_tag_name, status "
                                      "FROM finance_rule_proposals ORDER BY created_at DESC LIMIT 25")
    finally:
        conn.close()
    a = {r["action"]: r["n"] for r in actions}
    d = {r["d"]: r["n"] for r in decisions}
    judged = d.get("confirmed", 0) + d.get("corrected", 0) + d.get("rejected", 0)
    return {
        "total": sum(a.values()), "actions": a, "decisions": d, "awaiting": awaiting,
        "by_tag": by_tag, "daily": recent,
        "correction_rate": round(100 * (d.get("corrected", 0) + d.get("rejected", 0)) / judged) if judged else None,
        "who_map": wmap, "who_map_total": sum(r["n"] for r in wmap),
        "who_map_low": sum(r["low"] or 0 for r in wmap), "low_threshold": LOW_CONFIDENCE,
        "proposals": proposals, "proposals_pending": sum(1 for p in proposals if p["status"] == "proposed"),
    }


@common.guarded
def get_reports():
    conn = common.connect_ro()
    try:
        reports = common.rows(conn, "SELECT created_at, period, range_start, range_end, drive_url "
                                    "FROM finance_report_log ORDER BY created_at DESC LIMIT 40")
        advisor = _config(conn, "advisor_profile")
        retirement = _config(conn, "retirement_assumptions")
        runs = common.rows(conn, "SELECT created_at, agent FROM agent_results "
                                 "WHERE agent IN ('retirement','finance-advisor','finance-reporter') "
                                 "ORDER BY created_at DESC")
    finally:
        conn.close()
    last_by_period = {}
    for r in reports:
        last_by_period.setdefault(r["period"], r)
    advisor = advisor if isinstance(advisor, dict) else {}
    filled = [f for f in ADVISOR_FIELDS if advisor.get(f) not in (None, "")]
    retirement = retirement if isinstance(retirement, dict) else {}
    last_run = {}
    for r in runs:
        last_run.setdefault(r["agent"], r["created_at"])
    return {
        "reports": reports,
        "last_by_period": {p: last_by_period[p] for p in ("weekly", "monthly", "annual") if p in last_by_period},
        "advisor_filled": filled, "advisor_missing": [f for f in ADVISOR_FIELDS if f not in filled],
        "retirement": {k: retirement[k] for k in RETIREMENT_SAFE_KEYS if k in retirement},
        "retirement_scenarios": len(retirement.get("scenarios", [])) if isinstance(retirement.get("scenarios"), list) else 0,
        "last_run": last_run,
    }
