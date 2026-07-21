# Hy-Vee Data Discovery (read-only spike) — Findings

Date: 2026-07-21. Read-only reconnaissance of what data Hy-Vee Aisles Online exposes,
run against the live logged-in account to inform the cart-builder decision model and
storage schema. No cart/account changes were made. This is the input to the
decision-logic + schema design.

## 1. Two clean JSON APIs exist (prefer these over DOM scraping)

### Purchase history — `GET /aisles-online/api/purchase-history/online?page=N&pageSize=20`
- Returns JSON, **paginated** (`meta.pagination`: page, pageSize, `pagesTotal`).
- This account: **4 pages, ~20 orders/page (~80 orders of history)**; page 1 alone
  references **75 distinct products**.
- Shape:
  ```
  purchaseGroups[]            # grouped by month, e.g. "July 2026"
    purchaseCards[]           # one per order
      purchaseId  (uuid)
      date        (YYYY-MM-DD)
      summary     ("$75.45 - Your order was picked up")
      items[]                 # product thumbnails
        image.altText  -> product NAME  ("On the Border Cantina Thins")
        image.url      -> contains UPC  (.../products/00781138715157/...)
  ```
- Gives us, per product: **name + UPC + which orders/dates it appears in** →
  frequency & recency inventory. This is the backbone of "remember which milk I buy."
- NOT in this summary: per-line quantity and price. A per-order detail endpoint was
  not found by URL guessing (`/purchase-history/online/{id}` → 404) or by clicking
  through; likely a GraphQL query. **Deferred** — not needed for the first inventory.
- `?type=online` vs other types (in-store?) is a known query param worth exploring later.

### Active cart — `POST /aisles-online/api/graphql/three-legged/getActiveCart`
- GraphQL. Returns cart line items with **productId, upc, description, quantity**.
- Use this to read/verify cart contents robustly instead of scraping the cart bubble.

## 2. Product data available per search-result card (`[data-testid='UniversalProductCard']`)

Reliably extractable:
- **productId** — from product link `/aisles-online/p/{productId}/{slug}` (stable).
- **UPC/GTIN** — from image URL `.../products/{upc}/...` (stable; **joins to purchase history**).
- **name** — `.styles__ProductDescription...` (also in add-to-cart aria-label).
- **size / unit of measure** — `[class*='UnitOfMeasure']` (e.g. "0.5 gal", "12 fl oz").
- **price** — `[data-testid='product-card-price-amount']` (e.g. "$2.99").
- **unit price ($/oz etc.)** — price suffix/bottom labels (present on many items).
- **sale / was-now price** — price top/bottom labels (present on sale items).
- **promotions** — badges: `[data-testid='badge-DEAL']` (aria-label carries full deal text),
  `badge-SALE`, plus `badge-SNAP` (SNAP eligibility).
- **isBuyAgain** — `[data-testid='isBuyAgain']` present when the account bought it before.
- **sponsored** — `[data-testid='sponsored-text']` (non-empty when it's an ad — exclude these).

NOT reliably structured:
- **brand** — the `[data-testid='BRAND']` element is usually empty on search cards; brand is
  effectively embedded in the product name and would need parsing. Implication: prefer to
  "remember" the exact **productId/UPC** rather than rely on a brand string.

Important behavioral caveats for the decision logic:
- **Search results lazy-load** — only ~15 cards are in the DOM until you scroll; must scroll
  to enumerate all results (75+ for "milk").
- **Results are not relevance-first** — a "milk" search leads with sponsored sodas/juices and
  tangential items ("...Milk Chocolate... Ice Cream Bars"). **"Add the first result" is unsafe.**
  Item→product matching must filter sponsored, match on name/size, and prefer purchase-history
  matches.

## 3. What this means for the decision model + schema

- **Primary signal = purchase history.** For a Todoist line like "milk", the best match is the
  exact productId/UPC the user actually buys repeatedly (frequency + recency from the history API).
- **Preferences are fallbacks/tie-breakers** when there's no confident history match: preferred
  brand (string match in name), price ceiling, prefer-on-sale, preferred size.
- Suggested data to store per generic item ("milk", "bananas", ...):
  - resolved preferred product: productId, upc, name, size (learned from history / confirmed by user)
  - fallback preference weights: brand vs cost vs promo priority, max price, preferred size
  - history-derived stats: times bought, last bought date (from the history API)
- The cart-build flow becomes: read Todoist list → for each item, resolve to a product
  (history match first, else search+preferences) → add by productId → verify via getActiveCart →
  report a review summary. Never place the order.
