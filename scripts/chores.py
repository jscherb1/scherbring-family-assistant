"""Kid chore chart: the single shared service.

Zero-dependency (Python stdlib only), same SQLite file as state_store.py
(state/agent_results.db, schema in state/schema.sql). Streak math is NOT reimplemented here:
a chore assigned to a kid is handed to habits.habit_status() with that kid's completions as the logs.

Both front ends are thin wrappers over this module, so there is exactly one implementation of the
business rules (completion caps, money, bonuses, badges):
  - scripts/chores_store.py        the CLI the `chores` subagent runs
  - dashboard/routers/chores.py    the adult configuration UI and its /api/chores* twins
  - dashboard/routers/kids.py      the kid-facing tap-to-complete page

Every function takes an optional `db_path` (default: state/agent_results.db) and, where dates matter,
an optional `today` so tests can pin the clock. Failures raise ChoreError with a CLI-style code:
1 = not found, 2 = bad input.

Rules
  - Money is integer cents. A kid's balance is SUM(chore_ledger.amount_cents) and never goes negative:
    payouts, spends and negative adjustments that would overdraw are rejected.
  - Trust mode: a completion counts immediately. Guardrails instead of approval: no future dates, a
    daily chore can be done at most target_count times a day, a weekly chore once a day and at most
    target_count times a week, and undo only removes the latest completion (today only unless asked).
  - Streaks are per chore per kid (habit_status periods: day, or Monday-Sunday week). Weekday chores
    are weekly chores with `days`; target_count is the number of days.
  - Bonuses (perfect week, streak milestones, mystery) are idempotent via chore_ledger.ref and are
    removed together with the completion that triggered them if it is undone. The mystery bonus is
    deterministic per kid/chore/day (no re-rolling by undo + redo) and capped at one per kid per day.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import habits

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "state" / "agent_results.db"
SCHEMA_PATH = REPO_ROOT / "state" / "schema.sql"

FREQUENCIES = ("daily", "weekly")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
LEDGER_KINDS = ("earn", "bonus", "payout", "spend", "adjust")
AVATARS = ("🦄", "🦖", "🚀", "🐼", "🦊", "🐸")
COLORS = ("#ff7eb6", "#4dabf7", "#51cf66", "#ffa94d", "#b197fc", "#ffd43b")

CONFIG_DEFAULTS = {
    "perfect_week_bonus_cents": "100",
    "streak_milestones": '{"7": 50, "14": 100, "30": 250}',  # streak length (periods) -> bonus cents
    "mystery_enabled": "1",
    "mystery_chance_pct": "15",
    "mystery_max_cents": "25",
    "nudge_enabled": "1",
}

BADGES = {
    "first_chore": ("First Step", "🌟", "Did your very first chore"),
    "ten_chores": ("High Five", "🖐️", "Did 10 chores"),
    "fifty_chores": ("Chore Champ", "🏅", "Did 50 chores"),
    "streak_7": ("On Fire", "🔥", "A 7-in-a-row streak on any chore"),
    "streak_30": ("Unstoppable", "🚀", "A 30-in-a-row streak on any chore"),
    "perfect_week": ("Perfect Week", "👑", "Did every chore all week"),
    "earned_10": ("Ten Bucks", "💵", "Earned $10 in total"),
}


class ChoreError(Exception):
    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.message = message
        self.code = code


# ---------------------------------------------------------------------------
# plumbing
# ---------------------------------------------------------------------------


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _today(today: date | None) -> date:
    return today or datetime.now().date()


def _parse_date(value: str | None, field: str = "date") -> date | None:
    if value is None:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise ChoreError(f"invalid {field} {value!r}, expected YYYY-MM-DD") from None


def _clean_name(name: str | None) -> str:
    name = (name or "").strip()
    if not name:
        raise ChoreError("name is required")
    return name


def parse_money(value) -> int:
    """'1.50', '$2', 1.5 -> cents. Rejects negatives and junk (callers pass signed values through
    adjust(), not here)."""
    text = str(value).strip().lstrip("$").replace(",", "")
    try:
        cents = round(float(text) * 100)
    except ValueError:
        raise ChoreError(f"invalid amount {value!r}") from None
    if cents < 0:
        raise ChoreError("amount cannot be negative")
    return cents


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}${cents // 100}.{cents % 100:02d}"


def _check_cents(cents, field: str = "amount") -> int:
    if not isinstance(cents, int) or isinstance(cents, bool) or cents < 0:
        raise ChoreError(f"{field} must be a whole number of cents, 0 or more")
    return cents


def _check_frequency(frequency: str) -> str:
    if frequency not in FREQUENCIES:
        raise ChoreError(f"frequency must be one of {', '.join(FREQUENCIES)}")
    return frequency


def _parse_days(days) -> list[str]:
    if days is None or days == "" or days == []:
        return []
    items = days.split(",") if isinstance(days, str) else list(days)
    out: list[str] = []
    for item in items:
        key = str(item).strip().lower()[:3]
        if key not in WEEKDAYS:
            raise ChoreError(f"invalid weekday {item!r}; use {', '.join(WEEKDAYS)}")
        if key not in out:
            out.append(key)
    return sorted(out, key=WEEKDAYS.index)


def _normalize_schedule(frequency: str, target_count: int | None, days) -> tuple[int, str | None]:
    """Validate frequency/target/days together; returns (target_count, days_csv or None)."""
    _check_frequency(frequency)
    day_list = _parse_days(days)
    if day_list and frequency != "weekly":
        raise ChoreError("days only apply to weekly chores")
    if day_list:
        if target_count is not None and target_count != len(day_list):
            raise ChoreError("target_count must equal the number of days (or be omitted)")
        return len(day_list), ",".join(day_list)
    target = 1 if target_count is None else target_count
    if not isinstance(target, int) or target < 1:
        raise ChoreError("target_count must be 1 or more")
    if frequency == "weekly" and target > 7:
        raise ChoreError("a weekly chore can be done at most 7 times a week")
    return target, None


def _kid(conn: sqlite3.Connection, kid_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM chore_kids WHERE id = ?", (kid_id,)).fetchone()
    if row is None:
        raise ChoreError(f"no kid with id {kid_id}", 1)
    return row


def _chore(conn: sqlite3.Connection, chore_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM chores WHERE id = ?", (chore_id,)).fetchone()
    if row is None:
        raise ChoreError(f"no chore with id {chore_id}", 1)
    return row


def _reward(conn: sqlite3.Connection, reward_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM chore_rewards WHERE id = ?", (reward_id,)).fetchone()
    if row is None:
        raise ChoreError(f"no reward with id {reward_id}", 1)
    return row


def _resolve(rows: list[dict], ref: str, what: str) -> dict:
    ref = (ref or "").strip()
    if not ref:
        raise ChoreError(f"{what} is required")
    low = ref.lower()
    for stage in (lambda r: r["id"] == ref, lambda r: r["name"].lower() == low,
                  lambda r: low in r["name"].lower()):
        hits = [r for r in rows if stage(r)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            names = ", ".join(repr(h["name"]) for h in hits)
            raise ChoreError(f"{ref!r} matches more than one {what}: {names}")
    raise ChoreError(f"no {what} matching {ref!r}", 1)


def _resolve_table(table: str, ref: str, what: str, db_path) -> dict:
    conn = connect(db_path)
    try:
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY name").fetchall()]
    finally:
        conn.close()
    return _resolve(rows, ref, what)


def resolve_kid(ref: str, db_path=None) -> dict:
    """Find a kid by id, exact first name, or unique name fragment (case-insensitive)."""
    return _resolve_table("chore_kids", ref, "kid", db_path)


def resolve_chore(ref: str, db_path=None) -> dict:
    return _resolve_table("chores", ref, "chore", db_path)


def resolve_reward(ref: str, db_path=None) -> dict:
    return _resolve_table("chore_rewards", ref, "reward", db_path)


def _chore_dict(row) -> dict:
    d = dict(row)
    d["days"] = [x for x in (d.get("days") or "").split(",") if x]
    return d


# ---------------------------------------------------------------------------
# setup: kids, chores, assignments, rewards, goals, config
# ---------------------------------------------------------------------------


def add_kid(name: str, *, avatar: str | None = None, color: str | None = None, db_path=None) -> dict:
    name = _clean_name(name)
    now = _now_iso()
    conn = connect(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM chore_kids").fetchone()[0]
        if conn.execute("SELECT 1 FROM chore_kids WHERE name = ?", (name,)).fetchone():
            raise ChoreError(f"a kid named {name!r} already exists")
        new_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO chore_kids (id, name, avatar, color, active, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 1, ?, ?)",
            (new_id, name, avatar or AVATARS[n % len(AVATARS)], color or COLORS[n % len(COLORS)], now, now))
        conn.commit()
        return dict(_kid(conn, new_id))
    finally:
        conn.close()


_UNSET = object()


def _update(conn, table: str, row_id: str, fields: dict) -> None:
    if not fields:
        raise ChoreError("no fields to update")
    fields = {**fields}
    if table in ("chore_kids", "chores"):
        fields["updated_at"] = _now_iso()
    conn.execute(f"UPDATE {table} SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                 [*fields.values(), row_id])
    conn.commit()


def update_kid(kid_id: str, *, name=_UNSET, avatar=_UNSET, color=_UNSET, active=_UNSET, db_path=None) -> dict:
    fields: dict = {}
    if name is not _UNSET:
        fields["name"] = _clean_name(name)
    if avatar is not _UNSET:
        fields["avatar"] = avatar or None
    if color is not _UNSET:
        fields["color"] = color or None
    if active is not _UNSET:
        fields["active"] = 1 if active else 0
    conn = connect(db_path)
    try:
        _kid(conn, kid_id)
        _update(conn, "chore_kids", kid_id, fields)
        return dict(_kid(conn, kid_id))
    finally:
        conn.close()


def create_chore(name: str, frequency: str = "daily", target_count: int | None = None, *, value_cents: int = 0,
                 icon: str | None = None, description: str | None = None, days=None,
                 kids: list[str] | None = None, db_path=None) -> dict:
    """Create a chore. kids = ids or names to assign immediately (omit to assign later)."""
    name = _clean_name(name)
    target, days_csv = _normalize_schedule(frequency, target_count, days)
    _check_cents(value_cents, "value_cents")
    kid_ids = [resolve_kid(k, db_path)["id"] for k in (kids or [])]
    now = _now_iso()
    new_id = str(uuid.uuid4())
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO chores (id, name, icon, description, frequency, target_count, days, value_cents, "
            "active, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (new_id, name, icon or None, description or None, frequency, target, days_csv, value_cents, now, now))
        for kid_id in kid_ids:
            conn.execute("INSERT OR IGNORE INTO chore_assignments (chore_id, kid_id) VALUES (?, ?)",
                         (new_id, kid_id))
        conn.commit()
        return get_chore(new_id, db_path=db_path)
    finally:
        conn.close()


def update_chore(chore_id: str, *, name=_UNSET, icon=_UNSET, description=_UNSET, frequency=_UNSET,
                 target_count=_UNSET, days=_UNSET, value_cents=_UNSET, active=_UNSET, db_path=None) -> dict:
    conn = connect(db_path)
    try:
        current = _chore_dict(_chore(conn, chore_id))
        fields: dict = {}
        if name is not _UNSET:
            fields["name"] = _clean_name(name)
        if icon is not _UNSET:
            fields["icon"] = icon or None
        if description is not _UNSET:
            fields["description"] = description or None
        if value_cents is not _UNSET:
            fields["value_cents"] = _check_cents(value_cents, "value_cents")
        if active is not _UNSET:
            fields["active"] = 1 if active else 0
        if frequency is not _UNSET or target_count is not _UNSET or days is not _UNSET:
            freq = current["frequency"] if frequency is _UNSET else frequency
            if days is not _UNSET:
                day_arg, tgt = days, target_count if target_count is not _UNSET else None
            elif freq != current["frequency"]:
                day_arg, tgt = [], (None if target_count is _UNSET else target_count)  # old days no longer apply
            else:
                day_arg = current["days"]
                tgt = current["target_count"] if target_count is _UNSET else target_count
            target, days_csv = _normalize_schedule(freq, tgt, day_arg)
            fields.update({"frequency": freq, "target_count": target, "days": days_csv})
        _update(conn, "chores", chore_id, fields)
        return get_chore(chore_id, db_path=db_path)
    finally:
        conn.close()


def assign(chore_id: str, kid_id: str, *, assigned: bool = True, db_path=None) -> dict:
    conn = connect(db_path)
    try:
        _chore(conn, chore_id)
        _kid(conn, kid_id)
        if assigned:
            conn.execute("INSERT OR IGNORE INTO chore_assignments (chore_id, kid_id) VALUES (?, ?)",
                         (chore_id, kid_id))
        else:
            conn.execute("DELETE FROM chore_assignments WHERE chore_id = ? AND kid_id = ?", (chore_id, kid_id))
        conn.commit()
    finally:
        conn.close()
    return get_chore(chore_id, db_path=db_path)


def get_chore(chore_id: str, *, db_path=None) -> dict:
    conn = connect(db_path)
    try:
        d = _chore_dict(_chore(conn, chore_id))
        d["kids"] = [r["name"] for r in conn.execute(
            "SELECT k.name FROM chore_assignments a JOIN chore_kids k ON k.id = a.kid_id "
            "WHERE a.chore_id = ? ORDER BY k.name COLLATE NOCASE", (chore_id,))]
        d["value"] = money(d["value_cents"])
        return d
    finally:
        conn.close()


def list_chores(*, include_inactive: bool = False, db_path=None) -> list[dict]:
    conn = connect(db_path)
    try:
        sql = "SELECT id FROM chores" + ("" if include_inactive else " WHERE active = 1") + \
              " ORDER BY name COLLATE NOCASE"
        ids = [r["id"] for r in conn.execute(sql)]
    finally:
        conn.close()
    return [get_chore(i, db_path=db_path) for i in ids]


def list_kids(*, include_inactive: bool = False, db_path=None) -> list[dict]:
    conn = connect(db_path)
    try:
        sql = "SELECT * FROM chore_kids" + ("" if include_inactive else " WHERE active = 1") + \
              " ORDER BY created_at, name"
        return [dict(r) for r in conn.execute(sql)]
    finally:
        conn.close()


def create_reward(name: str, cost_cents: int, *, icon: str | None = None, db_path=None) -> dict:
    name = _clean_name(name)
    _check_cents(cost_cents, "cost_cents")
    if cost_cents < 1:
        raise ChoreError("cost_cents must be 1 or more")
    new_id = str(uuid.uuid4())
    conn = connect(db_path)
    try:
        conn.execute("INSERT INTO chore_rewards (id, name, icon, cost_cents, active, created_at) "
                     "VALUES (?, ?, ?, ?, 1, ?)", (new_id, name, icon or None, cost_cents, _now_iso()))
        conn.commit()
        return dict(_reward(conn, new_id))
    finally:
        conn.close()


def update_reward(reward_id: str, *, name=_UNSET, icon=_UNSET, cost_cents=_UNSET, active=_UNSET,
                  db_path=None) -> dict:
    fields: dict = {}
    if name is not _UNSET:
        fields["name"] = _clean_name(name)
    if icon is not _UNSET:
        fields["icon"] = icon or None
    if cost_cents is not _UNSET:
        fields["cost_cents"] = _check_cents(cost_cents, "cost_cents")
    if active is not _UNSET:
        fields["active"] = 1 if active else 0
    conn = connect(db_path)
    try:
        _reward(conn, reward_id)
        _update(conn, "chore_rewards", reward_id, fields)
        return dict(_reward(conn, reward_id))
    finally:
        conn.close()


def list_rewards(*, include_inactive: bool = False, db_path=None) -> list[dict]:
    conn = connect(db_path)
    try:
        sql = "SELECT * FROM chore_rewards" + ("" if include_inactive else " WHERE active = 1") + \
              " ORDER BY cost_cents, name"
        return [dict(r) for r in conn.execute(sql)]
    finally:
        conn.close()


def set_goal(kid_id: str, name: str, target_cents: int, *, db_path=None) -> dict:
    """Replace the kid's savings goal (one active goal per kid)."""
    name = _clean_name(name)
    if _check_cents(target_cents, "target_cents") < 1:
        raise ChoreError("target_cents must be 1 or more")
    conn = connect(db_path)
    try:
        _kid(conn, kid_id)
        conn.execute("UPDATE chore_goals SET active = 0 WHERE kid_id = ?", (kid_id,))
        conn.execute("INSERT INTO chore_goals (id, kid_id, name, target_cents, active, created_at) "
                     "VALUES (?, ?, ?, ?, 1, ?)", (str(uuid.uuid4()), kid_id, name, target_cents, _now_iso()))
        conn.commit()
        return _goal(conn, kid_id, _balance(conn, kid_id))
    finally:
        conn.close()


