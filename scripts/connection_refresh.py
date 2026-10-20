#!/usr/bin/env python3
"""Desktop-shortcut helper: real check first, login only if it is really down, real check again.

    python scripts/connection_refresh.py monarch|drive

Used by "Monarch Login.bat" and "Google Drive Login.bat". Exit 0 when the connection works at the end.
"""

from __future__ import annotations

import subprocess
import sys

from paths import REPO_ROOT

import credential_check as cc

LOGIN_CMDS = {
    "monarch": (
        [str(REPO_ROOT / "vendor/monarch-mcp-server/.venv/bin/python"), "login_setup.py"],
        REPO_ROOT / "vendor" / "monarch-mcp-server",
        "Choose option 1 and paste the cookie header from app.monarch.com.",
    ),
    "drive": (
        [sys.executable, "scripts/drive_upload.py", "auth"],
        REPO_ROOT,
        "Open the link that is printed below in your browser and approve.",
    ),
}


def main(argv: list[str]) -> int:
    key = argv[0] if argv else ""
    if key not in LOGIN_CMDS:
        print(f"usage: connection_refresh.py {'|'.join(LOGIN_CMDS)}", file=sys.stderr)
        return 2
    label = cc.SYSTEMS[key].label
    print(f"Checking {label} with a real request...")
    status = cc.PROBES[key]()
    if status == "ok":
        print(f"{label} is already working. Nothing to do.")
        return 0
    if status == "unknown":
        print(f"{label} could not be checked (network trouble?). Logging in anyway.")
    else:
        print(f"{label} is down.")
    cmd, cwd, hint = LOGIN_CMDS[key]
    print(hint + "\n")
    subprocess.run(cmd, cwd=cwd)
    if key == "monarch":
        cc.record_login("monarch")
    print(f"\nChecking {label} again...")
    status = cc.PROBES[key]()
    print(f"{label}: {'working' if status == 'ok' else 'STILL NOT WORKING (' + status + ')'}")
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
