"""Single source of truth for Hy-Vee (Aisles Online) site constants.

Every selector, URL, and API endpoint the automation depends on lives here so
that a site change requires editing exactly one file. This module is pure
constants — importing it must never have side effects (no network calls, no
browser launches).

Values were discovered against the live site (2026-07) via
`scripts/hyvee/login_test.py` (Phase 1: login + add-to-cart) and the
read-only data-discovery spike
(`docs/superpowers/specs/2026-07-21-hyvee-data-discovery.md`).

When the site breaks, see `scripts/hyvee/README.md` for the diagnose-and-fix
workflow.
"""

# --- URLs ---
HOME_URL = "https://www.hy-vee.com/"
LOGIN_URL = "https://www.hy-vee.com/main/login"
SEARCH_URL_TMPL = "https://www.hy-vee.com/aisles-online/search?search={term}"

# --- API endpoints ---
PURCHASE_HISTORY_API = (
    "https://www.hy-vee.com/aisles-online/api/purchase-history/online"
    "?page={page}&pageSize=20"
)
GET_ACTIVE_CART_API = (
    "https://www.hy-vee.com/aisles-online/api/graphql/three-legged/getActiveCart"
)

# --- Selectors ---
SELECTORS = {
    "cookie_accept": "#onetrust-accept-btn-handler",
    "username": "#username",
    "password": "#password",
    "mfa": (
        "input[autocomplete='one-time-code'], input[name*='code' i], "
        "input[id*='code' i]"
    ),
    "login_link": "[data-testid='global-navigation-login']",
    "cart_icon": "[data-testid='global-navigation-cart-icon-button']",
    "cart_bubble": "[data-testid='global-navigation-cart-bubble']",
    "search_input": "[data-testid='global-navigation-search-input']",
    "add_to_cart": "[data-testid='add-to-cart-button']",
    "product_card": "[data-testid='UniversalProductCard']",
    "sponsored": "[data-testid='sponsored-text']",
    # Not reliably populated on search cards (see discovery doc) — kept for
    # completeness/inspection; prefer productId/UPC over brand strings.
    "brand": "[data-testid='BRAND']",
    "unit_of_measure": "[class*='UnitOfMeasure']",
    "price_amount": "[data-testid='product-card-price-amount']",
    "is_buy_again": "[data-testid='isBuyAgain']",
}

# Each dependency the automation relies on, for `diagnose.py check`.
CRITICAL_CHECKS = [
    {
        "name": "login form",
        "kind": "selector",
        "url": LOGIN_URL,
        "selector": SELECTORS["username"],
    },
    {
        "name": "search input",
        "kind": "selector",
        "url": HOME_URL,
        "selector": SELECTORS["search_input"],
    },
    {
        "name": "product card",
        "kind": "selector",
        "url": SEARCH_URL_TMPL.format(term="milk"),
        "selector": SELECTORS["product_card"],
    },
    {
        "name": "add-to-cart button",
        "kind": "selector",
        "url": SEARCH_URL_TMPL.format(term="milk"),
        "selector": SELECTORS["add_to_cart"],
    },
    {
        "name": "cart bubble",
        "kind": "selector",
        "url": HOME_URL,
        "selector": SELECTORS["cart_bubble"],
    },
    {
        "name": "purchase-history API",
        "kind": "api",
        "url": PURCHASE_HISTORY_API.format(page=1),
        "expect_key": "purchaseGroups",
    },
    {
        # POST GraphQL endpoint; its real query body isn't built until Task 8
        # (cart_ops.py). This check is intentionally lenient — it only
        # confirms the endpoint exists (anything but a 404 counts as PASS).
        # Task 8's `cart_ops.py verify-cart` exercises the real contract
        # with the actual GraphQL query/response shape.
        "name": "getActiveCart API",
        "kind": "api",
        "url": GET_ACTIVE_CART_API,
        "method": "post",
    },
]
