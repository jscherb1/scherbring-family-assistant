"""Fast lane: answer short, clear commands without a full agent turn (stdlib only).

Why: a simple command ("add milk to the Costco list", "turn off the porch lights", "log stretching") takes the full
agent about 19 s although the action itself takes under a second. The fast lane runs the same services the agent and
the dashboard use (habits, dayrating, chores, todoist_api, ha, myhub) straight from the message, and anything it is
not sure about goes on to the agent untouched.

Shape (everything is in this module except the Haiku call, which is scripts/fastlane_model.py):
  - ACTIONS            registry of everything the fast lane can do; each action validates, then runs a service
  - tier0(text, ctx)   strict regex rules: only clear, complete commands match, anything else is None
  - check_model_output validates a Tier 1 (Haiku) JSON answer against the registry; the model can never reach an
                       action that is not in ACTIONS and every name or text it returns must appear in the message
  - guards             eligibility (attachments, replies, retries, dev chat, a pending question...), the text
                       vetoes (garage opening, locks, alarm), and the ordering rule

Safety rules that are enforced here, in code, after any model:
  - the only Home Assistant writes are ha.fastlane_act on the fast-lane allow-list: porch lights, thermostat setpoint,
    CLOSE a garage door. Opening a garage, locks and the alarm are never handled here (see `unsafe_text`).
  - default deny: an entity or action not in the registry or the allow-list is passed to the agent.
  - dry_run never writes and never calls the network.

Errors: `PassUp` means "decline, let the agent handle it" (nothing was changed). Anything the user should be told
(already logged, chore already done) is a normal Reply.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chores  # noqa: E402
import dayrating  # noqa: E402
import ha  # noqa: E402
import habits  # noqa: E402
import myhub  # noqa: E402
import todoist_api  # noqa: E402
from paths import dev_chat_ids  # noqa: E402

MAX_TEXT = 300            # longer messages are conversation, not commands
MAX_ITEMS = 8
CONFIDENCE_MIN = 0.85     # Tier 1 answers below this go to the agent
MAX_AGE_S = 8.0           # a row older than this is already on its way to the agent (claim_next takes it at 10 s)
CHANNELS = ("telegram", "terminal")
SNAPSHOT_STALE_H = 4      # calendar / task snapshot older than this is announced with a caveat


class PassUp(Exception):
    """Decline: the agent should handle this message. Nothing was changed."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Reply:
    text: str
    wrote: bool = False


@dataclass
class Match:
    action: str
    fields: dict
    tier: int = 0
    confidence: float = 1.0
    check_text: str | None = None   # the part of the message to test for back-references (default: all of it)
    cost_usd: float | None = None


# ---------------------------------------------------------------------------
# text helpers
# ---------------------------------------------------------------------------

_WD = r"(?:mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:s(?:day)?)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)"
DAY = rf"(?:today|yesterday|(?:last )?{_WD}|\d{{1,2}}/\d{{1,2}}|\d{{4}}-\d{{2}}-\d{{2}})"
_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_POLITE = re.compile(
    r"^(?:(?:hey|hi|ok|okay|alright|so)[,.!]?\s+)?"
    r"(?:(?:can|could|would|will) you\s+(?:please\s+)?|please\s+|go ahead and\s+|i need you to\s+|i want you to\s+)?", re.I)
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def _fix(text: str) -> str:
    return (text or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')


def strip_polite(text: str) -> str:
    """Drop a leading "can you / please / go ahead and" and surrounding whitespace; the rest is untouched."""
    text = " ".join(_fix(text).split()) if "\n" not in text else _fix(text).strip()
    return _POLITE.sub("", text, count=1).strip()


def clean(text: str) -> str:
    """strip_polite plus trailing "please" and ?.! (for commands without free text)."""
    t = strip_polite(text)
    t = re.sub(r"[\s?.!]+$", "", t)
    t = re.sub(r"[,\s]+please$", "", t, flags=re.I)
    return re.sub(r"[\s?.!]+$", "", t).strip()


def parse_day(value, today: date) -> date:
    """today | yesterday | a weekday (the most recent one, today counts; "last X" is strictly before today) |
    M/D | YYYY-MM-DD, all in local time. Anything unparseable or in the future raises PassUp."""
    v = str(value).strip().lower() if value not in (None, "") else "today"
    if v == "today":
        return today
    if v == "yesterday":
        return today - timedelta(days=1)
    m = re.fullmatch(rf"(last )?({_WD})", v)
    if m:
        want = _WEEKDAYS.index(m.group(2)[:3])
        back = (today.weekday() - want) % 7
        if m.group(1) and back == 0:
            back = 7
        return today - timedelta(days=back)
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})", v)
    try:
        if m:
            d = date(today.year, int(m.group(1)), int(m.group(2)))
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            d = date.fromisoformat(v)
        else:
            raise PassUp("date_unparsed")
    except ValueError:
        raise PassUp("date_invalid") from None
    if d > today:
        raise PassUp("date_future")
    return d


def day_label(day: date, today: date) -> str:
    if day == today:
        return "today"
    if day == today - timedelta(days=1):
        return "yesterday"
    return day.strftime("%a %b ") + str(day.day)


_STOP = {"the", "a", "an", "my", "our", "her", "his", "their", "your", "of", "for", "to", "habit", "chore", "and"}


def _stem(t: str) -> str:
    return t[:-1] if len(t) > 3 and t.endswith("s") and not t.endswith("ss") else t


def _toks(s: str) -> list[str]:
    return [_stem(t) for t in re.findall(r"[a-z0-9]+", _fix(s).lower().replace("'", ""))]


def match_entry(ref: str, entries: list[dict]) -> dict | None:
    """The one entry (habit, kid, chore) a spoken name refers to: same words, or every word of `ref` is a word of
    exactly one entry's name. Ambiguity or too little to go on is None."""
    rt = [t for t in _toks(ref) if t not in _STOP]
    if not rt or sum(len(t) for t in rt) < 3:
        return None
    exact = [e for e in entries if sorted(t for t in _toks(e["name"]) if t not in _STOP) == sorted(rt)]
    if len(exact) == 1:
        return exact[0]
    hits = [e for e in entries if set(rt) <= set(_toks(e["name"]))]
    return hits[0] if len(hits) == 1 else None


def mentions(text: str, name: str) -> bool:
    """True when at least one significant word of `name` appears in `text` (provenance check for model output)."""
    have = _toks(text)
    return any(_same_word(t, h) for t in _toks(name) if t not in _STOP and len(t) >= 3 for h in have)


def _same_word(a: str, b: str) -> bool:
    """Equal, or sharing a 5+ letter start ("stretched" ~ "stretching")."""
    return a == b or (len(a) >= 5 and len(b) >= 5 and a[:5] == b[:5])


# ---------------------------------------------------------------------------
# context: names the matcher and the model need
# ---------------------------------------------------------------------------


