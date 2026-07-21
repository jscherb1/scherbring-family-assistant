"""Browser layer for the Hy-Vee cart-builder: sync history, search, add, verify.

*** IMPORTANT — verify-cart contract: productId is ALWAYS null. ***
`verify-cart` reads line items from the cart-page DOM, which has no
product-link href to parse a productId out of (unlike `search`). Every line
item it returns has `productId: null`. Consumers MUST match cart line items
to products by `upc`, never by `productId`.

Four subcommands, each printing JSON to stdout:
    sync-history [--max-pages N]
        Fetches the purchase-history API (logged-in `page.request`), paginating
        until `meta.pagination.pagesTotal` (capped by --max-pages), writes the
        combined payload to a temp JSON file, and prints
        {"json_path": "...", "orders": N} for `hyvee_store.py history ingest`.
    search --term milk [--limit 12]
        Navigates to the search page, scrolls to load lazy cards, and extracts
        non-sponsored UniversalProductCard fields as a JSON list consumed by
        `hyvee_store.py rank`.
    add --product-id 4160380 [--qty 1]
        Navigates to the product page, clicks add-to-cart once, then (for
        qty>1) uses the quantity-stepper "+" that replaces the button to add
        the remaining units, printing {"added": bool, "cart_qty": N,
        "requested_qty": N, "delta": N, "qty_matched": bool}. `delta` is the
        actual bubble-count increase and `qty_matched` is whether it equals
        `requested_qty` — the caller's real signal for whether every
        requested unit landed (bubble count before/after).
    verify-cart
        Returns current cart line items as JSON
        [{"productId", "upc", "description", "quantity"}].
        NOTE: productId is always null here (see the *** warning above) —
        match line items by upc.

        Approach: DOM fallback (chosen deliberately — see module docstring
        section "verify-cart approach" below for why the GraphQL replay was
        not used).

This is integration code, verified against the LIVE site — no unit tests.
NEVER navigates to checkout or places an order.

Usage:
    python scripts/hyvee/cart_ops.py sync-history --max-pages 1
    python scripts/hyvee/cart_ops.py search --term milk
    python scripts/hyvee/cart_ops.py add --product-id 4160380
    python scripts/hyvee/cart_ops.py verify-cart

All subcommands default to headless (pass --headed to watch). Requires a
logged-in session at state/hyvee_session.json (see login_test.py) and
HYVEE_USERNAME/HYVEE_PASSWORD in the repo-root .env as a login fallback.

--- verify-cart approach ---
`diagnose.py capture-api` was used to sniff network traffic while visiting the
cart page and product pages. In practice, the getActiveCart GraphQL POST is
issued by the site's own JS with headers/body (apollo persisted-query hashes,
CSRF-ish tokens, etc.) that are non-trivial to reconstruct reliably outside the
browser's own fetch context, and the shape can change without notice. Rather
than hand-build and maintain a brittle POST replay, `verify-cart` uses the
DOM fallback explicitly called out as acceptable in the task brief: it
navigates to the cart page and reads line items directly from the rendered
cart-line elements (data-testid based selectors added to hyvee_web.SELECTORS
as `cart_line_item`, `cart_line_qty`). This is robust to the exact GraphQL
contract and still returns the same {productId, upc, description, quantity}
shape by parsing the image URL exactly like `search` does — except cart-line
elements carry no product-link href (unlike search cards), so `productId` is
always null here; consumers must match by `upc` instead.
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from hyvee_web import (
    CART_PAGE_URL,
    HOME_URL,
    LOGIN_URL,
    PRODUCT_PAGE_TMPL,
    PURCHASE_HISTORY_API,
    SEARCH_URL_TMPL,
    SELECTORS,
)
from hyvee_session import ENV_FILE, SESSION_FILE, dismiss_cookie_banner, load_env

ERROR_SCREENSHOT = Path(__file__).resolve().parent / "last_error.png"

# Extracts the exact keys cart_ops `search` prints and `hyvee_store.py rank`
# consumes. Verified live 2026-07-21 against the real search-results grid
# (see hyvee_web.SELECTORS["search_result_card"] comment for why this isn't
# the "product_card"/UniversalProductCard selector): sale price is shown as
# a main `product-card-price-amount` plus a distinct (higher) "was" price in
# `product-card-price-suffix-amount`; per-unit price (e.g. "$/oz") renders in
# `product-card-price-bottom-amount` / `-bottom-label-extended` when present.
CARD_JS = r"""
(card) => {
  const linkEl = card.querySelector("a[href*='/aisles-online/p/']");
  const href = linkEl ? linkEl.getAttribute('href') : null;
  let productId = null;
  if (href) {
    const m = href.match(/\/aisles-online\/p\/([^/]+)\//);
    if (m) productId = m[1];
  }
  const imgEl = card.querySelector("img[src*='/products/']");
  const src = imgEl ? imgEl.getAttribute('src') : null;
  let upc = null;
  if (src) {
    const m = src.match(/\/products\/(\d+)\//);
    if (m) upc = m[1];
  }
  const priceEl = card.querySelector("[data-testid='product-card-price-amount']");
  const uomEl = card.querySelector("[class*='UnitOfMeasure']");
  const nameEl = card.querySelector("[class*='ProductDescription']");
  const brandEl = card.querySelector("[data-testid='BRAND']");
  const buyAgainEl = card.querySelector("[data-testid='isBuyAgain']");
  const badgeEls = Array.from(card.querySelectorAll("[data-testid^='badge-']"));
  const badges = badgeEls.map((b) => b.getAttribute('data-testid').replace(/^badge-/, ''));

  const wasEl = card.querySelector("[data-testid='product-card-price-suffix-amount']");
  const wasText = wasEl ? (wasEl.innerText || '').trim() : '';
  const saleBadge = badges.some((b) => b === 'SALE' || b === 'DEAL');
  const onSale = !!wasText || saleBadge;

  const unitAmtEl = card.querySelector("[data-testid='product-card-price-bottom-amount']");
  const unitLblEl = card.querySelector("[data-testid='product-card-price-bottom-label-extended']");
  const unitAmt = unitAmtEl ? (unitAmtEl.innerText || '').trim() : '';
  const unitLbl = unitLblEl ? (unitLblEl.innerText || '').trim() : '';
  const unitPrice = (unitAmt || unitLbl) ? `${unitAmt} ${unitLbl}`.trim() : null;

  return {
    product_id: productId,
    upc: upc,
    name: nameEl ? (nameEl.innerText || '').trim().slice(0, 200) : null,
    brand: brandEl ? (brandEl.innerText || '').trim() || null : null,
    size: uomEl ? (uomEl.innerText || '').trim() || null : null,
    price: priceEl ? (priceEl.innerText || '').trim() || null : null,
    unit_price: unitPrice,
    on_sale: onSale,
    is_buy_again: !!buyAgainEl,
    badges: badges,
  };
}
"""


def _browser(p, headless: bool):
    browser = p.chromium.launch(
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = browser.new_context(
        storage_state=str(SESSION_FILE) if SESSION_FILE.exists() else None,
        viewport={"width": 1366, "height": 900},
    )
    return browser, context


def _cart_quantity(page) -> int:
    try:
        text = page.locator(SELECTORS["cart_bubble"]).first.inner_text(timeout=3000)
    except PlaywrightTimeoutError:
        return 0
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else 0


def _is_logged_in(page) -> bool:
    try:
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
    except PlaywrightTimeoutError:
        return False
    page.wait_for_timeout(2000)
    dismiss_cookie_banner(page)
    try:
        if page.locator(SELECTORS["login_link"]).first.is_visible(timeout=2000):
            return False
    except PlaywrightTimeoutError:
        pass
    try:
        return page.locator(SELECTORS["cart_icon"]).first.is_visible(timeout=3000)
    except PlaywrightTimeoutError:
        return False


def _do_login(page, username: str, password: str) -> None:
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2000)
    dismiss_cookie_banner(page)
    page.fill(SELECTORS["username"], username)
    page.fill(SELECTORS["password"], password)
    page.press(SELECTORS["password"], "Enter")
    page.wait_for_timeout(6000)
    try:
        field = page.locator(SELECTORS["mfa"]).first
        if field.is_visible(timeout=2000):
            code = input(
                "\n[mfa] A verification code field appeared. "
                "Enter the code you received: "
            ).strip()
            field.fill(code)
            field.press("Enter")
            page.wait_for_timeout(5000)
    except PlaywrightTimeoutError:
        pass


def _ensure_logged_in(page) -> None:
    if _is_logged_in(page):
        return
    env = load_env(ENV_FILE)
    username = env.get("HYVEE_USERNAME")
    password = env.get("HYVEE_PASSWORD")
    if not username or not password:
        raise RuntimeError(
            f"Not logged in and HYVEE_USERNAME/HYVEE_PASSWORD not set in {ENV_FILE}"
        )
    _do_login(page, username, password)
    if not _is_logged_in(page):
        raise RuntimeError("Login did not appear to succeed.")
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    page.context.storage_state(path=str(SESSION_FILE))


def _screenshot_on_error(page) -> None:
    try:
        page.screenshot(path=str(ERROR_SCREENSHOT))
        print(f"[error] Screenshot saved to {ERROR_SCREENSHOT}", file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


def cmd_sync_history(args) -> int:
    with sync_playwright() as p:
        browser, context = _browser(p, args.headless)
        page = context.new_page()
        try:
            _ensure_logged_in(page)
            # Establish session context on the purchase-history page first,
            # mirroring Phase-1's pattern of navigating before API calls.
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1500)
            dismiss_cookie_banner(page)

            all_groups = []
            meta = {}
            page_num = 1
            pages_total = None
            while True:
                url = PURCHASE_HISTORY_API.format(page=page_num)
                resp = page.request.get(url)
                if resp.status != 200:
                    raise RuntimeError(
                        f"purchase-history API returned status={resp.status} for page={page_num}"
                    )
                body = resp.json()
                groups = body.get("purchaseGroups", [])
                all_groups.extend(groups)
                meta = body.get("meta", meta)
                pagination = meta.get("pagination", {}) if isinstance(meta, dict) else {}
                pages_total = pagination.get("pagesTotal", page_num)
                if args.max_pages and page_num >= args.max_pages:
                    break
                if page_num >= pages_total:
                    break
                page_num += 1

            combined = {"purchaseGroups": all_groups, "meta": meta}
            orders = sum(
                len(grp.get("purchaseCards", [])) for grp in all_groups
            )

            fd, tmp_path = tempfile.mkstemp(
                prefix="hyvee_history_", suffix=".json"
            )
            with open(fd, "w", encoding="utf-8") as f:
                json.dump(combined, f)

            print(json.dumps({"json_path": tmp_path, "orders": orders}))
            return 0
        except Exception as exc:  # noqa: BLE001
            _screenshot_on_error(page)
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        finally:
            browser.close()


def cmd_search(args) -> int:
    with sync_playwright() as p:
        browser, context = _browser(p, args.headless)
        page = context.new_page()
        try:
            _ensure_logged_in(page)
            url = SEARCH_URL_TMPL.format(term=args.term)
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3000)
            dismiss_cookie_banner(page)

            # Scroll ~4x. The results grid here is actually paginated
            # (30/page) rather than infinite-scroll, but a few extra scrolls
            # are cheap insurance in case any card content lazy-renders.
            for _ in range(4):
                page.mouse.wheel(0, 2000)
                page.wait_for_timeout(1000)

            cards = page.locator(SELECTORS["search_result_card"])
            count = cards.count()

            results = []
            seen_ids = set()
            for i in range(count):
                if len(results) >= args.limit:
                    break
                card = cards.nth(i)
                try:
                    sponsored_el = card.locator(SELECTORS["sponsored"]).first
                    if sponsored_el.count() > 0:
                        text = sponsored_el.inner_text(timeout=1000).strip()
                        if text:
                            continue
                except Exception:  # noqa: BLE001
                    pass
                try:
                    data = card.evaluate(CARD_JS)
                except Exception:  # noqa: BLE001
                    continue
                pid = data.get("product_id")
                if not pid or pid in seen_ids:
                    continue
                seen_ids.add(pid)
                results.append(data)

            print(json.dumps(results))
            return 0
        except Exception as exc:  # noqa: BLE001
            _screenshot_on_error(page)
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        finally:
            browser.close()


def cmd_add(args) -> int:
    with sync_playwright() as p:
        browser, context = _browser(p, args.headless)
        page = context.new_page()
        try:
            _ensure_logged_in(page)

            # Read the "before" bubble count from a fresh nav (verified live:
            # the bubble on the product page itself does not reactively
            # update after an add-to-cart click even after 8+ seconds of
            # waiting — it only reflects the current count right after a
            # navigation/page load).
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1500)
            dismiss_cookie_banner(page)
            before = _cart_quantity(page)

            url = PRODUCT_PAGE_TMPL.format(product_id=args.product_id)
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)
            dismiss_cookie_banner(page)

            qty = max(1, args.qty)
            add_button = page.locator(SELECTORS["add_to_cart"]).first
            increment_button = page.locator(
                SELECTORS["product_qty_increment"]
            ).first

            clicks_remaining = qty
            if add_button.count() > 0:
                # Common path: nothing of this product in the cart yet, so
                # the add-to-cart button is present. First unit: click it —
                # the only add control that exists before this product has
                # anything in the cart.
                add_button.scroll_into_view_if_needed(timeout=10000)
                add_button.click(timeout=15000)
                page.wait_for_timeout(2000)
                clicks_remaining -= 1
            elif increment_button.count() > 0:
                # This product already has a line in the cart (e.g. a prior
                # run/test added it) — the add-to-cart button has already
                # been replaced by the stepper, so there's no "first click"
                # via add-to-cart; go straight to the stepper for every
                # requested unit.
                increment_button.scroll_into_view_if_needed(timeout=10000)
            else:
                raise RuntimeError(
                    "Neither add-to-cart button nor quantity stepper found "
                    "on product page"
                )

            # Remaining units (qty > 1, or all of qty if we started from the
            # stepper branch above): verified live 2026-07-21 that the
            # add-to-cart button is replaced by a quantity stepper
            # (incrementer) after the first click — re-clicking the original
            # add-to-cart selector for units 2..N is a no-op on this site.
            # Prefer the stepper's "+" control (SELECTORS
            # ["product_qty_increment"]); fall back to re-clicking
            # add-to-cart in case a product/page variant never shows the
            # stepper, since that's still strictly better than giving up.
            for _ in range(clicks_remaining):
                try:
                    increment = page.locator(
                        SELECTORS["product_qty_increment"]
                    ).first
                    if increment.count() > 0:
                        increment.click(timeout=10000)
                    else:
                        fallback_button = page.locator(
                            SELECTORS["add_to_cart"]
                        ).first
                        if fallback_button.count() > 0:
                            fallback_button.click(timeout=15000)
                except PlaywrightTimeoutError:
                    pass
                page.wait_for_timeout(2000)

            # Re-navigate to get an accurate "after" bubble count for the
            # same reason as above.
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1500)
            dismiss_cookie_banner(page)
            after = _cart_quantity(page)
            delta = after - before

            print(
                json.dumps(
                    {
                        "added": after > before,
                        "cart_qty": after,
                        "requested_qty": qty,
                        "delta": delta,
                        # The orchestrator (`.claude/agents/hyvee.md`) uses
                        # this as its signal for whether the exact requested
                        # quantity landed — a partial/no increase (e.g. the
                        # stepper never appeared and the fallback re-click
                        # no-op'd) still returns 0 here rather than raising,
                        # so the caller can flag the item instead of the
                        # whole run failing.
                        "qty_matched": delta == qty,
                    }
                )
            )
            return 0
        except Exception as exc:  # noqa: BLE001
            _screenshot_on_error(page)
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        finally:
            browser.close()


# JS that extracts a single cart line item's fields from the DOM. Cart line
# roots don't carry a product-page `<a href>` (navigation is JS/role="link"
# driven, no href attribute) so productId is usually null here; upc (from the
# thumbnail image URL) and description/quantity are reliable. See module
# docstring for why the DOM fallback was chosen over the GraphQL replay.
CART_LINE_JS = r"""
(el) => {
  const linkEl = el.querySelector("a[href*='/aisles-online/p/']");
  const href = linkEl ? linkEl.getAttribute('href') : null;
  let productId = null;
  if (href) {
    const m = href.match(/\/aisles-online\/p\/([^/]+)\//);
    if (m) productId = m[1];
  }
  const imgEl = el.querySelector("img[src*='/products/']");
  const src = imgEl ? imgEl.getAttribute('src') : null;
  let upc = null;
  if (src) {
    const m = src.match(/\/products\/(\d+)\//);
    if (m) upc = m[1];
  }
  const nameEl = el.querySelector("[class*='ProductDescription']");
  const qtyEl = el.querySelector("[data-testid='incrementer-input']");
  let quantity = null;
  if (qtyEl) {
    const raw = qtyEl.value !== undefined ? qtyEl.value : (qtyEl.innerText || '');
    const m = String(raw).match(/\d+/);
    quantity = m ? parseInt(m[0], 10) : null;
  }
  return {
    productId: productId,
    upc: upc,
    description: nameEl ? (nameEl.innerText || '').trim().slice(0, 200) : null,
    quantity: quantity,
  };
}
"""


def cmd_verify_cart(args) -> int:
    with sync_playwright() as p:
        browser, context = _browser(p, args.headless)
        page = context.new_page()
        try:
            _ensure_logged_in(page)
            # This is the cart *review* page (add/remove/verify quantities);
            # it is never used to click through to payment/checkout.
            page.goto(CART_PAGE_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3000)
            dismiss_cookie_banner(page)
            page.wait_for_timeout(1500)

            lines = page.locator(SELECTORS["cart_line_item"])
            n = lines.count()

            items = []
            for i in range(n):
                try:
                    data = lines.nth(i).evaluate(CART_LINE_JS)
                except Exception:  # noqa: BLE001
                    continue
                # Only keep elements that actually have a quantity control —
                # `cart_line_item`'s class is broad and could match nested
                # wrapper divs without cart-line content.
                if data.get("quantity") is not None:
                    items.append(data)

            print(json.dumps(items))
            return 0
        except Exception as exc:  # noqa: BLE001
            _screenshot_on_error(page)
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        finally:
            browser.close()


def _add_headless_flag(sp):
    group = sp.add_mutually_exclusive_group()
    group.add_argument(
        "--headless", dest="headless", action="store_true", default=True,
        help="run headless (default)",
    )
    group.add_argument(
        "--headed", dest="headless", action="store_false",
        help="show the browser window",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    sp_hist = sub.add_parser("sync-history", help="fetch purchase-history API pages, write combined JSON")
    sp_hist.add_argument("--max-pages", type=int, default=None, dest="max_pages")
    _add_headless_flag(sp_hist)
    sp_hist.set_defaults(func=cmd_sync_history)

    sp_search = sub.add_parser("search", help="search products, extract non-sponsored cards")
    sp_search.add_argument("--term", required=True)
    sp_search.add_argument("--limit", type=int, default=12)
    _add_headless_flag(sp_search)
    sp_search.set_defaults(func=cmd_search)

    sp_add = sub.add_parser("add", help="add a product to cart by product id")
    sp_add.add_argument("--product-id", required=True, dest="product_id")
    sp_add.add_argument("--qty", type=int, default=1)
    _add_headless_flag(sp_add)
    sp_add.set_defaults(func=cmd_add)

    sp_verify = sub.add_parser("verify-cart", help="return current cart line items")
    _add_headless_flag(sp_verify)
    sp_verify.set_defaults(func=cmd_verify_cart)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