def clear_goal(kid_id: str, *, db_path=None) -> None:
    conn = connect(db_path)
    try:
        _kid(conn, kid_id)
        conn.execute("UPDATE chore_goals SET active = 0 WHERE kid_id = ?", (kid_id,))
        conn.commit()
    finally:
        conn.close()


def _goal(conn, kid_id: str, balance_cents: int) -> dict | None:
    row = conn.execute("SELECT * FROM chore_goals WHERE kid_id = ? AND active = 1 ORDER BY created_at DESC LIMIT 1",
                       (kid_id,)).fetchone()
    if row is None:
        return None
    pct = min(100, int(balance_cents * 100 / row["target_cents"])) if balance_cents > 0 else 0
    return {"id": row["id"], "name": row["name"], "target_cents": row["target_cents"],
            "target": money(row["target_cents"]), "balance_cents": max(balance_cents, 0), "pct": pct,
            "reached": balance_cents >= row["target_cents"]}


def get_config(*, db_path=None) -> dict:
    conn = connect(db_path)
    try:
        stored = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM chore_config")}
    finally:
        conn.close()
    return {**CONFIG_DEFAULTS, **stored}


def set_config(key: str, value: str, *, db_path=None) -> dict:
    if key not in CONFIG_DEFAULTS:
        raise ChoreError(f"unknown config key {key!r}; known: {', '.join(CONFIG_DEFAULTS)}")
    value = str(value)
    if key in ("mystery_enabled", "nudge_enabled") and value not in ("0", "1"):
        raise ChoreError(f"{key} must be 0 or 1")
    if key == "mystery_chance_pct" and not (value.isdigit() and 0 <= int(value) <= 100):
        raise ChoreError("mystery_chance_pct must be 0-100")
    if key in ("perfect_week_bonus_cents", "mystery_max_cents") and not value.isdigit():
        raise ChoreError(f"{key} must be a whole number of cents")
    if key == "streak_milestones":
        try:
            parsed = json.loads(value)
            assert isinstance(parsed, dict)
            {int(k): int(v) for k, v in parsed.items()}
        except (ValueError, AssertionError, TypeError):
            raise ChoreError('streak_milestones must be JSON like {"7": 50, "30": 250} (streak length -> cents)') \
                from None
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO chore_config (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, _now_iso()))
        conn.commit()
    finally:
        conn.close()
    return get_config(db_path=db_path)


