# Hy-Vee Login + Add-to-Cart Test (Phase 1) — Design

## Context

The long-term goal is a "shopping cart builder": take the Todoist shopping list, apply learned item/brand preferences, and autonomously build (but never submit) a Hy-Vee online grocery order for the user to review and place. That full system needs several pieces — browser automation against Hy-Vee, a preference/history store, Todoist integration, and eventually price/store comparison.

Before building any of that, we need to prove the riskiest unknown first: can we reliably log into the user's real Hy-Vee account and add an item to the cart via automation at all? Hy-Vee's site could have anti-bot protections, unfamiliar 2FA behavior, or a fragile DOM that breaks selectors. This phase isolates that risk into a small, disposable test script before investing in the full feature (store schema, subagent, preference logic).

This repo currently has no browser-automation tooling and is otherwise dependency-free Python (stdlib only) driving a shared SQLite DB per `state/schema.sql`, with one subagent + one `scripts/<name>_store.py` per feature. This phase deliberately does NOT follow that full convention yet — it's a standalone diagnostic script, not a feature. The subagent/store/schema work comes in a later phase once login + add-to-cart is proven out.

## Scope

**In scope for this phase:**
- Logging into hyvee.com with the user's credentials via Playwright (Python), headed (visible browser).
- Persisting the logged-in session locally so repeat runs don't need to log in every time.
- Handling the possibility of a 2FA/MFA prompt by pausing for manual input (unknown today whether the account has this).
- Searching for one hardcoded test item (e.g. "milk"), adding the first result to the cart, and confirming it landed there.
- Screenshotting on failure for debugging.

**Explicitly out of scope for this phase** (later phases):
- Reading the Todoist shopping list.
- Any preference/brand/history matching logic.
- A `hyvee_store.py`, SQLite schema tables, or a `.claude/agents/hyvee-*.md` subagent.
- Price comparison across stores.
- Ever submitting/placing the order — this system only ever builds a cart for the user to review and place manually, in this phase and all future phases.

## Design

### Folder/file layout
- `scripts/hyvee/` — new folder, isolated from the rest of the (dependency-free) `scripts/` folder
  - `requirements.txt` — pins `playwright` (first non-stdlib Python dependency in the repo, scoped to this folder only)
  - `login_test.py` — the test script

### Credentials
- `HYVEE_USERNAME` / `HYVEE_PASSWORD` added to `.env.example` as blank placeholders, with real values in the user's local (gitignored) `.env`, matching the existing pattern for keeping secrets out of git.

### Session persistence
- After a successful login, Playwright's `storage_state` (cookies/localStorage) is saved to `state/hyvee_session.json`, gitignored alongside the existing `*.db`/`*.token` exclusions.
- On each run: if `state/hyvee_session.json` exists, load it and check whether the site still treats the browser as logged in (e.g. an account-page element is visible). Only fall back to a fresh username/password login if that check fails or the file doesn't exist.

### Login flow
1. Launch Chromium headed (visible window), optionally loading the saved session.
2. If not already logged in, navigate to Hy-Vee's sign-in page and submit username/password from the `.env` values.
3. If a 2FA/MFA prompt appears (unconfirmed whether the account has this), the script pauses and prompts the user in the terminal to type the received code, then submits it. There is no automated code retrieval (no email/SMS integration) — manual entry is intentional for this phase.
4. On success, save `storage_state` to `state/hyvee_session.json`.

### Add-to-cart test
1. Search for a hardcoded term (`"milk"` by default, easy to change at the top of the script).
2. Click "Add to cart" on the first product result.
3. Re-check the cart (e.g. cart icon count or cart page) to confirm the item was added.
4. Print a clear pass/fail result to the console. Never proceed to checkout/place-order.

### Error handling
- On any unexpected state (missing selector, unexpected popup, login failure), take a screenshot saved to `scripts/hyvee/last_error.png` (gitignored) and raise/print a clear error. No retry logic — this is a diagnostic script; a clear failure plus screenshot is sufficient to debug and iterate on selectors.

### Verification
- Run `python scripts/hyvee/login_test.py` locally.
- Watch the browser window perform login and add-to-cart.
- Manually confirm on Hy-Vee's site that the test item is present in the cart.
- Re-run a second time to confirm session reuse (no second login) works when the session file is already present.

## Follow-on phases (not part of this design)
1. Build the real `hyvee` subagent + `hyvee_store.py` + schema tables for remembered item/brand preferences and purchase history, following the repo's standard feature convention.
2. Wire in the Todoist shopping list as the source of items to add.
3. Add preference-based decision logic (brand vs. cost vs. promotion defaults) for items with multiple options.
4. (Much later) price/store comparison across retailers.