@dataclass
class Context:
    today: date
    habits: list[dict] = field(default_factory=list)   # active only
    kids: list[dict] = field(default_factory=list)
    chores: list[dict] = field(default_factory=list)
    db_path: Path | str | None = None

    @classmethod
    def load(cls, db_path: Path | str | None = None, today: date | None = None) -> "Context":
        """Names of active habits, kids and chores, read with a read-only connection (no schema, no writes).
        A missing database or table just means no names."""
        path = Path(db_path) if db_path else habits.DB_PATH
        ctx = cls(today=today or datetime.now().date(), db_path=db_path)
        if not path.exists():
            return ctx
        try:
            conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error:
            return ctx
        try:
            for attr, table in (("habits", "habits"), ("kids", "chore_kids"), ("chores", "chores")):
                try:
                    rows = conn.execute(f"SELECT id, name FROM {table} WHERE active = 1 ORDER BY name").fetchall()
                except sqlite3.Error:
                    rows = []
                setattr(ctx, attr, [{"id": r[0], "name": r[1]} for r in rows])
        finally:
            conn.close()
        return ctx


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Action:
    name: str
    summary: str                                   # one line, shown to the model
    fields: dict                                   # field -> description, shown to the model
    check: Callable[[dict, str, Context], dict]    # (fields, message text, ctx) -> canonical fields, or PassUp
    run: Callable[[dict, Context, bool], Reply]    # (canonical fields, ctx, dry_run) -> Reply, or PassUp
    keywords: Callable[[dict], list]               # lowercase words that tie this action to earlier messages
    writes: bool = False


def _str(v, what: str) -> str:
    if not isinstance(v, str) or not v.strip():
        raise PassUp(f"{what}_missing")
    return v.strip()


def _day_field(fields: dict, ctx: Context) -> str | None:
    """Canonical ISO day, or None for today. Future or unparseable days pass up."""
    raw = fields.get("day")
    if raw in (None, "", "today"):
        return None
    return parse_day(raw, ctx.today).isoformat()


def _need_in_text(name: str, text: str, what: str) -> None:
    if not mentions(text, name):
        raise PassUp(f"{what}_not_in_text")


def _day_of(fields: dict, ctx: Context) -> date:
    return date.fromisoformat(fields["day"]) if fields.get("day") else ctx.today


# ---- habits ----

def _check_habit(fields, text, ctx):
    h = match_entry(_str(fields.get("habit"), "habit"), ctx.habits)
    if h is None:
        raise PassUp("habit_unresolved")
    _need_in_text(h["name"], text, "habit")
    return {"habit": h["name"], "day": _day_field(fields, ctx)}


def _habit_log(f, ctx, dry):
    if dry:
        return Reply(f"Would log {f['habit']} for {day_label(_day_of(f, ctx), ctx.today)}.")
    try:
        h = habits.resolve_habit(f["habit"], ctx.db_path)
    except habits.HabitError:
        raise PassUp("habit_unresolved") from None
    if not h.get("active", 1):
        raise PassUp("habit_paused")
    day = _day_of(f, ctx)
    label = day_label(day, ctx.today)
    counter = h.get("target_count") is None   # a "just count it" habit: several a day are legitimate
    if not counter and any(lg["log_date"] == day.isoformat() for lg in habits.list_logs(h["id"], limit=None, db_path=ctx.db_path)):
        return Reply(f"{h['name']} is already logged for {label}, so I didn't add another.")
    res = habits.log_completion(h["id"], log_date=day.isoformat(), db_path=ctx.db_path, today=ctx.today)
    st = res["status"]
    extra = ""
    if st.get("has_goal"):
        unit = "day" if st["frequency"] == "daily" else "week"
        if st.get("current_streak"):
            extra = f" Streak: {st['current_streak']} {unit}{'s' if st['current_streak'] != 1 else ''}."
        if st["frequency"] == "weekly":
            extra += f" {st['done_this_period']}/{st['target_count']} this week."
    elif st.get("count_this_week") is not None:
        extra = f" {st['count_this_week']} this week."
    return Reply(f"✅ Logged {h['name']} for {label}.{extra}", wrote=True)


def _check_habit_status(fields, text, ctx):
    ref = fields.get("habit")
    if ref in (None, ""):
        return {"habit": None}
    return {"habit": _check_habit({"habit": ref}, text, ctx)["habit"]}


def _habit_status(f, ctx, dry):
    if dry:
        return Reply(f"Would read the status of {f['habit']}." if f.get("habit") else "Would read the status of every habit.")
    if f.get("habit"):
        try:
            h = habits.resolve_habit(f["habit"], ctx.db_path)
        except habits.HabitError:
            raise PassUp("habit_unresolved") from None
        return Reply(_status_line(habits.get_status(h["id"], db_path=ctx.db_path, today=ctx.today)))
    rows = habits.list_status(db_path=ctx.db_path, today=ctx.today)
    if not rows:
        raise PassUp("no_habits")
    return Reply("\n".join(_status_line(r) for r in rows))


def _status_line(st: dict) -> str:
    if st.get("has_goal"):
        unit = "day" if st["frequency"] == "daily" else "week"
        base = (f"{st['name']}: {st['done_this_period']}/{st['target_count']} this {unit}, streak "
                f"{st['current_streak']} (best {st['longest_streak']})")
        return base + (", at risk" if st.get("at_risk") else "")
    return f"{st['name']}: {st.get('count_this_week', 0)} this week, {st.get('count_last_30_days', 0)} in 30 days"


# ---- day rating ----

def _check_rating(fields, text, ctx):
    raw = fields.get("score")
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise PassUp("score_invalid")
    score = _NUMBER_WORDS.get(raw.strip().lower()) if isinstance(raw, str) and not raw.strip().isdigit() else None
    if score is None:
        try:
            score = int(raw)
        except (TypeError, ValueError):
            raise PassUp("score_invalid") from None
    if not dayrating.MIN_SCORE <= score <= dayrating.MAX_SCORE:
        raise PassUp("score_range")
    low = text.lower()
    if not re.search(rf"(?<!\d){score}(?!\d)", low) and not any(w in low for w, n in _NUMBER_WORDS.items() if n == score):
        raise PassUp("score_not_in_text")
    note = fields.get("note")
    if note is not None:
        if not isinstance(note, str):
            raise PassUp("note_invalid")
        note = note.strip() or None
    if note and (note not in text or len(note) > dayrating.NOTE_MAX):   # the note must be the user's own words
        raise PassUp("note_not_verbatim")
    return {"score": score, "note": note, "day": _day_field(fields, ctx)}


def _rating_set(f, ctx, dry):
    day = _day_of(f, ctx)
    label = day_label(day, ctx.today)
    if dry:
        return Reply(f"Would rate {label} {f['score']}/10" + (f" with the note {f['note']!r}." if f.get("note") else "."))
    before = dayrating.get_rating(day.isoformat(), db_path=ctx.db_path, today=ctx.today)
    if before:
        if before["score"] == f["score"] and (not f.get("note") or f["note"] == before.get("note")):
            return Reply(f"{label.capitalize()} is already rated {before['score']}/10, so I left it.")
        raise PassUp("rating_exists")    # a different score or note: changing a rating is for the agent
    try:
        r = dayrating.set_rating(f["score"], f.get("note"), rating_date=day.isoformat(), db_path=ctx.db_path, today=ctx.today)
    except dayrating.RatingError:
        raise PassUp("rating_rejected") from None
    out = f"✅ Rated {label} {r['score']}/10."
    if r.get("note"):
        out += f" Note: {r['note']}"
    return Reply(out, wrote=True)


# ---- chores ----

def _check_chore(fields, text, ctx):
    kid = match_entry(_str(fields.get("kid"), "kid"), ctx.kids)
    chore = match_entry(_str(fields.get("chore"), "chore"), ctx.chores)
    if kid is None or chore is None:
        raise PassUp("kid_or_chore_unresolved")
    _need_in_text(kid["name"], text, "kid")
    _need_in_text(chore["name"], text, "chore")
    return {"kid": kid["name"], "chore": chore["name"], "day": _day_field(fields, ctx)}


