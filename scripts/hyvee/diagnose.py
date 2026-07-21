"""Reusable diagnostics toolkit for the Hy-Vee automation.

Supersedes the throwaway `explore.py` harness. When something in
`login_test.py` / `cart_ops.py` breaks, this CLI pinpoints exactly which
constant in `hyvee_web.py` needs updating instead of re-discovering the whole
site from scratch. See `scripts/hyvee/README.md` for the runbook.

Subcommands:
    inspect --url <u>
        Dump every visible input/button/link + all data-testid attributes on
        a page to JSON, plus a full-page screenshot.
    capture-api --url <u> [--match kw]
        Navigate to a URL and record JSON network responses whose URL
        contains `kw` (default: "api").
    dump-card
        Load a milk search, extract the first product card's outerHTML and
        the fields the cart-builder relies on (productId, upc, name, size,
        price, sponsored, isBuyAgain, ...).
    check
        Run every entry in hyvee_web.CRITICAL_CHECKS against the live site
        and print a PASS/FAIL table. Exits non-zero if anything fails.

Usage:
    python scripts/hyvee/diagnose.py inspect --url https://www.hy-vee.com/
    python scripts/hyvee/diagnose.py capture-api --url "<search-url>" --match purchase-history
    python scripts/hyvee/diagnose.py dump-card
    python scripts/hyvee/diagnose.py check

All browser subcommands default to headless (pass --headed to watch).
Requires HYVEE_USERNAME / HYVEE_PASSWORD in the repo-root .env for any
subcommand that needs a logged-in session (reuses state/hyvee_session.json
from login_test.py — run that first if it doesn't exist yet).
"""

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from hyvee_web import CRITICAL_CHECKS, SEARCH_URL_TMPL, SELECTORS

# Force utf-8 stdout so PASS/FAIL glyphs and product names never explode on
# Windows consoles stuck in a legacy codepage.
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"
SESSION_FILE = REPO_ROOT / "state" / "hyvee_session.json"
DEFAULT_OUT = Path(__file__).resolve().parent / "_debug"


def load_env(path: Path) -> dict:
    """Minimal .env parser (stdlib only) — KEY=VALUE lines, ignores blanks/#."""
    values = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def dismiss_cookie_banner(page) -> None:
    """Accept the OneTrust cookie banner if it's covering the page."""
    try:
        btn = page.locator(SELECTORS["cookie_accept"]).first
        if btn.is_visible(timeout=3000):
            btn.click()
            page.wait_for_timeout(1000)
    except PlaywrightTimeoutError:
        pass


def new_context(p, headless: bool, use_session: bool = True):
    browser = p.chromium.launch(
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = browser.new_context(
        storage_state=str(SESSION_FILE)
        if (use_session and SESSION_FILE.exists())
        else None,
        viewport={"width": 1366, "height": 900},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    )
    return browser, context


# JS that snapshots every visible interactive element with useful attributes,
# plus every element carrying a data-testid (ported from explore.py, extended
# with the data-testid inventory).
DUMP_JS = r"""
() => {
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    const s = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' &&
           s.display !== 'none';
  };
  const grab = (el) => ({
    tag: el.tagName.toLowerCase(),
    type: el.getAttribute('type'),
    name: el.getAttribute('name'),
    id: el.id || null,
    placeholder: el.getAttribute('placeholder'),
    ariaLabel: el.getAttribute('aria-label'),
    dataTestId: el.getAttribute('data-testid'),
    autocomplete: el.getAttribute('autocomplete'),
    href: el.getAttribute('href'),
    text: (el.innerText || el.value || '').trim().slice(0, 80),
  });
  const out = { inputs: [], buttons: [], links: [], testIds: [] };
  document.querySelectorAll('input, textarea, select').forEach((el) => {
    if (vis(el)) out.inputs.push(grab(el));
  });
  document.querySelectorAll("button, [role='button']").forEach((el) => {
    if (vis(el)) out.buttons.push(grab(el));
  });
  document.querySelectorAll('a[href]').forEach((el) => {
    if (vis(el)) {
      const g = grab(el);
      if (g.text || (g.href && /account|sign|login|cart/i.test(g.href))) {
        out.links.push(g);
      }
    }
  });
  out.links = out.links.slice(0, 60);
  const seen = new Set();
  document.querySelectorAll('[data-testid]').forEach((el) => {
    const id = el.getAttribute('data-testid');
    if (id && !seen.has(id)) {
      seen.add(id);
      out.testIds.push(id);
    }
  });
  out.testIds.sort();
  return out;
}
"""


def dump_page(page, out_dir: Path, label: str) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    shot = out_dir / f"{label}.png"
    try:
        page.screenshot(path=str(shot), full_page=True)
    except Exception:  # noqa: BLE001
        page.screenshot(path=str(shot))
    data = page.evaluate(DUMP_JS)
    data["url"] = page.url
    data["title"] = page.title()
    (out_dir / f"{label}.json").write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )
    print(f"[dump] {label}: url={page.url}")
    print(f"[dump]   screenshot -> {shot}")
    print(f"[dump]   elements   -> {out_dir / (label + '.json')}")
    print(
        f"[dump]   inputs={len(data['inputs'])} "
        f"buttons={len(data['buttons'])} links={len(data['links'])} "
        f"testIds={len(data['testIds'])}"
    )
    return data


