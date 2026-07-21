# Hy-Vee Cart Builder — Decision Model, Learning Loop & Item Format — Design

## Context

Phase 1 proved we can autonomously log into Hy-Vee Aisles Online and add items to the cart
(`scripts/hyvee/login_test.py`). A read-only discovery spike
(`docs/superpowers/specs/2026-07-21-hyvee-data-discovery.md`) then mapped what data Hy-Vee
exposes: two clean JSON APIs (paginated **purchase history** and a **getActiveCart** GraphQL
endpoint) plus rich per-product fields (productId, UPC, name, size, price, unit price, sale
price, promo badges, `isBuyAgain`, `sponsored`). Brand is not structured — so we "remember"
products by exact productId/UPC, not brand strings.

This spec designs the layer that turns a Todoist shopping list into a **built (never placed)**
Hy-Vee cart: the decision model that resolves each list item to a specific product, the
learning loop that improves those decisions from user feedback, and the shared shopping-list
item format that lets the meal-planner attach structured detail up front so downstream matching
is easy rather than guessed.

Goals (confirmed with the user):
- Seed matching automatically from ~80 orders of purchase history; improve over time.
- Auto-add confident matches; flag ambiguous ones; **feedback tunes confidence** every run.
- Structure items at add-time (via meal-planner / manual add) to reduce downstream ambiguity.
- The subagent can ask the user for clarification — both at add-time and during cart build.

## Scope

**In scope:**
- Shared Todoist shopping-list item format (the agent contract).
- `hyvee` subagent + `scripts/hyvee_store.py` (stdlib-only CLI) + schema tables, following the
  repo's per-feature convention (see `scripts/lawn_garden_store.py`, `scripts/profile_store.py`).
- Purchase-history sync, item→product resolution, confidence/learning, cart build + verify.
- Integration touchpoints: meal-planner and todoist subagents adopt the item format and can
  ask clarifying questions at add-time.

**Out of scope (later):**
- Placing/checking out an order — never, in any phase.
- Multi-store price comparison across retailers.
- Per-line historical quantity/price (needs more API mapping; not required for v1).

## Shared shopping-list item format (agent contract)

Todoist tasks carry a clean human title plus **optional structured hints in the task
description** as simple `key: value` lines. Humans and agents both stay readable; the cart
builder parses what's present and falls back to learned prefs/history for the rest.

```
Title:        Milk
Description:  item: milk
              brand: Kemps
              size: 0.5 gal
              product_id: 4160380      # optional, most specific
              qty: 2
              note: whole, not 2%
```

- Only `item` is required (defaults to the title if absent). Everything else is optional.
- `product_id` (or `upc`) is the strongest hint — an exact match, no resolution needed.
- The meal-planner populates as much as it confidently can when adding items; it asks the user
  only when a detail is genuinely ambiguous and worth pinning (see Interaction Model).

## Architecture

One subagent, one store CLI, tables in the shared SQLite DB. **All AI usage is `model: sonnet`**
per repo policy.

- **`scripts/hyvee_store.py`** — zero-dependency stdlib/argparse/JSON-out CLI (repo convention).
  Owns all DB reads/writes: history rows, prefs, feedback, run logs, and confidence math.
  No browser logic here.
- **`scripts/hyvee/` browser layer** (extends Phase-1 work) — Playwright automation:
  `sync_history` (pull the purchase-history API), `search` (query + extract candidate products,
  dropping sponsored), `add_to_cart` (by productId), `verify_cart` (getActiveCart GraphQL).
  Emits JSON; persists the logged-in session (`state/hyvee_session.json`).
- **`.claude/agents/hyvee.md`** — the subagent that orchestrates: reads the Todoist list,
  calls the store to resolve items, drives the browser layer to add + verify, asks the user for
  clarification when needed, and writes feedback back through the store.

## Data model (new tables in `state/schema.sql`)

- `hyvee_purchase_history` — synced snapshot: `upc`, `product_name`, `order_date`,
  `purchase_id`, `synced_at`. Source for frequency/recency.
- `hyvee_item_prefs` — the learned map, one row per normalized `item` key:
  `preferred_product_id`, `preferred_upc`, `product_name`, `size`,
  `confidence` (0.0–1.0), fallback weights (`pref_brand`, `max_price`, `prefer_on_sale`,
  `pref_size`), `source` (history|user|mealplan), `times_confirmed`, `times_rejected`,
  `updated_at`.