def _chore_done(f, ctx, dry):
    if dry:
        return Reply(f"Would mark {f['chore']} done for {f['kid']} ({day_label(_day_of(f, ctx), ctx.today)}).")
    try:
        kid = chores.resolve_kid(f["kid"], ctx.db_path)
        chore = chores.resolve_chore(f["chore"], ctx.db_path)
    except chores.ChoreError:
        raise PassUp("kid_or_chore_unresolved") from None
    day = _day_of(f, ctx)
    label = day_label(day, ctx.today)
    try:
        res = chores.complete(kid["id"], chore["id"], log_date=None if day == ctx.today else day.isoformat(),
                              db_path=ctx.db_path, today=ctx.today)
    except chores.ChoreError as e:
        return Reply(e.message)    # "already done for this day", "not assigned to ...": the user should hear it
    out = f"✅ {res['kid']}: {res['chore']} done"
    out += "." if day == ctx.today else f" for {label}."
    if res["earned_cents"]:
        out += f" Earned {res['earned']}, balance {res['balance']}."
    if res["new_badges"]:
        out += " New badge: " + ", ".join(b.get("name", "") if isinstance(b, dict) else str(b) for b in res["new_badges"]) + "."
    if res["all_done"]:
        out += f" That's everything for {res['kid']} today."
    return Reply(out, wrote=True)


# ---- Todoist ----

_ITEM_BAD = re.compile(
    r"\b(it|that|this|those|these|them|everything|stuff|things?|items?|also|too|same|again|instead|"
    r"today|tomorrow|tonight|monday|tuesday|wednesday|thursday|friday|saturday|sunday|remind|reminder|due)\b|[?\n]", re.I)
_LIST_DROP = {"my", "our", "the", "list", "shopping", "grocery", "groceries"}


def _lkey(name: str) -> str:
    return " ".join(t for t in re.findall(r"[a-z0-9]+", _fix(name).lower().replace("'", "")) if t not in _LIST_DROP)


def resolve_list(phrase: str, projects: list[dict]) -> dict | None:
    """The Todoist project a spoken list name means ("Costco list", "shopping list", "grocery list"), or None when
    it is not exactly one project."""
    want = " ".join(_fix(phrase).lower().split())
    exact = [p for p in projects if " ".join(str(p.get("name", "")).lower().split()) == want]
    if len(exact) == 1:
        return exact[0]
    key = _lkey(phrase)
    hits = [p for p in projects if _lkey(str(p.get("name", ""))) == key]
    return hits[0] if len(hits) == 1 else None


def _check_todo(fields, text, ctx):
    items = fields.get("items")
    if isinstance(items, str):
        items = [items]
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise PassUp("items_invalid")
    out: list[str] = []
    low = text.lower()
    for it in items:
        if not isinstance(it, str):
            raise PassUp("items_invalid")
        it = it.strip().strip(".,;:!").strip()
        if not it or len(it) > 80 or _ITEM_BAD.search(it) or it.lower() not in low:
            raise PassUp("item_rejected")
        if it.lower() not in [o.lower() for o in out]:
            out.append(it)
    lst = _str(fields.get("list"), "list")
    if lst.lower() not in low or "list" not in lst.lower():
        raise PassUp("list_not_in_text")
    if not [t for t in re.findall(r"[a-z0-9]+", lst.lower()) if t not in {"my", "our", "the", "list"}]:
        raise PassUp("list_vague")   # "the list" names nothing; "shopping list" and "Costco list" do
    return {"items": out, "list": lst}


def _todo_add(f, ctx, dry):
    if dry:
        return Reply(f"Would add {', '.join(f['items'])} to {f['list']}.")
    try:
        projects = todoist_api.list_projects()
    except todoist_api.TodoistError:
        raise PassUp("todoist_unavailable") from None
    proj = resolve_list(f["list"], projects)
    if proj is None:
        raise PassUp("list_unresolved")
    added, skipped, failed, err = [], [], [], ""
    for item in f["items"]:
        try:
            res = todoist_api.add_task(item, proj["name"], skip_if_present=True)
        except todoist_api.TodoistError as e:
            if not added and not skipped and e.code in ("no_token", "auth", "not_found", "bad_input"):
                raise PassUp("todoist_" + e.code) from None   # certainly nothing was written
            failed.append(item)
            err = e.message
            break
        (skipped if res.get("skipped") else added).append(item)
    rest = f["items"][len(added) + len(skipped) + len(failed):]
    failed += rest
    parts = []
    if added:
        parts.append(f"✅ Added to {proj['name']}: {', '.join(added)}.")
    if skipped:
        parts.append(f"Already on {proj['name']}: {', '.join(skipped)}.")
    if failed:
        parts.append(f"Couldn't confirm: {', '.join(failed)} ({err}). Check the list before retrying.")
    return Reply(" ".join(parts), wrote=bool(added) or bool(failed))


# ---- My Hub snapshot (calendar and tasks as the myhub agent last synced them) ----

def _hub_card(ctx: Context) -> dict:
    from hub_providers import schedule
    conn = myhub.connect(ctx.db_path)
    try:
        card = (schedule.collect(conn, ctx.today) or {}).get("card") or {}
    finally:
        conn.close()
    return card


def _stale_note(card: dict, kind: str) -> str:
    at = (card.get("synced") or {}).get(kind)
    if not at:
        raise PassUp("hub_not_synced")
    try:
        age = datetime.now() - datetime.fromisoformat(at)
    except ValueError:
        raise PassUp("hub_not_synced") from None
    if age > timedelta(hours=SNAPSHOT_STALE_H):
        return f" (last synced {int(age.total_seconds() // 3600)} h ago)"
    return ""


def _clock(t: str | None) -> str:
    if not t:
        return "all day"
    try:
        d = datetime.strptime(t[:5], "%H:%M")
    except ValueError:
        return t
    return d.strftime("%I:%M %p").lstrip("0")


def _hub_today(f, ctx, dry):
    if dry:
        return Reply("Would read today's calendar from My Hub.")
    card = _hub_card(ctx)
    note = _stale_note(card, "event")
    events = card.get("today") or []
    lines = [f"Today, {ctx.today.strftime('%a %b')} {ctx.today.day}{note}:"]
    lines += [f"• {_clock(e.get('start_time'))} {e['title']}" for e in events] or ["• Nothing on the calendar."]
    if card.get("dinner"):
        lines.append(f"Dinner: {card['dinner']['title']}")
    tasks = card.get("tasks") or {}
    due = (tasks.get("overdue") or []) + (tasks.get("today") or [])
    if due:
        lines.append(f"Tasks due or overdue: {len(due)}")
    return Reply("\n".join(lines))


def _hub_tasks(f, ctx, dry):
    if dry:
        return Reply("Would read the task list from My Hub.")
    card = _hub_card(ctx)
    note = _stale_note(card, "task")
    t = card.get("tasks") or {}
    lines = [f"Tasks{note}: {t.get('open', 0)} open."]
    for label, key in (("Overdue", "overdue"), ("Due today", "today"), ("Coming up", "soon")):
        items = t.get(key) or []
        if items:
            lines.append(f"{label}:")
            lines += [f"• {x['title']}" + (f" ({x['day']})" if key != "today" else "") for x in items]
    if len(lines) == 1 and not t.get("open"):
        lines[0] = f"No open tasks{note}."
    return Reply("\n".join(lines))


