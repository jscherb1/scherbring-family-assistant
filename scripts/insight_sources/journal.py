"""Source: the daily yes/no journal (journal:<slug>), answered days only.

Unanswered days are left out rather than stored as 0, so "didn't answer" never counts as "no".
"""
SOURCE = {"name": "journal", "kind": "manual",
          "description": "Daily yes/no journal answers from the nightly check-in (alcohol, late caffeine, screens, ...)"}


def collect(conn, start, end, today):
    rows = conn.execute("SELECT q.slug, a.answer_date d, a.value v FROM journal_answers a "
                        "JOIN journal_questions q ON q.id = a.question_id WHERE a.answer_date BETWEEN ? AND ?",
                        (start.isoformat(), end.isoformat()))
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        out.setdefault(f"journal:{r['slug']}", {})[r["d"]] = float(r["v"])
    return out
