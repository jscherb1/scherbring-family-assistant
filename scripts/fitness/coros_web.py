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
TEAMAPI_BASE = "https://teamapi.coros.com"

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
# Verified live 2026-08-20 via DOM inspection (see coros_web.py history /
# the Phase 2 design doc for how these were found).
SCHEDULE_SELECTORS = {
    # Top-right icon-rail toggle that opens the Workout Library panel. No
    # visible label — a hover tooltip reads "Workouts". Toggling it closed
    # is the same selector (it's a stateful open/close button).
    # Two `.side-panel-toggle-btn` icons exist ("Workouts" and "Training
    # Plans" / "View plan") — must be disambiguated by their text.
    "workouts_panel_toggle": ".side-panel-toggle-btn:has-text('Workouts')",
    # Root of the opened panel.
    "workouts_panel": ".training-group",
    "workouts_panel_search": ".training-group input[placeholder='Search by name']",
    # The search box only actually filters the card list once the
    # magnifying-glass suffix icon is clicked — typing alone (even with an
    # Enter keypress) leaves the list unfiltered. Verified live 2026-08-21.
    # Click the `.arco-icon-hover` wrapper span, not the inner <svg> —
    # the svg's class list is inconsistent (sometimes carries
    # `arco-icon-search`, sometimes not) and isn't the actual click target.
    "workouts_panel_search_icon": ".training-group .arco-input-search .arco-icon-hover",
    "create_workout_button": ".training-group button:has-text('Create Workouts')",
    # One draggable card per library workout. Carries `data-id` (Coros's
    # numeric workout id) and `data-name` directly as DOM attributes —
    # no need to parse them out of text. The sport-type icon is a
    # `.iconfont-sport.icon-<slug>` element inside the card (slugs seen so
    # far: 'outrun' = Run, 'strength' = Strength, 'cycle' = Bike/Peloton).
    "workout_card": ".training-group-item.card-dragable",
    # The drag MUST start from this small grip handle (top-left of the
    # card) — starting a mousedown anywhere else on the card body does not
    # register as a drag (verified live 2026-08-21: dragging from the card
    # center left no drag-preview and dropped nothing).
    "workout_card_drag_handle": ".cursor-move",
    "workout_card_sport_icon": "[class*='iconfont-sport']",

    # Calendar grid day cells — `data-weekindex` (0-2, the 3 visible weeks)
    # and `data-dayindex` (0-6, Mon-Sun) locate a specific cell within the
    # *currently displayed* 3-week window; there is no absolute-date
    # attribute, so navigating to the right week first (via the date
    # textbox) and reading the visible day-number text is required to
    # target a specific calendar date.
    "calendar_day_cell": ".calender-day",
    "calendar_date_picker": "input[placeholder='Please select date']",

    # "+" add-menu that appears on hovering an empty calendar day.
    "add_menu_workouts_option": "text=Workouts",

    # Scheduled workout card actions (visible on hover): trash (delete)
    # and copy icons, in that order, via iconfont classes containing the
    # Chinese words for "delete" (删除/shanchu) and "copy" (复制/fuzhi).
    "scheduled_card_delete_icon": "[class*='iconicon_jichutubiao_shanchu']",
    # Scoped to the delete-confirm modal specifically — an unscoped
    # `button:has-text('OK')` matches multiple OK buttons elsewhere on the
    # page (e.g. the Week Events modal), some of which are hidden and hang
    # a bare `.click()`/`wait_for_selector` on visibility.
    "delete_confirm_ok": ":text('Are you sure you want to delete this workout?') >> xpath=ancestor::*[contains(@class,'arco-modal')] >> button:has-text('OK')",
}

# --- Findings not yet turned into selectors (scoped to a later phase) ---
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
