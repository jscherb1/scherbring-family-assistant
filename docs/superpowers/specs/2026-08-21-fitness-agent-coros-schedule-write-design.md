# Fitness Agent — Phase 3a: Writing Existing Workouts to the Coros Schedule

## Context

Continuation of Phase 3 (the approved fitness-agent plan). This piece
covers just the "reuse an existing library workout onto a specific day"
write path — `schedule_ops.py add-existing-workout` /
`remove-workout`. Calendar-conflict checking, the propose→confirm flow,
custom-workout creation, Google Calendar events, and the `fitness`
subagent itself are still open (see "Next" below).

## Why this took a long investigation: drag-and-drop was abandoned

The obvious approach — replicate the drag-and-drop UI gesture that
schedules a workout — was built two different ways (Playwright mouse
simulation, and real OS-level input via the Claude Chrome extension) and
both were flaky: roughly 2 successes out of 8 attempts, failing silently
(no error, nothing scheduled). A hover-then-pause before the drag
improved but didn't reliably fix it. Given this was going to undermine
a weekly automation's reliability regardless of which mechanism drove it,
the write path was rebuilt on the real API instead.

## The real API

Network capture during a successful manual drag found the actual call
Coros's own frontend makes:

```
POST https://teamapi.coros.com/training/schedule/update
{
  "entities": [{"happenDay": "YYYYMMDD", "idInPlan": N, "sortNoInSchedule": M}],
  "programs": [<full program object>],
  "versionObjects": [{"id": N, "status": 1}],
  "pbVersion": 2
}
```

Key findings, each verified live rather than assumed:

- **`programs[0]`** is exactly the response body of
  `GET /training/program/detail?id=<coros_workout_id>` (minus the
  `exerciseBarChart` key, which the real client strips before posting)
  with `idInPlan` added. Not a guessed/reconstructed shape — real server
  data round-tripped with one field changed (`entities[0].happenDay`).
- **Auth**: plain cookies aren't enough — `credentials: 'include'` alone
  gets `{"result": "1019", "message": "Access token is invalid"}`. The
  missing piece is an `accessToken` header whose value is the
  `CPL-coros-token` cookie, forwarded explicitly. Once added, both
  `program/detail` and `schedule/update` work, and so does
  `GET /training/schedule/query`.
- **`idInPlan`** (mirrored into `versionObjects[0].id`) is a simple
  incrementing integer, not tied to the workout or the date (confirmed:
  two real captures on the same account were 36 and 37). Rather than
  track this locally (risk of drift if the user schedules something via
  the Coros app between fitness-agent runs), `schedule/query`'s response
  includes a `maxIdInPlan` field directly — the live current maximum — so
  `schedule_ops.py` queries it fresh before every write and uses
  `maxIdInPlan + 1`. Self-correcting, no local state to go stale.
- `schedule/query` needs `startDate`/`endDate` as `YYYYMMDD` and
  `supportRestExercise` as the *string* `"1"` (not `"true"` — that 500s
  with `{"result": "1001", "message": "Service exceptions"}`).

## What was built

- `scripts/fitness/schedule_ops.py`:
  - `add-existing-workout --date YYYYMMDD --coros-workout-id ID`: fetches
    the workout template, queries the live max `idInPlan`, POSTs the
    schedule update, reloads the page, and verifies a new card appeared
    on that day (card count increased — not a name match, since newly
    created cards render without the score-badge leaf that `list-week`'s
    name-parsing depends on).
  - `remove-workout --date YYYYMMDD --scheduled-id ID`: unchanged from
    Phase 2/early Phase 3 — this one was never flaky, so it stayed
    UI-driven (hover → trash icon → confirm dialog).
- The module's docstring documents the full "why API, not drag"
  reasoning and the auth/idInPlan mechanics for future maintenance.

## Verification

Ran live 2026-08-21 against the real account, twice, both clean:

```
$ python schedule_ops.py add-existing-workout --date 20260821 --coros-workout-id 444159851300569094
{"date": "20260821", ..., "scheduled": true, "cards_on_day": [{"scheduled_id": "479767468322242766", ...}]}
$ python schedule_ops.py remove-workout --date 20260821 --scheduled-id 479767468322242766
{"date": "20260821", "scheduled_id": "479767468322242766", "removed": true}

$ python schedule_ops.py add-existing-workout --date 20260822 --coros-workout-id 443953246468489216
{"date": "20260822", "name": "Lower Body Strength", "scheduled": true, ...}
$ python schedule_ops.py remove-workout --date 20260822 --scheduled-id 479767480670273946
{"date": "20260822", "scheduled_id": "479767480670273946", "removed": true}
```

Final `list-week` confirmed the calendar returned to exactly its
pre-test state (only the pre-existing real entries remain).

## Next (not yet built)

Calendar conflict-checking against Google Calendar, the propose→confirm
weekly-plan flow, `fitness_weekly_plans` writes, Google Calendar event
creation, custom-workout creation for unmatched types (still deferred per
the user's "reuse-only first" decision), and the `fitness` subagent
itself.