def _hub_dinner(f, ctx, dry):
    if dry:
        return Reply("Would read tonight's dinner from My Hub.")
    card = _hub_card(ctx)
    note = _stale_note(card, "event")
    d = card.get("dinner")
    if d:
        return Reply(f"Dinner today: {d['title']}{note}")
    return Reply(f"Nothing is on the meal calendar for today{note}.")


# ---- Home Assistant (fast-lane allow-list only) ----

def ha_key(target: str) -> str:
    """Spoken name -> alias-table key: lowercase, apostrophes removed, "the/my/our" dropped."""
    t = " ".join(_fix(target).lower().replace("'", "").split())
    t = " ".join(w for w in t.split() if w not in {"the", "my", "our"})
    return t


def _alias_entities(target: str, table: dict) -> tuple[str, ...]:
    key = ha_key(target)
    for cand in (key, re.sub(r"\s*(thermostat|temperature|temp)\s*", " ", key).strip()):
        if cand in table:
            return table[cand]
    return ()


_HA_VERB = {"on": r"\bon\b", "off": r"\boff\b", "close": r"\b(?:close|closes|closing|shut|lower)\b"}


def _check_ha_set(fields, text, ctx):
    target = _str(fields.get("target"), "target")
    action = str(fields.get("action") or "").strip().lower()
    ents = _alias_entities(target, ha.FASTLANE_ALIASES)
    if not ents:
        raise PassUp("ha_target_unknown")
    if action in ("on", "off"):
        if ents != (ha.FASTLANE_PORCH,):
            raise PassUp("ha_action_not_allowed")
        if not re.search(_HA_VERB[action], text, re.I):
            raise PassUp("ha_verb_missing")
        return {"target": target, "action": action}
    if action == "close":
        if not set(ents) <= set(ha.FASTLANE_GARAGES):
            raise PassUp("ha_action_not_allowed")
        if not re.search(_HA_VERB["close"], text, re.I):
            raise PassUp("ha_verb_missing")
        return {"target": target, "action": "close"}
    if action == "set_temp":
        if len(ents) != 1 or not ents[0].startswith("climate."):
            raise PassUp("ha_action_not_allowed")
        try:
            value = float(fields.get("value"))
        except (TypeError, ValueError):
            raise PassUp("ha_value_invalid") from None
        if not 60 <= value <= 80:
            raise PassUp("ha_value_range")
        if not re.search(rf"(?<![\d.]){re.escape(str(int(value)))}(?![\d])", text):
            raise PassUp("ha_value_not_in_text")
        return {"target": target, "action": "set_temp", "value": int(value) if value == int(value) else value}
    raise PassUp("ha_action_unknown")


def _door_state(tile: dict) -> str:
    """The real state of a garage cover: open, opening, stopped, closing, closed. Anything else (unavailable,
    unknown, ...) is "unknown", which is never treated as closed."""
    st = str(tile.get("state") or "").lower()
    return st if st in ("open", "opening", "stopped", "closing", "closed") else "unknown"


def _ha_set(f, ctx, dry):
    ents = _alias_entities(f["target"], ha.FASTLANE_ALIASES)
    action = f["action"]
    if dry:
        verb = {"on": "turn on", "off": "turn off", "close": "close",
                "set_temp": f"set to {f.get('value')} degrees"}[action]
        return Reply(f"Would {verb}: {', '.join(ents)}.")
    try:
        if action in ("on", "off"):
            tile = ha.fastlane_act(ents[0], "turn_" + action)
            want = action
            name = f["target"].strip().rstrip(".").lower()
            if tile.get("state") == want:
                return Reply(f"✅ {name[:1].upper() + name[1:]} {action}.", wrote=True)
            return Reply(f"Sent turn {action} to the {name}; it still shows {tile.get('state')}, which can take a moment.", wrote=True)
        if action == "set_temp":
            tile = ha.fastlane_act(ents[0], "set_temperature", f["value"])
            lo, hi = tile.get("min_temp"), tile.get("max_temp")
            applied = f["value"] if lo is None or hi is None else max(lo, min(hi, f["value"]))
            note = "" if applied == f["value"] else f" (that's the thermostat's limit; you asked for {f['value']})"
            return Reply(f"✅ {tile.get('name') or f['target']} set to {applied:g}°.{note}", wrote=True)
        return _close_doors(ents)
    except ha.HAError as e:
        if e.code == 2:
            raise PassUp("ha_refused") from None
        return Reply(f"Home Assistant didn't answer ({e.message}). I can't tell if that went through; check it.", wrote=True)


def _close_doors(ents) -> Reply:
    """Close only doors that are open, opening or stopped; say what each one was. An unreadable state is never
    treated as closed. An error on a later door reports the earlier doors' outcome (side effects are possible, so
    this is a reply, not a pass-up). Completion is not awaited: the reply says it was requested."""
    said, attempted = [], False
    for eid in ents:
        try:
            tile = ha.fastlane_read(eid)
            name = tile.get("name") or eid
            state = _door_state(tile)
            if state == "closed":
                said.append(f"{name} is already closed.")
            elif state == "closing":
                said.append(f"{name} is already closing.")
            elif state == "unknown":
                said.append(f"I can't read the state of {name} (it reports {tile.get('state')!r}), so I did nothing for it. Please check it.")
            else:
                attempted = True
                ha.fastlane_act(eid, "close_cover")
                said.append(f"Closing {name} (it was {state}). I haven't confirmed it finished, and the doorway should be clear.")
        except ha.HAError as e:
            if e.code == 2 and not said:
                raise PassUp("ha_refused") from None
            said.append(f"Home Assistant didn't answer for {eid} ({e.message}); I can't tell whether that one went through. Please check it.")
            return Reply(" ".join(said), wrote=attempted)
    return Reply(" ".join(said), wrote=attempted)


def _check_ha_state(fields, text, ctx):
    target = _str(fields.get("target"), "target")
    what = "setpoint" if str(fields.get("what") or "").lower() == "setpoint" else None
    if not _read_entities(target, what):
        raise PassUp("ha_target_unknown")
    return {"target": target, "what": what}


def _read_entities(target: str, what: str | None = None) -> tuple[str, ...]:
    """Entities a read question can mean. "temperature in X" prefers the room's sensor; "what is X set to" the thermostat."""
    key = re.sub(r"^(?:in |at |of |for )", "", ha_key(target))
    thermostats = {k: v for k, v in ha.FASTLANE_ALIASES.items() if v and v[0].startswith("climate.")}
    if what == "setpoint":
        found = _alias_entities(key, thermostats)
        if found:
            return found
    ents = _alias_entities(target, ha.FASTLANE_READ_ALIASES)
    if ents:
        return ents
    for eid, label in ha.TEMP_SENSORS:
        if ha_key(label) == key:
            return (eid,)
    return _alias_entities(key, thermostats)


