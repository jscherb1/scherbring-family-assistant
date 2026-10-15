"""Remote Hy-Vee sign-in: drive a server-side browser from any device (phone included).

Hy-Vee's login can show a CAPTCHA that only a person can solve, and `login_test.py --headed` needs the machine's
screen. This runs the same login in a headless browser on the server and lets you see it (screenshots) and
tap/type into it from the dashboard, so you can solve the CAPTCHA from your phone. When the sign-in succeeds the
session is saved exactly like login_test.py does, so nothing else changes.

One session at a time, closed after IDLE_SECONDS of no input. All Playwright calls run on one worker thread
(its sync API is thread-bound); callers use the thin methods below, which are safe from request threads.
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

VIEWPORT = {"width": 900, "height": 760}
IDLE_SECONDS = 600
MAX_TYPED = 200
ALLOWED_KEYS = {"Enter", "Tab", "Backspace", "Escape", "ArrowDown", "ArrowUp"}


class RemoteLoginError(Exception):
    pass


class RemoteLogin:
    def __init__(self, session_file: Path | None = None, launcher=None):
        self._session_file = session_file
        self._launcher = launcher  # test seam: callable(playwright) -> (browser, context, page)
        self._cmds: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._state = {"phase": "idle", "message": "", "url": "", "started": 0.0}
        self._last_input = 0.0
        self._lock = threading.Lock()

    # -- public API (any thread) -------------------------------------------------------------------
    def start(self) -> dict:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.status()
            self._cmds = queue.Queue()
            self._state = {"phase": "starting", "message": "Opening the sign-in page...", "url": "", "started": time.time()}
            self._last_input = time.time()
            self._thread = threading.Thread(target=self._run, name="hyvee-remote-login", daemon=True)
            self._thread.start()
        return self.status()

    def status(self) -> dict:
        s = dict(self._state)
        s["active"] = bool(self._thread and self._thread.is_alive())
        return s

    def screenshot(self) -> bytes | None:
        return self._call("shot")

    def click(self, x: float, y: float) -> None:
        self._call("click", float(x), float(y))

    def type_text(self, text: str) -> None:
        if len(text) > MAX_TYPED:
            raise RemoteLoginError("Too much text.")
        self._call("type", text)

    def press(self, key: str) -> None:
        if key not in ALLOWED_KEYS:
            raise RemoteLoginError("Key not allowed.")
        self._call("press", key)

    def fill_saved_credentials(self) -> None:
        self._call("fill")

    def finish(self) -> dict:
        """Check whether the browser is now signed in; save the session if so."""
        self._call("finish")
        return self.status()

    def stop(self) -> None:
        self._cmds.put(("stop", (), None))

    # -- plumbing ------------------------------------------------------------------------------------
    def _call(self, name: str, *args):
        if not (self._thread and self._thread.is_alive()):
            raise RemoteLoginError("No sign-in session is running. Start one first.")
        self._last_input = time.time() if name != "shot" else self._last_input
        out: queue.Queue = queue.Queue(maxsize=1)
        self._cmds.put((name, args, out))
        try:
            ok, val = out.get(timeout=60)
        except queue.Empty:
            raise RemoteLoginError("The browser did not respond.")
        if not ok:
            raise RemoteLoginError(str(val))
        return val

    def _run(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
            import hyvee_session as hs
            import login_test as lt
        except Exception as exc:  # noqa: BLE001
            self._state.update(phase="error", message=f"Browser tooling unavailable: {exc}")
            return
        session_file = self._session_file or hs.SESSION_FILE
        try:
            with sync_playwright() as p:
                if self._launcher:
                    browser, context, page = self._launcher(p)
                else:
                    browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
                    context = browser.new_context(viewport=VIEWPORT)
                    page = context.new_page()
                try:
                    if not self._launcher:
                        hs.goto_login(page)
                        hs.dismiss_cookie_banner(page)
                    self._state.update(phase="waiting", message="Sign in below. Solve the CAPTCHA if one appears.")
                    self._loop(page, context, session_file, hs, lt)
                finally:
                    browser.close()
        except Exception as exc:  # noqa: BLE001
            self._state.update(phase="error", message=f"Sign-in browser failed: {exc}")

    def _loop(self, page, context, session_file, hs, lt) -> None:
        while True:
            if time.time() - self._last_input > IDLE_SECONDS:
                self._state.update(phase="closed", message="Closed after 10 minutes with no input.")
                return
            try:
                name, args, out = self._cmds.get(timeout=1)
            except queue.Empty:
                self._state["url"] = page.url
                continue
            try:
                val = self._handle(name, args, page, context, session_file, hs, lt)
                if out:
                    out.put((True, val))
            except Exception as exc:  # noqa: BLE001
                if out:
                    out.put((False, exc))
            if name == "stop" or self._state["phase"] in ("done", "closed"):
                return

    def _handle(self, name, args, page, context, session_file, hs, lt):
        if name == "shot":
            self._state["url"] = page.url
            return page.screenshot(type="jpeg", quality=60)
        if name == "click":
            page.mouse.click(args[0], args[1])
        elif name == "type":
            page.keyboard.type(args[0], delay=20)
        elif name == "press":
            page.keyboard.press(args[0])
        elif name == "fill":
            env = hs.load_env()
            user, pw = env.get("HYVEE_USERNAME"), env.get("HYVEE_PASSWORD")
            if not user or not pw:
                raise RemoteLoginError("HYVEE_USERNAME / HYVEE_PASSWORD are not configured.")
            page.fill(hs.SELECTORS["username"], user)
            page.fill(hs.SELECTORS["password"], pw)
            page.press(hs.SELECTORS["password"], "Enter")
            page.wait_for_timeout(4000)
        elif name == "finish":
            if "identity.hy-vee.com" in page.url:
                self._state["message"] = "Still on the sign-in page. Finish signing in first."
                return None
            if lt.is_logged_in(page):
                session_file.parent.mkdir(parents=True, exist_ok=True)
                context.storage_state(path=str(session_file))
                try:
                    from credential_check import record_login
                    record_login("hyvee")
                except ImportError:
                    pass
                self._state.update(phase="done", message="Signed in. The Hy-Vee session is saved.")
            else:
                self._state["message"] = "Not signed in yet."
        elif name == "stop":
            self._state.update(phase="closed", message="Stopped.")
        self._state["url"] = page.url
        return None


_session = RemoteLogin()


def get_session() -> RemoteLogin:
    return _session
