# Kitchen Display Webpage — Modernize Design

## Context

The "Kitchen Display Webpage" HA dashboard (`kitchen-display-webpage`) was just converted
to a hardcoded dark theme. The user now wants it to look more modern/sleek while staying
dark, and wants the three room-thermostat cards shrunk since they only need room, temp,
and on/off — freeing space for the calendar/meal-plan cards to grow.

## Style changes

- Single accent color `#5b8def` (soft blue) applied via `--accent-color` /
  `--primary-color` and used for icons, the calendar's "today" highlight, and the
  progress bar on the meal-plan card, instead of each card's mismatched default colors.
- `--ha-card-border-radius: 16px` (up from HA default ~12px) for a softer, more modern
  card shape.
- Add a subtle `box-shadow: 0 2px 8px rgba(0,0,0,0.4)` alongside the existing soft
  border, so cards read as slightly "raised" rather than purely outlined.
- Remove `text-decoration: underline` on the title card; keep bold/xx-large, add
  `letter-spacing: 0.5px` instead.
- `grid-gap` 10px → 14px for more breathing room between cards.

## Layout changes

- Replace the three `custom:mushroom-climate-card` thermostat cards (full controls,
  ~100px row) with plain `tile` cards: icon + room name + state only, no features/sliders.
  This matches HA's own best-practice guidance (tile is the modern default over legacy
  climate cards) and directly satisfies "just need room, temp, and on/off."
- Shrink the thermostat row: `100px → 56px`.
- Grow header row slightly: `50px → 60px` (the title card's padding added a couple of
  weeks ago needs a bit more room to not feel cramped).
- Give the freed height to the weather/calendar row: `250px → 284px`, so
  `atomic-calendar-revive` (family calendar + meal plan) get more visible rows before
  scrolling/truncating.
- New row total: 60 + 150 + 284 + 56 = 550px (unchanged from current 550px total, so
  the overall dashboard footprint on the Google TV screen doesn't shift).
- No data sources added or removed in this pass — user wants to see how much room is
  freed up first before deciding on new content (deferred, not in scope here).

## Undo plan

Full current dashboard config and the current dark-mode dashboard resource script are
saved to `docs/superpowers/specs/dashboard-backups/` before any change:
- `kitchen-display-webpage-2026-08-24-before-modernize.json`
- `kitchen-display-webpage-dark-override-resource-2026-08-24.js`

To revert: pass the JSON file's contents as `config=` to
`ha_config_set_dashboard(url_path="kitchen-display-webpage", config=...)`, and re-push
the `.js` file's contents as the content of dashboard resource
`1d94f2ad3048402c98dc8d30b2b4df7d` via `ha_config_set_dashboard_resource`.

## Out of scope

- No new data sources (security strip, etc.) — explicitly deferred by the user pending
  seeing the freed space.
- No changes to the `scherbring-family` Cast dashboard — this only touches the plain
  kiosk-browser webpage dashboard.