# ---------------------------------------------------------------------------
# ledger and balance
# ---------------------------------------------------------------------------


def _balance(conn, kid_id: str) -> int:
    return conn.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM chore_ledger WHERE kid_id = ?",
                        (kid_id,)).fetchone()[0]


def _ledger_add(conn, kid_id: str, kind: str, cents: int, *, completion_id=None, reward_id=None, ref=None,
                note=None) -> str:
    new_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO chore_ledger (id, kid_id, kind, amount_cents, completion_id, reward_id, ref, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (new_id, kid_id, kind, cents, completion_id, reward_id, ref, note, _now_iso()))
    return new_id


def balance(kid_id: str, *, db_path=None) -> int:
    conn = connect(db_path)
    try:
        _kid(conn, kid_id)
        return _balance(conn, kid_id)
    finally:
        conn.close()


def ledger(kid_id: str, *, limit: int | None = 50, db_path=None) -> list[dict]:
    conn = connect(db_path)
    try:
        _kid(conn, kid_id)
        sql = "SELECT * FROM chore_ledger WHERE kid_id = ? ORDER BY created_at DESC, rowid DESC"
        params: list = [kid_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()
    for r in rows:
        r["amount"] = money(r["amount_cents"])
    return rows


def payout(kid_id: str, cents: int, *, note: str | None = None, db_path=None) -> dict:
    """Parents record real money handed over. Rejects more than the balance."""
    if _check_cents(cents) < 1:
        raise ChoreError("payout must be more than $0")
    conn = connect(db_path)
    try:
        kid = _kid(conn, kid_id)
        bal = _balance(conn, kid_id)
        if cents > bal:
            raise ChoreError(f"{kid['name']} only has {money(bal)}; cannot pay out {money(cents)}")
        _ledger_add(conn, kid_id, "payout", -cents, note=note)
        conn.commit()
        return {"kid": kid["name"], "paid_cents": cents, "balance_cents": _balance(conn, kid_id),
                "balance": money(_balance(conn, kid_id))}
    finally:
        conn.close()


def adjust(kid_id: str, cents: int, *, note: str | None = None, db_path=None) -> dict:
    """Signed manual correction (+ gift / - fix). Cannot push the balance below zero."""
    if not isinstance(cents, int) or isinstance(cents, bool) or cents == 0:
        raise ChoreError("adjustment must be a non-zero whole number of cents")
    conn = connect(db_path)
    try:
        kid = _kid(conn, kid_id)
        if _balance(conn, kid_id) + cents < 0:
            raise ChoreError(f"that would put {kid['name']} below $0.00")
        _ledger_add(conn, kid_id, "adjust", cents, note=note)
        conn.commit()
        bal = _balance(conn, kid_id)
        return {"kid": kid["name"], "adjusted_cents": cents, "balance_cents": bal, "balance": money(bal)}
    finally:
        conn.close()


def redeem(kid_id: str, reward_id: str, *, db_path=None) -> dict:
    conn = connect(db_path)
    try:
        kid = _kid(conn, kid_id)
        reward = _reward(conn, reward_id)
        if not reward["active"]:
            raise ChoreError(f"reward {reward['name']!r} is not available")
        bal = _balance(conn, kid_id)
        if reward["cost_cents"] > bal:
            raise ChoreError(f"{kid['name']} has {money(bal)}; {reward['name']} costs {money(reward['cost_cents'])}")
        _ledger_add(conn, kid_id, "spend", -reward["cost_cents"], reward_id=reward_id, note=reward["name"])
        conn.commit()
        bal = _balance(conn, kid_id)
        return {"kid": kid["name"], "reward": reward["name"], "spent_cents": reward["cost_cents"],
                "balance_cents": bal, "balance": money(bal)}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# chore schedule + streaks (streak math is habits.habit_status)
# ---------------------------------------------------------------------------


def _due_on(chore: dict, day: date) -> bool:
    """Daily: every day. Weekly with days: those weekdays. Weekly without days: flexible, due all week."""
    if chore["frequency"] == "daily" or not chore["days"]:
        return True
    return WEEKDAYS[day.weekday()] in chore["days"]


def _assigned_chores(conn, kid_id: str) -> list[dict]:
    return [_chore_dict(r) for r in conn.execute(
        "SELECT c.* FROM chores c JOIN chore_assignments a ON a.chore_id = c.id "
        "WHERE a.kid_id = ? AND c.active = 1 ORDER BY c.created_at, c.name", (kid_id,))]


def _completions(conn, kid_id: str, chore_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT done_date AS log_date, count FROM chore_completions WHERE kid_id = ? AND chore_id = ? "
        "ORDER BY done_date", (kid_id, chore_id))]


