"""Non-interactive exploration harness for iterating on Hy-Vee selectors.

Unlike login_test.py, this never blocks on input(). It navigates, dumps the
real interactive elements (inputs/buttons/links) as JSON, and saves full-page
screenshots so the developer/agent can inspect the actual DOM and refine
selectors. Drive it a step at a time via the CLI arg.

Usage:
    python scripts/hyvee/explore.py inspect-login
    python scripts/hyvee/explore.py login
    python scripts/hyvee/explore.py search
    python scripts/hyvee/explore.py cart

Artifacts are written to the directory given by --out (default: this folder's
_debug/). Requires HYVEE_USERNAME / HYVEE_PASSWORD in the repo-root .env.
"""

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"
SESSION_FILE = REPO_ROOT / "state" / "hyvee_session.json"
DEFAULT_OUT = Path(__file__).resolve().parent / "_debug"

HOME_URL = "https://www.hy-vee.com/aisles-online/search"
SIGN_IN_URL = "https://www.hy-vee.com/main/login"

TEST_ITEM = "milk"


def load_env(path: Path) -> dict:
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


# JS that snapshots every visible interactive element with useful attributes.
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
  const out = { inputs: [], buttons: [], links: [] };
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
    print(f"[dump]   inputs={len(data['inputs'])} "
          f"buttons={len(data['buttons'])} links={len(data['links'])}")
    return data


def new_context(p, headless: bool, use_session: bool):
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


def cmd_inspect_login(args) -> int:
    out = Path(args.out)
    with sync_playwright() as p:
        browser, context = new_context(p, args.headless, use_session=False)
        page = context.new_page()
        try:
            page.goto(SIGN_IN_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)
            dump_page(page, out, "01_login_page")
            return 0
        finally:
            browser.close()


def cmd_login(args) -> int:
    out = Path(args.out)
    env = load_env(ENV_FILE)
    username, password = env.get("HYVEE_USERNAME"), env.get("HYVEE_PASSWORD")
    if not username or not password:
        print(f"ERROR: set HYVEE_USERNAME/HYVEE_PASSWORD in {ENV_FILE}",
              file=sys.stderr)
        return 2
    with sync_playwright() as p:
        browser, context = new_context(p, args.headless, use_session=False)
        page = context.new_page()
        try:
            page.goto(SIGN_IN_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)
            dump_page(page, out, "01_login_page")

            # Fill using the selectors passed in (discovered from inspect step).
            page.fill(args.user_selector, username)
            page.fill(args.pass_selector, password)
            dump_page(page, out, "02_filled")
            # Submit via Enter (avoids overlay intercepting a button click);
            # fall back to the Continue button if needed.
            try:
                page.press(args.pass_selector, "Enter")
            except Exception:  # noqa: BLE001
                page.get_by_role("button", name="Continue").first.click(
                    timeout=10000
                )
            page.wait_for_timeout(8000)
            dump_page(page, out, "03_after_submit")

            # Report likely MFA fields for the human to react to.
            print(f"[login] post-login url={page.url}")
            context.storage_state(path=str(SESSION_FILE))
            print(f"[login] session saved -> {SESSION_FILE}")
            return 0
        except Exception as exc:  # noqa: BLE001
            dump_page(page, out, "99_login_error")
            print(f"FAIL: {exc}", file=sys.stderr)
            return 1
        finally:
            browser.close()


def cmd_search(args) -> int:
    out = Path(args.out)
    with sync_playwright() as p:
        browser, context = new_context(p, args.headless, use_session=True)
        page = context.new_page()
        try:
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)
            dump_page(page, out, "10_home")
            if args.search_selector:
                box = page.locator(args.search_selector).first
                box.click()
                box.fill(TEST_ITEM)
                box.press("Enter")
                page.wait_for_timeout(6000)
                dump_page(page, out, "11_search_results")
            return 0
        except Exception as exc:  # noqa: BLE001
            dump_page(page, out, "99_search_error")
            print(f"FAIL: {exc}", file=sys.stderr)
            return 1
        finally:
            browser.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "command",
        choices=["inspect-login", "login", "search", "cart"],
    )
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--headless", action="store_true",
                    help="run headless (default: headed)")
    ap.add_argument("--user-selector", default="input[type='email']")
    ap.add_argument("--pass-selector", default="input[type='password']")
    ap.add_argument("--submit-selector", default="button[type='submit']")
    ap.add_argument("--search-selector", default="")
    args = ap.parse_args()

    dispatch = {
        "inspect-login": cmd_inspect_login,
        "login": cmd_login,
        "search": cmd_search,
        "cart": cmd_search,  # placeholder; refine once search works
    }
    return dispatch[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
