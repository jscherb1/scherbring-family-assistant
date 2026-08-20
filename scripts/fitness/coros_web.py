"""Single source of truth for COROS Training Hub site constants.

Every selector and URL the automation depends on lives here so that a site
change requires editing exactly one file.

Values were discovered live against t.coros.com (2026-08-20) via manual
browser exploration (see
docs/superpowers/specs/2026-08-20-fitness-agent-coros-login-design.md).

COROS Training Hub is built on Arco Design (`arco-*` classes are the
library's generic component classes, shared across the whole app — never
use one alone as a selector, always scope it). There's no separate
identity subdomain like Hy-Vee's Auth0 — login lives directly on
t.coros.com/login and redirects back to `?lastUrl=` on success.
"""

# --- URLs ---
LOGIN_URL = "https://t.coros.com/login"
SCHEDULE_URL = "https://t.coros.com/admin/views/schedule"

# --- Selectors: login page (t.coros.com/login) ---
# Verified live 2026-08-20 via login_test.py. Arco Design inputs have no
# id/name attributes, so these match on type + placeholder, which were
# stable and unique on the page.
SELECTORS = {
    "login_email": "input[type='text'][placeholder='E-mail']",
    "login_password": "input[type='password']",
    # Two `.arco-checkbox` controls exist on the form, in DOM order:
    # index 0 = "Remember me", index 1 = the required "I have read and
    # agree to the COROS Privacy Policy" box. The underlying
    # `.arco-checkbox-target` input is visually hidden (opacity 0) — Arco
    # renders the visible circle via CSS on the `.arco-checkbox` wrapper,
    # so the wrapper (not the input) must be clicked. The Login button
    # stays disabled (arco-btn-disabled) until index 1 is checked. Scripts
    # should locate `login_agree_checkbox` and call `.nth(1).click()`.
    "login_agree_checkbox": ".arco-checkbox",
    "login_submit": "button.arco-btn-primary[type='submit']",
}

# --- Schedule page (t.coros.com/admin/views/schedule) ---
# Discovered and exercised manually 2026-08-20 (workout library panel,
# drag-to-schedule, delete, and the "+" add-menu all confirmed working),
# but the underlying CSS classes were not yet extracted via DOM inspection
# — that + the "Create Workouts" custom-workout builder form fields are
# scoped to Phase 2 (library sync + read-only schedule inspection), not
# this Phase 1 (login/session) delivery. Recorded here as findings, not
# selectors, so Phase 2 starts from a known map instead of re-discovering:
#
# - A toggle button in the top-right icon rail above the calendar grid
#   (no visible label, hover tooltip reads "Workouts") opens a right-hand
#   panel listing reusable workout cards: name, type icon, sets, target
#   time/distance/load, a "Search by name" box, a type filter dropdown
#   ("All"), and a "+ Create Workouts" button at the bottom.
# - Dragging a workout card from that panel onto a calendar day cell
#   schedules it immediately (confirmed via weekly-stats update); this is
#   the primary "reuse an existing workout" mechanism.
# - Hovering an empty day cell reveals a "+" that opens an "Add" menu with
#   three options: "Quick Training", "Workouts" (same library, scoped to
#   that day), "Events".
# - "+ Create Workouts" opens a builder dialog: Training Type dropdown
#   (Run, Trail Run, Bike, Swim, Strength, Hybrid Fitness, Indoor Climb,
#   Bouldering, XC Ski), Workout Name, Intensity Type dropdown, Description,
#   a drag-and-drop interval/structure block builder, Add/Cancel/Save.
# - Hovering a scheduled workout card on the calendar reveals delete
#   (trash) and copy icons; delete prompts a "Delete — Are you sure you
#   want to delete this workout?" Cancel/OK confirmation.
# - Peloton rides already exist as first-class library workouts
#   ("Peloton - 45 min", "Peloton - 30 Min"), confirming the user's
#   Peloton-in-Coros requirement is directly supported.
# - "View plan" (separate from the "Workouts" panel) opens a distinct
#   "Training Plan" concept (search a library of full multi-week programs,
#   or "+ Create a training plan") — not used by this feature; per-workout
#   reuse goes through the "Workouts" panel instead.