def _chore_status(conn, kid_id: str, chore: dict, today: date) -> dict:
    return habits.habit_status(
        {"id": chore["id"], "name": chore["name"], "frequency": chore["frequency"],
         "target_count": chore["target_count"], "description": chore.get("description")},
        _completions(conn, kid_id, chore["id"]), today)


def _count_on(logs: list[dict], day: date) -> int:
    return sum(lg["count"] for lg in logs if lg["log_date"] == day.isoformat())


def _week_days(today: date) -> list[date]:
    start = habits.period_start("weekly", today)
    return [start + timedelta(days=i) for i in range(7)]


def _item(conn, kid_id: str, chore: dict, today: date) -> dict | None:
    """One to-do row for today, or None when the chore isn't relevant today."""
    logs = _completions(conn, kid_id, chore["id"])
    st = habits.habit_status(
        {"id": chore["id"], "name": chore["name"], "frequency": chore["frequency"],
         "target_count": chore["target_count"]}, logs, today)
    done_today = _count_on(logs, today)
    period_met = st["done_this_period"] >= chore["target_count"]
    if chore["frequency"] == "daily":
        done = period_met
    else:
        done = done_today >= 1 or period_met
    due = _due_on(chore, today)
    if not (due or done_today):
        return None
    if chore["frequency"] == "weekly" and period_met and not done_today:
        return None  # weekly goal already met on an earlier day
    can_do = (not period_met) and (chore["frequency"] == "daily" or done_today < 1)
    return {
        "chore_id": chore["id"], "name": chore["name"], "icon": chore["icon"] or "⭐",
        "description": chore["description"], "frequency": chore["frequency"],
        "target_count": chore["target_count"], "days": chore["days"],
        "value_cents": chore["value_cents"], "value": money(chore["value_cents"]),
        "done_today": done_today, "done_this_period": st["done_this_period"], "done": done,
        "can_complete": can_do, "can_undo": done_today > 0,
        "current_streak": st["current_streak"], "longest_streak": st["longest_streak"],
        "at_risk": st["at_risk"], "period_unit": "day" if chore["frequency"] == "daily" else "week",
    }


