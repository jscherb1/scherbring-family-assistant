"""One-shot 'ask the agent' box: the dashboard's only write path.

The message is injected into the bridge inbox as a Telegram-channel row for the owner's
chat, so the agent's reply is delivered in Telegram by the existing send loop and shares
the Telegram conversation's session. No chat UI and nothing to keep in sync.
Access control is the network (Tailscale-only bind), plus a same-origin check and a
small rate limit. Single user (Justin) for now.
"""
import sys
import time
import uuid
from collections import deque
from urllib.parse import urlparse

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ..data import common
from ..web import render

sys.path.insert(0, str(common.SCRIPTS_DIR))
from bridge_store import BridgeStore, session_key_for  # noqa: E402
from paths import get_env  # noqa: E402

router = APIRouter()

MAX_CHARS = 2000
RATE_LIMIT = 10  # messages per minute
ASK_USER = "Justin"
_recent: deque[float] = deque()


def _chat_id() -> str:
    explicit = (get_env("DASHBOARD_ASK_CHAT_ID") or "").strip()
    if explicit:
        return explicit
    return (get_env("BRIDGE_ALLOWED_CHAT_IDS") or "").split(",")[0].strip()


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin:
        host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
        return urlparse(origin).netloc == host
    return request.headers.get("sec-fetch-site", "same-origin") in ("same-origin", "none")


def _rate_limited() -> bool:
    now = time.monotonic()
    while _recent and now - _recent[0] > 60:
        _recent.popleft()
    if len(_recent) >= RATE_LIMIT:
        return True
    _recent.append(now)
    return False


def submit(request: Request, text: str, via: str = "web") -> tuple[bool, str]:
    """Validate and enqueue. Returns (ok, message for the user).

    The same-origin check only applies to browser submissions; non-browser clients (the iOS
    Shortcut) rely on the Tailscale-only bind.
    """
    text = (text or "").strip()
    if via == "web" and not _same_origin(request):
        return False, "Blocked: the request didn't come from this site."
    if not text:
        return False, "Type something to send first."
    if len(text) > MAX_CHARS:
        return False, f"That's too long ({len(text)} characters; the limit is {MAX_CHARS})."
    chat = _chat_id()
    if not chat:
        return False, "No Telegram chat is configured for the app (BRIDGE_ALLOWED_CHAT_IDS is empty)."
    if _rate_limited():
        return False, "Slow down a little: too many messages in the last minute."
    store = BridgeStore(common.state_dir() / "inbox.db")
    try:
        row_id = store.add_inbound(
            "telegram", chat, text, external_id=f"web-{uuid.uuid4()}",
            session_key=session_key_for(chat),
            payload={"user": ASK_USER, "via": via, "message_id": "", "date": time.time()})
    finally:
        store.close()
    if row_id is None:
        return False, "Couldn't queue that message. Try again."
    return True, "Sent. The reply will arrive in Telegram."


@router.post("/ask", include_in_schema=False)
def ask(request: Request, text: str = Form("")):
    ok, message = submit(request, text)
    if request.headers.get("hx-request"):
        return render(request, "summary/_ask_result.html", "summary", "overview", "Summary",
                      status_code=200 if ok else 400, ok=ok, message=message, text=text)
    target = "/?sent=1" if ok else "/?ask_error=" + message.replace(" ", "+")
    return RedirectResponse(target, status_code=303)


@router.post("/api/ask", include_in_schema=False)
async def api_ask(request: Request):
    """JSON endpoint for the iOS Shortcut: form field or JSON body `text`."""
    if "json" in request.headers.get("content-type", ""):
        try:
            body = await request.json()
        except ValueError:
            body = {}
        text = body.get("text", "") if isinstance(body, dict) else ""
    else:
        text = (await request.form()).get("text", "")
    ok, message = submit(request, str(text), via="ios")
    return JSONResponse({"ok": ok, "message": message}, status_code=200 if ok else 400)