def _ha_state(f, ctx, dry):
    ents = _read_entities(f["target"], f.get("what"))
    if dry:
        return Reply(f"Would read the state of: {', '.join(ents)}.")
    out = []
    try:
        for eid in ents:
            t = ha.fastlane_read(eid)
            name = t.get("name") or eid
            if eid.startswith("climate."):
                cur, tgt = t.get("current"), t.get("target")
                bit = f"{cur:g}° now" if isinstance(cur, (int, float)) else "no reading"
                if isinstance(tgt, (int, float)):
                    bit += f", set to {tgt:g}°"
                out.append(f"{name}: {bit}.")
            elif eid.startswith("sensor."):
                out.append(f"{name}: {t.get('display')}.")
            elif eid.startswith("cover."):
                st = _door_state(t)
                out.append(f"{name}: " + ("stopped partway" if st == "stopped" else st if st != "unknown"
                                           else f"state unavailable (it reports {t.get('state')!r})") + ".")
            elif eid.startswith("binary_sensor."):
                word = {"on": "open", "off": "closed"}.get(t.get("state"))
                out.append(f"{name}: {word}." if word else f"{name}: can't be read (it reports {t.get('state')!r}).")
            else:
                out.append(f"{name}: {t.get('state')}.")
    except ha.HAError as e:
        if e.code == 2:
            raise PassUp("ha_refused") from None
        return Reply(f"Home Assistant didn't answer ({e.message}).")
    return Reply(" ".join(out))


def _kw_none(f): return []  # noqa: E704
def _kw_habit(f): return [w for w in re.findall(r"[a-z0-9]+", (f.get("habit") or "").lower()) if len(w) > 2]  # noqa: E704
def _kw_chore(f): return [w for w in re.findall(r"[a-z0-9]+", f"{f.get('kid', '')} {f.get('chore', '')}".lower()) if len(w) > 2]  # noqa: E704
def _kw_todo(f): return [i.lower() for i in f.get("items", [])]  # noqa: E704
def _kw_rating(f): return ["rat", "score"]  # noqa: E704


def _kw_ha(f):
    ents = _alias_entities(f.get("target", ""), ha.FASTLANE_ALIASES)
    words = [w for w in ha_key(f.get("target", "")).split() if len(w) > 2]
    if ha.FASTLANE_PORCH in ents:
        words += ["porch", "outside", "outdoor", "front door"]
    if any(e in ha.FASTLANE_GARAGES for e in ents):
        words += ["garage"]
    return words


ACTIONS: dict[str, Action] = {a.name: a for a in [
    Action("habit.log", "Log a habit as done.", {"habit": "the habit's name as listed", "day": "optional: today, yesterday, a weekday name, or YYYY-MM-DD (never a future day)"},
           _check_habit, _habit_log, _kw_habit, writes=True),
    Action("habit.status", "Show streak / progress of one habit, or of all habits.", {"habit": "optional: the habit's name"},
           _check_habit_status, _habit_status, _kw_none),
    Action("rating.set", "Rate a day from 1 to 10 with an optional note.", {"score": "integer 1-10", "note": "optional: the user's own words, copied exactly", "day": "optional: today, yesterday, weekday, M/D or YYYY-MM-DD"},
           _check_rating, _rating_set, _kw_rating, writes=True),
    Action("chore.done", "Mark a kid's chore done.", {"kid": "the kid's name", "chore": "the chore's name as listed", "day": "optional day"},
           _check_chore, _chore_done, _kw_chore, writes=True),
    Action("todo.add", "Add one or more items to a named Todoist list.", {"items": "list of item texts, each copied exactly from the message", "list": "the list name as the user said it, e.g. 'Costco list'"},
           _check_todo, _todo_add, _kw_todo, writes=True),
    Action("hub.today", "Today's calendar and tasks.", {}, lambda f, t, c: {}, _hub_today, _kw_none),
    Action("hub.tasks", "The open task list.", {}, lambda f, t, c: {}, _hub_tasks, _kw_none),
    Action("hub.dinner", "What is for dinner today.", {}, lambda f, t, c: {}, _hub_dinner, _kw_none),
    Action("ha.set", "Switch the porch lights on/off, set a thermostat, or CLOSE a garage door. Nothing else.",
           {"target": "porch lights | a thermostat room name | single garage | double garage | garage", "action": "on | off | set_temp | close", "value": "degrees, only for set_temp"},
           _check_ha_set, _ha_set, _kw_ha, writes=True),
    Action("ha.state", "Read whether a door or garage is open, the porch lights state, or a temperature.", {"target": "what to read, e.g. 'garage', 'front door', 'basement'", "what": "optional: 'setpoint' when asking what a thermostat is set to"},
           _check_ha_state, _ha_state, _kw_none),
]}


def prepare(action: str, fields: dict, text: str, ctx: Context) -> dict:
    """Validate fields for a registered action against the message; the canonical fields, or PassUp."""
    spec = ACTIONS.get(action)
    if spec is None:
        raise PassUp("unknown_action")
    clean_fields = {k: v for k, v in (fields or {}).items() if k in spec.fields}
    return spec.check(clean_fields, text, ctx)


def execute(match: Match, ctx: Context, *, dry_run: bool = False) -> Reply:
    """Run a validated match through its service. dry_run reads and describes but never writes or calls out.
    Raises PassUp when it turns out the agent should handle it."""
    spec = ACTIONS.get(match.action)
    if spec is None:
        raise PassUp("unknown_action")
    return spec.run(match.fields, ctx, dry_run)


def keywords(match: Match) -> list[str]:
    spec = ACTIONS.get(match.action)
    return spec.keywords(match.fields) if spec else []


# ---------------------------------------------------------------------------
# Tier 0: strict rules
# ---------------------------------------------------------------------------