def _week_strip(conn, kid_id: str, chores: list[dict], today: date) -> list[dict]:
    days = []
    all_logs = {c["id"]: _completions(conn, kid_id, c["id"]) for c in chores}
    for d in _week_days(today):
        due = [c for c in chores if _due_on(c, d) and (c["frequency"] == "daily" or c["days"])]
        done = [c for c in due if _count_on(all_logs[c["id"]], d) >= 1]
        any_done = any(_count_on(all_logs[c["id"]], d) for c in chores)
        days.append({"date": d.isoformat(), "weekday": WEEKDAYS[d.weekday()].title(), "is_today": d == today,
                     "future": d > today, "any_done": any_done,
                     "perfect": bool(due) and len(done) == len(due)})
    return days


def _is_perfect_week(conn, kid_id: str, chores: list[dict], today: date) -> bool:
    if not chores:
        return False
    for c in chores:
        st = habits.habit_status({"id": c["id"], "name": c["name"], "frequency": c["frequency"],
                                  "target_count": c["target_count"]}, _completions(conn, kid_id, c["id"]), today)
        if c["frequency"] == "daily":
            week = _week_days(today)
            logs = _completions(conn, kid_id, c["id"])
            if any(_count_on(logs, d) < c["target_count"] for d in week):
                return False
        elif st["done_this_period"] < c["target_count"]:
            return False
    return True