def cmd_inspect(args) -> int:
    out = Path(args.out)
    with sync_playwright() as p:
        browser, context = new_context(p, args.headless, use_session=True)
        page = context.new_page()
        try:
            page.goto(args.url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3000)
            dismiss_cookie_banner(page)
            dump_page(page, out, "inspect")
            return 0
        finally:
            browser.close()


def cmd_capture_api(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    captured = []

    def on_response(response):
        url = response.url
        if args.match.lower() not in url.lower():
            return
        try:
            ctype = response.headers.get("content-type", "")
            if "json" not in ctype:
                return
            body = response.json()
        except Exception:  # noqa: BLE001
            return
        captured.append({"url": url, "status": response.status, "body": body})
        print(f"[capture-api] {response.status} {url}")

    with sync_playwright() as p:
        browser, context = new_context(p, args.headless, use_session=True)
        page = context.new_page()
        page.on("response", on_response)
        try:
            page.goto(args.url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(6000)
            dismiss_cookie_banner(page)
            page.wait_for_timeout(3000)
            out_file = out / "captured_api.json"
            out_file.write_text(json.dumps(captured, indent=2), encoding="utf-8")
            print(f"[capture-api] {len(captured)} matching response(s) -> {out_file}")
            return 0
        finally:
            browser.close()


# JS that extracts the fields cart-builder logic needs from a single
# UniversalProductCard element (ported from the discovery spike's findings).
CARD_JS = r"""
(card) => {
  const q = (sel) => card.querySelector(sel);
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
  const brandEl = card.querySelector("[data-testid='BRAND']");
  const sponsoredEl = card.querySelector("[data-testid='sponsored-text']");
  const buyAgainEl = card.querySelector("[data-testid='isBuyAgain']");
  const badges = Array.from(card.querySelectorAll("[data-testid^='badge-']"))
    .map((b) => b.getAttribute('data-testid'));
  return {
    productId,
    upc,
    name: (card.innerText || '').trim().slice(0, 200),
    brand: brandEl ? (brandEl.innerText || '').trim() : null,
    size: uomEl ? (uomEl.innerText || '').trim() : null,
    price: priceEl ? (priceEl.innerText || '').trim() : null,
    sponsored: !!(sponsoredEl && (sponsoredEl.innerText || '').trim()),
    isBuyAgain: !!buyAgainEl,
    badges,
    outerHTML: card.outerHTML.slice(0, 5000),
  };
}
"""


def cmd_dump_card(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    url = SEARCH_URL_TMPL.format(term=args.term)
    with sync_playwright() as p:
        browser, context = new_context(p, args.headless, use_session=True)
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)
            dismiss_cookie_banner(page)
            card = page.locator(SELECTORS["product_card"]).first
            card.scroll_into_view_if_needed(timeout=10000)
            page.wait_for_timeout(1000)
            data = card.evaluate(CARD_JS)
            out_file = out / "dump_card.json"
            out_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
            print(f"[dump-card] term={args.term!r} -> {out_file}")
            print(json.dumps({k: v for k, v in data.items() if k != "outerHTML"}, indent=2))
            return 0
        finally:
            browser.close()


def _check_selector(page, check: dict) -> tuple:
    try:
        page.goto(check["url"], wait_until="domcontentloaded", timeout=45000)
    except PlaywrightTimeoutError as exc:
        return False, f"navigation timeout: {exc}"
    page.wait_for_timeout(2500)
    dismiss_cookie_banner(page)
    # wait_for_selector actively polls for visibility (unlike is_visible(),
    # which checks once and returns immediately) — needed because product
    # cards / search results render asynchronously after DOMContentLoaded.
    try:
        page.wait_for_selector(check["selector"], state="visible", timeout=10000)
        return True, "visible"
    except PlaywrightTimeoutError:
        return False, "selector not visible"


def _check_api(page, check: dict) -> tuple:
    method = check.get("method", "get")
    try:
        if method == "post":
            resp = page.request.post(check["url"])
        else:
            resp = page.request.get(check["url"])
    except Exception as exc:  # noqa: BLE001
        return False, f"request error: {exc}"

    status = resp.status

    if method == "post":
        # Lenient: this endpoint's real GraphQL contract isn't built until
        # Task 8 (cart_ops.py verify-cart exercises it for real). Anything
        # other than a 404 means the endpoint exists.
        if status == 404:
            return False, f"status={status} (endpoint missing)"
        return True, f"status={status} (endpoint exists; lenient POST check)"

    if status != 200:
        return False, f"status={status}"

    expect_key = check.get("expect_key")
    if expect_key:
        try:
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            return False, f"status=200 but body not JSON: {exc}"
        if expect_key not in body:
            return False, f"status=200 but missing key '{expect_key}'"
        return True, f"status=200, has '{expect_key}'"

    return True, "status=200"


def cmd_check(args) -> int:
    results = []
    with sync_playwright() as p:
        browser, context = new_context(p, args.headless, use_session=True)
        page = context.new_page()
        # The login form only renders for a signed-out visitor — Auth0
        # redirects a session that's already authenticated straight past it.
        # Since diagnose.py normally runs with a saved (logged-in) session,
        # check that one dependency in its own logged-out context so the
        # result reflects the DOM, not our current auth state.
        guest_browser, guest_context = new_context(p, args.headless, use_session=False)
        guest_page = guest_context.new_page()
        try:
            for check in CRITICAL_CHECKS:
                if check["kind"] == "selector":
                    check_page = guest_page if check["name"] == "login form" else page
                    ok, detail = _check_selector(check_page, check)
                elif check["kind"] == "api":
                    ok, detail = _check_api(page, check)
                else:
                    ok, detail = False, f"unknown check kind {check['kind']!r}"
                results.append((check["name"], ok, detail))
        finally:
            guest_browser.close()
            browser.close()

    name_width = max(len(name) for name, _, _ in results) + 2
    print(f"\n{'DEPENDENCY'.ljust(name_width)}RESULT  DETAIL")
    print("-" * (name_width + 60))
    any_fail = False
    for name, ok, detail in results:
        status = "PASS" if ok else "FAIL"
        if not ok:
            any_fail = True
        print(f"{name.ljust(name_width)}{status:<8}{detail}")

    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{total} checks passed.")
    return 1 if any_fail else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    def add_common(sp):
        sp.add_argument("--out", default=str(DEFAULT_OUT))
        headless_group = sp.add_mutually_exclusive_group()
        headless_group.add_argument(
            "--headless", dest="headless", action="store_true", default=True,
            help="run headless (default)",
        )
        headless_group.add_argument(
            "--headed", dest="headless", action="store_false",
            help="show the browser window",
        )

    sp_inspect = sub.add_parser("inspect", help="dump inputs/buttons/links/testids for a URL")
    sp_inspect.add_argument("--url", required=True)
    add_common(sp_inspect)
    sp_inspect.set_defaults(func=cmd_inspect)

    sp_capture = sub.add_parser("capture-api", help="record JSON network responses matching a keyword")
    sp_capture.add_argument("--url", required=True)
    sp_capture.add_argument("--match", default="api")
    add_common(sp_capture)
    sp_capture.set_defaults(func=cmd_capture_api)

    sp_card = sub.add_parser("dump-card", help="extract fields from the first product card on a search")
    sp_card.add_argument("--term", default="milk")
    add_common(sp_card)
    sp_card.set_defaults(func=cmd_dump_card)

    sp_check = sub.add_parser("check", help="run all CRITICAL_CHECKS against the live site")
    add_common(sp_check)
    sp_check.set_defaults(func=cmd_check)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