_DAYTAIL = rf"(?: (?:for|on) (?P<d1>{DAY}|the day)| (?P<d2>{DAY}))?"
_DONE = r"(?:done|complete|completed|finished)"
_HABIT_RULES = [
    re.compile(rf"^(?:log|mark|record|check off|tick off)(?: my| the| a)? (?P<habit>.+?)(?: habit)?(?: (?:as )?{_DONE})?{_DAYTAIL}$", re.I),
    re.compile(rf"^(?:i )?(?:just )?(?:did|finished|completed|logged)(?: my| the| a)? (?P<habit>.+?)(?: habit)?{_DAYTAIL}$", re.I),
    re.compile(rf"^(?:i )?(?:just )?(?P<habit>.+?)(?: habit)? (?:is )?{_DONE}{_DAYTAIL}$", re.I),
]
_CHORE_RULES = [
    re.compile(rf"^(?:mark|log|record)(?: that)? (?P<kid>[a-z]+?)(?:'s)? (?P<chore>.+?) (?:as )?{_DONE}{_DAYTAIL}$", re.I),
    re.compile(rf"^(?P<kid>[a-z]+) (?:just )?(?:did|finished|completed)(?: her| his| their)? (?P<chore>.+?){_DAYTAIL}$", re.I),
    re.compile(rf"^(?P<kid>[a-z]+?)(?:'s)? (?P<chore>.+?) (?:is |are )?{_DONE}{_DAYTAIL}$", re.I),
]
_RATE_RULES = [
    re.compile(rf"^(?:rate|rating)(?:\s+(?:my|the))?(?:\s+day)?(?:\s+(?P<day>{DAY}))?(?:\s+(?:to|at|as))?\s+(?:an?\s+)?(?P<score>10|[1-9])(?:/10)?(?:\s*[-–—:,.;]\s*(?P<note>\S.*))?$", re.I | re.S),
    re.compile(rf"^(?P<day>{DAY})\s+(?:was|is)\s+(?:an?\s+)?(?P<score>10|[1-9])(?:/10)?(?:\s*[-–—:,.;]\s*(?P<note>\S.*))?$", re.I | re.S),
    re.compile(rf"^(?P<day>{DAY})\s+(?P<score>10|[1-9])/10(?:\s*[-–—:,.;]\s*(?P<note>\S.*))?$", re.I | re.S),
]
_TODO_RULE = re.compile(r"^(?:add|put) (?P<items>.+?) (?:to|on|onto) (?:my |the |our )?(?P<list>[a-z0-9' &-]+? list)$", re.I)
_HA_ONOFF = [
    re.compile(r"^(?:turn|switch) (?P<state>on|off) (?:the |my |our )?(?P<target>[a-z' ]+?)(?: back)?$", re.I),
    re.compile(r"^(?:turn|switch) (?:the |my |our )?(?P<target>[a-z' ]+?)(?: back)? (?P<state>on|off)$", re.I),
]
_HA_TEMP = re.compile(r"^set (?:the |my |our )?(?P<target>[a-z' ]+?)(?: thermostat| temp(?:erature)?)? to (?P<n>\d{2})(?:\s*(?:°|degrees?))?$", re.I)
_HA_CLOSE = re.compile(r"^(?:close|shut|lower) (?:the |my |our )?(?P<target>(?:both |single |double )?garage(?: door)?s?|single door|double door)$", re.I)
_HA_STATE = [
    re.compile(r"^(?:is|are) (?:the |my |our )?(?P<target>[a-z' ]+?) (?:open|closed|shut|on|off)$", re.I),
    re.compile(r"^(?:what(?:'s| is) )?the (?:temp(?:erature)?) (?:in|of|at|for) (?:the )?(?P<target>[a-z' ]+)$", re.I),
    re.compile(r"^what(?:'s| is) (?:the )?(?P<target>[a-z' ]+?) (?P<what>set to)$", re.I),
    re.compile(r"^what(?:'s| is) (?:the )?(?P<target>[a-z' ]+?) temp(?:erature)?$", re.I),
]
_HUB = {
    "hub.today": re.compile(
        r"^(?:what(?:'s| is) (?:on|happening)(?: (?:my|the) (?:calendar|schedule))?(?: today)?|what do i have(?: going on)?(?: today)?|"
        r"what(?:'s| is) my (?:day|schedule)(?: today)?|today'?s (?:schedule|calendar|agenda)|what(?:'s| is) on (?:the |my )?calendar(?: today)?)$", re.I),
    "hub.tasks": re.compile(
        r"^(?:what(?:'s| are| is) (?:on )?my (?:tasks|to-?do list|to-?dos|todo list)|what tasks do i have|my tasks|what(?:'s| is) due today|what do i need to do today)$", re.I),
    "hub.dinner": re.compile(
        r"^(?:what(?:'s| is| are we having) (?:for )?dinner(?: today| tonight)?|what(?:'s| is) for dinner(?: today| tonight)?|dinner (?:today|tonight)|what are we having for dinner(?: today| tonight)?)$", re.I),
}
_HABIT_STATUS = [
    re.compile(r"^(?:habit status|habit streaks|my habit streaks|my streaks|how are my (?:habits|streaks)(?: doing)?|how(?:'s| are) my habits(?: doing)?)$", re.I),
]
_HABIT_ONE_STATUS = re.compile(r"^(?:how(?:'s| is) |what(?:'s| is) )?(?:my )?(?P<habit>.+?) (?:streak|status)$", re.I)


def _day_of_match(m: re.Match) -> str | None:
    gd = m.groupdict()
    d = gd.get("d1") or gd.get("d2") or gd.get("day")
    if d and d.lower() == "the day":
        return None
    return d.lower() if d else None


def tier0(text: str, ctx: Context) -> Match | None:
    """A Match for a clear, complete command, or None. Strict by design: when in doubt, None."""
    if not text or len(text) > MAX_TEXT or "\n" in text.strip():
        return None
    raw = _POLITE.sub("", text.strip(), count=1).strip()   # untouched except the polite lead-in: a rating note stays as written
    c = clean(text)
    low = c.lower()
    if not c:
        return None

    def build(action: str, fields: dict, check_text: str | None = None) -> Match | None:
        try:
            fields = prepare(action, fields, text, ctx)
        except PassUp:
            return None
        if write_veto(action, fields, text, check_text):
            return None
        return Match(action, fields, tier=0, check_text=check_text)

    # Hub and status phrases (exact)
    for name, rx in _HUB.items():
        if rx.match(low):
            return build(name, {})
    if any(rx.match(low) for rx in _HABIT_STATUS):
        return build("habit.status", {})
    m = _HABIT_ONE_STATUS.match(low)
    if m and match_entry(m.group("habit"), ctx.habits):
        return build("habit.status", {"habit": m.group("habit")})

    # Home Assistant
    for rx in _HA_ONOFF:
        m = rx.match(low)
        if m:
            return build("ha.set", {"target": m.group("target"), "action": m.group("state").lower()})
    m = _HA_TEMP.match(low)
    if m:
        return build("ha.set", {"target": m.group("target"), "action": "set_temp", "value": int(m.group("n"))})
    m = _HA_CLOSE.match(low)
    if m:
        return build("ha.set", {"target": m.group("target"), "action": "close"})
    for rx in _HA_STATE:
        m = rx.match(low)
        what = "setpoint" if m and m.groupdict().get("what") else None
        if m and _read_entities(m.group("target"), what):
            return build("ha.state", {"target": m.group("target"), "what": what})

    # Day rating: the note is kept exactly as written (original case and punctuation)
    for rx in _RATE_RULES:
        m = rx.match(raw)
        if m:
            note = m.group("note")
            head = raw[:m.start("note")] if note else raw
            return build("rating.set", {"score": m.group("score"), "note": note, "day": (m.group("day") or None) and m.group("day").lower()},
                         check_text=head)

    # Todoist list
    m = _TODO_RULE.match(c)
    if m:
        items_text = m.group("items")
        if re.search(r"\band\b|&|\+|/|\bor\b", items_text, re.I):
            return None    # "mac and cheese" or two items? the agent (or Tier 1) decides
        items = [i.strip() for i in items_text.split(",") if i.strip()]
        return build("todo.add", {"items": items, "list": m.group("list")})

    # Chores (needs a real kid and chore), then habits (needs a real habit)
    for rx in _CHORE_RULES:
        m = rx.match(c)
        if m and match_entry(m.group("kid"), ctx.kids) and match_entry(m.group("chore"), ctx.chores):
            return build("chore.done", {"kid": m.group("kid"), "chore": m.group("chore"), "day": _day_of_match(m)})
    for rx in _HABIT_RULES:
        m = rx.match(c)
        if m and match_entry(m.group("habit"), ctx.habits):
            return build("habit.log", {"habit": m.group("habit"), "day": _day_of_match(m)})
    return None


# ---------------------------------------------------------------------------
# Tier 1: validate the model's answer
# ---------------------------------------------------------------------------


# What Tier 1 (the model) may run. Device control (ha.set) and habit logs (habit.log) are NOT here: they happen only
# through the anchored Tier 0 rules or the full agent, so a model that is fooled can never switch a device or log a habit.
# chore.done is not here either: chores involve money, so only the exact, anchored, past-tense Tier 0 rules do them.
TIER1_ACTIONS = frozenset({"ha.state", "hub.today", "hub.tasks", "hub.dinner", "habit.status", "todo.add", "rating.set"})

_RATING_HEAD_WORDS = {"rate", "rated", "rating", "i", "id", "i'd", "would", "my", "the", "day", "today", "yesterday", "was", "is",
                      "a", "an", "out", "of", "please", "can", "you", "could", "give", "it", "as", "to", "at", "overall"}