# ---------------------------------------------------------------------------
# kid actions: complete / undo (+ bonuses, badges)
# ---------------------------------------------------------------------------


def _config_in(conn) -> dict:
    stored = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM chore_config")}
    return {**CONFIG_DEFAULTS, **stored}


def _total_completions(conn, kid_id: str) -> int:
    return conn.execute("SELECT COALESCE(SUM(count), 0) FROM chore_completions WHERE kid_id = ?",
                        (kid_id,)).fetchone()[0]


def _earned_keys(conn, kid_id: str, today: date) -> set[str]:
    total = _total_completions(conn, kid_id)
    earned_total = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM chore_ledger WHERE kid_id = ? AND kind IN ('earn', 'bonus')",
        (kid_id,)).fetchone()[0]
    best = 0
    for c in _assigned_chores(conn, kid_id):
        st = _chore_status(conn, kid_id, c, today)
        best = max(best, st["longest_streak"] or 0)
    perfect = conn.execute("SELECT 1 FROM chore_ledger WHERE kid_id = ? AND ref LIKE 'perfect_week:%' LIMIT 1",
                           (kid_id,)).fetchone()
    keys = set()
    if total >= 1:
        keys.add("first_chore")
    if total >= 10:
        keys.add("ten_chores")
    if total >= 50:
        keys.add("fifty_chores")
    if best >= 7:
        keys.add("streak_7")
    if best >= 30:
        keys.add("streak_30")
    if perfect:
        keys.add("perfect_week")
    if earned_total >= 1000:
        keys.add("earned_10")
    return keys


def _badge_info(key: str, earned: bool = True) -> dict:
    label, icon, desc = BADGES[key]
    return {"key": key, "label": label, "icon": icon, "description": desc, "earned": earned}


def badges(kid_id: str, *, db_path=None, today: date | None = None) -> list[dict]:
    conn = connect(db_path)
    try:
        _kid(conn, kid_id)
        have = _earned_keys(conn, kid_id, _today(today))
    finally:
        conn.close()
    return [_badge_info(k, k in have) for k in BADGES]


def _roll(kid_id: str, chore_id: str, day: date) -> tuple[int, int]:
    """Deterministic 0-99 roll plus a 0-255 seed for the bonus size (no re-rolling via undo)."""
    digest = hashlib.sha256(f"{kid_id}:{chore_id}:{day.isoformat()}".encode()).digest()
    return digest[0] % 100, digest[1]


def _award(conn, kid_id: str, cents: int, ref: str, label: str, completion_id: str, bonuses: list) -> None:
    if cents <= 0 or conn.execute("SELECT 1 FROM chore_ledger WHERE kid_id = ? AND ref = ?",
                                  (kid_id, ref)).fetchone():
        return
    _ledger_add(conn, kid_id, "bonus", cents, completion_id=completion_id, ref=ref, note=label)
    bonuses.append({"kind": ref.split(":")[0], "cents": cents, "amount": money(cents), "label": label})


