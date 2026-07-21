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
PRODUCT_PAGE_TMPL = "https://www.hy-vee.com/aisles-online/p/{product_id}"
# The cart *review* page (view/adjust quantities before deciding to check
# out) — despite the "checkout" segment in its path, this is not the
# payment/checkout flow itself. cart_ops.py navigates here to verify cart
# contents but never clicks through to payment.
CART_PAGE_URL = "https://www.hy-vee.com/aisles-online/checkout/cart"

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
    # `product_card` (data-testid='UniversalProductCard') only matches the
    # small recommendation/carousel swimlane above the results grid (verified
    # live 2026-07-21 while building cart_ops.py: on a "milk" search it
    # matched 3 unrelated carousel items — Haagen-Dazs ice cream, cold brew
    # coffee — never the ~30-per-page paginated grid of actual milk
    # products). The real search-results grid renders the *same* internal
    # component markup but without that top-level testid; its card root is
    # reliably identified as the ancestor of the add-to-cart button's
    # `primary-action-component` wrapper. Use this selector for anything
    # that needs the actual result set (cart_ops.py `search`); `product_card`
    # is kept for the carousel / diagnose.py compatibility.
    "search_result_card": "div:has(> [data-testid='primary-action-component'])",
    # Cart line items (`/aisles-online/checkout/cart` — the cart *review*
    # page, not a payment/checkout step) reuse the exact same card-root
    # component/class as search results and the carousel, but with an
    # `incrementer-input` quantity control instead of an add-to-cart button.
    # Verified live 2026-07-21: matched exactly the 3 items actually in the
    # test cart.
    "cart_line_item": "[class*='OuterWrapper']",
    "cart_line_qty": "[data-testid='incrementer-input']",
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
        # The real search-results grid (see search_result_card comment above)
        # — `product card` above only covers the 3-item recommendation
        # carousel and would stay green even if the actual results grid the
        # cart-builder depends on broke. Both checks are kept: they verify
        # two distinct, real page elements.
        "name": "search result card",
        "kind": "selector",
        "url": SEARCH_URL_TMPL.format(term="milk"),
        "selector": SELECTORS["search_result_card"],
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
        # POST GraphQL endpoint; its real query body was never built —
        # Task 8's `cart_ops.py verify-cart` reads the cart via the
        # cart-page DOM (CART_PAGE_URL) instead of replaying this GraphQL
        # call. This check is intentionally lenient — it only confirms the
        # getActiveCart endpoint still exists (anything but a 404 counts as
        # PASS); it does not exercise the real query/response shape.
        "name": "getActiveCart API",
        "kind": "api",
        "url": GET_ACTIVE_CART_API,
        "method": "post",
    },
]