_RATING_DAY_CUE = re.compile(rf"\b(?:today|yesterday|{_WD}|(?:my|the) day)\b", re.I)


def rating_about_the_day(head: str) -> bool:
    """Tier 1 only. A rating is the day's rating only if the message (apart from a note after a separator) is about
    the day: it names today, yesterday, a weekday, "my day" or "the day", or is just a number. "rate the movie 7" and
    "Ruth was a 7 out of 10 at dance" are about something else and are refused."""
    h = _fix(head).strip().rstrip(" \t-\u2013\u2014:,.;")
    words = re.findall(r"[a-z']+|\d+", h.lower())
    rest = [w for w in words if not w.isdigit() and w not in _RATING_HEAD_WORDS and not re.fullmatch(_WD, w)]
    if rest:
        return False
    if re.fullmatch(r"(?:rate\s+)?(?:10|[1-9])(?:/10)?", h, re.I):
        return True      # a bare number
    return bool(_RATING_DAY_CUE.search(h))


def check_model_output(obj, text: str, ctx: Context) -> tuple[Match | None, str]:
    """(Match, "") for a usable answer, else (None, reason). The registry and the per-action checks decide, never
    the model: unknown action, low confidence, a name or text that is not in the message, a future date, an action
    outside the allow-list all end up as (None, reason)."""
    if not isinstance(obj, dict):
        return None, "model_not_object"
    name = obj.get("action")
    if not isinstance(name, str) or name in ("", "none"):
        return None, "model_none"
    if name not in ACTIONS:
        return None, "model_unknown_action"
    if name not in TIER1_ACTIONS:
        return None, "model_tier1_not_allowed"     # enforced here, not in the prompt
    try:
        conf = float(obj.get("confidence"))
    except (TypeError, ValueError):
        return None, "model_no_confidence"
    if conf < CONFIDENCE_MIN:
        return None, "model_low_confidence"
    fields = obj.get("fields")
    if fields is None:
        fields = {k: v for k, v in obj.items() if k not in ("action", "confidence")}
    if not isinstance(fields, dict):
        return None, "model_bad_fields"
    try:
        canon = prepare(name, fields, text, ctx)
    except PassUp as e:
        return None, "model_" + e.reason
    note = canon.get("note")
    probe = text.replace(note, "") if note else None
    if name == "rating.set" and not rating_about_the_day(probe if probe is not None else text):
        return None, "model_rating_not_about_the_day"
    why = write_veto(name, canon, text, probe)
    if why:
        return None, "model_veto_" + why
    return Match(name, canon, tier=1, confidence=conf, check_text=probe), ""


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------

# Never fast-laned, whatever the phrasing: opening a garage, unlocking, disarming. These words veto the whole message.
_STRONG_UNSAFE = re.compile(r"\b(?:unlock\w*|unlatch\w*|disarm\w*|unarm\w*|deadbolt\w*)\b", re.I)
_WEAK_UNSAFE = re.compile(r"\b(?:open\w*|raise\w*|lift\w*|arm\w*|rais\w*|up)\b", re.I)
_UNSAFE_NOUN = re.compile(r"\b(?:garages?|doors?|locks?|alarm\w*|gates?|latch\w*|entry)\b", re.I)


# "Is the garage open?" only reads a state; it is the one phrasing of these words the fast lane may answer.
_STATE_QUESTION = re.compile(r"^(?:is|are) (?:the |my |our )?[a-z' ]+? (?:open|closed|shut)\s*\??$", re.I)


def unsafe_text(text: str) -> bool:
    """True when the message is about opening/unlocking/disarming something. Such messages always go to the agent."""
    return bool(_STRONG_UNSAFE.search(text) or (_WEAK_UNSAFE.search(text) and _UNSAFE_NOUN.search(text)))


# Talk about the assistant itself or development work ("use the home-assistant agent", "live test", "merge it") is
# never a quick command, even when it mentions a light or a list.
_META = re.compile(r"\b(?:agents?|subagents?|tests?|testing|restart\w*|deploy\w*|branch\w*|merge\w*|commit\w*|pull request|"
                   r"worker|bridge|orchestrator|claude|prompt)\b", re.I)


def meta_text(text: str) -> bool:
    return bool(_META.search(text))


_REFERS_BACK = re.compile(
    r"\b(?:never ?mind|undo|cancel|scratch that|instead|actually|wait|stop|revert|that|those|these|this|it|them|again|"
    r"same|other|previous|earlier|before|first|second|last one|the one|reverse|change it|redo)\b", re.I)


def refers_back(text: str) -> bool:
    return bool(_REFERS_BACK.search(text))


_HINTS = [
    re.compile(r"\b(?:add|put)\b.*\blist\b", re.I),
    re.compile(r"\b(?:rate|rated|rating)\b|\bwas an? (?:10|[1-9])\b|\b(?:10|[1-9])\b.*\bday\b", re.I),
    re.compile(r"\b(?:dinner|calendar|schedule|tasks?|to-?do|agenda|habits?|streaks?)\b", re.I),
    re.compile(r"\b(?:porch|outside|outdoor|front door|back door|garage|thermostat|temperature|degrees|lights?)\b", re.I),
]


def worth_model(text: str, ctx: Context) -> bool:
    """Cheap lexical filter before a Tier 1 call: only messages that mention something the fast lane can act on.
    Everything else goes straight to the agent without waiting for a model."""
    if len(text) > 200 or text.count("\n") > 2:
        return False
    if any(rx.search(text) for rx in _HINTS):
        return True
    names = [e["name"] for e in ctx.habits + ctx.kids + ctx.chores]
    return any(mentions(text, n) for n in names)


def is_dev(session_key: str, chat_ref: str) -> bool:
    from bridge_store import session_key_for
    dev = dev_chat_ids()
    return session_key in dev or chat_ref in dev or any(session_key_for(c) == session_key for c in dev)


def eligibility(row: dict, store, *, now: float, ctx: Context | None = None) -> str | None:
    """A reason this message must go to the agent without any fast-lane attempt, or None when it may be tried."""
    p = row.get("payload") or {}
    text = row.get("text") or ""
    if row.get("kind") != "message":
        return "not_message"
    if row.get("channel") not in CHANNELS or str(p.get("user", "")).startswith("scheduled"):
        return "channel"
    if is_dev(row["session_key"], row["chat_ref"]):
        return "dev_chat"
    if p.get("attachments"):
        return "attachment"
    if p.get("reply_to"):
        return "reply_to"
    if p.get("edited"):
        return "edited"
    if (row.get("attempts") or 0) > 0:
        return "retry"
    if not text.strip():
        return "empty"
    if len(text) > MAX_TEXT:
        return "long"
    if now - row["received_at"] > MAX_AGE_S:
        return "late"
    q = store.recent_assistant_question(row["session_key"], now=now, exclude_id=row.get("id"))
    if q:
        return q
    if unsafe_text(text) and not _STATE_QUESTION.match(text.strip()):
        return "unsafe"
    if meta_text(text):
        return "meta"
    return None


_NEGATION = re.compile(
    r"\b\w+n't\b|\b(?:no|not|never|no longer|skip|cancel\w*|nothing|neither|nor|none|cannot|"
    r"dont|wont|cant|shouldnt|wouldnt|couldnt|isnt|arent|wasnt|werent|didnt|doesnt|havent|hasnt|hadnt|aint|mustnt|neednt)\b", re.I)