def complete(kid_id: str, chore_id: str, *, log_date: str | None = None, db_path=None,
             today: date | None = None) -> dict:
    """Record one completion and pay for it. Kid routes call this with no log_date (= today). Past
    dates (adult backfill) earn base pay only; bonuses are for same-day completions."""
    today_d = _today(today)
    day = _parse_date(log_date, "log_date") or today_d
    if day > today_d:
        raise ChoreError(f"log_date {day.isoformat()} is in the future")
    conn = connect(db_path)
    try:
        kid = _kid(conn, kid_id)
        chore = _chore_dict(_chore(conn, chore_id))
        if not kid["active"] or not chore["active"]:
            raise ChoreError("that kid or chore is paused")
        if not conn.execute("SELECT 1 FROM chore_assignments WHERE chore_id = ? AND kid_id = ?",
                            (chore_id, kid_id)).fetchone():
            raise ChoreError(f"{chore['name']!r} is not assigned to {kid['name']}")
        logs = _completions(conn, kid_id, chore_id)
        freq = chore["frequency"]
        pstart = habits.period_start(freq, day)
        pend = habits.period_end(freq, pstart)
        in_period = sum(lg["count"] for lg in logs
                        if pstart <= datetime.strptime(lg["log_date"], "%Y-%m-%d").date() <= pend)
        if in_period >= chore["target_count"]:
            raise ChoreError(f"{chore['name']!r} is already done for this {'day' if freq == 'daily' else 'week'}")
        if freq == "weekly" and _count_on(logs, day) >= 1:
            raise ChoreError(f"{chore['name']!r} is already done for {day.isoformat()}")

        badges_before = _earned_keys(conn, kid_id, today_d)
        comp_id = str(uuid.uuid4())
        conn.execute("INSERT INTO chore_completions (id, chore_id, kid_id, done_date, count, created_at) "
                     "VALUES (?, ?, ?, ?, 1, ?)", (comp_id, chore_id, kid_id, day.isoformat(), _now_iso()))
        if chore["value_cents"] > 0:
            _ledger_add(conn, kid_id, "earn", chore["value_cents"], completion_id=comp_id, note=chore["name"])

        bonuses: list[dict] = []
        status = _chore_status(conn, kid_id, chore, today_d)
        if day == today_d:
            cfg = _config_in(conn)
            if status["done_this_period"] >= chore["target_count"]:
                milestones = {int(k): int(v) for k, v in json.loads(cfg["streak_milestones"]).items()}
                streak = status["current_streak"]
                if streak in milestones:
                    unit = "day" if freq == "daily" else "week"
                    _award(conn, kid_id, milestones[streak], f"streak:{chore_id}:{streak}:{pstart.isoformat()}",
                           f"{streak}-{unit} streak on {chore['name']}!", comp_id, bonuses)
            if cfg["mystery_enabled"] == "1":
                roll, seed = _roll(kid_id, chore_id, day)
                max_cents = int(cfg["mystery_max_cents"])
                if roll < int(cfg["mystery_chance_pct"]) and max_cents >= 5:
                    steps = max(1, max_cents // 5)
                    _award(conn, kid_id, 5 * (1 + seed % steps), f"mystery:{day.isoformat()}",
                           "Mystery bonus!", comp_id, bonuses)
            chores = _assigned_chores(conn, kid_id)
            if _is_perfect_week(conn, kid_id, chores, today_d):
                _award(conn, kid_id, int(cfg["perfect_week_bonus_cents"]),
                       f"perfect_week:{habits.period_start('weekly', today_d).isoformat()}",
                       "Perfect week!", comp_id, bonuses)

        new_badges = [_badge_info(k) for k in BADGES if k in _earned_keys(conn, kid_id, today_d) - badges_before]
        conn.commit()
        bal = _balance(conn, kid_id)
        items = [i for i in (_item(conn, kid_id, c, today_d) for c in _assigned_chores(conn, kid_id)) if i]
        all_done = day == today_d and bool(items) and all(i["done"] for i in items)
        return {"kid": kid["name"], "chore": chore["name"], "completion_id": comp_id,
                "earned_cents": chore["value_cents"], "earned": money(chore["value_cents"]),
                "bonuses": bonuses, "bonus_cents": sum(b["cents"] for b in bonuses),
                "new_badges": new_badges, "all_done": all_done, "balance_cents": bal, "balance": money(bal),
                "status": status}
    finally:
        conn.close()


def undo(kid_id: str, chore_id: str, *, any_date: bool = False, db_path=None, today: date | None = None) -> dict:
    """Remove the most recently entered completion of this chore (a mis-tap fix), along with the money
    it earned. Only today's completions unless any_date (adult). Refuses if the money was already paid
    out - use adjust() to correct that."""
    today_d = _today(today)
    conn = connect(db_path)
    try:
        kid = _kid(conn, kid_id)
        chore = _chore_dict(_chore(conn, chore_id))
        sql = ("SELECT * FROM chore_completions WHERE kid_id = ? AND chore_id = ?"
               + ("" if any_date else " AND done_date = ?") + " ORDER BY created_at DESC, rowid DESC LIMIT 1")
        params = [kid_id, chore_id] + ([] if any_date else [today_d.isoformat()])
        row = conn.execute(sql, params).fetchone()
        if row is None:
            raise ChoreError(f"nothing to undo for {chore['name']!r}" + ("" if any_date else " today"), 1)
        removing = conn.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM chore_ledger WHERE completion_id = ?",
                                (row["id"],)).fetchone()[0]
        if _balance(conn, kid_id) - removing < 0:
            raise ChoreError(f"{kid['name']} was already paid for this; use an adjustment instead")
        conn.execute("DELETE FROM chore_ledger WHERE completion_id = ?", (row["id"],))
        conn.execute("DELETE FROM chore_completions WHERE id = ?", (row["id"],))
        conn.commit()
        bal = _balance(conn, kid_id)
        return {"kid": kid["name"], "chore": chore["name"], "removed_date": row["done_date"],
                "removed_cents": removing, "balance_cents": bal, "balance": money(bal),
                "status": _chore_status(conn, kid_id, chore, today_d)}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def kid_view(kid_id: str, *, db_path=None, today: date | None = None) -> dict:
    """Everything the kid page and the agent need for one kid: today's to-do list, progress, week strip,
    balance, goal, rewards the balance can buy, badges."""
    today_d = _today(today)
    conn = connect(db_path)
    try:
        kid = dict(_kid(conn, kid_id))
        chores = _assigned_chores(conn, kid_id)
        items = [i for i in (_item(conn, kid_id, c, today_d) for c in chores) if i]
        done = sum(1 for i in items if i["done"])
        bal = _balance(conn, kid_id)
        earned_today = conn.execute(
            "SELECT COALESCE(SUM(l.amount_cents), 0) FROM chore_ledger l JOIN chore_completions c "
            "ON c.id = l.completion_id WHERE l.kid_id = ? AND c.done_date = ?",
            (kid_id, today_d.isoformat())).fetchone()[0]
        have = _earned_keys(conn, kid_id, today_d)
        rewards = [dict(r, affordable=r["cost_cents"] <= bal, cost=money(r["cost_cents"]))
                   for r in conn.execute("SELECT * FROM chore_rewards WHERE active = 1 ORDER BY cost_cents, name")]
        week = _week_strip(conn, kid_id, chores, today_d)
        return {
            "kid": kid, "date": today_d.isoformat(), "todo": items,
            "done_count": done, "total_count": len(items),
            "progress_pct": int(done * 100 / len(items)) if items else 0,
            "all_done": bool(items) and done == len(items),
            "best_streak": max([i["current_streak"] or 0 for i in items], default=0),
            "balance_cents": bal, "balance": money(bal),
            "earned_today_cents": earned_today, "earned_today": money(earned_today),
            "week": week, "goal": _goal(conn, kid_id, bal), "rewards": rewards,
            "badges": [_badge_info(k, k in have) for k in BADGES],
            "perfect_days_this_week": sum(1 for d in week if d["perfect"]),
        }
    finally:
        conn.close()


