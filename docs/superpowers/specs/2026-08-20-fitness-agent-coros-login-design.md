# Fitness Agent — Phase 1: COROS Login/Session Proof

## Context

First phase of the fitness planning agent (see the approved plan for the
full multi-phase design). Before any weekly-planning logic can be built,
we need proof that COROS Training Hub (t.coros.com) can be logged into
and its session reused via Playwright, following the same pattern already
proven for the Hy-Vee cart builder (`scripts/hyvee/`).

## What was discovered

Live exploration against t.coros.com (2026-08-20, via the Claude Chrome
extension against the user's real, already-authenticated session) found:

- **Login** is plain — no separate identity subdomain like Hy-Vee's
  Auth0. `t.coros.com/login` renders an Arco Design form directly and
  redirects back to `?lastUrl=` on success.
- Login fields have no `id`/`name` attributes (Arco Design generic
  inputs); they're matched by `type` + `placeholder` instead.
- A "I have read and agree to the COROS Privacy Policy" checkbox is
  **required** — the Login button stays disabled until it's checked. It's
  the second of two `.arco-checkbox` controls on the page (the first is
  "Remember me").
- The checkbox's real `<input>` is visually hidden (opacity 0); Arco
  renders the visible circle via CSS on the `.arco-checkbox` wrapper, so
  the **wrapper**, not the input, must receive the click.
- No CAPTCHA or MFA was shown on this account/login. `login_test.py`
  still defends against one appearing later (Coros may add friction for
  new devices, like Hy-Vee's known CAPTCHA gap) by detecting
  `iframe[src*='captcha']` / `[class*='captcha']` and failing with an
  explicit "re-run with --headed" message rather than a confusing
  timeout.
- The **Workouts** library, drag-to-schedule, the "+" add-menu, and
  workout deletion were all exercised live and confirmed working — that
  map is recorded in `scripts/fitness/coros_web.py` as findings (not yet
  turned into selectors, since the underlying CSS wasn't extracted via
  DOM inspection). That's the starting point for Phase 2.

## What was built

- `scripts/fitness/coros_web.py` — URLs + verified login selectors, plus
  the Phase 2 findings above as documentation.
- `scripts/fitness/coros_session.py` — `.env` loader, `SESSION_FILE` =
  `state/coros_session.json` (Playwright `storage_state`). Mirrors
  `scripts/hyvee/hyvee_session.py`.
- `scripts/fitness/login_test.py` — reuses a saved session if valid, else
  logs in fresh with `COROS_USERNAME`/`COROS_PASSWORD` from `.env`, checks
  the required agree-checkbox, detects CAPTCHA, and persists the session.
- `scripts/fitness/requirements.txt` — `playwright` (matches Hy-Vee).
- `.gitignore` — added `state/coros_session.json`,
  `scripts/fitness/last_error.png`, `scripts/fitness/_debug/`.

## Verification

Ran live 2026-08-20:

```
$ python scripts/fitness/login_test.py --headless
[login] Not signed in — logging in fresh.
[login] Navigating to login page...
[login] Session saved to .../state/coros_session.json
PASS: logged in and schedule page reachable.

$ python scripts/fitness/login_test.py --headless   # second run
[login] Reusing saved session — already signed in.
PASS: logged in and schedule page reachable.
```

Confirms both fresh login and session-reuse paths work end-to-end.

## Next (Phase 2, not yet built)

Turn the Workouts-panel/drag-to-schedule/delete findings in
`coros_web.py` into real selectors and a `library_sync.py` +
`schedule_ops.py`, per the approved fitness-agent plan.
