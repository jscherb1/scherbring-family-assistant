#!/usr/bin/env python3
"""Cheap read-only probe: does the saved Monarch session still work?

Run with the vendored server's interpreter (it has the monarch libraries):
    vendor/monarch-mcp-server/.venv/bin/python scripts/monarch_probe.py

Prints one JSON line. {"status": "ok"} when an account listing succeeds,
{"status": "dead"} for an authentication failure, and {"status": "unknown"}
for anything else (network trouble, API outage), so callers never treat a
transient failure as an expired login. Prints no account data.
"""

import asyncio
import json
import logging


async def probe() -> dict:
    from monarch_mcp_server.client import get_monarch_client

    try:
        client = await get_monarch_client()
    except Exception as exc:  # noqa: BLE001 - no usable saved session
        return {"status": "dead", "detail": f"no usable session: {type(exc).__name__}"}
    try:
        data = await client.get_accounts()
    except Exception as exc:  # noqa: BLE001
        text = f"{type(exc).__name__}: {exc}".lower()
        auth = any(w in text for w in ("401", "unauthor", "forbidden", "403", "login", "token", "session"))
        return {"status": "dead" if auth else "unknown", "detail": type(exc).__name__}
    accounts = (data or {}).get("accounts") if isinstance(data, dict) else None
    return {"status": "ok" if accounts else "unknown", "detail": "accounts listed" if accounts else "empty response"}


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    print(json.dumps(asyncio.run(probe())))