def all_kid_views(*, db_path=None, today: date | None = None) -> list[dict]:
    return [kid_view(k["id"], db_path=db_path, today=today) for k in list_kids(db_path=db_path)]


def week_summary(*, db_path=None, today: date | None = None) -> list[dict]:
    """Payday numbers per active kid: what's owed now, what was earned this week, streaks."""
    today_d = _today(today)
    monday = habits.period_start("weekly", today_d).isoformat()
    out = []
    conn = connect(db_path)
    try:
        for k in conn.execute("SELECT * FROM chore_kids WHERE active = 1 ORDER BY created_at, name").fetchall():
            bal = _balance(conn, k["id"])
            earned_week = conn.execute(
                "SELECT COALESCE(SUM(amount_cents), 0) FROM chore_ledger WHERE kid_id = ? "
                "AND kind IN ('earn', 'bonus') AND substr(created_at, 1, 10) >= ?",
                (k["id"], monday)).fetchone()[0]
            week_done = conn.execute(
                "SELECT COALESCE(SUM(count), 0) FROM chore_completions WHERE kid_id = ? AND done_date >= ?",
                (k["id"], monday)).fetchone()[0]
            chores = _assigned_chores(conn, k["id"])
            streaks = sorted(((s["current_streak"] or 0, c["name"]) for c in chores
                              for s in [_chore_status(conn, k["id"], c, today_d)]), reverse=True)
            out.append({
                "kid": k["name"], "kid_id": k["id"], "owed_cents": bal, "owed": money(bal),
                "earned_this_week_cents": earned_week, "earned_this_week": money(earned_week),
                "chores_done_this_week": week_done,
                "perfect_week": _is_perfect_week(conn, k["id"], chores, today_d),
                "best_streak": ({"chore": streaks[0][1], "length": streaks[0][0]} if streaks and streaks[0][0] else None),
            })
    finally:
        conn.close()
    return out


def open_today(*, db_path=None, today: date | None = None) -> list[dict]:
    """Kids with chores still undone today (for the optional afternoon nudge to the adults). Empty
    when the nudge is off or everyone is done."""
    if get_config(db_path=db_path)["nudge_enabled"] != "1":
        return []
    out = []
    for v in all_kid_views(db_path=db_path, today=today):
        todo = [i["name"] for i in v["todo"] if not i["done"]]
        if todo:
            out.append({"kid": v["kid"]["name"], "remaining": todo, "done_count": v["done_count"],
                        "total_count": v["total_count"]})
    return out
