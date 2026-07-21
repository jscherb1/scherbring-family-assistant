# Hy-Vee automation

Playwright-based automation for Hy-Vee Aisles Online: signing in, adding
items to cart, and (in later tasks) building a cart from a shopping list.

## Files

- `hyvee_web.py` — single source of truth for every URL, API endpoint, and
  CSS/data-testid selector the automation depends on. Pure constants, no
  side effects on import.
- `login_test.py` — Phase 1 proof: log in (session reuse via
  `state/hyvee_session.json`) and add one test item to cart.
- `diagnose.py` — diagnostics CLI (see below).

## When the site breaks

Hy-Vee can change selectors, URLs, or API shapes without notice. If
`login_test.py` or the cart-builder starts failing:

1. **Run the health check** to find which dependency broke:

   ```
   python scripts/hyvee/diagnose.py check
   ```

   This walks every entry in `hyvee_web.CRITICAL_CHECKS` (login form, search
   input, product card, add-to-cart button, cart bubble, purchase-history
   API, getActiveCart API) and prints a PASS/FAIL table. It exits non-zero
   if anything fails.

2. **Investigate the failing dependency** with the matching subcommand:

   - Selector broke (e.g. "product card", "add-to-cart button") →

     ```
     python scripts/hyvee/diagnose.py inspect --url <page-url>
     ```

     Dumps every visible input/button/link plus every `data-testid` on the
     page to JSON, plus a full-page screenshot, under `scripts/hyvee/_debug/`.

   - API endpoint/shape changed →

     ```
     python scripts/hyvee/diagnose.py capture-api --url <page-url> --match <keyword>
     ```

     Records JSON network responses whose URL contains `<keyword>` (e.g.
     `purchase-history`) to `scripts/hyvee/_debug/captured_api.json`.

   - Product-card fields (price, size, UPC, etc.) look wrong →

     ```
     python scripts/hyvee/diagnose.py dump-card --term milk
     ```

     Extracts the first product card's outerHTML and the fields the
     cart-builder relies on (productId, upc, name, brand, size, price,
     sponsored, isBuyAgain, badges).

3. **Fix the one constant** in `hyvee_web.py` (a selector string, a URL
   template, or an API path) — everything else (`login_test.py`,
   `diagnose.py`, and the cart-builder scripts) imports from there, so a
   single edit propagates everywhere.

4. **Re-run `diagnose.py check`** to confirm all PASS, then re-run
   `login_test.py --headless` to confirm the end-to-end flow still works.

All browser subcommands default to headless; pass `--headed` to watch them
run.