- `hyvee_feedback_log` — append-only: `ts`, `item`, `proposed_product_id`, `action`
  (accepted|rejected|substituted), `chosen_product_id`, `note`. Auditable driver of confidence.
- `hyvee_cart_runs` — per build: `ts`, `items_json` (list in), `resolved_json`
  (item → product, auto vs flagged), `cart_verified` (bool), `summary`.

## Resolution: Todoist item → product

For each list item:
1. **Parse** the structured description; normalize `item` to a key ("2% milk" → `milk`).
2. **Exact hint** — if `product_id`/`upc` present, use it directly (confidence = 1.0).
3. **Learned pref** — else look up `hyvee_item_prefs`. If `confidence ≥ AUTO_THRESHOLD`,
   **auto-add** the preferred product.
4. **Resolve** — else `search` the item, **drop sponsored**, rank candidates by:
   purchase-history match (UPC/name in `hyvee_purchase_history`) → matches `pref_brand` →
   on-sale → lowest unit price. Present the top pick as **flagged** (needs confirmation).
5. Record the outcome to `hyvee_feedback_log` and update `hyvee_item_prefs`.

## Learning loop

- **Seed (one-time):** mine `hyvee_purchase_history` — for each recognizable item key, the
  most-frequent-and-recent product becomes the initial mapping. Starting confidence scales with
  buying consistency (bought 8 of last 10 → high; one-off → low).
- **Adjust (every run):** `accepted` → confidence↑ and `times_confirmed`++; `rejected`/
  `substituted` → confidence↓, `times_rejected`++, and `preferred_product_*` updates to the
  product the user actually chose. Items cross `AUTO_THRESHOLD` into auto-add after consistent
  confirmations; a substitution can drop them back to flagged.
- Thresholds (`AUTO_THRESHOLD`, step sizes) live as constants in `hyvee_store.py`, tunable.

## Interaction / clarification model

The subagent asks the user for clarification in two moments, kept low-friction:

- **At add-time (meal-planner / manual add):** when adding a genuinely new or ambiguous item
  and no confident history/pref exists, ask a short question and write the answer into the
  Todoist item's structured description. Known staples are added silently. This front-loads
  detail so cart build is smooth.
- **During cart build:** confident matches auto-add with no prompt. Flagged items are collected
  and the user is asked to confirm/substitute in a single batch (not one prompt per item).
  Answers are written back as feedback so the same question isn't needed next time.

## Build-only guarantee

The final step calls `verify_cart` (getActiveCart) and returns a review summary (each item →
chosen product, price, auto/flagged, cart total). The flow **never** navigates to checkout or
places an order — in this phase and all future phases.

## Integration touchpoints (other agents)

- **meal-planner** (`.claude/agents/meal-planner.md`): when it adds groceries to Todoist, write
  the structured description format above, and ask the user for a clarifying detail only when it
  materially improves matching. It already adds a grocery list after confirmation — this extends
  that step.
- **todoist** (`.claude/agents/todoist.md`): manual adds to the shopping list may also carry the
  format; no behavior change required, but it should preserve the description on updates.

## Build sequence (for the implementation plan)

1. Schema tables + `hyvee_store.py` CRUD + confidence math (unit-testable, no browser).
2. Browser layer: `sync_history`, `search`, `add_to_cart`, `verify_cart` (extend `scripts/hyvee/`).
3. History seed: sync + build initial `hyvee_item_prefs`; sanity-check against known staples.
4. `hyvee` subagent orchestration: resolve list → auto-add/flag → clarify → verify → feedback.
5. meal-planner/todoist item-format adoption + add-time clarification.

## Verification

- Store CLI: unit-style checks — seed from a sample history JSON, assert prefs/confidence,
  simulate accept/reject and assert confidence moves and threshold graduation.
- Browser layer: `sync_history` returns real orders; `search milk` returns non-sponsored
  candidates; `add_to_cart <productId>` increments the cart; `verify_cart` lists it.
- End-to-end (real account, build-only): put a few structured items on a test Todoist list,
  run the subagent, confirm confident items auto-add, a flagged item prompts once, the cart
  verifies, and feedback updates prefs. Manually confirm the cart on Hy-Vee; never place it.