# Not a command to do it now: later, tomorrow, a time, an event to wait for, tentative or reminder wording.
_DEFERRED = re.compile(
    r"\b(?:later|tomorrow|tonight|tonite|soon|eventually|someday|sometime|maybe|might|perhaps|probably|remind\w*|"
    r"next (?:week|month|year|time)|this (?:evening|afternoon|morning|weekend)|in the (?:morning|evening|afternoon)|"
    r"when|whenever|once|while|until)\b"
    r"|\b(?:at|in|after|before|by|until)\s+(?:\d|(?:noon|midnight|night|the (?:morning|evening|afternoon|night|weekend)|"
    r"dinner|lunch|breakfast|bed|bedtime|work|school|church|practice|(?:the )?kids|i|we|you|they|she|he)\b)", re.I)
_QUESTION_START = re.compile(
    r"^(?:should|shall|would|could|must|what if|if|did|do i|have i|has|can i|how|why|what|when|where|who|which|is|are|was|were|does|will)\b", re.I)
_MODAL = re.compile(r"\b(?:will|should|would|could|must|shall|going to|gonna)\b", re.I)
_CONDITIONAL = re.compile(r"\b(?:if|unless)\b", re.I)
_QUOTED = re.compile(r"\"|(?:^|\s)'[^']+'(?:\s|$|[.,!?])")
_REPORTED = re.compile(r"\b(?:said|says|told|asked|asks|thinks|wants me|wanted me|wants you)\b", re.I)
_MULTI = re.compile(r"\b(?:and|then|also|plus)\b|[,;]", re.I)
_SECOND_VERB = re.compile(
    r"(?:\band\b|\bthen\b|\balso\b|\bplus\b|[,;])\s*(?:please\s+)?(?:turn|switch|set|close|shut|lower|log|mark|add|put|rate|open|raise|"
    r"lift|unlock|remind|call|send|text|email|buy|get|check|schedule|make|start|stop|delete|remove|move|change|update)\b", re.I)
_TODO_FILLER = {"add", "put", "to", "on", "onto", "the", "my", "our", "please", "and", "a", "an", "some", "can", "you",
                "could", "would", "will", "list", "shopping", "grocery"}


def write_veto(action: str, fields: dict, text: str, probe: str | None = None) -> str | None:
    """A reason NOT to run a write action from this message, whatever tier chose it, or None. Negations, questions,
    conditionals, quotes and reported speech are not commands; a message with more than one command is never
    answered by doing part of it. `probe` is the message without free text (a rating note), so the user's own words in
    a note cannot trip it. Read-only actions are not checked."""
    spec = ACTIONS.get(action)
    if spec is None or not spec.writes:
        return None
    t = _fix(probe if probe is not None else text).strip().rstrip(" \t-–—:,.;")   # the separator before a note is not a comma
    lead = strip_polite(t)
    if _NEGATION.search(t):
        return "negation"
    if _QUESTION_START.match(lead) or t.endswith("?"):
        return "question"
    if _MODAL.search(lead):
        return "modal"          # "Claire will / should feed the dog": a prediction or an opinion, not a command
    if _CONDITIONAL.search(t):
        return "conditional"
    if _DEFERRED.search(t):
        return "deferred"
    if _QUOTED.search(t):
        return "quoted"
    if _REPORTED.search(t):
        return "reported"
    if action == "todo.add":
        if _SECOND_VERB.search(t):
            return "multi_command"
        words = set(re.findall(r"[a-z0-9']+", t.lower()))
        known = set(_TODO_FILLER)
        for part in [*fields.get("items", []), fields.get("list", "")]:
            known |= set(re.findall(r"[a-z0-9']+", part.lower()))
        if words - known:
            return "partial"     # words the action does not account for: something else was asked
    elif _MULTI.search(t):
        return "multi_command"
    return None


JOURNAL_LIKE = {"rating.set", "habit.log", "chore.done"}   # could answer a nightly check-in: not fast-laned while one is open
CHECKIN_WINDOW_S = 4 * 3600


def checkin_block(match: Match, store, row: dict, ctx: Context, now: float) -> str | None:
    """While a scheduled check-in asked something in the last 4 h and the day has no rating written since,
    a rating-like message is an answer to it and belongs to the journal. Lights and list adds are unaffected."""
    if match.action not in JOURNAL_LIKE:
        return None
    asked = store.open_checkin(row["session_key"], now=now, within_s=CHECKIN_WINDOW_S)
    if asked is None:
        return None
    try:
        rated = dayrating.get_rating(None, db_path=ctx.db_path, today=ctx.today)
    except Exception:  # noqa: BLE001
        return "checkin_open"
    since = datetime.fromtimestamp(asked).isoformat(timespec="seconds")
    if rated and (rated.get("updated_at") or "") >= since:
        return None     # already answered
    return "checkin_open"


def ordering_conflict(row: dict, match: Match, store) -> str | None:
    """Reorder guard: a fast action may jump ahead of a running or queued turn only if it is independent of it.
    Pass when the message refers back, or an earlier queued/running message of this session mentions the same
    things (so "turn off the porch light" never overtakes an earlier "turn on the porch light")."""
    probe = match.check_text if match.check_text is not None else row["text"]
    earlier = store.earlier_messages(row["session_key"], row["id"])
    if refers_back(probe):
        return "refers_back"
    words = keywords(match)
    if not words:
        return None
    for e in earlier:
        low = (e["text"] or "").lower()
        if any(w in low for w in words):
            return "order_conflict"
    return None


def usage_record(row: dict, *, tier: int, action: str | None, latency_ms: int, outcome: str, cost_usd: float | None = None,
                 reason: str | None = None) -> dict:
    """One line for state/usage.jsonl. `fastlane` carries the tier and end-to-end latency."""
    rec = {"t": datetime.now().isoformat(timespec="seconds"), "session": row.get("session_key"), "inbox_id": row.get("id"),
           "cost_usd": cost_usd, "fastlane": {"tier": tier, "action": action, "latency_ms": latency_ms, "outcome": outcome}}
    if reason:
        rec["fastlane"]["reason"] = reason
    return rec


def usage_summary(path: Path | str, days: int = 7, now: datetime | None = None) -> dict:
    """Counts and median end-to-end latency per tier from state/usage.jsonl lines that carry a `fastlane` field
    (tier 0 and 1 = answered by the fast lane, tier 2 = the agent). No message text, no chat ids."""
    import statistics

    cutoff = (now or datetime.now()) - timedelta(days=days)
    lat: dict[int, list[int]] = {0: [], 1: [], 2: []}
    actions: dict[str, int] = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            r = json.loads(line)
            fl = r.get("fastlane")
            if not isinstance(fl, dict) or datetime.fromisoformat(r["t"]) < cutoff or fl.get("tier") not in lat:
                continue
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        lat[fl["tier"]].append(int(fl.get("latency_ms") or 0))
        if fl["tier"] in (0, 1) and fl.get("action"):
            actions[fl["action"]] = actions.get(fl["action"], 0) + 1
    tiers = {f"tier{t}": {"count": len(v), "median_ms": int(statistics.median(v)) if v else None} for t, v in lat.items()}
    return {"days": days, "tiers": tiers, "actions": dict(sorted(actions.items(), key=lambda kv: -kv[1])),
            "handled": len(lat[0]) + len(lat[1])}
