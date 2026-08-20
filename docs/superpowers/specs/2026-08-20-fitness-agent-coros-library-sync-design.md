# Fitness Agent — Phase 2: Workout Library Sync + Read-Only Schedule Inspection

## Context

Second phase of the fitness planning agent (see the approved plan and the
Phase 1 doc, `2026-08-20-fitness-agent-coros-login-design.md`). With login
working, this phase turns the live-discovered Workout Library findings
into real selectors and scripts, and reads the calendar back.

## What was discovered

- Two `.side-panel-toggle-btn` icons exist in the top-right icon rail
  ("Workouts" and "Training Plans") — must be disambiguated by text, not
  position, since both toggle open/closed independently.
- The Workout Library panel root is `.training-group`; each reusable
  workout is a `.training-group-item.card-dragable` carrying `data-id`
  (Coros's numeric workout id) and `data-name` directly as attributes —
  no text-parsing needed for those two fields. Stats (sets, target
  distance/time, estimated load) render as flat alternating label/value
  leaf text nodes, with the workout name repeated as the first leaf.
- Sport type is a `.iconfont-sport.icon-<slug>` element inside each card.
  Slugs confirmed live: `outrun` = Run, `strength` = Strength, `cycle` =
  Bike/Peloton. The library only had 10 workouts and didn't paginate on
  scroll — likely everything loads at once for a normal-sized library.
- Calendar day cells (`.calender-day`) carry `data-weekindex`/
  `data-dayindex` for the currently-displayed 3-week window, but the
  cell's `.calender-day-header` element's DOM `id` is the actual date as
  `YYYYMMDD` (e.g. `id="20260818"`) — a much better anchor than
  week/day-index math, since it doesn't depend on which week is showing.
- Scheduled workout cards (`.calender-day-card-item`) carry `data-id`
  (a *different* id than the library workout — this is the scheduled
  *instance*), `data-type` (`'program'` for a scheduled/planned workout),
  and `data-st` (a numeric sport-type code — only `402` = strength was
  observed live, since the only real scheduled item was a strength
  workout; the map is a stub to extend as new types are seen).
- **Important operational discovery**: COROS enforces a single active web
  session per account. Logging in via Playwright silently invalidates the
  user's own browser session on t.coros.com (confirmed twice live), and
  presumably vice versa. Accepted as a known, documented limitation per
  user decision — the automation only runs once a week and self-heals via
  `.env` credentials if its session gets invalidated by the user logging
  in elsewhere.

## What was built

- `scripts/fitness/coros_web.py` — added `SCHEDULE_SELECTORS` with the
  real selectors above (workout library panel/cards, calendar day cells,
  scheduled-card delete icon).
- `scripts/fitness/library_sync.py` — logs in (session-first), opens the
  Workout Library panel, extracts every card via one `page.evaluate`,
  prints a JSON array to stdout.
- `scripts/fitness_store.py` — new store CLI (mirrors
  `lawn_garden_store.py`'s structure). `workout sync` reads that JSON
  array from stdin and upserts into `fitness_workout_library`, keyed on
  `coros_workout_id`. `workout list [--type] [--search]` queries it back.
- `scripts/fitness/schedule_ops.py` — `list-week`: reads every visible
  calendar day cell (the site shows ~5 weeks by default) and every
  scheduled-workout card within it, grouped by absolute `YYYYMMDD` date.
- `state/schema.sql` — added `fitness_workout_library` and
  `fitness_weekly_plans` (the latter unused until Phase 3, but present so
  the eventual history-informed-planning phase has a stable schema to
  read from without a migration).

## Verification

Ran live 2026-08-20 against the real Coros account:

```
$ python scripts/fitness/library_sync.py --headless | python scripts/fitness_store.py workout sync
[library_sync] 10 workouts found.
{"synced": 10, "workouts": [...]}
```

All 10 real library workouts (2 Peloton rides, 3 strength, 5 runs)
correctly upserted with real ids, sets, target distance/time, and
estimated load. Re-running `workout list --type cycle` correctly returned
just the two Peloton entries from the DB.

```
$ python scripts/fitness/schedule_ops.py list-week --headless
[schedule_ops] 35 days found.
```

The one real scheduled item on the calendar ("Ingebrigtsen Strength
Workout", Aug 18) came back with the correct `scheduled_id`, `sport_code:
"402"`, `sport_type: "strength"`, and `name`. Known gap: completed
(already-logged) activity cards use a different internal leaf order and
currently return the activity duration instead of a name in the `name`
field — harmless since this phase never writes to those, but noted in
`schedule_ops.py`'s docstring rather than fixed now (fixing it isn't
needed for anything Phase 3 does).

## Next (Phase 3, not yet built)

Calendar conflict-checking against Google Calendar, the propose→confirm
weekly-plan flow, `schedule_ops.py` write commands (drag-to-schedule an
existing library workout onto a day, build a custom workout via the
"Create Workouts" dialog for unmatched types), `fitness_weekly_plans`
writes, Google Calendar event creation, and the `fitness` subagent itself
— per the approved plan.
